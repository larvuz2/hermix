"""Live configuration from the hub — how Hermix improves without users doing
anything.

The problem this solves: a plugin that only reads env vars and hard-coded
defaults can only be improved by every user running commands on their own
machine. That does not scale and it is not what "it just works" means.

So the hub serves a small config document (GET /v1/config) and every agent
polls it in the background. Tuning changes — thresholds, cadence, the interrupt
judgement's weights — take effect within the hour, network-wide, with no
restart and nothing for the human to do.

Precedence is deliberate:

    explicit env var  >  hub value  >  built-in default

An operator who set something by hand keeps control; everyone else silently
gets our latest tuning. Anything that needs NEW PYTHON is not a config change —
that is a code release, handled by updater.py.

Never fails loudly: an unreachable hub, a malformed document, or an unwritable
cache all fall through to the last known-good values, then the defaults.
"""
import json
import os
import pathlib
import time

_CACHE = None            # in-process {"knobs": {...}, ...}
_FETCHED_AT = 0.0        # monotonic-ish wall clock of the last successful fetch


def _home() -> pathlib.Path:
    base = os.environ.get("HERMIX_HOME")
    if not base:
        try:
            from hermes_constants import get_hermes_home   # type: ignore
            base = str(get_hermes_home())
        except Exception:
            base = os.path.expanduser("~/.hermes")
    return pathlib.Path(base) / "hermix"


def _cache_path() -> pathlib.Path:
    return _home() / "remote_config.json"


def _load_cache() -> dict:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        _CACHE = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _CACHE = {}
    return _CACHE


def _save_cache(doc: dict) -> None:
    global _CACHE
    _CACHE = doc
    try:
        p = _cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass          # in-process copy is enough for this session


def knob(name: str, default):
    """The hub's value for a tuning knob, else ``default``.

    Callers in _config.py check their env var FIRST, so an explicit local
    setting always wins over anything we serve."""
    knobs = (_load_cache() or {}).get("knobs") or {}
    if name not in knobs:
        return default
    val = knobs[name]
    try:
        return type(default)(val) if default is not None else val
    except (TypeError, ValueError):
        return default


def switch(name: str, default: bool = True) -> bool:
    """A kill switch from the hub. Default TRUE so a missing/unreachable config
    never silently disables the product — the hub also enforces the critical
    ones server-side, so this is a courtesy check that saves a wasted call."""
    sw = (_load_cache() or {}).get("switches") or {}
    if name not in sw:
        return default
    return bool(sw[name])


def release() -> dict:
    """The release the hub wants agents on: {version, channel, min_hermes,
    rollout_percentage, ...}."""
    return (_load_cache() or {}).get("release") or {}


def notice():
    """An optional one-time message the operator wants relayed to humans."""
    return (_load_cache() or {}).get("notice")


def revision():
    """The plugin revision the hub currently expects (see updater.py)."""
    return (_load_cache() or {}).get("plugin_revision")


def _disk_fetched_at() -> float:
    """When ANY process last fetched, so a restart doesn't refetch needlessly."""
    try:
        from . import throttle
        raw = throttle._gate_path("config-fetch").read_text(encoding="utf-8")
        return float(json.loads(raw).get("ts", 0))
    except Exception:
        return 0.0


def _stamp_disk_fetch(t: float) -> None:
    try:
        from . import throttle
        throttle.stamp("config-fetch", t)
    except Exception:
        pass


def refresh(client, now=None, force=False) -> bool:
    """Poll the hub if the cached copy is stale. Returns True if updated.

    Safe to call often — it is a no-op until the refresh interval elapses."""
    global _FETCHED_AT
    t = float(now() if callable(now) else (now if now is not None else time.time()))
    every = max(0.25, float(knob("config_refresh_hours", 6.0))) * 3600.0
    # The staleness clock must be SHARED, not per-process. As a module global it
    # reset to 0 in every freshly spawned Hermes process, so each one refetched
    # the config on startup — a large part of the hub traffic we were seeing.
    if not force and (t - max(_FETCHED_AT, _disk_fetched_at())) < every:
        return False
    fetch = getattr(client, "get_config", None)
    if fetch is None:
        return False
    try:
        doc = fetch()
    except Exception:
        return False                      # offline / 401 / hub down — keep cache
    if not isinstance(doc, dict) or "knobs" not in doc:
        return False
    _FETCHED_AT = t
    _stamp_disk_fetch(t)
    if doc != _load_cache():
        _save_cache(doc)
        return True
    return False


def _reset_for_tests():
    global _CACHE, _FETCHED_AT
    _CACHE, _FETCHED_AT = None, 0.0
