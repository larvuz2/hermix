"""The briefing: how the human decides, never what the human has done.

This is the one new piece of knowledge the envoy gets, and the whole design
rests on one distinction:

    "Takes paid commercial work at mid-five-figure scale; cares about payment
     terms; prefers creative control over pure execution."

    NOT "quoted EUR 40k for the Telefonica spot, shipped March, paid late."

The first lets the envoy answer "would your human be interested?" — which is
the entire point of having a briefing. The second is the engagement itself, and
would be a leak.

Three rules make it safe, and all three are enforced here rather than trusted:

1. **Only the principal writes it.** Generation runs in the plugin, on the
   human's own machine, from their dossier. The envoy has no code path that can
   write a briefing — if it did, a hostile counterpart could talk it into
   recording something and disclosing it three digs later.
2. **A deterministic scrub runs after the model.** Generation is an LLM step
   and LLM steps err, so we do not rely on the prompt: every proper noun,
   figure, email, URL and date drawn from Ring 0 is collected up front, and any
   generated line containing one is DROPPED. Not redacted — dropped, because a
   half-scrubbed sentence is a sentence we no longer understand.
3. **The human can read it and delete it.** `/hermix briefing` prints it
   verbatim. If they cannot inspect what their envoy believes about them, the
   trust story fails.

Deleting the briefing reverts the envoy to card-only behaviour: less capable,
never broken.
"""
import json
import logging
import re
import time

from . import sanitize

log = logging.getLogger("hermix.briefing")

MAX_LINES = 12
MAX_LINE_LEN = 220
# Regenerating on every dossier edit would be wasteful and would make the envoy
# feel unstable; only at onboarding would leave it stale forever.
REFRESH_AFTER_DAYS = 7

SYSTEM = (
    "You write a BRIEFING that lets an agent represent a human to strangers "
    "without ever exposing that human's private life.\n\n"
    "You are given private notes. You must NOT summarise them. You must "
    "abstract them into general, reusable statements about how this person "
    "operates: the kind of work they take, the scale they work at, what they "
    "say yes and no to, how they decide, what they care about, what they are "
    "trying to move toward.\n\n"
    "ABSOLUTE RULES — a single violation makes the whole briefing unusable:\n"
    "- NEVER name a company, client, employer, product, project, place or "
    "person. Not one.\n"
    "- NEVER give a figure, price, salary, date, month or year.\n"
    "- NEVER describe a specific event, engagement or transaction.\n"
    "- Write about PATTERNS, not instances. 'Takes paid commercial work at "
    "mid-five-figure scale' is right; 'did a 40k spot for a telecoms brand' is "
    "wrong even without the name.\n\n"
    "Output 5-10 short lines, one statement per line, no numbering, no "
    "preamble, no markdown. If the notes do not support a statement, write "
    "fewer lines. Never invent."
)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_URL = re.compile(r"https?://\S+|www\.\S+")
_NUMBER = re.compile(r"\d")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")
_PROPER = re.compile(r"\b[A-Z][\w'&-]{2,}\b")

# Words that start a sentence or are ordinary English; seeing them capitalised
# in private notes must not turn them into forbidden terms.
_COMMON = {
    "The", "This", "That", "They", "Their", "There", "These", "Those", "Then",
    "And", "But", "For", "With", "From", "Into", "Also", "Not", "Now", "New",
    "Was", "Were", "Has", "Have", "Had", "Will", "Would", "Should", "Could",
    "When", "What", "Where", "Which", "While", "Who", "Why", "How", "Its",
    "Wants", "Needs", "Likes", "Prefers", "Working", "Building", "Looking",
    "Paid", "Client", "Project", "Work", "Started", "Finished", "Made",
}


def _forbidden_terms(ring0: dict) -> set:
    """Every proper noun, address and URL that appears in the private notes.

    Deliberately over-inclusive. A briefing line that trips a false positive is
    simply dropped, and losing one abstract sentence costs far less than
    leaking one concrete fact.
    """
    terms = set()
    for values in (ring0 or {}).values():
        for value in (values or []):
            text = str(value)
            for m in _EMAIL.findall(text):
                terms.add(m.lower())
            for m in _URL.findall(text):
                terms.add(m.lower())
            for m in _PROPER.findall(text):
                if m not in _COMMON:
                    terms.add(m.lower())
    return terms


def _rejects(line: str, forbidden: set) -> str:
    """Return a reason this line must not survive, or '' if it is safe."""
    low = line.lower()
    if _NUMBER.search(line):
        return "contains a figure or date"
    if _EMAIL.search(line) or _URL.search(line):
        return "contains an address"
    for month in _MONTHS:
        if re.search(r"\b" + month + r"\b", low):
            return "contains a date"
    for term in forbidden:
        if re.search(r"\b" + re.escape(term) + r"\b", low):
            return f"echoes a private term ({term})"
    return ""


def scrub(lines, ring0: dict) -> tuple:
    """Drop every line that carries anything concrete. Returns (kept, dropped).

    This is the part that does not trust the model. It runs on whatever the LLM
    produced, using terms harvested from the dossier itself, so it stays
    correct even if the prompt is ignored entirely.
    """
    forbidden = _forbidden_terms(ring0)
    kept, dropped = [], []
    for raw in lines or []:
        line = sanitize.clean_text(str(raw), max_len=MAX_LINE_LEN).strip(" -*\t")
        if not line or len(line) < 12:
            continue
        reason = _rejects(line, forbidden)
        if reason:
            dropped.append((line, reason))
        else:
            kept.append(line)
    return kept[:MAX_LINES], dropped


def generate(dossier_doc: dict, card, llm, now=None) -> dict:
    """Derive a fresh briefing from the dossier. Principal-side only.

    ``dossier_doc`` is the raw dossier dict (ring0 + ring1). It never leaves
    this function: the model sees it, the scrub uses it, and only abstracted
    survivors are returned.
    """
    t = float(now() if callable(now) else (now if now is not None else time.time()))
    ring0 = (dossier_doc or {}).get("ring0") or {}
    notes = []
    for section, values in ring0.items():
        for value in (values or [])[:20]:
            notes.append(f"[{section}] {value}")
    if not notes:
        return {"lines": [], "updated_at": int(t), "dropped": 0,
                "reason": "no private notes to abstract from"}

    user = ("PRIVATE NOTES (abstract these; never repeat them):\n"
            + "\n".join(notes[:80]))
    try:
        raw = llm(SYSTEM, user, purpose="refresh") if callable(llm) else ""
    except Exception as e:
        log.debug("briefing generation failed: %s", e)
        raw = ""
    if not isinstance(raw, str) or not raw.strip():
        return {"lines": [], "updated_at": int(t), "dropped": 0,
                "reason": "no response from the model"}

    kept, dropped = scrub(raw.splitlines(), ring0)
    for line, reason in dropped:
        log.debug("briefing line dropped (%s)", reason)
    return {"lines": kept, "updated_at": int(t), "dropped": len(dropped),
            "reason": ""}


# --------------------------------------------------------------------------- #
# Storage — the briefing lives in the ENVOY profile, written only from here.
# --------------------------------------------------------------------------- #

def load() -> dict:
    from . import envoy_profile
    try:
        doc = json.loads(envoy_profile.briefing_path().read_text(encoding="utf-8"))
        if isinstance(doc, dict) and isinstance(doc.get("lines"), list):
            return doc
    except (OSError, ValueError):
        pass
    return {"lines": [], "updated_at": 0}


def save(doc: dict) -> bool:
    from . import envoy_profile
    try:
        envoy_profile._write(envoy_profile.briefing_path(),
                             json.dumps(doc, indent=2))
        return True
    except OSError:
        return False


def clear() -> bool:
    from . import envoy_profile
    try:
        envoy_profile.briefing_path().unlink(missing_ok=True)
        return True
    except OSError:
        return False


def lines() -> list:
    return load().get("lines") or []


def is_stale(now=None) -> bool:
    t = float(now() if callable(now) else (now if now is not None else time.time()))
    doc = load()
    if not doc.get("lines"):
        return True
    return (t - float(doc.get("updated_at") or 0)) > REFRESH_AFTER_DAYS * 86400


def refresh_if_due(dossier_doc, card, llm, now=None, force=False) -> dict:
    """Regenerate when stale. Cheap no-op otherwise."""
    if not force and not is_stale(now):
        return load()
    doc = generate(dossier_doc, card, llm, now=now)
    if doc.get("lines"):
        save(doc)
        return doc
    existing = load()
    return existing if existing.get("lines") else doc


def format_for_human(doc: dict = None) -> str:
    """What /hermix briefing prints. Plain, complete, and honest about limits."""
    doc = doc if doc is not None else load()
    lines_ = doc.get("lines") or []
    if not lines_:
        return ("Your envoy has no briefing yet — it is representing you from "
                "your public card alone.\n"
                "Add to your dossier (`/hermix dossier`) and I'll derive one, "
                "or run `/hermix briefing refresh`.")
    out = ["This is everything your envoy believes about you, beyond your "
           "public card:", ""]
    for line in lines_:
        out.append(f"  • {line}")
    out += ["",
            "It shapes how your envoy judges what to bring you. It is never "
            "quoted to anyone, and it deliberately contains no names, figures "
            "or dates from your private notes.",
            "`/hermix briefing refresh` to rebuild it · "
            "`/hermix briefing clear` to remove it."]
    return "\n".join(out)
