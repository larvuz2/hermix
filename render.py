"""The deterministic response compiler: packets in, the user's agent's voice out.

This module is the ONLY thing that turns judgement into prose. No model writes
what the human reads. That boundary is the whole point — see response.py for
why — and it buys three properties nothing else could:

  * Attribution cannot be lost. A counterpart's claim renders as "her agent
    said…" every single time, because a function decides that, not a writer
    who might feel the sentence flows better without it.
  * Uncertainty cannot be dropped. If the packet says budget is unknown, the
    sentence appears. There is no temperature setting that can omit it.
  * Length and shape are stable. Users learn the format once.

What the compiler deliberately does NOT do is sound identical every time in the
way a template does. The old formatter opened every message with a branded
banner and closed every item with the same five-option menu; that reads as
software, and software is easy to ignore. Here the opening adapts to what was
actually found and the close is a real question about a real next step.

The principal Hermes agent may adjust surface style when it relays this (a
contraction, a transition, "Found…" instead of "I found…"). It must not rewrite
factual clauses; skills/hermix-delivery states that rule for the agent.
"""
import re

from . import response as R
from . import sanitize

# Bumped when rendered output changes in a way a reader would notice. Paired
# with response.CONTRACT_VERSION in telemetry so a quality regression can be
# attributed to a specific compiler rather than guessed at.
COMPILER_VERSION = "1.0.0"

# How a claim is introduced, per evidence state. "conversation_established" and
# above are stated plainly because both agents worked them out; everything else
# names who is asserting it. `{who}` is the counterpart's display name.
_ATTRIBUTION = {
    "profile_only":            "{who}'s profile says",
    "counterpart_claim":       "{who}'s agent said",
    "unknown":                 "I could not establish whether",
    "conversation_established": "",
    "independently_verified":  "",
    "system_fact":             "",
}


def _clean(text, limit=300):
    return sanitize.clean_text(str(text or "").strip(), max_len=limit)


def _display(counterpart):
    """Prefer a human first name; fall back to the handle without the @."""
    c = counterpart or {}
    who = (c.get("display") or "").strip()
    if who:
        return _clean(who, 40)
    handle = (c.get("handle") or "").strip()
    return _clean(handle.split("-")[0].capitalize() or "They", 40)


def _sentence(text):
    """One clause, ending in exactly one full stop."""
    t = _clean(text).rstrip()
    if not t:
        return ""
    if t[-1] not in ".!?":
        t += "."
    return t


def _lower_first(text):
    t = str(text or "").strip()
    return (t[0].lower() + t[1:]) if t else t


def _upper_first(text):
    """Claim texts are authored lowercase so they can follow an attribution
    lead ("her agent said she is…"). When there is no lead the claim starts the
    sentence, so it has to be capitalised — otherwise a plainly-stated fact
    renders mid-paragraph in lowercase and reads as broken."""
    t = str(text or "").strip()
    return (t[0].upper() + t[1:]) if t else t


def _role(text):
    """Just who they are, from the hub's longer reason string.

    That string is assembled for scoring, not for reading — "a game-studio
    founder — offers playtesting network, unity tooling, grants intel". Splicing
    the whole thing in produced a second em-dash inside our own em-dash clause
    and then truncated mid-word. Take the leading role phrase and stop.
    """
    t = _clean(text, 200)
    for sep in (" — ", " – ", " - ", " · ", ";"):
        if sep in t:
            t = t.split(sep, 1)[0]
    if len(t) > 64:
        cut = t[:64].rsplit(" ", 1)[0]
        t = cut or t[:64]
    return t.rstrip(" ,.")


def _render_claim(c, who):
    """One claim as a sentence, carrying its attribution."""
    text = _clean(c.get("text"))
    if not text:
        return ""
    state = c.get("evidence_state")
    lead = _ATTRIBUTION.get(state, "")
    if not lead:
        return _sentence(_upper_first(text))
    return _sentence(f"{lead.format(who=who)} {_lower_first(text)}")


def _close(actions, who=None):
    """One question about one real next step, plus a graceful way out.

    Never "let me know what you think" — a close the product cannot act on
    teaches the user their answer does not matter.
    """
    available = [a for a in (actions or []) if a.get("available")]
    if not available:
        return ""
    primary = [a for a in available if a.get("id") != "dismiss"]
    if not primary:
        return "Happy to leave it there unless you want something."

    labels = [_lower_first(_clean(a.get("label"), 60)) for a in primary[:2]]
    can_dismiss = any(a.get("id") == "dismiss" for a in available)

    # Grammar matters here: joining two options with ", or" and then appending
    # ", or leave it?" produced "A, or B, or leave it?" — three ors in one
    # sentence, which reads as a form rather than a question. The dismiss option
    # is the final alternative, so it owns the only "or".
    if len(labels) == 1:
        options = labels[0]
    else:
        options = f"{labels[0]}, {labels[1]}"
    if can_dismiss:
        return f"Want me to {options}, or leave it?"
    if len(labels) == 1:
        return f"Want me to {options}?"
    return f"Want me to {labels[0]}, or {labels[1]}?"


# --------------------------------------------------------------------------- #
# Per-type renderers
# --------------------------------------------------------------------------- #
def _engaged(p):
    """Did the counterpart actually say anything, or is this just their profile?

    The distinction decides the opening line. "I found someone worth your time"
    over a card nobody replied to is the single most damaging sentence this
    product could send: it converts a guess into a recommendation, and the user
    finds out only after spending their own time on it.
    """
    return any(c.get("evidence_state") in
               ("counterpart_claim", "conversation_established",
                "independently_verified")
               for c in (p.get("claims") or []))


def _render_finding(p, *, lead=True):
    who = _display(p.get("counterpart"))
    parts = []

    rel = (p.get("user_relevance") or {}).get("summary") or ""
    represents = _role((p.get("counterpart") or {}).get("represents"))

    # A standing intent means the human asked us to hunt for this specific
    # thing. Leading with their own words is both warmer and more useful than a
    # generic opener — it answers "why am I hearing about this?" immediately.
    intent = _clean((p.get("system") or {}).get("intent"), 90)

    if lead:
        if intent:
            parts.append(f'You asked me to look for "{intent}".')
        elif _engaged(p):
            parts.append("I found someone worth your time.")
        else:
            parts.append(f"{who}'s profile looks relevant, though I have not "
                         "confirmed anything.")

    # `represents` comes from the hub's own reason text and is a noun phrase of
    # unpredictable shape ("a game-studio founder — offers playtesting, grants
    # intel"). Splicing it into a sentence as a verb phrase produced things like
    # "Kip a game-studio founder", so it renders as an appositive and nothing
    # else. Relevance always gets its own sentence.
    if represents:
        parts.append(_sentence(f"{who} — {_lower_first(represents)}"))
    if rel:
        parts.append(_sentence(_upper_first(rel)))

    for c in p.get("claims") or []:
        s = _render_claim(c, who)
        if s:
            parts.append(s)

    # An uncertainty may arrive as a bare noun phrase ("budget and timing") or
    # as a whole sentence ("budget was not discussed"). Prefixing the second
    # kind produced "We did not get into budget was not discussed", so only
    # short noun phrases get the prefix; anything sentence-shaped stands alone.
    uncertainties = [_clean(u, 120) for u in (p.get("uncertainties") or []) if u]
    if uncertainties:
        phrases = [u for u in uncertainties[:2] if R.word_count(u) <= 4]
        sentences = [u for u in uncertainties[:2] if R.word_count(u) > 4]
        if phrases:
            parts.append(_sentence("We did not get into "
                                   + "; ".join(_lower_first(u) for u in phrases)))
        for u in sentences:
            parts.append(_sentence(_upper_first(u)))

    return " ".join(x for x in parts if x)


def _render_ask(p):
    """An answer, not a pitch. It reports what was asked and what came back —
    including "nothing", which is a successful response, not a failure."""
    who = _display(p.get("counterpart"))
    sysinfo = p.get("system") or {}
    question = _clean(sysinfo.get("question"), 200)
    parts = []

    if question:
        # NOT lower-cased: the question is the user's own words and usually
        # opens with a name. "You asked whether mira's workflow…" is worse than
        # a rare capital mid-sentence. Packet authors supply a clause, not an
        # interrogative — see docs/RESPONSE-SPRINT-CONTRACT.md.
        parts.append(_sentence(f"You asked whether {question}"))

    claims = p.get("claims") or []
    if not claims:
        parts.append(_sentence(
            f"I could not get an answer from {who}'s agent, and I would rather "
            "not guess"))
    for c in claims:
        s = _render_claim(c, who)
        if s:
            parts.append(s)

    for u in (p.get("uncertainties") or [])[:2]:
        parts.append(_sentence(_clean(u, 160)))

    return " ".join(x for x in parts if x)


def _render_checkin(p):
    """Proof of life. Real numbers, no invented activity, nothing to do."""
    s = p.get("system") or {}
    seen = int(s.get("seen_count") or 0)
    talked = int(s.get("talked_count") or 0)
    open_now = int(s.get("open_count") or 0)
    parts = ["Quick update, and nothing for you to do."]

    if talked:
        parts.append(_sentence(
            f"I have looked through {seen} profile"
            f"{'s' if seen != 1 else ''} and started {talked} conversation"
            f"{'s' if talked != 1 else ''}"))
        parts.append("Nothing is solid enough to bring you yet.")
    elif seen:
        parts.append(_sentence(
            f"I have looked through {seen} profile{'s' if seen != 1 else ''}, "
            "but found nobody worth starting a conversation with yet"))
        parts.append("The network is still small in your areas, which is "
                     "expected this early.")
    else:
        parts.append("I am set up and looking, but there is nobody in your "
                     "areas yet.")

    if open_now:
        parts.append(_sentence(
            f"{open_now} conversation{'s are' if open_now != 1 else ' is'} "
            "still going"))

    intents = [i for i in (s.get("intents") or []) if i]
    if intents:
        parts.append(_sentence(
            f"I am still looking for {_lower_first(_clean(intents[0], 90))}"))

    parts.append("I will come to you the moment there is something real.")
    return " ".join(parts)


def _render_reveal_request(p):
    """A preview. It must be unmistakable that nothing has moved yet."""
    who = _display(p.get("counterpart"))
    contact = p.get("contact") or {}
    lines = [f"{who}'s agent is open to an introduction.",
             "If you approve, I would share only:"]
    for key in ("name", "email", "handle", "phone", "url"):
        if contact.get(key):
            lines.append(f"  - {_clean(contact[key], 120)}")
    lines.append("Nothing has been shared yet.")
    lines.append("Approve this introduction, or decline?")
    return "\n".join(lines)


def _render_reveal_outcome(p):
    """Never imply the humans are connected until BOTH sides have approved."""
    who = _display(p.get("counterpart"))
    s = p.get("system") or {}
    if s.get("declined"):
        return (f"I declined the introduction to {who}. Nothing was shared, and "
                "they are not told why.")
    if s.get("released"):
        return (f"Both sides approved, so I have shared your details with {who}. "
                "They have yours and you have theirs.")
    return (f"You approved sharing those details. I have not released them yet "
            f"because {who} still has to approve on their side. I will tell you "
            "if they do.")


def _render_safety(p):
    s = p.get("system") or {}
    handle = _clean(s.get("handle"), 60)
    kind = s.get("kind")
    if kind == "block":
        return (f"Blocked @{handle}. They cannot start another conversation with "
                "you, and neither of you will be shown to the other. They are "
                f"not told. You can undo this with /hermix unblock {handle}.")
    if kind == "unblock":
        return (f"Unblocked @{handle}. They can reach you again, and you may "
                "each be shown to the other.")
    if kind == "report":
        return (f"Reported @{handle} to the operator. That does not block them — "
                f"if you also want them gone, run /hermix block {handle}.")
    if kind == "injection":
        return ("I stopped a conversation early: the other side was trying to "
                "get me to act on instructions rather than talk. Nothing of "
                "yours was shared.")
    return "Done."


def _render_error(p):
    """Say what happened and whether the user must act. Usually they need not."""
    s = p.get("system") or {}
    kind = s.get("kind")
    if kind == "budget":
        return ("I cannot keep conversations going right now — the shared model "
                "budget is unavailable. I will stay quiet rather than spend "
                "yours. Nothing for you to do.")
    if kind == "offline":
        return ("I cannot reach the network at the moment, so I have paused "
                "looking. I will pick it up again on my own.")
    if kind == "paused":
        return "I have stopped looking. Say resume when you want me to start again."
    if kind == "no_card":
        return ("I have not published anything for you yet, so nobody can find "
                "you. Want to set that up?")
    if kind == "degraded":
        return ("I am still looking, but with reduced ability to spot "
                "connections worded differently from yours.")
    if kind == "timeout":
        return ("I did not get an answer in time. I can try again later or "
                "leave it.")
    return "Something went wrong on my side. Nothing for you to do."


_RENDERERS = {
    "finding": _render_finding,
    "ask_result": _render_ask,
    "checkin": _render_checkin,
    "reveal_request": _render_reveal_request,
    "reveal_outcome": _render_reveal_outcome,
    "safety": _render_safety,
    "error": _render_error,
}


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def render(p):
    """One packet to prose. Raises PacketError if the packet is not valid.

    Callers treat that as silence — never as "render it anyway". An unsourced
    claim reaching a user is worse than saying nothing.
    """
    R.ensure_valid(p)
    fn = _RENDERERS.get(p.get("response_type"))
    if fn is None:
        raise R.PacketError(f"no renderer for {p.get('response_type')!r}")
    body = fn(p)

    close = ""
    if p.get("response_type") in ("finding", "ask_result"):
        close = _close(p.get("next_actions"), _display(p.get("counterpart")))
    return "\n\n".join(x for x in (body, close) if x).strip()


def render_batch(packets):
    """Several findings as ONE message: best first, one close, one invitation.

    The old formatter repeated a five-option feedback menu after every item,
    which made a three-finding message read like a form. The cost to the human
    is the interruption, so the batch earns exactly one closing question.
    """
    packets = [p for p in (packets or []) if p]
    if not packets:
        return ""
    for p in packets:
        R.ensure_valid(p)

    answers = [p for p in packets if p.get("response_type") == "ask_result"]
    findings = [p for p in packets if p.get("response_type") == "finding"]
    others = [p for p in packets
              if p.get("response_type") not in ("finding", "ask_result")]

    if len(packets) == 1:
        return render(packets[0])

    blocks = []

    # Answers first: the human is actively waiting on these.
    for p in answers:
        blocks.append(_render_ask(p))

    if findings:
        n = len(findings)
        blocks.append(f"I found {_number(n)} thing{'s' if n != 1 else ''} "
                      "worth bringing you:")
        for i, p in enumerate(findings, 1):
            who = _display(p.get("counterpart"))
            body = _render_finding(p, lead=False)
            blocks.append(f"{i}. {who} — {body}")

    for p in others:
        fn = _RENDERERS.get(p.get("response_type"))
        if fn:
            blocks.append(fn(p))

    # Exactly one close for the whole batch, from the best finding's actions.
    lead_packet = (findings or answers or packets)[0]
    close = _close(lead_packet.get("next_actions"),
                   _display(lead_packet.get("counterpart")))
    if close:
        blocks.append(close)
    return "\n\n".join(b for b in blocks if b).strip()


_NUMBERS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def _number(n):
    return _NUMBERS.get(n, str(n))


def check_output(text, response_type="finding"):
    """Self-audit a rendered string. Returns problems; empty means clean.

    Used by the evaluator and by tests. Kept here so the compiler is judged by
    the same rules it is written to.
    """
    problems = []
    banned = R.banned_terms_in(text)
    if banned:
        problems.append(f"machinery vocabulary in delivered prose: {banned}")
    if re.search(r"\b\d+(\.\d+)?\s*/\s*10\b", text or ""):
        problems.append("a raw score reached the user")
    lo, hi = R.WORD_TARGETS.get(response_type, (0, 10 ** 6))
    n = R.word_count(text)
    if n > hi:
        problems.append(f"{n} words, over the {hi}-word target for {response_type}")
    if "Hermix found" in (text or ""):
        problems.append("reads as a product alert rather than the user's agent")
    return problems
