"""SQLite persistence for the fixed C/R/U conversation loop.

Each public operation owns a short connection. Network requests happen outside
transactions: ``claim_turn`` records their identity, and ``complete_turn`` uses
the claimed revision to commit the answer and working state together.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from uuid import uuid4

from .sharing import SharingMixin
from .extensions import ExtensionsMixin


SCENES = ("planning", "shopping", "cooking")
REFERENCE_KINDS = ("candidate_set", "selected_plan", "recipe", "shopping_reference")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空。")
    return value


def _state(conversation: dict) -> dict:
    return {key: conversation[key] for key in ("conditions", "carry_note", "reference")}


class Repository(SharingMixin, ExtensionsMixin):
    """A file-backed repository; persisted pending calls never retry themselves."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def _connect(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
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

    def initialize(self, initial_defaults: dict | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS models (
                    id TEXT PRIMARY KEY,
                    profile_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scene_models (
                    scene TEXT PRIMARY KEY CHECK(scene IN ('planning','shopping','cooking')),
                    model_id TEXT NOT NULL REFERENCES models(id)
                );
                CREATE TABLE IF NOT EXISTS meals (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    selected_snapshot_json TEXT,
                    selected_owner_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    meal_id TEXT REFERENCES meals(id),
                    scene TEXT NOT NULL CHECK(scene IN ('planning','shopping','cooking')),
                    parent_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
                    explain INTEGER NOT NULL CHECK(explain IN (0,1)),
                    model_id TEXT NOT NULL REFERENCES models(id),
                    title TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
                    conditions TEXT NOT NULL DEFAULT '',
                    carry_note TEXT NOT NULL DEFAULT '',
                    reference_id TEXT REFERENCES reference_versions(id) ON DELETE SET NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reference_versions (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN
                        ('candidate_set','selected_plan','recipe','shopping_reference')),
                    content TEXT NOT NULL,
                    source_turn_id TEXT REFERENCES turns(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    user_input TEXT NOT NULL,
                    answer TEXT NOT NULL DEFAULT '',
                    reference_id TEXT REFERENCES reference_versions(id) ON DELETE SET NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','succeeded','failed')),
                    expected_revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK(status IN ('pending','succeeded','failed')),
                    request_snapshot_json TEXT NOT NULL,
                    before_state_json TEXT NOT NULL,
                    after_state_json TEXT,
                    raw_response TEXT NOT NULL DEFAULT '',
                    usage_json TEXT NOT NULL DEFAULT '{}',
                    elapsed_ms INTEGER NOT NULL DEFAULT 0,
                    http_status INTEGER,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS state_audits (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    source TEXT NOT NULL,
                    source_turn_id TEXT REFERENCES turns(id) ON DELETE SET NULL,
                    before_state_json TEXT NOT NULL,
                    after_state_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meal_shared_contexts (
                    meal_id TEXT PRIMARY KEY REFERENCES meals(id) ON DELETE CASCADE,
                    summary TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shared_context_audits (
                    id TEXT PRIMARY KEY,
                    meal_id TEXT NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
                    source_turn_id TEXT REFERENCES turns(id) ON DELETE SET NULL,
                    before_summary TEXT NOT NULL,
                    after_summary TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS conversation_meal_scene
                    ON conversations(meal_id,scene,created_at);
                CREATE INDEX IF NOT EXISTS conversation_parent ON conversations(parent_id);
                CREATE INDEX IF NOT EXISTS turn_conversation_status
                    ON turns(conversation_id,status,created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS one_pending_call_per_conversation
                    ON calls(conversation_id) WHERE status='pending';
            """)
            self._initialize_sharing(connection)
            self._initialize_extensions(connection)
            from .attempts import initialize_attempts
            initialize_attempts(connection)
            from .legacy_search_schema import initialize_search
            initialize_search(connection)
            from .maintenance import initialize_maintenance
            initialize_maintenance(connection)
            from .reference_layout import initialize_reference_layout
            initialize_reference_layout(connection)
        # Seeding is idempotent and never overwrites a configured default/profile.
        from .adapters import validate_profile

        defaults = None
        if initial_defaults is not None:
            if not isinstance(initial_defaults, dict) or set(initial_defaults) != {"models", "scene_models"}:
                raise ValueError("初始模型配置格式无效。")
            models = initial_defaults["models"]
            scenes = initial_defaults["scene_models"]
            if not isinstance(models, list) or not isinstance(scenes, dict) or set(scenes) != set(SCENES):
                raise ValueError("初始模型或场景配置无效。")
            models = [validate_profile(item) for item in models]
            ids = [item["id"] for item in models]
            if len(ids) != len(set(ids)) or not all(model_id in ids for model_id in scenes.values()):
                raise ValueError("初始场景模型不存在或模型 ID 重复。")
            defaults = models, scenes

        profile = validate_profile({
            "id": "demo", "name": "本地演示（不调用模型）", "adapter": "demo",
            "base_url": "", "model": "demo", "credential_ref": "",
        })
        timestamp = _now()
        with self._connect(write=True) as connection:
            # Only a fresh database receives distribution defaults. Existing
            # profiles and scene choices remain untouched on every upgrade.
            if defaults is not None and not connection.execute("SELECT 1 FROM models LIMIT 1").fetchone():
                models, scenes = defaults
                connection.executemany(
                    "INSERT INTO models VALUES (?,?,?,?)",
                    [(item["id"], _json(item), timestamp, timestamp) for item in models],
                )
                connection.executemany("INSERT INTO scene_models VALUES (?,?)", list(scenes.items()))
            connection.execute(
                "INSERT OR IGNORE INTO models VALUES (?,?,?,?)",
                (profile["id"], _json(profile), timestamp, timestamp),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO scene_models VALUES (?,?)",
                [(scene, "demo") for scene in SCENES],
            )
        # Additive observation tables: no rewrite of working state or history.
        from .feedback import FeedbackStore
        from .comparisons import initialize_comparisons
        FeedbackStore(self.path).initialize()
        initialize_comparisons(self.path)

    @staticmethod
    def _meal(row: sqlite3.Row) -> dict:
        result = dict(row)
        raw = result.pop("selected_snapshot_json")
        result["selected_snapshot"] = json.loads(raw) if raw else None
        return result

    @staticmethod
    def _reference(connection: sqlite3.Connection, reference_id: str | None) -> dict | None:
        if reference_id is None:
            return None
        row = connection.execute(
            "SELECT id,kind,content FROM reference_versions WHERE id=?", (reference_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("参考版本不存在，数据库状态不完整。")
        return dict(row)

    @classmethod
    def _conversation(cls, connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
        result = dict(row)
        result["reference"] = cls._reference(connection, result.pop("reference_id"))
        result["explain"] = bool(result["explain"])
        result["archived"] = bool(result["archived"])
        return result

    @classmethod
    def _get_conversation(cls, connection: sqlite3.Connection, conversation_id: str) -> dict:
        row = connection.execute(
            "SELECT * FROM conversations WHERE id=?", (conversation_id,)
        ).fetchone()
        if row is None:
            raise ValueError("会话不存在。")
        return cls._conversation(connection, row)

    @staticmethod
    def _shared_context(connection: sqlite3.Connection, conversation: dict) -> dict | None:
        if conversation["parent_id"] is not None or conversation["meal_id"] is None:
            return None
        row = connection.execute(
            "SELECT summary,revision FROM meal_shared_contexts WHERE meal_id=?",
            (conversation["meal_id"],),
        ).fetchone()
        return dict(row) if row else {"summary": "", "revision": 0}

    def get_shared_context(self, conversation_id: str) -> dict | None:
        """Read the current meal summary; branches keep their own context."""
        with self._connect() as connection:
            conversation = self._get_conversation(connection, conversation_id)
            return self._shared_context(connection, conversation)

    @classmethod
    def _check_shared_snapshot(
        cls, connection: sqlite3.Connection, conversation: dict, request_snapshot: dict,
    ) -> None:
        # Older persisted requests have no shared-context snapshot.
        context_meta = request_snapshot.get("context_meta")
        if isinstance(context_meta, dict) and "shared_delivery" in context_meta:
            return
        if not isinstance(context_meta, dict) or "shared_context" not in context_meta:
            return
        snapshot = context_meta["shared_context"]
        current = cls._shared_context(connection, conversation)
        if snapshot != current:
            raise RuntimeError("共享空间已变化，请重新预览并提交，本次响应未应用。")

    @classmethod
    def _update_shared_context(
        cls, connection: sqlite3.Connection, conversation: dict,
        turn_id: str, summary: str | None,
    ) -> None:
        if summary is None:
            return
        before = cls._shared_context(connection, conversation)
        if before is None:
            raise ValueError("该会话没有共享空间，不能更新共享摘要。")
        if before["summary"] == summary:
            return
        revision, timestamp = before["revision"] + 1, _now()
        connection.execute(
            "INSERT INTO meal_shared_contexts(meal_id,summary,revision,updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(meal_id) DO UPDATE SET "
            "summary=excluded.summary,revision=excluded.revision,updated_at=excluded.updated_at",
            (conversation["meal_id"], summary, revision, timestamp),
        )
        connection.execute(
            "INSERT INTO shared_context_audits "
            "(id,meal_id,source_turn_id,before_summary,after_summary,revision,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (str(uuid4()), conversation["meal_id"], turn_id, before["summary"], summary, revision, timestamp),
        )

    def list_meals(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM meals ORDER BY created_at DESC,rowid DESC")
            return [self._meal(row) for row in rows]

    def create_meal(self, name: str) -> dict:
        name = _required_text(name, "餐次名称").strip()
        meal_id, timestamp = str(uuid4()), _now()
        with self._connect(write=True) as connection:
            connection.execute(
                "INSERT INTO meals(id,name,created_at,updated_at) VALUES (?,?,?,?)",
                (meal_id, name, timestamp, timestamp),
            )
            return self._meal(connection.execute("SELECT * FROM meals WHERE id=?", (meal_id,)).fetchone())

    def rename_meal(self, meal_id: str, name: str) -> None:
        name = _required_text(name, "餐次名称").strip()
        with self._connect(write=True) as connection:
            cursor = connection.execute(
                "UPDATE meals SET name=?,updated_at=? WHERE id=?", (name, _now(), meal_id)
            )
            if cursor.rowcount != 1:
                raise ValueError("餐次不存在。")

    def get_meal(self, meal_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM meals WHERE id=?", (meal_id,)).fetchone()
            if row is None:
                raise ValueError("餐次不存在。")
            return self._meal(row)

    def list_models(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("SELECT profile_json FROM models ORDER BY created_at,rowid")
            return [json.loads(row["profile_json"]) for row in rows]

    def save_model(self, profile: dict) -> None:
        from .adapters import validate_profile

        normalized = validate_profile(profile)
        timestamp = _now()
        with self._connect(write=True) as connection:
            connection.execute("""
                INSERT INTO models(id,profile_json,created_at,updated_at) VALUES (?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    profile_json=excluded.profile_json,updated_at=excluded.updated_at
                """, (normalized["id"], _json(normalized), timestamp, timestamp))

    def get_model(self, model_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute("SELECT profile_json FROM models WHERE id=?", (model_id,)).fetchone()
            if row is None:
                raise ValueError("模型配置不存在。")
            return json.loads(row["profile_json"])

    def get_scene_model(self, scene: str) -> str:
        if scene not in SCENES:
            raise ValueError("未知场景。")
        with self._connect() as connection:
            row = connection.execute("SELECT model_id FROM scene_models WHERE scene=?", (scene,)).fetchone()
            if row is None:
                raise RuntimeError("场景默认模型尚未初始化。")
            return row["model_id"]

    def set_scene_model(self, scene: str, model_id: str) -> None:
        if scene not in SCENES:
            raise ValueError("未知场景。")
        with self._connect(write=True) as connection:
            if connection.execute("SELECT 1 FROM models WHERE id=?", (model_id,)).fetchone() is None:
                raise ValueError("模型配置不存在。")
            connection.execute(
                "INSERT INTO scene_models VALUES (?,?) ON CONFLICT(scene) DO UPDATE SET model_id=excluded.model_id",
                (scene, model_id),
            )

    def create_conversation(
        self, scene: str, meal_id: str | None = None, parent_id: str | None = None,
        explain: bool = False, model_id: str | None = None, title: str | None = None,
    ) -> dict:
        with self._connect(write=True) as connection:
            if parent_id is not None:
                parent = self._get_conversation(connection, parent_id)
                scene, meal_id = parent["scene"], parent["meal_id"]
                explain, model_id = parent["explain"], parent["model_id"]
            if scene not in SCENES:
                raise ValueError("未知场景。")
            if type(explain) is not bool:
                raise ValueError("解释模式必须为布尔值。")
            meal = None
            if meal_id is not None:
                meal_row = connection.execute("SELECT * FROM meals WHERE id=?", (meal_id,)).fetchone()
                if meal_row is None:
                    raise ValueError("餐次不存在。")
                meal = self._meal(meal_row)
            if model_id is None:
                model_id = connection.execute(
                    "SELECT model_id FROM scene_models WHERE scene=?", (scene,)
                ).fetchone()["model_id"]
            if connection.execute("SELECT 1 FROM models WHERE id=?", (model_id,)).fetchone() is None:
                raise ValueError("模型配置不存在。")
            if title is None:
                title = {"planning": "饮食规划", "shopping": "买菜挑菜", "cooking": "备菜煮菜"}[scene]
                if parent_id is not None:
                    title += " · 临时分支"
            title = _required_text(title, "会话名称").strip()
            conversation_id, timestamp = str(uuid4()), _now()
            connection.execute("""
                INSERT INTO conversations(
                    id,meal_id,scene,parent_id,explain,model_id,title,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """, (conversation_id, meal_id, scene, parent_id, int(explain), model_id,
                       title, timestamp, timestamp))
            if parent_id is None and scene in ("shopping", "cooking") and meal and meal["selected_snapshot"]:
                snapshot = meal["selected_snapshot"]
                reference = snapshot["reference"]
                reference_id = self._insert_reference(
                    connection, conversation_id, reference["kind"], reference["content"], None
                )
                connection.execute(
                    "UPDATE conversations SET conditions=?,reference_id=? WHERE id=?",
                    (snapshot["conditions"], reference_id, conversation_id),
                )
            return self._get_conversation(connection, conversation_id)

    def get_conversation(self, conversation_id: str) -> dict:
        with self._connect() as connection:
            return self._get_conversation(connection, conversation_id)

    def list_conversations(self, meal_id: str | None = None, include_archived: bool = True) -> list[dict]:
        with self._connect() as connection:
            query = "SELECT * FROM conversations WHERE meal_id IS ?"
            if not include_archived:
                query += " AND archived=0"
            rows = connection.execute(query + " ORDER BY created_at DESC,rowid DESC", (meal_id,))
            return [self._conversation(connection, row) for row in rows]

    def find_active_conversation(self, meal_id: str | None, scene: str) -> dict | None:
        if scene not in SCENES:
            raise ValueError("未知场景。")
        with self._connect() as connection:
            row = connection.execute("""
                SELECT * FROM conversations
                WHERE meal_id IS ? AND scene=? AND parent_id IS NULL AND archived=0
                ORDER BY created_at DESC,rowid DESC LIMIT 1
                """, (meal_id, scene)).fetchone()
            return self._conversation(connection, row) if row else None

    def update_settings(
        self, conversation_id: str, explain: bool | None = None,
        model_id: str | None = None, title: str | None = None,
    ) -> None:
        with self._connect(write=True) as connection:
            existing = self._get_conversation(connection, conversation_id)
            if explain is not None and type(explain) is not bool:
                raise ValueError("解释模式必须为布尔值。")
            if model_id is not None and connection.execute(
                "SELECT 1 FROM models WHERE id=?", (model_id,)
            ).fetchone() is None:
                raise ValueError("模型配置不存在。")
            explain = existing["explain"] if explain is None else explain
            model_id = existing["model_id"] if model_id is None else model_id
            title = existing["title"] if title is None else _required_text(title, "会话名称").strip()
            if (explain, model_id, title) == (existing["explain"], existing["model_id"], existing["title"]):
                return
            connection.execute("""
                UPDATE conversations SET explain=?,model_id=?,title=?,revision=revision+1,updated_at=?
                WHERE id=?
                """, (int(explain), model_id, title, _now(), conversation_id))

    def archive_conversation(self, conversation_id: str, archived: bool = True) -> None:
        if type(archived) is not bool:
            raise ValueError("归档标记必须为布尔值。")
        with self._connect(write=True) as connection:
            cursor = connection.execute(
                "UPDATE conversations SET archived=?,updated_at=? WHERE id=?",
                (int(archived), _now(), conversation_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("会话不存在。")

    def delete_conversation(self, conversation_id: str) -> None:
        with self._connect(write=True) as connection:
            self._get_conversation(connection, conversation_id)
            rows = connection.execute("""
                WITH RECURSIVE descendants(id) AS (
                    SELECT id FROM conversations WHERE id=?
                    UNION
                    SELECT c.id FROM conversations c JOIN descendants d ON c.parent_id=d.id
                ) SELECT id FROM descendants
                """, (conversation_id,)).fetchall()
            ids = [row["id"] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            if connection.execute(
                f"SELECT 1 FROM calls WHERE conversation_id IN ({placeholders}) AND status='pending' LIMIT 1", ids
            ).fetchone():
                raise RuntimeError("会话或其分支还有待完成的请求，请先将中断请求标记为失败。")
            if connection.execute(
                f"SELECT 1 FROM reference_layout_tasks WHERE conversation_id IN ({placeholders}) AND status='pending' LIMIT 1", ids
            ).fetchone():
                raise RuntimeError("会话还有待完成的参考排版，请先等待或将中断排版标记结束。")
            connection.execute(
                f"UPDATE meals SET selected_snapshot_json=NULL,selected_owner_id=NULL,updated_at=? "
                f"WHERE selected_owner_id IN ({placeholders})", [_now(), *ids],
            )
            # Clear all circular references before removing the parent tree.
            connection.execute(f"DELETE FROM state_audits WHERE conversation_id IN ({placeholders})", ids)
            connection.execute(f"DELETE FROM calls WHERE conversation_id IN ({placeholders})", ids)
            connection.execute(f"DELETE FROM turns WHERE conversation_id IN ({placeholders})", ids)
            connection.execute(f"UPDATE conversations SET reference_id=NULL WHERE id IN ({placeholders})", ids)
            connection.execute(f"DELETE FROM reference_versions WHERE conversation_id IN ({placeholders})", ids)
            connection.execute(f"DELETE FROM conversations WHERE id IN ({placeholders})", ids)

    @staticmethod
    def _has_pending_usage_requests(connection: sqlite3.Connection) -> bool:
        return any(connection.execute(
            f"SELECT 1 FROM {table} WHERE status='pending' LIMIT 1"
        ).fetchone() is not None for table in ("calls", "turns", "comparisons", "search_tasks", "maintenance_tasks", "reference_layout_tasks"))

    def has_pending_usage_requests(self) -> bool:
        with self._connect() as connection:
            return self._has_pending_usage_requests(connection)

    def reset_usage_data(self) -> dict[str, int]:
        """Clear usage atomically; model profiles and application settings survive.

        BEGIN IMMEDIATE prevents a new request from being claimed between the
        pending check and deletion. Never abort an in-flight paid request here.
        """
        tables = (
            "reference_layout_tasks", "maintenance_tasks", "maintenance_notices", "persistent_maintenance_state", "search_tasks", "call_attempts", "turn_extensions", "context_displays",
            "model_feedback", "context_feedback", "app_feedback", "user_feedback",
            "comparisons", "shared_receipts", "shared_entries", "day_scopes",
            "shared_context_audits", "meal_shared_contexts", "state_audits",
            "calls", "turns", "reference_versions", "conversations", "meals",
        )
        with self._connect(write=True) as connection:
            if self._has_pending_usage_requests(connection):
                raise RuntimeError("还有未结束的普通请求、版本比较或工具任务，请先等待完成，或将中断请求标记为失败，再清空使用数据。")
            counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in tables}
            for table in tables:
                connection.execute(f"DELETE FROM {table}")
            return counts

    @staticmethod
    def _insert_reference(
        connection: sqlite3.Connection, conversation_id: str,
        kind: str, content: str, source_turn_id: str | None,
    ) -> str:
        if kind not in REFERENCE_KINDS or not isinstance(content, str) or not content.strip():
            raise ValueError("参考类型或正文无效。")
        reference_id = str(uuid4())
        connection.execute(
            "INSERT INTO reference_versions VALUES (?,?,?,?,?,?)",
            (reference_id, conversation_id, kind, content, source_turn_id, _now()),
        )
        return reference_id

    @staticmethod
    def _publish(connection: sqlite3.Connection, conversation: dict) -> None:
        if conversation["scene"] != "planning" or conversation["parent_id"] is not None or conversation["meal_id"] is None:
            return
        reference = conversation["reference"]
        if reference and reference["kind"] == "selected_plan":
            snapshot = {
                "conditions": conversation["conditions"], "reference": reference,
                "owner_conversation_id": conversation["id"], "published_at": _now(),
            }
            connection.execute(
                "UPDATE meals SET selected_snapshot_json=?,selected_owner_id=?,updated_at=? WHERE id=?",
                (_json(snapshot), conversation["id"], _now(), conversation["meal_id"]),
            )
        else:
            connection.execute(
                "UPDATE meals SET selected_snapshot_json=NULL,selected_owner_id=NULL,updated_at=? "
                "WHERE id=? AND selected_owner_id=?",
                (_now(), conversation["meal_id"], conversation["id"]),
            )

    @staticmethod
    def _audit(
        connection: sqlite3.Connection, conversation_id: str, source: str,
        before: dict, after: dict, revision: int, turn_id: str | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO state_audits VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid4()), conversation_id, source, turn_id, _json(before), _json(after), revision, _now()),
        )

    def correct_state(
        self, conversation_id: str, conditions: str, carry_note: str,
        reference_kind: str | None = None, reference_content: str | None = None,
    ) -> None:
        if not isinstance(conditions, str) or not isinstance(carry_note, str):
            raise ValueError("C 和 U 必须为文本。")
        with self._connect(write=True) as connection:
            before = self._get_conversation(connection, conversation_id)
            reference_id = None
            if reference_kind is not None:
                reference_id = self._insert_reference(
                    connection, conversation_id, reference_kind, reference_content, None
                )
            connection.execute(
                "UPDATE conversations SET conditions=?,carry_note=?,reference_id=?,revision=revision+1,updated_at=? WHERE id=?",
                (conditions, carry_note, reference_id, _now(), conversation_id),
            )
            after = self._get_conversation(connection, conversation_id)
            self._audit(connection, conversation_id, "manual_correction", _state(before), _state(after), after["revision"])
            self._publish(connection, after)

    @classmethod
    def _turn(cls, connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
        result = dict(row)
        result["reference"] = cls._reference(connection, result.pop("reference_id"))
        return result

    @staticmethod
    def _call(row: sqlite3.Row) -> dict:
        result = dict(row)
        for field in ("request_snapshot", "before_state", "after_state", "usage"):
            raw = result.pop(field + "_json")
            result[field] = json.loads(raw) if raw is not None else None
        return result

    def recent_turns(self, conversation_id: str, limit: int) -> list[dict]:
        if type(limit) is not int or limit < 0:
            raise ValueError("近期轮数必须为非负整数。")
        with self._connect() as connection:
            self._get_conversation(connection, conversation_id)
            rows = connection.execute(
                "SELECT * FROM turns WHERE conversation_id=? AND status='succeeded' "
                "ORDER BY created_at DESC,rowid DESC LIMIT ?", (conversation_id, limit)
            ).fetchall()
            return [self._turn(connection, row) for row in reversed(rows)]

    def list_turns(self, conversation_id: str) -> list[dict]:
        with self._connect() as connection:
            self._get_conversation(connection, conversation_id)
            rows = connection.execute(
                "SELECT * FROM turns WHERE conversation_id=? ORDER BY created_at,rowid", (conversation_id,)
            )
            return [self._turn(connection, row) for row in rows]

    def list_calls(self, conversation_id: str) -> list[dict]:
        with self._connect() as connection:
            self._get_conversation(connection, conversation_id)
            rows = connection.execute(
                "SELECT * FROM calls WHERE conversation_id=? ORDER BY created_at DESC,rowid DESC", (conversation_id,)
            )
            return [self._call(row) for row in rows]

    def get_call(self, turn_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM calls WHERE id=?", (turn_id,)).fetchone()
            return self._call(row) if row else None

    def claim_turn(
        self, turn_id: str, conversation_id: str, user_input: str,
        request_snapshot: dict, before_state: dict, expected_revision: int,
    ) -> bool:
        _required_text(turn_id, "提交 ID")
        _required_text(user_input, "用户输入")
        if not isinstance(request_snapshot, dict) or not isinstance(before_state, dict):
            raise ValueError("请求和状态快照必须为对象。")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("会话版本无效。")
        with self._connect(write=True) as connection:
            if connection.execute("SELECT 1 FROM turns WHERE id=?", (turn_id,)).fetchone():
                return False
            conversation = self._get_conversation(connection, conversation_id)
            if conversation["revision"] != expected_revision or _state(conversation) != before_state:
                raise RuntimeError("会话状态已变化，请重新预览并提交。")
            self._check_shared_snapshot(connection, conversation, request_snapshot)
            self._check_shared_delivery(connection, conversation, request_snapshot)
            if connection.execute(
                "SELECT 1 FROM calls WHERE conversation_id=? AND status='pending'", (conversation_id,)
            ).fetchone():
                raise RuntimeError("该会话已有待完成请求，不会重复调用。")
            timestamp = _now()
            connection.execute(
                "INSERT INTO turns(id,conversation_id,user_input,status,expected_revision,created_at) VALUES (?,?,?,'pending',?,?)",
                (turn_id, conversation_id, user_input, expected_revision, timestamp),
            )
            connection.execute(
                "INSERT INTO calls(id,conversation_id,status,request_snapshot_json,before_state_json,created_at) "
                "VALUES (?,?,'pending',?,?,?)",
                (turn_id, conversation_id, _json(request_snapshot), _json(before_state), timestamp),
            )
            return True

    def complete_turn(
        self, turn_id: str, output: dict, raw_response: str, usage: dict,
        elapsed_ms: int, http_status: int | None = None, *, shared_update: str | None = None,
        shared_additions: list[dict] | None = None, extensions: dict | None = None,
    ) -> dict:
        from .protocol import validate_response, normalize_shared_additions

        # Validate even for direct callers: malformed updates cannot partly apply.
        output = validate_response(_json(output))
        additions = normalize_shared_additions([] if shared_additions is None else shared_additions)
        if not isinstance(raw_response, str) or not isinstance(usage, dict):
            raise ValueError("原始响应与用量格式无效。")
        if shared_update is not None and not isinstance(shared_update, str):
            raise ValueError("共享摘要必须为文本或 null。")
        usage_json = _json(usage)
        with self._connect(write=True) as connection:
            turn = connection.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
            call = connection.execute("SELECT * FROM calls WHERE id=?", (turn_id,)).fetchone()
            if turn is None or call is None:
                raise ValueError("请求不存在。")
            if turn["status"] != "pending" or call["status"] != "pending":
                raise RuntimeError("该请求已结束，不能重复保存。")
            before = self._get_conversation(connection, turn["conversation_id"])
            if before["revision"] != turn["expected_revision"]:
                raise RuntimeError("请求期间会话已修改，本次响应未应用到工作状态。")
            self._check_shared_snapshot(connection, before, json.loads(call["request_snapshot_json"]))
            self._update_shared_context(connection, before, turn_id, shared_update)
            self._complete_shared_delivery(
                connection, before, turn_id, json.loads(call["request_snapshot_json"]), additions,
            )
            current_reference_id = before["reference"]["id"] if before["reference"] else None
            replacement_id = None
            update = output["reference_update"]
            if update is not None:
                if update["action"] == "clear":
                    current_reference_id = None
                else:
                    replacement_id = self._insert_reference(
                        connection, before["id"], update["kind"], update["content"], turn_id
                    )
                    current_reference_id = replacement_id
            cursor = connection.execute(
                "UPDATE conversations SET conditions=?,carry_note=?,reference_id=?,revision=revision+1,updated_at=? "
                "WHERE id=? AND revision=?",
                (output["state"]["conditions"], output["state"]["carry_note"], current_reference_id,
                 _now(), before["id"], turn["expected_revision"]),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("会话版本冲突，响应未保存。")
            after = self._get_conversation(connection, before["id"])
            timestamp = _now()
            connection.execute(
                "UPDATE turns SET answer=?,reference_id=?,status='succeeded',completed_at=? WHERE id=?",
                (output["answer"], replacement_id, timestamp, turn_id),
            )
            connection.execute(
                "UPDATE calls SET status='succeeded',raw_response=?,usage_json=?,elapsed_ms=?,http_status=?,"
                "after_state_json=?,completed_at=? WHERE id=?",
                (raw_response, usage_json, max(0, int(elapsed_ms)), http_status, _json(_state(after)), timestamp, turn_id),
            )
            self._audit(connection, before["id"], "model_turn", _state(before), _state(after), after["revision"], turn_id)
            saved_extensions = self._save_extensions(connection, before["id"], turn_id, extensions)
            if before["parent_id"] is None:
                from .persistent_impact import record_impact
                snapshot_meta = json.loads(call["request_snapshot_json"]).get("context_meta", {})
                generation = snapshot_meta.get("persistent_generation") if isinstance(snapshot_meta, dict) else None
                record_impact(connection, saved_extensions.get("persistent_impact"), generation)
            self._publish(connection, after)
            result = self._turn(connection, connection.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone())
            result["before_state"], result["after_state"] = _state(before), _state(after)
            return result

    def fail_turn(
        self, turn_id: str, error: str, raw_response: str = "", usage: dict | None = None,
        elapsed_ms: int = 0, http_status: int | None = None,
    ) -> None:
        if not isinstance(error, str) or not isinstance(raw_response, str) or (usage is not None and not isinstance(usage, dict)):
            raise ValueError("失败记录格式无效。")
        with self._connect(write=True) as connection:
            call = connection.execute("SELECT * FROM calls WHERE id=?", (turn_id,)).fetchone()
            if call is None:
                raise ValueError("请求不存在。")
            if call["status"] != "pending":
                return
            timestamp = _now()
            connection.execute(
                "UPDATE calls SET status='failed',error=?,raw_response=?,usage_json=?,elapsed_ms=?,http_status=?,completed_at=? WHERE id=?",
                (error, raw_response, _json(usage or {}), max(0, int(elapsed_ms)), http_status, timestamp, turn_id),
            )
            connection.execute("UPDATE turns SET status='failed',completed_at=? WHERE id=?", (timestamp, turn_id))

    def export_conversation(self, conversation_id: str) -> dict:
        # A read transaction keeps the exported state and histories consistent.
        with self._connect() as connection:
            connection.execute("BEGIN")
            conversation = self._get_conversation(connection, conversation_id)
            turns = connection.execute(
                "SELECT * FROM turns WHERE conversation_id=? ORDER BY created_at,rowid", (conversation_id,)
            ).fetchall()
            calls = connection.execute(
                "SELECT * FROM calls WHERE conversation_id=? ORDER BY created_at,rowid", (conversation_id,)
            ).fetchall()
            references = connection.execute(
                "SELECT * FROM reference_versions WHERE conversation_id=? ORDER BY created_at,rowid", (conversation_id,)
            ).fetchall()
            audits = []
            for row in connection.execute(
                "SELECT * FROM state_audits WHERE conversation_id=? ORDER BY created_at,rowid", (conversation_id,)
            ):
                audit = dict(row)
                audit["before_state"] = json.loads(audit.pop("before_state_json"))
                audit["after_state"] = json.loads(audit.pop("after_state_json"))
                audits.append(audit)
            return {
                "format_version": 1, "exported_at": _now(), "conversation": conversation,
                "turns": [self._turn(connection, row) for row in turns],
                "calls": [self._call(row) for row in calls],
                "reference_versions": [dict(row) for row in references], "state_audits": audits,
            }
