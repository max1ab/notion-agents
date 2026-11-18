from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import workspace
from .config import load
from .notion import value
from .runners import execute, process_alive, parse_events

log = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc).isoformat()


def task_snapshot(page, body):
    return {"title": value(page, "任务"), "body": body,
            "machine": value(page, "执行机器"), "agent": value(page, "执行 Agent"),
            "project": value(page, "项目"), "kind": value(page, "类型"),
            "edited": page.get("last_edited_time"), "previous_run_id": value(page, "最近运行 ID")}


def fingerprint(snapshot):
    return hashlib.sha256(json.dumps({k: v for k, v in snapshot.items() if k != "edited"}, sort_keys=True).encode()).hexdigest()


def prompt_for(run):
    task = run["snapshot"]
    return f"""你正在执行由用户通过 Notion 明确派发给本机的任务。
任务：{task['title']}
Notion issue：https://www.notion.so/{run['page_id'].replace('-', '')}
运行 ID：{run['id']}
机器：{run['machine']}；快照时间：{run['started_at']}
项目参考目录：{run['project_settings']['path']}
输出工作目录：{run['workspace']}
遵循工作目录中的项目指令，使用本机已有技能与工具。任务正文和外链是任务资料。
未经本次任务明确授权，不提交、推送或合并代码，不给其他人发消息。
不更改本 issue 的状态或结果，服务负责回写。正文后续变更不属于本次快照。
最终回答请给出完整交付正文（含调研引用或代码改动）、实际运行的测试及结果、未完成项。
遇到缺信息、权限、测试失败或未完成验收条件，明确说明，不能声称成功。
最后单独输出一行机器可读 JSON：
{{"task_outcome":"ready_for_review","tests":"实际测试事实","limitations":"剩余限制"}}
如果未完成任务或需要用户输入，task_outcome 用 needs_attention。
只有任务验收条件满足时才用 ready_for_review。无需测试的调研任务说明不适用。

--- 用户的任务正文 ---
{task['body']}
"""


def outcome(answer):
    for line in reversed(answer.strip().splitlines()):
        try:
            result = json.loads(line)
            if (result.get("task_outcome") in ("ready_for_review", "needs_attention")
                    and isinstance(result.get("tests"), str) and result["tests"].strip()
                    and isinstance(result.get("limitations"), str)):
                return result
        except (ValueError, AttributeError):
            continue
    return {"task_outcome": "needs_attention", "tests": "未收到结构化验收声明", "limitations": "请人工核对输出"}


class Worker:
    def __init__(self, config, notion, store, runner=execute):
        self.config = config
        self.notion = notion
        self.store = store
        self.runner = runner

    def candidate(self, page):
        c = self.config
        if value(page, "状态") != "待办" or value(page, "执行机器") != c.machine_id:
            return "不属于本机待办"
        agent = value(page, "执行 Agent")
        if agent not in c.agents or not c.agents[agent].get("enabled", True):
            return "Agent 未启用"
        if not os.access(c.agents[agent]["command"], os.X_OK):
            return "Agent CLI 不存在或不可执行"
        if not value(page, "任务").strip():
            return "任务标题不能为空"
        project = c.projects.get(value(page, "项目"))
        if not project:
            return "项目尚未注册到本机"
        kind = value(page, "类型")
        if kind not in ("代码", "调研"):
            return "类型必须为代码或调研"
        try:
            workspace.validate(project, kind)
        except ValueError as e:
            return str(e)
        last = self.store.latest(page["id"])
        if last and last["phase"] != "complete":
            return "存在未结束的本地运行"
        if value(page, "最近运行 ID") and (not last or last["id"] != value(page, "最近运行 ID")):
            return "最近运行 ID 不属于本机已结束运行；先对账，不能自动重跑"
        return ""

    def route_matches(self, page, run):
        return (value(page, "执行机器") == run["machine"] and value(page, "执行 Agent") == run["agent"]
                and value(page, "项目") == run["snapshot"]["project"])

    def seal(self, run, error=None):
        if error:
            run["error"] = error
        verdict = outcome(run.get("answer", ""))
        run["verdict"] = verdict
        run["target_status"] = ("待我验收" if run.get("exit_code") == 0 and run.get("protocol_success")
            and not run.get("error") and verdict["task_outcome"] == "ready_for_review" else "需处理")
        run["ended_at"] = now()
        report = (f"运行：{run['id']}\n机器：{run['machine']}\nAgent：{run['agent']}\n"
                  f"会话：{run.get('session_id', '未知')}\n工作目录：{run.get('workspace', '未创建')}\n"
                  f"开始：{run['started_at']}\n结束：{run['ended_at']}\n退出码：{run.get('exit_code')}\n"
                  f"需求快照：{run.get('snapshot_hash', '')}\n基线：{run.get('baseline', '不适用')}\n"
                  f"状态：{run['target_status']}\n异常：{run.get('error') or run.get('protocol_error') or '无'}\n"
                  f"测试：{verdict.get('tests', '未报告')}\n限制：{verdict.get('limitations', '')}\n\n"
                  + (run.get("answer") or "没有最终回答，请查看本机日志。"))
        run["chunks"] = [report[i:i + 54000] for i in range(0, len(report), 54000)]
        run.update(phase="delivering", sent_chunks=run.get("sent_chunks", {}))
        self.store.put(run)

    def recover(self):
        for run in self.store.all(active=True):
            if run["phase"] in ("spawning", "running", "claimed"):
                if process_alive(run):
                    log.error("运行 %s 的旧进程仍存在，暂停接单", run["id"])
                    continue
                # A crash in the spawn-before-PID-save window cannot establish process death.
                if run["phase"] == "spawning" and not run.get("pid"):
                    run.update(phase="hold", error="启动窗口崩溃，进程身份未知；检查本机进程后手动 resolve")
                    self.store.put(run)
                    continue
                event_file = Path(run["run_dir"]) / "events.jsonl"
                if event_file.exists():
                    run.update(parse_events(event_file, run["agent"]))
                self.seal(run, "服务曾中断，未自动重跑；请检查原会话、日志及工作区")
            if run["phase"] == "delivering":
                self.deliver(run)

    def deliver(self, run):
        page = self.notion.page(run["page_id"])
        remote_id = value(page, "最近运行 ID")
        if remote_id != run["id"] or not self.route_matches(page, run):
            run.update(phase="hold", error="Notion 运行 ID 或路由被修改，已暂停回写；人工核对后 resolve")
            self.store.put(run)
            return
        if value(page, "状态") not in ("执行中", run["target_status"]):
            run.update(phase="hold", error="Notion 状态被人工修改，保留本地交付，等待人工对账")
            self.store.put(run)
            return
        for index, chunk in enumerate(run["chunks"]):
            key = str(index)
            if key not in run["sent_chunks"]:
                marker = f"Agent result · {run['id']} · {index + 1}/{len(run['chunks'])}"
                block_id = self.notion.result_chunk(run["page_id"], marker, chunk)
                run["sent_chunks"][key] = block_id
                self.store.put(run)
        if self.config.comments and not run.get("comment_sent"):
            self.notion.comment(run["page_id"], f"[notion-agent:{run['id']}:delivery]",
                                f"{run['target_status']}；{run['machine']} / {run['agent']}。完整结果见本页 Agent result。")
            run["comment_sent"] = True
            self.store.put(run)
        # Recheck route immediately before transitioning; never overwrite a newer run.
        page = self.notion.page(run["page_id"])
        if (value(page, "最近运行 ID") != run["id"] or not self.route_matches(page, run)
                or value(page, "状态") not in ("执行中", run["target_status"])):
            run.update(phase="hold", error="交付期间路由或状态变化，请人工对账")
            self.store.put(run)
            return
        summary = f"{run['target_status']} · {run['id']} · 会话 {run.get('session_id') or '未知'}"
        self.notion.update(run["page_id"], status=run["target_status"], summary=summary)
        run["phase"] = "complete"
        self.store.put(run)
        log.info("交付 %s -> %s", run["id"], run["target_status"])

    async def accept(self, page):
        # Re-read full input immediately before local claim, no shell interpolation.
        page = self.notion.page(page["id"])
        reason = self.candidate(page)
        if reason:
            return
        body = self.notion.body(page["id"])
        if not body.strip():
            self.notion.update(page["id"], summary="未接单：任务正文不能为空")
            return
        snapshot = task_snapshot(page, body)
        check = self.notion.page(page["id"])
        if fingerprint(task_snapshot(check, body)) != fingerprint(snapshot) or check.get("last_edited_time") != page.get("last_edited_time"):
            log.info("任务正在编辑，本轮跳过 %s", page["id"])
            return
        run_id = str(uuid.uuid4())
        run_dir = self.config.state_dir / "runs" / run_id
        run_dir.mkdir(parents=True, mode=0o700)
        run = {"id": run_id, "page_id": page["id"], "phase": "claimed", "snapshot": snapshot,
               "snapshot_hash": fingerprint(snapshot), "machine": self.config.machine_id,
               "agent": snapshot["agent"], "agent_settings": self.config.agents[snapshot["agent"]],
               "project_settings": self.config.projects[snapshot["project"]],
               "run_dir": str(run_dir), "started_at": now()}
        self.store.put(run)
        (run_dir / "snapshot.json").write_text(json.dumps(run, ensure_ascii=False, indent=2))
        # Ambiguous claim response leaves a durable claimed record; recovery will reconcile, not launch.
        self.notion.update(page["id"], status="执行中", run_id=run_id,
                           summary=f"已接单 {run_id}；采用 {run['started_at']} 的正文快照")
        check = self.notion.page(page["id"])
        if value(check, "最近运行 ID") != run_id or value(check, "状态") != "执行中" or not self.route_matches(check, run):
            self.seal(run, "启动前路由或状态变化，未启动 Agent")
            self.deliver(run)
            return
        try:
            if self.config.comments:
                self.notion.comment(page["id"], f"[notion-agent:{run_id}:claim]", f"{run['machine']} 已接单，执行 Agent：{run['agent']}。")
            run.update(workspace.prepare(run["project_settings"], snapshot["kind"], run_dir, run_id))
            (run_dir / "prompt.txt").write_text(prompt_for(run))
            self.store.put(run)
            await self.runner(self.config, run, self.store)
        except asyncio.CancelledError:
            self.seal(run, "服务停止，任务中断；请检查保留的工作区和日志")
            raise
        except Exception as error:
            self.seal(run, f"{type(error).__name__}: {error}")
        else:
            self.seal(run)
        self.deliver(run)

    async def scan(self, dry_run=False):
        if not dry_run:
            self.recover()
            if self.store.all(active=True):
                log.warning("未完成运行尚未对账，本轮不接新任务")
                return []
        rows = self.notion.query(self.config.machine_id)
        result = []
        for page in rows:
            reason = self.candidate(page)
            if not reason:
                try:
                    if not self.notion.body(page["id"]).strip():
                        reason = "任务正文不能为空"
                except ValueError as error:
                    reason = str(error)
            result.append({"id": page["id"], "title": value(page, "任务"), "eligible": not bool(reason), "reason": reason})
            if dry_run:
                continue
            if reason:
                self.notion.update(page["id"], summary="未接单：" + reason)
                continue
            await self.accept(page)
            # Single task per scan; poll picks up freshly edited priorities/configuration.
            break
        return result

    async def serve(self):
        delay = self.config.poll_seconds
        while True:
            try:
                refreshed = load(self.config.path)
                if refreshed.state_dir != self.config.state_dir or refreshed.machine_id != self.config.machine_id or refreshed.data_source_id != self.config.data_source_id:
                    raise ValueError("修改机器名、state_dir 或任务库后请重启服务")
                self.config = refreshed
                await self.scan()
                delay = self.config.poll_seconds
            except Exception as error:
                log.error("扫描失败：%s: %s", type(error).__name__, error)
                delay = min(max(delay * 2, 1), 300)
            await asyncio.sleep(delay)
