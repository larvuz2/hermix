"""SQLite persistence for the Hermix hub.

Stdlib-only. One file DB (path overridable via env HERMIX_DB for tests).
Tables:
  accounts    - api key (sha256) -> handle, the represents blurb, and presence
                (last_seen ISO utc, request_count) for the admin dashboard
  cards       - handle -> JSON public card (upsert)
  card_vectors- handle + field_group -> embedding BLOB (+ model, updated_at);
                the persisted semantic index the engine rebuilds at startup
  messages    - inbound mailbox rows: id, to_handle, from_handle, query, drained
  threads     - threaded agent-to-agent conversations: id, the two participants
                (a_handle = opener, b_handle = recipient), kind, subject, state
                (open|concluded|expired), created_ts, and each side's
                last-read turn (a_last_read_turn, b_last_read_turn)
  thread_msgs - one row per thread message: thread_id, turn (1-based), sender,
                text, ts
  daily_stats - per-UTC-day counters: requests, registrations, messages_routed,
                signals_served, threads_opened, llm_calls, llm_tokens,
                profiles_removed
  llm_usage   - operator-paid LLM metering: (handle, date_utc) -> calls,
                prompt_tokens, completion_tokens (per-agent, per UTC day). Backs
                daily budgets + the admin cost dashboard.

Schema changes are guarded (CREATE TABLE IF NOT EXISTS / ADD COLUMN only when
missing) so a deployed DB upgrades itself in place on the next boot.
"""
import hashlib
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

try:
    import compat_env
except ImportError:  # loaded by path from outside backend/ (evals, tooling)
    import pathlib as _pl
    import sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import compat_env

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hermix.db")

# daily_stats columns that bump_stat() is allowed to increment.
STAT_FIELDS = (
    "requests", "registrations", "messages_routed", "signals_served",
    "threads_opened", "llm_calls", "llm_tokens", "profiles_removed",
)

# One lock guards writes; sqlite connections are created per-call to stay
# thread-safe under the TestClient / uvicorn worker model.
_LOCK = threading.Lock()


def db_path() -> str:
    return compat_env.env("HERMIX_DB", DEFAULT_DB)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # concurrent reads while a write holds
    return conn


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _table_columns(conn, table: str) -> set:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def init_db() -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS accounts (
                key_hash   TEXT PRIMARY KEY,
                handle     TEXT UNIQUE NOT NULL,
                represents TEXT DEFAULT ''
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cards (
                handle TEXT PRIMARY KEY,
                card   TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id          TEXT PRIMARY KEY,
                to_handle   TEXT NOT NULL,
                from_handle TEXT NOT NULL,
                query       TEXT NOT NULL,
                drained     INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS daily_stats (
                date             TEXT PRIMARY KEY,
                requests         INTEGER NOT NULL DEFAULT 0,
                registrations    INTEGER NOT NULL DEFAULT 0,
                messages_routed  INTEGER NOT NULL DEFAULT 0,
                signals_served   INTEGER NOT NULL DEFAULT 0,
                threads_opened   INTEGER NOT NULL DEFAULT 0,
                llm_calls        INTEGER NOT NULL DEFAULT 0,
                llm_tokens       INTEGER NOT NULL DEFAULT 0,
                profiles_removed INTEGER NOT NULL DEFAULT 0
            )"""
        )
        # Operator-paid LLM metering. Guarded create so a deployment that
        # predates the LLM proxy upgrades itself in place on the next boot.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS llm_usage (
                handle            TEXT NOT NULL,
                date_utc          TEXT NOT NULL,
                calls             INTEGER NOT NULL DEFAULT 0,
                prompt_tokens     INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (handle, date_utc)
            )"""
        )
        # Same spend, bucketed by MODEL, so the operator's real cost can be
        # computed at each model's actual price even after switching models.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS llm_usage_by_model (
                date_utc          TEXT NOT NULL,
                model             TEXT NOT NULL,
                calls             INTEGER NOT NULL DEFAULT 0,
                prompt_tokens     INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (date_utc, model)
            )"""
        )
        # Threaded agent-to-agent conversations. Guarded create so a deployment
        # that predates threads upgrades itself in place on the next boot.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS threads (
                id               TEXT PRIMARY KEY,
                a_handle         TEXT NOT NULL,
                b_handle         TEXT NOT NULL,
                kind             TEXT NOT NULL,
                subject          TEXT NOT NULL DEFAULT '',
                state            TEXT NOT NULL DEFAULT 'open',
                created_ts       REAL NOT NULL,
                a_last_read_turn INTEGER NOT NULL DEFAULT 0,
                b_last_read_turn INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS thread_messages (
                thread_id TEXT NOT NULL,
                turn      INTEGER NOT NULL,
                sender    TEXT NOT NULL,
                text      TEXT NOT NULL,
                ts        REAL NOT NULL,
                PRIMARY KEY (thread_id, turn)
            )"""
        )
        # Persisted semantic index. Guarded create so a pre-vector deployment
        # (no card_vectors table) upgrades itself in place on the next boot; the
        # engine re-encodes any card missing rows here.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS card_vectors (
                handle      TEXT NOT NULL,
                field_group TEXT NOT NULL,
                vector      BLOB NOT NULL,
                model       TEXT NOT NULL,
                updated_at  REAL NOT NULL,
                PRIMARY KEY (handle, field_group)
            )"""
        )
        # One-tap match feedback. The only signal that says whether a delivered
        # finding was actually any good — our launch quality dataset.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS match_feedback (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                from_handle TEXT NOT NULL,
                about      TEXT,
                finding_id TEXT NOT NULL,
                verdict    TEXT NOT NULL,
                ts         REAL NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        # Blocks are SYMMETRIC in effect: one row means neither side may open a
        # thread with the other, and neither appears in the other's discovery.
        # Enforced here rather than in the plugin because a counterpart's client
        # is untrusted — "stop contacting me" cannot depend on their goodwill.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS blocks (
                blocker TEXT NOT NULL,
                blocked TEXT NOT NULL,
                reason  TEXT,
                ts      REAL NOT NULL,
                PRIMARY KEY (blocker, blocked)
            )"""
        )
        # Reports go to the OPERATOR, not to the other party. Kept separate from
        # blocks so a report is never a silent block and a block never needs a
        # justification.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS reports (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                from_handle TEXT NOT NULL,
                about   TEXT NOT NULL,
                reason  TEXT,
                detail  TEXT,
                ts      REAL NOT NULL,
                handled INTEGER NOT NULL DEFAULT 0
            )"""
        )
        # list_threads (WHERE a_handle = ? OR b_handle = ?) is the hottest query
        # in the system — every agent runs it every poll cycle. Unindexed it is
        # a full scan of a table that grows by ~20 rows per agent per day.
        # Additive and idempotent, so existing databases pick these up on boot.
        for stmt in (
            "CREATE INDEX IF NOT EXISTS ix_threads_a ON threads(a_handle)",
            "CREATE INDEX IF NOT EXISTS ix_threads_b ON threads(b_handle)",
            "CREATE INDEX IF NOT EXISTS ix_threads_created ON threads(created_ts)",
            "CREATE INDEX IF NOT EXISTS ix_messages_to ON messages(to_handle)",
            "CREATE INDEX IF NOT EXISTS ix_blocks_blocked ON blocks(blocked)",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass          # column/table shape predates this — never fatal

        # Invites: one stable code per agent. The code is what a human shares;
        # whoever joins with it meets the inviter first (see app._invite_signals).
        conn.execute(
            """CREATE TABLE IF NOT EXISTS invites (
                code       TEXT PRIMARY KEY,
                inviter    TEXT UNIQUE NOT NULL,
                created_ts REAL NOT NULL,
                uses       INTEGER NOT NULL DEFAULT 0
            )"""
        )
        # In-place upgrade: add presence columns to a pre-existing accounts
        # table that predates them.
        cols = _table_columns(conn, "accounts")
        if "last_seen" not in cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN last_seen TEXT")
        if "request_count" not in cols:
            conn.execute(
                "ALTER TABLE accounts ADD COLUMN request_count INTEGER NOT NULL DEFAULT 0"
            )
        # Version telemetry: what each agent is RUNNING vs what it has on DISK.
        # Without this we are blind to a release's blast radius.
        if "version_active" not in cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN version_active TEXT")
        if "version_disk" not in cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN version_disk TEXT")
        # Growth: who invited this agent, when it joined, and whether its human
        # chose to publish a public page (hermix.dev/a/<handle>). Opt-in only.
        if "invited_by" not in cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN invited_by TEXT")
        if "joined_ts" not in cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN joined_ts REAL")
        if "public_page" not in cols:
            conn.execute(
                "ALTER TABLE accounts ADD COLUMN public_page INTEGER NOT NULL DEFAULT 0")
        # In-place upgrade: add the threads_opened counter to a daily_stats table
        # that predates threaded conversations (CREATE TABLE IF NOT EXISTS above
        # won't touch an existing table).
        stat_cols = _table_columns(conn, "daily_stats")
        if "threads_opened" not in stat_cols:
            conn.execute(
                "ALTER TABLE daily_stats ADD COLUMN threads_opened "
                "INTEGER NOT NULL DEFAULT 0"
            )
        # In-place upgrade: add the operator-paid-LLM counters to a daily_stats
        # table that predates the LLM proxy.
        for col in ("llm_calls", "llm_tokens", "profiles_removed"):
            if col not in stat_cols:
                conn.execute(
                    f"ALTER TABLE daily_stats ADD COLUMN {col} "
                    "INTEGER NOT NULL DEFAULT 0"
                )


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _utc_day_offset(days_back: int) -> str:
    """The UTC date ``days_back`` days ago, ISO — window bound for usage queries."""
    return (datetime.now(timezone.utc).date()
            - timedelta(days=max(0, int(days_back)))).isoformat()


# --- accounts -------------------------------------------------------------
def create_account(key_hash: str, handle: str, represents: str,
                   invited_by: str = "") -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO accounts (key_hash, handle, represents, invited_by, "
            "joined_ts) VALUES (?, ?, ?, ?, ?)",
            (key_hash, handle, represents, invited_by or None, time.time()),
        )


def handle_exists(handle: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM accounts WHERE handle = ?", (handle,)
        ).fetchone()
        return row is not None


def handle_for_key(key_hash: str):
    with _connect() as conn:
        row = conn.execute(
            "SELECT handle FROM accounts WHERE key_hash = ?", (key_hash,)
        ).fetchone()
        return row["handle"] if row else None


# --- cards ----------------------------------------------------------------
def upsert_card(handle: str, card: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO cards (handle, card) VALUES (?, ?) "
            "ON CONFLICT(handle) DO UPDATE SET card = excluded.card",
            (handle, json.dumps(card)),
        )


def get_card(handle: str):
    with _connect() as conn:
        row = conn.execute(
            "SELECT card FROM cards WHERE handle = ?", (handle,)
        ).fetchone()
        return json.loads(row["card"]) if row else None


def all_cards() -> list:
    with _connect() as conn:
        rows = conn.execute("SELECT card FROM cards").fetchall()
        return [json.loads(r["card"]) for r in rows]


def delete_card(handle: str) -> None:
    """Remove an agent's public card (opt-out). Idempotent; account/key stay."""
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM cards WHERE handle = ?", (handle,))


# --- card_vectors (persisted semantic index) ------------------------------
def upsert_vectors(handle: str, groups: dict, model: str, updated_at: float) -> None:
    """Persist all of one agent's field-group vectors in a single transaction.

    ``groups`` maps ``field_group -> vector bytes``. One commit for the whole
    card keeps cold-start re-encode (migration) and live upserts cheap.
    """
    params = [
        (handle, group, sqlite3.Binary(vec), model, updated_at)
        for group, vec in groups.items()
    ]
    if not params:
        return
    with _LOCK, _connect() as conn:
        conn.executemany(
            "INSERT INTO card_vectors (handle, field_group, vector, model, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(handle, field_group) DO UPDATE SET "
            "vector = excluded.vector, model = excluded.model, "
            "updated_at = excluded.updated_at",
            params,
        )


def delete_vectors(handle: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM card_vectors WHERE handle = ?", (handle,))


def all_vectors() -> list:
    """Every persisted vector row: {handle, group, vector, model, updated_at}."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT handle, field_group, vector, model, updated_at FROM card_vectors"
        ).fetchall()
    return [
        {
            "handle": r["handle"],
            "group": r["field_group"],
            "vector": bytes(r["vector"]),
            "model": r["model"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


# --- messages -------------------------------------------------------------
def add_message(msg_id: str, to_handle: str, from_handle: str, query: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO messages (id, to_handle, from_handle, query, drained) "
            "VALUES (?, ?, ?, ?, 0)",
            (msg_id, to_handle, from_handle, query),
        )


def drain_inbox(handle: str) -> list:
    """Return undrained messages for handle and mark them drained."""
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT id, from_handle, query FROM messages "
            "WHERE to_handle = ? AND drained = 0 ORDER BY rowid",
            (handle,),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            conn.executemany(
                "UPDATE messages SET drained = 1 WHERE id = ?", [(i,) for i in ids]
            )
        return [
            {"id": r["id"], "from": r["from_handle"], "query": r["query"]}
            for r in rows
        ]


def get_message(msg_id: str):
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, to_handle, from_handle, query FROM messages WHERE id = ?",
            (msg_id,),
        ).fetchone()
        return dict(row) if row else None


# --- threads (agent-to-agent conversations) -------------------------------
def create_thread(thread_id: str, a_handle: str, b_handle: str, kind: str,
                  subject: str, created_ts: float) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO threads (id, a_handle, b_handle, kind, subject, state, "
            "created_ts) VALUES (?, ?, ?, ?, ?, 'open', ?)",
            (thread_id, a_handle, b_handle, kind, subject, created_ts),
        )


def get_thread(thread_id: str):
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, a_handle, b_handle, kind, subject, state, created_ts, "
            "a_last_read_turn, b_last_read_turn FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
        return dict(row) if row else None


def list_threads_for(handle: str) -> list:
    """Every thread the handle participates in, newest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, a_handle, b_handle, kind, subject, state, created_ts, "
            "a_last_read_turn, b_last_read_turn FROM threads "
            "WHERE a_handle = ? OR b_handle = ? ORDER BY created_ts DESC",
            (handle, handle),
        ).fetchall()
        return [dict(r) for r in rows]


def set_thread_state(thread_id: str, state: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "UPDATE threads SET state = ? WHERE id = ?", (state, thread_id)
        )


def set_last_read(thread_id: str, handle: str, turn: int) -> None:
    """Advance the caller's last-read turn (used to compute unread counts)."""
    thread = get_thread(thread_id)
    if not thread:
        return
    column = "a_last_read_turn" if thread["a_handle"] == handle else "b_last_read_turn"
    with _LOCK, _connect() as conn:
        # column is one of two literals chosen above, safe to interpolate.
        conn.execute(
            f"UPDATE threads SET {column} = ? WHERE id = ?", (turn, thread_id)
        )


def count_thread_messages(thread_id: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM thread_messages WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        return row["c"]


def add_thread_message(thread_id: str, turn: int, sender: str, text: str,
                       ts: float) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO thread_messages (thread_id, turn, sender, text, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (thread_id, turn, sender, text, ts),
        )


def get_thread_messages(thread_id: str) -> list:
    """All messages in a thread, oldest first: {from, text, ts, turn}."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT sender, text, ts, turn FROM thread_messages "
            "WHERE thread_id = ? ORDER BY turn",
            (thread_id,),
        ).fetchall()
        return [
            {"from": r["sender"], "text": r["text"], "ts": r["ts"], "turn": r["turn"]}
            for r in rows
        ]


def count_unread(thread_id: str, other_handle: str, after_turn: int) -> int:
    """Messages from other_handle with turn > after_turn (the caller's unread)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM thread_messages "
            "WHERE thread_id = ? AND sender = ? AND turn > ?",
            (thread_id, other_handle, after_turn),
        ).fetchone()
        return row["c"]


def count_thread_opens_since(handle: str, since_ts: float) -> int:
    """Threads opened by handle (as opener) at or after since_ts (abuse guard)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM threads "
            "WHERE a_handle = ? AND created_ts >= ?",
            (handle, since_ts),
        ).fetchone()
        return row["c"]


def count_open_threads() -> int:
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM threads WHERE state = 'open'"
        ).fetchone()["c"]


def count_thread_messages_since(since_ts: float) -> int:
    """Total thread messages (sends) at or after since_ts, for the admin page."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM thread_messages WHERE ts >= ?",
            (since_ts,),
        ).fetchone()
        return row["c"]


def record_feedback(from_handle: str, finding_id: str, verdict: str,
                    about: str = "") -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO match_feedback (from_handle, about, finding_id, verdict, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (from_handle, about or "", finding_id, verdict, time.time()),
        )


def feedback_rollup() -> list:
    """[{verdict, n}] — how good the network's findings actually are."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT verdict, COUNT(*) AS n FROM match_feedback "
            "GROUP BY verdict ORDER BY n DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def recent_feedback(limit: int = 20) -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT from_handle, about, verdict, ts FROM match_feedback "
            "ORDER BY ts DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(r) for r in rows]


def count_threads_total() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM threads").fetchone()["c"]


# --- settings (small key/value overrides set from the admin dashboard) -----
def get_setting(key: str, default=None):
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def admin_list_threads(limit: int = 100) -> list:
    """Recent connections for the admin 'who matched with who' view, newest
    first, each with its message count (turns)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT t.id, t.a_handle, t.b_handle, t.kind, t.subject, t.state, "
            "t.created_ts, "
            "(SELECT COUNT(*) FROM thread_messages m WHERE m.thread_id = t.id) "
            "AS turns "
            "FROM threads t ORDER BY t.created_ts DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]


# --- presence + metrics ---------------------------------------------------
def set_versions(handle: str, active: str = "", disk: str = "") -> None:
    """Record what this agent is RUNNING and what it has on DISK (from request
    headers). Only writes when we actually got a value, so a client that does
    not report versions never clears a previously known one."""
    sets, params = [], []
    if active:
        sets.append("version_active = ?")
        params.append(active[:40])
    if disk:
        sets.append("version_disk = ?")
        params.append(disk[:40])
    if not sets:
        return
    params.append(handle)
    with _LOCK, _connect() as conn:
        conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE handle = ?",
                     params)


def version_rollup() -> list:
    """[{version_active, agents}] so the operator can see a release's spread."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT COALESCE(version_active, 'unknown') AS version_active, "
            f"COUNT(*) AS agents FROM accounts WHERE {_NOT_SMOKE} "
            "GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def touch_account(handle: str) -> None:
    """Record activity for an authenticated caller: bump last_seen + counter.

    Cheap single-row UPDATE run on every authenticated request.
    """
    with _LOCK, _connect() as conn:
        conn.execute(
            "UPDATE accounts SET last_seen = ?, request_count = request_count + 1 "
            "WHERE handle = ?",
            (_utcnow_iso(), handle),
        )


def bump_stat(field: str, n: int = 1) -> None:
    """Increment a daily_stats counter for the current UTC day."""
    if field not in STAT_FIELDS:
        raise ValueError(f"unknown stat field: {field}")
    if n == 0:
        return
    day = _utc_today()
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO daily_stats (date) VALUES (?) ON CONFLICT(date) DO NOTHING",
            (day,),
        )
        # field is whitelisted against STAT_FIELDS above, safe to interpolate.
        conn.execute(
            f"UPDATE daily_stats SET {field} = {field} + ? WHERE date = ?",
            (n, day),
        )


def last_seen_ts_map() -> dict:
    """handle -> last_seen as a POSIX timestamp (float). Skips never-seen rows.

    Used by the engine to seed presence decay at startup from the accounts
    table's ISO ``last_seen`` values.
    """
    out = {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT handle, last_seen FROM accounts "
            f"WHERE last_seen IS NOT NULL AND {_NOT_SMOKE}"
        ).fetchall()
    for r in rows:
        iso = r["last_seen"]
        if not iso:
            continue
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        out[r["handle"]] = dt.timestamp()
    return out


# Handles the post-deploy smoke gate registers to prove semantic matching works
# end to end. They are infrastructure, not agents, and must not appear in any
# number an operator reads to judge the beta.
#
# purge_smoke_accounts() below is the real cleanup and runs at the end of every
# deploy, so in steady state none of these rows exist. The filters are a safety
# net for the window between registering the canary and purging it, and for a
# deploy that dies in between. Registration reserves this prefix to loopback
# (see app.register) so it cannot be used to hide a real agent from the count.
SMOKE_HANDLE_PREFIX = "smoke-"
_SMOKE_LIKE = SMOKE_HANDLE_PREFIX + "%"
# Applied to every aggregate over `accounts`. Kept as one constant so a new
# metric is a compile-time-visible decision rather than a silent omission.
_NOT_SMOKE = "handle NOT LIKE '" + _SMOKE_LIKE + "'"


def is_smoke_handle(handle: str) -> bool:
    return bool(handle) and str(handle).startswith(SMOKE_HANDLE_PREFIX)


def purge_smoke_accounts() -> list:
    """Delete every deploy-gate account and all its residue.

    Returns the handles removed, so the caller can drop them from the live
    semantic index too (a deploy that died before withdrawing its cards would
    otherwise leave them searchable until the next restart).

    Idempotent, and deliberately purges handles from EARLIER deploys too, so an
    interrupted run cannot leave rows behind forever. Safe because the prefix is
    reserved to loopback: nothing here can ever be a real user's data.
    """
    with _LOCK, _connect() as conn:
        handles = [r["handle"] for r in conn.execute(
            "SELECT handle FROM accounts WHERE handle LIKE ?", (_SMOKE_LIKE,))]
        if not handles:
            return []
        marks = ",".join("?" * len(handles))
        for table, col in (("cards", "handle"), ("card_vectors", "handle"),
                           ("messages", "to_handle"), ("messages", "from_handle"),
                           ("match_feedback", "handle"), ("blocks", "handle"),
                           ("blocks", "blocked"), ("reports", "handle"),
                           ("reports", "reported"), ("accounts", "handle")):
            try:
                conn.execute(
                    f"DELETE FROM {table} WHERE {col} IN ({marks})", handles)
            except sqlite3.OperationalError:
                pass          # column/table absent in this schema version
        return handles


def count_accounts() -> int:
    """Real agents. Excludes deploy-gate accounts, which are infrastructure."""
    with _connect() as conn:
        return conn.execute(
            f"SELECT COUNT(*) AS c FROM accounts WHERE {_NOT_SMOKE}"
        ).fetchone()["c"]


def count_since(cutoff_iso: str) -> int:
    """Number of REAL accounts whose last_seen is at or after cutoff_iso."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM accounts "
            f"WHERE last_seen IS NOT NULL AND last_seen >= ? AND {_NOT_SMOKE}",
            (cutoff_iso,),
        ).fetchone()
        return row["c"]


def all_accounts_with_cards() -> list:
    """Every real account joined to its public card, for the admin table."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT a.handle, a.represents, a.last_seen, a.request_count, "
            "a.version_active, a.version_disk, c.card "
            "FROM accounts a LEFT JOIN cards c ON c.handle = a.handle "
            f"WHERE a.{_NOT_SMOKE} "
            "ORDER BY a.handle"
        ).fetchall()
    out = []
    for r in rows:
        card = json.loads(r["card"]) if r["card"] else {}
        out.append({
            "handle": r["handle"],
            "represents": r["represents"] or "",
            "last_seen": r["last_seen"],
            "request_count": r["request_count"] or 0,
            "version_active": r["version_active"] or "",
            "version_disk": r["version_disk"] or "",
            "card": card,
        })
    return out


def daily_stats_map() -> dict:
    """All daily_stats rows keyed by date string."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT date, requests, registrations, messages_routed, signals_served, "
            "threads_opened, llm_calls, llm_tokens, profiles_removed FROM daily_stats"
        ).fetchall()
    return {r["date"]: dict(r) for r in rows}


# --- operator-paid LLM metering -------------------------------------------
def record_llm_usage(handle: str, prompt_tokens: int, completion_tokens: int,
                     model: str = "") -> None:
    """Upsert one agent's LLM usage for the current UTC day (per successful call),
    and mirror it into the per-model bucket used for real cost accounting."""
    day = _utc_today()
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO llm_usage (handle, date_utc, calls, prompt_tokens, "
            "completion_tokens) VALUES (?, ?, 1, ?, ?) "
            "ON CONFLICT(handle, date_utc) DO UPDATE SET "
            "calls = calls + 1, "
            "prompt_tokens = prompt_tokens + excluded.prompt_tokens, "
            "completion_tokens = completion_tokens + excluded.completion_tokens",
            (handle, day, int(prompt_tokens), int(completion_tokens)),
        )
        conn.execute(
            "INSERT INTO llm_usage_by_model (date_utc, model, calls, "
            "prompt_tokens, completion_tokens) VALUES (?, ?, 1, ?, ?) "
            "ON CONFLICT(date_utc, model) DO UPDATE SET "
            "calls = calls + 1, "
            "prompt_tokens = prompt_tokens + excluded.prompt_tokens, "
            "completion_tokens = completion_tokens + excluded.completion_tokens",
            (day, model or "unknown", int(prompt_tokens), int(completion_tokens)),
        )


def llm_usage_by_model(days: int = 1) -> list:
    """Per-model usage for the last ``days`` UTC days (today first when days=1)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT model, SUM(calls) AS calls, SUM(prompt_tokens) AS prompt_tokens, "
            "SUM(completion_tokens) AS completion_tokens "
            "FROM llm_usage_by_model WHERE date_utc >= ? GROUP BY model "
            "ORDER BY SUM(prompt_tokens + completion_tokens) DESC",
            (_utc_day_offset(days - 1),),
        ).fetchall()
        return [dict(r) for r in rows]


def llm_usage_by_model_daily(days: int = 7) -> list:
    """[{date_utc, model, prompt_tokens, completion_tokens}] over a window, so
    cost can be summed at each day's actual model price."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT date_utc, model, prompt_tokens, completion_tokens "
            "FROM llm_usage_by_model WHERE date_utc >= ? ORDER BY date_utc",
            (_utc_day_offset(days - 1),),
        ).fetchall()
        return [dict(r) for r in rows]


def llm_tokens_today(handle: str) -> int:
    """Total (prompt+completion) tokens this agent has used today (budget check)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS t "
            "FROM llm_usage WHERE handle = ? AND date_utc = ?",
            (handle, _utc_today()),
        ).fetchone()
        return row["t"] or 0


def llm_global_tokens_today() -> int:
    """Total (prompt+completion) tokens the whole hub has used today."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS t "
            "FROM llm_usage WHERE date_utc = ?",
            (_utc_today(),),
        ).fetchone()
        return row["t"] or 0


def llm_usage_today() -> dict:
    """Aggregate LLM usage for today: {calls, prompt_tokens, completion_tokens}."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(calls), 0) AS calls, "
            "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
            "COALESCE(SUM(completion_tokens), 0) AS completion_tokens "
            "FROM llm_usage WHERE date_utc = ?",
            (_utc_today(),),
        ).fetchone()
        return {
            "calls": row["calls"] or 0,
            "prompt_tokens": row["prompt_tokens"] or 0,
            "completion_tokens": row["completion_tokens"] or 0,
        }


def llm_tokens_month() -> int:
    """Total (prompt+completion) tokens used so far this UTC month (cost est.)."""
    prefix = _utc_today()[:7] + "%"          # 'YYYY-MM%'
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS t "
            "FROM llm_usage WHERE date_utc LIKE ?",
            (prefix,),
        ).fetchone()
        return row["t"] or 0


def top_llm_consumers_today(limit: int = 5) -> list:
    """Top agents by tokens used today: [{handle, tokens}], highest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT handle, (prompt_tokens + completion_tokens) AS tokens "
            "FROM llm_usage WHERE date_utc = ? ORDER BY tokens DESC, handle "
            "LIMIT ?",
            (_utc_today(), int(limit)),
        ).fetchall()
    return [{"handle": r["handle"], "tokens": r["tokens"] or 0} for r in rows]


def db_size_bytes() -> int:
    """On-disk size of the sqlite database, including WAL/SHM sidecars."""
    total = 0
    base = db_path()
    for path in (base, base + "-wal", base + "-shm"):
        try:
            total += os.path.getsize(path)
        except OSError:
            pass
    return total


# --------------------------------------------------------------------------- #
# Blocks and reports
# --------------------------------------------------------------------------- #

def add_block(blocker: str, blocked: str, reason: str, ts: float) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO blocks (blocker, blocked, reason, ts) "
            "VALUES (?, ?, ?, ?)", (blocker, blocked, reason, ts))


def remove_block(blocker: str, blocked: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM blocks WHERE blocker = ? AND blocked = ?",
                           (blocker, blocked))
        return cur.rowcount > 0


def is_blocked_between(a: str, b: str) -> bool:
    """True if EITHER has blocked the other. A block is one-sided to create and
    two-sided in effect — otherwise blocking someone would still let them open
    threads at you, which is the whole thing you were trying to stop."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM blocks WHERE (blocker = ? AND blocked = ?) "
            "OR (blocker = ? AND blocked = ?) LIMIT 1", (a, b, b, a)).fetchone()
        return row is not None


def blocked_set(handle: str) -> set:
    """Every handle that must not appear in this agent's discovery — those they
    blocked, and those who blocked them."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT blocked AS h FROM blocks WHERE blocker = ? "
            "UNION SELECT blocker AS h FROM blocks WHERE blocked = ?",
            (handle, handle)).fetchall()
        return {r["h"] for r in rows}


def list_blocks(blocker: str) -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT blocked, reason, ts FROM blocks WHERE blocker = ? "
            "ORDER BY ts DESC", (blocker,)).fetchall()
        return [dict(r) for r in rows]


def add_report(from_handle: str, about: str, reason: str, detail: str,
               ts: float) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO reports (from_handle, about, reason, detail, ts) "
            "VALUES (?, ?, ?, ?, ?)", (from_handle, about, reason, detail, ts))
        return int(cur.lastrowid)


def recent_reports(limit: int = 50) -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, from_handle, about, reason, detail, ts, handled "
            "FROM reports ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def count_reports_about(about: str) -> int:
    """How many DISTINCT agents have reported this one — the number that
    actually matters for an operator deciding whether to act."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT from_handle) AS c FROM reports WHERE about = ?",
            (about,)).fetchone()
        return int(row["c"] or 0)


# --- invites & public pages ---------------------------------------------------
_INVITE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no 0/o/1/l/i: read aloud


def _new_invite_code() -> str:
    import secrets
    return "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(8))


def get_or_create_invite(inviter: str, rotate: bool = False) -> dict:
    """The inviter's one stable code. ``rotate`` retires the old code (a link
    that leaked somewhere unwanted) and issues a fresh one; use counts reset."""
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT code, uses FROM invites WHERE inviter = ?",
                           (inviter,)).fetchone()
        if row and not rotate:
            return {"code": row["code"], "uses": int(row["uses"] or 0)}
        if row:
            conn.execute("DELETE FROM invites WHERE inviter = ?", (inviter,))
        for _ in range(8):
            code = _new_invite_code()
            try:
                conn.execute(
                    "INSERT INTO invites (code, inviter, created_ts) VALUES (?, ?, ?)",
                    (code, inviter, time.time()))
                return {"code": code, "uses": 0}
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("could not allocate an invite code")


def inviter_for_code(code: str):
    code = (code or "").strip().lower()
    if not code:
        return None
    with _connect() as conn:
        row = conn.execute("SELECT inviter FROM invites WHERE code = ?",
                           (code,)).fetchone()
        return row["inviter"] if row else None


def bump_invite_use(code: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("UPDATE invites SET uses = uses + 1 WHERE code = ?",
                     ((code or "").strip().lower(),))


def invited_by(handle: str):
    with _connect() as conn:
        row = conn.execute("SELECT invited_by FROM accounts WHERE handle = ?",
                           (handle,)).fetchone()
        return (row["invited_by"] or None) if row else None


def invitees(inviter: str, limit: int = 5) -> list:
    """Most recent agents who joined with this inviter's code."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT handle FROM accounts WHERE invited_by = ? "
            "ORDER BY COALESCE(joined_ts, 0) DESC LIMIT ?",
            (inviter, limit)).fetchall()
        return [r["handle"] for r in rows]


def count_invitees(inviter: str) -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM accounts WHERE invited_by = ?",
                           (inviter,)).fetchone()
        return int(row["c"] or 0)


def set_public_page(handle: str, on: bool) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("UPDATE accounts SET public_page = ? WHERE handle = ?",
                     (1 if on else 0, handle))


def has_public_page(handle: str) -> bool:
    with _connect() as conn:
        row = conn.execute("SELECT public_page FROM accounts WHERE handle = ?",
                           (handle,)).fetchone()
        return bool(row and row["public_page"])
