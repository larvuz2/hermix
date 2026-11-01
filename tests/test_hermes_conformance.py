"""Conformance tests: our register(ctx) must match the REAL Hermes plugin API.

Every signature and contract asserted here was verified verbatim against the
Hermes Agent source (see docs/HERMES-API-GROUND-TRUTH.md, with file:line refs).
A real end-to-end load of this plugin through hermes_cli.plugins.PluginManager
also passed during development; because that source lives outside this repo, we
encode the same call patterns here with a spy PluginContext so the contract is
enforced in CI without the Hermes checkout.

If Hermes ever changes these signatures, these tests are the tripwire.
"""
import inspect
import pathlib

import pytest

import hermix
from hermix import commands, envoy, service


# Hook names Hermes accepts (subset of VALID_HOOKS, hermes_cli/plugins.py:135).
# register_hook warns-but-stores unknown names, so we assert we only use real
# ones to avoid silently-dead hooks.
REAL_VALID_HOOKS = {
    "pre_tool_call", "post_tool_call", "transform_terminal_output",
    "transform_tool_result", "transform_llm_output", "pre_llm_call",
    "post_llm_call", "pre_verify", "pre_api_request", "post_api_request",
    "api_request_error", "on_session_start", "on_session_end",
    "on_session_finalize", "on_session_reset", "subagent_start",
    "subagent_stop", "pre_gateway_dispatch", "pre_approval_request",
    "post_approval_response", "kanban_task_claimed", "kanban_task_completed",
    "kanban_task_blocked",
}


class FakeLlmResult:
    """Mimics agent.plugin_llm.PluginLlmCompleteResult — text lives on .text."""
    def __init__(self, text):
        self.text = text


class FakeLlm:
    """Mimics agent.plugin_llm.PluginLlm.complete (plugin_llm.py:622):
    synchronous, keyword-only extras, takes OpenAI-shaped `messages`, returns
    an object whose text is on `.text`."""
    def __init__(self):
        self.calls = []

    def complete(self, messages, *, provider=None, model=None, temperature=None,
                 max_tokens=None, timeout=None, agent_id=None, profile=None,
                 purpose=None):
        assert isinstance(messages, list), "messages must be an OpenAI-shape list"
        for m in messages:
            assert set(m) >= {"role", "content"}, "each message needs role+content"
        self.calls.append(messages)
        return FakeLlmResult("ENVOY_REPLY")


class SpyContext:
    """A PluginContext stand-in whose method signatures mirror the real facade
    (hermes_cli/plugins.py). Anything our register() does that the real facade
    would reject will blow up here too."""

    def __init__(self):
        self.tools = {}
        self.commands = {}
        self.hooks = {}
        self.skills = {}
        self.injected = []
        self._llm = FakeLlm()

    @property
    def llm(self):
        return self._llm

    # Real: register_tool(name, toolset, schema, handler, check_fn=None,
    #   requires_env=None, is_async=False, description="", emoji="", override=False)
    def register_tool(self, name, toolset, schema, handler, check_fn=None,
                      requires_env=None, is_async=False, description="",
                      emoji="", override=False):
        assert isinstance(name, str) and name
        assert isinstance(toolset, str) and toolset
        assert isinstance(schema, dict)
        assert callable(handler)
        self.tools[name] = dict(toolset=toolset, schema=schema, handler=handler,
                                description=description)

    # Real: register_command(name, handler, description="", args_hint="")
    def register_command(self, name, handler, description="", args_hint=""):
        assert isinstance(name, str) and name
        assert callable(handler)
        self.commands[name] = dict(handler=handler, description=description,
                                   args_hint=args_hint)

    # Real: register_hook(hook_name, callback)
    def register_hook(self, hook_name, callback):
        assert callable(callback)
        self.hooks.setdefault(hook_name, []).append(callback)

    # Real: register_skill(name, path: Path, description="")
    def register_skill(self, name, path, description=""):
        import re
        assert isinstance(name, str) and name
        assert ":" not in name, "namespace is derived; name must not contain ':'"
        assert re.match(r"^[a-zA-Z0-9_-]+$", name)
        assert pathlib.Path(path).exists(), f"skill path must exist: {path}"
        self.skills[name] = dict(path=str(path), description=description)

    # Real: inject_message(content, role="user") -> bool
    def inject_message(self, content, role="user"):
        self.injected.append((content, role))
        return True


@pytest.fixture
def loaded(monkeypatch, tmp_path):
    """Run our real register(ctx) against a spy, WITHOUT spawning the daemon
    thread — capture the (client, card, inject, llm) the plugin wired instead.

    HERMIX_HOME is redirected to a fresh tmp dir so the dossier is deterministic
    (absent -> not onboarded -> the register() bootstrap injection fires)."""
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
    # Force offline/mock so register()'s connectivity probe never hits the real
    # hub during unit tests (the default URL is now the public hub).
    monkeypatch.setenv("HERMIX_API_URL", "")
    captured = {}

    def fake_start(client, card, inject, llm, interval=90, matchmake=None,
                   match_interval=None, engine=None, inject_works=True):
        captured.update(client=client, card=card, inject=inject, llm=llm,
                        matchmake=matchmake, engine=engine,
                        inject_works=inject_works)
        return None  # no thread

    monkeypatch.setattr(service, "start", fake_start)
    ctx = SpyContext()
    hermix.register(ctx)
    return ctx, captured


# --------------------------------------------------------------------------- #
# register() overall
# --------------------------------------------------------------------------- #

def test_register_is_sync_single_positional_ctx():
    sig = inspect.signature(hermix.register)
    params = list(sig.parameters.values())
    assert len(params) == 1 and params[0].name == "ctx"
    assert not inspect.iscoroutinefunction(hermix.register)


def test_register_wires_expected_surface(loaded):
    ctx, _ = loaded
    assert set(ctx.tools) == {
        "hermix_scout", "hermix_search_agents", "hermix_list_signals",
        "hermix_send_message", "hermix_install_skill",
        "hermix_dossier", "hermix_ask", "hermix_thread",
        "hermix_reveal_request", "hermix_reveal_respond", "hermix_intent",
        "hermix_pending", "hermix_pause", "hermix_deliver_pending",
        "hermix_scan_now", "hermix_feedback", "hermix_why",
        "hermix_intro_preview", "hermix_ask_preview", "hermix_ask_status",
        "hermix_block", "hermix_unblock", "hermix_report",
            "hermix_share",
    }
    assert set(ctx.commands) == {"hermix"}
    # Both hooks are wired: the pre_tool_call consent gate and the pre_llm_call
    # first-run onboarding nudge (the gateway-safe bootstrap).
    assert list(ctx.hooks) == ["pre_tool_call", "pre_llm_call"]


def test_register_wires_the_five_behavioral_skills(loaded):
    ctx, _ = loaded
    assert set(ctx.skills) == {
        "hermix-context", "hermix-voice", "hermix-onboarding",
        "hermix-envoy-protocol", "hermix-delivery",
    }
    # Every registered skill path points at a real SKILL.md.
    for spec in ctx.skills.values():
        assert spec["path"].endswith("SKILL.md")


def test_first_run_injects_onboarding_bootstrap_once(loaded):
    """A fresh (not-onboarded) dossier => register() injects exactly one
    onboarding nudge via inject_message."""
    ctx, _ = loaded
    onboarding = [c for (c, _r) in ctx.injected if "onboarding" in c.lower()]
    assert len(onboarding) == 1


# --------------------------------------------------------------------------- #
# register_tool conformance
# --------------------------------------------------------------------------- #

def test_tool_schema_shape_and_toolset(loaded):
    ctx, _ = loaded
    for name, spec in ctx.tools.items():
        assert spec["toolset"] == "hermix"
        schema = spec["schema"]
        # Real shape (Spotify SPOTIFY_*_SCHEMA): {name, description, parameters}
        assert schema.get("name") == name, "schema.name must equal the tool name"
        assert "description" in schema
        assert schema["parameters"]["type"] == "object"


def test_tool_handler_convention(loaded):
    """Real dispatch (tools/registry.py:631): handler(args: dict, **kwargs) -> str."""
    ctx, _ = loaded
    out = ctx.tools["hermix_search_agents"]["handler"]({"query": "video"})
    assert isinstance(out, str)  # JSON string, normalized by the registry
    import json
    assert json.loads(out)["success"] is True


# --------------------------------------------------------------------------- #
# register_command conformance
# --------------------------------------------------------------------------- #

def test_command_handler_takes_single_raw_args_string(loaded):
    ctx, _ = loaded
    handler = ctx.commands["hermix"]["handler"]
    # Real call site (cli.py:9575 / gateway/run.py:12039): handler(user_args)
    result = handler("status")
    assert isinstance(result, str) and result


# --------------------------------------------------------------------------- #
# pre_tool_call gate: the security-critical return contract
# --------------------------------------------------------------------------- #

def test_install_gate_block_contract(loaded):
    ctx, _ = loaded
    gate = ctx.hooks["pre_tool_call"][0]
    # Real invocation is kwargs-only with tool_name + args
    # (hermes_cli/plugins.py:2145).
    directive = gate(tool_name="hermix_install_skill", args={"name": "x:y"})
    assert directive == {
        "action": "block",
        "message": directive["message"],
    }
    assert directive["action"] == "block"
    assert isinstance(directive["message"], str) and directive["message"]
    # Must NOT use the imagined {"allow": ...} / {"reason": ...} shape.
    assert "allow" not in directive and "reason" not in directive


def test_install_gate_allows_when_approved(loaded):
    ctx, _ = loaded
    gate = ctx.hooks["pre_tool_call"][0]
    assert gate(tool_name="hermix_install_skill",
                args={"name": "x:y", "approved": True}) is None


def test_install_gate_ignores_other_tools(loaded):
    ctx, _ = loaded
    gate = ctx.hooks["pre_tool_call"][0]
    assert gate(tool_name="some_other_tool", args={"a": 1}) is None
    # Only real kwargs supplied; must not read a legacy 'params'/'name' key.
    assert gate(tool_name="hermix_send_message", args={}) is None


def test_gate_hook_name_is_real(loaded):
    ctx, _ = loaded
    for hook_name in ctx.hooks:
        assert hook_name in REAL_VALID_HOOKS


# --------------------------------------------------------------------------- #
# ctx.llm adapter conformance
# --------------------------------------------------------------------------- #

def test_llm_adapter_uses_messages_and_returns_text(loaded, monkeypatch):
    """The SHAPE of the ctx.llm call, which is the Hermes contract we must not
    drift from. Routing to ctx.llm at all is opt-in (HERMIX_LLM defaults to
    "hub" so network work can never bill the user), so opt in explicitly here —
    test_llm_routing_and_optout.py owns the routing decision itself."""
    monkeypatch.setenv("HERMIX_LLM", "local")
    ctx, captured = loaded
    llm = captured["llm"]
    out = llm("SYSTEM PROMPT", "USER TEXT")
    assert out == "ENVOY_REPLY"  # unwrapped from .text
    assert len(ctx._llm.calls) == 1
    messages = ctx._llm.calls[0]
    assert messages[0] == {"role": "system", "content": "SYSTEM PROMPT"}
    assert messages[1] == {"role": "user", "content": "USER TEXT"}


def test_envoy_respond_flows_through_the_llm_adapter(loaded, monkeypatch):
    """End-to-end: the envoy answers using ONLY the card-derived system prompt
    via the wired llm adapter (proves the adapter is callable as envoy expects).

    Explicitly local for the same reason as the test above."""
    monkeypatch.setenv("HERMIX_LLM", "local")
    ctx, captured = loaded
    reply = envoy.respond(captured["card"], "who are you?", captured["llm"])
    assert isinstance(reply, str) and reply
    assert ctx._llm.calls, "envoy.respond must have driven ctx.llm.complete"


# --------------------------------------------------------------------------- #
# inject_message adapter conformance
# --------------------------------------------------------------------------- #

def test_inject_adapter_calls_inject_message(loaded):
    ctx, captured = loaded
    ctx.injected.clear()  # drop the first-run onboarding bootstrap
    captured["inject"]("hello human", "user")
    assert ctx.injected == [("hello human", "user")]


# --------------------------------------------------------------------------- #
# Open-source hygiene: the manifest is the first thing a reviewer reads, and it
# had silently drifted to 12 of 20 tools before anyone noticed.
# --------------------------------------------------------------------------- #
def _repo_root():
    import pathlib
    return pathlib.Path(__file__).resolve().parents[1]


def test_manifest_declares_every_registered_tool():
    import re
    from hermix import tools as tools_mod, profile as profile_mod
    from hermix.client import HermixClient
    from hermix.mock_backend import MockBackend

    registered = {s["name"] for s in tools_mod.build(
        HermixClient(MockBackend()), profile_mod.PublicCard(handle="x"), llm=None)}
    manifest = set(re.findall(r"- (hermix_\w+)",
                              (_repo_root() / "plugin.yaml").read_text(encoding="utf-8")))
    assert registered == manifest, {
        "missing from plugin.yaml": sorted(registered - manifest),
        "declared but not registered": sorted(manifest - registered),
    }


def test_the_project_is_actually_licensed():
    """plugin.yaml claims MIT. Without a LICENSE file the repo is legally
    all-rights-reserved and nobody may use or contribute to it."""
    text = (_repo_root() / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in text
    assert "Copyright (c)" in text
    manifest = (_repo_root() / "plugin.yaml").read_text(encoding="utf-8")
    assert "license: MIT" in manifest, "manifest and LICENSE must agree"


def test_security_policy_exists_and_names_the_real_limits():
    """We ship a privacy claim; the honest caveats have to be written down."""
    text = (_repo_root() / "SECURITY.md").read_text(encoding="utf-8")
    assert "security/advisories/new" in text          # a real reporting channel
    for limitation in ("not a sandbox", "LLM step", "untrusted"):
        assert limitation in text, limitation
