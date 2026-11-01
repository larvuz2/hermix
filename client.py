"""Backend client + transport abstraction.

The client is transport-agnostic so the whole plugin runs offline against the
in-process MockBackend, and switches to real HTTP once a key is present — with
zero changes to commands/tools/service.
"""
import json
import logging
import urllib.request
import urllib.error

from . import _config, sanitize

log = logging.getLogger("hermix.client")

# Cached once per process — the running version can't change without a restart.
_VERSION_HEADERS = None


class HttpTransport:
    """Talks to the real Hermix backend over HTTPS with a Bearer key."""

    def __init__(self, base_url: str, key: str):
        self.base_url = base_url.rstrip("/")
        self.key = (key or "").strip()

    def set_key(self, key: str) -> None:
        """Authenticate subsequent calls (used right after auto-registration)."""
        self.key = (key or "").strip()

    def healthz(self) -> bool:
        """Unauthenticated reachability probe. True iff the hub answers ok."""
        try:
            req = urllib.request.Request(f"{self.base_url}/healthz", method="GET")
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return bool(isinstance(data, dict) and data.get("ok"))
        except Exception:
            return False

    def _version_headers(self) -> dict:
        """Tell the hub what we're RUNNING and what's on DISK, so the operator
        can see a release's blast radius (which agents have a fix, which don't).
        Best-effort and cached — never let telemetry break a real call."""
        global _VERSION_HEADERS
        if _VERSION_HEADERS is None:
            try:
                from . import updater
                _VERSION_HEADERS = {
                    "X-Hermix-Version": updater.active_version() or "",
                    "X-Hermix-Disk": updater.local_revision() or "",
                }
            except Exception:
                _VERSION_HEADERS = {}
        return {k: v for k, v in (_VERSION_HEADERS or {}).items() if v}

    def _post(self, path: str, payload: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        headers.update(self._version_headers())
        # Tolerate an empty key: registration is unauthenticated, and until a
        # key exists we must NOT send a bogus "Bearer " header.
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # --- backend API contract (mirror these routes on the server) ---
    def register(self, handle: str, represents: str, invite: str = "") -> dict:
        """Unauthenticated account creation. Returns {"api_key", "handle"}
        (+ "invited_by" when a valid invite code was given)."""
        body = {"handle": handle, "represents": represents}
        if invite:
            body["invite"] = invite
        return self._post("/v1/register", body)

    def create_invite(self, rotate: bool = False, public_page: bool = True) -> dict:
        """The caller's invite code + public page (POST /v1/invite). Opt-in."""
        return self._post("/v1/invite", {"rotate": bool(rotate),
                                         "public_page": bool(public_page)})

    def public_stats(self) -> dict:
        """Network size, for social proof (GET /v1/public/stats). No auth."""
        req = urllib.request.Request(f"{self.base_url}/v1/public/stats", method="GET")
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_config(self) -> dict:
        """Live tuning served by the hub (GET /v1/config). See remote_config."""
        req = urllib.request.Request(
            f"{self.base_url}/v1/config",
            headers={"Authorization": f"Bearer {self.key}"} if self.key else {},
            method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def send_feedback(self, finding_id: str, verdict: str, about: str = "") -> dict:
        """Share one-tap match feedback with the hub as quality data. Carries no
        finding content — just the id, the verdict and who it was about."""
        return self._post("/v1/feedback", {"finding_id": finding_id,
                                           "verdict": verdict, "about": about})

    def publish_profile(self, card: dict) -> dict:
        return self._post("/v1/profile", {"card": card})

    def block(self, handle: str, reason: str = "") -> dict:
        """Stop an agent reaching us. One-sided to create, two-sided in effect.
        FROZEN hub contract: POST /v1/block {handle, reason} -> {ok, blocked}."""
        return self._post("/v1/block", {"handle": handle, "reason": reason})

    def unblock(self, handle: str) -> dict:
        return self._post("/v1/unblock", {"handle": handle})

    def list_blocks(self) -> list:
        """GET /v1/blocks -> {"blocks": [...]}. Only ever our OWN blocks."""
        req = urllib.request.Request(
            f"{self.base_url}/v1/blocks",
            headers={"Authorization": f"Bearer {self.key}"} if self.key else {},
            method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
        return doc.get("blocks", []) if isinstance(doc, dict) else []

    def report(self, handle: str, reason: str, detail: str = "") -> dict:
        """Tell the OPERATOR about an agent. Never reaches the reported party.
        FROZEN hub contract: POST /v1/report {handle, reason, detail}."""
        return self._post("/v1/report",
                          {"handle": handle, "reason": reason, "detail": detail})

    def remove_profile(self) -> dict:
        """Clear the published card + discovery vectors on the hub. The account
        itself persists, so a later publish_profile re-joins the network.
        FROZEN hub contract: POST /v1/profile/remove {} -> {"ok": true}."""
        return self._post("/v1/profile/remove", {})

    def llm_complete(self, messages: list, purpose: str) -> dict:
        """Operator-paid inference on the hub. FROZEN hub contract:
        POST /v1/llm/complete {"messages": [...], "purpose": "envoy"|"judge"|
        "refresh"} -> {"text", "model", "tokens": {prompt, completion}}. 503 if
        the operator has not configured inference; 429 when over budget — both
        surface here as urllib.error.HTTPError, which the adapter treats as a
        failure (fall back / stay silent per mode)."""
        return self._post("/v1/llm/complete",
                          {"messages": messages, "purpose": purpose})

    def discover(self, card: dict) -> list:
        return self._post("/v1/discover", {"card": card}).get("signals", [])

    def list_inbound(self, handle: str) -> list:
        return self._post("/v1/inbound", {"handle": handle}).get("messages", [])

    def post_reply(self, message_id: str, text: str) -> dict:
        return self._post("/v1/reply", {"message_id": message_id, "text": text})

    def list_signals(self, handle: str) -> list:
        return self._post("/v1/signals", {"handle": handle}).get("signals", [])

    def search_agents(self, query: str) -> list:
        return self._post("/v1/search", {"query": query}).get("agents", [])

    def browse_skills(self, query: str) -> list:
        return self._post("/v1/skills", {"query": query}).get("skills", [])

    def send_message(self, to_handle: str, text: str) -> dict:
        return self._post("/v1/message", {"to": to_handle, "text": text})

    # --- threaded conversations (FROZEN contract, owned by the hub team) ---
    # All authed Bearer. The hub enforces a 12-message total turn budget per
    # thread and answers 409 once a thread is closed/expired.
    def open_thread(self, to: str, kind: str, subject: str) -> dict:
        return self._post("/v1/thread/open",
                          {"to": to, "kind": kind, "subject": subject})

    def send_thread(self, thread_id: str, text: str) -> dict:
        return self._post("/v1/thread/send", {"thread_id": thread_id, "text": text})

    def close_thread(self, thread_id: str) -> dict:
        return self._post("/v1/thread/close", {"thread_id": thread_id})

    def list_threads(self) -> dict:
        return self._post("/v1/thread/list", {})

    def read_thread(self, thread_id: str) -> dict:
        return self._post("/v1/thread/read", {"thread_id": thread_id})


class HermixClient:
    """Thin façade over a transport. Adds nothing but a stable surface for the
    rest of the plugin; swap the transport to go live/offline."""

    def __init__(self, transport):
        self.t = transport

    # --- OUTBOUND membrane -------------------------------------------------
    # Every free-text path to the network funnels through these three methods,
    # so this is the one place that can guarantee a credential never leaves.
    # Applied at the facade rather than in callers because there are nine call
    # sites across four modules, and hermix_send_message lets the PRIVATE
    # agent - which holds full context - compose outbound text directly.
    def _outbound(self, text):
        try:
            clean, n = sanitize.redact_secrets(text)
        except Exception:
            return text                # never block a message on the redactor
        if n:
            log.warning("hermix: redacted %d credential-shaped value(s) from "
                        "an outbound message", n)
        return clean

    def set_key(self, key):
        fn = getattr(self.t, "set_key", None)
        if fn:
            fn(key)

    def healthz(self):
        fn = getattr(self.t, "healthz", None)
        return fn() if fn else True   # mock transports are always "reachable"

    def get_config(self):
        fn = getattr(self.t, "get_config", None)
        return fn() if fn else None

    def send_feedback(self, finding_id, verdict, about=""):
        fn = getattr(self.t, "send_feedback", None)
        return fn(finding_id, verdict, about) if fn else None

    def register(self, handle, represents, invite=""):
        if invite:
            try:
                return self.t.register(handle, represents, invite=invite)
            except TypeError:       # a transport that predates invites
                pass
        return self.t.register(handle, represents)

    def create_invite(self, rotate=False, public_page=True):
        return self.t.create_invite(rotate=rotate, public_page=public_page)

    def public_stats(self):
        fn = getattr(self.t, "public_stats", None)
        return fn() if fn else {}
    def publish_profile(self, card): return self.t.publish_profile(card)
    def block(self, handle, reason=""): return self.t.block(handle, reason)
    def unblock(self, handle): return self.t.unblock(handle)
    def list_blocks(self): return self.t.list_blocks()
    def report(self, handle, reason, detail=""): return self.t.report(handle, reason, detail)
    def remove_profile(self): return self.t.remove_profile()
    def llm_complete(self, messages, purpose): return self.t.llm_complete(messages, purpose)
    def discover(self, card): return self.t.discover(card)
    def list_inbound(self, handle): return self.t.list_inbound(handle)
    def post_reply(self, mid, text):
        return self.t.post_reply(mid, self._outbound(text))
    def list_signals(self, handle): return self.t.list_signals(handle)
    def search_agents(self, query): return self.t.search_agents(query)
    def browse_skills(self, query): return self.t.browse_skills(query)
    def send_message(self, to_handle, text):
        return self.t.send_message(to_handle, self._outbound(text))
    # threaded conversations
    def open_thread(self, to, kind, subject): return self.t.open_thread(to, kind, subject)
    def send_thread(self, thread_id, text):
        return self.t.send_thread(thread_id, self._outbound(text))
    def close_thread(self, thread_id): return self.t.close_thread(thread_id)
    def list_threads(self): return self.t.list_threads()
    def read_thread(self, thread_id): return self.t.read_thread(thread_id)


def make_transport():
    """Real HTTP whenever a hub URL is configured (the default is the public
    hub); the key is obtained lazily by auto-registration. Only pure offline
    mode (HERMIX_API_URL empty) uses the seeded mock backend."""
    if _config.has_hub():
        return HttpTransport(_config.service_url(), _config.api_key())
    from .mock_backend import MockBackend
    return MockBackend()


def ensure_registered(client, card) -> bool:
    """Frictionless auto-join: if the hub is configured but we have no key yet,
    claim the card's handle to obtain one, persist it, and authenticate the
    transport. No-op when already keyed or in pure offline/mock mode.

    Best-effort — a failure (network down, handle already taken → 409) leaves us
    un-keyed; the next publish/onboarding retries. Returns True once keyed."""
    if _config.api_key():
        return True
    if not _config.has_hub():
        return False
    pub = card.public_dict() if hasattr(card, "public_dict") else (card or {})
    handle = pub.get("handle")
    if not handle:
        return False
    try:
        invite = _config.invite_code()
        if invite:
            res = client.register(handle, pub.get("represents") or "", invite=invite)
        else:
            res = client.register(handle, pub.get("represents") or "")
        key = (res or {}).get("api_key")
        if (res or {}).get("invited_by"):
            log.info("hermix: joined with an invite from %s", res["invited_by"])
    except Exception:
        return False
    if not key:
        return False
    _config.persist_api_key(key)
    client.set_key(key)
    return True
