"""The response contract: nothing reaches a human except through a validated packet.

Why this exists
---------------
Before this module, the judge wrote a short prose ``pitch`` and the formatter
printed it. That is fine until you ask the only question that matters:

    "Can every sentence the user reads be traced to something real?"

It could not. A model asked to write *compelling* copy is under quiet pressure
to strengthen weak evidence — to turn "their agent said she might be free" into
"she's available", a semantic similarity into "a great fit", or a next step we
merely *could* take into one already taken. None of that is lying on purpose;
it is what fluent writing does to hedged material.

So the model no longer writes what the user reads. It produces **auditable
judgement data** — claims, each carrying an evidence state and the source it
came from — and a deterministic compiler (``render.py``) turns that into prose.
The compiler owns attribution, uncertainty wording, ordering and length. The
model owns judgement. Neither can quietly do the other's job.

Fail closed
-----------
``validate()`` returns problems; ``ensure_valid()`` raises. Callers must treat
an invalid packet as *silence*, never as "send it anyway" — an unverifiable
claim is worse than no message, because the whole product is a claim about
trustworthiness. The one exception is a REQUESTED answer, which must still
reach the human; the caller degrades it to a conservative deterministic
summary rather than dropping it (see matchmaker).

Source identifiers
------------------
Every claim names where it came from:

    ``card:ours``        the user's own approved public card
    ``card:theirs``      the counterpart's public card
    ``ring1:<n>``        an approved Ring-1 fact, by index
    ``turn:<n>``         a specific turn in the counterpart conversation
    ``system:<name>``    a deterministic fact we computed (e.g. no reply)

``ring0:*`` is rejected outright. Ring 0 may *inform* a local judgement, but it
must never be the stated reason for anything a counterpart or the hub could
see, and it must never appear in delivered prose. That is the membrane.
"""
import re

# Bumped whenever the packet shape or the compiler's output changes in a way a
# reader would notice. Recorded in telemetry so a quality regression can be
# traced to a specific version rather than guessed at.
CONTRACT_VERSION = "1.0.0"

# --------------------------------------------------------------------------- #
# Enumerations — closed sets, on purpose
# --------------------------------------------------------------------------- #
RESPONSE_TYPES = (
    "finding",          # unsolicited: we went looking and found something
    "ask_result",       # the human asked; this is the answer
    "checkin",          # one-time proof of life
    "reveal_request",   # someone wants an introduction — preview only
    "reveal_outcome",   # what happened after the human decided
    "safety",           # block / unblock / report / injection termination
    "error",            # hub down, budget out, paused, degraded
)

# How strongly a claim is supported. Deliberately NOT a boolean: "verified"
# collapses four very different situations into one, and the difference between
# "their profile says" and "they told us" and "we established together" is
# exactly what a user needs to judge risk.
EVIDENCE_STATES = (
    "profile_only",             # only their public card says so
    "counterpart_claim",        # their agent asserted it in conversation
    "conversation_established",  # both sides worked it out in the transcript
    "independently_verified",   # we checked it against something else
    "system_fact",              # we computed it (turn counts, no reply, time)
    "unknown",                  # explicitly not established
)

# Claims we will let the compiler state without hedging. Everything else gets
# attribution or an uncertainty marker.
_UNHEDGED_STATES = ("conversation_established", "independently_verified",
                    "system_fact")

# Actions must be things Hermix can actually do. A next step the product cannot
# perform is worse than no next step: it teaches the user the agent is unreliable.
NEXT_ACTIONS = {
    "ask_followup":  "Ask a follow-up question",
    "ask_budget":    "Ask about budget and timing",
    "request_intro": "Preview an introduction",
    "compare_cost":  "Compare it against what you pay now",
    "send_sample":   "Send a sample of your work",
    "wait":          "Wait until something changes",
    "dismiss":       "Leave it here",
    "retry_later":   "Try them again later",
    "approve_reveal": "Approve the introduction",
    "decline_reveal": "Decline",
    "unblock":       "Undo the block",
    "nothing":       "Nothing — no action needed",
}

_SOURCE_PATTERNS = (
    re.compile(r"^card:(ours|theirs)$"),
    re.compile(r"^ring1:\d+$"),
    re.compile(r"^turn:\d+$"),
    re.compile(r"^system:[a-z_]+$"),
)

# Words that describe our machinery rather than the user's life. A human-facing
# response containing these has leaked the implementation. Kept in sync with
# tests/test_vocabulary.py, which owns the same rule for skills and commands.
BANNED_TERMS = (
    "match", "matchmaking", "matchmaker", "signal", "score", "embedding",
    "cron", "pipeline", "handshake", "thread id", "thread_id", "hub",
    "envoy", "dossier", "ring 0", "ring0", "ring 1", "ring1", "verdict",
    "payload", "packet", "corpus", "llm", "prompt", "candidate",
)

# Length budgets per response type, in words. Evaluation targets rather than
# truncation points — a safety clarification may exceed them, and the validator
# reports rather than trims.
WORD_TARGETS = {
    "finding":        (25, 90),
    "finding_batch":  (60, 220),
    "ask_result":     (20, 160),
    "checkin":        (20, 90),
    "reveal_request": (20, 100),
    "reveal_outcome": (10, 90),
    "safety":         (8, 80),
    "error":          (8, 60),
}


class PacketError(ValueError):
    """An invalid packet. Callers treat this as silence, never as 'send anyway'."""


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #
def claim(text, evidence_state, source_ids=None):
    """One factual assertion plus where it came from.

    A claim with no source is only legal when it is a ``system_fact`` — something
    we computed rather than learned. Everything else must point at a card, an
    approved Ring-1 fact, or a conversation turn.
    """
    return {
        "text": str(text or "").strip(),
        "evidence_state": evidence_state,
        "source_ids": list(source_ids or []),
    }


def packet(response_type, *, finding_id="", counterpart=None, user_relevance=None,
           claims=None, uncertainties=None, next_actions=None, delivery=None,
           contact=None, system=None):
    """Assemble a response packet. Does not validate — call validate/ensure_valid."""
    return {
        "contract_version": CONTRACT_VERSION,
        "response_type": response_type,
        "finding_id": finding_id or "",
        "counterpart": dict(counterpart or {}),
        "user_relevance": dict(user_relevance or {}),
        "claims": [dict(c) for c in (claims or [])],
        "uncertainties": [str(u).strip() for u in (uncertainties or []) if str(u).strip()],
        "next_actions": [dict(a) for a in (next_actions or [])],
        "delivery": {
            "requested": bool((delivery or {}).get("requested")),
            "redelivery": bool((delivery or {}).get("redelivery")),
            "batch_position": int((delivery or {}).get("batch_position", 1)),
            "batch_size": int((delivery or {}).get("batch_size", 1)),
        },
        # Only legal on reveal packets; validated below.
        "contact": dict(contact or {}),
        # Free-form deterministic facts for error/safety renderers (never prose
        # written by a model).
        "system": dict(system or {}),
    }


def action(action_id, available=True, label=None):
    return {
        "id": action_id,
        "label": label or NEXT_ACTIONS.get(action_id, action_id),
        "available": bool(available),
    }


# --------------------------------------------------------------------------- #
# Validation — the gate
# --------------------------------------------------------------------------- #
def _valid_source(sid):
    return any(p.match(sid or "") for p in _SOURCE_PATTERNS)


def validate(p, *, known_sources=None, forbidden_strings=()):
    """Return a list of problems. Empty list means the packet may be rendered.

    ``known_sources`` — if given, every cited source must exist in it. This is
    what stops a model inventing ``turn:9`` for a four-turn conversation, which
    is the most likely way a fabricated claim would otherwise slip through with
    a plausible-looking citation attached.

    ``forbidden_strings`` — Ring-0 sentinels and contact values. Any appearance
    anywhere in the packet is a hard failure.
    """
    problems = []

    if not isinstance(p, dict):
        return ["packet is not a dict"]

    rtype = p.get("response_type")
    if rtype not in RESPONSE_TYPES:
        problems.append(f"unknown response_type: {rtype!r}")

    # --- claims ------------------------------------------------------------
    for i, c in enumerate(p.get("claims") or []):
        where = f"claims[{i}]"
        text = (c.get("text") or "").strip()
        state = c.get("evidence_state")
        sources = c.get("source_ids") or []

        if not text:
            problems.append(f"{where}: empty text")
        if state not in EVIDENCE_STATES:
            problems.append(f"{where}: unknown evidence_state {state!r}")
        if not sources and state != "system_fact":
            problems.append(
                f"{where}: no source, and evidence_state is {state!r} — only "
                "system_fact may be unsourced")
        for sid in sources:
            if not _valid_source(sid):
                problems.append(f"{where}: malformed source id {sid!r}")
            elif sid.startswith("ring0"):
                problems.append(f"{where}: Ring 0 may never be a delivered source")
            elif known_sources is not None and sid not in known_sources:
                problems.append(
                    f"{where}: cites {sid!r}, which does not exist in this "
                    "scenario — claim is unsupported")

    # --- user relevance ----------------------------------------------------
    rel = p.get("user_relevance") or {}
    if rel.get("summary"):
        rel_sources = rel.get("source_ids") or []
        if not rel_sources:
            problems.append("user_relevance: stated without a source")
        for sid in rel_sources:
            if not _valid_source(sid):
                problems.append(f"user_relevance: malformed source id {sid!r}")
            elif known_sources is not None and sid not in known_sources:
                problems.append(f"user_relevance: cites unknown source {sid!r}")

    # --- actions -----------------------------------------------------------
    actions = p.get("next_actions") or []
    for i, a in enumerate(actions):
        aid = a.get("id")
        if aid not in NEXT_ACTIONS:
            problems.append(f"next_actions[{i}]: {aid!r} is not a real capability")
    offered = [a for a in actions if a.get("available")]
    if rtype in ("finding", "ask_result") and not offered:
        problems.append(f"{rtype}: no available next action")

    # --- contact -----------------------------------------------------------
    contact = p.get("contact") or {}
    if contact and rtype not in ("reveal_request", "reveal_outcome"):
        problems.append(
            f"contact details present on a {rtype} packet — they may only "
            "appear in a reveal, after explicit approval")

    # --- reveal-specific ---------------------------------------------------
    if rtype == "reveal_request":
        if not contact:
            problems.append("reveal_request: must state exactly what would be shared")
        if p.get("system", {}).get("released"):
            problems.append("reveal_request: must never imply anything was sent")

    # --- privacy sentinels -------------------------------------------------
    blob = _all_text(p).lower()
    for secret in forbidden_strings:
        s = str(secret or "").strip().lower()
        if s and s in blob:
            problems.append(f"private value leaked into the packet: {secret!r}")

    # --- delivery ----------------------------------------------------------
    d = p.get("delivery") or {}
    if d.get("batch_size", 1) < d.get("batch_position", 1):
        problems.append("delivery: batch_position beyond batch_size")

    return problems


def ensure_valid(p, **kw):
    problems = validate(p, **kw)
    if problems:
        raise PacketError("; ".join(problems))
    return p


def _all_text(p):
    """Every human-readable string in the packet, for sentinel scanning."""
    parts = []

    def walk(v):
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    walk(p)
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Helpers shared with the compiler and the evaluator
# --------------------------------------------------------------------------- #
def source_map(*, ring1=(), turns=0, their_card=True, system_facts=()):
    """The set of sources a given scenario legitimately has.

    Passed to validate() as ``known_sources`` so a citation to something that
    does not exist is caught rather than trusted.
    """
    ids = {"card:ours"}
    if their_card:
        ids.add("card:theirs")
    ids.update(f"ring1:{i}" for i in range(len(ring1)))
    ids.update(f"turn:{i}" for i in range(1, int(turns) + 1))
    ids.update(f"system:{name}" for name in system_facts)
    return ids


def needs_attribution(state):
    """True when the compiler must say who claimed it rather than stating it."""
    return state not in _UNHEDGED_STATES


def word_count(text):
    return len([w for w in re.split(r"\s+", str(text or "")) if w])


def banned_terms_in(text):
    """Banned machinery vocabulary present in delivered prose, as whole words.

    Substring matching would fire on ordinary English — "matches" inside
    "watchmaker", "hub" inside "hubbub" — so this is word-boundary based. The
    handle-like forms (thread_id) are checked as literals since they cannot
    occur naturally.
    """
    low = str(text or "").lower()
    hits = []
    for term in BANNED_TERMS:
        if " " in term or "_" in term:
            if term in low:
                hits.append(term)
        elif re.search(r"\b" + re.escape(term) + r"(es|s|ing|ed)?\b", low):
            hits.append(term)
    return sorted(set(hits))
