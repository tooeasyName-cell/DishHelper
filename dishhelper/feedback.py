"""Local, optional observations. Nothing in this module updates C, R, U or W."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3

from .adapters import redact
from .protocol import normalize_app_feedback, normalize_context_feedback
from .storage import _json, _now

RATINGS = ("helpful", "problem", "neutral")
TAGS = ("state_wrong", "lost_condition", "reference_wrong", "too_long", "other")
FEEDBACK_STATUSES = ("absent", "empty", "reported", "invalid")
FIELDS = ("conditions", "reference", "carry_note", "answer")
CONTEXT_FIELDS = ("shared", "conditions", "reference", "carry_note")
CONTEXT_VALUES = ("helpful", "unused", "uncertain")
APP_AREAS = ("context", "prompt", "ui", "workflow")


def scrub(text, profiles=()) -> str:
    """Redact known environment credentials and recognizable token patterns.

    Profiles contain credential *references* only. This function never reads an
    .env file, enumerates the environment, or exposes credential values.
    """
    result = redact(text)
    for profile in profiles:
        result = redact(result, profile)
    result = re.sub(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|authorization)\s*[=:]\s*)(?:Bearer\s+)?[^\s,;\"']+",
        lambda match: match.group(1) + "[REDACTED]", result,
    )
    return result


class FeedbackStore:
    def __init__(self, db_path):
        self.path = Path(db_path)

    @contextmanager
    def _connect(self, write=False):
        connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if connection.in_transaction:
                connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        """Add independent tables; existing conversation data is never migrated."""
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS feedback_settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_feedback (
                    turn_id TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    feedback_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL CHECK(status IN ('absent','empty','reported','invalid')),
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS model_feedback_conversation
                    ON model_feedback(conversation_id,created_at);
                CREATE TABLE IF NOT EXISTS context_feedback (
                    turn_id TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    feedback_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL CHECK(status IN ('absent','empty','reported','invalid')),
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS context_feedback_conversation
                    ON context_feedback(conversation_id,created_at);
                CREATE TABLE IF NOT EXISTS app_feedback (
                    turn_id TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    feedback_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL CHECK(status IN ('absent','empty','reported','invalid')),
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS app_feedback_conversation
                    ON app_feedback(conversation_id,created_at);
                CREATE TABLE IF NOT EXISTS user_feedback (
                    turn_id TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    rating TEXT NOT NULL CHECK(rating IN ('helpful','problem','neutral')),
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    comment TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS user_feedback_conversation
                    ON user_feedback(conversation_id,updated_at);
            """)

    def get_settings(self):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM feedback_settings WHERE key='model_feedback_enabled'"
            ).fetchone()
        return {"model_feedback_enabled": json.loads(row[0]) if row else True}

    def set_settings(self, model_feedback_enabled):
        if type(model_feedback_enabled) is not bool:
            raise ValueError("模型反馈开关必须为布尔值。")
        with self._connect(write=True) as connection:
            connection.execute("""
                INSERT INTO feedback_settings VALUES ('model_feedback_enabled',?,?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at
            """, (_json(model_feedback_enabled), _now()))
        return self.get_settings()

    def _profiles(self, connection):
        profiles = []
        for row in connection.execute("SELECT profile_json FROM models"):
            try:
                profile = json.loads(row[0])
                if isinstance(profile, dict):
                    profiles.append(profile)
            except (TypeError, ValueError):
                continue
        # Retain redaction for a profile edited/deleted after its original call.
        for row in connection.execute("""
            SELECT DISTINCT json_extract(request_snapshot_json,'$.profile.credential_ref')
            FROM calls WHERE json_valid(request_snapshot_json)
        """):
            if isinstance(row[0], str) and row[0]:
                profiles.append({"credential_ref": row[0]})
        refs = {profile.get("credential_ref") for profile in profiles
                if isinstance(profile.get("credential_ref"), str) and profile.get("credential_ref")}
        return [{"credential_ref": ref} for ref in sorted(refs)]

    def scrub(self, text):
        with self._connect() as connection:
            profiles = self._profiles(connection)
        return scrub(text, profiles)

    @staticmethod
    def _turn(connection, turn_id):
        row = connection.execute(
            "SELECT conversation_id,status FROM calls WHERE id=?", (turn_id,)
        ).fetchone()
        if row is None:
            raise ValueError("反馈对应的请求不存在。")
        return row

    def record_model_feedback(self, turn_id, feedback, status, error=""):
        if status not in FEEDBACK_STATUSES:
            raise ValueError("未知模型反馈状态。")
        valid = isinstance(feedback, list) and len(feedback) <= 3
        if valid:
            for item in feedback:
                if (not isinstance(item, dict) or set(item) - {"field", "issue", "suggestion"}
                        or item.get("field") not in FIELDS
                        or not isinstance(item.get("issue"), str) or not item["issue"].strip()
                        or len(item["issue"]) > 300
                        or ("suggestion" in item and (not isinstance(item["suggestion"], str)
                                                     or len(item["suggestion"]) > 300))):
                    valid = False
                    break
        if (not valid or (status == "reported" and not feedback)
                or (status != "reported" and bool(feedback))):
            feedback, status, error = [], "invalid", "反馈字段格式无效，已忽略。"
        if not isinstance(error, str):
            error = "反馈字段格式无效，已忽略。"
        with self._connect(write=True) as connection:
            turn = self._turn(connection, turn_id)
            profiles = self._profiles(connection)
            # Scrub strings separately to preserve valid JSON with unusual keys.
            safe = [{key: scrub(value, profiles) for key, value in item.items()} for item in feedback]
            timestamp = _now()
            connection.execute("""
                INSERT INTO model_feedback VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(turn_id) DO UPDATE SET feedback_json=excluded.feedback_json,
                    status=excluded.status,error=excluded.error,updated_at=excluded.updated_at
            """, (turn_id, turn["conversation_id"], _json(safe), status,
                  scrub(error, profiles)[:1000], timestamp, timestamp))

    @staticmethod
    def _model(row):
        result = dict(row)
        result["feedback"] = json.loads(result.pop("feedback_json"))
        return result

    def list_model_feedback(self, conversation_id=None):
        with self._connect() as connection:
            where, args = (" WHERE conversation_id=?", (conversation_id,)) if conversation_id else ("", ())
            return [self._model(row) for row in connection.execute(
                "SELECT * FROM model_feedback" + where + " ORDER BY created_at,turn_id", args)]

    def record_context_feedback(self, turn_id, feedback, status, error=""):
        """Store optional input-use observations independently of state warnings."""
        try:
            feedback = normalize_context_feedback(feedback)
            valid = True
        except ValueError:
            valid = False
        if (not valid or status not in FEEDBACK_STATUSES
                or (status == "reported" and not feedback) or (status != "reported" and bool(feedback))):
            feedback, status, error = [], "invalid", "信息使用反馈格式无效，已忽略。"
        if not isinstance(error, str):
            error = "信息使用反馈格式无效，已忽略。"
        with self._connect(write=True) as connection:
            turn = self._turn(connection, turn_id)
            profiles = self._profiles(connection)
            safe = [{**item, **({"note": scrub(item["note"], profiles)} if "note" in item else {})}
                    for item in feedback]
            timestamp = _now()
            connection.execute("""
                INSERT INTO context_feedback VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(turn_id) DO UPDATE SET feedback_json=excluded.feedback_json,
                    status=excluded.status,error=excluded.error,updated_at=excluded.updated_at
            """, (turn_id, turn["conversation_id"], _json(safe), status,
                  scrub(error, profiles)[:1000], timestamp, timestamp))

    def list_context_feedback(self, conversation_id=None):
        with self._connect() as connection:
            where, args = (" WHERE conversation_id=?", (conversation_id,)) if conversation_id else ("", ())
            return [self._model(row) for row in connection.execute(
                "SELECT * FROM context_feedback" + where + " ORDER BY created_at,turn_id", args)]

    def record_app_feedback(self, turn_id, feedback, status, error=""):
        """Store optional process suggestions; they never authorize a state/code change."""
        try:
            feedback = normalize_app_feedback(feedback)
            valid = True
        except ValueError:
            valid = False
        if (not valid or status not in FEEDBACK_STATUSES
                or (status == "reported" and not feedback) or (status != "reported" and bool(feedback))):
            feedback, status, error = [], "invalid", "应用改进反馈格式无效，已忽略。"
        if not isinstance(error, str):
            error = "应用改进反馈格式无效，已忽略。"
        with self._connect(write=True) as connection:
            turn = self._turn(connection, turn_id)
            profiles = self._profiles(connection)
            safe = [{"area": item["area"], "observation": scrub(item["observation"], profiles)[:300],
                     "suggestion": scrub(item["suggestion"], profiles)[:300]} for item in feedback]
            timestamp = _now()
            connection.execute("""
                INSERT INTO app_feedback VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(turn_id) DO UPDATE SET feedback_json=excluded.feedback_json,
                    status=excluded.status,error=excluded.error,updated_at=excluded.updated_at
            """, (turn_id, turn["conversation_id"], _json(safe), status,
                  scrub(error, profiles)[:1000], timestamp, timestamp))

    def list_app_feedback(self, conversation_id=None):
        with self._connect() as connection:
            where, args = (" WHERE conversation_id=?", (conversation_id,)) if conversation_id else ("", ())
            return [self._model(row) for row in connection.execute(
                "SELECT * FROM app_feedback" + where + " ORDER BY created_at,turn_id", args)]

    def set_user_feedback(self, turn_id, rating, tags=None, comment=""):
        if rating not in RATINGS:
            raise ValueError("评分必须是 helpful、problem 或 neutral。")
        tags = [] if tags is None else tags
        if (not isinstance(tags, list) or len(tags) > 5
                or any(not isinstance(tag, str) or tag not in TAGS for tag in tags)):
            raise ValueError("反馈标签无效。")
        if not isinstance(comment, str) or len(comment) > 1000:
            raise ValueError("反馈说明最多 1000 字。")
        tags = list(dict.fromkeys(tags))
        with self._connect(write=True) as connection:
            turn = self._turn(connection, turn_id)
            if turn["status"] == "pending":
                raise ValueError("请求完成后才能评分。")
            timestamp = _now()
            safe_comment = scrub(comment, self._profiles(connection))
            connection.execute("""
                INSERT INTO user_feedback VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(turn_id) DO UPDATE SET rating=excluded.rating,tags_json=excluded.tags_json,
                    comment=excluded.comment,updated_at=excluded.updated_at
            """, (turn_id, turn["conversation_id"], rating, _json(tags), safe_comment,
                  timestamp, timestamp))
        return self.get_user_feedback(turn_id)

    @staticmethod
    def _user(row):
        result = dict(row)
        result["tags"] = json.loads(result.pop("tags_json"))
        return result

    def get_user_feedback(self, turn_id):
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM user_feedback WHERE turn_id=?", (turn_id,)).fetchone()
            return self._user(row) if row else None

    def list_user_feedback(self, conversation_id=None):
        with self._connect() as connection:
            where, args = (" WHERE conversation_id=?", (conversation_id,)) if conversation_id else ("", ())
            return [self._user(row) for row in connection.execute(
                "SELECT * FROM user_feedback" + where + " ORDER BY updated_at,turn_id", args)]
