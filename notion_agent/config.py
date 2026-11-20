from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path


AGENT_NAMES = ("Codex", "Claude Code")


def read_env(path: Path) -> dict[str, str]:
    """Read single-line dotenv assignments without executing or exporting them."""
    if not path.exists():
        return {}
    values = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw = line.partition("=")
        key = key.strip()
        if not separator or not key.isidentifier():
            raise ValueError(f"{path.name} 第 {number} 行格式错误，应为 KEY=value")
        try:
            parts = shlex.split(raw, comments=True)
        except ValueError:
            raise ValueError(f"{path.name} 第 {number} 行引号未闭合") from None
        if len(parts) > 1:
            raise ValueError(f"{path.name} 第 {number} 行含空格的值需要引号")
        values[key] = parts[0] if parts else ""
    return values


@dataclass
class Config:
    path: Path
    machine_id: str
    data_source_id: str
    state_dir: Path
    poll_seconds: float
    timeout_seconds: float
    token_env: str
    token_file: Path | None
    agents: dict
    projects: dict
    comments: bool = True
    env_file: Path | None = None
    projects_data_source_id: str | None = None
    agent_guide_page_id: str | None = None
    execution_data_source_id: str | None = None
    max_parallel_projects: int = 2
    parent_property: str | None = None

    def token(self):
        value = os.environ.get(self.token_env, "")
        if not value and self.env_file:
            value = read_env(self.env_file).get(self.token_env, "")
        if not value and self.token_file:
            if not self.token_file.is_file():
                raise ValueError(f"尚未配置 Notion 凭证文件：{self.token_file}")
            mode = self.token_file.stat().st_mode
            if stat.S_IMODE(mode) & 0o077:
                raise ValueError("凭证文件权限必须为 0600")
            value = self.token_file.read_text().strip()
        if not value:
            raise ValueError(f"缺少 Notion 凭证：在 .env 中填写 {self.token_env} 或设置同名环境变量")
        return value


def load(path: Path) -> Config:
    path = path.expanduser().resolve()
    data = tomllib.loads(path.read_text())
    def local(value):
        p = Path(value).expanduser()
        return (path.parent / p).resolve() if not p.is_absolute() else p.resolve()
    machine = data.get("machine_id") or socket.gethostname()
    if not machine.strip():
        raise ValueError("machine_id 不能为空")
    agents = data.get("agents", {})
    for name, agent in agents.items():
        if name not in AGENT_NAMES:
            raise ValueError(f"未知 Agent：{name}")
        binary = agent.get("command", "")
        if not Path(binary).is_absolute():
            raise ValueError(f"{name} command 必须是绝对路径")
    projects = data.get("projects", {})
    for project in projects.values():
        project["path"] = str(local(project["path"]))
    poll = float(data.get("poll_seconds", 30))
    timeout = float(data.get("timeout_seconds", 3600))
    if poll < 1 or timeout < 1:
        raise ValueError("poll_seconds 和 timeout_seconds 必须至少为 1")
    parallel = data.get("max_parallel_projects", 2)
    if type(parallel) is not int or parallel < 1:
        raise ValueError("max_parallel_projects 必须为正整数")
    return Config(path, machine, data["data_source_id"], local(data.get("state_dir", ".state")),
                  poll, timeout, data.get("token_env", "NOTION_TOKEN"),
                  local(data["token_file"]) if data.get("token_file") else None,
                  agents, projects, data.get("comments", True),
                  env_file=local(data.get("env_file", ".env")),
                  projects_data_source_id=data.get("projects_data_source_id") or None,
                  agent_guide_page_id=data.get("agent_guide_page_id") or None,
                  execution_data_source_id=data.get("execution_data_source_id") or None,
                  max_parallel_projects=parallel, parent_property=data.get("parent_property") or None)


def save(data: dict, path: Path):
    """Small TOML writer for our config's strings, booleans, numbers and arrays."""
    lines = []
    def emit(table, parts):
        if parts:
            lines.append("[" + ".".join(json.dumps(p, ensure_ascii=False) for p in parts) + "]")
        for key, value in table.items():
            if not isinstance(value, dict):
                lines.append(f"{json.dumps(key, ensure_ascii=False)} = {json.dumps(value, ensure_ascii=False)}")
        for key, value in table.items():
            if isinstance(value, dict):
                lines.append("")
                emit(value, parts + [key])
    emit(data, [])
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(lines) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def initial():
    return {"machine_id": socket.gethostname(),
            "data_source_id": "00000000-0000-0000-0000-000000000000",
            "projects_data_source_id": "",
            "state_dir": ".state", "poll_seconds": 30, "timeout_seconds": 3600,
            "max_parallel_projects": 2,
            "token_env": "NOTION_TOKEN", "env_file": ".env", "comments": True,
            "agents": {"Codex": {"command": shutil.which("codex") or "/usr/local/bin/codex",
                                   "args": ["--sandbox", "workspace-write"]},
                       "Claude Code": {"command": shutil.which("claude") or "/usr/local/bin/claude",
                                       "args": ["--permission-mode", "dontAsk"]}},
            "projects": {}}
