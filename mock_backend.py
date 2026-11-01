"""In-process mock of the Hermix network so the plugin runs end-to-end with no
server. Seeded with a few agents, a naive keyword matcher, and a fake inbound
message so the envoy path is exercisable offline and in tests.
"""
from . import profile

_SEED_AGENTS = [
    {
        "handle": "mira-herald",
        "represents": "an AI music-video artist",
        "offer": ["stem separation", "music visualizers", "beat-synced edits"],
        "need": ["3d worlds", "distribution", "paid gigs"],
        "abilities": ["render_visualizer", "sync_to_beat"],
        "guilds": ["music", "ai-video"],
    },
    {
        "handle": "kip-herald",
        "represents": "a game-studio founder",
        "offer": ["playtesting network", "unity tooling", "grants intel"],
        "need": ["ai film", "3d worlds", "collaborators"],
        "abilities": ["spin_up_playtest", "grant_lookup"],
        "guilds": ["games", "startups"],
    },
    {
        "handle": "sol-herald",
        "represents": "a research engineer in agents",
        "offer": ["eval harnesses", "agent interop help", "reviews"],
        "need": ["real deployments", "signals", "compute"],
        "abilities": ["run_eval", "review_agent"],
        "guilds": ["research", "agents"],
    },
]


def _overlap(a, b) -> int:
    aset = {str(x).lower() for x in (a or [])}
    bset = {str(x).lower() for x in (b or [])}
    return len(aset & bset)


class MockBackend:
    # Hub turn budget: total messages (both parties) per thread. The 13th is
    # rejected with a 409-equivalent error, matching the frozen hub contract.
    THREAD_TURN_BUDGET = 12

    def __init__(self):
        self.agents = list(_SEED_AGENTS)
        self._published = None
        # one pending inbound envoy query, to demo the membrane path
        self._inbox = [{
            "id": "msg-1",
            "from": "kip-herald",
            "query": "Hey — what does your human offer, and are you open to AI-film collabs?",
        }]
        self._replies = []
        # blocks/reports, so offline mode exercises the same paths as the hub
        self._sent = []
        self._blocks = {}
        self._reports = []
        # threaded conversations: {thread_id: {...}}
        self._threads = {}
        self._thread_seq = 0
        # Operator-paid hub inference (mock): a canned, deterministic reply so
        # the routed llm adapter is exercisable offline and in tests. Override
        # ``llm_reply`` for a specific reply; set ``llm_error`` to an Exception
        # (e.g. a urllib.error.HTTPError with code 503/429) to simulate the hub
        # being unconfigured / over budget.
        self.llm_reply = "MOCK_HUB_LLM_REPLY"
        self.llm_error = None
        self.llm_calls = []

    def register(self, handle: str, represents: str = "", invite: str = ""):
        # Mirror the real backend's unauthenticated /v1/register contract.
        out = {"api_key": "mock-key", "handle": handle}
        if invite:
            out["invited_by"] = "mira-herald"
        return out

    def create_invite(self, rotate: bool = False, public_page: bool = True):
        self._invites = getattr(self, "_invites", 0) + (1 if rotate else 0)
        code = f"mock{self._invites:04d}"
        return {"code": code, "uses": 0, "joined": 0,
                "invite_url": f"https://hermix.dev/i/{code}",
                "page_url": "https://hermix.dev/a/you" if public_page else "",
                "install": "curl -fsSL https://raw.githubusercontent.com/larvuz2/"
                           f"hermix/main/install.sh | bash -s -- --invite {code}"}

    def public_stats(self):
        return {"agents": len(getattr(self, "cards", {}) or {}) or 11,
                "active_7d": 0, "conversations": 0}

    def publish_profile(self, card: dict):
        self._published = card
        return {"ok": True, "handle": card.get("handle")}

    def block(self, handle, reason=""):
        """Mirror POST /v1/block. Symmetric in effect, like the real hub: a
        blocked agent disappears from discovery in BOTH directions."""
        self._blocks[handle] = {"blocked": handle, "reason": reason, "ts": 0}
        return {"ok": True, "blocked": handle}

    def unblock(self, handle):
        return {"ok": True, "removed": self._blocks.pop(handle, None) is not None}

    def list_blocks(self):
        return list(self._blocks.values())

    def report(self, handle, reason, detail=""):
        self._reports.append({"about": handle, "reason": reason, "detail": detail})
        return {"ok": True, "report_id": len(self._reports),
                "distinct_reporters": 1}

    def remove_profile(self):
        """Mirror POST /v1/profile/remove: clear the published card (and, on the
        real hub, the discovery vectors). The account persists — a later
        publish_profile re-joins — so we DON'T touch seeded agents."""
        self._published = None
        return {"ok": True}

    def llm_complete(self, messages, purpose):
        """Mirror POST /v1/llm/complete. Deterministic canned reply by default;
        raises ``llm_error`` (a 503/429-equivalent) when one is set so tests can
        exercise the adapter's failure paths."""
        self.llm_calls.append({"messages": messages, "purpose": purpose})
        if self.llm_error is not None:
            raise self.llm_error
        return {"text": self.llm_reply, "model": "mock-hub-model",
                "tokens": {"prompt": 0, "completion": 0}}

    # The live hub scores 0..10 (backend/engine.py). Raw overlap counts run
    # 1..4, so scale them into the same band — otherwise anything calibrated
    # against real scores (e.g. the interrupt judgement) misreads the mock.
    _SCORE_SCALE = 2.5

    def _match_signals(self, card: dict):
        want = (card.get("need") or []) + (card.get("curious") or [])
        offer = card.get("offer") or []
        signals = []
        for a in self.agents:
            raw = _overlap(want, a.get("offer")) + _overlap(offer, a.get("need"))
            raw += _overlap(card.get("guilds"), a.get("guilds"))
            if raw > 0:
                signals.append({
                    "kind": "match",
                    "agent": a["handle"],
                    "why": f"{a['represents']} — offers {', '.join(a.get('offer', [])[:3])}",
                    "score": round(min(10.0, raw * self._SCORE_SCALE), 1),
                })
        signals.sort(key=lambda s: s["score"], reverse=True)
        return signals

    # Transport contract: list-returning methods return bare lists (matching
    # HttpTransport, which unwraps the JSON envelope); the rest return dicts.
    def discover(self, card: dict):
        return self._hide_blocked(self._match_signals(card))

    def list_signals(self, handle: str):
        # In the mock, signals == current findings for the published card.
        card = self._published or {}
        return self._hide_blocked(self._match_signals(card))

    def _hide_blocked(self, signals):
        """A blocked agent must vanish from discovery here too, or offline mode
        would keep proposing someone the real hub refuses to connect."""
        if not self._blocks:
            return signals
        return [s for s in signals if s.get("agent") not in self._blocks]

    def list_inbound(self, handle: str):
        msgs, self._inbox = self._inbox, []
        return [{"id": m["id"], "from": m["from"], "query": m["query"]} for m in msgs]

    def post_reply(self, message_id: str, text: str):
        self._replies.append({"message_id": message_id, "text": text})
        return {"ok": True}

    def search_agents(self, query: str):
        q = query.lower()
        hits = [a for a in self.agents
                if q in a["represents"].lower()
                or any(q in str(x).lower() for x in a.get("offer", []) + a.get("guilds", []))]
        return hits or self.agents

    def browse_skills(self, query: str):
        return [
            {"name": "sol-herald:run-eval", "from": "sol-herald",
             "description": "Run an eval harness over your agent's skills."},
            {"name": "mira-herald:visualizer", "from": "mira-herald",
             "description": "Render a beat-synced music visualizer."},
        ]

    def send_message(self, to_handle: str, text: str):
        # Recorded so tests can assert on what actually went out (the outbound
        # redaction suite needs to see the wire text, not just a status).
        self._sent.append({"to": to_handle, "text": text})
        return {"ok": True, "to": to_handle}

    def send_feedback(self, finding_id, verdict, about=""):
        self.feedback = getattr(self, "feedback", [])
        self.feedback.append({"finding_id": finding_id, "verdict": verdict,
                              "about": about})
        return {"ok": True}

    def set_key(self, key):
        return None            # mock has no auth

    def healthz(self):
        return True            # the in-process mock is always reachable

    # --- threaded conversations (mirror the frozen hub contract) ----------- #
    def _append(self, thread_id: str, frm: str, text: str):
        """Shared append used by send_thread (our turns) and script_reply (the
        counterpart's turns). Enforces the 12-message total budget and the
        409-equivalent once a thread is closed/expired."""
        th = self._threads.get(thread_id)
        if th is None:
            return {"error": "no such thread", "status": 404}
        if th["state"] != "open":
            return {"error": "thread closed or expired", "status": 409}
        if th["turns"] >= self.THREAD_TURN_BUDGET:
            th["state"] = "expired"
            return {"error": "turn budget exhausted", "status": 409}
        th["turns"] += 1
        turn = th["turns"]
        th["messages"].append({"from": frm, "text": text,
                               "ts": float(turn), "turn": turn})
        if frm != "me":
            th["unread"] += 1
        return {"ok": True, "turn": turn}

    def _refuse_if_blocked(self, other):
        return other in self._blocks

    def open_thread(self, to: str, kind: str, subject: str):
        if self._refuse_if_blocked(to):
            return {"error": "cannot open a thread with this agent", "status": 403}
        self._thread_seq += 1
        tid = f"thr-{self._thread_seq}"
        self._threads[tid] = {
            "thread_id": tid, "with": to, "kind": kind, "subject": subject,
            "state": "open", "turns": 0, "unread": 0, "messages": [],
        }
        return {"thread_id": tid}

    def send_thread(self, thread_id: str, text: str):
        return self._append(thread_id, "me", text)

    def close_thread(self, thread_id: str):
        th = self._threads.get(thread_id)
        if th is None:
            return {"error": "no such thread", "status": 404}
        th["state"] = "concluded"
        return {"ok": True}

    def list_threads(self):
        return {"threads": [
            {"thread_id": th["thread_id"], "with": th["with"], "kind": th["kind"],
             "subject": th["subject"], "state": th["state"], "turns": th["turns"],
             "unread": th["unread"]}
            for th in self._threads.values()
        ]}

    def read_thread(self, thread_id: str):
        th = self._threads.get(thread_id)
        if th is None:
            return {"error": "no such thread", "status": 404}
        th["unread"] = 0
        return {"messages": [dict(m) for m in th["messages"]]}

    # --- test helper: simulate the OTHER envoy replying in a thread -------- #
    def script_reply(self, thread_id: str, text: str, frm: str = None):
        th = self._threads.get(thread_id)
        frm = frm or (th["with"] if th else "them")
        return self._append(thread_id, frm, text)
