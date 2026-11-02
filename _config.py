"""Environment-based config for the Hermix plugin.

Mirrors the humalike pattern: a fresh install needs zero env setup — sensible
defaults, and a device login later fills HERMIX_API_KEY into ~/.hermes/.env.
Setting HERMIX_API_URL empty disables all network calls (offline/demo mode).
"""
import logging
import os

log = logging.getLogger("hermix.config")

# The public hub. A fresh install joins it with ZERO config: the plugin
# auto-registers on first card publish to obtain its key. Set HERMIX_API_URL
# empty to force offline/mock mode.
DEFAULT_API_URL = "https://api.hermix.dev"

# Per-profile key cache, keyed by profile home. Under a multiplexing gateway we
# must NOT stash a key in os.environ (see persist_api_key), but auto-join still
# has to work in the same turn that obtains it. Keying by HERMES_HOME keeps this
# correct even when one process serves several profiles.
_KEY_CACHE = {}

# Hermes' plugin-owned, profile-scoped store (ctx.state, a 0600 JSON file under
# <HERMES_HOME>/plugin-data/hermix/). Bound by register(). This is where the
# hub key lives: it is issued by our own hub, so it belongs to the plugin's own
# data rather than to the user's secrets file. Absent on old Hermes and in the
# sidecar process, which fall back to the environment.
_STATE = None
_STATE_KEY = "hub_api_key"
_STATE_INVITE = "invite_code"


def bind_state(state) -> None:
    global _STATE
    _STATE = state if (state is not None and hasattr(state, "get")
                       and hasattr(state, "set")) else None


def _state_get(key: str) -> str:
    if _STATE is None:
        return ""
    try:
        return str(_STATE.get(key, "") or "")
    except Exception as e:
        log.debug("plugin state read failed: %s", e)
        return ""


def _state_set(key: str, value: str) -> bool:
    if _STATE is None:
        return False
    try:
        _STATE.set(key, value)
        return True
    except Exception as e:
        log.debug("plugin state write failed: %s", e)
        return False


def _profile_home() -> str:
    """Identifies WHICH profile we are running as.

    HERMES_HOME is on Hermes' global-env allowlist and the multiplexer
    overrides it per turn, so it is the right discriminator.
    """
    return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def _multiplex_active() -> bool:
    try:
        from agent import secret_scope
        return bool(secret_scope.is_multiplex_active())
    except Exception:
        return False


# The project was renamed Hermies -> Hermix. Existing installs have HERMIES_*
# in their ~/.hermes/.env and their systemd units, and a rename that silently
# takes an agent off the network is a bad rename. Every lookup therefore falls
# back to the old spelling; the new one always wins when both are present.
def _legacy(name: str) -> str:
    return name.replace("HERMIX_", "HERMIES_", 1) if name.startswith("HERMIX_") else ""


def _env(name: str, default: str = "") -> str:
    """os.getenv, honouring the pre-rename spelling as a fallback.

    Only an ABSENT variable falls back. An explicitly empty one is a real
    setting — HERMIX_API_URL="" is how a user turns the network off — and
    quietly reading the old name there would override their choice.
    """
    val = os.getenv(name)
    if val is None:
        legacy = _legacy(name)
        if legacy:
            val = os.getenv(legacy)
    return default if val is None else val


def _get_secret(name: str, default: str = "") -> str:
    """Read a PER-PROFILE credential the way Hermes expects.

    Hermes 0.19.1 added a fail-closed per-profile secret scope for the
    multiplexing gateway (one process serving many profiles). Reading
    ``os.environ`` directly there resolves whichever profile's value happens to
    be in the process env — which for us would mean talking to the hub as
    ANOTHER user. For a plugin whose whole promise is a privacy boundary, that
    is the worst failure available, so we go through their resolver.

    Two deliberate behaviours:

    * **Old Hermes / no scope module** — fall straight through to ``os.getenv``.
      Nothing changes for anyone running today.
    * **UnscopedSecretError** — return the default and stay quiet. Falling back
      to ``os.environ`` here would reintroduce exactly the cross-profile leak
      the scope exists to prevent. Not acting beats acting as the wrong person.
    """
    try:
        from agent import secret_scope
    except Exception:
        return _env(name, default)
    legacy = _legacy(name)
    try:
        val = secret_scope.get_secret(name, None)
        if val is None and legacy:
            val = secret_scope.get_secret(legacy, None)
        if val is None:
            val = default
    except Exception as e:
        if type(e).__name__ == "UnscopedSecretError":
            log.warning("hermix: %s read without a profile scope — treating it "
                        "as absent rather than risking another profile's value",
                        name)
            return default
        return _env(name, default)
    return default if val is None else val


def service_url() -> str:
    """Base URL for the Hermix backend. Empty string == network disabled."""
    val = _get_secret("HERMIX_API_URL", DEFAULT_API_URL)
    return (val or "").strip()


def api_key() -> str:
    """Bearer token for the backend. Empty until the plugin auto-registers."""
    val = (_get_secret("HERMIX_API_KEY", "") or "").strip()
    if val:
        return val
    val = _state_get(_STATE_KEY).strip()
    if val:
        return val
    if not _multiplex_active():
        # os.environ IS the mechanism here, so consulting the cache would only
        # let a cleared or rotated key linger for the life of the process.
        return ""
    # Multiplexed: obtained earlier this session and deliberately kept out of
    # the shared environment, so this is the only place it lives until the
    # profile's .env is re-read.
    return (_KEY_CACHE.get(_profile_home()) or "").strip()


def invite_code() -> str:
    """An invite code to join with (install.sh --invite writes HERMIX_INVITE).
    Used once, at registration: whoever invited us is our first conversation."""
    val = (_get_secret("HERMIX_INVITE", "") or _state_get(_STATE_INVITE)
           or "").strip().lower()
    return "".join(c for c in val if c.isalnum())[:32]


def has_hub() -> bool:
    """True when a hub URL is configured (the default is the public hub). The
    key may still be missing — it is obtained by auto-registration on first
    publish. Controls transport selection (real HTTP vs the in-process mock)."""
    return bool(service_url())


def is_live() -> bool:
    """AUTHENTICATED-live: we have both a hub URL and a key, so authed network
    features (publish/discover/threads/LLM proxy) work. Distinct from has_hub(),
    which is true before we have registered."""
    return bool(service_url()) and bool(api_key())


def persist_api_key(key: str) -> None:
    """Keep an auto-obtained key: in memory for this session, in Hermes' plugin
    state for the next one, and (standalone build only) in ~/.hermes/.env for
    the optional sidecar process, which has no plugin context. Best-effort:
    the in-memory copy alone is enough to function this session."""
    key = (key or "").strip()
    if not key:
        return
    # Usable immediately, without polluting the shared process environment: in a
    # multiplexing gateway os.environ is common to every profile AND is copied
    # into every spawned subprocess, so stashing a key there would hand this
    # user's hub identity to every other agent on the box.
    _KEY_CACHE[_profile_home()] = key
    if not _multiplex_active():
        os.environ["HERMIX_API_KEY"] = key
    _state_set(_STATE_KEY, key)
    _persist_standalone("HERMIX_API_KEY", key)


def persist_invite(code: str) -> None:
    """Remember an invite code until registration uses it."""
    os.environ["HERMIX_INVITE"] = code
    _state_set(_STATE_INVITE, code)
    _persist_standalone("HERMIX_INVITE", code)


def _persist_standalone(name: str, value: str) -> None:
    """GitHub build only: mirror a value into the Hermes .env file for the
    optional systemd sidecar, which runs outside Hermes and cannot reach
    ctx.state. The catalog build ships without _standalone.py, so there this
    is a no-op and credentials never touch a file Hermes doesn't own."""
    try:
        from . import _standalone
    except ImportError:
        return
    _standalone.write_env_value(_standalone.env_file_path(), name, value)


def share_version() -> bool:
    """Whether to tell the hub which plugin version is running (two request
    headers, no content). On by default in the standalone build, where it
    drives staged rollouts; opt-in (HERMIX_SHARE_VERSION=1) in the catalog
    build, whose rules make any usage reporting opt-in."""
    from . import _distribution
    raw = (_env("HERMIX_SHARE_VERSION", "") or "").strip().lower()
    if raw:
        return raw not in ("0", "false", "no", "off")
    return not _distribution.is_catalog()


def llm_mode() -> str:
    """Where the network's "thinking" runs, from HERMIX_LLM:

      - "hub" (default): hub inference ONLY. The user's model budget is never
                 spent; on any hub failure the caller receives the safe silence
                 sentinel and the agent simply goes quiet.
      - "auto":  hub inference when live, falling back to the user's own
                 ctx.llm on hub failure or before registration. Costs the user
                 money, so it must be chosen explicitly.
      - "local": the user's own ctx.llm ONLY — never touch the hub.

    The default is "hub" because README and onboarding both promise, without
    qualification, that network work never spends the user's own budget. Under
    "auto" that promise held only for OUR budget 429 — a hub 503, a dropped
    connection, or any cycle before registration would quietly bill the user
    for work they never asked to pay for. A promise that holds only on the
    happy path is not a promise, so the paying mode is now opt-in.
    """
    raw = _env("HERMIX_LLM", "")
    raw = raw.strip().lower() if isinstance(raw, str) else ""
    if raw in ("auto", "hub", "local"):
        return raw
    return "hub"


# --------------------------------------------------------------------------- #
# Matchmaker knobs — all env-overridable, all with sane silence-by-default
# values. Read through functions (never cached) so a value written to ~/.hermes
# /.env mid-session is honoured on the next cycle, and so tests can flip a knob
# with monkeypatch.setenv. See matchmaker.py.
# --------------------------------------------------------------------------- #

def _knob_name(env_name: str) -> str:
    """HERMIX_INTERRUPT_THRESHOLD -> interrupt_threshold (the hub's key)."""
    return env_name[len("HERMIX_"):].lower() if env_name.startswith("HERMIX_") \
        else env_name.lower()


def _remote(env_name: str, default):
    """The hub's live value for this knob, or ``default``.

    This is what lets the network keep improving without a single user running
    a command: we tune centrally, every agent picks it up within the hour. An
    explicit env var still wins (see _int_env/_float_env)."""
    try:
        from . import remote_config
        return remote_config.knob(_knob_name(env_name), default)
    except Exception:
        return default


def _int_env(name: str, default: int) -> int:
    raw = _env(name, "")
    raw = raw.strip() if isinstance(raw, str) else ""
    if not raw:
        return int(_remote(name, default))       # hub value, else built-in
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = _env(name, "")
    raw = raw.strip() if isinstance(raw, str) else ""
    if not raw:
        return float(_remote(name, default))     # hub value, else built-in
    try:
        return float(raw)
    except ValueError:
        return default


def min_score() -> float:
    """Stage-1 cheap filter: drop any candidate signal below this score."""
    return _float_env("HERMIX_MIN_SCORE", 3.0)


def match_every_hours() -> int:
    """How often the matchmaker cron/daemon looks for opportunities.

    6h for the beta rather than 4h: with a small cohort the network changes
    slowly, so a shorter cycle mostly re-scores the same cards at the operator's
    expense. Nothing is lost by looking less often — findings queue and wait."""
    return _int_env("HERMIX_MATCH_EVERY_HOURS", 6)


# --------------------------------------------------------------------------- #
# When to interrupt the human.
#
# There is deliberately NO daily quota. A good friend doesn't ration contact —
# they judge whether this particular thing is worth your attention right now.
# The model: score every finding, and compare it against a threshold that RISES
# as we interrupt more (a recovering "social battery") and FALLS when the human
# engages with what we bring. Anything under the bar is not discarded — it rides
# along with the next natural conversation (see hermix_pending).
# --------------------------------------------------------------------------- #

def interrupt_threshold() -> float:
    """Base bar (0..10) a finding must clear to interrupt the human proactively.

    Deliberately modest: everything reaching this point has ALREADY survived the
    cheap filter, a real agent-to-agent dig, and an LLM judge that returned
    "notify". The bar's job is pacing, not re-litigating quality — pressure
    raises it after each interruption."""
    return _float_env("HERMIX_INTERRUPT_THRESHOLD", 5.0)


def urgent_threshold() -> float:
    """At/above this, it goes through regardless of pressure or quiet hours —
    the 'a friend calls you at midnight for this' line."""
    return _float_env("HERMIX_URGENT_THRESHOLD", 8.5)


def pressure_half_life_hours() -> float:
    """How fast the social battery recovers after an interruption."""
    return _float_env("HERMIX_PRESSURE_HALF_LIFE_H", 8.0)


def pressure_weight() -> float:
    """How much each recent interruption raises the bar."""
    return _float_env("HERMIX_PRESSURE_WEIGHT", 1.2)


def engagement_weight() -> float:
    """How much demonstrated interest (asking for more, requesting intros)
    lowers the bar — the human is telling us they want this."""
    return _float_env("HERMIX_ENGAGEMENT_WEIGHT", 0.8)


def quiet_hours() -> tuple:
    """Hours to stay silent unless something is urgent, as (start, end).

    OFF by default, deliberately: this reads the *machine's* clock, and the
    agent usually runs on a UTC VPS while its human lives somewhere else — so a
    default window would just as likely mute the workday as protect the night.
    Set HERMIX_QUIET_HOURS="22-8" when the host clock really is the human's."""
    raw = _env("HERMIX_QUIET_HOURS", "")
    raw = raw.strip() if isinstance(raw, str) else ""
    if not raw:
        return ()
    try:
        a, b = raw.split("-")
        return (int(a) % 24, int(b) % 24)
    except (ValueError, AttributeError):
        return (22, 8)


def max_findings_per_batch() -> int:
    """How many UNSOLICITED findings may ride along in one interruption.

    skills/hermix-delivery says "multiple findings become one message, best
    first, max 3" — that is a promise about what the human reads, so it is
    enforced here rather than left to the model to remember. Findings beyond
    the limit are not dropped; they stay queued for the next interruption, by
    which time the newer ones may well outrank them.

    Answers the human asked for are exempt: a batch is a digest, and holding
    back a reply to keep a digest tidy would be absurd. 0 = no limit."""
    return _int_env("HERMIX_MAX_FINDINGS_PER_BATCH", 3)


def max_notify_per_day() -> int:
    """Hard ceiling on UNSOLICITED interruptions per rolling 24h. 0 = no cap.

    Defaults to 1 for the beta. The adaptive bar above is still the mechanism
    that decides what is worth saying; this only bounds how wrong that bar can
    be while its constants are untested against real humans. A first cohort
    that gets talked at too much leaves and does not come back, and no amount
    of later tuning recovers them — whereas a cohort that hears from its agent
    once a day can be relaxed the moment the feedback says to.

    Answers the human asked for are exempt (see matchmaker._emit), so this
    rations noise, never responsiveness. Set 0 to restore judgement-only."""
    return _int_env("HERMIX_MAX_NOTIFY_PER_DAY", 1)


def handshake_timeout_days() -> int:
    """If no reply arrives within this window, judge on cards alone."""
    return _int_env("HERMIX_HANDSHAKE_TIMEOUT_DAYS", 4)


def watch_days() -> int:
    """A 'watch' verdict is re-judged after this many days."""
    return _int_env("HERMIX_WATCH_DAYS", 7)


def drop_cooldown_days() -> int:
    """After a 'drop' verdict a candidate is ignored for this long."""
    return _int_env("HERMIX_DROP_COOLDOWN_DAYS", 14)


def startup_gate_seconds() -> int:
    """Minimum seconds between the hub calls register() makes on startup.

    Hermes spawns a process per subagent and each one registers, so without a
    shared gate this traffic scales with process count. 0 disables the gate."""
    return _int_env("HERMIX_STARTUP_GATE_SECONDS", 300)


def max_new_digs_per_cycle() -> int:
    """How many NEW conversations one agent may start in a single cycle.

    Discovery returns up to 20 candidates, and every one of them used to become
    a dig immediately. On launch day, with everyone new to everyone, that is a
    thundering herd: a burst of thread-opens per agent, the hub's per-agent
    60/min limit tripped, and a day's inference budget spent in one wave.
    Starting a few and continuing next cycle costs nothing — the agent has all
    day, and a human is never waiting on it.

    2 for the beta: in a cohort of 10-25 everyone is plausibly relevant to
    everyone, so a higher number burns inference re-litigating a small network
    rather than finding more in it."""
    return _int_env("HERMIX_MAX_NEW_DIGS_PER_CYCLE", 2)


def thread_budget() -> int:
    """The hub's hard message budget for one thread. We conclude two messages
    short of it so a dig always ends with a findings note we wrote, rather than
    being killed mid-sentence by the hub (14 of 24 production threads died that
    way, spending a full budget of inference to produce nothing)."""
    return _int_env("HERMIX_THREAD_BUDGET", 12)


def redig_after_days() -> int:
    """How long before we may talk to a counterpart we already dug into again.

    Without this a small network dies of its own success: once every pair has
    concluded one conversation there is nothing left to discover and the agents
    go permanently silent. People's projects and needs change; a re-look after
    a while is what a real contact would do. 0 disables re-digging."""
    return _int_env("HERMIX_REDIG_AFTER_DAYS", 14)


def redig_max() -> int:
    """Hard cap on how many times we will ever re-open with the same agent, so
    a re-look can never decay into pestering."""
    return _int_env("HERMIX_REDIG_MAX", 3)


def card_refresh_days() -> int:
    """How often to propose (never auto-apply) an improved public card."""
    return _int_env("HERMIX_CARD_REFRESH_DAYS", 7)


def dig_max_turns() -> int:
    """Max OUTBOUND turns the matchmaker (initiator) spends on a dig thread
    before it concludes with a findings note. The opener counts as turn 1."""
    return _int_env("HERMIX_DIG_MAX_TURNS", 3)


def checkin_after_hours() -> int:
    """Hours after joining before the one-time "here's what I've been doing"
    check-in. 0 disables it.

    Four hours, not twenty-four. Silence is the product's core discipline, but
    it is indistinguishable from breakage to someone who installed this an hour
    ago — and the person most likely to conclude it is broken and uninstall is
    the brand-new user, on day one, before anything has had time to work.

    Four hours is late enough that at least one cycle has run and the note has
    real numbers in it, and early enough that nobody spends an evening
    wondering. It happens exactly once; after that, silence means silence."""
    return _int_env("HERMIX_CHECKIN_AFTER_HOURS", 4)


def ask_max_turns() -> int:
    """Max messages WE send in a user-requested investigation before reporting
    back. Enough to clarify, not enough to become a pen-pal."""
    return _int_env("HERMIX_ASK_MAX_TURNS", 3)


def envoy_max_replies() -> int:
    """Max replies the answering-side envoy daemon posts into a single thread
    before it politely concludes and closes it."""
    return _int_env("HERMIX_ENVOY_MAX_REPLIES", 6)
