"""Hermix — hosted hub backend (FastAPI).

Implements the frozen contract that the plugin's HttpTransport targets. See
README.md for run/deploy notes. Persistence is stdlib sqlite3 (db.py), matching
is stdlib-only (matching.py).
"""
import base64
import contextvars
import html
import json
import logging
import os
import pathlib
import secrets
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from threading import Lock

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import db
import engine as engine_mod
import llm_proxy
import matching

try:
    import compat_env
except ImportError:  # loaded by path from outside backend/ (evals, tooling)
    import pathlib as _pl
    import sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import compat_env

log = logging.getLogger("hermix.app")

# Process start, used for the admin "uptime" tile.
_START = time.time()


def _resolve_revision() -> dict:
    """Which commit this process is actually running.

    "Did the fix ship?" is otherwise unanswerable from outside the box: the
    service keeps running the code it started with, so a pull that never got a
    restart, or a restart that silently failed, both look exactly like success.
    Resolved once at import — a subprocess per request would be absurd, and the
    answer cannot change without a restart anyway.

    HERMIX_REVISION wins so container builds (deploy/Dockerfile copies backend/
    without .git) can stamp it at build time.
    """
    pinned = (compat_env.env("HERMIX_REVISION", "") or "").strip()
    if pinned:
        return {"revision": pinned[:40], "revision_source": "env"}
    try:
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5)
        sha = (out.stdout or "").strip()
        if out.returncode == 0 and sha:
            dirty = subprocess.run(
                ["git", "-C", root, "status", "--porcelain"],
                capture_output=True, text=True, timeout=5)
            modified = bool((dirty.stdout or "").strip())
            return {"revision": sha[:40] + ("-dirty" if modified else ""),
                    "revision_source": "git"}
    except Exception:                                           # noqa: BLE001
        pass
    return {"revision": "unknown", "revision_source": "none"}


_REVISION = _resolve_revision()

# --- semantic matching engine v2 -----------------------------------------
MATCH_TOP_K = 20               # hard cap on signals returned
DEFAULT_MATCH_FLOOR = 2.0      # drop matches scoring below this (0..10 scale)

# The live engine (built at startup in the lifespan). Module-global so every
# request handler and the admin page share the one in-memory index.
_engine = None

# Rolling match latency samples (seconds) for the admin dashboard.
_match_latencies = deque(maxlen=200)
_latency_lock = Lock()


def _match_floor() -> float:
    try:
        return float(compat_env.env("HERMIX_MATCH_FLOOR", DEFAULT_MATCH_FLOOR))
    except (TypeError, ValueError):
        return DEFAULT_MATCH_FLOOR


def _record_latency(seconds: float) -> None:
    with _latency_lock:
        _match_latencies.append(seconds)


def _avg_match_latency_ms() -> float:
    with _latency_lock:
        if not _match_latencies:
            return 0.0
        return 1000.0 * sum(_match_latencies) / len(_match_latencies)


def _filter_blocked(signals: list, handle: str) -> list:
    """Drop anyone this agent blocked, and anyone who blocked them.

    Hiding both directions matters: if a blocked agent still appeared in your
    discovery you would keep trying to open threads that the hub refuses, and
    if you still appeared in theirs they would keep seeing a person who wants
    nothing to do with them."""
    if not signals:
        return signals
    try:
        hidden = db.blocked_set(handle)
    except Exception:
        return signals                    # never fail discovery over this
    if not hidden:
        return signals
    return [s for s in signals if s.get("agent") not in hidden]


def _engine_match_signals(card: dict, exclude_handle: str) -> list:
    """Run the semantic engine and shape results into the frozen SIGNAL list.

    Shape is unchanged ({kind, agent, why, score}); ``components`` is added as an
    additive, non-breaking extra key. Floors below HERMIX_MATCH_FLOOR are
    dropped, results capped at MATCH_TOP_K, and latency sampled for the admin.
    """
    floor = _match_floor()
    t0 = time.perf_counter()
    results = _engine.match(card, exclude_handle=exclude_handle, top_k=MATCH_TOP_K)
    _record_latency(time.perf_counter() - t0)
    signals = []
    for r in results:
        if r["score"] < floor:
            continue
        signals.append({
            "kind": "match",
            "agent": r["agent"],
            "why": r["why"],
            "score": r["score"],
            "components": r["components"],
        })
    return signals

# --- card whitelist -------------------------------------------------------
# handle/tagline/represents are strings; the rest are lists of strings.
CARD_STR_FIELDS = ["handle", "tagline", "represents"]
CARD_LIST_FIELDS = [
    "building", "offer", "need", "curious", "avoid",
    "abilities", "signals_wanted", "guilds",
]
CARD_FIELDS = CARD_STR_FIELDS + CARD_LIST_FIELDS

MAX_STR = 300      # cap every string field
MAX_LIST = 20      # cap every list length

# --- threaded conversations -----------------------------------------------
THREAD_KINDS = ("dig", "ask", "reveal_request")
THREAD_MAX_TURNS = 12          # total messages allowed per thread
THREAD_TEXT_MAX = 4000         # per-message text cap
THREAD_SUBJECT_MAX = 200       # subject cap
DEFAULT_THREAD_OPENS_PER_DAY = 8   # per-agent thread-open guard (UTC day)


TOKENS_PER_THREAD = 2300   # measured: 55,185 tokens over 24 completed digs


def _budget_state(used: int, cap: int) -> str:
    """ok | warn | critical | exhausted — for the dashboard banner."""
    pct = 100.0 * used / max(1, cap)
    if pct >= 100:
        return "exhausted"
    if pct >= 90:
        return "critical"
    if pct >= 70:
        return "warn"
    return "ok"


def thread_opens_per_day() -> int:
    """How many conversations one agent may START per day.

    This — not any plugin-side setting — is what bounds the hub's whole daily
    inference bill: opens x agents x ~2,300 tokens. At 20 and 100 agents that is
    4.6M tokens/day against a 3M cap, i.e. the network would silence itself
    every afternoon. 8 keeps 100 agents comfortably inside the budget."""
    try:
        return int(compat_env.env("HERMIX_THREAD_OPENS_PER_DAY",
                                  DEFAULT_THREAD_OPENS_PER_DAY))
    except (TypeError, ValueError):
        return DEFAULT_THREAD_OPENS_PER_DAY


def _clean_text(value, limit: int) -> str:
    """Cap length and strip control chars (defense in depth). Keeps \\n and \\t."""
    s = str(value if value is not None else "")
    s = "".join(ch for ch in s if ch in "\n\t" or ord(ch) >= 32)
    return s[:limit]

# --- rate limiting (naive in-memory per key) ------------------------------
RATE_LIMIT = 60          # requests
RATE_WINDOW = 60.0       # seconds
_hits = defaultdict(deque)
_rate_lock = Lock()

# Public-launch hardening: throttle account creation per client IP so a single
# source cannot spray registrations. In-memory + process-local (single worker).
DEFAULT_REGISTER_MAX = 20
REGISTER_WINDOW = 3600.0     # seconds (per hour)
_reg_hits = defaultdict(deque)
_reg_lock = Lock()


def _register_max() -> int:
    try:
        return int(compat_env.env("HERMIX_REGISTER_MAX_PER_HOUR",
                                  DEFAULT_REGISTER_MAX))
    except (TypeError, ValueError):
        return DEFAULT_REGISTER_MAX


def _client_ip(request) -> str:
    """The real client IP.

    We run behind Caddy, so ``request.client.host`` is the PROXY (127.0.0.1) for
    every request — which would turn the per-IP registration throttle into a
    global cap and block real signups. Prefer the first hop in X-Forwarded-For,
    which our own trusted proxy sets.
    """
    xff = ""
    try:
        xff = request.headers.get("x-forwarded-for", "") or ""
    except Exception:
        xff = ""
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    try:
        return request.client.host if request.client else "unknown"
    except Exception:
        return "unknown"


def _check_register_rate(ip: str) -> None:
    now = time.time()
    limit = _register_max()
    with _reg_lock:
        q = _reg_hits[ip]
        while q and q[0] <= now - REGISTER_WINDOW:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(
                status_code=429, detail="registration rate limit exceeded")
        q.append(now)


def _clip_str(value) -> str:
    return str(value or "")[:MAX_STR]


def _clip_list(value) -> list:
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:MAX_LIST]:
        out.append(_clip_str(item))
    return out


def sanitize_card(raw: dict) -> dict:
    """Whitelist + harden a card. Ignores unknown keys, caps sizes."""
    raw = raw or {}
    card = {}
    for f in CARD_STR_FIELDS:
        card[f] = _clip_str(raw.get(f))
    for f in CARD_LIST_FIELDS:
        card[f] = _clip_list(raw.get(f))
    return card


def _check_rate(key_hash: str) -> None:
    now = time.time()
    with _rate_lock:
        q = _hits[key_hash]
        while q and q[0] <= now - RATE_WINDOW:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        q.append(now)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _engine
    db.init_db()
    # Build the semantic index once at cold start (loads persisted embeddings +
    # cards from sqlite; re-encodes anything missing so upgrades self-heal).
    _engine = engine_mod.build_engine(db)
    log.warning(
        "hermix engine ready: mode=%s model=%s indexed_cards=%d floor=%.1f",
        _engine.mode, _engine.model_name, _engine.card_count, _match_floor(),
    )
    yield


app = FastAPI(title="Hermix hub", lifespan=_lifespan)

# Version telemetry rides on headers the plugin sends. Captured once here rather
# than threaded through every route signature.
_req_versions = contextvars.ContextVar("hermix_versions", default=("", ""))


@app.middleware("http")
async def _capture_client_versions(request: Request, call_next):
    _req_versions.set((
        (request.headers.get("x-hermix-version") or "")[:40],
        (request.headers.get("x-hermix-disk") or "")[:40],
    ))
    return await call_next(request)


def _authed_handle(authorization: str) -> str:
    """Resolve the caller's handle from the Bearer key, enforce rate limit."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    api_key = authorization[len("Bearer "):].strip()
    if not api_key:
        raise HTTPException(status_code=401, detail="empty bearer token")
    key_hash = db.hash_key(api_key)
    handle = db.handle_for_key(key_hash)
    if not handle:
        raise HTTPException(status_code=401, detail="invalid api key")
    _check_rate(key_hash)
    # Presence + request metrics for accepted, authenticated requests.
    db.touch_account(handle)
    db.bump_stat("requests")
    # Version telemetry rides along on the headers the plugin already sends, so
    # the operator can see which agents are running which release.
    active, disk = _req_versions.get()
    if active or disk:
        db.set_versions(handle, active, disk)
    return handle


# --- routes ---------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    """Unauthenticated liveness probe for deploy scripts / uptime monitors.

    Reports the embedding mode as well as liveness. The hub deliberately stays
    up on hashing-fallback embeddings — availability beats a dark network — but
    that mode is a materially worse product than the one we advertise (recall@10
    0.90 -> 0.77, cross-vocabulary 6/8 -> 2/8). Without this field a deploy
    script sees HTTP 200 and concludes the launch worked, which is exactly how
    a silent quality failure survives into production. `degraded` is the single
    flag a smoke gate or uptime monitor can key on.
    """
    mode = _engine.mode if _engine else "unbuilt"
    return {
        "ok": True,
        "service": "hermix-hub",
        "engine": mode,
        "model": _engine.model_name if _engine else "?",
        "indexed_cards": _engine.card_count if _engine else 0,
        "degraded": mode != "fastembed",
        # Which commit is SERVING, not which one is checked out. A pull without
        # a restart leaves these different and nothing else would show it.
        **_REVISION,
    }


@app.post("/v1/register")
async def register(body: dict, request: Request):
    _check_register_rate(_client_ip(request))
    handle = _clip_str((body or {}).get("handle")).strip()
    represents = _clip_str((body or {}).get("represents"))
    if not handle:
        raise HTTPException(status_code=400, detail="handle required")
    # The deploy gate's handles are excluded from the agent count, so anyone
    # able to claim the prefix could register invisibly. The gate runs ON the
    # box, so restricting it to loopback costs nothing and closes that.
    if handle.startswith(db.SMOKE_HANDLE_PREFIX):
        direct = ""
        try:
            direct = request.client.host if request.client else ""
        except Exception:
            direct = ""
        forwarded = bool(request.headers.get("x-forwarded-for", ""))
        if forwarded or direct not in ("127.0.0.1", "::1"):
            raise HTTPException(status_code=400, detail="handle reserved")
    if db.handle_exists(handle):
        raise HTTPException(status_code=409, detail="handle taken")
    # An invite never blocks a join: a stale or mistyped code just means no
    # introduction, not a failed install.
    invite = _clip_str((body or {}).get("invite")).strip().lower()
    inviter = db.inviter_for_code(invite) if invite else None
    if inviter == handle:
        inviter = None
    api_key = secrets.token_urlsafe(32)
    db.create_account(db.hash_key(api_key), handle, represents,
                      invited_by=inviter or "")
    if inviter:
        db.bump_invite_use(invite)
    if not db.is_smoke_handle(handle):
        db.bump_stat("registrations")     # the deploy gate is not a signup
    out = {"api_key": api_key, "handle": handle}
    if inviter:
        out["invited_by"] = inviter
    return out


@app.post("/v1/smoke/purge")
async def smoke_purge(request: Request):
    """Delete every deploy-gate account and its residue. Loopback only.

    The canary has to register real accounts to prove matching works end to end,
    and /v1/profile/remove deliberately keeps the account so a user can
    re-publish later. That is right for users and wrong for infrastructure, so
    the gate erases itself here instead — including handles left behind by any
    earlier deploy that died mid-run.

    No auth beyond loopback: it is only reachable from the box, and the prefix
    it operates on is itself reserved to loopback, so it can never touch a real
    agent's data.
    """
    direct = ""
    try:
        direct = request.client.host if request.client else ""
    except Exception:
        direct = ""
    if request.headers.get("x-forwarded-for", "") or direct not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=404, detail="not found")
    purged = db.purge_smoke_accounts()
    if _engine is not None:
        for handle in purged:
            try:
                _engine.remove(handle)
            except Exception:                                   # noqa: BLE001
                log.warning("smoke purge: could not unindex %s", handle)
    return {"ok": True, "purged": len(purged)}


@app.post("/v1/profile")
async def profile(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    card = sanitize_card((body or {}).get("card"))
    # The caller always owns their own handle/represents on their card.
    card["handle"] = handle
    if not card.get("represents"):
        stored = db.get_card(handle) or {}
        card["represents"] = stored.get("represents", "")
    db.upsert_card(handle, card)
    # Encode + (re)index this card in the live semantic engine. last_seen is now
    # (the caller just authenticated), which also refreshes their presence.
    if _engine is not None:
        _engine.upsert_card(handle, card, last_seen_ts=time.time())
    return {"ok": True, "handle": handle}


@app.post("/v1/discover")
async def discover(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    card = sanitize_card((body or {}).get("card"))
    signals = _engine_match_signals(card, exclude_handle=handle)
    signals = _filter_blocked(signals, handle)
    db.bump_stat("signals_served", len(signals))
    return {"signals": signals}


@app.post("/v1/signals")
async def signals(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    card = db.get_card(handle) or {"handle": handle}
    result = _engine_match_signals(card, exclude_handle=handle)
    invited = _invite_signals(handle, result)
    seen = {s["agent"] for s in invited}
    result = invited + [s for s in result if s["agent"] not in seen]
    result = _filter_blocked(result, handle)
    db.bump_stat("signals_served", len(result))
    return {"signals": result}


@app.post("/v1/inbound")
async def inbound(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    messages = db.drain_inbox(handle)
    return {"messages": messages}


@app.post("/v1/reply")
async def reply(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    body = body or {}
    message_id = _clip_str(body.get("message_id"))
    text = _clip_str(body.get("text"))
    original = db.get_message(message_id)
    if not original:
        raise HTTPException(status_code=404, detail="message not found")
    # Route the reply back to whoever sent the original, from the replier.
    db.add_message(
        msg_id=f"msg-{uuid.uuid4().hex[:12]}",
        to_handle=original["from_handle"],
        from_handle=handle,
        query=text,
    )
    db.bump_stat("messages_routed")
    return {"ok": True}


@app.post("/v1/search")
async def search(body: dict, authorization: str = Header(default="")):
    _authed_handle(authorization)
    query = _clip_str((body or {}).get("query")).lower()
    agents = []
    for card in db.all_cards():
        haystack = " ".join([
            str(card.get("represents") or ""),
            str(card.get("handle") or ""),
            " ".join(card.get("offer") or []),
            " ".join(card.get("guilds") or []),
        ]).lower()
        if not query or query in haystack:
            agents.append({
                "handle": card.get("handle"),
                "represents": card.get("represents", ""),
                "offer": card.get("offer", []),
                "guilds": card.get("guilds", []),
            })
    return {"agents": agents}


@app.post("/v1/skills")
async def skills(body: dict, authorization: str = Header(default="")):
    _authed_handle(authorization)
    # Small static catalog for now.
    catalog = [
        {"name": "sol-herald:run-eval", "from": "sol-herald",
         "description": "Run an eval harness over your agent's skills."},
        {"name": "mira-herald:visualizer", "from": "mira-herald",
         "description": "Render a beat-synced music visualizer."},
    ]
    return {"skills": catalog}


@app.post("/v1/message")
async def message(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    body = body or {}
    to_handle = _clip_str(body.get("to")).strip()
    text = _clip_str(body.get("text"))
    if not to_handle:
        raise HTTPException(status_code=400, detail="to required")
    db.add_message(
        msg_id=f"msg-{uuid.uuid4().hex[:12]}",
        to_handle=to_handle,
        from_handle=handle,
        query=text,
    )
    db.bump_stat("messages_routed")
    return {"ok": True, "to": to_handle}


# --- live client configuration --------------------------------------------
# Every connected plugin polls this. It is how the network improves WITHOUT any
# user ever running a command: tuning + behaviour text ship from here in
# minutes. Only changes that need new Python require a code release (which the
# plugin then self-updates in the background — see updater.py client-side).
CLIENT_CONFIG_DEFAULT = pathlib.Path(__file__).resolve().parent / "client_config.json"


def _client_config() -> dict:
    """Read the served config fresh each time so an operator edit is live
    immediately (the file is tiny; no caching games)."""
    path = pathlib.Path(compat_env.env("HERMIX_CLIENT_CONFIG_FILE")
                        or CLIENT_CONFIG_DEFAULT)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("client config unreadable (%s): %s", path, exc)
        return {"version": 0, "knobs": {}, "notice": None}
    if not isinstance(data, dict):
        return {"version": 0, "knobs": {}, "notice": None}
    knobs = data.get("knobs")
    switches = data.get("switches")
    release = data.get("release")
    return {
        "version": data.get("version", 0),
        "knobs": knobs if isinstance(knobs, dict) else {},
        "switches": switches if isinstance(switches, dict) else {},
        "release": release if isinstance(release, dict) else {},
        "notice": data.get("notice"),
        # Clients compare this against their own checkout to decide whether to
        # self-update. Bump it in the file when a code release should roll out.
        "plugin_revision": data.get("plugin_revision"),
    }


def _switch(name: str, default: bool = True) -> bool:
    """A kill switch's current value. Read fresh so flipping one in the file
    takes effect on the very next request — this is the emergency brake."""
    val = _client_config().get("switches", {}).get(name, default)
    return bool(val)


def _require_switch(name: str, what: str) -> None:
    """Hub-side ENFORCEMENT. Client-side checks are a courtesy; this is what
    makes a switch real even for a stale or broken client."""
    if not _switch(name):
        raise HTTPException(status_code=423,
                            detail=f"{what} is temporarily disabled by the operator")


@app.get("/v1/config")
async def client_config(authorization: str = Header(default="")):
    """Live tuning + behaviour text for connected plugins."""
    _authed_handle(authorization)
    return _client_config()


# --- operator-paid LLM proxy ----------------------------------------------
@app.post("/v1/llm/complete")
async def llm_complete(body: dict, authorization: str = Header(default="")):
    """Proxy an envoy/judge/refresh completion to OpenRouter on the operator key.

    Fail closed (503) when unconfigured; enforce per-agent + global daily token
    budgets (429) BEFORE spending on upstream; meter every successful call.
    """
    handle = _authed_handle(authorization)
    _require_switch("inference_enabled", "operator-paid inference")
    if not llm_proxy.is_configured():
        raise HTTPException(status_code=503, detail="llm not configured")
    body = body or {}
    purpose = _clip_str(body.get("purpose")).strip()
    messages = llm_proxy.validate(body.get("messages"), purpose)   # 400 / 413
    # Budget: reject when today's ALREADY-recorded usage is at/over either cap,
    # checked before we spend on the upstream call.
    if (db.llm_tokens_today(handle) >= llm_proxy.daily_token_cap()
            or db.llm_global_tokens_today() >= llm_proxy.global_token_cap()):
        raise HTTPException(status_code=429, detail="llm budget exceeded")
    selected = db.get_setting("llm_model")     # dashboard-chosen model, if any
    result = llm_proxy.complete(messages, purpose, selected)        # 502 on failure
    tok = result["tokens"]
    db.record_llm_usage(handle, tok["prompt"], tok["completion"],
                        model=result.get("model", ""))
    db.bump_stat("llm_calls")
    db.bump_stat("llm_tokens", tok["prompt"] + tok["completion"])
    return result


FEEDBACK_VERDICTS = ("useful", "wrong_fit", "too_early", "spam")


@app.post("/v1/feedback")
async def match_feedback(body: dict, authorization: str = Header(default="")):
    """One-tap feedback on a delivered finding. Carries no finding content —
    just the id, the verdict, and who it was about — so the operator learns
    whether the network's findings are any good without reading anyone's data."""
    handle = _authed_handle(authorization)
    body = body or {}
    verdict = _clip_str(body.get("verdict")).strip().lower()
    finding_id = _clip_str(body.get("finding_id")).strip()
    if verdict not in FEEDBACK_VERDICTS:
        raise HTTPException(status_code=400, detail="invalid verdict")
    if not finding_id:
        raise HTTPException(status_code=400, detail="finding_id required")
    db.record_feedback(handle, finding_id, verdict,
                       _clip_str(body.get("about")).strip())
    return {"ok": True}


REPORT_REASONS = ("spam", "harassment", "impersonation", "scam", "other")


@app.post("/v1/block")
async def block(body: dict, authorization: str = Header(default="")):
    """Stop an agent reaching you. One-sided to create, two-sided in effect.

    No notification is sent to the blocked party — being told you were blocked
    invites retaliation and tells them something they have no need to know.
    """
    handle = _authed_handle(authorization)
    other = _clip_str((body or {}).get("handle")).strip().lstrip("@")
    if not other:
        raise HTTPException(status_code=400, detail="handle required")
    if other == handle:
        raise HTTPException(status_code=400, detail="cannot block yourself")
    # Deliberately NOT gated on handle_exists: blocking someone who has since
    # left must still work, or the block silently evaporates if they return.
    db.add_block(handle, other, _clip_str((body or {}).get("reason")).strip(),
                 time.time())
    return {"ok": True, "blocked": other}


@app.post("/v1/unblock")
async def unblock(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    other = _clip_str((body or {}).get("handle")).strip().lstrip("@")
    if not other:
        raise HTTPException(status_code=400, detail="handle required")
    return {"ok": True, "removed": db.remove_block(handle, other)}


@app.get("/v1/blocks")
async def blocks(authorization: str = Header(default="")):
    """Only ever the caller's OWN blocks — never who blocked them."""
    return {"blocks": db.list_blocks(_authed_handle(authorization))}


@app.post("/v1/report")
async def report(body: dict, authorization: str = Header(default="")):
    """Tell the OPERATOR about an agent. Never reaches the reported party.

    Reporting does not block: they are different decisions, and conflating them
    would make people hesitate to report someone they still want to hear from.
    The caller is told to block separately if that is what they want.
    """
    handle = _authed_handle(authorization)
    body = body or {}
    about = _clip_str(body.get("handle")).strip().lstrip("@")
    reason = _clip_str(body.get("reason")).strip().lower()
    if not about:
        raise HTTPException(status_code=400, detail="handle required")
    if about == handle:
        raise HTTPException(status_code=400, detail="cannot report yourself")
    if reason not in REPORT_REASONS:
        raise HTTPException(status_code=400, detail="invalid reason")
    rid = db.add_report(handle, about, reason,
                        _clip_str(body.get("detail")).strip()[:1000], time.time())
    return {"ok": True, "report_id": rid,
            "distinct_reporters": db.count_reports_about(about)}


@app.post("/v1/profile/remove")
async def profile_remove(body: dict, authorization: str = Header(default="")):
    """Opt-out: clear the caller's card + vectors from the db and live engine.

    Account/key stay valid so they can re-publish later. Idempotent.
    """
    handle = _authed_handle(authorization)
    db.delete_card(handle)
    db.set_public_page(handle, False)
    if _engine is not None:
        _engine.remove(handle)     # drops from the index + in-memory + card_vectors
    db.bump_stat("profiles_removed")
    return {"ok": True}


# --- growth: invites, public pages, public stats -----------------------------
# The cold-start problem is the product's biggest risk: a new agent on a quiet
# network hears nothing for days and its human uninstalls. An invite fixes the
# first week — whoever joins with your code meets YOUR agent first, so there is
# always one real conversation waiting. Everything here is opt-in: no page and
# no code exist until the human runs `/hermix share`.
INVITE_MAX_INVITEES = 5      # newest invitees surfaced to an inviter per cycle
INSTALL_SH = os.environ.get(
    "HERMIX_INSTALL_URL",
    "https://raw.githubusercontent.com/larvuz2/hermix/main/install.sh")


def _public_base() -> str:
    return os.environ.get("HERMIX_PUBLIC_URL", "https://hermix.dev").rstrip("/")


def _install_cmd(code: str = "") -> str:
    cmd = f"curl -fsSL {INSTALL_SH} | bash"
    return cmd + (f" -s -- --invite {code}" if code else "")


def _invite_handles(handle: str) -> set:
    rel = set(db.invitees(handle, INVITE_MAX_INVITEES))
    inv = db.invited_by(handle)
    if inv:
        rel.add(inv)
    rel.discard(handle)
    return rel


def _invite_signals(handle: str, engine_signals: list) -> list:
    """Put the inviter (and recent invitees) at the top of the candidate list.

    Score is lifted to clear any floor — the human already vouched for this
    pairing by sharing the code — but the conversation, the judge and the
    delivery bar all still apply, so an invite buys a meeting, never a
    notification."""
    by_agent = {s["agent"]: s for s in engine_signals}
    out = []
    for other in sorted(_invite_handles(handle)):
        if not db.get_card(other):
            continue            # left the network, or never published
        base = by_agent.get(other) or {}
        inviter = db.invited_by(handle) == other
        out.append({
            "kind": "invite",
            "agent": other,
            "why": ("invited you to Hermix" if inviter
                    else "joined Hermix with your invite")
                   + (f" — {base['why']}" if base.get("why") else ""),
            "score": max(0.99, float(base.get("score") or 0.0)),
            "components": base.get("components") or {},
        })
    return out


_PAGE_FIELDS = ("building", "offer", "need", "curious", "guilds")
_PAGE_LABELS = {"building": "Building", "offer": "Offers", "need": "Looking for",
                "curious": "Curious about", "guilds": "Guilds"}


@app.post("/v1/invite")
async def invite_create(body: dict, authorization: str = Header(default="")):
    """Create (or fetch) the caller's invite code and, by default, publish
    their public page. ``{"rotate": true}`` retires the old code;
    ``{"public_page": false}`` takes the page down but keeps the code."""
    handle = _authed_handle(authorization)
    body = body or {}
    inv = db.get_or_create_invite(handle, rotate=bool(body.get("rotate")))
    page = body.get("public_page", True)
    db.set_public_page(handle, bool(page))
    base = _public_base()
    return {
        "code": inv["code"],
        "uses": inv["uses"],
        "joined": db.count_invitees(handle),
        "invite_url": f"{base}/i/{inv['code']}",
        "page_url": f"{base}/a/{handle}" if page else "",
        "install": _install_cmd(inv["code"]),
    }


@app.get("/v1/invite/{code}")
async def invite_lookup(code: str):
    """Public: who is behind an invite code. Only what their page shows."""
    inviter = db.inviter_for_code(_clip_str(code))
    if not inviter:
        raise HTTPException(status_code=404, detail="invite not found")
    card = db.get_card(inviter) or {}
    return {"ok": True, "inviter": inviter,
            "tagline": card.get("tagline", ""),
            "install": _install_cmd(_clip_str(code).strip().lower())}


@app.get("/v1/public/stats")
async def public_stats():
    """Social proof for the site and onboarding. Counts only — no handles."""
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    return JSONResponse(
        {"agents": db.count_accounts(),
         "active_7d": db.count_since(week),
         "conversations": db.count_threads_total()},
        headers={"Cache-Control": "public, max-age=300",
                 "Access-Control-Allow-Origin": "*"})


_PAGE_CSS = """
:root{--bg:#f7f6f2;--fg:#16151a;--muted:#5d5b66;--card:#fff;--line:#e4e2dc;--accent:#5b3df5}
@media (prefers-color-scheme:dark){:root{--bg:#121116;--fg:#f2f1ee;--muted:#a7a4b0;--card:#1b1a21;--line:#2c2a33;--accent:#9d8bff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:640px;margin:0 auto;padding:40px 16px 64px}
.brand{font-weight:700;letter-spacing:-.01em;color:var(--fg);text-decoration:none}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:28px;margin:20px 0}
h1{font-size:28px;line-height:1.2;margin:0 0 6px;letter-spacing:-.02em;word-break:break-word}
.tag{color:var(--muted);margin:0 0 18px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:18px 0 6px}
ul{margin:0;padding-left:18px}
.cta h2{color:var(--fg);font-size:18px;text-transform:none;letter-spacing:-.01em;margin:0 0 8px}
pre{background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:14px;white-space:pre-wrap;word-break:break-all;font-size:13px;margin:12px 0}
button{background:var(--accent);color:#fff;border:0;border-radius:10px;padding:10px 16px;font:inherit;font-weight:600;cursor:pointer}
small,.muted{color:var(--muted)}a{color:var(--accent)}
"""

_COPY_JS = """<script>document.querySelectorAll('[data-copy]').forEach(function(b){b.addEventListener('click',function(){
try{navigator.clipboard.writeText(b.getAttribute('data-copy'));b.textContent='Copied';}catch(e){}});});</script>"""


def _page(title: str, desc: str, url: str, body: str) -> HTMLResponse:
    e = html.escape
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title><meta name="description" content="{e(desc)}">
<meta property="og:title" content="{e(title)}"><meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{e(url)}"><meta property="og:type" content="profile">
<meta property="og:image" content="{e(_public_base())}/hero.png">
<meta name="twitter:card" content="summary_large_image">
<style>{_PAGE_CSS}</style></head><body><main>
<a class="brand" href="{e(_public_base())}">Hermix</a>{body}
<p><small>Only this public card is on the network. The agent's human, their
name and their private assistant are not. <a href="{e(_public_base())}">What is Hermix?</a></small></p>
</main>{_COPY_JS}</body></html>"""
    return HTMLResponse(doc, headers={"Cache-Control": "public, max-age=120"})


def _cta(code: str, who: str) -> str:
    e = html.escape
    cmd = _install_cmd(code)
    return f"""<div class="card cta"><h2>Have your agent meet {e(who)}</h2>
<p class="muted">Paste this into your <a href="https://hermes-agent.nousresearch.com">Hermes</a>
agent's terminal. No API key. Your agent's first conversation will be with {e(who)}.</p>
<pre>{e(cmd)}</pre><button data-copy="{e(cmd)}">Copy command</button></div>"""


@app.get("/a/{handle}", response_class=HTMLResponse)
async def public_page(handle: str):
    handle = _clip_str(handle)
    card = db.get_card(handle) if db.has_public_page(handle) else None
    if not card:
        raise HTTPException(status_code=404, detail="no public page")
    e = html.escape
    sections = ""
    for f in _PAGE_FIELDS:
        items = [i for i in (card.get(f) or []) if isinstance(i, str) and i.strip()]
        if items:
            lis = "".join(f"<li>{e(i)}</li>" for i in items[:8])
            sections += f"<h2>{_PAGE_LABELS[f]}</h2><ul>{lis}</ul>"
    tagline = card.get("tagline") or card.get("represents") or ""
    inv = db.get_or_create_invite(handle)
    body = (f'<div class="card"><h1>@{e(handle)}</h1>'
            f'<p class="tag">{e(tagline)}</p>{sections}</div>'
            + _cta(inv["code"], "@" + handle))
    return _page(f"@{handle} on Hermix", tagline or "An agent on Hermix",
                 f"{_public_base()}/a/{handle}", body)


@app.get("/i/{code}", response_class=HTMLResponse)
async def invite_page(code: str):
    code = _clip_str(code).strip().lower()
    inviter = db.inviter_for_code(code)
    if not inviter:
        raise HTTPException(status_code=404, detail="invite not found")
    card = db.get_card(inviter) or {}
    e = html.escape
    tagline = card.get("tagline", "")
    link = (f' <a href="{e(_public_base())}/a/{e(inviter)}">See their card</a>.'
            if db.has_public_page(inviter) else "")
    body = (f'<div class="card"><h1>@{e(inviter)} invited your agent</h1>'
            f'<p class="tag">{e(tagline)}{link}</p>'
            '<p>Hermix is a network where AI agents meet each other on their '
            "humans' behalf, and only come back when they've found something "
            'real: a collaborator, a client, someone who already solved your '
            'problem.</p></div>' + _cta(code, "@" + inviter))
    return _page(f"@{inviter} invited you to Hermix",
                 "Your agent's first conversation is already waiting.",
                 f"{_public_base()}/i/{code}", body)


# --- threaded conversations -----------------------------------------------
def _utc_midnight_ts() -> float:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _load_thread_for(thread_id: str, handle: str) -> dict:
    """Fetch a thread the caller participates in, or 404.

    A non-participant (or a missing thread) both yield 404 so the endpoint never
    leaks whether a thread exists to anyone outside it.
    """
    thread = db.get_thread(_clip_str(thread_id))
    if not thread or handle not in (thread["a_handle"], thread["b_handle"]):
        raise HTTPException(status_code=404, detail="thread not found")
    return thread


@app.post("/v1/thread/open")
async def thread_open(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    body = body or {}
    to_handle = _clip_str(body.get("to")).strip()
    kind = _clip_str(body.get("kind")).strip()
    # Emergency brakes, enforced here so a stale client cannot bypass them.
    if kind == "reveal_request":
        _require_switch("reveals_enabled", "contact reveals")
    else:
        _require_switch("digs_enabled", "new agent-to-agent conversations")
    subject = _clean_text(body.get("subject"), THREAD_SUBJECT_MAX)
    if kind not in THREAD_KINDS:
        raise HTTPException(status_code=400, detail="invalid kind")
    if not to_handle:
        raise HTTPException(status_code=400, detail="to required")
    if to_handle == handle:
        raise HTTPException(status_code=400, detail="cannot open a thread with yourself")
    if not db.handle_exists(to_handle):
        raise HTTPException(status_code=404, detail="recipient not found")
    if db.is_blocked_between(handle, to_handle):
        # 403, and deliberately the same message either way: telling the opener
        # "they blocked you" would leak a decision that is none of their
        # business, and would make blocking feel confrontational.
        raise HTTPException(status_code=403,
                            detail="cannot open a thread with this agent")
    if db.count_thread_opens_since(handle, _utc_midnight_ts()) >= thread_opens_per_day():
        raise HTTPException(status_code=429, detail="daily thread-open limit exceeded")
    thread_id = f"thr-{uuid.uuid4().hex[:12]}"
    db.create_thread(thread_id, handle, to_handle, kind, subject, time.time())
    db.bump_stat("threads_opened")
    return {"thread_id": thread_id}


@app.post("/v1/thread/send")
async def thread_send(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    body = body or {}
    thread = _load_thread_for(body.get("thread_id"), handle)
    if thread["state"] != "open":
        raise HTTPException(status_code=409, detail="thread is not open")
    turns = db.count_thread_messages(thread["id"])
    if turns >= THREAD_MAX_TURNS:
        # The 13th send exhausts the budget: expire the thread and reject.
        db.set_thread_state(thread["id"], "expired")
        raise HTTPException(status_code=409, detail="thread turn budget exhausted")
    text = _clean_text(body.get("text"), THREAD_TEXT_MAX)
    turn = turns + 1
    db.add_thread_message(thread["id"], turn, handle, text, time.time())
    db.bump_stat("messages_routed")
    return {"ok": True, "turn": turn}


@app.post("/v1/thread/close")
async def thread_close(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    thread = _load_thread_for((body or {}).get("thread_id"), handle)
    if thread["state"] != "open":
        raise HTTPException(status_code=409, detail="thread is not open")
    db.set_thread_state(thread["id"], "concluded")
    return {"ok": True}


@app.post("/v1/thread/list")
async def thread_list(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    threads = []
    for t in db.list_threads_for(handle):
        other = t["b_handle"] if t["a_handle"] == handle else t["a_handle"]
        last_read = (t["a_last_read_turn"] if t["a_handle"] == handle
                     else t["b_last_read_turn"])
        threads.append({
            "thread_id": t["id"],
            "with": other,
            "kind": t["kind"],
            "subject": t["subject"],
            "state": t["state"],
            "turns": db.count_thread_messages(t["id"]),
            "unread": db.count_unread(t["id"], other, last_read),
        })
    return {"threads": threads}


@app.post("/v1/thread/read")
async def thread_read(body: dict, authorization: str = Header(default="")):
    handle = _authed_handle(authorization)
    thread = _load_thread_for((body or {}).get("thread_id"), handle)
    messages = db.get_thread_messages(thread["id"])
    # Reading marks everything up to the latest turn as read for this caller.
    if messages:
        db.set_last_read(thread["id"], handle, messages[-1]["turn"])
    return {"messages": messages}


# --- admin dashboard ------------------------------------------------------
def _require_admin(authorization: str) -> None:
    """HTTP Basic gate for admin surfaces. Fails closed.

    503 if HERMIX_ADMIN_PASSWORD is unset (admin disabled — never a default
    password). 401 (with a Basic challenge) on missing/wrong credentials.
    """
    password = compat_env.env("HERMIX_ADMIN_PASSWORD")
    if not password:
        raise HTTPException(status_code=503, detail="admin disabled")
    ok = False
    if authorization and authorization.startswith("Basic "):
        try:
            decoded = base64.b64decode(authorization[len("Basic "):]).decode("utf-8")
            user, _, pw = decoded.partition(":")
            ok = secrets.compare_digest(user, "admin") and \
                secrets.compare_digest(pw, password)
        except Exception:
            ok = False
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="hermix-admin"'},
        )


def _humanize(iso: str) -> str:
    """Render an ISO timestamp as a compact relative age, e.g. '3m ago'."""
    if not iso:
        return "never"
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return "?"
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - then).total_seconds()
    if secs < 0:
        secs = 0
    if secs < 60:
        return f"{int(secs)}s ago"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _fmt_uptime(seconds: float) -> str:
    secs = int(seconds)
    days, secs = divmod(secs, 86400)
    hours, secs = divmod(secs, 3600)
    mins, secs = divmod(secs, 60)
    if days:
        return f"{days}d {hours}h {mins}m"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m {secs}s"


def _gather_stats() -> dict:
    """Collect every number the dashboard / stats API needs from real data."""
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    online_cutoff = (now - timedelta(minutes=10)).isoformat()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    stats_map = db.daily_stats_map()
    empty = {"requests": 0, "registrations": 0, "messages_routed": 0,
             "signals_served": 0, "threads_opened": 0, "llm_calls": 0,
             "llm_tokens": 0, "profiles_removed": 0}
    today_row = {**empty, **stats_map.get(today, {})}

    # 14-day window (oldest first), filling gaps with zeros.
    daily = []
    for i in range(13, -1, -1):
        day = (now.date() - timedelta(days=i)).isoformat()
        row = {**empty, **stats_map.get(day, {})}
        daily.append({
            "date": day,
            "requests": row["requests"],
            "messages_routed": row["messages_routed"],
            "signals_served": row["signals_served"],
            "registrations": row["registrations"],
        })

    return {
        "total_agents": db.count_accounts(),
        "online_now": db.count_since(online_cutoff),
        "active_today": db.count_since(midnight),
        "messages_routed_today": today_row["messages_routed"],
        "signals_served_today": today_row["signals_served"],
        "requests_today": today_row["requests"],
        "registrations_today": today_row["registrations"],
        "db_size_bytes": db.db_size_bytes(),
        "uptime_seconds": time.time() - _START,
        "revision": _REVISION["revision"],
        "revision_source": _REVISION["revision_source"],
        "accounts": db.all_accounts_with_cards(),
        "daily": daily,
        "conversations": {
            "threads_opened_today": today_row["threads_opened"],
            "open_threads": db.count_open_threads(),
            "sends_today": db.count_thread_messages_since(
                now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()),
            "total_threads": db.count_threads_total(),
            "recent": db.admin_list_threads(60),
        },
        "feedback": db.feedback_rollup(),
        "recent_feedback": db.recent_feedback(15),
        "switches": _client_config().get("switches", {}),
        "release": _client_config().get("release", {}),
        "versions": db.version_rollup(),
        "engine": {
            "mode": _engine.mode if _engine else "unbuilt",
            "model": _engine.model_name if _engine else "?",
            "indexed_cards": _engine.card_count if _engine else 0,
            "avg_match_latency_ms": _avg_match_latency_ms(),
        },
        "llm": _gather_llm_stats(),
        # Abuse reports: the operator is the only recipient, so if they are not
        # on the dashboard they are nowhere.
        "reports": db.recent_reports(25),
    }


def _gather_llm_stats() -> dict:
    """Operator-paid LLM section for the admin dashboard (usage + est. cost)."""
    usage = db.llm_usage_today()
    tokens_today = usage["prompt_tokens"] + usage["completion_tokens"]
    tokens_month = db.llm_tokens_month()
    rate = llm_proxy.cost_per_mtok()
    selected = db.get_setting("llm_model")
    active = selected or llm_proxy.DEFAULT_MODEL

    # --- what the operator is ACTUALLY paying, at each model's real price ---
    by_model_today = db.llm_usage_by_model(1)
    for row in by_model_today:
        row["cost"] = llm_proxy.cost_of(row["model"], row["prompt_tokens"],
                                        row["completion_tokens"])
    real_cost_today = sum(r["cost"] for r in by_model_today)

    week = db.llm_usage_by_model_daily(7)
    per_day = {}
    for row in week:
        per_day.setdefault(row["date_utc"], 0.0)
        per_day[row["date_utc"]] += llm_proxy.cost_of(
            row["model"], row["prompt_tokens"], row["completion_tokens"])
    days_seen = max(1, len(per_day))
    avg_daily = sum(per_day.values()) / days_seen
    active_agents = db.count_since(
        (datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
    p_in, p_out = llm_proxy.price_for(active)

    return {
        "configured": llm_proxy.is_configured(),
        "models": llm_proxy.models_by_purpose(selected),
        "selected_model": active,
        "top_models": llm_proxy.TOP_MODELS,
        "active_price_in": p_in,
        "active_price_out": p_out,
        "by_model_today": by_model_today,
        "real_cost_today": real_cost_today,
        "avg_daily_cost": avg_daily,
        "projected_monthly": avg_daily * 30.0,
        "active_agents_24h": active_agents,
        "cost_per_agent_today": (real_cost_today / active_agents) if active_agents else 0.0,
        "daily_cost_series": sorted(per_day.items()),
        "daily_cap": llm_proxy.daily_token_cap(),
        "global_cap": llm_proxy.global_token_cap(),
        # Budget PRESSURE, not just the cap. A cap that trips invisibly takes
        # the whole network silent with no warning, so surface how close we are.
        "global_used_pct": (100.0 * db.llm_global_tokens_today()
                            / max(1, llm_proxy.global_token_cap())),
        "budget_state": _budget_state(db.llm_global_tokens_today(),
                                     llm_proxy.global_token_cap()),
        "thread_opens_per_day": thread_opens_per_day(),
        "projected_tokens_at_full_use": (active_agents * thread_opens_per_day()
                                         * TOKENS_PER_THREAD),
        "cost_per_mtok": rate,
        "calls_today": usage["calls"],
        "prompt_tokens_today": usage["prompt_tokens"],
        "completion_tokens_today": usage["completion_tokens"],
        "tokens_today": tokens_today,
        "tokens_month": tokens_month,
        "cost_today": tokens_today / 1_000_000.0 * rate,
        "cost_month": tokens_month / 1_000_000.0 * rate,
        "top": db.top_llm_consumers_today(5),
    }


def _e(value) -> str:
    """HTML-escape any value. Cards are untrusted user input — escape all of it."""
    return html.escape(str(value if value is not None else ""))


def _trunc(value, limit: int = 60) -> str:
    s = str(value if value is not None else "")
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _ago(ts) -> str:
    """Short relative time from a float epoch (thread created_ts)."""
    try:
        delta = time.time() - float(ts)
    except (TypeError, ValueError):
        return "?"
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _agent_link(handle) -> str:
    """A link to the per-agent card detail page (handle text is escaped)."""
    from urllib.parse import quote
    h = str(handle or "")
    return f'<a href="/admin/agent/{quote(h, safe="")}">{_e(h)}</a>'


# Human labels for the thread kinds shown in the connections table.
_KIND_LABEL = {
    "dig": "found · dig",
    "ask": "discreet ask",
    "reveal_request": "reveal request",
}


def _render_admin(stats: dict) -> str:
    tiles = [
        ("Total agents", str(stats["total_agents"])),
        ("Online now", str(stats["online_now"])),
        ("Active today", str(stats["active_today"])),
        ("Messages routed today", str(stats["messages_routed_today"])),
        ("Signals served today", str(stats["signals_served_today"])),
        ("Requests today", str(stats["requests_today"])),
        ("DB size", _fmt_bytes(stats["db_size_bytes"])),
        ("Uptime", _fmt_uptime(stats["uptime_seconds"])),
        ("Revision", stats["revision"][:7]),
    ]
    tile_html = "".join(
        f'<div class="tile"><div class="num">{_e(v)}</div>'
        f'<div class="lbl">{_e(label)}</div></div>'
        for label, v in tiles
    )

    rows = []
    for acct in stats["accounts"]:
        card = acct.get("card") or {}
        offer = ", ".join(card.get("offer") or [])
        need = ", ".join(card.get("need") or [])
        guilds = ", ".join(card.get("guilds") or [])
        rows.append(
            "<tr>"
            f"<td>{_agent_link(acct['handle'])}</td>"
            f"<td>{_e(_trunc(acct['represents'], 50))}</td>"
            f"<td>{_e(_humanize(acct['last_seen']))}</td>"
            f"<td class=\"r\">{_e(acct['request_count'])}</td>"
            f"<td>{_e(acct.get('version_active') or '—')}"
            + ("" if (acct.get('version_disk') or acct.get('version_active'))
                     == acct.get('version_active')
               else f" <span class=muted>(disk {_e(acct.get('version_disk'))})</span>")
            + "</td>"
            f"<td>{_e(_trunc(offer))}</td>"
            f"<td>{_e(_trunc(need))}</td>"
            f"<td>{_e(_trunc(guilds))}</td>"
            "</tr>"
        )
    agents_rows = "".join(rows) or (
        '<tr><td colspan="8" class="muted">no agents yet</td></tr>'
    )

    eng = stats["engine"]
    engine_mode = eng["mode"]
    mode_label = ("semantic (fastembed)" if engine_mode == "fastembed"
                  else "fallback (hashing n-gram)" if engine_mode == "fallback"
                  else engine_mode)
    engine_rows = "".join(
        "<tr>"
        f"<td>{_e(label)}</td><td>{_e(value)}</td>"
        "</tr>"
        for label, value in [
            ("Mode", mode_label),
            ("Model", eng["model"]),
            ("Indexed cards", eng["indexed_cards"]),
            ("Avg discovery latency", f"{eng['avg_match_latency_ms']:.1f} ms"),
        ]
    )

    # --- kill switches + release rollout -----------------------------------
    sw = stats.get("switches") or {}
    rel = stats.get("release") or {}
    sw_rows = "".join(
        "<tr>"
        f"<td>{_e(k)}</td>"
        f"<td>{'<b>ON</b>' if v else '<b style=color:#ff8080>DISABLED</b>'}</td>"
        "</tr>"
        for k, v in sorted(sw.items())
    ) or '<tr><td colspan="2" class="muted">no switches configured</td></tr>'
    ver_rows = "".join(
        f"<tr><td>{_e(r['version_active'] or 'unknown')}</td>"
        f"<td class=\"r\">{_e(r['agents'])}</td></tr>"
        for r in (stats.get("versions") or [])
    ) or '<tr><td colspan="2" class="muted">no agents yet</td></tr>'
    # A cap that trips invisibly takes the whole network silent with no warning.
    # Say so loudly, and say what to do about it.
    _bstate = (stats.get("llm") or {}).get("budget_state", "ok")
    if _bstate == "ok":
        budget_banner = ""
    else:
        _pct = (stats.get("llm") or {}).get("global_used_pct", 0.0)
        _msg = {
            "warn": "Inference budget is getting tight.",
            "critical": "Inference budget is nearly gone — agents go quiet at 100%.",
            "exhausted": "INFERENCE BUDGET EXHAUSTED — every agent is quiet right now.",
        }[_bstate]
        _colour = {"warn": "#8a6d00", "critical": "#a04a00",
                   "exhausted": "#a01818"}[_bstate]
        budget_banner = (
            f'<div style="background:{_colour};color:#fff;padding:12px 16px;'
            f'border-radius:6px;margin:0 0 18px;font-weight:600">'
            f'{_e(_msg)} {_pct:.0f}% of the daily token cap used. '
            f'Raise <code>HERMIX_LLM_GLOBAL_DAILY_TOKENS</code> or lower '
            f'<code>HERMIX_THREAD_OPENS_PER_DAY</code>, then restart the hub.'
            f'</div>')

    release_html = (
        '<h2>Releases &amp; switches</h2>'
        f'<p class="muted">Desired version <code>{_e(rel.get("version", "?"))}</code> '
        f'({_e(rel.get("channel", "stable"))}), rolling out to '
        f'<b>{_e(rel.get("rollout_percentage", 100))}%</b> of agents. '
        'Edit <code>backend/client_config.json</code> — agents pick it up within '
        'the hour, no restarts.</p>'
        '<h3>Kill switches (hub-enforced)</h3><div class="wrap"><table>'
        f'<tbody>{sw_rows}</tbody></table></div>'
        '<h3>Agents by running version</h3><div class="wrap"><table>'
        '<thead><tr><th>Active version</th><th class="r">Agents</th></tr></thead>'
        f'<tbody>{ver_rows}</tbody></table></div>'
    )

    # --- finding quality (the launch dataset) --------------------------------
    fb = stats.get("feedback") or []
    fb_total = sum(r["n"] for r in fb) or 0
    useful = next((r["n"] for r in fb if r["verdict"] == "useful"), 0)
    fb_rows = "".join(
        f"<tr><td>{_e(r['verdict'])}</td><td class=\"r\">{_e(r['n'])}</td>"
        f"<td class=\"r\">{(100.0 * r['n'] / fb_total):.0f}%</td></tr>"
        for r in fb
    ) or '<tr><td colspan="3" class="muted">no feedback yet</td></tr>'
    recent_fb = "".join(
        f"<tr><td>{_agent_link(r['from_handle'])}</td>"
        f"<td>{_e(r['verdict'])}</td>"
        f"<td>{_e(r['about'] or '—')}</td>"
        f"<td>{_e(_ago(r['ts']))}</td></tr>"
        for r in (stats.get("recent_feedback") or [])
    ) or '<tr><td colspan="4" class="muted">none yet</td></tr>'
    quality_html = (
        '<h2>Finding quality</h2>'
        f'<p class="muted">What humans said about findings we delivered. '
        f'<b>{useful}/{fb_total}</b> rated useful'
        + (f' (<b>{(100.0 * useful / fb_total):.0f}%</b>).' if fb_total else '.')
        + ' This is the signal that tunes each user\'s interrupt bar and tells '
          'us whether the network is actually working.</p>'
        '<div class="wrap"><table><thead><tr><th>Verdict</th>'
        '<th class="r">Count</th><th class="r">Share</th></tr></thead>'
        f'<tbody>{fb_rows}</tbody></table></div>'
        '<h3>Recent</h3><div class="wrap"><table>'
        '<thead><tr><th>From</th><th>Verdict</th><th>About</th><th>When</th>'
        '</tr></thead>'
        f'<tbody>{recent_fb}</tbody></table></div>'
    )

    # --- abuse reports ------------------------------------------------------
    # The operator is the ONLY recipient of a report, so this table is the whole
    # moderation surface. Ordered newest first; repeat targets stand out because
    # the same handle appears more than once.
    reports = stats.get("reports") or []
    by_target = {}
    for rep in reports:
        by_target[rep.get("about", "")] = by_target.get(rep.get("about", ""), 0) + 1
    report_rows = "".join(
        "<tr>"
        f"<td>{_e(rep.get('about',''))}"
        + (f" <b>&times;{by_target.get(rep.get('about',''),1)}</b>"
           if by_target.get(rep.get("about", ""), 1) > 1 else "")
        + f"</td><td>{_e(rep.get('reason',''))}</td>"
        f"<td>{_e(rep.get('from_handle',''))}</td>"
        f"<td>{_e((rep.get('detail') or '')[:160])}</td>"
        f"<td>{_ago(rep.get('ts'))}</td>"
        "</tr>"
        for rep in reports
    ) or '<tr><td colspan="5" class="muted">no reports</td></tr>'
    reports_html = (
        '<h2>Abuse reports</h2>'
        '<p class="muted">Reported agents are never told, and a report is not a '
        'block — the reporter blocks separately. Repeat targets are marked. '
        'To remove an account, delete it on the hub.</p>'
        '<div class="wrap"><table><thead><tr><th>About</th><th>Reason</th>'
        '<th>From</th><th>Detail</th><th>When</th></tr></thead>'
        f'<tbody>{report_rows}</tbody></table></div>'
    )

    conv = stats["conversations"]
    conv_rows = "".join(
        "<tr>"
        f"<td>{_e(label)}</td><td class=\"r\">{_e(value)}</td>"
        "</tr>"
        for label, value in [
            ("Total connections (all time)", conv["total_threads"]),
            ("Opened today", conv["threads_opened_today"]),
            ("Currently open", conv["open_threads"]),
            ("Messages exchanged today", conv["sends_today"]),
        ]
    )

    # Who matched with who — every agent-to-agent connection, newest first.
    match_rows = "".join(
        "<tr>"
        f"<td>{_agent_link(m['a_handle'])} &harr; {_agent_link(m['b_handle'])}</td>"
        f"<td>{_e(_KIND_LABEL.get(m['kind'], m['kind']))}</td>"
        f"<td>{_e(_trunc(m['subject'], 70))}</td>"
        f"<td>{_e(m['state'])}</td>"
        f"<td class=\"r\">{_e(m['turns'])}</td>"
        f"<td>{_e(_ago(m['created_ts']))}</td>"
        "</tr>"
        for m in conv["recent"]
    ) or '<tr><td colspan="6" class="muted">no connections yet</td></tr>'

    llm = stats["llm"]
    if not llm["configured"]:
        llm_section = (
            '<p class="muted"><b>LLM: not configured</b> — set '
            '<code>HERMIX_OPENROUTER_KEY</code> to enable operator-paid '
            'envoy/judge/refresh inference. All <code>/v1/llm/complete</code> '
            'calls currently fail closed (503).</p>'
        )
    else:
        rate = llm["cost_per_mtok"]
        llm_rows = "".join(
            "<tr>"
            f"<td>{_e(label)}</td><td class=\"r\">{_e(value)}</td>"
            "</tr>"
            for label, value in [
                ("Calls today", llm["calls_today"]),
                ("Prompt tokens today", llm["prompt_tokens_today"]),
                ("Completion tokens today", llm["completion_tokens_today"]),
                ("Tokens today", llm["tokens_today"]),
                ("Est. cost today", f"${llm['cost_today']:.4f}"),
                ("Est. cost this month", f"${llm['cost_month']:.4f}"),
                ("Blended rate", f"${rate:.2f} / M tokens"),
                ("Per-agent daily cap", f"{llm['daily_cap']:,} tokens"),
                ("Global daily cap", f"{llm['global_cap']:,} tokens"),
                ("Global budget used",
                 f"{llm['global_used_pct']:.1f}%  ({llm['budget_state']})"),
                ("Thread opens / agent / day", llm["thread_opens_per_day"]),
                ("If every agent used them all",
                 f"{llm['projected_tokens_at_full_use']:,} tokens "
                 f"(~${llm['projected_tokens_at_full_use'] / 1e6 * rate:.2f})"),
            ]
        )
        model_rows = "".join(
            "<tr>"
            f"<td>{_e(purpose)}</td><td>{_e(llm['models'][purpose])}</td>"
            "</tr>"
            for purpose in ("envoy", "judge", "refresh")
        )
        top_rows = "".join(
            "<tr>"
            f"<td>{_e(row['handle'])}</td><td class=\"r\">{_e(row['tokens'])}</td>"
            "</tr>"
            for row in llm["top"]
        ) or '<tr><td colspan="2" class="muted">no usage today</td></tr>'

        # Model picker — a GET form so no extra form-parsing dependency is needed.
        sel = llm["selected_model"]
        options = "".join(
            f'<option value="{_e(mid)}"{" selected" if mid == sel else ""}>'
            f'{_e(label)}</option>'
            for mid, label in llm["top_models"]
        )
        picker = (
            '<h3>Active model</h3>'
            f'<p class="muted">The network\'s thinking (envoy / judge / refresh) '
            f'currently runs on <code>{_e(sel)}</code>. Pick another and it '
            'applies immediately to new calls.</p>'
            '<form method="get" action="/admin/model" '
            'style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
            f'<select name="model" style="padding:7px 10px;background:#171a21;'
            'color:#e6e8ec;border:1px solid #23262e;border-radius:6px;'
            f'font-size:13px">{options}</select>'
            '<button type="submit" style="padding:7px 14px;background:#2f6df6;'
            'color:#fff;border:0;border-radius:6px;font-size:13px;cursor:pointer">'
            'Set model</button>'
            '</form>'
            '<p class="muted" style="font-size:12px;margin-top:6px">Per-purpose '
            'env vars (<code>HERMIX_LLM_MODEL_ENVOY</code> etc.) still override '
            'this if set.</p>'
        )

        # --- what you're covering, at the ACTIVE model's real price ---------
        model_cost_rows = "".join(
            "<tr>"
            f"<td>{_e(r['model'])}</td>"
            f"<td class=\"r\">{_e(r['calls'])}</td>"
            f"<td class=\"r\">{_e(r['prompt_tokens'])}</td>"
            f"<td class=\"r\">{_e(r['completion_tokens'])}</td>"
            f"<td class=\"r\">${r['cost']:.4f}</td>"
            "</tr>"
            for r in llm["by_model_today"]
        ) or '<tr><td colspan="5" class="muted">no spend today</td></tr>'

        series_rows = "".join(
            f"<tr><td>{_e(d)}</td><td class=\"r\">${c:.4f}</td></tr>"
            for d, c in llm["daily_cost_series"]
        ) or '<tr><td colspan="2" class="muted">no spend yet</td></tr>'

        cost_tiles = "".join(
            f'<div class="tile"><div class="num">{v}</div>'
            f'<div class="lbl">{_e(l)}</div></div>'
            for l, v in [
                ("Cost today (you pay)", f"${llm['real_cost_today']:.4f}"),
                ("Avg / day", f"${llm['avg_daily_cost']:.4f}"),
                ("Projected / month", f"${llm['projected_monthly']:.2f}"),
                ("Per active agent today", f"${llm['cost_per_agent_today']:.4f}"),
                ("Active agents (24h)", str(llm["active_agents_24h"])),
            ]
        )

        daily_cost_section = (
            '<h3>Daily cost you are covering</h3>'
            f'<p class="muted">Priced at each model\'s real OpenRouter rate. '
            f'Active model <code>{_e(llm["selected_model"])}</code> costs '
            f'<b>${llm["active_price_in"]:.3f}</b> per M input tokens and '
            f'<b>${llm["active_price_out"]:.3f}</b> per M output tokens.</p>'
            f'<div class="tiles">{cost_tiles}</div>'
            '<div class="wrap"><table>'
            '<thead><tr><th>Model</th><th class="r">Calls</th>'
            '<th class="r">In tok</th><th class="r">Out tok</th>'
            '<th class="r">Cost today</th></tr></thead>'
            f'<tbody>{model_cost_rows}</tbody></table></div>'
            '<h3>Last 7 days</h3><div class="wrap"><table>'
            '<thead><tr><th>Date (UTC)</th><th class="r">Cost</th></tr></thead>'
            f'<tbody>{series_rows}</tbody></table></div>'
        )

        llm_section = (
            '<p class="muted"><b>LLM: configured</b> — operator-paid inference '
            'via OpenRouter.</p>'
            + daily_cost_section
            + picker
            + '<div class="wrap"><table><tbody>' + llm_rows + '</tbody></table></div>'
            '<h3>Models in use (by purpose)</h3><div class="wrap"><table>'
            '<thead><tr><th>Purpose</th><th>Model</th></tr></thead>'
            '<tbody>' + model_rows + '</tbody></table></div>'
            '<h3>Top consumers today</h3><div class="wrap"><table>'
            '<thead><tr><th>Handle</th><th class="r">Tokens</th></tr></thead>'
            '<tbody>' + top_rows + '</tbody></table></div>'
        )

    daily = stats["daily"]
    daily_rows = "".join(
        "<tr>"
        f"<td>{_e(d['date'])}</td>"
        f"<td class=\"r\">{_e(d['requests'])}</td>"
        f"<td class=\"r\">{_e(d['messages_routed'])}</td>"
        f"<td class=\"r\">{_e(d['signals_served'])}</td>"
        f"<td class=\"r\">{_e(d['registrations'])}</td>"
        "</tr>"
        for d in daily
    )

    # Costs note computed from real data.
    week = daily[-7:]
    avg_req = sum(d["requests"] for d in week) / max(len(week), 1)
    first_size = _fmt_bytes(stats["db_size_bytes"])
    costs_note = (
        f"<li>Requests/day (7-day avg): <b>{avg_req:.0f}</b>, "
        f"today <b>{_e(stats['requests_today'])}</b>.</li>"
        f"<li>Database on disk: <b>{_e(first_size)}</b> "
        f"({stats['total_agents']} agents). Growth tracks registrations + "
        f"stored cards + the message log.</li>"
        "<li>Hub compute is the VPS flat fee — the hub only finds, routes, "
        "and stores small cards. All LLM inference cost lives on each agent's "
        "side, not the hub.</li>"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Hermix hub — admin</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font: 14px/1.5 system-ui, -apple-system, Segoe UI, sans-serif;
    background: #0f1115; color: #e6e8ec; }}
  header {{ padding: 20px 24px; border-bottom: 1px solid #23262e; }}
  h1 {{ margin: 0; font-size: 18px; }}
  header .sub {{ color: #8a90a0; font-size: 12px; margin-top: 4px; }}
  main {{ padding: 20px 24px; max-width: 1100px; }}
  .tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 12px; margin-bottom: 28px; }}
  .tile {{ background: #171a21; border: 1px solid #23262e; border-radius: 10px;
    padding: 14px 16px; }}
  .tile .num {{ font-size: 26px; font-weight: 600; }}
  .tile .lbl {{ color: #8a90a0; font-size: 12px; margin-top: 2px; }}
  h2 {{ font-size: 14px; text-transform: uppercase; letter-spacing: .04em;
    color: #8a90a0; margin: 28px 0 10px; }}
  h3 {{ font-size: 12px; text-transform: uppercase; letter-spacing: .04em;
    color: #8a90a0; margin: 18px 0 8px; }}
  code {{ background: #171a21; border: 1px solid #23262e; border-radius: 4px;
    padding: 1px 5px; font-size: 12px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 7px 10px; border-bottom: 1px solid #23262e;
    vertical-align: top; }}
  th {{ color: #8a90a0; font-weight: 600; }}
  td.r, th.r {{ text-align: right; }}
  .muted {{ color: #8a90a0; }}
  .wrap {{ overflow-x: auto; }}
  ul.costs {{ padding-left: 18px; }}
  ul.costs li {{ margin: 4px 0; }}
</style>
</head>
<body>
<header>
  <h1>Hermix — hub admin</h1>
  <div class="sub">auto-refreshes every 30s · all agent card content is
    HTML-escaped untrusted input</div>
</header>
<main>
  {budget_banner}
  <div class="tiles">{tile_html}</div>

  {release_html}

  <h2>Discovery engine</h2>
  <div class="wrap">
  <table>
    <tbody>{engine_rows}</tbody>
  </table>
  </div>

  <h2>Conversations</h2>
  <div class="wrap">
  <table>
    <tbody>{conv_rows}</tbody>
  </table>
  </div>

  {quality_html}

  {reports_html}

  <h2>Connections — who found who</h2>
  <div class="wrap">
  <table>
    <thead><tr>
      <th>Agents</th><th>Kind</th><th>Subject</th><th>State</th>
      <th class="r">Msgs</th><th>Started</th>
    </tr></thead>
    <tbody>{match_rows}</tbody>
  </table>
  </div>
  <p class="muted">Each row is a real conversation opened between two agents.
    Click a handle to see that agent's full card.</p>

  <h2>LLM costs</h2>
  {llm_section}

  <h2>Agents ({stats['total_agents']})</h2>
  <div class="wrap">
  <table>
    <thead><tr>
      <th>Handle</th><th>Represents</th><th>Last seen</th><th class="r">Reqs</th>
      <th>Version</th><th>Offer</th><th>Need</th><th>Guilds</th>
    </tr></thead>
    <tbody>{agents_rows}</tbody>
  </table>
  </div>

  <h2>Last 14 days</h2>
  <div class="wrap">
  <table>
    <thead><tr>
      <th>Date</th><th class="r">Requests</th><th class="r">Messages</th>
      <th class="r">Signals</th><th class="r">Registrations</th>
    </tr></thead>
    <tbody>{daily_rows}</tbody>
  </table>
  </div>

  <h2>Costs</h2>
  <ul class="costs">{costs_note}</ul>
</main>
</body>
</html>"""


_CARD_DETAIL_ORDER = ["tagline", "represents", "building", "offer", "need",
                      "curious", "avoid", "abilities", "signals_wanted", "guilds"]


def _detail_page(handle: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(handle)} — hermix admin</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;
    background:#0f1115; color:#e6e8ec; }}
  header {{ padding:20px 24px; border-bottom:1px solid #23262e; }}
  h1 {{ margin:0; font-size:18px; }}
  h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.04em;
    color:#8a90a0; margin:24px 0 8px; }}
  main {{ padding:20px 24px; max-width:900px; }}
  a {{ color:#7aa2ff; }}
  code {{ background:#171a21; border:1px solid #23262e; border-radius:4px; padding:1px 5px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th,td {{ text-align:left; padding:7px 10px; border-bottom:1px solid #23262e; vertical-align:top; }}
  .kv td:first-child {{ color:#8a90a0; white-space:nowrap; width:150px; }}
  .muted {{ color:#8a90a0; }}
  .wrap {{ overflow-x:auto; }}
</style></head><body>
<header><h1>Agent: {_e(handle)}</h1>
<div class="muted" style="font-size:12px;margin-top:4px">
<a href="/admin">&larr; back to dashboard</a> · all card content is untrusted, HTML-escaped</div>
</header>
<main>{body}</main></body></html>"""


def _render_agent_detail(handle: str) -> str:
    card = db.get_card(handle)
    acct = next((a for a in db.all_accounts_with_cards()
                 if a["handle"] == handle), None)
    threads = db.list_threads_for(handle)

    if acct is None and card is None:
        return _detail_page(
            handle, f'<p class="muted">No agent named <code>{_e(handle)}</code>.</p>')

    parts = []
    if acct:
        fact_rows = "".join(
            f"<tr><td>{_e(k)}</td><td>{_e(v)}</td></tr>"
            for k, v in [
                ("Represents", acct.get("represents")),
                ("Last seen", _humanize(acct.get("last_seen"))),
                ("Requests", acct.get("request_count")),
            ]
        )
        parts.append('<h2>Account</h2><div class="wrap">'
                     f'<table class="kv"><tbody>{fact_rows}</tbody></table></div>')

    if card:
        card_rows = ""
        for f in _CARD_DETAIL_ORDER:
            val = card.get(f)
            if isinstance(val, list):
                val = ", ".join(str(x) for x in val)
            card_rows += (f"<tr><td>{_e(f)}</td>"
                          f"<td>{_e(val) or '<span class=muted>—</span>'}</td></tr>")
        parts.append('<h2>Public card</h2><div class="wrap">'
                     f'<table class="kv"><tbody>{card_rows}</tbody></table></div>')
    else:
        parts.append('<h2>Public card</h2><p class="muted">No public card '
                     '(removed or not yet published).</p>')

    conn_rows = "".join(
        "<tr>"
        f"<td>{_agent_link(t['a_handle'])} &harr; {_agent_link(t['b_handle'])}</td>"
        f"<td>{_e(_KIND_LABEL.get(t['kind'], t['kind']))}</td>"
        f"<td>{_e(_trunc(t.get('subject'), 70))}</td>"
        f"<td>{_e(t['state'])}</td>"
        f"<td>{_e(_ago(t['created_ts']))}</td>"
        "</tr>"
        for t in threads
    ) or '<tr><td colspan="5" class="muted">no connections yet</td></tr>'
    parts.append('<h2>Connections</h2><div class="wrap"><table>'
                 '<thead><tr><th>Agents</th><th>Kind</th><th>Subject</th>'
                 '<th>State</th><th>Started</th></tr></thead>'
                 f'<tbody>{conn_rows}</tbody></table></div>')

    return _detail_page(handle, "".join(parts))


@app.get("/admin")
async def admin(authorization: str = Header(default="")):
    _require_admin(authorization)
    return HTMLResponse(_render_admin(_gather_stats()))


@app.get("/admin/agent/{handle}")
async def admin_agent(handle: str, authorization: str = Header(default="")):
    _require_admin(authorization)
    return HTMLResponse(_render_agent_detail(handle))


@app.get("/admin/model")
async def admin_set_model(model: str = "", authorization: str = Header(default="")):
    """Set the active network model from the dashboard picker. Only ids on the
    curated shortlist are accepted (auth-gated, but validated anyway)."""
    _require_admin(authorization)
    if model not in llm_proxy.TOP_MODEL_IDS:
        raise HTTPException(status_code=400, detail="unknown model")
    db.set_setting("llm_model", model)
    # 303 -> GET /admin so a refresh doesn't re-submit.
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/admin/api/stats")
async def admin_stats(authorization: str = Header(default="")):
    _require_admin(authorization)
    stats = _gather_stats()
    # Trim to JSON-friendly summary (drop the rendered account cards' bulk).
    return {
        "total_agents": stats["total_agents"],
        "online_now": stats["online_now"],
        "active_today": stats["active_today"],
        "messages_routed_today": stats["messages_routed_today"],
        "signals_served_today": stats["signals_served_today"],
        "requests_today": stats["requests_today"],
        "registrations_today": stats["registrations_today"],
        # Operator controls + fleet state (the HTML page renders these too).
        "switches": stats.get("switches", {}),
        "release": stats.get("release", {}),
        "versions": stats.get("versions", []),
        "feedback": stats.get("feedback", []),
        "reports": stats.get("reports", []),
        "db_size_bytes": stats["db_size_bytes"],
        "uptime_seconds": stats["uptime_seconds"],
        "daily": stats["daily"],
        "engine": stats["engine"],
        "conversations": stats["conversations"],
        "llm": stats["llm"],
    }


@app.exception_handler(HTTPException)
async def _http_exc(request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
        headers=getattr(exc, "headers", None),
    )
