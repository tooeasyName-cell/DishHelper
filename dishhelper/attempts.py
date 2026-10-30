"""Per-network-attempt evidence for bounded normal-chat retries."""
from datetime import datetime, timezone
import json


def initialize_attempts(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS call_attempts (
            turn_id TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
            number INTEGER NOT NULL CHECK(number BETWEEN 1 AND 2),
            status TEXT NOT NULL CHECK(status IN ('pending','succeeded','failed')),
            request_snapshot_json TEXT NOT NULL,
            raw_response TEXT NOT NULL DEFAULT '',
            usage_json TEXT NOT NULL DEFAULT '{}',
            elapsed_ms INTEGER NOT NULL DEFAULT 0,
            http_status INTEGER,
            error TEXT NOT NULL DEFAULT '',
            error_category TEXT NOT NULL DEFAULT '',
            retry_delay_seconds REAL,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            PRIMARY KEY(turn_id,number)
        );
    """)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class AttemptStore:
    def __init__(self, repository):
        self.repo = repository

    def begin(self, turn_id, number, request):
        snapshot = _json(request)
        with self.repo._connect(write=True) as connection:
            row = connection.execute("SELECT status FROM calls WHERE id=?", (turn_id,)).fetchone()
            if row is None or row["status"] != "pending":
                raise RuntimeError("请求已经结束，不能再发起模型尝试。")
            connection.execute("""INSERT INTO call_attempts
                (turn_id,number,status,request_snapshot_json,created_at) VALUES (?,?,'pending',?,?)""",
                (turn_id, number, snapshot, _now()))
            # Frozen A/B input is the request that produced the stored answer;
            # each earlier request remains independently available in this table.
            connection.execute("UPDATE calls SET request_snapshot_json=? WHERE id=?", (snapshot, turn_id))

    def finish(self, turn_id, number, *, status, raw_response, usage, elapsed_ms,
               http_status=None, error="", error_category="", retry_delay_seconds=None):
        with self.repo._connect(write=True) as connection:
            cursor = connection.execute("""UPDATE call_attempts SET status=?,raw_response=?,usage_json=?,
                elapsed_ms=?,http_status=?,error=?,error_category=?,retry_delay_seconds=?,completed_at=?
                WHERE turn_id=? AND number=? AND status='pending'""",
                (status, raw_response, _json(usage), elapsed_ms, http_status, error, error_category,
                 retry_delay_seconds, _now(), turn_id, number))
            if cursor.rowcount != 1:
                raise RuntimeError("模型尝试记录已结束或不存在。")

    def list(self, turn_id):
        with self.repo._connect() as connection:
            result = []
            for row in connection.execute("SELECT * FROM call_attempts WHERE turn_id=? ORDER BY number", (turn_id,)):
                item = dict(row)
                item["request_snapshot"] = json.loads(item.pop("request_snapshot_json"))
                item["usage"] = json.loads(item.pop("usage_json"))
                result.append(item)
            return result
