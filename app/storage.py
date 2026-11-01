import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.schemas import TripResult


class VersionConflict(Exception):
    pass


class Repository:
    def __init__(self, path, clock=time.time):
        self.path = str(path)
        self.clock = clock
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
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
            columns = {r[1] for r in db.execute("PRAGMA table_info(assistant_turns)")}
            for name, declaration in {"state": "TEXT NOT NULL DEFAULT 'interrupted'", "owner": "TEXT",
                                      "lease_until": "REAL NOT NULL DEFAULT 0", "result_version": "INTEGER"}.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE assistant_turns ADD COLUMN {name} {declaration}")
            db.execute("UPDATE assistant_turns SET state='completed' WHERE response IS NOT NULL AND state!='completed'")
            db.execute("PRAGMA user_version = 4")

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

    def save(self, result, expected_version=0, chat_claim=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if chat_claim:
                self.check_chat_owner(db, *chat_claim)
                if chat_claim[0] != result.id or db.execute(
                        "SELECT result_version FROM assistant_turns WHERE trip_id=? AND request_id=?", chat_claim[:2]).fetchone()[0] is not None:
                    raise VersionConflict("这条消息已有处理结果，请重新读取。")
            current = db.execute("SELECT COALESCE(MAX(version),0) FROM trips WHERE id=?", (result.id,)).fetchone()[0]
            if current != expected_version or result.version != current + 1:
                raise VersionConflict("行程已被更新，请加载最新版本后重试")
            cursor = db.execute("INSERT INTO trips (id,version,payload) VALUES (?,?,?)",
                                (result.id, result.version, result.model_dump_json()))
            self.index(db, result.model_dump(mode="json"), cursor.lastrowid)
            if chat_claim:
                db.execute("UPDATE assistant_turns SET result_version=? WHERE trip_id=? AND request_id=?",
                           (result.version, *chat_claim[:2]))

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

    def pending_chat(self, trip_id):
        with self.connect() as db:
            row = db.execute("SELECT request_id,message,base_version,state,lease_until FROM assistant_turns "
                             "WHERE trip_id=? AND response IS NULL ORDER BY rowid DESC LIMIT 1", (trip_id,)).fetchone()
        if row is None:
            return None
        return {"request_id": row[0], "message": row[1], "base_version": row[2],
                "state": "running" if row[3] == "running" and row[4] > self.clock() else "interrupted"}

    def begin_chat(self, trip_id, request_id, message, version, lease_seconds=330):
        if not 0 < lease_seconds <= 660:
            raise ValueError("Invalid chat lease")
        owner, now = uuid4().hex, self.clock()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT message,base_version,response,state,lease_until,result_version FROM assistant_turns WHERE trip_id=? AND request_id=?",
                             (trip_id, request_id)).fetchone()
            if old:
                if old[:2] != (message, version):
                    raise VersionConflict("这次发送标记已用于其他内容，请重新发送。")
                if old[2]:
                    return {"response": json.loads(old[2])}
                if old[3] == "running" and old[4] > now:
                    raise VersionConflict("这条消息正在处理中，请稍后查看回复。")
                db.execute("UPDATE assistant_turns SET state='running',owner=?,lease_until=? WHERE trip_id=? AND request_id=?",
                           (owner, now + lease_seconds, trip_id, request_id))
            else:
                db.execute("INSERT INTO assistant_turns(trip_id,request_id,message,base_version,state,owner,lease_until) VALUES(?,?,?,?,'running',?,?)",
                           (trip_id, request_id, message, version, owner, now + lease_seconds))
                db.execute("INSERT INTO assistant_messages(trip_id,role,content,created_at,version) VALUES(?,?,?,?,?)",
                       (trip_id, "user", message, datetime.now(UTC).isoformat(), version))
        return {"claim": (trip_id, request_id, owner), "result_version": old[5] if old else None}

    def check_chat_owner(self, db, trip_id, request_id, owner):
        row = db.execute("SELECT state,owner,lease_until FROM assistant_turns WHERE trip_id=? AND request_id=?",
                         (trip_id, request_id)).fetchone()
        if not row or row[0] != "running" or row[1] != owner or row[2] <= self.clock():
            raise VersionConflict("这条消息的处理已中断，请重新发送刚才的消息以恢复。")

    def release_chat(self, claim):
        with self.connect() as db:
            db.execute("UPDATE assistant_turns SET state='interrupted',lease_until=0 WHERE trip_id=? AND request_id=? AND owner=? AND state='running'", claim)

    def finish_chat(self, claim, response):
        trip_id, request_id, _ = claim
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.check_chat_owner(db, *claim)
            db.execute("INSERT INTO assistant_messages(trip_id,role,content,created_at,version) VALUES(?,?,?,?,?)",
                       (trip_id, "assistant", response["reply"], datetime.now(UTC).isoformat(), response["trip"]["version"]))
            db.execute("UPDATE assistant_turns SET response=?,state='completed',lease_until=0 WHERE trip_id=? AND request_id=?",
                       (json.dumps(response, ensure_ascii=False), trip_id, request_id))
