"""LLM-callable tools so the PRIVATE agent can work the network agentically.

These belong to the private agent (it can pull from the network). They never
expose private context outward — outward-facing answers only ever go through
the envoy. Handlers return JSON strings, matching Hermes' tool convention.
"""
import json
import time

from . import matchmaker, dossier, sanitize

_REPORT_REASONS = ("spam", "harassment", "impersonation", "scam", "other")


def build(client, card, llm=None):
    """Return a list of tool specs to register with ctx.register_tool."""

    def share_tool(params, **kwargs):
        """Invite link + public page. Opt-in; the human asked for it."""
        from . import share
        action = str((params or {}).get("action") or "get").lower()
        if action == "redeem":
            res = share.redeem((params or {}).get("code"))
            return json.dumps({"success": bool(res.pop("ok", False)), **res})
        res = share.share(client, card, rotate=action == "rotate", off=action == "off")
        if res.get("ok"):
            res["share_text"] = share.share_text(res)
        return json.dumps({"success": bool(res.pop("ok", False)), **res})

    def scan_now(params, **kwargs):
        """Run ONE matchmaking cycle immediately — execution only.

        Used right after onboarding so a brand-new user's first intent starts
        working straight away instead of waiting hours for the first cron.
        Returns counts, never a notification: findings still have to earn their
        way to the human through the normal judgement."""
        try:
            intents = [i for i in dossier.list_intents()
                       if i.get("status") == "active"]
        except Exception:
            intents = []
        try:
            ring1 = dossier.get_ring1()
        except Exception:
            ring1 = []
        try:
            added = matchmaker.run_engine_and_persist(
                client, card, llm, time.time, intents=intents, ring1=ring1)
            st = matchmaker.load_state()
            return json.dumps({
                "success": True,
                "digs_open": len([d for d in (st.get("digs") or {}).values()
                                  if not d.get("concluded")]),
                "findings_ready": len(((st.get("outbox") or {}).get("ready")) or []),
                "new_findings": added,
                "note": ("Scan started. Conversations take time — say nothing "
                         "about findings now; I'll surface anything worthwhile."),
            })
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)[:200]})

    def block_agent(params, **kwargs):
        """Stop an agent reaching this human. Enforced by the hub."""
        who = str(params.get("handle") or "").strip().lstrip("@")
        if not who:
            return json.dumps({"success": False, "error": "need 'handle'"})
        try:
            client.block(who, str(params.get("reason") or "")[:200])
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)[:200]})
        return json.dumps({
            "success": True, "blocked": who,
            "note": ("Blocked. They cannot open a conversation with your human, "
                     "they will not be surfaced again, and they are not told."),
        })

    def unblock_agent(params, **kwargs):
        who = str(params.get("handle") or "").strip().lstrip("@")
        if not who:
            return json.dumps({"success": False, "error": "need 'handle'"})
        try:
            res = client.unblock(who)
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)[:200]})
        return json.dumps({"success": True, "handle": who,
                           "was_blocked": bool(res.get("removed"))})

    def report_agent(params, **kwargs):
        """Tell the network operator about an agent. Never reaches them."""
        who = str(params.get("handle") or "").strip().lstrip("@")
        reason = str(params.get("reason") or "").strip().lower()
        if not who or reason not in _REPORT_REASONS:
            return json.dumps({"success": False,
                               "error": "need 'handle' and a valid 'reason'",
                               "accepted": list(_REPORT_REASONS)})
        try:
            res = client.report(who, reason, str(params.get("detail") or "")[:1000])
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)[:200]})
        return json.dumps({
            "success": True, "reported": who,
            "distinct_reporters": res.get("distinct_reporters", 1),
            "note": ("Sent to the operator. They are not told, and this does "
                     "NOT block them — call hermix_block if your human also "
                     "wants them to stop reaching out."),
        })

    def feedback(params, **kwargs):
        """Record one-tap feedback on a delivered finding and act on it."""
        fid = str(params.get("finding_id") or "").strip()
        verdict = str(params.get("verdict") or "").strip()
        if not fid or not verdict:
            return json.dumps({"success": False,
                               "error": "need 'finding_id' and 'verdict'",
                               "accepted": list(matchmaker.FEEDBACK_VERDICTS)})
        st = matchmaker.load_state()
        res = matchmaker.record_feedback(st, fid, verdict)
        matchmaker.save_state(st)
        if not res.get("ok"):
            return json.dumps({"success": False, **res})
        # Share it with the hub as anonymous quality data (best-effort).
        try:
            client.send_feedback(fid, res["verdict"], res.get("handle", ""))
        except Exception:
            pass
        note = "Thanks — that changes what I bring you next."
        # "Spam" already meant "never surface them again", but that was OUR
        # decision alone — they could still open threads at us and spend our
        # inference. Enforce it at the hub, where it does not depend on their
        # client behaving.
        if res["verdict"] == "spam" and res.get("handle"):
            try:
                client.block(res["handle"], "marked as spam by the human")
                note = ("Thanks — I've blocked them. They can't reach you and "
                        "won't come up again.")
            except Exception:
                pass          # local suppression still applies
        return json.dumps({"success": True, "verdict": res["verdict"],
                           "about": res.get("handle", ""), "note": note})

    def why(params, **kwargs):
        """The trust receipt for one finding — why it matched, what was
        verified, what the conversation could use, what never left, why now."""
        fid = str(params.get("finding_id") or "").strip()
        if not fid:
            return json.dumps({"success": False, "error": "need 'finding_id'"})
        return json.dumps({"success": True,
                           "receipt": matchmaker.receipt(matchmaker.load_state(), fid)})

    def intro_preview_tool(params, **kwargs):
        """Show EXACTLY what an introduction would share. Sends nothing."""
        to = str(params.get("to") or "").strip()
        if not to:
            return json.dumps({"success": False, "error": "need 'to'"})
        try:
            contact = dossier.get_contact()
        except Exception:
            contact = {}
        preview = matchmaker.intro_preview(matchmaker.load_state(), to, contact)
        return json.dumps({
            "success": True,
            "preview_text": matchmaker.format_intro_preview(preview),
            "have_contact": preview["have_contact"],
            "blocked": preview["blocked"],
            "next": ("Show preview_text to your human verbatim. Only if they "
                     "clearly approve, call hermix_reveal_request with "
                     "to=<handle>, include_contact=true, human_approved=true."),
        })

    def deliver_pending_tool(params, **kwargs):
        """DELIVERY ONLY — what the cron job calls.

        It never discovers, digs or judges; the daemon already did that. It just
        asks whether anything the engine completed is worth interrupting the
        human with right now, and hands it over. Claimed findings move to an
        inflight state (not deleted), so a delivery that never lands comes back
        rather than being silently consumed."""
        try:
            text = matchmaker.deliver_and_persist()
        except Exception as e:
            return json.dumps({"result": matchmaker.SILENT, "error": str(e)[:200]})
        return json.dumps({"result": text})

    def matchmake(params, **kwargs):
        # The autonomous brain. The cron prompt calls this a few times a day and
        # relays the result ONLY if it is not the silent marker. Real clock here
        # (this is an IO boundary, not a logic path — run_cycle stays clock-pure).
        # Standing intents + Ring-1 facts are read here (the IO boundary) and
        # passed in, so intent-driven discovery and Ring-1 dig color work while
        # run_cycle stays a pure function of its arguments.
        try:
            intents = [i for i in dossier.list_intents()
                       if i.get("status") == "active"]
        except Exception:
            intents = []
        try:
            ring1 = dossier.get_ring1()
        except Exception:
            ring1 = []
        result = matchmaker.run_and_persist(client, card, llm, time.time,
                                            intents=intents, ring1=ring1)
        return json.dumps({"result": result})

    def pending(params, **kwargs):
        # Deliver-on-next-interaction queue (per the delivery skill): the agent
        # peeks queued findings at a natural moment and pops them once relayed.
        action = (params.get("action") or "peek").lower()
        state = matchmaker.load_state()
        queue = list(state.get("queue") or [])
        reveals = list(state.get("pending_reveals") or [])
        if action == "pop":
            send = queue[:3]                       # batch, best-first, max 3
            state["queue"] = queue[3:]
            matchmaker.save_state(state)
            return json.dumps({
                "delivered": send,
                "text": matchmaker._format_notification(send) if send else "",
                "remaining": len(state["queue"]),
                "pending_reveals": reveals,
            })
        return json.dumps({
            "queued": queue,
            "text": matchmaker._format_notification(queue[:3]) if queue else "",
            "pending_reveals": reveals,
        })

    def pause(params, **kwargs):
        # The agent's lever for "my human declined onboarding / wants out": flip
        # the matchmaker paused flag (run_cycle no-ops while set) so the
        # onboarding nudge and all matchmaking go quiet. The human re-joins with
        # /hermix resume (or by publishing a card). Mirrors commands._pause; a
        # tool exists because in gateway mode the agent can't type a slash
        # command, and the onboarding nudge tells it to call hermix_pause.
        state = matchmaker.load_state()
        already = bool(state.get("paused"))
        state["paused"] = True
        matchmaker.save_state(state)
        return json.dumps({
            "success": True,
            "paused": True,
            "already_paused": already,
            "note": ("Scouting and onboarding reminders are off. Resume "
                     "anytime with /hermix resume."),
        })

    def search_agents(params, **kwargs):
        agents = client.search_agents(params.get("query", ""))
        return json.dumps({"success": True, "agents": agents})

    def list_signals(params, **kwargs):
        handle = card.public_dict().get("handle", "")
        return json.dumps({"success": True, "signals": client.list_signals(handle)})

    def send_message(params, **kwargs):
        to = params.get("to", "")
        text = params.get("text", "")
        if not to or not text:
            return json.dumps({"success": False, "error": "need 'to' and 'text'"})
        return json.dumps({"success": True, "result": client.send_message(to, text)})

    def install_skill(params, **kwargs):
        # Gated by commands.install_gate (pre_tool_call). If we get here it was
        # approved. Real impl: download the bundle into the plugin skills dir.
        name = params.get("name", "")
        return json.dumps({"success": True, "installed": name,
                           "note": "stub — wire bundle download in Phase 1"})

    # --- dossier / rings / conversations / reveals ---

    def dossier_view(params, **kwargs):
        # READ-ONLY views. "summary" NEVER contains contact values (only a
        # boolean) — see dossier.summary and the membrane tests.
        view = (params.get("view") or "summary").lower()
        if view == "ring1":
            return json.dumps({"ring1": dossier.get_ring1()})
        if view == "intents":
            return json.dumps({"intents": dossier.list_intents()})
        return json.dumps({"summary": dossier.summary()})

    def _ring1():
        try:
            return dossier.get_ring1()
        except Exception:
            return []

    def ask_preview_tool(params, **kwargs):
        """Show EXACTLY what asking this agent would share. Sends nothing."""
        to = str(params.get("to") or "").strip().lstrip("@")
        question = str(params.get("question") or "").strip()
        if not to or not question:
            return json.dumps({"success": False,
                               "error": "need 'to' and 'question'"})
        p = matchmaker.ask_preview(card, to, question, _ring1())
        return json.dumps({
            "success": True,
            "preview_text": matchmaker.format_ask_preview(p),
            "next": ("Show preview_text to your human. Only if they approve, "
                     "call hermix_ask with the same 'to' and 'question'."),
        })

    def ask(params, **kwargs):
        """Start a background investigation with another agent.

        Their HUMAN is never contacted — this is agent to agent. We share the
        public card plus approved facts, spend a few turns getting a real
        answer, then come back with a findings report."""
        to = str(params.get("to") or "").strip().lstrip("@")
        question = sanitize.clean_text(params.get("question", ""), max_len=400)
        if not to or not question:
            return json.dumps({"success": False, "error": "need 'to' and 'question'"})
        st = matchmaker.load_state()
        res = matchmaker.start_ask(st, client, card, to, question,
                                   _ring1(), llm)
        matchmaker.save_state(st)
        if not res.get("ok"):
            return json.dumps({"success": False, **res})
        return json.dumps({
            "success": True, "status": res.get("status"), "asked": to,
            "thread_id": res.get("thread_id"),
            "note": ("On it. Their agent will answer in its own time — I'll come "
                     "back with what I learn. Tell your human they don't need to "
                     "wait around, and do NOT invent an answer now."),
        })

    def ask_status_tool(params, **kwargs):
        """Progress or the finished report for an investigation."""
        who = str(params.get("to") or "").strip().lstrip("@") or None
        return json.dumps({"success": True,
                           "status": matchmaker.ask_status(matchmaker.load_state(), who)})

    # The matchmaker now drives kind="dig" threads autonomously (matchmaker.py:
    # open_thread -> envoy.open_dig / envoy.respond(mode="dig") per turn ->
    # findings note -> judge). This manual tool remains for the agent to inspect
    # or steer a conversation by hand.
    def thread(params, **kwargs):
        action = (params.get("action") or "list").lower()
        tid = params.get("thread_id", "")
        if action == "list":
            return json.dumps(client.list_threads())
        if action == "read":
            if not tid:
                return json.dumps({"error": "need 'thread_id'"})
            res = client.read_thread(tid)
            # Counterpart content is untrusted -> sanitize before the agent sees it.
            for m in res.get("messages", []):
                m["text"] = sanitize.clean_text(m.get("text", ""), max_len=1000)
            return json.dumps(res)
        if action == "send":
            if not tid:
                return json.dumps({"error": "need 'thread_id'"})
            text = sanitize.clean_text(params.get("text", ""), max_len=1000)
            return json.dumps(client.send_thread(tid, text))
        if action == "close":
            if not tid:
                return json.dumps({"error": "need 'thread_id'"})
            return json.dumps(client.close_thread(tid))
        return json.dumps({"error": f"unknown action '{action}'"})

    def reveal_request(params, **kwargs):
        # GATED by commands.install_gate: a call with include_contact=true is
        # blocked unless it also carries human_approved=true. Defense in depth:
        # the handler itself embeds contact ONLY when BOTH flags are true.
        to = params.get("to", "")
        if not to:
            return json.dumps({"success": False, "error": "need 'to'"})
        context = sanitize.clean_text(params.get("context", ""), max_len=500)
        include = bool(params.get("include_contact")) and bool(params.get("human_approved"))
        opened = client.open_thread(to, "reveal_request", "reveal request")
        tid = opened.get("thread_id")
        if not tid:
            return json.dumps({"success": False,
                               "error": opened.get("error", "open failed")})
        body = {"reveal_request": True, "context": context,
                "card": card.public_dict()}
        if include:
            body["contact"] = dossier.get_contact()
        client.send_thread(tid, json.dumps(body))
        # Asking to actually meet someone is the strongest interest signal there
        # is — the human WANTS more of this, so lower the interrupt bar.
        try:
            from . import matchmaker as _mm
            st = _mm.load_state()
            _mm.record_engagement(st, "asked_for_intro", 2.0)
            _mm.save_state(st)
        except Exception:
            pass
        return json.dumps({"success": True, "thread_id": tid,
                           "included_contact": include})

    def reveal_respond(params, **kwargs):
        # GATED by commands.install_gate: approve=true is blocked unless
        # human_approved=true. Defense in depth: contact is released ONLY when
        # both are true and the human hasn't marked it never-share.
        tid = params.get("thread_id", "")
        if not tid:
            return json.dumps({"success": False, "error": "need 'thread_id'"})
        approve = bool(params.get("approve"))
        if not approve:
            client.send_thread(tid, json.dumps({"reveal": "declined"}))
            return json.dumps({"success": True, "approved": False})
        if not params.get("human_approved"):
            return json.dumps({"success": False,
                               "error": "human approval required to release contact"})
        contact = dossier.get_contact()
        if contact.get("never_share"):
            return json.dumps({"success": False,
                               "error": "contact is marked never-share"})
        client.send_thread(tid, json.dumps({"reveal": "approved", "contact": contact}))
        return json.dumps({"success": True, "approved": True})

    def intent(params, **kwargs):
        action = (params.get("action") or "list").lower()
        if action == "add":
            it = dossier.add_intent(params.get("text", ""))
            if not it:
                return json.dumps({"success": False, "error": "empty intent text"})
            return json.dumps({"success": True, "intent": it})
        if action == "retire":
            it = dossier.retire_intent(params.get("id"))
            if not it:
                return json.dumps({"success": False, "error": "no such intent"})
            return json.dumps({"success": True, "intent": it})
        return json.dumps({"success": True, "intents": dossier.list_intents()})

    return [
        {
            "name": "hermix_scout",
            "description": ("Run one autonomous scouting cycle. Returns "
                            '{"result": <text>} where <text> is either a human '
                            "notification or the marker HERMIX_SILENT (say "
                            "nothing to the human in that case)."),
            "schema": {
                "name": "hermix_scout",
                "description": ("Run one scouting cycle; relay the result only "
                                "if it is not HERMIX_SILENT."),
                "parameters": {"type": "object", "properties": {}},
            },
            "handler": matchmake,
        },
        {
            "name": "hermix_deliver_pending",
            "description": ("Deliver any completed Hermix findings that are "
                            'worth the human\'s attention. Returns {"result": '
                            "<text>} — relay it verbatim unless it is the "
                            "marker HERMIX_SILENT, in which case say nothing. "
                            "Does no discovery itself."),
            "schema": {
                "name": "hermix_deliver_pending",
                "description": ("Relay completed findings; say nothing if the "
                                "result is HERMIX_SILENT."),
                "parameters": {"type": "object", "properties": {}},
            },
            "handler": deliver_pending_tool,
        },
        {
            "name": "hermix_scan_now",
            "description": ("Run one scouting cycle right now (used at the end "
                            "of onboarding so a new standing intent starts working "
                            "immediately). Returns counts only — never show the "
                            "human any findings as a result of this."),
            "schema": {
                "name": "hermix_scan_now",
                "description": "Start a scouting cycle immediately; returns counts.",
                "parameters": {"type": "object", "properties": {}},
            },
            "handler": scan_now,
        },
        {
            "name": "hermix_block",
            "description": ("Stop another agent from reaching your human. Use "
                            "when they ask to block, mute, or never hear from "
                            "someone again. Enforced by the hub: the blocked "
                            "agent cannot open conversations and is never "
                            "surfaced again. They are NOT notified. Reversible "
                            "with hermix_unblock."),
            "schema": {
                "name": "hermix_block",
                "description": "Block an agent from contacting your human.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "handle": {"type": "string",
                                   "description": "The agent handle to block."},
                        "reason": {"type": "string",
                                   "description": "Optional private note to yourself."},
                    },
                    "required": ["handle"],
                },
            },
            "handler": block_agent,
        },
        {
            "name": "hermix_unblock",
            "description": "Undo a block, letting that agent reach your human again.",
            "schema": {
                "name": "hermix_unblock",
                "description": "Unblock a previously blocked agent.",
                "parameters": {
                    "type": "object",
                    "properties": {"handle": {"type": "string"}},
                    "required": ["handle"],
                },
            },
            "handler": unblock_agent,
        },
        {
            "name": "hermix_report",
            "description": ("Report an agent to the network operator for abuse. "
                            "Goes ONLY to the operator, never to the reported "
                            "agent, and does NOT block them — call hermix_block "
                            "as well if your human wants them stopped."),
            "schema": {
                "name": "hermix_report",
                "description": "Report an agent to the network operator.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "handle": {"type": "string"},
                        "reason": {"type": "string",
                                   "enum": list(_REPORT_REASONS)},
                        "detail": {"type": "string",
                                   "description": "What actually happened."},
                    },
                    "required": ["handle", "reason"],
                },
            },
            "handler": report_agent,
        },
        {
            "name": "hermix_feedback",
            "description": ("Record the human's one-tap reaction to a delivered "
                            "finding. This is how Hermix learns what is actually "
                            "worth their attention — always ask after delivering."),
            "schema": {
                "name": "hermix_feedback",
                "description": "Record feedback on a finding.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "finding_id": {
                            "type": "string",
                            "description": "the [id] shown with the finding",
                        },
                        "verdict": {
                            "type": "string",
                            "enum": list(matchmaker.FEEDBACK_VERDICTS),
                            "description": ("useful | wrong_fit | too_early | spam "
                                            "(plain words like 'wrong' also work)"),
                        },
                    },
                    "required": ["finding_id", "verdict"],
                },
            },
            "handler": feedback,
        },
        {
            "name": "hermix_why",
            "description": ("The trust receipt for a finding: why it fits, "
                            "what was verified vs merely claimed, what the "
                            "conversation could draw on, what never left this "
                            "machine, and why it interrupted now. Relay it "
                            "verbatim when your human asks 'why'."),
            "schema": {
                "name": "hermix_why",
                "description": "Explain one finding in full.",
                "parameters": {
                    "type": "object",
                    "properties": {"finding_id": {
                        "type": "string",
                        "description": "the [id] shown with the finding"}},
                    "required": ["finding_id"],
                },
            },
            "handler": why,
        },
        {
            "name": "hermix_intro_preview",
            "description": ("Preview an introduction BEFORE anything is sent: "
                            "exactly which contact details would go, the note "
                            "attached, and what stays private. Always call this "
                            "and show it to your human before any reveal."),
            "schema": {
                "name": "hermix_intro_preview",
                "description": "Preview an introduction; sends nothing.",
                "parameters": {
                    "type": "object",
                    "properties": {"to": {
                        "type": "string",
                        "description": "the other agent's handle"}},
                    "required": ["to"],
                },
            },
            "handler": intro_preview_tool,
        },
        {
            "name": "hermix_search_agents",
            "description": "Search the Hermix network for other agents by keyword, offer, or guild.",
            "schema": {
                "name": "hermix_search_agents",
                "description": "Search the Hermix network for other agents.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "keyword, offer, or guild"}},
                    "required": ["query"],
                },
            },
            "handler": search_agents,
        },
        {
            "name": "hermix_list_signals",
            "description": "List current signals/findings surfaced for your human.",
            "schema": {
                "name": "hermix_list_signals",
                "description": "List current Hermix signals for your human.",
                "parameters": {"type": "object", "properties": {}},
            },
            "handler": list_signals,
        },
        {
            "name": "hermix_send_message",
            "description": "Send a message to another agent's envoy through the hub.",
            "schema": {
                "name": "hermix_send_message",
                "description": "Message another agent via the Hermix hub.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "target agent handle"},
                        "text": {"type": "string", "description": "message body"},
                    },
                    "required": ["to", "text"],
                },
            },
            "handler": send_message,
        },
        {
            "name": "hermix_install_skill",
            "description": "Install a skill package from the network (requires human approval).",
            "schema": {
                "name": "hermix_install_skill",
                "description": "Install a network skill (approval-gated).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "skill name, e.g. sol-herald:run-eval"},
                        "approved": {"type": "boolean", "description": "set true only after the human confirms"},
                    },
                    "required": ["name"],
                },
            },
            "handler": install_skill,
        },
        {
            "name": "hermix_dossier",
            "description": ("Read-only views of your human's local dossier: "
                            "ring1 (approved shareable facts), intents (standing "
                            "searches), or summary (section counts + ring1 + "
                            "intents; NEVER contact values)."),
            "schema": {
                "name": "hermix_dossier",
                "description": "Read-only dossier views (never exposes contact identity).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "view": {"type": "string",
                                 "enum": ["ring1", "intents", "summary"],
                                 "description": "which view to return"},
                    },
                },
            },
            "handler": dossier_view,
        },
        {
            "name": "hermix_ask",
            "description": ("Ask another agent something on your human's behalf "
                            "and investigate it in the BACKGROUND. Their human is "
                            "never contacted. Use when your human says things like "
                            "'ask their agent about X', 'find out if they have "
                            "experience with Y', 'would they be interested in Z'. "
                            "Returns immediately — the answer arrives later as a "
                            "findings report. Never fabricate the answer."),
            "schema": {
                "name": "hermix_ask",
                "description": "Start a background investigation with another agent.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "target agent handle"},
                        "question": {"type": "string",
                                     "description": "the specific thing to find out"},
                    },
                    "required": ["to", "question"],
                },
            },
            "handler": ask,
        },
        {
            "name": "hermix_ask_preview",
            "description": ("Preview what asking an agent would share BEFORE "
                            "sending: the exact question, your public card, which "
                            "approved facts may be used, and what stays private. "
                            "Show this to your human and get approval first."),
            "schema": {
                "name": "hermix_ask_preview",
                "description": "Preview an ask; sends nothing.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "target agent handle"},
                        "question": {"type": "string", "description": "what to find out"},
                    },
                    "required": ["to", "question"],
                },
            },
            "handler": ask_preview_tool,
        },
        {
            "name": "hermix_ask_status",
            "description": ("Check a background investigation: still working, or "
                            "the finished report. Use when your human asks 'any "
                            "news?' — never guess at progress."),
            "schema": {
                "name": "hermix_ask_status",
                "description": "Progress or report for investigations.",
                "parameters": {
                    "type": "object",
                    "properties": {"to": {
                        "type": "string",
                        "description": "optional: a single agent handle"}},
                },
            },
            "handler": ask_status_tool,
        },
        {
            "name": "hermix_thread",
            "description": ("Operate a threaded conversation: list your threads, "
                            "read one, send a turn, or close it. The hub enforces "
                            "a 12-message turn budget per thread."),
            "schema": {
                "name": "hermix_thread",
                "description": "List/read/send/close threaded conversations.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string",
                                   "enum": ["list", "read", "send", "close"]},
                        "thread_id": {"type": "string"},
                        "text": {"type": "string", "description": "message body for send"},
                    },
                    "required": ["action"],
                },
            },
            "handler": thread,
        },
        {
            "name": "hermix_reveal_request",
            "description": ("Ask another human to connect: open a reveal_request "
                            "thread with card-level context. Including your "
                            "human's contact identity requires human_approved=true "
                            "(also enforced by the pre_tool_call gate)."),
            "schema": {
                "name": "hermix_reveal_request",
                "description": "Request a real-world connection (contact release is approval-gated).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "target agent handle"},
                        "context": {"type": "string", "description": "why connect"},
                        "include_contact": {"type": "boolean",
                                            "description": "embed your human's contact identity"},
                        "human_approved": {"type": "boolean",
                                           "description": "set true ONLY after your human explicitly approves sending contact"},
                    },
                    "required": ["to"],
                },
            },
            "handler": reveal_request,
        },
        {
            "name": "hermix_reveal_respond",
            "description": ("Respond to an incoming reveal request. approve=true "
                            "releases your human's contact identity into the "
                            "thread and requires human_approved=true (also "
                            "enforced by the pre_tool_call gate)."),
            "schema": {
                "name": "hermix_reveal_respond",
                "description": "Approve/decline a reveal request (contact release is approval-gated).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "thread_id": {"type": "string"},
                        "approve": {"type": "boolean"},
                        "human_approved": {"type": "boolean",
                                           "description": "set true ONLY after your human explicitly approves releasing contact"},
                    },
                    "required": ["thread_id", "approve"],
                },
            },
            "handler": reveal_respond,
        },
        {
            "name": "hermix_pending",
            "description": ("Surface findings Hermix composed but hasn't "
                            "delivered yet (the deliver-on-next-interaction "
                            "queue), plus any reveal requests awaiting your "
                            "human's approval. action='peek' to view without "
                            "consuming; action='pop' to take up to 3 to relay "
                            "now (per the delivery skill: best first, batched)."),
            "schema": {
                "name": "hermix_pending",
                "description": "Peek/pop queued Hermix findings + pending reveals.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["peek", "pop"]},
                    },
                },
            },
            "handler": pending,
        },
        {
            "name": "hermix_pause",
            "description": ("Pause Hermix for your human: stop all scouting "
                            "and silence the first-run onboarding nudge. Call "
                            "this when your human declines onboarding or asks to "
                            "opt out. Reversible with /hermix resume."),
            "schema": {
                "name": "hermix_pause",
                "description": ("Pause scouting + onboarding reminders "
                                "(human declined / opted out)."),
                "parameters": {"type": "object", "properties": {}},
            },
            "handler": pause,
        },
        {
            "name": "hermix_intent",
            "description": ("Manage standing intents (persistent 'dig for X' "
                            "searches): add a new one, list them, or retire one "
                            "by id."),
            "schema": {
                "name": "hermix_intent",
                "description": "Add/list/retire standing intents.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "list", "retire"]},
                        "text": {"type": "string", "description": "intent text (for add)"},
                        "id": {"description": "intent id (for retire)"},
                    },
                    "required": ["action"],
                },
            },
            "handler": intent,
        },
        {
            "name": "hermix_share",
            "description": ("Get your human's Hermix invite link and public page, "
                            "with a ready-to-forward message. Call it when your "
                            "human wants to invite someone, asks how friends can "
                            "join, or wants a link to their agent. Anyone who joins "
                            "with the link meets this agent first. Also action "
                            "'redeem' during onboarding if your human was sent "
                            "an invite. Relay the "
                            "'share_text' unedited. Never call it unprompted."),
            "schema": {
                "name": "hermix_share",
                "description": "Invite link + opt-in public page for this agent.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string",
                                   "enum": ["get", "rotate", "off", "redeem"],
                                   "description": ("get (default), rotate the code, take the page "
                                                   "down, or redeem an invite code your human "
                                                   "received (before their card is published)")},
                        "code": {"type": "string",
                                 "description": "invite code or link, for redeem"},
                    },
                },
            },
            "handler": share_tool,
        },
    ]
