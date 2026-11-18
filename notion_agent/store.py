from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def machine_lock(config):
    identity = hashlib.sha256((config.data_source_id + ":" + config.machine_id).encode()).hexdigest()
    lock_dir = Path("/tmp") / f"notion-agent-{os.getuid()}" / identity
    with instance_lock(lock_dir), instance_lock(config.state_dir):
        yield


@contextmanager
def instance_lock(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state_dir / "worker.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("此 state_dir 已有 worker 运行") from None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(directory / "runs.sqlite3")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, page_id TEXT, phase TEXT, data TEXT)")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS active_page ON runs(page_id) WHERE phase != 'complete'")
        self.db.commit()

    def put(self, run):
        with self.db:
            self.db.execute("INSERT INTO runs VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET phase=excluded.phase, data=excluded.data",
                            (run["id"], run["page_id"], run["phase"], json.dumps(run, ensure_ascii=False)))

    def get(self, run_id):
        row = self.db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def latest(self, page_id):
        row = self.db.execute("SELECT data FROM runs WHERE page_id=? ORDER BY rowid DESC LIMIT 1", (page_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, active=False):
        sql = "SELECT data FROM runs" + (" WHERE phase != 'complete'" if active else "") + " ORDER BY rowid"
        return [json.loads(row[0]) for row in self.db.execute(sql)]
