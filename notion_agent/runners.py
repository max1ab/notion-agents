from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
from pathlib import Path


def process_stamp(pid):
    try:
        result = subprocess.run(["ps", "-p", str(int(pid)), "-o", "lstart="], capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ""


def process_alive(run):
    pid = run.get("pid")
    if not pid:
        return False
    current = process_stamp(pid)
    if current:
        return not run.get("process_stamp") or current == run["process_stamp"]
    # Inaccessible process identity is uncertainty, not proof of death.
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def command(agent_name, settings, run_dir):
    args = settings.get("args", [])
    if agent_name == "Codex":
        return [settings["command"], "exec", *args, "--skip-git-repo-check", "--json", "-o", str(run_dir / "last-message.txt"), "-"]
    return [settings["command"], "--print", *args, "--output-format", "stream-json", "--verbose"]


def child_environment(config):
    env = os.environ.copy()
    # New task sessions must not inherit the caller's conversation identity or board secret.
    for key in (config.token_env, "CODEX_THREAD_ID", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID"):
        env.pop(key, None)
    return env


def parse_events(path, agent):
    session = None
    final = ""
    success = False
    problems = []
    denied = False
    with path.open() as stream:
        lines = stream.readlines()
    for line in lines:
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if agent == "Codex":
            if kind == "thread.started":
                session = event.get("thread_id")
            if kind == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    final = item.get("text", "")
            if kind == "turn.completed":
                success = True
            if kind in ("error", "turn.failed"):
                problems.append(str(event.get("error", event.get("message", kind))))
        else:
            session = event.get("session_id", session)
            if kind == "result":
                success = event.get("subtype") == "success" and not event.get("is_error", False)
                final = event.get("result", "")
                denied = bool(event.get("permission_denials"))
                if not success:
                    problems.append(str(event.get("errors", event.get("subtype"))))
    return {"session_id": session, "answer": final, "protocol_success": success and not problems and not denied,
            "protocol_error": "; ".join(problems), "permission_denied": denied}


async def execute(config, run, store):
    run_dir = Path(run["run_dir"])
    prompt = (run_dir / "prompt.txt").read_text()
    argv = command(run["agent"], run["agent_settings"], run_dir)
    with (run_dir / "events.jsonl").open("w") as stdout, (run_dir / "stderr.log").open("w") as stderr:
        # Persist the uncertainty window before process creation. Recovery never starts it twice.
        run["phase"] = "spawning"
        store.put(run)
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            *argv, cwd=run["workspace"], env=child_environment(config),
            stdin=asyncio.subprocess.PIPE, stdout=stdout, stderr=stderr, start_new_session=True))
        cancelled_during_spawn = False
        try:
            proc = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            # Complete the spawn handshake so shutdown can stop the actual process group.
            proc = await spawning
            cancelled_during_spawn = True
        run.update(phase="running", pid=proc.pid, process_stamp=process_stamp(proc.pid), command=argv)
        store.put(run)
        async def stop():
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except asyncio.TimeoutError:
                pass
            # The CLI can exit while a tool subprocess ignores SIGTERM.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
        try:
            if cancelled_during_spawn:
                raise asyncio.CancelledError()
            await asyncio.wait_for(proc.communicate(prompt.encode()), config.timeout_seconds)
        except asyncio.TimeoutError:
            await stop()
            run["error"] = "执行超时，进程组已停止"
        except asyncio.CancelledError:
            await stop()
            run["error"] = "服务停止，进程组已停止；请检查保留的工作区"
            raise
        finally:
            run["exit_code"] = proc.returncode
            run.update(parse_events(run_dir / "events.jsonl", run["agent"]))
            last = run_dir / "last-message.txt"
            if run["agent"] == "Codex" and last.exists():
                run["answer"] = last.read_text()
            store.put(run)
    return run
