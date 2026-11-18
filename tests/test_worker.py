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
from notion_agent.notion import Notion, NotionError, rich, value
from notion_agent.runners import command, execute, parse_events, child_environment
from notion_agent.store import Store, instance_lock
from notion_agent.worker import Worker, outcome
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
        self.fail_delivery_once = False
        self.body_text = "请输出测试报告，验收：给出结论。"
        self.writes = []

    def query(self, machine):
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

    async def test_success_and_no_duplicate_scan(self):
        worker = self.worker()
        await worker.scan()
        await worker.scan()
        self.assertEqual(self.calls, 1)
        self.assertEqual(value(self.notion.p, "状态"), "待我验收")
        self.assertEqual(len(self.notion.blocks), 1)
        self.assertEqual(self.store.all()[0]["phase"], "complete")
        self.assertEqual(self.notion.body_text, "请输出测试报告，验收：给出结论。")

    async def test_lost_delivery_response_reconciles_without_rerunning(self):
        self.notion.fail_delivery_once = True
        worker = self.worker()
        with self.assertRaises(NotionError):
            await worker.scan()
        self.assertEqual(self.calls, 1)
        self.assertEqual(value(self.notion.p, "状态"), "执行中")
        await worker.scan()
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(self.notion.blocks), 1)
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
        self.notion.p["properties"]["执行机器"]["select"]["name"] = "test-machine"
        self.notion.p["properties"]["项目"]["select"]["name"] = "missing"
        await self.worker().scan()
        self.assertEqual(self.calls, 0)
        self.assertEqual(value(self.notion.p, "状态"), "待办")

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
        self.assertEqual(len(self.notion.blocks), 1)
        self.assertEqual(self.store.all()[0]["phase"], "complete")


class UnitTests(unittest.TestCase):
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

    def test_code_workspace_isolated_clean_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            def git(*args):
                return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
            git("init", "-b", "main")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "base")
            run = root / "run"
            run.mkdir()
            data = prepare({"path": str(repo), "base": "main"}, "代码", run, "test")
            (Path(data["workspace"]) / "new.txt").write_text("isolated")
            self.assertFalse((repo / "new.txt").exists())
            self.assertEqual(data["branch"], "notion/test")
            (repo / "dirty.txt").write_text("dirty")
            with self.assertRaises(ValueError):
                prepare({"path": str(repo), "base": "main"}, "代码", root / "other", "other")

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
