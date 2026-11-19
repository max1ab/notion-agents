from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import plistlib
import signal
import sys
import tomllib
from pathlib import Path

from .config import initial, load, save
from .notion import Notion
from .runners import process_alive
from .store import Store, machine_lock
from .worker import Worker


def parser():
    p = argparse.ArgumentParser(description="Notion 本机 Agent 接单服务")
    p.add_argument("--config", type=Path, default=Path("config.toml"))
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("init", help="生成本机配置，不启动服务")
    for name in ("doctor", "register", "dry-run", "once", "run", "status"):
        sub.add_parser(name)
    machine = sub.add_parser("machine", help="设置机器名，服务需重启")
    machine.add_argument("name")
    project = sub.add_parser("project", help="新增/修改项目，下一轮自动生效")
    project.add_argument("key")
    project.add_argument("path", type=Path)
    logs = sub.add_parser("logs")
    logs.add_argument("run_id")
    resolve = sub.add_parser("resolve", help="人工确认旧运行结束后解除本地阻塞，不重新执行")
    resolve.add_argument("run_id")
    resolve.add_argument("--confirm-stopped", action="store_true", required=True)
    service = sub.add_parser("service-files", help="生成 launchd/systemd 文件供安装")
    service.add_argument("--output", type=Path, default=Path("deploy/generated"))
    return p


def service_files(config, output):
    output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parent.parent
    argv = [sys.executable, "-m", "notion_agent", "--config", str(config.path), "run"]
    config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = {"Label": "top.mclab.notion-agent", "ProgramArguments": argv, "WorkingDirectory": str(root),
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
            "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            "StandardOutPath": str(config.state_dir / "service.log"),
            "StandardErrorPath": str(config.state_dir / "service.log")}
    (output / "top.mclab.notion-agent.plist").write_bytes(plistlib.dumps(data))
    # systemd quoting escapes %, slashes and double quotes independently of a shell.
    def quote(s):
        return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
    unit = ("[Unit]\nDescription=Notion local agent worker\nAfter=network-online.target\n\n[Service]\nType=simple\n"
            + "WorkingDirectory=" + str(root).replace("%", "%%") + "\nExecStart=" + " ".join(map(quote, argv))
            + "\nRestart=on-failure\nRestartSec=30\nTimeoutStopSec=30\nKillMode=control-group\nUMask=0077\n\n[Install]\nWantedBy=default.target\n")
    (output / "notion-agent.service").write_text(unit)
    print(f"服务文件已生成：{output.resolve()}")


def main():
    args = parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.umask(0o077)
    try:
        return run(args)
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("服务已停止；运行中的任务保留在本地以便对账。", file=sys.stderr)


def run(args):
    path = args.config.expanduser().resolve()
    if args.action == "init":
        if path.exists():
            raise ValueError("配置已存在，不覆盖")
        save(initial(), path)
        print(f"配置已生成：{path}；请配置凭证并添加项目")
        return
    if args.action in ("machine", "project"):
        data = tomllib.loads(path.read_text())
        if args.action == "machine":
            if not args.name.strip():
                raise ValueError("机器名不能为空")
            data["machine_id"] = args.name
        else:
            if data.get("projects_data_source_id"):
                raise ValueError("已启用 Notion 项目表，请直接在 Notion 添加项目、执行机器和本机目录")
            project_path = args.path.expanduser().resolve()
            if not project_path.is_dir():
                raise ValueError("项目目录不存在")
            project = {"path": str(project_path)}
            data.setdefault("projects", {})[args.key] = project
        save(data, path)
        print("配置已更新；项目下轮生效，机器名修改后需重启。使用 register 同步下拉选项。")
        return
    config = load(path)
    if args.action == "service-files":
        service_files(config, args.output)
        return
    store = Store(config.state_dir)
    if args.action == "status":
        for r in store.all():
            print(json.dumps({key: r.get(key) for key in ("id", "page_id", "phase", "agent", "workspace", "session_id", "error")}, ensure_ascii=False))
        return
    if args.action == "logs":
        r = store.get(args.run_id)
        if not r:
            raise ValueError("运行不存在")
        print(json.dumps(r, ensure_ascii=False, indent=2))
        print(f"原始日志：{r['run_dir']}/events.jsonl 和 stderr.log")
        return
    if args.action == "resolve":
        with machine_lock(config):
            r = store.get(args.run_id)
            if not r:
                raise ValueError("运行不存在")
            if process_alive(r):
                raise ValueError("原进程尚在，不能解除运行")
            r.update(phase="complete", resolution="用户已确认原进程结束；人工处理 Notion 状态和本地结果")
            store.put(r)
            print("本地阻塞已解除。请先处理 Notion 原卡状态；只有重置为待办才会重新派单。")
        return
    if args.action == "doctor":
        failed = False
        print(f"机器：{config.machine_id}\n状态目录：{config.state_dir}")
        if not config.projects_data_source_id:
            print("本地项目：" + (", ".join(config.projects) or "尚无"))
        for name, settings in config.agents.items():
            available = os.access(settings["command"], os.X_OK)
            print(f"{name}：{'CLI 可用' if available else 'CLI 不存在'} ({settings['command']})")
            failed |= not available
        try:
            notion = Notion(config.token(), config.data_source_id)
            notion.check_schema()
            if config.execution_data_source_id:
                notion.check_execution_schema(config.execution_data_source_id)
                print("Notion 执行记录 schema：通过")
            if config.projects_data_source_id:
                config.projects = notion.projects(config.projects_data_source_id, config.machine_id,
                                                 by_id=notion.project_property_type == "relation")
                print("Notion 项目配置：" + (", ".join(p.get("name", k) for k, p in config.projects.items()) or "本机尚无项目"))
            print("Notion 凭证与看板 schema：通过")
        except (ValueError, OSError, RuntimeError) as error:
            print(f"Notion：{error}")
            failed = True
        print("CLI 登录、MCP、技能与 OpenViking：需真实任务验收，doctor 不据 CLI 路径推断")
        if failed:
            raise SystemExit(1)
        return
    notion = Notion(config.token(), config.data_source_id)
    if args.action == "register":
        notion.check_schema()
        if notion.project_property_type == "relation":
            print("项目使用 Notion 原生关联，无需同步下拉选项；机器和目录从项目记录读取。")
            return
        if config.projects_data_source_id:
            config.projects = notion.projects(config.projects_data_source_id, config.machine_id)
        notion.register(config.machine_id, list(config.projects))
        print("机器名与项目下拉选项已同步；旧选项已保留。")
        return
    with machine_lock(config):
        notion.check_schema()
        if config.execution_data_source_id:
            notion.check_execution_schema(config.execution_data_source_id)
        worker = Worker(config, notion, store)
        async def work():
            loop = asyncio.get_running_loop()
            task = asyncio.current_task()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, task.cancel)
            try:
                if args.action == "run":
                    await worker.serve()
                else:
                    print(json.dumps(await worker.scan(dry_run=args.action == "dry-run"), ensure_ascii=False, indent=2))
            except asyncio.CancelledError:
                pass
        asyncio.run(work())
