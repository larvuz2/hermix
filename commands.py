"""`/hermix` slash-command handlers and the skill-install approval gate."""
import datetime
import json
import pathlib
import time

from . import profile, service, sanitize, matchmaker, dossier


def _ts(epoch) -> str:
    try:
        return datetime.datetime.utcfromtimestamp(int(epoch)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


def make_handler(client, card, llm):
    """Return a handler for `/hermix <sub> [args]`."""

    def _network_line() -> str:
        """One-line connectivity status for the human/operator."""
        from . import _config
        if not _config.has_hub():
            return "Network: OFFLINE (mock) — HERMIX_API_URL is empty."
        hub = _config.service_url()
        try:
            reachable = client.healthz()
        except Exception:
            reachable = False
        if not reachable:
            return f"Network: hub {hub} is UNREACHABLE right now."
        if _config.api_key():
            return f"Network: connected ✓  ({hub})"
        return (f"Network: hub reachable ({hub}) — not registered yet. "
                "Publish your card to join.")

    def handler(args: str = "", **kwargs) -> str:
        parts = (args or "").strip().split(maxsplit=1)
        sub = parts[0].lower() if parts else "status"
        rest = parts[1] if len(parts) > 1 else ""

        if sub == "update":
            # Manual trigger — normally this happens by itself in the daemon.
            from . import updater, remote_config
            cfg = "updated" if remote_config.refresh(client, force=True) else "current"
            res = updater.check_and_update(force=True)
            line = f"Settings: {cfg} (live from the hub)."
            if res.get("updated"):
                return (line + f"\nCode: updated {res['from']} → {res['to']}. "
                        "It goes live at the next gateway restart — no rush.")
            return line + f"\nCode: {res.get('reason', 'current')} "
        if sub in ("", "status"):
            pub = card.public_dict()
            net = _network_line()
            # Surface first-run setup state right after the connectivity line so
            # the human (and the agent reading status) sees onboarding is due.
            if not dossier.is_onboarded():
                net = (net + "\nSetup: NOT onboarded — run the hermix-onboarding "
                       "skill with your human.")
            net = net + "\n" + _engine_line()
            if card.is_empty():
                return (net + "\n\nNo public profile yet. Set one with:\n"
                        "  /hermix profile {\"handle\": \"gus-herald\", "
                        "\"represents\": \"a creative technologist in AI film\", "
                        "\"offer\": [\"ai video\"], \"need\": [\"collaborators\"]}")
            return net + "\n\nYour PUBLIC card:\n" + json.dumps(pub, indent=2)

        if sub == "profile":
            if not rest:
                return json.dumps(card.public_dict(), indent=2)
            try:
                patch = json.loads(rest)
            except json.JSONDecodeError as e:
                return f"Could not parse JSON: {e}"
            for k, v in patch.items():
                if k in profile.PUBLIC_FIELDS:
                    setattr(card, k, v)
            profile.save_card(card)
            # Frictionless auto-join: claim our handle + get a key on first
            # publish, so no API key is ever required from the user.
            from .client import ensure_registered
            ensure_registered(client, card)
            client.publish_profile(card.public_dict())
            rejoined = _resume_matchmaking()  # publishing re-joins after a leave
            note = ("\n(Re-joined the network — I'm looking again.)"
                    if rejoined else "")
            return ("Updated & published your PUBLIC card:\n"
                    + json.dumps(card.public_dict(), indent=2) + note)

        if sub == "discover":
            signals = client.discover(card.public_dict())
            if not signals:
                return "Nothing yet. Flesh out your `need`/`offer`/`guilds` and try again."
            return service._format_digest(signals)

        if sub == "signals":
            handle = card.public_dict().get("handle", "")
            signals = client.list_signals(handle)
            return service._format_digest(signals) if signals else "No signals right now."

        if sub == "search":
            agents = client.search_agents(rest)
            if not agents:
                return "No agents found."
            # Untrusted network content: render sanitized values only.
            return "\n".join(
                f"  • @{sanitize.clean_text(a.get('handle', ''))} — "
                f"{sanitize.clean_text(a.get('represents', ''))}"
                for a in agents)

        if sub == "skills":
            skills = client.browse_skills(rest)
            # Untrusted network content: render sanitized values only.
            return "Available skills:\n" + "\n".join(
                f"  • {sanitize.clean_text(s.get('name', ''))} — "
                f"{sanitize.clean_text(s.get('description', ''))}"
                for s in skills)

        if sub == "dossier":
            return _dossier_view()

        if sub == "intents":
            return _intents_view(rest)

        if sub in ("findings", "matches"):   # "matches" kept as a silent alias
            return _matches_view(matchmaker.load_state())

        if sub == "log":
            return _log_view(matchmaker.load_state())

        if sub == "card":
            state = matchmaker.load_state()
            if rest.strip().lower() == "apply":
                return _card_apply(state, card, client)
            return _card_view(state, card)

        if sub == "ask":
            parts2 = rest.split(maxsplit=1)
            if len(parts2) < 2:
                return ("Usage: /hermix ask <handle> <question>\n"
                        "I'll ask their agent privately and report back. "
                        "Their human is never contacted.")
            who = parts2[0].lstrip("@")
            try:
                ring1 = dossier.get_ring1()
            except Exception:
                ring1 = []
            state = matchmaker.load_state()
            res = matchmaker.start_ask(state, client, card, who, parts2[1],
                                       ring1, llm)
            matchmaker.save_state(state)
            if not res.get("ok"):
                return f"Couldn't start that: {res.get('error')}"
            return (f"Asking @{who} now — their human isn't involved. "
                    "I'll come back when I have something; no need to wait.")

        if sub == "asks":
            return matchmaker.ask_status(matchmaker.load_state(),
                                         rest.strip().lstrip("@") or None)

        if sub == "why":
            fid = rest.strip()
            if not fid:
                return "Usage: /hermix why <finding-id>  (the [id] with a finding)"
            return matchmaker.receipt(matchmaker.load_state(), fid)

        if sub == "intro":
            who = rest.strip().lstrip("@")
            if not who:
                return "Usage: /hermix intro <handle>  — previews, sends nothing"
            try:
                contact = dossier.get_contact()
            except Exception:
                contact = {}
            p = matchmaker.intro_preview(matchmaker.load_state(), who, contact)
            return matchmaker.format_intro_preview(p)

        if sub == "feedback":
            parts2 = rest.split(maxsplit=1)
            if len(parts2) < 2:
                return ("Usage: /hermix feedback <finding-id> "
                        "<useful|wrong_fit|too_early|spam>")
            state = matchmaker.load_state()
            res = matchmaker.record_feedback(state, parts2[0], parts2[1])
            matchmaker.save_state(state)
            if not res.get("ok"):
                return (f"Didn't recognise that. Use one of: "
                        f"{', '.join(matchmaker.FEEDBACK_VERDICTS)}")
            try:
                client.send_feedback(parts2[0], res["verdict"], res.get("handle", ""))
            except Exception:
                pass
            note = {
                "useful": "Noted — I'll bring you more like that.",
                "too_early": "Noted — right person, wrong moment. I'll park them.",
                "wrong_fit": "Noted — I'll raise the bar and set them aside.",
                "spam": "Noted — you won't hear about them again.",
            }[res["verdict"]]
            return note

        if sub in ("block", "unblock", "blocked", "report"):
            return _safety_view(sub, rest, client)

        if sub == "briefing":
            return _briefing_view(rest, card, llm)

        if sub == "doctor":
            if rest.strip().lower() in ("repair", "fix"):
                from . import envoy_profile
                res = envoy_profile.ensure()
                envoy_profile.install_skills(
                    pathlib.Path(__file__).resolve().parent / "skills")
                head = ("Created the envoy profile." if res.get("created")
                        else ("Repaired: " + ", ".join(res["repaired"])
                              if res.get("repaired") else "Nothing to repair."))
                return head + "\n\n" + _doctor_view()
            return _doctor_view()

        if sub == "pause":
            return _pause()

        if sub == "resume":
            return _resume()

        if sub == "leave":
            return _leave(client)

        return (f"Unknown subcommand '{sub}'. Try: status | profile | discover | "
                "signals | search <q> | skills | dossier | intents | findings | "
                "log | card | card apply | ask <handle> <q> | asks | why <id> | "
                "intro <handle> | feedback <id> <verdict> | block <handle> | "
                "unblock <handle> | blocked | report <handle> <reason> | "
                "briefing | doctor | update | pause | resume | leave")

    return handler


# --------------------------------------------------------------------------- #
# Opt-out: pause / resume / leave. These flip the matchmaker's ``paused`` flag
# (run_cycle returns SILENT while set, so the daemon + hermix_scout tool
# no-op) and, for leave, remove the published card from the hub. The local
# dossier is NEVER touched by any of these.
# --------------------------------------------------------------------------- #

def _pause() -> str:
    state = matchmaker.load_state()
    state["paused"] = True
    matchmaker.save_state(state)
    return ("Paused. I've stopped looking — no digs, no new outreach, and "
            "I won't interrupt you. Your public card is still up. Say "
            "`/hermix resume` to start again.")


def _resume() -> str:
    state = matchmaker.load_state()
    was = bool(state.get("paused"))
    state["paused"] = False
    matchmaker.save_state(state)
    if not was:
        return "I was already looking — nothing to resume."
    return ("Resumed. I'll go back to quietly digging for you and only surface "
            "something when it's genuinely worth it.")


def _resume_matchmaking() -> bool:
    """Clear the paused flag as a side effect of re-publishing a card. Returns
    True only if it was actually paused (so the caller can note the re-join)."""
    state = matchmaker.load_state()
    if not state.get("paused"):
        return False
    state["paused"] = False
    matchmaker.save_state(state)
    return True


def _leave(client) -> str:
    """Remove the published card + discovery vectors from the hub and stop
    matchmaking, while keeping everything local (the dossier stays put). The
    account persists on the hub, so re-publishing a card (/hermix profile)
    re-joins later."""
    removed = False
    try:
        res = client.remove_profile()
        removed = bool(isinstance(res, dict) and res.get("ok")) or res is not None
    except Exception:
        removed = False
    state = matchmaker.load_state()
    state["paused"] = True
    matchmaker.save_state(state)
    head = ("Left the network. I removed your public card and discovery vectors "
            "from the hub"
            if removed else
            "Stopped looking and paused you locally, but I couldn't reach "
            "the hub to remove your public card — I'll treat you as off the "
            "network here")
    return (head + ", and I've stopped looking for you.\n\n"
            "Your private dossier stays right here on this machine — nothing "
            "local was deleted. Whenever you want back in, just re-publish a "
            "card with `/hermix profile {...}` and you'll re-join.")


def _dossier_view() -> str:
    """Human-facing dossier summary. Shows Ring-0 section COUNTS (never values),
    Ring-1 facts, standing intents, and whether contact identity is on file —
    but never the contact values themselves (dossier.summary enforces this)."""
    s = dossier.summary()
    lines = ["Your dossier (private — never leaves this machine):", "",
             "Ring 0 (private) — kept local, used only to reason for you:"]
    for sec, n in s["ring0_counts"].items():
        lines.append(f"  • {sec}: {n}")
    lines.append("")
    lines.append("Ring 1 (shareable in agent-to-agent conversation):")
    if s["ring1"]:
        for f in s["ring1"]:
            lines.append(f"  • {f}")
    else:
        lines.append("  (none yet)")
    lines.append("")
    active = [i for i in s["intents"] if i.get("status") == "active"]
    lines.append("Standing intents (active):")
    if active:
        for i in active:
            lines.append(f"  • [{i.get('id')}] {i.get('text')}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append(f"Contact identity on file: {'yes' if s['contact_set'] else 'no'} "
                 "— never shared without your explicit, per-time approval.")
    return "\n".join(lines)


def _intents_view(rest: str) -> str:
    """`/hermix intents` lists; `intents add <text>` and `intents retire <id>`
    mutate."""
    parts = (rest or "").split(maxsplit=1)
    verb = parts[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""
    if verb == "add" and arg:
        it = dossier.add_intent(arg)
        # The human asked us to hunt for something: strong interest signal, so
        # lower the bar for interrupting them about it.
        _note_engagement("intent_added", 1.5)
        return (f"Added standing intent [{it['id']}]: {it['text']}"
                if it else "Nothing to add.")
    if verb == "retire" and arg:
        it = dossier.retire_intent(arg)
        return f"Retired intent [{arg}]." if it else f"No intent with id '{arg}'."
    intents = dossier.list_intents()
    if not intents:
        return ("No standing intents yet. Add one with:\n"
                "  /hermix intents add <what to hunt for>")
    lines = ["Standing intents:"]
    for i in intents:
        lines.append(f"  • [{i.get('id')}] ({i.get('status')}) {i.get('text')}")
    return "\n".join(lines)


def _ago(ts) -> str:
    if not ts:
        return "never"
    d = max(0, int(time.time()) - int(ts))
    if d < 90:
        return f"{d}s ago"
    if d < 5400:
        return f"{d // 60}m ago"
    if d < 172800:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


def _engine_line() -> str:
    """Make the matchmaking engine's health VISIBLE.

    The engine once silently never ran for a whole day while the agent looked
    perfectly healthy (polling fine, zero digs). Anything that can quietly stop
    has to report itself."""
    try:
        st = matchmaker.load_state()
    except Exception:
        return "Scouting: state unavailable."
    if st.get("paused"):
        return "Scouting: PAUSED (say `/hermix resume` to restart)."
    hb = st.get("engine") or {}
    last = hb.get("last_success_at")
    lines = [f"Scouting: last cycle {_ago(last)}"
             f" · {hb.get('cycles_total', 0)} total"]
    if last is None:
        lines[0] += "  ← never run; the background daemon should do this"
    lines.append(
        f"  digs open: {len(st.get('digs') or {})} · "
        f"findings ready: {len(((st.get('outbox') or {}).get('ready')) or [])} · "
        f"awaiting delivery: {len(((st.get('outbox') or {}).get('inflight')) or [])}")
    if hb.get("last_error"):
        lines.append(f"  last error ({_ago(hb.get('last_error_at'))}): "
                     f"{sanitize.clean_text(hb['last_error'], max_len=120)}")
    return "\n".join(lines)


def _note_engagement(kind: str, weight: float = 1.0) -> None:
    """Record that the human leaned in. Cheap + best-effort: a failure here must
    never break the command the human actually ran."""
    try:
        st = matchmaker.load_state()
        matchmaker.record_engagement(st, kind, weight)
        matchmaker.save_state(st)
    except Exception:
        pass


def _matches_view(state) -> str:
    """Everything held back for the next natural conversation, plus recent
    verdicts. All candidate content already passed sanitize on the way in."""
    # Asking to see findings IS interest — it lowers the bar for a while.
    _note_engagement("asked_for_matches", 1.0)
    lines = []
    queue = state.get("queue") or []
    if queue:
        lines.append("Pending (queued for the next quiet slot):")
        for it in queue:
            lead = (f'you asked me to find "{it.get("intent")}" — '
                    if it.get("intent") else "")
            lines.append(f"  • @{it.get('handle', '')} — {lead}{it.get('pitch', '')}")
    else:
        lines.append("Nothing queued right now.")

    reveals = state.get("pending_reveals") or []
    if reveals:
        lines.append("")
        lines.append("Reveal requests awaiting YOUR approval (contact is never "
                     "released without your explicit yes):")
        for r in reveals:
            ctx = r.get("context", "")
            lines.append(f"  • @{r.get('handle', '?')} — {ctx}")
            lines.append(f"    To connect, say yes, then: hermix_reveal_respond("
                         f"thread_id='{r.get('thread_id', '')}', approve=true, "
                         "human_approved=true)")

    seen = state.get("seen") or {}
    if seen:
        recent = sorted(seen.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)[:8]
        lines.append("")
        lines.append("Recent verdicts:")
        for handle, rec in recent:
            lines.append(f"  • @{handle}: {rec.get('verdict', '?')} "
                         f"({_ts(rec.get('ts', 0))})")
    return "\n".join(lines)


def _log_view(state) -> str:
    entries = (state.get("log") or [])[-20:]
    if not entries:
        return "No matchmaker activity yet."
    lines = ["Last matchmaker decisions:"]
    for e in entries:
        note = e.get("note", "")
        note = f" — {note}" if note else ""
        lines.append(f"  {_ts(e.get('ts', 0))}  @{e.get('handle', '')}  "
                     f"[{e.get('verdict', '')}]{note}")
    return "\n".join(lines)


def _card_view(state, card) -> str:
    proposal = state.get("card_proposal")
    current = card.public_dict()
    if not proposal or not proposal.get("proposed"):
        return ("Current PUBLIC card:\n" + json.dumps(current, indent=2) +
                "\n\n(No refreshed proposal pending.)")
    proposed = proposal["proposed"]
    lines = [f"Proposed card refresh (from {_ts(proposal.get('ts', 0))}). "
             "The matchmaker only sharpens wording of what you already have — "
             "review and run `/hermix card apply` to accept.", ""]
    for k in profile.PUBLIC_FIELDS:
        if k not in proposed:
            continue
        cur, new = current.get(k), proposed.get(k)
        if cur != new:
            lines.append(f"  {k}:")
            lines.append(f"    - now: {json.dumps(cur, ensure_ascii=False)}")
            lines.append(f"    + new: {json.dumps(new, ensure_ascii=False)}")
    if len(lines) == 2:
        lines.append("  (No differences from your current card.)")
    return "\n".join(lines)


def _card_apply(state, card, client) -> str:
    proposal = state.get("card_proposal")
    if not proposal or not proposal.get("proposed"):
        return "Nothing to apply — no proposed card refresh pending."
    proposed = proposal["proposed"]
    applied = []
    for k, v in proposed.items():
        if k in profile.PUBLIC_FIELDS:
            setattr(card, k, v)
            applied.append(k)
    profile.save_card(card)
    client.publish_profile(card.public_dict())
    state["card_proposal"] = None
    matchmaker.save_state(state)
    return ("Applied & republished the refreshed card (fields: "
            + ", ".join(applied) + "):\n" + json.dumps(card.public_dict(), indent=2))


def install_gate(**kwargs):
    """pre_tool_call hook: the human-consent membrane for the two irreversible
    actions — installing a network skill, and releasing your human's contact
    identity in a reveal.

    Return contract verified against Hermes source
    (hermes_cli/plugins.py::_get_pre_tool_call_directive_details, see
    docs/HERMES-API-GROUND-TRUTH.md §4): the hook is invoked kwargs-only with
    ``tool_name`` and the argument dict ``args``. To BLOCK a call, return
    ``{"action": "block", "message": "<reason>"}`` (the message becomes the
    tool result the model sees); return ``None`` to allow. There is no
    ``allow``/``reason`` shape.

    Blocks:
      - hermix_install_skill unless ``approved=True``.
      - hermix_reveal_request with ``include_contact=True`` unless
        ``human_approved=True``.
      - hermix_reveal_respond with ``approve=True`` unless
        ``human_approved=True``.

    Contact identity therefore cannot move without the human having said yes and
    the caller having set ``human_approved=true`` on THIS specific call.
    """
    tool = kwargs.get("tool_name")
    args = kwargs.get("args") or {}

    if tool == "hermix_install_skill" and not args.get("approved"):
        return {
            "action": "block",
            "message": "Installing a network skill requires human approval. "
                       "Confirm in chat first, then retry with approved=true.",
        }

    if tool == "hermix_reveal_request" and args.get("include_contact") \
            and not args.get("human_approved"):
        return {
            "action": "block",
            "message": "Sending your human's contact identity (name/email/"
                       "socials) requires their explicit approval. Ask your "
                       "human directly, get a clear yes, then retry this call "
                       "with human_approved=true.",
        }

    if tool == "hermix_reveal_respond" and args.get("approve") \
            and not args.get("human_approved"):
        return {
            "action": "block",
            "message": "Releasing your human's contact identity requires their "
                       "explicit approval. Ask your human directly, get a clear "
                       "yes, then retry this call with human_approved=true.",
        }

    return None  # allow everything else


# --------------------------------------------------------------------------- #
# First-run onboarding nudge — a pre_llm_call hook (works in gateway mode).
#
# The register() bootstrap uses ctx.inject_message, which is a NO-OP in gateway
# mode (Telegram/Discord/etc.) — so gateway users enable the plugin, restart,
# say "hello", and the agent has no idea it should onboard. This hook rides the
# pre_llm_call channel, which DOES fire in gateway mode: a callback may return
# ``{"context": <text>}`` (or a plain string) and Hermes appends that text to
# the CURRENT turn's user message — never the system prompt, so the prompt-cache
# prefix is preserved. Return ``None`` to contribute nothing. Contract verified
# against the real Hermes source: hermes_cli/plugins.py::PluginManager.invoke_hook
# (docstring: pre_llm_call returns {"context": str} | str) and
# agent/turn_context.py (which reads r["context"] / str(r) and injects it into
# the user message). See docs/HERMES-API-GROUND-TRUTH.md §4.
#
# Cost discipline: once onboarding is confirmed we latch that in module state
# and never touch disk again (zero steady-state IO). An in-memory throttle keeps
# repeat calls within the hour free of IO too. Only the rare
# first-call-per-hour-while-not-onboarded actually reads state (one small file,
# then one more only if not onboarded).
# --------------------------------------------------------------------------- #

ONBOARDING_NUDGE_CONTEXT = (
    "Hermix is installed but not set up. Before continuing, tell your human it "
    "takes ~2 minutes and run the hermix:hermix-onboarding skill with them "
    "now. If they decline, call the hermix_pause tool so you are not reminded "
    "again."
)

_NUDGE_INTERVAL_SECONDS = 3600  # throttle: at most one nudge per hour

# Per-process module state. ``_onboarded`` latches True forever once we confirm
# onboarding, so the steady state costs zero file reads. ``_last_nudge_ts``
# mirrors the persisted stamp so throttled calls also do zero IO.
_onboarded = False
_last_nudge_ts = None


def _now():
    """Clock seam so tests can drive throttling with a fake clock."""
    return time.time()


def _reset_nudge_state():
    """Test seam: clear the in-process nudge caches."""
    global _onboarded, _last_nudge_ts
    _onboarded = False
    _last_nudge_ts = None


def onboarding_nudge(**kwargs):
    """pre_llm_call hook. Returns ``{"context": <text>}`` to steer the agent to
    run onboarding on the human's first message when the plugin is installed but
    not yet set up — and ``None`` the vast majority of the time (onboarded,
    paused, or throttled). Kwargs-only per the Hermes hook contract."""
    global _onboarded, _last_nudge_ts

    # Steady state after onboarding: never touch disk again.
    if _onboarded:
        return None

    now = _now()

    # In-memory throttle: repeat calls within the window do zero IO.
    if _last_nudge_ts is not None and (now - _last_nudge_ts) < _NUDGE_INTERVAL_SECONDS:
        return None

    # Onboarded? (one small file read) -> latch and go silent forever.
    if dossier.is_onboarded():
        _onboarded = True
        return None

    # Not onboarded: consult matchmaker state for the opt-out flag + the
    # persisted last-nudge stamp (one small file read).
    state = matchmaker.load_state()
    if state.get("paused"):
        return None  # human declined / left — never nag someone who said no

    last = state.get("onboarding_nudge_ts")
    if last is not None and (now - last) < _NUDGE_INTERVAL_SECONDS:
        _last_nudge_ts = last  # rehydrate the throttle after a process restart
        return None

    state["onboarding_nudge_ts"] = int(now)
    matchmaker.save_state(state)
    _last_nudge_ts = now
    return {"context": ONBOARDING_NUDGE_CONTEXT}


def _briefing_view(rest: str, card, llm) -> str:
    """Show, rebuild or delete what the envoy believes about its human.

    This command is the whole trust story for the briefing: if the human cannot
    read it, they have no way to know what their envoy thinks of them.
    """
    from . import briefing, dossier
    arg = (rest or "").strip().lower()

    if arg in ("clear", "delete", "off"):
        briefing.clear()
        return ("Briefing deleted. Your envoy now represents you from your "
                "public card alone — less able to judge what you'd care "
                "about, but it will never guess.")

    if arg in ("refresh", "rebuild", "update"):
        try:
            doc = briefing.generate(dossier.load(), card, llm)
        except Exception as e:
            return f"Couldn't rebuild the briefing: {sanitize.clean_text(str(e), max_len=120)}"
        if not doc.get("lines"):
            reason = doc.get("reason") or "nothing abstract enough survived the check"
            return (f"No briefing written — {reason}.\n"
                    "Your envoy keeps working from your public card.")
        briefing.save(doc)
        note = ""
        if doc.get("dropped"):
            note = (f"\n\n({doc['dropped']} generated line(s) were dropped for "
                    "naming something concrete — that check is deliberately strict.)")
        return briefing.format_for_human(doc) + note

    if arg:
        return "Usage: /hermix briefing [refresh | clear]"
    return briefing.format_for_human()


def _doctor_view() -> str:
    """Assert the envoy profile is still locked down.

    The tool denylist is the ONLY thing standing between the envoy and the
    dossier (Hermes profiles are not a sandbox), so a missing entry is reported
    as a fault, never as a warning.
    """
    from . import envoy_profile, briefing
    lines = ["Hermix doctor", ""]

    info = envoy_profile.info()
    lines.append(f"Envoy profile: {info['path']}")
    if not info["exists"]:
        lines += [
            "  NOT PRESENT — your envoy is running from your public card only.",
            "  This is degraded, not broken. `/hermix doctor repair` or a "
            "plugin restart will create it.",
        ]
    else:
        problems = info["problems"]
        if problems:
            lines.append(f"  {len(problems)} PROBLEM(S) — fix before trusting the membrane:")
            for prob in problems:
                lines.append(f"    ! {prob}")
            lines.append("  Restart the plugin (or run `/hermix doctor repair`) "
                         "to restore the pinned files.")
        else:
            lines.append(f"  OK — SOUL pinned at {info['soul']}, tools locked "
                         "down, no credentials, real HOME hidden.")

    doc = briefing.load()
    n = len(doc.get("lines") or [])
    lines.append("")
    lines.append(f"Briefing: {n} line(s)" if n else
                 "Briefing: none (card-only judgement)")
    if n:
        lines.append("  `/hermix briefing` to read exactly what it says.")

    # Is the proactive ping actually alive? A cron job that exists but never
    # fires looks exactly like a healthy quiet network from the outside, so
    # without this the failure is invisible for as long as it lasts.
    from . import matchmaker as _mm
    try:
        health = _mm.delivery_health()
    except Exception:
        health = None
    if health:
        lines.append("")
        status = health["status"]
        if status == "ok":
            hrs = (health["age_seconds"] or 0) / 3600.0
            lines.append(f"Delivery: OK — last ran {hrs:.1f}h ago.")
        elif status == "daemon-fallback":
            lines += ["Delivery: no scheduler on this Hermes — the background "
                      "loop carries it instead.",
                      "  Findings still arrive, just alongside your next "
                      "conversation rather than unprompted."]
        elif status == "missing":
            lines += ["Delivery: PROBLEM — the scheduled job is gone.",
                      "  Conversations continue, but nothing will reach you "
                      "unprompted. Restart the plugin to recreate it."]
        elif status == "never-fired":
            lines += ["Delivery: PROBLEM — the job is scheduled but has never "
                      "run.",
                      "  Nothing has reached you unprompted, and nothing will "
                      "until it fires."]
        elif status == "stalled":
            hrs = (health["age_seconds"] or 0) / 3600.0
            lines += [f"Delivery: PROBLEM — last ran {hrs:.0f}h ago, well past "
                      f"the {health['interval_seconds'] // 3600}h schedule.",
                      "  Findings are queued and safe; they just are not being "
                      "handed to you."]

    # Who pays for the network's thinking. This is a promise in the README, so
    # it should be inspectable rather than something the user has to take on
    # faith — and if they ever opted into paying, they should be able to see it.
    from . import _config
    mode = _config.llm_mode()
    lines.append("")
    if mode == "hub":
        lines.append("Inference: hub only — network work never touches your "
                     "own model budget.")
    elif mode == "auto":
        lines += [
            "Inference: auto — hub first, BUT falls back to YOUR model (and "
            "your bill) whenever the hub is unreachable.",
            "  Unset HERMIX_LLM to return to hub-only.",
        ]
    else:
        lines += [
            "Inference: local — ALL network work runs on your own model, at "
            "your expense.",
            "  Unset HERMIX_LLM to return to hub-only.",
        ]

    try:
        lines += ["", _engine_line()]
    except Exception:
        pass
    return "\n".join(lines)


REPORT_REASONS = ("spam", "harassment", "impersonation", "scam", "other")


def _safety_view(sub: str, rest: str, client) -> str:
    """Block, unblock, list blocks, report.

    Blocking is enforced by the HUB, not by us: a counterpart runs their own
    client, so "stop contacting me" can never depend on their goodwill. It is
    one-sided to create and two-sided in effect — they disappear from our
    discovery and we from theirs, and neither can open a thread with the other.
    They are never told.
    """
    parts = (rest or "").split(maxsplit=1)
    who = parts[0].lstrip("@") if parts else ""
    extra = parts[1] if len(parts) > 1 else ""

    if sub == "blocked":
        try:
            rows = client.list_blocks()
        except Exception as e:
            return f"Couldn't read your block list: {sanitize.clean_text(str(e), max_len=120)}"
        if not rows:
            return "You haven't blocked anyone."
        out = ["Blocked — they can't reach you and won't be surfaced to you:"]
        for r in rows:
            reason = sanitize.clean_text(str(r.get("reason") or ""), max_len=80)
            out.append(f"  • @{sanitize.clean_text(str(r.get('blocked','')))}"
                       + (f" — {reason}" if reason else ""))
        return "\n".join(out)

    if not who:
        if sub == "report":
            return ("Usage: /hermix report <handle> <reason> [detail]\n"
                    f"Reasons: {', '.join(REPORT_REASONS)}. This goes to the "
                    "network operator, never to them. It does NOT block them — "
                    "use `/hermix block` for that.")
        return f"Usage: /hermix {sub} <handle>"

    if sub == "block":
        try:
            client.block(who, extra.strip()[:200])
        except Exception as e:
            return f"Couldn't block @{who}: {sanitize.clean_text(str(e), max_len=120)}"
        _note_engagement("blocked", 0.0)
        return (f"Blocked @{who}. They can't open a conversation with you, they "
                "won't appear in what I look through, and you won't appear in "
                "theirs. They are not told.\n"
                "`/hermix unblock " + who + "` to undo it.")

    if sub == "unblock":
        try:
            res = client.unblock(who)
        except Exception as e:
            return f"Couldn't unblock @{who}: {sanitize.clean_text(str(e), max_len=120)}"
        return (f"Unblocked @{who}." if res.get("removed")
                else f"@{who} wasn't blocked.")

    # report
    bits = extra.split(maxsplit=1)
    reason = (bits[0].lower() if bits else "")
    detail = bits[1] if len(bits) > 1 else ""
    if reason not in REPORT_REASONS:
        return ("Which reason? " + ", ".join(REPORT_REASONS)
                + f"\n  /hermix report {who} spam <what happened>")
    try:
        res = client.report(who, reason, detail[:1000])
    except Exception as e:
        return f"Couldn't send that report: {sanitize.clean_text(str(e), max_len=120)}"
    n = int(res.get("distinct_reporters") or 1)
    tail = (f" You're the {n}th person to report them." if n > 1 else "")
    return (f"Reported @{who} to the network operator.{tail} They are not told, "
            "and this does not block them — say `/hermix block " + who + "` "
            "if you also want them to stop reaching you.")
