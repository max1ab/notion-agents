from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
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
        self.db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('comments_since', ?)",
                        (datetime.now(timezone.utc).isoformat(),))
        self.db.execute("CREATE TABLE IF NOT EXISTS comment_triggers (page_id TEXT, comment_id TEXT, agent TEXT, run_id TEXT, PRIMARY KEY(page_id, comment_id, agent))")
        self.db.commit()

    @property
    def comments_since(self):
        return self.db.execute("SELECT value FROM metadata WHERE key='comments_since'").fetchone()[0]

    def metadata(self, key):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_metadata(self, key, data):
        with self.db:
            self.db.execute("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, json.dumps(data, ensure_ascii=False)))

    def triggered(self, page_id, comment_id, agent):
        return self.db.execute("SELECT 1 FROM comment_triggers WHERE page_id=? AND comment_id=? AND agent=?",
                               (page_id, comment_id, agent)).fetchone() is not None

    def history(self, page_id):
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM runs WHERE page_id=? ORDER BY rowid", (page_id,))]

    def latest_primary(self, page_id):
        return next((r for r in reversed(self.history(page_id)) if r.get("primary", True)), None)

    def timing_stats(self, page_id):
        measurements = []
        for run in self.history(page_id):
            try:
                start = datetime.fromisoformat(run["started_at"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(run["ended_at"].replace("Z", "+00:00"))
                if start.tzinfo is None or end.tzinfo is None:
                    continue
                seconds = (end - start).total_seconds()
            except (KeyError, TypeError, ValueError):
                continue
            if seconds >= 0:
                measurements.append((end, seconds))
        if not measurements:
            return None
        return {"latest_seconds": round(max(measurements, key=lambda m: m[0])[1], 3),
                "total_seconds": round(math.fsum(m[1] for m in measurements), 3),
                "execution_count": len(measurements)}

    def sent_comment_ids(self, page_id):
        operations = [op for run in self.history(page_id)
                      for op in run.get("comment_operations", {}).values()]
        operations.extend(json.loads(row[0]) for row in self.db.execute(
            "SELECT value FROM metadata WHERE key LIKE ?", ("comment-problem:" + page_id + ":%",)))
        return {op["comment_id"] for op in operations if op.get("comment_id")}

    def put(self, run):
        with self.db:
            if run.get("trigger"):
                self.db.execute("INSERT OR IGNORE INTO comment_triggers VALUES (?,?,?,?)",
                                (run["page_id"], run["trigger"]["id"], run["agent"], run["id"]))
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
