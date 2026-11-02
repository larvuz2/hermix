"""The local DOSSIER — your human's rich private profile, split into rings.

This module is the single writer of the dossier and the guardian of its
boundaries. It NEVER produces outbound text; it only stores and hands back the
narrow, ring-appropriate slices that the outbound layers (envoy.py, tools.py)
are allowed to use.

Privacy model (see skills/hermix-context/SKILL.md), enforced structurally here:

  - Ring 0 (PRIVATE): the full dossier — work history, projects, hobbies,
    goals, bucket list, expenses, notes. Handed out ONLY as section *counts*
    via :func:`summary`; the values never leave this module for any outbound
    path.
  - Ring 1 (SHAREABLE-IN-CONVERSATION): a flat list of facts the human approved
    for agent-to-agent conversation. :func:`get_ring1` is the only export the
    envoy weaves in.
  - Contact identity (name/email/socials): OUTSIDE all rings. Reachable ONLY
    via :func:`get_contact`, which the reveal tools call under a human-approval
    gate. :func:`summary` reports a boolean, never the values.

Persistence follows the blessed pattern (mirrors matchmaker.py): a JSON file at
``$HERMES_HOME/hermix/dossier.json``, atomic temp+rename with a ``.bak``
backup, and a ``get_hermes_home`` fallback. Every string is passed through
``sanitize.clean_text`` on write, so no control/zero-width/fence smuggling ever
lands in the store.
"""
import json
import os
import pathlib
import shutil
import time

from . import sanitize

# Generous cap — dossier facts can be a sentence or two, unlike terse card fields.
_MAX = 500

# Note: per-dig findings notes (3-6 lines per skills/hermix-envoy-protocol/
# SKILL.md) live in the matchmaker's own state (matchmaker.py: state["findings"])
# alongside the dig/thread bookkeeping that judgment reads, not here. This module
# stays the guardian of the human's static profile + contact identity.

RING0_SECTIONS = (
    "work_history", "projects", "hobbies", "goals",
    "bucket_list", "expenses", "notes",
)


# --------------------------------------------------------------------------- #
# Store path + shape
# --------------------------------------------------------------------------- #

def _dossier_path() -> pathlib.Path:
    base = os.environ.get("HERMIX_HOME")
    if base:
        d = pathlib.Path(base)
    else:
        try:  # blessed resolver when running inside Hermes
            from hermes_constants import get_hermes_home
            d = pathlib.Path(get_hermes_home()) / "hermix"
        except Exception:
            d = pathlib.Path(os.path.expanduser("~/.hermes")) / "hermix"
    return d / "dossier.json"


def _new() -> dict:
    return {
        "ring0": {s: [] for s in RING0_SECTIONS},
        "ring1": [],
        "contact": {"name": "", "email": "", "socials": [], "never_share": False},
        "intents": [],
        "onboarded": False,
    }


def _ensure_shape(d) -> dict:
    """Coerce any on-disk / hand-built dict into the canonical shape. Missing
    keys are filled; unknown keys are dropped (defense in depth)."""
    base = _new()
    if not isinstance(d, dict):
        return base
    r0 = d.get("ring0") if isinstance(d.get("ring0"), dict) else {}
    for s in RING0_SECTIONS:
        v = r0.get(s)
        base["ring0"][s] = [str(x) for x in v] if isinstance(v, list) else []
    if isinstance(d.get("ring1"), list):
        base["ring1"] = [str(x) for x in d["ring1"]]
    c = d.get("contact") if isinstance(d.get("contact"), dict) else {}
    base["contact"]["name"] = str(c.get("name", "") or "")
    base["contact"]["email"] = str(c.get("email", "") or "")
    socials = c.get("socials")
    base["contact"]["socials"] = [str(x) for x in socials] if isinstance(socials, list) else []
    base["contact"]["never_share"] = bool(c.get("never_share", False))
    if isinstance(d.get("intents"), list):
        base["intents"] = [i for i in d["intents"] if isinstance(i, dict)]
    base["onboarded"] = bool(d.get("onboarded", False))
    return base


def _clean(s) -> str:
    return sanitize.clean_text(s, max_len=_MAX)


# --------------------------------------------------------------------------- #
# Load / save (atomic temp+rename + .bak)
# --------------------------------------------------------------------------- #

def load(path=None) -> dict:
    p = pathlib.Path(path) if path else _dossier_path()
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    return _ensure_shape(data)


def save(state, path=None) -> pathlib.Path:
    p = pathlib.Path(path) if path else _dossier_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(_ensure_shape(state), indent=2), encoding="utf-8")
    if p.exists():
        try:
            shutil.copy2(p, p.with_name(p.name + ".bak"))
        except Exception:
            pass
    os.replace(tmp, p)
    return p


# --------------------------------------------------------------------------- #
# Ring facts
# --------------------------------------------------------------------------- #

def add_fact(ring, section, text, path=None) -> dict:
    """Append a sanitized fact. ``ring`` is "ring0" or "ring1"; for ring0,
    ``section`` selects a Ring-0 bucket (unknown -> "notes"); for ring1,
    ``section`` is ignored (Ring 1 is a flat list). Empty text is a no-op."""
    d = load(path)
    text = _clean(text)
    if text:
        if ring == "ring1":
            if text not in d["ring1"]:
                d["ring1"].append(text)
        else:
            sec = section if section in RING0_SECTIONS else "notes"
            d["ring0"][sec].append(text)
        save(d, path)
    return d


def move_to_ring1(text, from_section=None, path=None) -> dict:
    """Approve a fact for conversation: add ``text`` to Ring 1. If
    ``from_section`` is a Ring-0 bucket, remove the matching value there so a
    fact isn't duplicated across rings."""
    d = load(path)
    text = _clean(text)
    if text:
        if from_section in RING0_SECTIONS:
            d["ring0"][from_section] = [x for x in d["ring0"][from_section] if x != text]
        if text not in d["ring1"]:
            d["ring1"].append(text)
        save(d, path)
    return d


def get_ring1(path=None) -> list:
    """The ONLY dossier export the envoy may weave into agent-to-agent replies."""
    return list(load(path)["ring1"])


# --------------------------------------------------------------------------- #
# Contact identity — outside all rings, released only by an approved reveal
# --------------------------------------------------------------------------- #

def set_contact(name=None, email=None, socials=None, never_share=None, path=None) -> dict:
    """Store contact identity locally (sanitized). Only the arguments that are
    not None are updated. This is captured during onboarding and released ONLY
    through the human-approval-gated reveal tools."""
    d = load(path)
    c = d["contact"]
    if name is not None:
        c["name"] = _clean(name)
    if email is not None:
        c["email"] = _clean(email)
    if socials is not None:
        c["socials"] = [x for x in (_clean(s) for s in socials) if x]
    if never_share is not None:
        c["never_share"] = bool(never_share)
    save(d, path)
    return dict(c)


def get_contact(path=None) -> dict:
    """Return the contact identity. Callers MUST be behind the reveal
    human-approval gate (commands.install_gate); this module cannot enforce that
    but never calls it itself."""
    return dict(load(path)["contact"])


# --------------------------------------------------------------------------- #
# Onboarding flag
# --------------------------------------------------------------------------- #

def set_onboarded(value=True, path=None) -> bool:
    d = load(path)
    d["onboarded"] = bool(value)
    save(d, path)
    return d["onboarded"]


def is_onboarded(path=None) -> bool:
    return bool(load(path)["onboarded"])


# --------------------------------------------------------------------------- #
# Standing intents ("dig for X")
# --------------------------------------------------------------------------- #

def add_intent(text, path=None) -> dict:
    """Create an active standing intent. Returns the intent dict, or None if the
    text sanitized to empty."""
    d = load(path)
    text = _clean(text)
    if not text:
        return None
    next_id = 1 + max([int(i.get("id", 0)) for i in d["intents"]] or [0])
    intent = {
        "id": next_id,
        "text": text,
        "created_ts": int(time.time()),
        "status": "active",
    }
    d["intents"].append(intent)
    save(d, path)
    return intent


def list_intents(path=None) -> list:
    return list(load(path)["intents"])


def retire_intent(intent_id, path=None) -> dict:
    """Mark an intent stale (retired). Returns the updated intent, or None if no
    such id exists."""
    d = load(path)
    found = None
    for i in d["intents"]:
        if str(i.get("id")) == str(intent_id):
            i["status"] = "stale"
            found = dict(i)
    if found is not None:
        save(d, path)
    return found


# --------------------------------------------------------------------------- #
# Safe summary — counts + Ring 1 + intents, NEVER any contact value
# --------------------------------------------------------------------------- #

def summary(path=None) -> dict:
    """A read-only, contact-free view for the human and for hermix_dossier.

    Returns Ring-0 section *counts* (never values), the Ring-1 fact list, the
    intents, and a BOOLEAN for whether contact is on file. Contact name/email/
    socials are structurally impossible to appear in this output."""
    d = load(path)
    c = d["contact"]
    return {
        "ring0_counts": {s: len(d["ring0"][s]) for s in RING0_SECTIONS},
        "ring1": list(d["ring1"]),
        "intents": list(d["intents"]),
        "onboarded": bool(d["onboarded"]),
        "contact_set": bool(c["name"] or c["email"] or c["socials"]),
    }
