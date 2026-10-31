import sqlite3
from contextlib import contextmanager
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
            db.execute("PRAGMA user_version = 1")

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
            db.execute("INSERT INTO trips (id,version,payload) VALUES (?,?,?)",
                       (result.id, result.version, result.model_dump_json()))

    def list(self, query="", limit=50):
        with self.connect() as db:
            rows = db.execute("SELECT t.payload FROM trips t JOIN (SELECT id, MAX(version) v FROM trips GROUP BY id) latest "
                              "ON t.id=latest.id AND t.version=latest.v ORDER BY t.rowid DESC LIMIT 500").fetchall()
        summaries = []
        for row in rows:
            r = TripResult.model_validate_json(row[0])
            title = " → ".join(r.constraints.cities) or "待补充旅行"
            title += f" · {r.constraints.start_date or '日期待定'} · {r.constraints.days or '?'}天"
            if query and query.casefold() not in title.casefold():
                continue
            summaries.append({"id": r.id, "version": r.version, "title": title, "created_at": r.created_at,
                              "status": r.status})
            if len(summaries) >= limit:
                break
        return summaries

    def versions(self, trip_id):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM trips WHERE id=? ORDER BY version DESC", (trip_id,)).fetchall()
        results = [TripResult.model_validate_json(row[0]) for row in rows]
        return [{"version": r.version, "created_at": r.created_at, "status": r.status,
                 "budget": r.budget.total.model_dump() if r.budget else None} for r in results]
