"""Append-only shared facts with per-conversation delivery receipts.

This module never interprets whether a model absorbed a fact. A receipt records
only that the fact was sent in a request whose answer was successfully committed.
"""

from datetime import date, datetime, timezone
from uuid import uuid4


def _timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class SharingMixin:
    @staticmethod
    def _initialize_sharing(connection):
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS day_scopes (
                day TEXT PRIMARY KEY,
                meal_id TEXT NOT NULL UNIQUE REFERENCES meals(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS shared_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL CHECK(scope IN ('day','persistent')),
                meal_id TEXT REFERENCES meals(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                source_conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
                source_scene TEXT,
                source_turn_id TEXT REFERENCES turns(id) ON DELETE SET NULL,
                source_day TEXT,
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                created_at TEXT NOT NULL,
                legacy_key TEXT UNIQUE,
                CHECK((scope='day' AND meal_id IS NOT NULL) OR
                      (scope='persistent' AND meal_id IS NULL))
            );
            -- Manual replacement must receive a new identity even for reused text.
            -- Model additions retain their existing exact-content check below.
            DROP INDEX IF EXISTS shared_day_content;
            DROP INDEX IF EXISTS shared_persistent_content;
            CREATE TABLE IF NOT EXISTS shared_receipts (
                entry_id INTEGER NOT NULL REFERENCES shared_entries(id) ON DELETE CASCADE,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                consumed_turn_id TEXT REFERENCES turns(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(entry_id,conversation_id)
            );
        """)
        # Legacy groups remain separate; do not infer a calendar date for them.
        connection.execute("""
            INSERT OR IGNORE INTO shared_entries
                (scope,meal_id,content,created_at,legacy_key)
            SELECT 'day',meal_id,summary,updated_at,'summary:' || meal_id
            FROM meal_shared_contexts WHERE trim(summary)<>''
        """)

    def get_or_create_day(self, day):
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise ValueError("日期必须为 YYYY-MM-DD。")
        with self._connect(write=True) as connection:
            row = connection.execute(
                "SELECT meals.* FROM day_scopes JOIN meals ON meals.id=day_scopes.meal_id WHERE day=?",
                (day,),
            ).fetchone()
            if row is not None:
                return self._meal(row)
            meal_id, timestamp = str(uuid4()), _timestamp()
            connection.execute(
                "INSERT INTO meals(id,name,created_at,updated_at) VALUES (?,?,?,?)",
                (meal_id, day, timestamp, timestamp),
            )
            connection.execute("INSERT INTO day_scopes(day,meal_id) VALUES (?,?)", (day, meal_id))
            return self._meal(connection.execute("SELECT * FROM meals WHERE id=?", (meal_id,)).fetchone())

    @staticmethod
    def _day_date(connection, meal_id):
        row = connection.execute("SELECT day FROM day_scopes WHERE meal_id=?", (meal_id,)).fetchone()
        return row["day"] if row else None

    def get_day_date(self, meal_id):
        with self._connect() as connection:
            return self._day_date(connection, meal_id)

    def list_day_scopes(self):
        with self._connect() as connection:
            return [dict(row) for row in connection.execute("SELECT day,meal_id FROM day_scopes ORDER BY day DESC")]

    @staticmethod
    def _entry_item(row):
        return {key: row[key] for key in ("id", "scope", "content", "source_scene", "source_day")}

    @staticmethod
    def _eligible_entry(row, conversation):
        if conversation["parent_id"] is not None or row["source_conversation_id"] == conversation["id"]:
            return False
        if row["scope"] == "persistent":
            return True
        return (conversation["meal_id"] is not None
                and row["meal_id"] == conversation["meal_id"]
                and row["source_scene"] != conversation["scene"])

    @classmethod
    def _shared_delivery(cls, connection, conversation):
        if conversation["parent_id"] is not None:
            return None
        rows = connection.execute("""
            SELECT e.* FROM shared_entries e
            WHERE e.active=1 AND (e.scope='persistent' OR e.meal_id=?)
                AND NOT EXISTS (SELECT 1 FROM shared_receipts r
                    WHERE r.entry_id=e.id AND r.conversation_id=?)
            ORDER BY e.id
        """, (conversation["meal_id"], conversation["id"]))
        return {
            "items": [cls._entry_item(row) for row in rows if cls._eligible_entry(row, conversation)],
            "day": cls._day_date(connection, conversation["meal_id"]),
            "day_scope": conversation["meal_id"] is not None,
        }

    def get_shared_delivery(self, conversation_id):
        with self._connect() as connection:
            return self._shared_delivery(connection, self._get_conversation(connection, conversation_id))

    def list_shared_entries(self, conversation_id, include_inactive=True):
        with self._connect() as connection:
            conversation = self._get_conversation(connection, conversation_id)
            if conversation["parent_id"] is not None:
                return []
            rows = connection.execute("""
                SELECT e.*,r.consumed_turn_id,r.created_at AS consumed_at
                FROM shared_entries e LEFT JOIN shared_receipts r
                    ON r.entry_id=e.id AND r.conversation_id=?
                WHERE (e.scope='persistent' OR e.meal_id=?) AND (? OR e.active=1)
                ORDER BY e.id
            """, (conversation_id, conversation["meal_id"], int(bool(include_inactive))))
            result = []
            for row in rows:
                item = dict(row)
                item["active"] = bool(item["active"])
                item["consumed"] = item["consumed_at"] is not None
                item["eligible"] = self._eligible_entry(row, conversation)
                item["pending"] = item["active"] and item["eligible"] and not item["consumed"]
                result.append(item)
            return result

    def set_shared_entry_active(self, entry_id, active):
        if type(entry_id) is not int or type(active) is not bool:
            raise ValueError("共享条目 ID 或启用标记无效。")
        with self._connect(write=True) as connection:
            cursor = connection.execute("UPDATE shared_entries SET active=? WHERE id=?", (int(active), entry_id))
            if cursor.rowcount != 1:
                raise ValueError("共享条目不存在。")

    def edit_shared_entry(self, entry_id, content):
        """Retire the old snapshot and publish a fresh, unconsumed manual entry."""
        if type(entry_id) is not int or not isinstance(content, str) or not content.strip():
            raise ValueError("共享条目 ID 或正文无效。")
        with self._connect(write=True) as connection:
            row = connection.execute("SELECT * FROM shared_entries WHERE id=?", (entry_id,)).fetchone()
            if row is None:
                raise ValueError("共享条目不存在。")
            connection.execute("UPDATE shared_entries SET active=0 WHERE id=?", (entry_id,))
            # No publisher exclusion or inherited receipt: all scenes in scope
            # need to receive the user's correction, including its original source.
            cursor = connection.execute("""
                INSERT INTO shared_entries(scope,meal_id,content,source_day,created_at)
                VALUES (?,?,?,?,?)
            """, (row["scope"], row["meal_id"], content, row["source_day"], _timestamp()))
            return cursor.lastrowid

    def delete_shared_entry(self, entry_id):
        """Stop future delivery without rewriting old requests or C/R/U."""
        self.set_shared_entry_active(entry_id, False)

    @classmethod
    def _check_shared_delivery(cls, connection, conversation, snapshot, *, completing=False):
        meta = snapshot.get("context_meta")
        if not isinstance(meta, dict) or "shared_delivery" not in meta:
            return []
        delivery = meta["shared_delivery"]
        if conversation["parent_id"] is not None:
            if delivery is not None:
                raise ValueError("分支不能消费共享信息。")
            return []
        if (not isinstance(delivery, dict) or not isinstance(delivery.get("items"), list)
                or delivery.get("day") != cls._day_date(connection, conversation["meal_id"])
                or type(delivery.get("day_scope")) is not bool
                or delivery["day_scope"] != (conversation["meal_id"] is not None)):
            raise ValueError("共享传递快照格式或范围无效。")
        consumed_ids = []
        for item in delivery["items"]:
            if (not isinstance(item, dict) or type(item.get("id")) is not int
                    or item["id"] in consumed_ids):
                raise ValueError("共享传递条目无效或重复。")
            row = connection.execute("SELECT * FROM shared_entries WHERE id=?", (item["id"],)).fetchone()
            if (row is None or cls._entry_item(row) != item or not cls._eligible_entry(row, conversation)
                    or (not completing and not row["active"])):
                raise RuntimeError("共享条目已变化或不属于当前会话，请重新预览。")
            if connection.execute(
                "SELECT 1 FROM shared_receipts WHERE entry_id=? AND conversation_id=?",
                (item["id"], conversation["id"]),
            ).fetchone():
                raise RuntimeError("共享条目已消费，请重新预览。")
            consumed_ids.append(item["id"])
        return consumed_ids

    @classmethod
    def _complete_shared_delivery(cls, connection, conversation, turn_id, snapshot, additions):
        ids = cls._check_shared_delivery(connection, conversation, snapshot, completing=True)
        if additions and conversation["parent_id"] is not None:
            raise ValueError("分支不能发布共享信息。")
        if any(item["scope"] == "day" for item in additions) and conversation["meal_id"] is None:
            raise ValueError("没有日期分组的会话不能发布当日共享信息。")
        timestamp = _timestamp()
        connection.executemany(
            "INSERT INTO shared_receipts(entry_id,conversation_id,consumed_turn_id,created_at) VALUES (?,?,?,?)",
            [(entry_id, conversation["id"], turn_id, timestamp) for entry_id in ids],
        )
        for item in additions:
            # Preserve the pre-existing exact-text guard for model additions.
            # The surrounding write transaction also serializes concurrent writers.
            meal_id = conversation["meal_id"] if item["scope"] == "day" else None
            if connection.execute("""
                SELECT 1 FROM shared_entries
                WHERE active=1 AND scope=? AND meal_id IS ? AND content=? LIMIT 1
            """, (item["scope"], meal_id, item["content"])).fetchone():
                continue
            connection.execute("""
                INSERT OR IGNORE INTO shared_entries
                    (scope,meal_id,content,source_conversation_id,source_scene,source_turn_id,source_day,created_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (item["scope"], conversation["meal_id"] if item["scope"] == "day" else None,
                  item["content"], conversation["id"], conversation["scene"], turn_id,
                  cls._day_date(connection, conversation["meal_id"]), timestamp))
