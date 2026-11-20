import asyncio
import copy
import io
import json
import os
import subprocess
import tempfile
import tomllib
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from notion_agent import cli
from notion_agent.config import initial, load, save
from notion_agent.notion import (Notion, NotionError, NotionRequestRejected, CommentWriteUncertain,
                                 ExecutionWriteUncertain, rich, value)
from notion_agent.runners import command, execute, parse_events, child_environment
from notion_agent.store import Store, instance_lock
from notion_agent.worker import Worker, outcome, readable_answer, mentioned_agents, prompt_for
from notion_agent.workspace import prepare


def page(machine="test-machine", agent="Codex", kind="调研"):
    props = {"任务": {"type": "title", "title": rich("测试任务")},
             "最近运行 ID": {"type": "rich_text", "rich_text": []},
             "运行摘要": {"type": "rich_text", "rich_text": []}}
    for key, val in {"状态": "待办", "执行机器": machine, "执行 Agent": agent, "类型": kind, "项目": "demo", "优先级": "高"}.items():
        t = "status" if key == "状态" else "select"
        props[key] = {"type": t, t: {"name": val}}
    return {"id": "page-1", "properties": props, "last_edited_time": "2026-11-18T00:00:00Z"}


class FakeNotion:
    def __init__(self, p):
        self.p = p
        self.extra_pages = {}
        self.blocks = {}
        self.comments = {}
        self.comment_names = {}
        self.fail_delivery_once = False
        self.body_text = "请输出测试报告，验收：给出结论。"
        self.writes = []
        self.timing_writes = []
        self.execution_records = {}
        self.execution_writes = []
        self.execution_creates = []
        self.missing = set()
        self.stats_down = False

    def sync_execution(self, source, run, persist):
        if self.stats_down:
            raise NotionError("Notion POST data_sources/records/query: HTTP 502")
        if "execution_record" not in run:
            self.execution_creates.append(run["id"])
        operation = run.setdefault("execution_record", {"source_id": source, "page_id": "record-" + run["id"]})
        persist()
        self.execution_records[run["id"]] = {"start": run["started_at"], "end": run.get("ended_at"),
                                             "result": run.get("target_status", "执行中"), "page_id": operation["page_id"]}
        self.execution_writes.append(copy.deepcopy(self.execution_records[run["id"]]))
        if getattr(self, "fail_execution_once", False) and run.get("ended_at"):
            self.fail_execution_once = False
            raise NotionError("record update response lost")
        return operation["page_id"]

    def update_timings(self, page_id, stats):
        if self.stats_down:
            raise NotionError(f"Notion PATCH pages/{page_id}: HTTP 502")
        self.timing_writes.append(copy.deepcopy(stats))
        if stats:
            for name, key in {"最近耗时（秒）": "latest_seconds", "累计耗时（秒）": "total_seconds", "执行次数": "execution_count"}.items():
                self.card(page_id)["properties"][name] = {"type": "number", "number": stats[key]}

    def query(self, machine, project_ids=None, status="待办"):
        rows = []
        for card in (self.p, *self.extra_pages.values()):
            if card["id"] in self.missing or card.get("in_trash"):
                continue
            if project_ids is not None:
                ids = [x["id"] for x in card.get("properties", {}).get("项目", {}).get("relation", [])]
                parent = getattr(self, "parent_property", None)
                inherits = not ids and bool(value(card, parent))
                if not set(ids).intersection(project_ids) and not inherits:
                    continue
            if status is None or value(card, "状态") == status:
                rows.append(copy.deepcopy(card))
        return rows

    def list_comments(self, page_id):
        return getattr(self, "incoming_comments", []) if page_id == self.p["id"] else []

    def comment_author_is_bot(self, comment):
        return comment.get("created_by", {}).get("type") == "bot"

    def card(self, page_id):
        return self.extra_pages.get(page_id, self.p)

    def page(self, page_id):
        if page_id in self.missing:
            raise NotionError(f"Notion GET pages/{page_id}: HTTP 404")
        return copy.deepcopy(self.card(page_id))

    def body(self, _):
        return self.body_text

    def update(self, page_id, status=None, run_id=None, summary=None):
        self.writes.append((status, run_id, summary))
        for key, val in (("状态", status), ("最近运行 ID", run_id), ("运行摘要", summary)):
            if val is not None:
                t = "status" if key == "状态" else "rich_text"
                self.card(page_id)["properties"][key] = {"type": t, t: {"name": val} if t == "status" else rich(val)}

    def result_chunk(self, page_id, marker, text):
        self.blocks.setdefault(marker, text)
        if self.fail_delivery_once:
            self.fail_delivery_once = False
            raise NotionError("写入成功但响应丢失")
        return marker

    def comment(self, page_id, marker, text):
        self.comments.setdefault(marker, text)

    def post_comment(self, page_id, message, operation, persist, discussion_id=None, display_name=None):
        if operation.get("comment_id"):
            return operation["comment_id"], operation["remote_discussion_id"]
        if "fake_key" not in operation:
            operation["fake_key"] = "comment-" + str(len(self.comments))
            persist()
        key = operation["fake_key"]
        self.comments.setdefault(key, message)
        self.comment_names[key] = display_name
        if self.fail_delivery_once and discussion_id:
            self.fail_delivery_once = False
            raise NotionError("comment response lost")
        thread = discussion_id or "thread-" + key
        operation.update(comment_id=key, remote_discussion_id=thread)
        persist()
        return key, thread


class RemoteCommentNotion(FakeNotion):
    """Runs the real comment reconciliation against an in-memory comments API."""
    def __init__(self, p):
        super().__init__(p)
        self.remote = []
        self.posts = []
        self.reply_fault = None  # "lost": written, response lost; "vanished": also resolved before reconciling

    def request(self, method, path, body=None):
        if path == "users/me":
            return {"id": "integration"}
        if method == "GET":
            page_id = path.split("block_id=")[1].split("&")[0]
            return {"results": [c for c in self.remote if c["page"] == page_id], "has_more": False}
        page_id = body.get("parent", {}).get("page_id") or next(
            c["page"] for c in self.remote if c["discussion_id"] == body["discussion_id"])
        self.posts.append((page_id, body))
        comment = {"id": f"remote-{len(self.posts)}", "page": page_id,
                   "discussion_id": body.get("discussion_id") or f"thread-{len(self.posts)}",
                   "created_by": {"id": "integration"}, "rich_text": body["rich_text"],
                   "display_name": {"resolved_name": body["display_name"]["custom"]["name"]}}
        self.remote.append(comment)
        fault = self.reply_fault if body.get("discussion_id") else None
        if fault:
            self.reply_fault = None
            if fault == "vanished":
                self.remote.remove(comment)
            raise NotionError("Notion POST comments: 网络错误或响应不确定")
        return comment

    def post_comment(self, *args, **kwargs):
        return Notion.post_comment(self, *args, **kwargs)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "config.toml"
        data = initial()
        data.update(machine_id="test-machine", state_dir=str(self.root / "state"))
        data["agents"]["Codex"]["command"] = "/usr/bin/true"
        data["projects"] = {"demo": {"path": str(self.root)}}
        save(data, self.path)
        self.config = load(self.path)
        self.store = Store(self.config.state_dir)
        self.notion = FakeNotion(page())
        self.calls = 0

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    async def fake_runner(self, config, run, store):
        self.calls += 1
        run.update(exit_code=0, protocol_success=True, session_id="actual-session",
                   answer='结论：通过。\n{"task_outcome":"ready_for_review","tests":"模拟验证通过","limitations":"无"}')

    def worker(self):
        return Worker(self.config, self.notion, self.store, self.fake_runner)

    def enable_handoff(self):
        props = self.notion.p["properties"]
        props["允许 Agent 转派"] = {"type": "checkbox", "checkbox": True}
        self.config.agents["Claude Code"].update(command="/usr/bin/true", enabled=True)

    async def delegate(self, config, run, store):
        await self.fake_runner(config, run, store)
        target = "@claude" if run["agent"] == "Codex" else "@codex"
        run["answer"] = f"{target} 测试当前修改\n" + json.dumps(outcome(run["answer"]))

    async def test_round_trip_cli_resume_and_lost_handoff_callback_replies(self):
        self.notion = RemoteCommentNotion(page())
        self.enable_handoff()
        # Run the production command builder, child process and event parsers in both directions.
        script = self.root / "cli"
        script.write_text("""#!/usr/bin/env python3
import sys, json
prompt = sys.stdin.read()
verdict = {"task_outcome":"ready_for_review","tests":"通过","limitations":""}
def emit(obj):
    print(json.dumps(obj))
if sys.argv[1] == "exec":
    if "resume" in sys.argv:
        assert sys.argv[sys.argv.index("resume")+1] == "original-codex"
        assert "审查发现" in prompt and "检查路由" in prompt
        note = "已按审查意见处理"
    else:
        note = "代码已就绪\\n@claude 检查路由，完成后请叫我继续"
    emit({"type":"thread.started","thread_id":"original-codex"})
    emit({"type":"item.completed","item":{"type":"agent_message","text":note+"\\n"+json.dumps(verdict)}})
    emit({"type":"turn.completed"})
else:
    assert "代码已就绪" in prompt and "检查路由" in prompt
    emit({"type":"result","subtype":"success","session_id":"claude-review","result":"审查发现\\n@codex 请按意见修正\\n"+json.dumps(verdict)})
""")
        script.chmod(0o700)
        for agent in self.config.agents.values():
            agent["command"] = str(script)
        update = self.notion.update
        def lose_callback_claim(page_id, **fields):
            update(page_id, **fields)
            if fields.get("run_id") and len(self.store.all()) == 3:
                raise NotionError("callback claim written, response lost")
        self.notion.update = lose_callback_claim
        for i in range(3):
            self.notion.reply_fault = "lost"
            await Worker(self.config, self.notion, self.store).scan()
            self.assertEqual(len(self.store.all()), i + 1)
            self.assertEqual(self.store.all()[-1]["phase"], "delivering")
            self.restart()
        for _ in range(2):
            await Worker(self.config, self.notion, self.store).scan()
        runs = self.store.all()
        self.assertEqual([r["agent"] for r in runs], ["Codex", "Claude Code", "Codex"])
        self.assertEqual([r.get("automatic", {}).get("kind") for r in runs], [None, "派单", "续跑"])
        self.assertTrue(all(r["phase"] == "complete" and r["target_status"] == "待我验收" for r in runs))
        self.assertEqual(len({r["discussion_id"] for r in runs}), 1)
        self.assertEqual(len(self.notion.remote), 4)  # Initial claim plus three replies.
        self.assertEqual(value(self.notion.p, "最近运行 ID"), runs[-1]["id"])

    async def test_claude_to_codex_return_result_only(self):
        self.enable_handoff()
        self.notion.p["properties"]["执行 Agent"]["select"]["name"] = "Claude Code"
        async def research(config, run, store):
            await self.fake_runner(config, run, store)
            if run["agent"] == "Claude Code":
                run["answer"] = "@codex 调研可选方案\n" + json.dumps(outcome(run["answer"]))
            else:
                self.assertIn("调研可选方案", prompt_for(run))
        for _ in range(4):
            await Worker(self.config, self.notion, self.store, research).scan()
        self.assertEqual([r["agent"] for r in self.store.all()], ["Claude Code", "Codex"])
        self.assertIn("自动派单 · Codex", "".join(self.notion.comments.values()))

    async def test_unlimited_handoffs_allow_manual_dual_mentions_in_order(self):
        self.enable_handoff()
        worker = Worker(self.config, self.notion, self.store, self.delegate)
        for _ in range(6):
            await worker.scan()
        self.assertEqual([r["agent"] for r in self.store.all()], ["Codex", "Claude Code"] * 3)
        self.incoming("@claude @codex 手动验证")
        await self.worker().scan()
        self.restart()
        await self.worker().scan()
        self.assertEqual([r["agent"] for r in self.store.all()[6:]], ["Claude Code", "Codex"])
        self.assertTrue(all("automatic" not in r for r in self.store.all()[6:]))

    async def test_only_checked_checkbox_authorizes_dispatch(self):
        for prop in [{}, {"type":"checkbox", "checkbox":False}, {"type":"number", "number":1}]:
            self.notion.p["properties"]["允许 Agent 转派"] = prop
            self.notion.update("page-1", status="待办")
            await Worker(self.config, self.notion, self.store, self.delegate).scan()
            await self.worker().scan()
        self.assertEqual(self.calls, 3)
        self.assertFalse(any(r.get("actions") for r in self.store.all()))

    async def test_revocation_stops_pending_dispatch_even_after_restart(self):
        self.enable_handoff()
        await Worker(self.config, self.notion, self.store, self.delegate).scan()
        self.notion.p["properties"]["允许 Agent 转派"]["checkbox"] = False
        await self.worker().scan()
        self.restart()
        self.enable_handoff()
        await self.worker().scan()
        self.assertEqual(self.calls, 1)
        self.assertIn("已关闭转派", "".join(self.notion.comments.values()))

    async def test_unavailable_target_leaves_manual_takeover(self):
        self.enable_handoff()
        await Worker(self.config, self.notion, self.store, self.delegate).scan()
        self.config.agents["Claude Code"]["enabled"] = False
        await self.worker().scan()
        self.assertIn("Agent 未启用", "".join(self.notion.comments.values()))
        self.assertEqual(self.calls, 1)

    async def test_failed_target_does_not_wake_sender(self):
        self.enable_handoff()
        async def fail_review(config, run, store):
            await self.delegate(config, run, store)
            if run["agent"] == "Claude Code":
                run.update(exit_code=1, protocol_error="会话无法恢复")
        worker = Worker(self.config, self.notion, self.store, fail_review)
        for _ in range(4):
            await worker.scan()
        self.assertEqual(self.calls, 2)
        self.assertIn("自动派单失败：会话无法恢复", "".join(self.notion.comments.values()))

    async def test_callback_also_checks_checkbox(self):
        self.enable_handoff()
        worker = Worker(self.config, self.notion, self.store, self.delegate)
        await worker.scan()
        await worker.scan()
        self.notion.p["properties"]["允许 Agent 转派"]["checkbox"] = False
        await worker.scan()
        self.assertEqual(self.calls, 2)
        self.assertIn("自动续跑停止", "".join(self.notion.comments.values()))

    async def test_only_final_addressed_request_dispatches(self):
        self.enable_handoff()
        verdict = {"task_outcome": "ready_for_review", "tests": "通过", "limitations": ""}
        for note in ["文中引用 @claude 调研。", "> @claude 引用", "@claude 示例\n另外还有结论。",
                     "```text\n@claude 示例\n```", "@codex 请自己继续", "@claude"]:
            answer = note + "\n" + json.dumps(verdict)
            async def report(config, run, store):
                await self.fake_runner(config, run, store)
                run["answer"] = answer
            self.notion.update("page-1", status="待办")
            await Worker(self.config, self.notion, self.store, report).scan()
            await self.worker().scan()
        self.assertEqual(self.calls, 6)
        self.assertFalse(any(r["actions"] for r in self.store.all()))

    def stats_due(self):
        """Simulate the retry backoff elapsing."""
        with self.store.db:
            self.store.db.execute("UPDATE stats_outbox SET next_at=''")

    def assert_delivery_pending(self, run_id=None):
        run = self.store.get(run_id) if run_id else self.store.all()[-1]
        self.assertEqual(run["phase"], "delivering")
        self.assertTrue(run["delivery_error"])
        self.assertGreaterEqual(run["delivery_attempts"], 1)
        return run

    def incoming(self, text, id="human-1", bot=False, created=None):
        from notion_agent.worker import now
        comment = {"id": id, "discussion_id": "human-thread", "created_time": created or now(),
                   "created_by": {"id": "bot" if bot else "human", "type": "bot" if bot else "person"},
                   "rich_text": rich(text)}
        self.notion.incoming_comments = getattr(self.notion, "incoming_comments", []) + [comment]
        return comment

    def add_card(self, page_id, project):
        card = page()
        card["id"] = page_id
        card["properties"]["项目"]["select"]["name"] = project
        self.notion.extra_pages[page_id] = card

    async def test_service_runs_independent_directories_and_waits_for_all_jobs_on_shutdown(self):
        first = self.root / "first"
        child = first / "child"
        alias = self.root / "alias"
        second = self.root / "second"
        third = self.root / "third"
        child.mkdir(parents=True)
        second.mkdir()
        third.mkdir()
        alias.symlink_to(first, target_is_directory=True)
        config = tomllib.loads(self.path.read_text())
        config.update(poll_seconds=1, projects={name: {"path": str(path)} for name, path in (
            ("demo", first), ("alias", alias), ("child", child), ("second", second), ("third", third))})
        save(config, self.path)
        for project in ("alias", "child", "second", "third"):
            self.add_card(project, project)

        started = asyncio.Queue()
        release_second = asyncio.Event()
        cancelled = set()

        async def runner(cfg, run, store):
            page_id = run["page_id"]
            started.put_nowait(page_id)
            try:
                if page_id == "second":
                    await release_second.wait()
                    await self.fake_runner(cfg, run, store)
                else:
                    await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.add(page_id)
                raise

        worker = Worker(self.config, self.notion, self.store, runner)
        serving = asyncio.create_task(worker.serve())
        try:
            first_batch = {await asyncio.wait_for(started.get(), 5) for _ in range(2)}
            self.assertEqual(first_batch, {"page-1", "second"})
            self.assertEqual(len(worker.jobs), 2)
            self.assertEqual(value(self.notion.p, "状态"), "执行中")
            for waiting in ("alias", "child", "third"):
                self.assertEqual(value(self.notion.card(waiting), "状态"), "待办")
            comment = self.incoming("@codex 等当前任务结束再继续")
            release_second.set()
            self.assertEqual(await asyncio.wait_for(started.get(), 5), "third")
            self.assertFalse(self.store.triggered("page-1", comment["id"], "Codex"))
            self.assertEqual(value(self.notion.p, "状态"), "执行中")
            self.assertEqual(value(self.notion.card("second"), "状态"), "待我验收")
        finally:
            serving.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await serving
        self.assertEqual(cancelled, {"page-1", "third"})
        self.assertFalse(worker.jobs)
        self.assertEqual({r["phase"] for r in self.store.all(active=True)}, {"delivering"})
        self.worker().recover()
        self.assertFalse(self.store.all(active=True))
        for stopped in ("page-1", "third"):
            self.assertEqual(value(self.notion.card(stopped), "状态"), "需处理")

    async def test_once_still_runs_only_one_pending_card(self):
        self.add_card("page-2", "demo")
        await self.worker().scan()
        self.assertEqual(self.calls, 1)
        self.assertEqual(value(self.notion.card("page-2"), "状态"), "待办")

    def restart(self):
        self.store.db.close()
        self.store = Store(self.config.state_dir)

    async def test_statistics_outage_never_blocks_delivery_and_backfills_once(self):
        self.config.execution_data_source_id = "records"
        self.add_card("page-2", "demo")
        self.notion.stats_down = True
        worker = self.worker()
        await worker.scan()
        await worker.scan()
        self.assertEqual(self.calls, 2)
        for card in ("page-1", "page-2"):
            self.assertEqual(value(self.notion.card(card), "状态"), "待我验收")
        self.assertEqual(list(self.notion.comments.values()).count("结论：通过。"), 2)
        self.assertFalse(self.store.all(active=True))
        runs = [r["id"] for r in self.store.all()]
        pending = self.store.pending_stats()
        self.assertEqual({(i["channel"], i["key"]) for i in pending},
                         {("execution", runs[0]), ("execution", runs[1]), ("timings", "page-1"), ("timings", "page-2")})
        self.assertTrue(all("HTTP 502" in i["error"] and i["attempts"] >= 1 for i in pending))
        self.restart()
        self.stats_due()
        await self.worker().scan()
        self.assertEqual(self.calls, 2)
        self.notion.stats_down = False
        self.stats_due()
        await self.worker().scan()
        self.assertFalse(self.store.pending_stats())
        self.assertEqual(sorted(self.notion.execution_creates), sorted(runs))
        self.assertTrue(all(r["end"] and r["result"] == "待我验收" for r in self.notion.execution_records.values()))
        for card in ("page-1", "page-2"):
            self.assertEqual(self.notion.card(card)["properties"]["执行次数"]["number"], 1)
        writes = len(self.notion.execution_writes)
        await self.worker().scan()
        self.assertEqual(len(self.notion.execution_writes), writes)
        self.assertEqual(self.calls, 2)

    async def test_statistics_queue_survives_upgrade_of_stuck_delivery(self):
        self.store.put({"id": "legacy", "page_id": "page-1", "phase": "delivering"})
        with self.store.db:
            self.store.db.execute("DROP TABLE stats_outbox")
        self.restart()
        self.assertEqual([(i["channel"], i["key"]) for i in self.store.pending_stats()], [("execution", "legacy")])

    async def lose_card_after_execution(self, lose):
        self.add_card("page-2", "demo")
        async def runner(config, run, store):
            await self.fake_runner(config, run, store)
            if run["page_id"] == "page-1":
                lose()
        worker = Worker(self.config, self.notion, self.store, runner)
        await worker.scan()
        lost = self.assert_delivery_pending()
        await worker.scan()
        self.assertEqual(self.calls, 2)
        self.assertEqual(value(self.notion.card("page-2"), "状态"), "待我验收")
        lost = self.assert_delivery_pending(lost["id"])
        self.assertIn("结论：通过", lost["answer"])
        self.assertEqual(lost["reply_chunks"], ["结论：通过。"])
        self.assertEqual(lost["delivery_attempts"], 2)
        return worker, lost

    async def test_inaccessible_card_keeps_local_result_and_queue_moves_on(self):
        worker, lost = await self.lose_card_after_execution(lambda: self.notion.missing.add("page-1"))
        self.assertIn("HTTP 404", lost["delivery_error"])
        self.notion.missing.discard("page-1")
        await worker.scan()
        self.assertEqual(self.calls, 2)
        self.assertEqual(value(self.notion.p, "状态"), "待我验收")
        self.assertNotIn("delivery_error", self.store.get(lost["id"]))

    async def test_trashed_card_is_not_written_and_queue_moves_on(self):
        worker, lost = await self.lose_card_after_execution(lambda: self.notion.p.update(in_trash=True))
        self.assertIn("回收站", lost["delivery_error"])
        # Only page-2 received a reply; the trashed card keeps its status untouched.
        self.assertEqual(list(self.notion.comments.values()).count("结论：通过。"), 1)
        self.assertEqual(value(self.notion.p, "状态"), "执行中")
        self.notion.p["in_trash"] = False
        await worker.scan()
        self.assertEqual(value(self.notion.p, "状态"), "待我验收")

    async def test_unreadable_card_does_not_stop_the_queue(self):
        self.add_card("page-2", "demo")
        original = self.notion.body
        def body(page_id):
            if page_id == "page-1":
                raise NotionError("Notion GET blocks/page-1/children: HTTP 403")
            return original(page_id)
        self.notion.body = body
        await self.worker().scan()
        self.assertEqual(self.calls, 1)
        self.assertEqual(value(self.notion.p, "状态"), "待办")
        self.assertEqual(value(self.notion.card("page-2"), "状态"), "待我验收")

    async def test_lost_reply_response_reconciles_after_restart(self):
        self.notion = RemoteCommentNotion(page())
        self.notion.reply_fault = "lost"
        await self.worker().scan()
        run = self.assert_delivery_pending()
        self.assertEqual(value(self.notion.p, "状态"), "执行中")
        self.restart()
        await self.worker().scan()
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(self.notion.posts), 2)
        self.assertEqual(value(self.notion.p, "状态"), "待我验收")
        self.assertEqual(self.store.get(run["id"])["comment_operations"]["reply-0"]["comment_id"], "remote-2")

    async def test_uncertain_reply_is_never_resent_or_rerun(self):
        self.notion = RemoteCommentNotion(page())
        self.add_card("page-2", "demo")
        self.notion.reply_fault = "vanished"
        await self.worker().scan()
        run = self.assert_delivery_pending()
        for _ in range(2):
            self.restart()
            await self.worker().scan()
        run = self.assert_delivery_pending(run["id"])
        self.assertIn("CommentWriteUncertain", run["delivery_error"])
        self.assertEqual(self.calls, 2)
        self.assertEqual(sum(1 for page_id, body in self.notion.posts if page_id == "page-1" and body.get("discussion_id")), 1)
        self.assertEqual(value(self.notion.p, "状态"), "执行中")
        self.assertEqual(value(self.notion.card("page-2"), "状态"), "待我验收")

    async def test_rerouted_card_waits_alone_without_overwriting_human_status(self):
        self.config.agents["Claude Code"]["command"] = "/usr/bin/true"
        self.add_card("page-2", "demo")
        async def reroute(config, run, store):
            await self.fake_runner(config, run, store)
            if run["page_id"] == "page-1":
                self.notion.p["properties"]["执行 Agent"]["select"]["name"] = "Claude Code"
        worker = Worker(self.config, self.notion, self.store, reroute)
        await worker.scan()
        held = self.store.all()[0]
        self.assertEqual(held["phase"], "hold")
        self.notion.update("page-1", status="待办")
        comment = self.incoming("@codex 继续")
        await worker.scan()
        await worker.scan()
        self.assertEqual(self.calls, 2)
        self.assertEqual(value(self.notion.card("page-2"), "状态"), "待我验收")
        self.assertEqual(value(self.notion.p, "状态"), "待办")
        self.assertEqual(value(self.notion.p, "最近运行 ID"), held["id"])
        self.assertEqual(self.store.get(held["id"])["phase"], "hold")
        self.assertFalse(self.store.triggered("page-1", comment["id"], "Codex"))
        self.assertFalse(any("暂未接单" in text for text in self.notion.comments.values()))

    async def test_surviving_process_of_finished_run_still_blocks_machine(self):
        self.add_card("page-2", "demo")
        async def lingering(config, run, store):
            await self.fake_runner(config, run, store)
            run["pid"] = os.getpid()  # A process outlived the CLI and may still write the directory.
        await Worker(self.config, self.notion, self.store, lingering).scan()
        self.assertEqual(self.store.latest("page-1")["phase"], "delivering")
        await self.worker().scan()
        self.assertEqual(self.calls, 1)
        self.assertEqual(value(self.notion.p, "状态"), "执行中")
        self.assertNotIn("结论：通过。", self.notion.comments.values())
        self.assertEqual(value(self.notion.card("page-2"), "状态"), "待办")
        with patch("notion_agent.worker.process_alive", return_value=False):
            await self.worker().scan()
        self.assertEqual(self.calls, 2)
        self.assertEqual(value(self.notion.p, "状态"), "待我验收")
        self.assertEqual(value(self.notion.card("page-2"), "状态"), "待我验收")

    async def test_ambiguous_claim_never_launches_and_only_holds_that_card(self):
        self.add_card("page-2", "demo")
        original = self.notion.update
        def lost(page_id, **kwargs):
            original(page_id, **kwargs)
            if page_id == "page-1" and kwargs.get("status") == "执行中":
                self.notion.update = original
                raise NotionError("claim response lost")
        self.notion.update = lost
        worker = self.worker()
        await worker.scan()
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.store.all()[0]["phase"], "claimed")
        await worker.scan()
        self.assertEqual(self.calls, 1)
        first = self.store.all()[0]
        self.assertEqual(first["page_id"], "page-1")
        self.assertIn("未自动重跑", first["error"])
        self.assertEqual(value(self.notion.p, "状态"), "需处理")
        self.assertEqual(value(self.notion.card("page-2"), "状态"), "待我验收")

    async def test_comment_resumes_codex_and_survives_store_restart(self):
        await self.worker().scan()
        original = self.store.all()[0]
        self.incoming("@codex 请按反馈继续")
        self.store.db.close()
        self.store = Store(self.config.state_dir)
        await self.worker().scan()
        run = self.store.all()[-1]
        self.assertEqual(run["resume_session_id"], original["session_id"])
        self.assertTrue(run["primary"])
        self.assertEqual(run["discussion_id"], "human-thread")
        self.assertIn("@codex 请按反馈继续", prompt_for(run))
        await self.worker().scan()
        self.assertEqual(self.calls, 2)
        self.assertEqual(value(self.notion.p, "最近运行 ID"), run["id"])

    async def test_execution_record_start_finish_resume_and_lost_response(self):
        self.config.execution_data_source_id = "execution-source"
        self.notion.fail_execution_once = True
        worker = self.worker()
        await worker.scan()
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.notion.execution_writes[0]["result"], "执行中")
        self.assertIsNone(self.notion.execution_writes[0]["end"])
        self.assertTrue(self.notion.execution_writes[-1]["end"])
        # The lost record response neither blocks delivery nor is forgotten.
        self.assertEqual(self.store.all()[0]["phase"], "complete")
        self.assertEqual(value(self.notion.p, "状态"), "待我验收")
        self.assertEqual([item["channel"] for item in self.store.pending_stats()], ["execution"])
        self.stats_due()
        await worker.scan()
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(self.notion.execution_records), 1)
        self.assertFalse(self.store.pending_stats())
        self.config.agents["Claude Code"]["command"] = "/usr/bin/true"
        self.incoming("@claude review")
        await worker.scan()
        self.assertEqual(len(self.notion.execution_records), 2)
        self.assertTrue(all(record["end"] for record in self.notion.execution_records.values()))
        self.assertFalse(self.store.all()[-1]["primary"])

    async def test_execution_source_is_bound_to_run_before_config_changes(self):
        self.config.execution_data_source_id = "original-source"
        self.notion.fail_delivery_once = True
        worker = self.worker()
        await worker.scan()
        self.assert_delivery_pending()
        self.config.execution_data_source_id = "new-source"
        await worker.scan()
        self.assertEqual(self.store.all()[0]["execution_record"]["source_id"], "original-source")

    async def test_finished_execution_record_is_closed_when_task_routing_changes(self):
        self.config.execution_data_source_id = "records"
        async def reroute(config, run, store):
            await self.fake_runner(config, run, store)
            self.notion.p["properties"]["执行机器"]["select"]["name"] = "other-machine"
        await Worker(self.config, self.notion, self.store, reroute).scan()
        run = self.store.all()[0]
        self.assertEqual(run["phase"], "hold")
        self.assertTrue(self.notion.execution_records[run["id"]]["end"])
        self.assertEqual(value(self.notion.p, "执行机器"), "other-machine")

    async def test_auxiliary_claude_creation_resume_and_owner_return(self):
        self.config.agents["Claude Code"]["command"] = "/usr/bin/true"
        await self.worker().scan()
        owner_id = value(self.notion.p, "最近运行 ID")
        writes = len(self.notion.writes)
        self.incoming("@claude review 当前改动")
        await self.worker().scan()
        auxiliary = self.store.all()[-1]
        self.assertEqual(auxiliary["agent"], "Claude Code")
        self.assertFalse(auxiliary["primary"])
        self.assertIsNone(auxiliary["resume_session_id"])
        self.assertEqual(value(self.notion.p, "最近运行 ID"), owner_id)
        self.assertEqual(len(self.notion.writes), writes)
        self.assertIn("结论：通过", auxiliary["context"])
        self.incoming("@claude 再看一遍", "human-2")
        await self.worker().scan()
        self.assertEqual(self.store.all()[-1]["resume_session_id"], auxiliary["session_id"])
        self.incoming("@codex 修正 review 发现", "human-3")
        await self.worker().scan()
        self.assertTrue(self.store.all()[-1]["primary"])
        self.assertEqual(self.calls, 4)

    async def test_draft_comment_can_start_with_empty_body(self):
        self.notion.update("page-1", status="草稿")
        self.notion.body_text = ""
        self.incoming("@codex 请检查当前目录")
        await self.worker().scan()
        self.assertEqual(self.calls, 1)
        self.assertTrue(self.store.all()[0]["primary"])
        self.assertEqual(value(self.notion.p, "状态"), "待我验收")

    async def test_comments_work_in_every_status(self):
        for index, status in enumerate(("草稿", "待办", "执行中", "待我验收", "完成", "需处理")):
            self.notion.update("page-1", status=status)
            self.incoming("@codex 继续", f"human-{index}")
            await self.worker().scan()
        self.assertEqual(self.calls, 6)

    async def test_bot_and_pre_activation_comments_never_trigger(self):
        self.notion.update("page-1", status="草稿")
        self.incoming("@codex 旧指令", created="2020-01-01T00:00:00Z")
        self.incoming("@codex 自动回复", "bot-comment", bot=True)
        await self.worker().scan()
        self.assertEqual(self.calls, 0)
        self.assertFalse(self.store.all())

    async def test_person_attributed_service_reply_never_triggers_after_restart(self):
        await self.worker().scan()
        run = self.store.all()[0]
        reply_id = run["comment_operations"]["reply-0"]["comment_id"]
        self.incoming("用 @codex 继续", reply_id)
        self.store.db.close()
        self.store = Store(self.config.state_dir)
        await self.worker().scan()
        self.assertEqual(self.calls, 1)
        # A genuine request by the same person still dispatches.
        self.incoming("@codex 请继续", "real-human-request")
        await self.worker().scan()
        self.assertEqual(self.calls, 2)

    async def test_person_attributed_problem_notice_never_triggers(self):
        self.notion.update("page-1", status="草稿")
        notice = self.incoming("@codex 请继续", "service-notice")
        self.store.save_metadata("comment-problem:page-1:original:Codex:disabled",
                                 {"comment_id": notice["id"]})
        await self.worker().scan()
        self.assertEqual(self.calls, 0)

    async def test_multi_mentions_are_serial_and_deduplicated(self):
        self.config.agents["Claude Code"]["command"] = "/usr/bin/true"
        self.incoming("@codex @claude @CODEX 请分别检查")
        await self.worker().scan()
        await self.worker().scan()
        await self.worker().scan()
        self.assertEqual([r["agent"] for r in self.store.all()], ["Codex", "Claude Code"])
        self.assertEqual(self.calls, 2)

    async def test_auxiliary_response_loss_does_not_execute_twice(self):
        self.config.agents["Claude Code"]["command"] = "/usr/bin/true"
        await self.worker().scan()
        self.config.comments = False
        self.incoming("@claude review")
        self.notion.fail_delivery_once = True
        await self.worker().scan()
        self.assert_delivery_pending()
        await self.worker().scan()
        self.assertEqual(self.calls, 2)
        self.assertEqual(self.store.all()[-1]["phase"], "complete")

    async def test_missing_session_fails_closed_with_readable_notice(self):
        await self.worker().scan()
        run = self.store.all()[0]
        run["session_id"] = None
        self.store.put(run)
        self.incoming("@codex 继续")
        await self.worker().scan()
        await self.worker().scan()
        self.assertEqual(self.calls, 1)
        self.assertEqual(sum("会话 ID 缺失" in s for s in self.notion.comments.values()), 1)

    async def test_disabled_agent_notice_retries_when_enabled(self):
        self.config.agents["Claude Code"]["command"] = "/usr/bin/true"
        self.config.agents["Claude Code"]["enabled"] = False
        self.notion.update("page-1", status="草稿")
        self.incoming("@claude 检查")
        await self.worker().scan()
        await self.worker().scan()
        self.assertEqual(self.calls, 0)
        self.assertEqual(len(self.notion.comments), 1)
        self.config.agents["Claude Code"]["enabled"] = True
        await self.worker().scan()
        self.assertEqual(self.calls, 1)
        self.assertTrue(self.store.all()[0]["primary"])

    async def test_comment_dry_run_has_no_writes_or_execution(self):
        self.incoming("@codex 检查")
        await self.worker().scan(dry_run=True)
        self.assertEqual(self.calls, 0)
        self.assertFalse(self.store.all())
        self.assertFalse(self.notion.writes)
        self.assertFalse(self.notion.comments)

    async def test_auxiliary_failure_preserves_issue_status(self):
        self.config.agents["Claude Code"]["command"] = "/usr/bin/true"
        await self.worker().scan()
        before = copy.deepcopy(self.notion.p)
        async def fail(config, run, store):
            run.update(exit_code=1, protocol_success=False, answer="review 失败，请检查权限")
        self.incoming("@claude review")
        await Worker(self.config, self.notion, self.store, fail).scan()
        for name in ("最近耗时（秒）", "累计耗时（秒）", "执行次数"):
            before["properties"][name] = self.notion.p["properties"][name]
        self.assertEqual(self.notion.p, before)
        self.assertEqual(self.notion.p["properties"]["执行次数"]["number"], 2)
        self.assertIn("review 失败，请检查权限", self.notion.comments.values())

    async def test_timing_write_response_loss_does_not_double_count_or_rerun(self):
        original = self.notion.update_timings
        def ambiguous(page_id, stats):
            original(page_id, stats)
            self.notion.update_timings = original
            raise NotionError("timing write response lost")
        self.notion.update_timings = ambiguous
        worker = self.worker()
        await worker.scan()
        first = copy.deepcopy(self.notion.timing_writes[-1])
        self.assertEqual(self.store.all()[0]["phase"], "complete")
        self.assertEqual(value(self.notion.p, "状态"), "待我验收")
        self.assertEqual(self.store.pending_stats()[0]["channel"], "timings")
        self.stats_due()
        await worker.scan()
        self.assertEqual(self.calls, 1)
        self.assertFalse(self.store.pending_stats())
        self.assertEqual(self.notion.timing_writes[-1], first)
        self.assertEqual(first["execution_count"], 1)
        self.incoming("@codex 继续")
        await worker.scan()
        self.assertEqual(self.notion.timing_writes[-1]["execution_count"], 2)

    async def test_draft_rejection_has_no_execution_timing(self):
        self.notion.body_text = ""
        await self.worker().scan()
        self.assertEqual(value(self.notion.p, "状态"), "草稿")
        self.assertFalse(self.notion.timing_writes)

    async def test_context_excludes_later_pending_comments(self):
        self.incoming("@codex 第一个请求")
        self.incoming("@codex 第二个请求", "human-2")
        await self.worker().scan()
        self.assertNotIn("第二个请求", prompt_for(self.store.all()[0]))

    async def test_auxiliary_project_move_holds_without_status_write(self):
        self.config.agents["Claude Code"]["command"] = "/usr/bin/true"
        await self.worker().scan()
        self.incoming("@claude review")
        writes = len(self.notion.writes)
        async def moved(config, run, store):
            await self.fake_runner(config, run, store)
            self.notion.p["properties"]["执行机器"]["select"]["name"] = "another-machine"
        await Worker(self.config, self.notion, self.store, moved).scan()
        self.assertEqual(self.store.all()[-1]["phase"], "hold")
        self.assertEqual(len(self.notion.writes), writes)

    async def test_foreign_run_never_resumes_or_reassigns(self):
        self.notion.update("page-1", status="草稿", run_id="foreign")
        self.incoming("@codex 继续")
        writes = len(self.notion.writes)
        await self.worker().scan()
        self.assertEqual(self.calls, 0)
        self.assertFalse(self.store.all())
        self.assertEqual(len(self.notion.writes), writes)
        self.assertTrue(any("不属于本机" in t for t in self.notion.comments.values()))

    async def test_resume_subprocess_receives_exact_session_and_stdin(self):
        script = self.root / "mock-resume"
        script.write_text('#!/usr/bin/env python3\nimport json,sys\nassert "resume" in sys.argv\nassert sys.argv[sys.argv.index("resume")+1] == "original-session"\nassert "本次用户评论" in sys.stdin.read()\nprint(json.dumps({"type":"thread.started","thread_id":"original-session"}))\nprint(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"continued"}}))\nprint(json.dumps({"type":"turn.completed"}))\n')
        script.chmod(0o700)
        run_dir = self.root / "resume-run"
        run_dir.mkdir()
        (run_dir / "prompt.txt").write_text("本次用户评论：继续")
        run = {"id": "resume", "page_id": "resume", "phase": "claimed", "agent": "Codex", "run_dir": str(run_dir),
               "workspace": str(self.root), "agent_settings": {"command": str(script)}, "resume_session_id": "original-session"}
        await execute(self.config, run, self.store)
        self.assertEqual(run["exit_code"], 0)
        self.assertEqual(run["session_id"], "original-session")
        self.assertEqual(run["answer"], "continued")

    async def test_comment_draft_without_agent_selection_uses_mention(self):
        self.notion.update("page-1", status="草稿")
        self.notion.p["properties"]["执行 Agent"]["select"] = None
        self.incoming("@codex 请检查")
        await self.worker().scan()
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.store.all()[0]["agent"], "Codex")

    async def test_failed_resume_can_retry_original_session(self):
        await self.worker().scan()
        self.incoming("@codex 继续")
        async def fail(config, run, store):
            run.update(exit_code=1, protocol_success=False, session_id=None, answer="CLI 连接失败")
        await Worker(self.config, self.notion, self.store, fail).scan()
        self.incoming("@codex 再试", "human-2")
        await self.worker().scan()
        self.assertEqual(self.store.all()[-1]["resume_session_id"], "actual-session")
        self.assertEqual(self.calls, 2)

    async def test_issue_owner_does_not_change_via_dropdown(self):
        await self.worker().scan()
        self.config.agents["Claude Code"]["command"] = "/usr/bin/true"
        self.notion.p["properties"]["执行 Agent"]["select"]["name"] = "Claude Code"
        self.notion.update("page-1", status="待办")
        await self.worker().scan()
        self.assertEqual(self.calls, 1)
        self.assertIn("主 Agent 是 Codex", value(self.notion.p, "运行摘要"))

    def relation_project(self):
        self.notion.project_property_type = "relation"
        self.notion.project_relation_target = "remote-projects"
        self.config.projects_data_source_id = "remote-projects"
        self.notion.p["properties"]["项目"] = {"type": "relation", "relation": [{"id": "project-1"}]}
        self.notion.p["properties"].pop("执行机器", None)
        remote = {"project-1": {"name": "demo", "path": str(self.root), "machine_id": "test-machine",
                                "notion_page_id": "project-1", "notion_last_edited_time": "v1"}}
        self.notion.projects = lambda source, machine, by_id=False: copy.deepcopy(remote)
        original_page = self.notion.page
        original_body = self.notion.body
        def project_page(id):
            if id != "project-1":
                return original_page(id)
            return {"last_edited_time": "v1", "properties": {
                "项目": {"type": "title", "title": rich(remote[id]["name"])},
                "执行机器": {"type": "rich_text", "rich_text": rich(remote[id]["machine_id"])},
                "本机目录": {"type": "rich_text", "rich_text": rich(remote[id]["path"])}}}
        self.notion.page = project_page
        self.notion.body = lambda id: "项目指令" if id == "project-1" else original_body(id)
        return remote

    def subitem_tree(self):
        self.relation_project()
        self.notion.parent_property = "父项目"
        child = self.notion.p
        root = copy.deepcopy(child)
        root["id"] = "root"
        root["properties"]["状态"]["status"]["name"] = "完成"
        root["properties"]["执行 Agent"]["select"]["name"] = "Claude Code"
        root["properties"]["类型"]["select"]["name"] = "代码"
        parent = copy.deepcopy(root)
        parent["id"] = "parent"
        parent["url"] = "https://www.notion.so/parent-link"
        parent["properties"]["任务"]["title"] = rich("直接父卡标题")
        parent["properties"]["允许 Agent 转派"] = {"type": "checkbox", "checkbox": True}
        for card, ancestor in ((parent, root), (child, parent)):
            card["properties"]["父项目"] = {"type": "relation", "relation": [{"id": ancestor["id"]}]}
            card["properties"]["项目"]["relation"] = []
            for key in ("执行 Agent", "类型"):
                card["properties"][key]["select"] = None
        self.config.agents["Claude Code"]["command"] = "/usr/bin/true"
        self.notion.extra_pages.update(parent=parent, root=root)
        bodies = {"root": "祖卡独有正文", "parent": "直接父卡正文", child["id"]: "当前任务", "project-1": "项目指令"}
        self.notion.body = lambda key: bodies[key]
        return child, parent, root, bodies

    async def test_subitem_inherits_and_resumes_with_only_fresh_direct_parent_context(self):
        child, parent, root, bodies = self.subitem_tree()
        worker = self.worker()
        with patch.object(self.notion, "body", wraps=self.notion.body) as body:
            await worker.scan()
            bodies["parent"] = "父卡修订正文"
            self.incoming("@claude 请继续")
            await worker.scan()
        self.assertNotIn(("root",), [call.args for call in body.call_args_list])
        runs = self.store.history(child["id"])
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[1]["resume_session_id"], "actual-session")
        for run, expected in zip(runs, ("直接父卡正文", "父卡修订正文")):
            snap = run["snapshot"]
            self.assertEqual((snap["agent"], snap["kind"], snap["project"]), ("Claude Code", "代码", "project-1"))
            self.assertFalse(snap["delegation_allowed"])
            directory = Path(run["run_dir"])
            self.assertEqual(json.loads((directory / "snapshot.json").read_text())["snapshot"], snap)
            prompt = (directory / "prompt.txt").read_text()
            for field, actual in (("url", parent["url"]), ("body", expected), ("title", "直接父卡标题")):
                self.assertEqual(snap["parent"][field], actual)
                self.assertIn(actual, prompt)
            self.assertNotIn("祖卡独有正文", prompt)
        self.assertEqual(value(child, "状态"), "待我验收")
        for ancestor in (root, parent):
            self.assertEqual(value(ancestor, "状态"), "完成")
            self.assertFalse(self.store.history(ancestor["id"]))
        self.assertFalse(value(child, "项目"))

    async def test_subitem_overrides_remote_project_agent_and_type(self):
        child, parent, root, _ = self.subitem_tree()
        root["properties"]["项目"]["relation"] = [{"id": "foreign-project"}]
        self.incoming("@codex 开始")
        await self.worker().scan()
        self.assertFalse(self.notion.comments)
        self.assertFalse(self.notion.writes)
        child["properties"]["项目"]["relation"] = [{"id": "project-1"}]
        child["properties"]["执行 Agent"]["select"] = {"name": "Codex"}
        parent["properties"]["类型"]["select"] = {"name": "调研"}
        await self.worker().scan()
        snap = self.store.all()[0]["snapshot"]
        self.assertEqual((snap["agent"], snap["kind"], snap["project"]), ("Codex", "调研", "project-1"))
        child["properties"]["类型"]["select"] = {"name": "代码"}
        self.incoming("@codex 再检查", "human-2")
        await self.worker().scan()
        self.assertEqual(self.store.all()[-1]["snapshot"]["kind"], "代码")

    async def test_inherited_route_changed_before_delivery_holds_child_only(self):
        child, parent, root, _ = self.subitem_tree()
        async def move(config, run, store):
            await self.fake_runner(config, run, store)
            root["properties"]["项目"]["relation"] = [{"id": "foreign-project"}]
        await Worker(self.config, self.notion, self.store, move).scan()
        self.assertEqual(self.store.history(child["id"])[0]["phase"], "hold")
        self.assertEqual(value(parent, "状态"), "完成")
        self.assertEqual(value(root, "状态"), "完成")

    async def test_subitem_parent_edit_during_claim_retries_without_starting(self):
        child, parent, _, _ = self.subitem_tree()
        original = self.notion.body
        def edit_parent(key):
            if key == "parent":
                parent["last_edited_time"] = "v2"
            return original(key)
        self.notion.body = edit_parent
        await self.worker().scan()
        self.assertEqual(self.calls, 0)
        self.assertEqual(value(child, "状态"), "待办")
        await self.worker().scan()
        self.assertEqual(self.calls, 1)

    async def test_relation_routes_from_project_without_task_machine(self):
        self.relation_project()
        await self.worker().scan()
        self.assertEqual(self.calls, 1)
        run = self.store.all()[0]
        self.assertEqual(run["snapshot"]["project"], "project-1")
        self.assertEqual(run["project_settings"]["name"], "demo")
        self.assertEqual(run["machine"], "test-machine")
        self.assertEqual(value(self.notion.p, "状态"), "待我验收")

    async def test_relation_project_rename_keeps_routing_identity(self):
        remote = self.relation_project()
        async def rename(config, run, store):
            await self.fake_runner(config, run, store)
            remote["project-1"]["name"] = "renamed"
        await Worker(self.config, self.notion, self.store, rename).scan()
        self.assertEqual(self.store.all()[0]["phase"], "complete")

    async def test_relation_project_move_after_execution_holds_delivery(self):
        remote = self.relation_project()
        async def move(config, run, store):
            await self.fake_runner(config, run, store)
            remote["project-1"]["machine_id"] = "other-machine"
        await Worker(self.config, self.notion, self.store, move).scan()
        self.assertEqual(self.store.all()[0]["phase"], "hold")
        self.assertEqual(value(self.notion.p, "状态"), "执行中")

    async def test_relation_requires_exactly_one_project(self):
        self.relation_project()
        worker = self.worker()
        worker.refresh_projects()
        self.notion.p["properties"]["项目"]["relation"] = []
        self.assertIn("只选择一个", worker.candidate(self.notion.p))
        self.notion.p["properties"]["项目"]["relation"] = [{"id": "project-1"}, {"id": "project-2"}]
        self.assertIn("只选择一个", worker.candidate(self.notion.p))

    def project_instructions(self, text):
        self.config.projects["demo"].update(notion_page_id="project-page", notion_last_edited_time="v1")
        original_page = self.notion.page
        original_body = self.notion.body
        self.notion.page = lambda id: {"last_edited_time": "v1"} if id == "project-page" else original_page(id)
        self.notion.body = lambda id: text if id == "project-page" else original_body(id)

    async def test_project_body_is_snapshotted_separately_from_task(self):
        self.project_instructions("每次修改需要 review。\n保留已有改动。")
        await self.worker().scan()
        run = self.store.all()[0]
        self.assertEqual(run["project_settings"]["instructions"], "每次修改需要 review。\n保留已有改动。")
        self.assertEqual((Path(run["run_dir"]) / "project-instructions.txt").read_text(), run["project_settings"]["instructions"])
        self.assertNotIn("每次修改需要 review", (Path(run["run_dir"]) / "prompt.txt").read_text())
        self.config.projects["demo"]["instructions"] = "later edit"
        self.assertNotEqual(self.store.all()[0]["project_settings"]["instructions"], "later edit")

    async def test_project_body_failure_prevents_claim_and_execution(self):
        self.project_instructions("instructions")
        original = self.notion.body
        def unsupported(id):
            if id == "project-page":
                raise ValueError("unsupported block")
            return original(id)
        self.notion.body = unsupported
        await self.worker().scan()
        self.assertEqual(self.calls, 0)
        self.assertFalse(self.store.all())
        self.assertEqual(value(self.notion.p, "状态"), "草稿")

    async def test_project_edit_during_read_skips_launch(self):
        self.project_instructions("instructions")
        original = self.notion.page
        self.notion.page = lambda id: {"last_edited_time": "v2"} if id == "project-page" else original(id)
        await self.worker().scan()
        self.assertEqual(self.calls, 0)
        self.assertFalse(self.store.all())

    async def test_default_project_instructions_are_not_inherited(self):
        self.config.projects["default"] = {"path": str(self.root), "instructions": "default-only"}
        await self.worker().scan()
        run = self.store.all()[0]
        self.assertNotIn("default-only", (Path(run["run_dir"]) / "project-instructions.txt").read_text())

    async def test_success_and_no_duplicate_scan(self):
        worker = self.worker()
        await worker.scan()
        await worker.scan()
        self.assertEqual(self.calls, 1)
        self.assertEqual(value(self.notion.p, "状态"), "待我验收")
        self.assertEqual(len(self.notion.blocks), 0)
        self.assertEqual(len(self.notion.comments), 2)
        self.assertEqual(self.store.all()[0]["phase"], "complete")
        self.assertEqual(self.notion.body_text, "请输出测试报告，验收：给出结论。")

    async def test_conversation_has_only_readable_content_and_summary(self):
        await self.worker().scan()
        messages = list(self.notion.comments.values())
        self.assertEqual(messages, ["Codex 开始处理这个任务。", "结论：通过。"])
        self.assertEqual(list(self.notion.comment_names.values()), ["调度程序", "Codex"])
        summary = value(self.notion.p, "运行摘要")
        self.assertEqual(summary, "已完成，等你验收。")
        self.assertNotIn(self.store.all()[0]["id"], "\n".join(messages) + summary)
        self.assertFalse(self.notion.blocks)

    async def test_claude_answer_is_signed_as_claude(self):
        self.config.agents["Claude Code"]["command"] = "/usr/bin/true"
        self.notion.p["properties"]["执行 Agent"]["select"]["name"] = "Claude Code"
        await self.worker().scan()
        self.assertEqual(list(self.notion.comment_names.values()), ["调度程序", "Claude Code"])

    async def test_workspace_guide_is_saved_and_not_inserted_into_task(self):
        self.config.agent_guide_page_id = "guide"
        original = self.notion.body
        self.notion.body = lambda id: "使用工作区的说明" if id == "guide" else original(id)
        await self.worker().scan()
        run = self.store.all()[0]
        self.assertEqual(run["workspace_guide"]["content"], "使用工作区的说明")
        self.assertEqual((Path(run["run_dir"]) / "workspace-guide.txt").read_text(), "使用工作区的说明")

    async def test_lost_delivery_response_reconciles_without_rerunning(self):
        self.notion.fail_delivery_once = True
        worker = self.worker()
        await worker.scan()
        self.assert_delivery_pending()
        self.assertEqual(self.calls, 1)
        self.assertEqual(value(self.notion.p, "状态"), "执行中")
        await worker.scan()
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(self.notion.blocks), 0)
        self.assertEqual(len(self.notion.comments), 2)
        self.assertEqual(value(self.notion.p, "状态"), "待我验收")

    async def test_reset_after_delivery_creates_new_run(self):
        worker = self.worker()
        await worker.scan()
        first = value(self.notion.p, "最近运行 ID")
        self.notion.update("page-1", status="待办")
        await worker.scan()
        self.assertEqual(self.calls, 2)
        self.assertNotEqual(first, value(self.notion.p, "最近运行 ID"))

    async def test_other_machine_and_unknown_project_not_started(self):
        self.notion.p["properties"]["执行机器"]["select"]["name"] = "other"
        await self.worker().scan()
        self.assertEqual(self.calls, 0)
        self.assertEqual(value(self.notion.p, "状态"), "待办")
        self.notion.p["properties"]["执行机器"]["select"]["name"] = "test-machine"
        self.notion.p["properties"]["项目"]["select"]["name"] = "missing"
        await self.worker().scan()
        self.assertEqual(self.calls, 0)
        self.assertEqual(value(self.notion.p, "状态"), "草稿")

    async def test_invalid_task_returns_to_draft_until_explicit_resubmission(self):
        self.notion.p["properties"]["类型"]["select"]["name"] = ""
        worker = self.worker()
        await worker.scan()
        self.assertEqual(value(self.notion.p, "状态"), "草稿")
        self.assertIn("类型必须为代码或调研", value(self.notion.p, "运行摘要"))
        self.assertEqual(self.calls, 0)
        writes = len(self.notion.writes)
        self.notion.p["properties"]["类型"]["select"]["name"] = "代码"
        await worker.scan()
        self.assertEqual(len(self.notion.writes), writes)
        self.assertEqual(self.calls, 0)
        self.notion.update("page-1", status="待办")
        await worker.scan()
        self.assertEqual(self.calls, 1)
        self.assertEqual(value(self.notion.p, "状态"), "待我验收")

    async def test_reject_preserves_human_changes_since_scan(self):
        self.notion.p["properties"]["类型"]["select"]["name"] = ""
        original_query = self.notion.query
        def edited_query(*args, **kwargs):
            rows = original_query(*args, **kwargs)
            self.notion.update("page-1", status="完成")
            return rows
        self.notion.query = edited_query
        await self.worker().scan()
        self.assertEqual(value(self.notion.p, "状态"), "完成")
        self.assertEqual(self.calls, 0)

    async def test_empty_body_not_started_and_dry_run_is_read_only(self):
        self.notion.body_text = " "
        rows = await self.worker().scan(dry_run=True)
        self.assertFalse(rows[0]["eligible"])
        self.assertFalse(self.notion.writes)
        self.assertFalse(self.store.all())

    async def test_ambiguous_spawning_blocks_restarts(self):
        self.add_card("page-2", "demo")
        self.store.put({"id": "uncertain", "page_id": "page-1", "phase": "spawning"})
        await self.worker().scan()
        await self.worker().scan()
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.store.get("uncertain")["phase"], "hold")
        self.assertEqual(value(self.notion.card("page-2"), "状态"), "待办")

    async def test_failure_requires_attention(self):
        async def fail(config, run, store):
            run.update(exit_code=1, protocol_success=False, answer="测试未通过")
        await Worker(self.config, self.notion, self.store, fail).scan()
        self.assertEqual(value(self.notion.p, "状态"), "需处理")

    async def test_no_structured_verdict_requires_attention(self):
        async def unstructured(config, run, store):
            run.update(exit_code=0, protocol_success=True, answer="完成啦")
        await Worker(self.config, self.notion, self.store, unstructured).scan()
        self.assertEqual(value(self.notion.p, "状态"), "需处理")

    async def test_human_reroute_preserved_and_blocks_new_work(self):
        async def reroute(config, run, store):
            await self.fake_runner(config, run, store)
            self.notion.p["properties"]["执行机器"]["select"]["name"] = "other-machine"
        await Worker(self.config, self.notion, self.store, reroute).scan()
        self.assertEqual(self.store.all()[0]["phase"], "hold")
        self.assertEqual(value(self.notion.p, "执行机器"), "other-machine")
        self.assertEqual(value(self.notion.p, "状态"), "执行中")

    async def test_live_recovery_not_reexecuted(self):
        worker = self.worker()
        self.add_card("page-2", "demo")
        self.store.put({"id": "live", "page_id": "page-1", "phase": "running", "pid": os.getpid()})
        await worker.scan()
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.store.get("live")["phase"], "running")
        self.assertEqual(value(self.notion.card("page-2"), "状态"), "待办")

    async def test_completed_process_crash_recovery_delivers_attention(self):
        self.notion.fail_delivery_once = True
        worker = self.worker()
        await worker.scan()
        run = self.assert_delivery_pending()
        run.update(phase="running", pid=99999999)
        self.store.put(run)
        await worker.scan()
        self.assertEqual(self.calls, 1)
        self.assertEqual(value(self.notion.p, "状态"), "需处理")

    async def test_actual_subprocess_runner_with_mock_cli(self):
        script = self.root / "mock-cli"
        script.write_text('#!/usr/bin/env python3\nimport json,sys\nsys.stdin.read()\nprint(json.dumps({"type":"thread.started","thread_id":"mock-thread"}))\nprint(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"hello"}}))\nprint(json.dumps({"type":"turn.completed"}))\n')
        script.chmod(0o700)
        run_dir = self.root / "run"
        run_dir.mkdir()
        (run_dir / "prompt.txt").write_text("prompt `$(do not run)`")
        run = {"id": "mock", "page_id": "mock", "phase": "claimed", "agent": "Codex", "run_dir": str(run_dir),
               "workspace": str(run_dir), "agent_settings": {"command": str(script)}}
        await execute(self.config, run, self.store)
        self.assertEqual(run["exit_code"], 0)
        self.assertEqual(run["session_id"], "mock-thread")
        self.assertTrue(run["protocol_success"])

    async def test_subprocess_timeout_stops_process(self):
        script = self.root / "sleep-cli"
        script.write_text('#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n')
        script.chmod(0o700)
        run_dir = self.root / "run"
        run_dir.mkdir()
        (run_dir / "prompt.txt").write_text("prompt")
        run = {"id": "timeout", "page_id": "timeout", "phase": "claimed", "agent": "Codex", "run_dir": str(run_dir),
               "workspace": str(run_dir), "agent_settings": {"command": str(script)}}
        self.config.timeout_seconds = 0.1
        await execute(self.config, run, self.store)
        self.assertIn("超时", run["error"])
        self.assertFalse(run["protocol_success"])

    async def test_actual_claude_subprocess_protocol(self):
        script = self.root / "mock-claude"
        script.write_text('#!/usr/bin/env python3\nimport json,sys\nsys.stdin.read()\nprint(json.dumps({"type":"system","subtype":"init","session_id":"claude-session"}))\nprint(json.dumps({"type":"result","subtype":"success","session_id":"claude-session","result":"report"}))\n')
        script.chmod(0o700)
        run_dir = self.root / "claude-run"
        run_dir.mkdir()
        (run_dir / "prompt.txt").write_text("prompt")
        run = {"id": "claude", "page_id": "claude", "phase": "claimed", "agent": "Claude Code", "run_dir": str(run_dir),
               "workspace": str(run_dir), "agent_settings": {"command": str(script)}}
        await execute(self.config, run, self.store)
        self.assertEqual(run["session_id"], "claude-session")
        self.assertEqual(run["answer"], "report")
        self.assertTrue(run["protocol_success"])

    async def test_shutdown_cancels_process_and_keeps_delivery(self):
        async def interrupted(config, run, store):
            raise asyncio.CancelledError()
        worker = Worker(self.config, self.notion, self.store, interrupted)
        with self.assertRaises(asyncio.CancelledError):
            await worker.scan()
        saved = self.store.all()[0]
        self.assertEqual(saved["phase"], "delivering")
        self.assertEqual(saved["target_status"], "需处理")
        await self.worker().scan()
        self.assertEqual(self.calls, 0)
        self.assertEqual(value(self.notion.p, "状态"), "需处理")

    async def test_final_status_response_lost_does_not_rerun(self):
        original = self.notion.update
        lost = [False]
        def update(page_id, **kwargs):
            original(page_id, **kwargs)
            if kwargs.get("status") == "待我验收" and not lost[0]:
                lost[0] = True
                raise NotionError("status response lost")
        self.notion.update = update
        worker = self.worker()
        await worker.scan()
        self.assert_delivery_pending()
        await worker.scan()
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(self.notion.blocks), 0)
        self.assertEqual(len(self.notion.comments), 2)
        self.assertEqual(self.store.all()[0]["phase"], "complete")

    async def test_remote_projects_refresh_and_dry_run_does_not_sync(self):
        self.config.projects_data_source_id = "remote"
        remote = {"demo": {"path": str(self.root)}}
        self.notion.projects = lambda source, machine: copy.deepcopy(remote)
        syncs = []
        self.notion.register = lambda machine, keys: syncs.append(keys)
        worker = self.worker()
        await worker.scan(dry_run=True)
        self.assertFalse(syncs)
        remote.clear()
        rows = await worker.scan(dry_run=True)
        self.assertFalse(rows[0]["eligible"])
        self.assertFalse(self.config.projects)

    async def test_remote_project_failure_never_uses_stale_mapping(self):
        self.config.projects_data_source_id = "remote"
        def unavailable(source, machine):
            raise NotionError("offline")
        self.notion.projects = unavailable
        with self.assertRaises(NotionError):
            await self.worker().scan()
        self.assertEqual(self.calls, 0)



class UnitTests(unittest.TestCase):
    def test_service_startup_ignores_execution_table_outage_but_doctor_reports_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            data = initial()
            data["execution_data_source_id"] = "records"
            save(data, path)
            store = Store(Path(directory) / ".state")
            self.addCleanup(store.db.close)
            with (patch("notion_agent.cli.Notion", autospec=True) as notion_cls,
                  patch("notion_agent.cli.Worker", autospec=True) as worker_cls,
                  patch("notion_agent.cli.Store", return_value=store),
                  patch.dict(os.environ, {"NOTION_TOKEN": "fake"})):
                notion = notion_cls.return_value
                notion.parent_property = None
                notion.check_execution_schema.side_effect = NotionError("Execution table is unavailable")
                cli.run(cli.parser().parse_args(["--config", str(path), "run"]))
                worker_cls.return_value.serve.assert_awaited_once()
                notion.check_schema.assert_called_once()
                notion.check_execution_schema.assert_not_called()
                with redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as failure:
                    cli.run(cli.parser().parse_args(["--config", str(path), "doctor"]))
                self.assertEqual(failure.exception.code, 1)
                self.assertIn("Execution table is unavailable", output.getvalue())
                notion.check_execution_schema.assert_called_once_with("records")

    def test_execution_create_retries_after_http_rejection(self):
        notion = Notion("fake", "tasks")
        notion._execution_source_checked = "records"
        run = {"id": "run-1", "page_id": "task-1", "agent": "Codex", "machine": "mac",
               "started_at": "2026-11-19T01:00:00Z", "snapshot": {"title": "Task"}}
        saved = []
        creates = []

        def urlopen(request, timeout):
            if request.full_url.endswith("/query"):
                return io.BytesIO(b'{"results": [], "has_more": false}')
            if request.get_method() == "POST" and request.full_url.endswith("/pages"):
                creates.append(request)
                if len(creates) == 1:
                    raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)
                return io.BytesIO(b'{"id": "remote-record"}')
            return io.BytesIO(b'{"id": "remote-record"}')  # Later sync updates the same row.

        with patch("notion_agent.notion.urllib.request.urlopen", side_effect=urlopen), patch("notion_agent.notion.time.sleep"):
            with self.assertRaises(NotionRequestRejected):
                notion.sync_execution("records", run, lambda: saved.append(copy.deepcopy(run)))
            self.assertTrue(saved[0]["execution_record"]["attempted"])
            self.assertNotIn("attempted", saved[-1]["execution_record"])
            run = saved[-1]  # Verify the database copy is also retryable after restart.
            self.assertEqual(notion.sync_execution("records", run, lambda: None), "remote-record")
            self.assertEqual(notion.sync_execution("records", run, lambda: None), "remote-record")
        self.assertEqual(len(creates), 2)

    def test_execution_create_lost_response_reconciles_same_row(self):
        notion = Notion("fake", "tasks")
        notion._execution_source_checked = "records"
        run = {"id": "run-1", "page_id": "task-1", "agent": "Codex", "machine": "mac",
               "started_at": "2026-11-19T01:00:00Z", "snapshot": {"title": "Task"}}
        rows = []
        creates = []
        patches = []
        saved = []
        def request(method, path, body=None):
            if path.endswith("/query"):
                return {"results": rows, "has_more": False}
            if method == "POST" and path == "pages":
                creates.append(body)
                rows.append({"id": "remote-record", "properties": body["properties"]})
                raise NotionError("create response lost")
            if method == "PATCH":
                patches.append(body)
                return {"id": "remote-record"}
            raise AssertionError((method, path))
        notion.request = request
        persist = lambda: saved.append(copy.deepcopy(run))
        with self.assertRaises(NotionError):
            notion.sync_execution("records", run, persist)
        self.assertTrue(saved[-1]["execution_record"]["attempted"])
        run.update(ended_at="2026-11-19T01:00:30Z", target_status="待我验收", trigger={"id": "comment"})
        self.assertEqual(notion.sync_execution("records", run, persist), "remote-record")
        self.assertEqual(len(creates), 1)
        props = patches[-1]["properties"]
        self.assertEqual(props["执行时间"]["date"]["end"], run["ended_at"])
        self.assertEqual(props["结束时间（原始）"]["rich_text"][0]["text"]["content"], run["ended_at"])
        self.assertEqual(props["耗时（秒）"]["number"], 30)
        self.assertEqual(props["触发方式"]["select"]["name"], "评论续聊")
        self.assertNotIn("耗时（分钟）", props)

    def test_uncertain_or_duplicate_execution_records_are_not_recreated(self):
        notion = Notion("fake", "tasks")
        notion._execution_source_checked = "records"
        run = {"id": "run-1", "page_id": "task-1", "agent": "Codex", "machine": "mac",
               "started_at": "2026-11-19T01:00:00Z", "snapshot": {"title": "Task"},
               "execution_record": {"source_id": "records", "attempted": True}}
        calls = []
        def request(method, path, body=None):
            calls.append(path)
            return {"results": [], "has_more": False}
        notion.request = request
        with self.assertRaises(ExecutionWriteUncertain):
            notion.sync_execution("records", run, lambda: None)
        self.assertNotIn("pages", calls)
        notion.request = lambda *args: {"results": [{"id": "one"}, {"id": "two"}]}
        with self.assertRaises(ExecutionWriteUncertain):
            notion.sync_execution("records", run, lambda: None)

    def test_timing_stats_include_failed_and_auxiliary_runs_but_not_unfinished(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            try:
                self.assertIsNone(store.timing_stats("page"))
                base = {"page_id": "page", "phase": "complete", "started_at": "2026-11-19T01:00:00Z"}
                store.put({**base, "id": "success", "ended_at": "2026-11-19T01:00:10Z"})
                store.put({**base, "id": "failed", "primary": False, "exit_code": 1, "ended_at": "2026-11-19T01:00:25Z"})
                store.put({**base, "id": "unfinished"})
                store.put({**base, "id": "bad-clock", "ended_at": "2026-11-18T01:00:00Z"})
                self.assertEqual(store.timing_stats("page"), {"latest_seconds": 25.0, "total_seconds": 35.0, "execution_count": 2})
                self.assertIsNone(store.timing_stats("different-page"))
            finally:
                store.db.close()

    def test_timing_api_updates_only_numeric_fields_and_is_optional(self):
        notion = Notion("fake", "source")
        calls = []
        notion.request = lambda *args: calls.append(args)
        stats = {"latest_seconds": 2.5, "total_seconds": 10, "execution_count": 3}
        notion.update_timings("page", stats)
        self.assertFalse(calls)
        notion.timing_enabled = True
        notion.update_timings("page", stats)
        self.assertEqual(calls, [("PATCH", "pages/page", {"properties": {
            "最近耗时（秒）": {"number": 2.5}, "累计耗时（秒）": {"number": 10}, "执行次数": {"number": 3}}})])

    def test_mentions_require_explicit_alias_and_deduplicate(self):
        self.assertEqual(mentioned_agents("@Codex 修复；@CLAUDE CODE review，@claude 再看"), ["Codex", "Claude Code"])
        self.assertEqual(mentioned_agents("a@codex.example @codextra @claude-bot @@codex"), [])
        self.assertEqual(mentioned_agents("请@codex修复，@claude检查"), ["Codex", "Claude Code"])

    def test_resume_commands_use_exact_session_without_last_or_fork(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex = command("Codex", {"command": "/codex", "args": ["--sandbox", "workspace-write"]}, root, "codex-original")
            self.assertEqual(codex[:7], ["/codex", "exec", "--sandbox", "workspace-write", "resume", "codex-original", "--skip-git-repo-check"])
            claude = command("Claude Code", {"command": "/claude"}, root, "claude-original")
            self.assertEqual(claude[claude.index("--resume") + 1], "claude-original")
            for argv in (codex, claude):
                self.assertNotIn("--last", argv)
                self.assertNotIn("--fork-session", argv)

    def test_comment_list_paginates_and_bot_lookup_is_cached(self):
        notion = Notion("fake", "source")
        calls = []
        def request(method, path, body=None):
            calls.append(path)
            if path.startswith("users/"):
                return {"type": "bot"}
            second = "start_cursor=" in path
            return {"results": [{"id": "b" if second else "a", "created_time": "2026-01-01"}],
                    "has_more": not second, "next_cursor": "next cursor" if not second else None}
        notion.request = request
        self.assertEqual([c["id"] for c in notion.list_comments("page")], ["a", "b"])
        self.assertIn("start_cursor=next%20cursor", calls[-1])
        for _ in range(2):
            self.assertTrue(notion.comment_author_is_bot({"created_by": {"id": "robot"}}))
        self.assertEqual(calls.count("users/robot"), 1)

    def test_existing_wal_database_migrates_without_losing_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root)
            store.db.execute("PRAGMA journal_mode=WAL")
            run = {"id": "saved", "page_id": "page", "phase": "complete"}
            store.put(run)
            store.db.close()
            migrated = Store(root)
            try:
                self.assertEqual(migrated.db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
                self.assertEqual(migrated.get("saved"), run)
                self.assertEqual(migrated.db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            finally:
                migrated.db.close()

    def test_parent_property_discovery_checks_target_and_supports_renames(self):
        schema = page()["properties"]
        schema["状态"]["status"]["options"] = [{"name": name} for name in
                                            ("草稿", "待办", "执行中", "待我验收", "完成", "需处理")]
        schema["子项目"] = {"type": "relation", "relation": {"data_source_id": "tasks"}}
        schema["父项目"] = {"type": "relation", "relation": {"data_source_id": "other"}}
        notion = Notion("fake", "tasks")
        notion.source_schema = lambda: {"properties": schema}
        notion.check_schema()
        self.assertIsNone(notion.parent_property)
        schema["父项目"]["relation"]["data_source_id"] = "tasks"
        notion.check_schema()
        self.assertEqual(notion.parent_property, "父项目")
        schema["归属任务"] = schema.pop("父项目")
        notion.configured_parent_property = "归属任务"
        notion.check_schema()
        self.assertEqual(notion.parent_property, "归属任务")
        notion.configured_parent_property = "不存在"
        with self.assertRaisesRegex(ValueError, "parent_property"):
            notion.check_schema()

    def test_subitem_query_finds_blank_project_children_for_dispatch_and_comments(self):
        notion = Notion("fake", "tasks")
        notion.parent_property = "父项目"
        for status in ("待办", None):
            with patch.object(notion, "request", return_value={"results": [page()]}) as request:
                self.assertEqual(len(notion.query("mac", ["local"], status)), 1)
                filters = request.call_args.args[2]["filter"]["and"]
                self.assertEqual(filters[-1], {"and": [
                    {"property": "项目", "relation": {"is_empty": True}},
                    {"property": "父项目", "relation": {"is_not_empty": True}}]})
                self.assertEqual(len(filters), 2 if status else 1)

    def test_relation_query_filters_by_local_projects_and_deduplicates(self):
        notion = Notion("fake", "tasks")
        calls = []
        def request(method, path, body=None):
            calls.append(copy.deepcopy(body))
            return {"results": [page()], "has_more": False}
        notion.request = request
        rows = notion.query("machine", project_ids=[str(i) for i in range(100)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["filter"]["and"][1]["or"][0], {"property": "项目", "relation": {"contains": "0"}})
        self.assertNotIn("执行机器", json.dumps(calls, ensure_ascii=False))
        self.assertEqual(notion.query("machine", project_ids=[]), [])

    def test_protocol_metadata_removed_without_removing_content(self):
        self.assertEqual(readable_answer('完成。\n```json\n{"task_outcome":"ready_for_review","tests":"ok"}\n```'), "完成。")
        self.assertEqual(readable_answer('说明：\n{"user_data": 1}'), '说明：\n{"user_data": 1}')

    def test_comment_receipt_recovers_lost_response_without_visible_marker(self):
        notion = Notion("fake", "source")
        remote = []
        writes = []
        def request(method, path, body=None):
            if method == "GET" and path == "users/me":
                return {"id": "author"}
            if method == "GET":
                return {"results": remote, "has_more": False}
            writes.append(body)
            remote.append({"id": "comment", "discussion_id": "thread", "created_by": {"id": "author"}, "rich_text": body["rich_text"]})
            raise NotionError("response lost")
        notion.request = request
        operation = {}
        with self.assertRaises(NotionError):
            notion.post_comment("page", "已完成，请验收。", operation, lambda: None)
        self.assertEqual(notion.post_comment("page", "已完成，请验收。", operation, lambda: None), ("comment", "thread"))
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["rich_text"], rich("已完成，请验收。"))

    def test_personal_token_owner_reply_recovers_lost_response(self):
        notion = Notion("fake", "source")
        remote = []
        writes = []
        def request(method, path, body=None):
            if path == "users/me":
                return {"id": "api-bot", "bot": {"owner": {
                    "type": "user", "user": {"id": "token-owner"}}}}
            if method == "GET":
                return {"results": remote, "has_more": False}
            writes.append(body)
            remote.append({"id": "reply", "discussion_id": "thread",
                           "created_by": {"id": "token-owner"}, "rich_text": body["rich_text"],
                           "display_name": {"resolved_name": "Codex"}})
            raise NotionError("response lost")
        notion.request = request
        operation = {}
        with self.assertRaises(NotionError):
            notion.post_comment("page", "使用 @codex 继续", operation, lambda: None, display_name="Codex")
        self.assertEqual(notion.post_comment("page", "使用 @codex 继续", operation, lambda: None,
                                             display_name="Codex"), ("reply", "thread"))
        self.assertEqual(len(writes), 1)

    def test_uncertain_comment_missing_from_list_is_not_duplicated(self):
        notion = Notion("fake", "source")
        calls = []
        notion.request = lambda method, path, body=None: calls.append(method) or {"results": [], "has_more": False}
        operation = {"baseline_ids": [], "message": "done", "discussion_id": None, "actor_id": "author", "attempted": True}
        with self.assertRaises(CommentWriteUncertain):
            notion.post_comment("page", "done", operation, lambda: None)
        self.assertNotIn("POST", calls)

    def test_comment_reconciliation_distinguishes_display_names(self):
        notion = Notion("fake", "source")
        comments = [{"id": name, "discussion_id": "thread", "created_by": {"id": "author"},
                     "rich_text": rich("done"), "display_name": {"type": "custom", "resolved_name": name}}
                    for name in ("调度程序", "Codex")]
        notion.request = lambda method, path, body=None: {"results": comments, "has_more": False}
        operation = {"baseline_ids": [], "message": "done", "discussion_id": None,
                     "actor_id": "author", "attempted": True, "display_name": "Codex"}
        self.assertEqual(notion.post_comment("page", "done", operation, lambda: None, display_name="Codex"), ("Codex", "thread"))

    def test_custom_display_name_is_in_comment_request(self):
        notion = Notion("fake", "source")
        sent = []
        def request(method, path, body=None):
            if path == "users/me":
                return {"id": "author"}
            if method == "GET":
                return {"results": [], "has_more": False}
            sent.append(body)
            return {"id": "comment", "discussion_id": "thread"}
        notion.request = request
        notion.post_comment("page", "自然语言回复", {}, lambda: None, display_name="Claude Code")
        self.assertEqual(sent[0]["display_name"], {"type": "custom", "custom": {"name": "Claude Code"}})
        self.assertEqual(sent[0]["rich_text"], rich("自然语言回复"))

    def test_old_identical_comment_is_not_mistaken_for_new_delivery(self):
        notion = Notion("fake", "source")
        old = {"id": "old", "discussion_id": "old-thread", "created_by": {"id": "author"}, "rich_text": rich("done")}
        posted = []
        def request(method, path, body=None):
            if path == "users/me":
                return {"id": "author"}
            if method == "GET":
                return {"results": [old], "has_more": False}
            posted.append(body)
            return {"id": "new", "discussion_id": "new-thread"}
        notion.request = request
        self.assertEqual(notion.post_comment("page", "done", {}, lambda: None), ("new", "new-thread"))
        self.assertEqual(len(posted), 1)

    def test_project_instructions_use_executor_instruction_channels(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            instructions = '需要 review。\n引号 " 和 $(not-a-command) 保持原样。'
            (directory / "project-instructions.txt").write_text(instructions)
            settings = {"command": "/bin/example", "args": ["--example"]}
            codex = command("Codex", settings, directory)
            argument = codex[codex.index("-c") + 1]
            self.assertEqual(json.loads(argument.split("=", 1)[1]), instructions)
            self.assertTrue(argument.startswith("developer_instructions="))
            claude = command("Claude Code", settings, directory)
            self.assertEqual(claude[claude.index("--append-system-prompt") + 1], instructions)
            self.assertEqual(settings["args"], ["--example"])

    def test_dotenv_relative_to_config_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            save(initial(), path)
            (path.parent / ".env").write_text('# comment\nexport NOTION_TOKEN="file-token" # inline comment\n')
            with patch.dict(os.environ, {}, clear=True):
                config = load(path)
                self.assertEqual(config.token(), "file-token")
                self.assertNotIn("NOTION_TOKEN", os.environ)
                self.assertNotIn("NOTION_TOKEN", child_environment(config))
            with patch.dict(os.environ, {"NOTION_TOKEN": "environment-token"}):
                self.assertEqual(load(path).token(), "environment-token")

    def test_empty_dotenv_reports_missing_credential(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            save(initial(), path)
            (path.parent / ".env").write_text("NOTION_TOKEN=\n")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, r"在 .env 中填写 NOTION_TOKEN"):
                    load(path).token()

    def test_dotenv_error_does_not_include_secret(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            save(initial(), path)
            (path.parent / ".env").write_text('NOTION_TOKEN="private-value\n')
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ValueError) as error:
                    load(path).token()
                self.assertNotIn("private-value", str(error.exception))

    def test_config_dynamic_reload_and_unicode_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            data = initial()
            data["projects"] = {"中文项目": {"path": td, "base": "main"}}
            save(data, path)
            self.assertIn("中文项目", load(path).projects)
            data["projects"]["new"] = {"path": td}
            save(data, path)
            self.assertIn("new", load(path).projects)

    def test_instance_lock(self):
        with tempfile.TemporaryDirectory() as td:
            with instance_lock(Path(td)):
                with self.assertRaises(RuntimeError):
                    with instance_lock(Path(td)):
                        pass

    def test_claude_permission_denials(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "events.jsonl"
            path.write_text(json.dumps({"type": "result", "subtype": "success", "session_id": "s", "result": "done", "permission_denials": [{"tool": "Bash"}]}) + "\n")
            result = parse_events(path, "Claude Code")
            self.assertFalse(result["protocol_success"])
            self.assertEqual(result["session_id"], "s")

    def test_reconcile_real_adapter_marker_after_ambiguous_write(self):
        notion = Notion("fake", "source")
        remote = []
        notion.children = lambda _: iter(remote)
        def append(method, path, body):
            remote.append({"id": "block", **body["children"][0]})
            raise NotionError("response lost")
        notion.request = append
        with self.assertRaises(NotionError):
            notion.result_chunk("page", "Agent result · run · 1/1", "hello")
        self.assertEqual(notion.result_chunk("page", "Agent result · run · 1/1", "hello"), "block")
        self.assertEqual(len(remote), 1)

    def test_both_task_types_use_existing_project_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            (project / "existing.txt").write_text("uncommitted user content")
            run = root / "logs"
            run.mkdir()
            for kind in ("代码", "调研"):
                data = prepare({"path": str(project)}, kind, run, "test")
                self.assertEqual(data, {"workspace": str(project.resolve())})
                self.assertEqual((project / "existing.txt").read_text(), "uncommitted user content")
            self.assertFalse((run / "workspace").exists())
            self.assertFalse((project / ".git").exists())

    def test_missing_project_directory_not_created(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "missing"
            with self.assertRaises(ValueError):
                prepare({"path": str(missing)}, "代码", Path(td), "test")
            self.assertFalse(missing.exists())

    def test_notion_projects_pagination_duplicate_and_machine_filter(self):
        notion = Notion("fake", "tasks")
        schema = {"properties": {"项目": {"type": "title"}, "执行机器": {"type": "rich_text"}, "本机目录": {"type": "rich_text"}}}
        def row(name, machine, directory, id):
            return {"id": id, "properties": {"项目": {"type": "title", "title": rich(name)},
                    "执行机器": {"type": "rich_text", "rich_text": rich(machine)},
                    "本机目录": {"type": "rich_text", "rich_text": rich(directory)}}}
        seen = []
        def request(method, path, body=None):
            seen.append((method, path, copy.deepcopy(body)))
            if method == "GET":
                return schema
            if body.get("start_cursor"):
                return {"results": [row("app", "mac", "/second", "2"), row("bad", "mac", "relative", "3")], "has_more": False}
            return {"results": [row("app", "mac", "/first", "1"), row("other", "linux", "/other", "4")], "has_more": True, "next_cursor": "next"}
        notion.request = request
        projects = notion.projects("projects-source", "mac")
        self.assertIn("多条配置", projects["app"]["error"])
        self.assertIn("绝对目录", projects["bad"]["error"])
        self.assertNotIn("other", projects)
        self.assertEqual(seen[-1][2]["start_cursor"], "next")

    def test_env_does_not_forward_board_token_or_session(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            save(initial(), path)
            with patch.dict(os.environ, {"NOTION_TOKEN": "secret", "CODEX_THREAD_ID": "parent", "CLAUDECODE": "1"}):
                env = child_environment(load(path))
            self.assertNotIn("NOTION_TOKEN", env)
            self.assertNotIn("CODEX_THREAD_ID", env)
            self.assertNotIn("CLAUDECODE", env)


if __name__ == "__main__":
    unittest.main()
