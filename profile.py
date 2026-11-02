"""The public agent card — the ONLY thing Hermix ever exposes to the network.

This is the structured-card model. The whitelist PUBLIC_FIELDS is the single
source of truth for what may ever leave this machine; the envoy membrane
(envoy.py) reads through it so no private field can leak even if it ends up on
the card object by accident.
"""
import json
import os
import pathlib
from dataclasses import dataclass, field, asdict

# The hard boundary. Nothing outside this list is ever published or spoken.
PUBLIC_FIELDS = [
    "handle",          # public name, e.g. "gus-herald"
    "tagline",         # one-line pitch
    "represents",      # short description of the human/role
    "building",        # what they're building
    "offer",           # what they can offer
    "need",            # what they're looking for
    "curious",         # what they're curious about
    "avoid",           # what they avoid / don't want
    "abilities",       # names of callable abilities this agent exposes
    "signals_wanted",  # kinds of signals worth bringing back
    "guilds",          # guilds/interest groups joined
]


def _store_path() -> pathlib.Path:
    base = os.environ.get("HERMIX_HOME") or os.path.expanduser("~/.hermes/hermix")
    return pathlib.Path(base) / "profile.json"


@dataclass
class PublicCard:
    handle: str = ""
    tagline: str = ""
    represents: str = ""
    building: list = field(default_factory=list)
    offer: list = field(default_factory=list)
    need: list = field(default_factory=list)
    curious: list = field(default_factory=list)
    avoid: list = field(default_factory=list)
    abilities: list = field(default_factory=list)
    signals_wanted: list = field(default_factory=list)
    guilds: list = field(default_factory=list)

    def public_dict(self) -> dict:
        """Return ONLY whitelisted fields — the membrane's serialization point."""
        d = asdict(self)
        return {k: d.get(k) for k in PUBLIC_FIELDS}

    def is_empty(self) -> bool:
        return not any(self.public_dict().values())


def load_card() -> PublicCard:
    p = _store_path()
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        # Only pull whitelisted keys off disk — defense in depth.
        return PublicCard(**{k: data[k] for k in PUBLIC_FIELDS if k in data})
    return PublicCard()


def save_card(card: PublicCard) -> pathlib.Path:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(card.public_dict(), indent=2), encoding="utf-8")
    return p
