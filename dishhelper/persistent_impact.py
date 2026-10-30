"""Local maximum impact, partitioned by successful maintenance generations."""

LEVELS = {None: 0, "low": 1, "medium": 2, "high": 3}


def initialize_impact(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS persistent_maintenance_state (
        id INTEGER PRIMARY KEY CHECK(id=1),
        generation INTEGER NOT NULL CHECK(generation>=0),
        level TEXT CHECK(level IN ('low','medium','high')),
        dismissed_level TEXT CHECK(dismissed_level IN ('low','medium','high'))
    )''')


def read_impact(connection):
    row = connection.execute(
        "SELECT generation,level,dismissed_level FROM persistent_maintenance_state WHERE id=1").fetchone()
    return dict(row) if row else {"generation": 0, "level": None, "dismissed_level": None}


def _write(connection, state):
    connection.execute('''INSERT INTO persistent_maintenance_state VALUES (1,?,?,?)
        ON CONFLICT(id) DO UPDATE SET generation=excluded.generation,
        level=excluded.level,dismissed_level=excluded.dismissed_level''',
        (state["generation"], state["level"], state["dismissed_level"]))


def record_impact(connection, level, generation):
    # Only validated observations tied to the request's local generation count.
    if not isinstance(level, str) or level not in LEVELS or type(generation) is not int:
        return
    state = read_impact(connection)
    if generation != state["generation"] or LEVELS[level] <= LEVELS[state["level"]]:
        return
    state["level"] = level
    _write(connection, state)


def impact_signal(state):
    return f"{state['generation']}:{state['level']}"


def dismiss_impact(connection, signal_key):
    state = read_impact(connection)
    if state["level"] in ("medium", "high") and signal_key == impact_signal(state):
        state["dismissed_level"] = state["level"]
        _write(connection, state)


def reset_impact(connection):
    """Call inside the same transaction that saves maintenance changes."""
    before = read_impact(connection)
    _write(connection, {"generation": before["generation"] + 1,
                        "level": None, "dismissed_level": None})
    return before


def restore_impact(connection, before):
    level = before.get("level")
    state = read_impact(connection)
    if isinstance(level, str) and level in LEVELS and LEVELS[level] > LEVELS[state["level"]]:
        state["level"] = level
        # A restored issue is a fresh reason to show the current generation's cue.
        state["dismissed_level"] = None
        _write(connection, state)
