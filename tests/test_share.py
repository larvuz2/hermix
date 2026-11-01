"""Invites from the plugin side: sharing is opt-in and pasteable, and an invite
code from the installer reaches the hub exactly once, at registration."""
import json

from hermix import _config, commands, profile, share, tools
from hermix.client import HermixClient, ensure_registered
from hermix.mock_backend import MockBackend


def _llm(system, user, **kw):
    return "x"


def _live(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
    monkeypatch.delenv("HERMIX_API_URL", raising=False)
    monkeypatch.setenv("HERMIX_API_KEY", "key123")


def test_share_refuses_before_onboarding(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
    monkeypatch.delenv("HERMIX_API_KEY", raising=False)
    res = share.share(HermixClient(MockBackend()), profile.PublicCard())
    assert res["ok"] is False and "setup" in res["error"].lower()


def test_share_command_gives_a_forwardable_message(monkeypatch, tmp_path):
    _live(monkeypatch, tmp_path)
    h = commands.make_handler(HermixClient(MockBackend()),
                              profile.PublicCard(handle="gus-herald"), _llm)
    out = h("share")
    assert "https://hermix.dev/i/" in out
    assert "--invite" in out and "@gus-herald" in out
    assert "page is OFF" in h("share off")


def test_share_tool_returns_share_text(monkeypatch, tmp_path):
    _live(monkeypatch, tmp_path)
    specs = {s["name"]: s for s in tools.build(HermixClient(MockBackend()),
                                               profile.PublicCard(handle="gus-herald"), _llm)}
    res = json.loads(specs["hermix_share"]["handler"]({"action": "rotate"}))
    assert res["success"] is True and "--invite" in res["share_text"]


def test_invite_code_is_sent_at_registration(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMIX_API_URL", raising=False)
    monkeypatch.delenv("HERMIX_API_KEY", raising=False)
    monkeypatch.setenv("HERMIX_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("HERMIX_INVITE", " AbCd-2345 ")
    seen = {}
    backend = MockBackend()
    real = backend.register

    def spy(handle, represents="", invite=""):
        seen["invite"] = invite
        return real(handle, represents, invite=invite)
    backend.register = spy
    assert ensure_registered(HermixClient(backend), profile.PublicCard(handle="ana"))
    assert seen["invite"] == "abcd2345"


def test_old_transport_without_invite_support_still_joins(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMIX_API_KEY", raising=False)
    monkeypatch.setenv("HERMIX_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("HERMIX_INVITE", "abcd2345")

    class Old(MockBackend):
        def register(self, handle, represents=""):
            return {"api_key": "k", "handle": handle}
    assert ensure_registered(HermixClient(Old()), profile.PublicCard(handle="ana"))


def test_help_lists_share(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
    out = commands.make_handler(HermixClient(MockBackend()), profile.PublicCard(), _llm)("help")
    assert "/hermix share" in out and "match" not in out.lower()


def test_redeem_saves_the_code_before_joining(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMIX_API_KEY", raising=False)
    monkeypatch.delenv("HERMIX_INVITE", raising=False)
    monkeypatch.setenv("HERMIX_ENV_FILE", str(tmp_path / ".env"))
    res = share.redeem("https://hermix.dev/i/abcd2345/")
    assert res["ok"] and _config.invite_code() == "abcd2345"
    assert "HERMIX_INVITE=abcd2345" in (tmp_path / ".env").read_text()


def test_redeem_after_joining_says_so(monkeypatch):
    monkeypatch.setenv("HERMIX_API_KEY", "k")
    assert share.redeem("abcd2345")["ok"] is False
