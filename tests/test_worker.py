import asyncio
import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from notion_agent.config import initial, load, save
from notion_agent.notion import Notion, NotionError, CommentWriteUncertain, rich, value
from notion_agent.runners import command, execute, parse_events, child_environment
from notion_agent.store import Store, instance_lock
from notion_agent.worker import Worker, outcome, readable_answer
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
        self.blocks = {}
        self.comments = {}
        self.comment_names = {}
        self.fail_delivery_once = False
        self.body_text = "请输出测试报告，验收：给出结论。"
        self.writes = []

    def query(self, machine, project_ids=None):
        if project_ids is not None:
            ids = [x["id"] for x in self.p.get("properties", {}).get("项目", {}).get("relation", [])]
            if not set(ids).intersection(project_ids):
                return []
        return [copy.deepcopy(self.p)] if value(self.p, "状态") == "待办" else []

    def page(self, _):
        return copy.deepcopy(self.p)

    def body(self, _):
        return self.body_text

    def update(self, _, status=None, run_id=None, summary=None):
        self.writes.append((status, run_id, summary))
        for key, val in (("状态", status), ("最近运行 ID", run_id), ("运行摘要", summary)):
            if val is not None:
                t = "status" if key == "状态" else "rich_text"
                self.p["properties"][key] = {"type": t, t: {"name": val} if t == "status" else rich(val)}

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
        with self.assertRaises(NotionError):
            await worker.scan()
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
        self.store.put({"id": "uncertain", "page_id": "page-1", "phase": "spawning"})
        await self.worker().scan()
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.store.get("uncertain")["phase"], "hold")

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
        self.store.put({"id": "live", "page_id": "page-1", "phase": "running", "pid": os.getpid()})
        await worker.scan()
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.store.get("live")["phase"], "running")

    async def test_completed_process_crash_recovery_delivers_attention(self):
        self.notion.fail_delivery_once = True
        worker = self.worker()
        with self.assertRaises(NotionError):
            await worker.scan()
        run = self.store.all()[0]
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
        with self.assertRaises(NotionError):
            await worker.scan()
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
