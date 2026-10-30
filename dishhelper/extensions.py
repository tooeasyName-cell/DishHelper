"""Optional per-turn observations and persistent, local-only display choices."""
import json
from datetime import datetime, timezone


class ExtensionsMixin:
    @staticmethod
    def _initialize_extensions(connection):
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS turn_extensions (
                turn_id TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS context_displays (
                conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
                selection_json TEXT NOT NULL,
                updated_turn_id TEXT REFERENCES turns(id) ON DELETE SET NULL,
                updated_at TEXT NOT NULL
            );
        """)

    @staticmethod
    def _save_extensions(connection, conversation_id, turn_id, parsed):
        from .protocol import normalize_context_display, normalize_search_proposal, normalize_persistent_impact, normalize_long_reference
        parsed = parsed if isinstance(parsed, dict) else {}
        payload = {}
        errors = dict(parsed.get("extension_errors") or {})
        for key, validator in (("context_display", normalize_context_display),
                               ("search_proposal", normalize_search_proposal),
                               ("persistent_impact", normalize_persistent_impact),
                               ("long_reference", normalize_long_reference)):
            if key not in parsed:
                continue
            try:
                payload[key] = validator(parsed[key])
            except (ValueError, TypeError):
                errors[key] = f"{key} 格式无效，已忽略。"
        if errors:
            payload["extension_errors"] = errors
        connection.execute("INSERT INTO turn_extensions VALUES (?,?,?)",
            (turn_id, conversation_id, json.dumps(payload, ensure_ascii=False)))
        if "context_display" in payload:
            connection.execute("""INSERT INTO context_displays VALUES (?,?,?,?)
                ON CONFLICT(conversation_id) DO UPDATE SET selection_json=excluded.selection_json,
                updated_turn_id=excluded.updated_turn_id,updated_at=excluded.updated_at""",
                (conversation_id, json.dumps(payload["context_display"], ensure_ascii=False), turn_id,
                 datetime.now(timezone.utc).isoformat()))
        return payload

    def get_turn_extensions(self, turn_id):
        with self._connect() as connection:
            row = connection.execute("SELECT payload_json FROM turn_extensions WHERE turn_id=?", (turn_id,)).fetchone()
            return json.loads(row[0]) if row else {}

    def latest_extensions(self, conversation_id):
        with self._connect() as connection:
            row = connection.execute("""SELECT e.payload_json FROM turns t
                LEFT JOIN turn_extensions e ON e.turn_id=t.id
                WHERE t.conversation_id=? AND t.status='succeeded'
                ORDER BY t.created_at DESC,t.rowid DESC LIMIT 1""", (conversation_id,)).fetchone()
            return json.loads(row[0]) if row and row[0] else {}

    def get_long_reference_marks(self, conversation_id):
        """One latest target per chat, selected only from saved model marks.

        False/missing marks do not revoke a previous target. Read-time status
        checks also exclude failed/interrupted calls and uncommitted results.
        The payload is checked as a literal bool so legacy numeric data cannot
        become a mark by truthiness.
        """
        with self._connect() as connection:
            self._get_conversation(connection, conversation_id)
            rows = connection.execute("""SELECT t.id,e.payload_json FROM turns t
                JOIN calls c ON c.id=t.id
                JOIN turn_extensions e ON e.turn_id=t.id
                WHERE t.conversation_id=? AND t.status='succeeded' AND c.status='succeeded'
                ORDER BY t.created_at,t.rowid""", (conversation_id,))
            marked = [row["id"] for row in rows
                      if json.loads(row["payload_json"]).get("long_reference") is True]
            return {"latest_turn_id": marked[-1] if marked else None, "marked_turn_ids": marked}

    def get_context_display(self, conversation_id):
        with self._connect() as connection:
            conversation = self._get_conversation(connection, conversation_id)
            row = connection.execute("SELECT selection_json FROM context_displays WHERE conversation_id=?",
                                     (conversation_id,)).fetchone()
            selection = json.loads(row[0]) if row else []
            return self._resolve_display(selection, conversation)

    @staticmethod
    def _resolve_display(selection, state):
        values = {"C": state.get("conditions", ""), "U": state.get("carry_note", ""),
                  "R": (state.get("reference") or {}).get("content", "")}
        return [{"label": item["label"], "content": item.get("content", values.get(item["label"], ""))}
                for item in selection if item.get("content", values.get(item["label"], "")).strip()]

    @staticmethod
    def _display_sources(cards, previous, turn_id):
        # Match exact visible cards, including duplicates, before assigning a new
        # source. Reordering or repeating an unchanged selection renews no link.
        available = list(previous)
        sourced, changed = [], []
        for index, card in enumerate(cards):
            match = next((old for old in available if old["label"] == card["label"]
                          and old["content"] == card["content"]), None)
            if match is not None:
                available.remove(match)
                sourced.append({**card, "source_turn_id": match["source_turn_id"],
                                "source_index": match["source_index"]})
            else:
                sourced.append({**card, "source_turn_id": turn_id, "source_index": index})
                changed.append({**card, "source_turn_id": turn_id, "source_index": index})
        return sourced, changed

    def get_context_display_history(self, conversation_id):
        """Resolve navigation sources from committed snapshots, never model work.

        Historical notes use that turn's after-state. Current C/R/U references
        remain live; manual corrections absent from a model turn use a tail-note
        source (None) rather than attributing new text to an older model answer.
        """
        with self._connect() as connection:
            connection.execute("BEGIN")
            conversation = self._get_conversation(connection, conversation_id)
            row = connection.execute("SELECT selection_json FROM context_displays WHERE conversation_id=?",
                                     (conversation_id,)).fetchone()
            current_selection = json.loads(row[0]) if row else []
            rows = connection.execute("""SELECT t.id,c.after_state_json,e.payload_json FROM turns t
                JOIN calls c ON c.id=t.id
                LEFT JOIN turn_extensions e ON e.turn_id=t.id
                WHERE t.conversation_id=? AND t.status='succeeded' AND c.status='succeeded'
                ORDER BY t.created_at,t.rowid""", (conversation_id,)).fetchall()
            selection, previous, updates, changed_turns, cleared_turns = [], [], {}, [], []
            for row in rows:
                payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
                explicit = payload.get("context_display")
                if "context_display" in payload:
                    selection = explicit
                state = json.loads(row["after_state_json"]) if row["after_state_json"] else {}
                visible = self._resolve_display(selection, state)
                sourced, changed = self._display_sources(visible, previous, row["id"])
                before_visible = [{"label": card["label"], "content": card["content"]} for card in previous]
                if visible != before_visible or explicit == []:
                    changed_turns.append(row["id"])
                if changed:
                    updates[row["id"]] = changed
                if explicit == [] or (previous and not visible):
                    cleared_turns.append(row["id"])
                previous = sourced
            current = self._resolve_display(current_selection, conversation)
            cards, _ = self._display_sources(current, previous, None)
            return {"cards": cards, "updates": updates,
                    "changed_turn_ids": changed_turns, "cleared_turn_ids": cleared_turns}
