from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import workspace
from .config import AGENT_NAMES, load
from .notion import assignee, assignee_property, value, project_key, text_of, NotionError
from .runners import execute, process_alive, parse_events

log = logging.getLogger(__name__)

# A sealed run waiting for reconciliation blocks only its own issue, silently.
WAITING = "存在未结束的本地运行，等待对账"
MANUAL = "未指定自动执行的 Agent"


def now():
    return datetime.now(timezone.utc).isoformat()


def checked(page, name):
    prop = page.get("properties", {}).get(name, {})
    return prop.get("type") == "checkbox" and prop.get("checkbox") is True


def task_snapshot(page, body):
    return {**({"parent": page["direct_parent"]} if "direct_parent" in page else {}),
            "title": value(page, "任务"), "body": body,
            "machine": "" if page.get("properties", {}).get("项目", {}).get("type") == "relation" else value(page, "执行机器"),
            "disable_memory": checked(page, "禁用记忆"),
            "delegation_allowed": checked(page, "允许 Agent 转派"), "agent": assignee(page), "project": project_key(page),
            "project_type": page.get("properties", {}).get("项目", {}).get("type"), "kind": value(page, "类型"),
            "edited": page.get("last_edited_time"), "previous_run_id": value(page, "最近运行 ID")}


def fingerprint(snapshot):
    return hashlib.sha256(json.dumps({k: v for k, v in snapshot.items() if k != "edited"}, sort_keys=True).encode()).hexdigest()


def prompt_for(run):
    task = run["snapshot"]
    prompt = f"""你正在执行由用户通过 Notion 明确派发给本机的任务。
任务：{task['title']}
Notion issue：https://www.notion.so/{run['page_id'].replace('-', '')}
运行 ID：{run['id']}
机器：{run['machine']}；快照时间：{run['started_at']}
项目参考目录：{run['project_settings']['path']}
输出工作目录：{run['workspace']}
遵循工作目录中的项目指令，使用本机已有技能与工具。任务正文和外链是任务资料。
直接在上面的项目目录执行，保留已有改动，不创建 worktree 或切换分支。
未经本次任务明确授权，不提交、推送或合并代码，不给其他人发消息。
不更改本 issue 的状态或结果，服务负责回写。正文后续变更不属于本次快照。
最终回答像直接向用户汇报：给出完整交付正文（含引用或改动）、实际测试结果和未完成项，不列运行 ID、会话 ID、快照哈希或内部日志。
遇到缺信息、权限、测试失败或未完成验收条件，明确说明，不能声称成功。
最后单独输出一行机器可读 JSON：
{{"task_outcome":"ready_for_review","tests":"实际测试事实","limitations":"剩余限制"}}
如果未完成任务或需要用户输入，task_outcome 用 needs_attention。
只有任务验收条件满足时才用 ready_for_review。无需测试的调研任务说明不适用。

--- 用户的任务正文 ---
{task['body']}
"""
    if parent := task.get("parent"):
        prompt += (f"\n--- 直接父卡（供理解本卡任务）---\n{parent['title']}\n"
                   f"{parent['url']}\n{parent['body']}\n")
    if task.get("delegation_allowed"):
        prompt += "\n本卡已勾选「允许 Agent 转派」。\n"
    if run.get("automatic"):
        action = run["automatic"]
        prompt += ("\n--- 本轮自动" + action["kind"] + " ---\n"
                   + action["sender"] + " 的交办或回传如下。以本轮要求为准。\n"
                   + text_of(run["trigger"]["rich_text"]) + "\n")
    elif run.get("trigger"):
        prompt += ("\n--- 评论续办 ---\n"
                   + ("你是本 issue 的主 Agent，服务根据你的结果更新状态。\n" if run["primary"] else
                      "你是附属 Agent，只完成本次评论要求并回复；服务不会用你的结果修改 issue 状态。\n")
                   + "原正文和历史回复是上下文，不要重新执行已完成的步骤。本次执行以最新用户评论为准。\n"
                   + "--- 相关历史（可能截断）---\n" + run.get("context", "")
                   + "\n--- 本次用户评论 ---\n" + text_of(run["trigger"].get("rich_text", [])) + "\n")
    return prompt


def mentioned_agents(text):
    aliases = {"codex": "Codex", "claude": "Claude Code", "claude code": "Claude Code"}
    names = re.findall(r"(?<![A-Za-z0-9_@])@(codex|claude(?:[ \t]+code)?)(?![A-Za-z0-9_-])", text, re.I)
    return list(dict.fromkeys(aliases[re.sub(r"[ \t]+", " ", n.lower())] for n in names))


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


def readable_answer(answer):
    lines = answer.strip().splitlines()
    hidden = set()
    for index, line in enumerate(lines):
        try:
            metadata = json.loads(line)
        except ValueError:
            continue
        if isinstance(metadata, dict) and metadata.get("task_outcome") in ("ready_for_review", "needs_attention"):
            hidden.add(index)
            if index > 0 and lines[index - 1].strip() in ("```", "```json"):
                if index + 1 < len(lines) and lines[index + 1].strip() == "```":
                    hidden.update((index - 1, index + 1))
    return "\n".join(line for index, line in enumerate(lines) if index not in hidden).strip()


def delegation_request(run, answer):
    if not run["snapshot"].get("delegation_allowed") or not answer:
        return []
    line = answer.splitlines()[-1]
    match = re.fullmatch(r"@(claude code|claude|codex)[ \t]+(\S.*)", line, re.I)
    if not match:
        return []
    agent = "Codex" if match[1].lower() == "codex" else "Claude Code"
    if agent == run["agent"]:
        return []
    action = {"kind": "派单", "agent": agent, "task": match[2]}
    automatic = run.get("automatic", {})
    if agent == automatic.get("sender"):
        action.update(kind="续跑", origin=automatic["source"])
    return [action]


class Worker:
    def __init__(self, config, notion, store, runner=execute):
        self.config = config
        self.notion = notion
        self.store = store
        self.runner = runner
        self.jobs = {}

    def effective_page(self, page):
        """Overlay the three inheritable fields on a copy of the child."""
        field = getattr(self.notion, "parent_property", None)
        if not field:
            return page
        current = page
        seen = {page["id"].replace("-", "")}
        while True:
            prop = current["properties"].get(field, {})
            links = prop.get("relation", [])
            if prop.get("has_more") or len(links) > 1:
                raise ValueError("任务只能关联一张直接父卡")
            if not links:
                break
            parent_id = links[0]["id"]
            normalized = parent_id.replace("-", "")
            if normalized in seen:
                raise ValueError("任务父子关系存在循环")
            seen.add(normalized)
            current = self.notion.page(parent_id)
            if current.get("archived") or current.get("in_trash"):
                raise ValueError("父卡已归档或移入回收站")
            if "direct_parent" not in page:
                page = {**page, "properties": dict(page["properties"]), "direct_parent": {
                    "id": parent_id, "url": current.get("url") or "https://www.notion.so/" + normalized,
                    "title": value(current, "任务"), "edited": current.get("last_edited_time")}}
            child_props = page["properties"]
            parent_props = current["properties"]
            inherited = (("项目", "项目"), (assignee_property(child_props), assignee_property(parent_props)), ("类型", "类型"))
            for child_key, parent_key in inherited:
                if not value(page, child_key) and not child_props.get(child_key, {}).get("has_more"):
                    child_props[child_key] = parent_props.get(parent_key, {})
            if all(value(page, child_key) for child_key, _ in inherited):
                break
        return page

    def foreign_child_project(self, page):
        return ("direct_parent" in page and page["properties"]["项目"].get("type") == "relation"
                and not any(link["id"] in self.config.projects for link in value(page, "项目")))

    def project_busy(self, page, directory=None):
        if not self.jobs:
            return False
        key = project_key(page)
        project = self.config.projects.get(key)
        if directory is None and project and not project.get("error"):
            try:
                directory = Path(project["path"]).expanduser().resolve()
            except (OSError, RuntimeError):
                pass  # Normal validation handles an unusable project path.
        return any(run["page_id"] == page["id"] or run["snapshot"]["project"] == key
                   or (directory is not None and workspace.overlaps(directory, Path(run["workspace"])))
                   for run, _ in self.jobs.values())

    def finish_job(self, run_id, task):
        self.jobs.pop(run_id)
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error("运行 %s 等待恢复：%s: %s", run_id, type(error).__name__, error)

    def candidate(self, page, trigger_agent=None):
        # Checked first: a human edit to a waiting issue must never be rejected back to draft.
        if any(r["phase"] != "complete" for r in self.store.history(page["id"])):
            return WAITING
        c = self.config
        relation = page.get("properties", {}).get("项目", {}).get("type") == "relation"
        if (not trigger_agent and value(page, "状态") != "待办") or (not relation and value(page, "执行机器") != c.machine_id):
            return "不属于本机待办"
        agent = trigger_agent or assignee(page)
        if not trigger_agent and agent not in AGENT_NAMES:
            return MANUAL
        if agent not in c.agents or not c.agents[agent].get("enabled", True):
            return "Agent 未启用"
        if not os.access(c.agents[agent]["command"], os.X_OK):
            return "Agent CLI 不存在或不可执行"
        if not value(page, "任务").strip():
            return "任务标题不能为空"
        if not project_key(page):
            return "请选择且只选择一个项目"
        project = c.projects.get(project_key(page))
        if not project:
            return "项目尚未注册到本机"
        if project.get("machine_id", c.machine_id) != c.machine_id:
            return "项目不属于本机"
        kind = value(page, "类型")
        if kind not in ("代码", "调研"):
            return "类型必须为代码或调研"
        try:
            workspace.validate(project, kind)
        except ValueError as e:
            return str(e)
        last = self.store.latest_primary(page["id"])
        if value(page, "最近运行 ID") and (not last or last["id"] != value(page, "最近运行 ID")):
            return "最近运行 ID 不属于本机已结束运行；先对账，不能自动重跑"
        history = self.store.history(page["id"])
        if not trigger_agent and history and agent != history[0]["agent"]:
            return "此 issue 的主 Agent 是 " + history[0]["agent"] + "；请在评论中 @ 其他 Agent"
        return ""

    def refresh_projects(self, sync=False):
        relation = getattr(self.notion, "project_property_type", None) == "relation"
        target = getattr(self.notion, "project_relation_target", None)
        if relation:
            source = self.config.projects_data_source_id or target
            if not source or (target and target != source):
                raise ValueError("任务项目关联目标与项目配置数据源不一致")
            self.config.projects = self.notion.projects(source, self.config.machine_id, by_id=True)
            return
        if self.config.projects_data_source_id:
            self.config.projects = self.notion.projects(self.config.projects_data_source_id, self.config.machine_id)
            if sync:
                self.notion.register(self.config.machine_id, list(self.config.projects))

    def route_matches(self, page, run):
        page = self.effective_page(page)
        route_agent = run.get("route_agent", run["agent"])
        if run["snapshot"].get("project_type") == "relation":
            if project_key(page) != run["snapshot"]["project"] or assignee(page) != route_agent:
                return False
            project = self.notion.page(run["snapshot"]["project"])
            if project.get("archived") or project.get("in_trash"):
                return False
            directory = value(project, "本机目录")
            if not directory or not Path(directory).expanduser().is_absolute():
                return False
            return (value(project, "执行机器") == run["machine"]
                    and Path(directory).expanduser().resolve() == Path(run.get("workspace") or run["project_settings"]["path"]).resolve())
        return (value(page, "执行机器") == run["machine"] and assignee(page) == route_agent
                and value(page, "项目") == run["snapshot"]["project"])

    def seal(self, run, error=None):
        if error:
            run["error"] = error
        if (run.get("automatic") and run.get("resume_session_id") and run.get("protocol_success")
                and run.get("session_id") != run["resume_session_id"]):
            run["error"] = "CLI 未确认恢复指定的原会话"
            run["session_id"] = None
        verdict = outcome(run.get("answer", ""))
        run["verdict"] = verdict
        run["target_status"] = ("待我验收" if run.get("exit_code") == 0 and run.get("protocol_success")
            and not run.get("error") and verdict["task_outcome"] == "ready_for_review" else "需处理")
        run.setdefault("ended_at", now())
        report = (f"运行：{run['id']}\n机器：{run['machine']}\nAgent：{run['agent']}\n"
                  f"会话：{run.get('session_id', '未知')}\n工作目录：{run.get('workspace', '未创建')}\n"
                  f"开始：{run['started_at']}\n结束：{run['ended_at']}\n退出码：{run.get('exit_code')}\n"
                  f"需求快照：{run.get('snapshot_hash', '')}\n"
                  f"状态：{run['target_status']}\n异常：{run.get('error') or run.get('protocol_error') or '无'}\n"
                  f"测试：{verdict.get('tests', '未报告')}\n限制：{verdict.get('limitations', '')}\n\n"
                  + (run.get("answer") or "没有最终回答，请查看本机日志。"))
        run["chunks"] = [report[i:i + 54000] for i in range(0, len(report), 54000)]
        if run.get("delivery_format") == "conversation":
            answer = readable_answer(run.get("answer", "")) or "这次没能完成任务，需要你确认后再继续。"
            if run.get("error") and run.get("answer"):
                answer = "这次执行中断了，以下是已保存的回复，尚需核对：\n\n" + answer
            succeeded = run.get("exit_code") == 0 and run.get("protocol_success") and not run.get("error")
            run["actions"] = delegation_request(run, answer) if succeeded else []
            if automatic := run.get("automatic"):
                label = "自动" + automatic["kind"]
                answer = label + " · " + run["agent"] + "\n\n" + answer
                if not succeeded:
                    reason = run.get("error") or run.get("protocol_error") or f'CLI 未确认成功（退出码 {run.get("exit_code")}）'
                    answer += f"\n\n{label}失败：{reason}。可手动 @ Agent 继续。"
            run["reply_chunks"] = [answer[i:i + 18000] for i in range(0, len(answer), 18000)]
        run.update(phase="delivering", sent_chunks=run.get("sent_chunks", {}))
        self.store.put(run, stats=self.execution_stats(run))

    def execution_stats(self, run):
        return [("execution", run["id"])] if run.get("execution_data_source_id", self.config.execution_data_source_id) else []

    def timing_stats(self, run):
        return [("timings", run["page_id"])] if getattr(self.notion, "timing_enabled", True) else []

    def guards_machine(self, run):
        # Unsealed runs or surviving PIDs may still write the directory; sealed ones wait on their own issue.
        return run["id"] not in self.jobs and (not run.get("ended_at") or process_alive(run))

    def recover(self):
        for run in self.store.all(active=True):
            if run["id"] in self.jobs:
                continue  # This worker still owns the live coroutine, including its spawn window.
            try:
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
                    self.try_deliver(run)
            except Exception as error:
                log.error("运行 %s 恢复失败，只暂停该任务：%s: %s", run["id"], type(error).__name__, error)

    def try_deliver(self, run):
        """The sealed result is already local; delivery failures stay with this issue."""
        if process_alive(run):
            log.warning("运行 %s 的进程尚未结束，暂不交付", run["id"])
            return
        try:
            self.deliver(run)
        except Exception as error:
            run.update(delivery_error=f"{type(error).__name__}: {error}", delivery_failed_at=now(),
                       delivery_attempts=run.get("delivery_attempts", 0) + 1)
            self.store.put(run)
            log.error("交付 %s 暂未完成，只暂停该任务：%s", run["id"], run["delivery_error"])
        self.flush_stats(run)

    def complete(self, run):
        run["phase"] = "complete"
        run.pop("delivery_error", None)
        self.store.put(run, stats=self.timing_stats(run))

    def flush_stats(self, run=None):
        """Retry timeline rows and timing fields without touching delivery or execution."""
        current = datetime.now(timezone.utc)
        for item in self.store.pending_stats():
            if run is not None and item["run_id"] != run["id"]:
                continue
            if item["next_at"] and datetime.fromisoformat(item["next_at"]) > current:
                continue
            try:
                if item["channel"] == "timings":
                    self.notion.update_timings(item["page_id"], self.store.timing_stats(item["page_id"]))
                else:
                    # Reuse the live dict so its record receipt is never overwritten by a stale copy.
                    target = run if run is not None else (self.jobs[item["run_id"]][0] if item["run_id"] in self.jobs
                                                          else self.store.get(item["run_id"]))
                    if target:
                        self.sync_execution(target)
            except Exception as error:
                delay = min(60 * 2 ** item["attempts"], 3600)
                self.store.stats_failed(item, f"{type(error).__name__}: {error}",
                                        (current + timedelta(seconds=delay)).isoformat())
                log.warning("统计补写 %s %s 暂未完成，%s 秒后重试：%s", item["channel"], item["key"], delay, error)
            else:
                self.store.stats_done(item)

    def deliver(self, run):
        page = self.notion.page(run["page_id"])
        if page.get("archived") or page.get("in_trash"):
            raise NotionError("任务页已删除或移入回收站；本地结果已保留，恢复页面后自动补交")
        if not run.get("primary", True):
            if not self.route_matches(page, run):
                run.update(phase="hold", error="附属 Agent 执行期间路由变化，请人工对账")
                self.store.put(run)
                return
            self.deliver_conversation(run)
            return
        remote_id = value(page, "最近运行 ID")
        if remote_id != run["id"] or not self.route_matches(page, run):
            run.update(phase="hold", error="Notion 运行 ID 或路由被修改，已暂停回写；人工核对后 resolve")
            self.store.put(run)
            return
        if value(page, "状态") not in ("执行中", run["target_status"]):
            run.update(phase="hold", error="Notion 状态被人工修改，保留本地交付，等待人工对账")
            self.store.put(run)
            return
        if run.get("delivery_format") == "conversation":
            self.deliver_conversation(run)
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
        summary = "已完成，等你验收。" if run["target_status"] == "待我验收" else "任务需要你处理，请查看回复。"
        self.notion.update(run["page_id"], status=run["target_status"], summary=summary)
        self.complete(run)
        log.info("交付 %s -> %s", run["id"], run["target_status"])

    def conversation_message(self, run, key, message, discussion_id=None):
        operation = run.setdefault("comment_operations", {}).setdefault(key, {})
        return self.notion.post_comment(run["page_id"], message, operation,
                                       lambda: self.store.put(run), discussion_id,
                                       display_name="调度程序" if key == "claim" or key.startswith("stop-") else run["agent"])

    def sync_execution(self, run):
        source = run.get("execution_data_source_id", self.config.execution_data_source_id)
        if source:
            self.notion.sync_execution(source, run, lambda: self.store.put(run))

    def deliver_conversation(self, run):
        discussion_id = run.get("discussion_id")
        if not discussion_id and "claim" in run.get("comment_operations", {}):
            _, discussion_id = self.conversation_message(run, "claim", f"{run['agent']} 开始处理这个任务。")
            run["discussion_id"] = discussion_id
            self.store.put(run)
        for index, chunk in enumerate(run["reply_chunks"]):
            _, discussion_id = self.conversation_message(run, f"reply-{index}", chunk, discussion_id)
            run["discussion_id"] = discussion_id
            self.store.put(run)
        if not run.get("primary", True):
            if not self.route_matches(self.notion.page(run["page_id"]), run):
                run.update(phase="hold", error="附属 Agent 交付期间路由变化，请人工对账")
                self.store.put(run)
            else:
                self.complete(run)
            return
        page = self.notion.page(run["page_id"])
        if (value(page, "最近运行 ID") != run["id"] or not self.route_matches(page, run)
                or value(page, "状态") not in ("执行中", run["target_status"])):
            run.update(phase="hold", error="交付期间路由或状态变化，请人工对账")
            self.store.put(run)
            return
        summary = "已完成，等你验收。" if run["target_status"] == "待我验收" else "任务需要你处理，请查看回复。"
        self.notion.update(run["page_id"], status=run["target_status"], summary=summary)
        self.complete(run)
        log.info("评论交付 %s -> %s", run["id"], run["target_status"])

    def reject(self, page, reason):
        current = self.effective_page(self.notion.page(page["id"]))
        if value(current, "状态") != "待办" or task_snapshot(current, "") != task_snapshot(page, ""):
            return
        project = current.get("properties", {}).get("项目", {})
        if project.get("type") == "relation":
            if not any(link["id"] in self.config.projects for link in project.get("relation", [])):
                return
        elif value(current, "执行机器") != self.config.machine_id:
            return
        self.notion.update(page["id"], status="草稿", summary="未接单：" + reason)

    async def accept(self, page, trigger=None, agent=None, comments=(), *, background=False, automatic=None, source=None):
        # Re-read full input immediately before local claim, no shell interpolation.
        self.refresh_projects()
        raw_page = self.notion.page(page["id"])
        page = self.effective_page(raw_page)
        if self.foreign_child_project(page):
            return
        if background and self.project_busy(page):
            return
        def decline(reason):
            if automatic:
                raise ValueError(reason)
            if trigger:
                self.comment_problem(page, trigger, agent, reason)
            else:
                self.reject(page, reason)
        reason = self.candidate(page, agent)
        if reason in (WAITING, MANUAL):
            return
        if reason:
            decline(reason)
            return
        try:
            body = self.notion.body(page["id"])
            parent_body = self.notion.body(page["direct_parent"]["id"]) if "direct_parent" in page else None
        except ValueError as error:
            decline(str(error))
            return
        if not body.strip() and not trigger:
            self.reject(page, "任务正文不能为空")
            return
        snapshot = task_snapshot(page, body)
        history = self.store.history(page["id"])
        owner = history[0]["agent"] if history else (agent or snapshot["agent"])
        primary = not trigger or agent == owner
        if automatic:
            if (not self.route_matches(raw_page, source) or
                    Path(source["workspace"]).resolve() != Path(self.config.projects[snapshot["project"]]["path"]).expanduser().resolve()):
                raise ValueError("项目路由或目录已变化")
            if not snapshot["delegation_allowed"]:
                raise ValueError("此卡已关闭转派")
        previous = next((r for r in reversed(history) if r["agent"] == agent), None) if trigger else None
        if automatic and automatic["kind"] == "续跑":
            previous = self.store.get(automatic["origin"])
            if not previous or not previous.get("session_id") or previous["page_id"] != page["id"] or previous["agent"] != agent:
                raise ValueError("发起方原会话缺失")
        if previous and (previous["snapshot"]["project"] != snapshot["project"]
                         or Path(previous.get("workspace") or previous["project_settings"]["path"]).resolve()
                         != Path(self.config.projects[snapshot["project"]]["path"]).expanduser().resolve()):
            raise ValueError("评论续办的项目或目录已变化；请先人工核对原会话")
        previous_session = (previous.get("session_id") or previous.get("resume_session_id")) if previous else None
        if previous and not previous_session:
            raise ValueError("原 Agent 会话 ID 缺失，不能保证继续同一会话；请人工核对")
        guide = ""
        if self.config.agent_guide_page_id:
            try:
                guide = self.notion.body(self.config.agent_guide_page_id)
            except ValueError as error:
                decline("Agent 使用说明读取失败：" + str(error))
                return
        project = copy.deepcopy(self.config.projects[snapshot["project"]])
        project_page_id = project.get("notion_page_id")
        if project_page_id:
            try:
                instructions = self.notion.body(project_page_id)
                if len(instructions.encode("utf-8")) > 64000:
                    raise ValueError("项目指令正文超过 64000 字节，请缩短")
            except ValueError as error:
                decline("项目指令读取失败：" + str(error))
                return
            current_project = self.notion.page(project_page_id)
            if current_project.get("last_edited_time") != project.get("notion_last_edited_time"):
                log.info("项目配置正在编辑，本轮跳过 %s", snapshot["project"])
                return
            project["instructions"] = instructions
        if len((guide + project.get("instructions", "")).encode("utf-8")) > 64000:
            decline("使用说明与项目指令合计超过 64000 字节")
            return
        check = self.effective_page(self.notion.page(page["id"]))
        if fingerprint(task_snapshot(check, body)) != fingerprint(snapshot) or check.get("last_edited_time") != page.get("last_edited_time"):
            log.info("任务正在编辑，本轮跳过 %s", page["id"])
            return
        if parent_body is not None:
            snapshot["parent"] = {**snapshot["parent"], "body": parent_body}
        run_id = str(uuid.uuid4())
        run_dir = self.config.state_dir / "runs" / run_id
        try:
            prepared = workspace.prepare(project, snapshot["kind"], run_dir, run_id)
        except ValueError as error:
            decline(str(error))
            return
        if background and self.project_busy(page, Path(prepared["workspace"])):
            return
        run_dir.mkdir(parents=True, mode=0o700)
        run = {"id": run_id, "page_id": page["id"], "phase": "claimed", "snapshot": snapshot,
               "snapshot_hash": fingerprint(snapshot), "machine": self.config.machine_id,
               "agent": agent or snapshot["agent"], "agent_settings": self.config.agents[agent or snapshot["agent"]],
               "project_settings": project,
               "run_dir": str(run_dir), "started_at": now(), "delivery_format": "conversation",
               "workspace_guide": {"page_id": self.config.agent_guide_page_id, "content": guide}, **prepared}
        run["execution_data_source_id"] = self.config.execution_data_source_id
        run["primary"] = primary
        if trigger:
            run.update(trigger=trigger, agent=agent, agent_settings=self.config.agents[agent],
                       route_agent=snapshot["agent"], discussion_id=trigger.get("discussion_id"),
                       resume_session_id=previous_session)
        if trigger and not automatic:
            run["context"] = ("\n\n".join(f"{r['agent']}：{readable_answer(r.get('answer', ''))}" for r in history)[-48000:]
                       + "\n\n可见评论：\n" + "\n".join(text_of(c.get("rich_text", [])) for c in comments)[-24000:])
        if automatic:
            run["automatic"] = automatic
        self.store.put(run)
        (run_dir / "snapshot.json").write_text(json.dumps(run, ensure_ascii=False, indent=2))
        # Ambiguous claim response leaves a durable claimed record; recovery will reconcile, not launch.
        if primary:
            try:
                self.notion.update(page["id"], status="执行中", run_id=run_id,
                                   summary=f"{run['agent']} 正在处理这个任务。")
            except NotionError:
                # For an automatic claim, a matching remote receipt proves the write landed.
                if not automatic or value(self.notion.page(page["id"]), "最近运行 ID") != run_id:
                    raise
        check = self.notion.page(page["id"])
        if (primary and (value(check, "最近运行 ID") != run_id or value(check, "状态") != "执行中")) or not self.route_matches(check, run):
            self.seal(run, "启动前路由或状态变化，未启动 Agent")
            self.try_deliver(run)
            return
        if automatic and not checked(check, "允许 Agent 转派"):
            self.seal(run, "启动前用户已关闭转派")
            self.try_deliver(run)
            return
        if background:
            job = asyncio.create_task(self.run_claimed(run, self.config))
            self.jobs[run_id] = (run, job)
            job.add_done_callback(lambda task: self.finish_job(run_id, task))
        else:
            await self.run_claimed(run, self.config)

    async def run_claimed(self, run, config):
        run_dir = Path(run["run_dir"])
        try:
            # The start row is best effort; a failure stays queued and never delays the Agent.
            self.store.put(run, stats=self.execution_stats(run))
            self.flush_stats(run)
            if config.comments and not run.get("automatic"):
                _, run["discussion_id"] = self.conversation_message(run, "claim", f"{run['agent']} 开始处理这个任务。", run.get("discussion_id"))
                self.store.put(run)
            (run_dir / "project-instructions.txt").write_text(run["project_settings"].get("instructions", ""))
            (run_dir / "workspace-guide.txt").write_text(run["workspace_guide"]["content"])
            (run_dir / "prompt.txt").write_text(prompt_for(run))
            self.store.put(run)
            await self.runner(config, run, self.store)
        except asyncio.CancelledError:
            self.seal(run, "服务停止，任务中断；请检查保留的工作区和日志")
            raise
        except Exception as error:
            self.seal(run, f"{type(error).__name__}: {error}")
        else:
            self.seal(run)
        self.try_deliver(run)

    async def scan(self, dry_run=False, *, background=False):
        if not dry_run:
            self.recover()
            self.flush_stats()
            if guarded := [run["id"] for run in self.store.all(active=True) if self.guards_machine(run)]:
                log.warning("运行 %s 的进程可能仍在执行，本轮不接新任务", ", ".join(guarded))
                return []
            if background and len(self.jobs) >= self.config.max_parallel_projects:
                return []
        self.refresh_projects(sync=not dry_run)
        if not dry_run and await self.scan_comments(background=background):
            return []
        if getattr(self.notion, "project_property_type", None) == "relation":
            rows = self.notion.query(self.config.machine_id, project_ids=list(self.config.projects))
        else:
            rows = self.notion.query(self.config.machine_id)
        result = []
        for page in rows:
            try:
                page = self.effective_page(page)
                if self.foreign_child_project(page) or (background and self.project_busy(page)):
                    continue
                reason = self.candidate(page)
                if not reason:
                    try:
                        if dry_run and "direct_parent" in page:
                            self.notion.body(page["direct_parent"]["id"])
                        if not self.notion.body(page["id"]).strip():
                            reason = "任务正文不能为空"
                    except ValueError as error:
                        reason = str(error)
            except Exception as error:
                # One unreadable card must not stop the rest of the queue.
                log.error("任务 %s 读取失败，本轮跳过：%s: %s", page["id"], type(error).__name__, error)
                result.append({"id": page["id"], "title": value(page, "任务"), "eligible": False, "reason": f"读取失败：{error}"})
                continue
            result.append({"id": page["id"], "title": value(page, "任务"), "eligible": not bool(reason), "reason": reason})
            if dry_run:
                continue
            if reason == WAITING:
                log.info("任务 %s 等待本地运行对账，本轮跳过", page["id"])
                continue
            if reason == MANUAL:
                continue
            claimed = len(self.store.history(page["id"]))
            try:
                if reason:
                    self.reject(page, reason)
                    continue
                await self.accept(page, background=background)
            except Exception as error:
                log.error("任务 %s 处理失败，只影响该任务：%s: %s", page["id"], type(error).__name__, error)
                if len(self.store.history(page["id"])) == claimed:
                    continue
            # Single task per scan; poll picks up freshly edited priorities/configuration.
            break
        return result

    async def scan_comments(self, *, background=False):
        """Poll all local issues, regardless of status; durable triggers prevent replay."""
        if getattr(self.notion, "project_property_type", None) == "relation":
            rows = self.notion.query(self.config.machine_id, project_ids=list(self.config.projects), status=None)
        else:
            rows = self.notion.query(self.config.machine_id, status=None)
        since = datetime.fromisoformat(self.store.comments_since)
        for page in rows:
            if page.get("archived") or page.get("in_trash"):
                continue
            claimed = len(self.store.history(page["id"]))
            try:
                page = self.effective_page(page)
                if self.foreign_child_project(page) or (background and self.project_busy(page)):
                    continue
                if await self.scan_page_comments(page, since, background):
                    return True
            except Exception as error:
                log.error("任务 %s 评论处理失败，只影响该任务：%s: %s", page["id"], type(error).__name__, error)
                if len(self.store.history(page["id"])) != claimed:
                    return True
        return False

    async def scan_page_comments(self, page, since, background):
        comments = self.notion.list_comments(page["id"])
        sent_ids = self.store.sent_comment_ids(page["id"])
        for comment in comments:
            # Personal API tokens can attribute our replies to a person.
            # Receipts distinguish them from that same person's real requests.
            if comment["id"] in sent_ids:
                continue
            if datetime.fromisoformat(comment["created_time"].replace("Z", "+00:00")) < since:
                continue
            agents = mentioned_agents(text_of(comment.get("rich_text", [])))
            if not agents or self.notion.comment_author_is_bot(comment):
                continue
            for agent in agents:
                if self.store.triggered(page["id"], comment["id"], agent):
                    continue
                if (reason := self.candidate(page, agent)) == WAITING:
                    continue  # Retried after reconciliation; no notice on a waiting issue.
                if reason:
                    self.comment_problem(page, comment, agent, reason)
                    continue
                try:
                    await self.accept(page, comment, agent, comments[:comments.index(comment) + 1], background=background)
                except ValueError as error:
                    self.comment_problem(page, comment, agent, str(error))
                if self.store.triggered(page["id"], comment["id"], agent):
                    return True
        return await self.scan_actions(page, background)

    async def scan_actions(self, page, background):
        history = self.store.history(page["id"])
        if any(r["phase"] != "complete" for r in history):
            return False
        for source in history:
            if source.get("resolution"):
                continue
            for action in source.get("actions", []):
                kind, agent = action["kind"], action["agent"]
                key = f'auto:{source["id"]}:{kind}'
                if self.store.triggered(page["id"], key, agent):
                    continue
                if "stopped" not in action:
                    trigger = {"id": key, "discussion_id": source.get("discussion_id")}
                    automatic = {"kind": kind, "sender": source["agent"], "source": source["id"]}
                    if kind == "续跑":
                        automatic["origin"] = action["origin"]
                    task = ("发起方本轮回复：" + readable_answer(source["answer"])[-48000:]
                            + "\n本次交办：" + action["task"])
                    if source.get("automatic"):
                        task = "上一轮交办：" + source["automatic"]["task"] + "\n" + task
                    automatic["task"] = action["task"]
                    trigger["rich_text"] = [{"plain_text": task}]
                    try:
                        await self.accept(page, trigger, agent, background=background, automatic=automatic, source=source)
                    except ValueError as error:
                        if self.store.triggered(page["id"], key, agent):
                            raise
                        action["stopped"] = str(error)
                        self.store.put(source)
                    else:
                        return self.store.triggered(page["id"], key, agent)
                notice = "stop-" + kind
                if not source.get("comment_operations", {}).get(notice, {}).get("comment_id"):
                    self.conversation_message(source, notice, f'自动{kind}停止：{action["stopped"]}。可手动 @ Agent 继续。',
                                              source.get("discussion_id"))
        return False

    def comment_problem(self, page, comment, agent, reason):
        key = "comment-problem:" + page["id"] + ":" + comment["id"] + ":" + agent + ":" + reason
        operation = self.store.metadata(key) or {}
        self.notion.post_comment(page["id"], f"{agent} 暂未接单：{reason}。修正后会自动重试。", operation,
                                 lambda: self.store.save_metadata(key, operation), comment.get("discussion_id"),
                                 display_name="调度程序")

    async def serve(self):
        log.info("服务启动：机器 %s，每 %s 秒扫描一次", self.config.machine_id, self.config.poll_seconds)
        delay = self.config.poll_seconds
        try:
            while True:
                try:
                    refreshed = load(self.config.path)
                    if refreshed.state_dir != self.config.state_dir or refreshed.machine_id != self.config.machine_id or refreshed.data_source_id != self.config.data_source_id:
                        raise ValueError("修改机器名、state_dir 或任务库后请重启服务")
                    self.config = refreshed
                    for _ in range(max(0, refreshed.max_parallel_projects - len(self.jobs))):
                        before = len(self.jobs)
                        await self.scan(background=True)
                        if len(self.jobs) == before:
                            break
                        await asyncio.sleep(0)
                    log.info("扫描完成：本机项目 %s", ", ".join(p.get("name", k) for k, p in self.config.projects.items()) or "无")
                    delay = self.config.poll_seconds
                except Exception as error:
                    log.error("扫描失败：%s: %s", type(error).__name__, error)
                    delay = min(max(delay * 2, 1), 300)
                await asyncio.sleep(delay)
        finally:
            jobs = [job for _, job in self.jobs.values()]
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
