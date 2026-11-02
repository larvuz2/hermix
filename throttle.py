"""A cross-PROCESS rate gate.

Hermes runs subagents as separate processes and every one of them calls
``register()``. The poller lease (service._claim_poller_lock) keeps only one of
them *polling* — but the work register() does on the way up (a health check, a
forced config refresh) ran in every single process, unguarded.

That was the bulk of the hub's traffic in production: two agents producing
~48 requests/minute where ~3 was expected, with zero conversations to show for
it. The lease fixed the loop; nothing fixed startup.

A gate is a file holding one timestamp. The first caller inside a window does
the work, everyone else skips. Deliberately fail-OPEN: if the file cannot be
read or written we do the work, because a chatty agent is a much smaller
problem than a silently disconnected one.
"""
import json
import os
import time


def _gate_path(name: str):
    from . import matchmaker           # lazy: avoids an import cycle
    return matchmaker._state_path().parent / f"gate-{name}.json"


def due(name: str, seconds: float, now=None) -> bool:
    """True if ``seconds`` have passed since any process last claimed ``name``.

    Claiming and reporting are the same call: whoever gets True has stamped the
    gate, so concurrent callers do not all pass. ``seconds <= 0`` disables the
    gate (always due), which keeps it easy to turn off from the hub.
    """
    if seconds <= 0:
        return True
    t = float(now() if callable(now) else (now if now is not None else time.time()))
    try:
        path = _gate_path(name)
    except Exception:
        return True                    # can't resolve a home -> don't block work
    try:
        last = float(json.loads(path.read_text(encoding="utf-8")).get("ts", 0))
        if (t - last) < seconds:
            return False
    except (OSError, ValueError, AttributeError, TypeError):
        pass                           # missing/corrupt gate -> treat as due
    stamp(name, t)
    return True


def stamp(name: str, now=None) -> None:
    """Record that ``name`` just happened, without asking whether it was due.

    Callers that already know they did the work (a config fetch that succeeded)
    use this; ``due`` cannot serve that purpose because a disabled gate returns
    early without writing."""
    t = float(now() if callable(now) else (now if now is not None else time.time()))
    try:
        path = _gate_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": int(t), "pid": os.getpid()}),
                       encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass                           # unwritable -> still do the work
