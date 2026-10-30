"""Compatibility tables only. No search runtime or tool dependencies."""

def initialize_search(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS search_config (
            key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS search_tasks (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK(status IN ('pending','prepared','waiting_login','succeeded','failed','cancelled')),
            request_json TEXT NOT NULL,
            query TEXT NOT NULL,
            sort TEXT NOT NULL,
            results_json TEXT NOT NULL DEFAULT '{}',
            errors_json TEXT NOT NULL DEFAULT '{}',
            preparation_json TEXT NOT NULL DEFAULT '{}',
            login_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_pending_search_per_conversation
            ON search_tasks(conversation_id) WHERE status='pending';
    """)

