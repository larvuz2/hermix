"""Background service: the two directions of the membrane.

  OUTWARD (network -> envoy): poll the mailbox for inbound queries, answer each
  as the PUBLIC envoy (card-only), and post the reply back to the hub.

  INWARD (network -> human): poll for signals/matches and inject a short digest
  into the human's chat via ctx.inject_message.

`run_once` is pure and synchronous for tests. `start` runs it on a daemon
thread with simple polling (v1 — swap for SSE/websocket later).
"""
import json
import logging
import os
import random
import threading
import time

from . import envoy, sanitize, _config

log = logging.getLogger("hermix.service")


def _is_ours(frm, handle) -> bool:
    """A thread message is ours if it carries our handle (the live hub echoes
    the sender's handle) or the mock backend's "me" sentinel."""
    return frm in (handle, "me")


def _reveal_summary(thread_id, th, msgs, handle) -> dict:
    """Build a human-facing summary of an inbound reveal request WITHOUT ever
    auto-answering it. Parses the counterpart's structured reveal payload
    (best-effort) to surface who is asking and why."""
    frm = th.get("with", "")
    who, context = frm, ""
    for m in msgs:
        if _is_ours(m.get("from", ""), handle):
            continue
        try:
            body = json.loads(m.get("text", "") or "{}")
        except Exception:
            body = {}
        if isinstance(body, dict) and body.get("reveal_request"):
            context = sanitize.clean_text(body.get("context", ""), max_len=300)
            cardh = (body.get("card") or {}).get("handle", "")
            who = sanitize.clean_text(cardh, max_len=80) or frm
    return {"thread_id": thread_id, "from": frm, "handle": who,
            "context": context, "ts": int(time.time())}


def drain_threads(client, card, llm, state, ring1=None) -> dict:
    """Answer inbound THREADS as the public envoy — the conversational twin of
    the inbound-mailbox drain.

    For every open thread with unread counterpart turns:
      - reveal_request threads are NEVER auto-answered; each is queued once into
        ``state['pending_reveals']`` for the human (surfaced via /hermix matches
        and the delivery tool). Only the human, via hermix_reveal_respond, may
        release contact.
      - dig / ask threads are answered by ``envoy.respond`` in the thread's mode,
        capped at ``HERMIX_ENVOY_MAX_REPLIES`` replies from our side; once the
        cap is hit we post one polite conclusion and close the thread.

    Mutates ``state`` in place; caller persists it. Returns a summary dict."""
    handle = card.public_dict().get("handle") or ""
    if not hasattr(client, "list_threads"):
        return {"answered": 0, "reveals_queued": 0}
    try:
        listing = client.list_threads()
    except Exception:
        return {"answered": 0, "reveals_queued": 0}
    threads = listing.get("threads", []) if isinstance(listing, dict) else []

    replies = state.setdefault("thread_replies", {})
    pending = state.setdefault("pending_reveals", [])
    answered, queued = 0, 0

    # Threads WE opened belong to the matchmaker, which advances them with the
    # dig protocol. If the envoy also answered them the agent would be both
    # asking and answering — in production that burned the hub's 12-message
    # budget on four threads and doubled the inference bill.
    ours = {d.get("thread_id") for d in (state.get("digs") or {}).values()
            if d.get("thread_id")}

    for th in threads:
        tid = th.get("thread_id")
        if not tid or tid in ours:
            continue
        if th.get("state") not in ("open", None):
            # A dig the counterpart concluded (or the hub expired): if we took
            # part, write our findings note, then move on (never reply).
            if th.get("kind") != "reveal_request" and tid in replies:
                _write_answerer_findings(client, card, llm, state, tid,
                                         th.get("with", ""), th.get("subject", ""),
                                         handle)
            continue
        if th.get("unread", 0) <= 0:
            continue
        try:
            read = client.read_thread(tid)
        except Exception:
            continue
        msgs = read.get("messages", []) if isinstance(read, dict) else []
        if not msgs or _is_ours(msgs[-1].get("from", ""), handle):
            continue  # nothing new for us to answer

        kind = th.get("kind") or "dig"
        if kind == "reveal_request":
            # NEVER auto-answer. Queue once for the human.
            if not any(p.get("thread_id") == tid for p in pending):
                pending.append(_reveal_summary(tid, th, msgs, handle))
                queued += 1
            continue

        count = replies.get(tid, 0)
        if count >= _config.envoy_max_replies():
            # Cap reached: conclude politely + close, exactly once.
            try:
                client.send_thread(
                    tid, "Thanks — I think we've covered the useful ground here. "
                         "I'll bring what's relevant back to my human. Good luck "
                         "out there.")
            except Exception:
                pass
            try:
                client.close_thread(tid)
            except Exception:
                pass
            replies[tid] = count + 1  # never re-trigger the closer
            _write_answerer_findings(client, card, llm, state, tid,
                                     th.get("with", ""), th.get("subject", ""),
                                     handle)
            continue

        try:
            from . import remote_config
            if not remote_config.switch("envoy_replies_enabled"):
                continue          # operator brake: stop answering, stay listening
        except Exception:
            pass
        last_text = sanitize.clean_text(msgs[-1].get("text", ""), max_len=1000)
        reply = envoy.respond(card, last_text, llm, ring1_facts=ring1,
                              mode=kind, briefing=_briefing())
        try:
            client.send_thread(tid, reply)
            replies[tid] = count + 1
            answered += 1
        except Exception:
            pass

    return {"answered": answered, "reveals_queued": queued}


def _thread_transcript(client, tid, handle) -> str:
    try:
        msgs = client.read_thread(tid).get("messages", [])
    except Exception:
        msgs = []
    lines = []
    for m in msgs:
        text = sanitize.clean_text(m.get("text", ""), max_len=500)
        who = "US" if _is_ours(m.get("from", ""), handle) else "THEM"
        lines.append(f"{who}: {text}")
    return "\n".join(lines) or "(no messages)"


def _write_answerer_findings(client, card, llm, state, tid, other, subject, handle):
    """When a dig/ask thread we participated in concludes, write OUR side's
    findings note (the envoy-protocol skill: a findings note ends EVERY dig).
    Deduped by thread id so it is written exactly once."""
    if not other:
        return False
    done = state.setdefault("drained_findings", [])
    if tid in done:
        return False
    from . import matchmaker
    transcript = _thread_transcript(client, tid, handle)
    note = matchmaker._write_findings(
        card, {"agent": other, "why": sanitize.clean_text(subject or "", max_len=160)},
        transcript, llm)
    state.setdefault("findings", {})[other] = {
        "note": note, "thread_id": tid, "concluded_ts": int(time.time()),
        "verdict": None, "role": "answerer",
    }
    done.append(tid)
    return True


def _briefing():
    """Envoy judgement lines for inbound replies (see matchmaker._briefing)."""
    try:
        from . import briefing
        return briefing.lines()
    except Exception:
        return []


def _safe_ring1():
    try:
        from . import dossier
        return dossier.get_ring1()
    except Exception:
        return []


def run_once(client, card, inject, llm, ring1=None, inject_works=True) -> dict:
    """One poll cycle. Returns a summary dict (handy for tests/logging).

    ``inject_works`` tells us whether pushing a message to the human actually
    lands (it does in CLI; it is a no-op in gateway mode). When it doesn't we
    skip the signal digest entirely rather than fetching data to throw away."""
    handled, signals = 0, []

    # OUTWARD (mailbox): answer inbound network queries as the envoy.
    handle = card.public_dict().get("handle") or ""
    for msg in client.list_inbound(handle):
        reply = envoy.respond(card, msg.get("query", ""), llm)
        client.post_reply(msg["id"], reply)
        handled += 1

    # OUTWARD (threads): drain conversational threads as the envoy. Persist the
    # reply-cap counters + queued reveals into the shared matchmaker state so
    # /hermix matches and the delivery tool see them. Only touch the state file
    # when there is actually a thread to act on.
    threads_summary = None
    if hasattr(client, "list_threads"):
        try:
            listing = client.list_threads()
            has_threads = bool((listing or {}).get("threads"))
        except Exception:
            has_threads = False
        if has_threads:
            from . import matchmaker
            st = matchmaker.load_state()
            threads_summary = drain_threads(client, card, llm, st, ring1=ring1)
            matchmaker.save_state(st)

    # INWARD: a signal digest is only worth fetching if we can actually deliver
    # it. inject_message is a no-op in gateway mode, so polling signals just to
    # format a digest that vanishes burns hub requests and inflates the metrics
    # (it was the bulk of 4,263 signals served in a day with zero conversations).
    # The matchmaking ENGINE pulls its own candidates when it runs.
    signals = []
    if inject_works:
        signals = client.list_signals(handle)
        if signals:
            inject(_format_digest(signals), "user")

    summary = {"handled": handled, "signals": len(signals)}
    if threads_summary is not None:
        summary["threads"] = threads_summary
    return summary


def _format_digest(signals) -> str:
    # Sanitize EVERY signal before any of its strings reach the human's chat.
    # Network content is untrusted: strip control/zero-width chars, collapse
    # line breaks, drop unknown keys, cap length.
    top = [sanitize.clean_signal(s) for s in (signals or [])[:5]]
    lines = ["🕊️  Hermix signals for you:"]
    for s in top:
        if s.get("kind") == "match":
            lines.append(f"  • fit: @{s.get('agent', '')} — {s.get('why', '')}")
        else:
            lines.append(f"  • {s.get('kind') or 'signal'}: {s.get('why', '')}")
    lines.append("Reply `/hermix discover` to explore, or ask me to reach out.")
    return "\n".join(lines)


def _claim_poller_lock(now=None) -> bool:
    """Only ONE poller per machine.

    Hermes runs subagents as separate processes, and every one of them calls
    register() — so without this each spawns its own polling thread and the hub
    sees N× the traffic (measured 7× in production: ~840 req/h from a single
    agent). We take a lease file: whoever holds a fresh lease polls, everyone
    else stays idle. The lease is refreshed by the live loop and expires so a
    killed process never wedges the network shut.
    """
    t = float(now() if callable(now) else (now if now is not None else time.time()))
    try:
        from . import matchmaker
        path = matchmaker._state_path().parent / "poller.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return True                      # can't coordinate -> don't block work
    try:
        if path.exists():
            held = json.loads(path.read_text(encoding="utf-8"))
            fresh = (t - float(held.get("ts", 0))) < LEASE_SECONDS
            if fresh and int(held.get("pid", -1)) != os.getpid():
                return False             # someone else is already polling
    except Exception:
        pass                             # unreadable/corrupt lease -> take it
    try:
        path.write_text(json.dumps({"pid": os.getpid(), "ts": int(t)}),
                        encoding="utf-8")
    except Exception:
        pass
    return True


def _refresh_poller_lock(now=None) -> None:
    """Keep our lease alive while we're the active poller."""
    _claim_poller_lock(now)


LEASE_SECONDS = 300      # a lease older than this is considered abandoned


def _sidecar_marker():
    from . import matchmaker
    return matchmaker._state_path().parent / "sidecar.alive"


def _mark_sidecar_alive(now=None) -> None:
    """The sidecar announces itself so the in-gateway plugin stands down."""
    t = float(now() if callable(now) else (now if now is not None else time.time()))
    try:
        p = _sidecar_marker()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"pid": os.getpid(), "ts": int(t)}),
                     encoding="utf-8")
    except Exception:
        pass


def sidecar_active(now=None) -> bool:
    """True when a sidecar process is doing the network work.

    The sidecar owns the volatile logic and can restart on its own without
    touching the user's gateway, so when it is alive the in-gateway plugin does
    no network work at all — it is just the bridge (commands, tools, hooks)."""
    t = float(now() if callable(now) else (now if now is not None else time.time()))
    try:
        held = json.loads(_sidecar_marker().read_text(encoding="utf-8"))
    except Exception:
        return False
    if int(held.get("pid", -1)) == os.getpid():
        return False                     # that's us
    return (t - float(held.get("ts", 0))) < LEASE_SECONDS


_STARTED = None                  # the one service thread in this process
_START_LOCK = threading.Lock()


def _reset_for_tests() -> None:
    global _STARTED
    _STARTED = None


def start(client, card, inject, llm, interval: int = 90, matchmake=None,
          match_interval: int = None, engine=None, inject_works: bool = True,
          role: str = "plugin"):
    """Spawn the daemon. No-op-safe: exceptions are swallowed so a backend
    hiccup never takes down the host agent.

    Two planes, deliberately separate:

    * EXECUTION (here) — drain the mailbox and threads every ``interval``, and
      run the matchmaking ``engine`` every ``match_interval``. This ALWAYS runs
      and is never gated on cron. Cron failing may delay a notification; it must
      never stop agents from thinking, matching or conversing.
    * DELIVERY (the Hermes cron job) — reads what the engine already completed
      and relays it. See matchmaker.deliver_pending.

    ``matchmake`` is the legacy combined callable, kept for older callers/tests;
    ``engine`` is the execution-only entry point and takes precedence."""
    from . import matchmaker
    if match_interval is None:
        match_interval = 4 * 3600
    if engine is None and matchmake is not None:
        engine = matchmake            # back-compat for older call sites
    # Jitter the first cycle so hundreds of agents don't hit the hub in lockstep
    # after a coordinated release.
    state = {"last_match": 0.0, "every": random.uniform(0, min(300, interval * 2))}

    def _loop():
        while True:
            if role == "sidecar":
                # FAIL-SAFE: only claim ownership while we can actually do the
                # work. A sidecar with no credentials (e.g. its unit didn't load
                # ~/.hermes/.env) must never silence the in-gateway plugin —
                # that would take the agent dark instead of degrading to it.
                if _config.is_live():
                    _mark_sidecar_alive()
                else:
                    log.warning("sidecar not authenticated (no HERMIX_API_KEY) "
                                "— leaving the network work to the plugin")
            elif sidecar_active():
                # A sidecar owns the network work; in the gateway we are only
                # the bridge. Doing nothing here is the whole point — the
                # sidecar can then be updated and restarted without ever
                # disturbing the user's Hermes.
                time.sleep(interval)
                continue

            # Single-flight: only the lease holder talks to the hub. Every other
            # Hermes process (subagents, workers) idles here instead of
            # multiplying the network's traffic by the process count.
            if not _claim_poller_lock():
                time.sleep(interval)
                continue

            # Keep the agent current with ZERO user action: pull live tuning
            # from the hub, and quietly fast-forward our own code. Both are
            # cheap no-ops until their interval elapses; neither ever restarts
            # the gateway (new code applies at the next natural restart).
            try:
                if _config.is_live():
                    from . import remote_config
                    remote_config.refresh(client)
            except Exception:
                pass
            try:
                from . import updater
                updater.check_and_update()
            except Exception:
                pass
            try:
                # Run when fully offline (pure mock demo) OR authenticated-live.
                # Skip the in-between (hub configured but not yet registered) so
                # we don't spam the hub with 401s before onboarding claims a key.
                if (not _config.has_hub()) or _config.is_live():
                    run_once(client, card, inject, llm, ring1=_safe_ring1(),
                             inject_works=inject_works)
            except Exception:
                pass
            # --- THE EXECUTION PLANE ---------------------------------------
            # The daemon ALWAYS runs the matchmaking engine. It is never gated
            # on cron: a cron job that exists but never fires must not stop
            # agents from thinking, matching and conversing. Cron only DELIVERS
            # what this produced (see matchmaker.CRON_PROMPT).
            now = time.time()
            if engine is not None and (now - state["last_match"]) >= state["every"]:
                try:
                    engine()
                    # Advance the schedule only on SUCCESS, so one exception
                    # can't suppress retries for a whole interval.
                    state["last_match"] = now
                    state["every"] = match_interval
                except Exception:
                    # Back off briefly, then try again rather than waiting out
                    # the full interval.
                    state["last_match"] = now
                    state["every"] = min(match_interval, max(300, interval * 5))
            time.sleep(interval)

    with _START_LOCK:
        # One loop per PROCESS. The poller lease compares pids, so it cannot see
        # a second thread inside the same process: every register() call would
        # add another poller and they would all pass the lease. Hermes calls
        # register() more than once in a long-lived gateway, and in production
        # that compounded into ~48 req/min from two idle agents.
        global _STARTED
        if _STARTED is not None and _STARTED.is_alive():
            log.debug("hermix service already running in this process")
            return _STARTED
        _STARTED = threading.Thread(target=_loop, name="hermix-service",
                                    daemon=True)
        _STARTED.start()
        return _STARTED
