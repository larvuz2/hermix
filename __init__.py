"""Hermix — Hermes plugin entry point.

The plugin is the "embassy": it connects the local Hermes agent to the Hermix
agent-to-agent network while keeping the private agent behind the envoy
membrane (see envoy.py). Everything is wired from register(ctx).

NOTE: the exact ctx.* signatures below follow the documented Hermes plugin API.
Confirm register_command / register_hook / inject_message / ctx.llm signatures
against your Hermes version and adjust the thin adapters if they differ — the
plugin's own logic lives in the sibling modules and is API-independent.
"""
import logging
import pathlib

log = logging.getLogger("hermix")

# The five behavioral skills that steer the agent's Hermix behavior. They are
# opt-in explicit loads (register_skill does NOT add them to <available_skills>);
# the agent resolves them as hermix:<name> when a situation calls for one.
_BEHAVIORAL_SKILLS = [
    ("hermix-context",
     "World model for the Hermix network — entities, rings, public vs private."),
    ("hermix-voice",
     "How to talk to your human about Hermix — tone, framing, hard bans."),
    ("hermix-onboarding",
     "The one-time onboarding ritual: build the dossier, draft/publish the card."),
    ("hermix-envoy-protocol",
     "Rules for talking to other agents — digs, asks, reveals, rings, defense."),
    ("hermix-delivery",
     "When to speak vs stay quiet — the worth-it bar, intents, follow-ups."),
]


def _result_text(res) -> str:
    """Pull the reply text off a hub llm_complete result (frozen contract:
    {"text", "model", "tokens"}), tolerating an object with a ``.text`` attr."""
    if isinstance(res, dict):
        return res.get("text", "") or ""
    return getattr(res, "text", res) or ""


def _is_budget_429(exc) -> bool:
    """True when the hub refused because the OPERATOR is over budget.

    Distinguished from a transient 503/network failure because the two deserve
    opposite responses: a hub hiccup is worth falling back for, an exhausted
    operator budget is not the user's to pay for."""
    code = getattr(exc, "code", None) or getattr(exc, "status", None)
    if code == 429:
        return True
    return "budget" in str(exc).lower()


def make_llm(ctx, client):
    """Build the routed LLM callable the plugin hands to the envoy, matchmaker,
    and service. It decides — per ``_config.llm_mode()`` — whether the network's
    thinking runs on operator-paid HUB inference or on the user's own ctx.llm.

    Signature: ``llm(system, user, *, purpose="envoy") -> str``. ``system``/
    ``user`` stay positional (so every existing call site + test keeps working);
    ``purpose`` ("envoy" | "judge" | "refresh") is a keyword-only label that
    rides along to the hub so the operator can meter/route by call kind.

    Routing (see _config.llm_mode):
      - "hub":   hub ONLY. Success -> hub text. ANY failure, or not-live ->
                 the safe sentinel "" (callers already fail toward silence). The
                 user's model budget is NEVER spent.
      - "auto":  hub when live; on ANY hub failure (503/429/network) OR when not
                 live, fall back to ctx.llm so the plugin never goes mute.
      - "local": ctx.llm ONLY — the hub is never called.
    """
    from . import _config

    def _via_ctx(system: str, user: str) -> str:
        """The user's own model (agent/plugin_llm.py::PluginLlm.complete):
        synchronous, OpenAI-shaped ``messages``, text on ``.text``.
        See docs/HERMES-API-GROUND-TRUTH.md §5. Billed to the USER."""
        result = ctx.llm.complete(messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])
        return result.text

    def llm(system: str, user: str, *, purpose: str = "envoy") -> str:
        mode = _config.llm_mode()
        if mode == "local":
            return _via_ctx(system, user)
        # mode is "hub" or "auto": prefer operator-paid hub inference.
        if _config.is_live():
            try:
                res = client.llm_complete(
                    [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
                    purpose,
                )
                return _result_text(res)
            except Exception as e:  # 503/429/network — hub unavailable
                log.debug("hub llm_complete(%s) failed: %s", purpose, e)
                if _is_budget_429(e):
                    # OUR cap, not their problem. Falling back here would quietly
                    # spend the user's own model budget on network work — the one
                    # thing Hermix promises never to do. Go quiet instead; the
                    # operator sees the budget on the dashboard and raises it.
                    log.warning("hermix: hub inference budget exhausted — "
                                "staying quiet rather than billing the user")
                    return ""
                if mode == "hub":
                    return ""             # fail toward silence; never bill user
                return _via_ctx(system, user)   # auto: fall back to local
        # Not live (no hub configured).
        if mode == "hub":
            return ""                     # hub-only: cannot bill operator, hush
        return _via_ctx(system, user)     # auto with no hub -> local
    return llm


def register(ctx):
    import time
    from . import profile, tools, commands, service, matchmaker, dossier, _config
    from .client import HermixClient, make_transport

    # The Hermies -> Hermix rename moved our data directory. Carry the human's
    # dossier and history across BEFORE anything reads them, or an upgrading
    # user looks brand new and starts over from an empty card.
    try:
        from . import migrate
        migrate.run()
    except Exception as e:
        log.debug("rename migration skipped: %s", e)

    card = profile.load_card()
    client = HermixClient(make_transport())

    # Frictionless auto-join + connectivity check. If the hub is reachable and
    # we already have an onboarded card but no key (e.g. upgraded from an offline
    # install), claim the handle now. Fresh users register on first publish.
    from .client import ensure_registered
    from . import throttle
    connected = False
    # Every Hermes process — including every subagent — runs register(). These
    # two calls are the only hub traffic here, and unguarded they scaled with
    # the process count rather than the agent count (measured: ~48 req/min from
    # two agents). A shared gate lets the first process in the window do it and
    # the rest come up on the cached copy. Joining is NEVER gated: an agent
    # without a key has to be able to claim its handle immediately.
    needs_join = bool(card.public_dict().get("handle")) and not _config.api_key()
    status = {"connected": False}

    def _startup_network():
        try:
            if _config.has_hub() and (needs_join
                                      or throttle.due("startup", _config.startup_gate_seconds())):
                status["connected"] = client.healthz()
                if status["connected"] and needs_join:
                    ensure_registered(client, card)
        except Exception as e:
            log.debug("connectivity/auto-join check skipped: %s", e)
        # Pull the hub's live tuning on load so a restart comes up on current
        # settings. Not forced: refresh() shares its staleness clock across
        # processes, so this is a no-op for the rest of the interval.
        try:
            if _config.is_live():
                from . import remote_config
                remote_config.refresh(client)
        except Exception as e:
            log.debug("remote config refresh skipped: %s", e)

    # Hermes disables a plugin whose register() takes longer than
    # plugins.load_timeout_seconds (10 s by default), and these calls carry
    # 8-15 s network timeouts. Run them on a thread and wait briefly: a healthy
    # hub answers inside the wait exactly as before, a slow one finishes in the
    # background instead of costing the human their plugin.
    import threading
    t_net = threading.Thread(target=_startup_network, name="hermix-startup",
                             daemon=True)
    t_net.start()
    t_net.join(timeout=3.0)
    connected = status["connected"]

    # The routed LLM adapter: operator-paid hub inference (with local fallback),
    # so users never bring their own model key. See make_llm above.
    llm = make_llm(ctx, client)

    def inject(content: str, role: str = "user"):
        try:
            ctx.inject_message(content, role=role)
        except Exception as e:  # gateway mode / not available — degrade quietly
            log.debug("inject_message unavailable: %s", e)

    # --- commands ---
    ctx.register_command(
        "hermix",
        commands.make_handler(client, card, llm),
        "Manage your Hermix profile, discover agents, see findings & decisions",
    )

    # --- tools (private agent works the network agentically) ---
    for spec in tools.build(client, card, llm):
        ctx.register_tool(
            name=spec["name"],
            toolset="hermix",
            schema=spec["schema"],
            handler=spec["handler"],
            description=spec["description"],
        )

    # --- approval gate for skill installs AND contact reveals ---
    ctx.register_hook("pre_tool_call", commands.install_gate)

    # --- first-run onboarding nudge (gateway-safe) ---
    # inject_message (below) is a no-op in gateway mode, so gateway users would
    # otherwise never be prompted to onboard. pre_llm_call DOES fire in gateway
    # mode: this hook appends a one-time nudge to the human's first message so
    # the agent runs onboarding before anything else. See commands.onboarding_nudge
    # and docs/HERMES-API-GROUND-TRUTH.md §4.
    ctx.register_hook("pre_llm_call", commands.onboarding_nudge)

    # --- the envoy's own Hermes profile ---------------------------------
    # A separate HERMES_HOME holding the pinned SOUL, the briefing and the
    # envoy's network memory, so the membrane is a filesystem boundary rather
    # than a convention inside this process. See docs/DESIGN-ENVOY-PROFILE.md.
    # Gated: this touches disk and re-verifies the SOUL hash, and register()
    # runs in every spawned Hermes process. Never fatal — a failure here just
    # means the card-only envoy, which is degraded rather than broken.
    try:
        from . import envoy_profile, throttle
        if throttle.due("envoy-profile", _config.startup_gate_seconds()):
            res = envoy_profile.ensure()
            envoy_profile.install_skills(
                pathlib.Path(__file__).resolve().parent / "skills")
            if res.get("created"):
                log.info("hermix: created the envoy profile at %s",
                         envoy_profile.profile_dir())
            for fixed in res.get("repaired") or []:
                # A modified envoy SOUL is either a bug or an attack; say so.
                log.warning("hermix: restored envoy profile (%s)", fixed)
    except Exception as e:
        log.debug("envoy profile setup skipped: %s", e)

    # --- behavioral skills (opt-in explicit loads) ---
    if hasattr(ctx, "register_skill"):
        skill_root = pathlib.Path(__file__).resolve().parent / "skills"
        for sname, sdesc in _BEHAVIORAL_SKILLS:
            try:
                ctx.register_skill(sname, skill_root / sname / "SKILL.md", sdesc)
            except Exception as e:  # a packaging hiccup must not kill the plugin
                log.debug("register_skill(%s) skipped: %s", sname, e)

    # --- first-run bootstrap: point the agent at onboarding, once ---
    # inject_message is the safest surface (no-op in gateway mode, never raises —
    # see docs/HERMES-API-GROUND-TRUTH.md §6). We say it ONCE, only while the
    # dossier does not exist / is not onboarded; the onboarding skill drives the
    # actual consented setup with the human.
    try:
        onboarded = dossier.is_onboarded()
    except Exception as e:
        log.debug("onboarding state check skipped: %s", e)
        onboarded = False
    if not onboarded:
        inject(
            "Hermix is installed but not set up yet. Next time you talk "
            "with your human, run the hermix:hermix-onboarding skill "
            "together to build their private dossier and public card before "
            "doing anything else on the network.",
            role="user",
        )

    # --- the notification path: prefer the blessed cron scheduler ---
    # The cron job calls hermix_scout a few times a day and relays the
    # result only when it is not the silent marker. If cron is unavailable
    # (older Hermes / tests), fall back to running matchmake inside the daemon
    # loop and surfacing results via /hermix matches (degraded mode).
    # --- DELIVERY plane: the cron job relays what the engine already produced.
    # Its success or failure NEVER gates the engine below — a cron job that
    # exists but never fires previously disabled matchmaking entirely.
    cron_ok = matchmaker.ensure_cron()

    # --- EXECUTION plane: the daemon ALWAYS runs the matchmaking engine.
    def engine():
        return matchmaker.run_engine_and_persist(client, card, llm, time.time)

    # inject_message is a no-op in gateway mode; when it can't reach the human
    # we skip the signal digest instead of fetching data to discard.
    inject_works = bool(getattr(ctx, "inject_message", None)) and not cron_ok

    service.start(client, card, inject, llm, engine=engine,
                  match_interval=_config.match_every_hours() * 3600,
                  inject_works=inject_works)

    if not _config.has_hub():
        mode = "offline/mock"
    elif _live():
        mode = "connected+live"
    elif connected:
        mode = "hub-reachable/unregistered"
    else:
        mode = "hub-unreachable"
    notify = "cron" if cron_ok else "daemon-fallback"
    log.info("hermix registered (%s, notify=%s, onboarded=%s) hub=%s handle=%s",
             mode, notify, "yes" if onboarded else "no",
             _config.service_url() or "-",
             card.public_dict().get("handle") or "<unset>")


def _live() -> bool:
    from . import _config
    return _config.is_live()
