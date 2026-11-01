import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.schemas import TripResult


class VersionConflict(Exception):
    pass


class Repository:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS trips (id TEXT NOT NULL, version INTEGER NOT NULL, "
                       "payload TEXT NOT NULL, PRIMARY KEY (id, version))")
            db.execute("CREATE TABLE IF NOT EXISTS trip_history (id TEXT PRIMARY KEY, version INTEGER NOT NULL, "
                       "title TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL, updated_seq INTEGER NOT NULL)")
            if db.execute("PRAGMA user_version").fetchone()[0] < 2:
                rows = db.execute("SELECT t.payload,t.rowid FROM trips t JOIN "
                    "(SELECT id,MAX(version) v FROM trips GROUP BY id) latest ON t.id=latest.id AND t.version=latest.v")
                for payload, sequence in rows.fetchall():
                    self.index(db, json.loads(payload), sequence)
                db.execute("PRAGMA user_version = 2")
            db.execute("CREATE TABLE IF NOT EXISTS assistant_messages (id INTEGER PRIMARY KEY, trip_id TEXT NOT NULL, "
                       "role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL, version INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS assistant_turns (trip_id TEXT NOT NULL, request_id TEXT NOT NULL, "
                       "message TEXT NOT NULL, base_version INTEGER NOT NULL, response TEXT, PRIMARY KEY(trip_id,request_id))")
            db.execute("PRAGMA user_version = 3")

    @staticmethod
    def index(db, result, sequence):
        c = result["constraints"]
        title = " → ".join(c["cities"]) or "待补充旅行"
        title += f" · {c.get('start_date') or '日期待定'} · {c.get('days') or '?'}天"
        db.execute("INSERT OR REPLACE INTO trip_history VALUES (?,?,?,?,?,?)", (result["id"], result["version"],
                   title, result["created_at"], result["status"], sequence))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            # sqlite3.Connection.__exit__ commits/rolls back but DOES NOT close.
            db.close()

    def get(self, trip_id, version=None):
        with self.connect() as db:
            if version is None:
                row = db.execute("SELECT payload FROM trips WHERE id=? ORDER BY version DESC LIMIT 1", (trip_id,)).fetchone()
            else:
                row = db.execute("SELECT payload FROM trips WHERE id=? AND version=?", (trip_id, version)).fetchone()
        return TripResult.model_validate_json(row[0]) if row else None

    def save(self, result, expected_version=0):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT COALESCE(MAX(version),0) FROM trips WHERE id=?", (result.id,)).fetchone()[0]
            if current != expected_version or result.version != current + 1:
                raise VersionConflict("行程已被更新，请加载最新版本后重试")
            cursor = db.execute("INSERT INTO trips (id,version,payload) VALUES (?,?,?)",
                                (result.id, result.version, result.model_dump_json()))
            self.index(db, result.model_dump(mode="json"), cursor.lastrowid)

    def list(self, query="", limit=50, offset=0):
        with self.connect() as db:
            rows = db.execute("SELECT id,version,title,created_at,status FROM trip_history "
                "WHERE instr(lower(title),lower(?))>0 ORDER BY updated_seq DESC LIMIT ? OFFSET ?",
                (query, limit, offset)).fetchall()
        return [dict(zip(("id", "version", "title", "created_at", "status"), row, strict=True)) for row in rows]

    def count(self, query=""):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM trip_history WHERE instr(lower(title),lower(?))>0", (query,)).fetchone()[0]

    def versions(self, trip_id):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM trips WHERE id=? ORDER BY version DESC", (trip_id,)).fetchall()
        results = [TripResult.model_validate_json(row[0]) for row in rows]
        return [{"version": r.version, "created_at": r.created_at, "status": r.status,
                 "budget": r.budget.total.model_dump() if r.budget else None} for r in results]

    def messages(self, trip_id, limit=100):
        with self.connect() as db:
            rows = db.execute("SELECT role,content,created_at,version FROM assistant_messages WHERE trip_id=? "
                              "ORDER BY id DESC LIMIT ?", (trip_id, limit)).fetchall()
        return [dict(zip(("role", "content", "created_at", "version"), row, strict=True)) for row in reversed(rows)]

    def begin_chat(self, trip_id, request_id, message, version):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT message,base_version,response FROM assistant_turns WHERE trip_id=? AND request_id=?",
                             (trip_id, request_id)).fetchone()
            if old:
                if old[:2] != (message, version):
                    raise VersionConflict("这次发送标记已用于其他内容，请重新发送。")
                if old[2]:
                    return json.loads(old[2])
                raise VersionConflict("这条消息正在处理中，请稍后查看回复。")
            db.execute("INSERT INTO assistant_turns VALUES(?,?,?,?,NULL)", (trip_id, request_id, message, version))
            db.execute("INSERT INTO assistant_messages(trip_id,role,content,created_at,version) VALUES(?,?,?,?,?)",
                       (trip_id, "user", message, datetime.now(UTC).isoformat(), version))
        return None

    def finish_chat(self, trip_id, request_id, response):
        with self.connect() as db:
            db.execute("INSERT INTO assistant_messages(trip_id,role,content,created_at,version) VALUES(?,?,?,?,?)",
                       (trip_id, "assistant", response["reply"], datetime.now(UTC).isoformat(), response["trip"]["version"]))
            db.execute("UPDATE assistant_turns SET response=? WHERE trip_id=? AND request_id=?",
                       (json.dumps(response, ensure_ascii=False), trip_id, request_id))
