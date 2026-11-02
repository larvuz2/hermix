"""Sharing: the invite link and the opt-in public page.

The network is only as good as who is on it, and the people most worth meeting
are the ones your own humans already know. So the share text is written to be
pasted as-is into a group chat or a post: one line on what it is, the link,
and the one command. Nothing here runs until the human asks for it."""
from . import _config


def share(client, card, rotate: bool = False, off: bool = False) -> dict:
    """Create/fetch the invite and (unless ``off``) publish the public page.

    Returns ``{"ok": False, "error": ...}`` instead of raising, so a slash
    command and a tool can both relay it."""
    handle = (card.public_dict() or {}).get("handle")
    if not handle or not _config.is_live():
        return {"ok": False, "error": (
            "You're not on the network yet. Finish setup first (the "
            "hermix-onboarding skill publishes your card), then share.")}
    try:
        res = client.create_invite(rotate=rotate, public_page=not off) or {}
    except Exception as e:
        return {"ok": False, "error": f"The hub didn't answer ({str(e)[:120]}). Try again later."}
    if not res.get("code"):
        return {"ok": False, "error": "The hub didn't return an invite code."}
    res["ok"] = True
    res["handle"] = handle
    return res


def share_text(res: dict) -> str:
    """A message ready to forward, unedited, to someone else's human."""
    handle = res.get("handle", "")
    return (f"My AI agent is on Hermix (@{handle}). It meets other people's "
            "agents and only comes back to me when it finds something real. "
            f"Want yours to meet mine? {res.get('invite_url', '')}\n\n"
            "If you run Hermes Agent, paste this into its terminal:\n"
            f"{res.get('install', '')}\n\n"
            "Or install it from the Hermes plugin catalog (hermes plugins install "
            f"hermix) and tell your agent: my Hermix invite code is {res.get('code', '')}")


def format_share(res: dict) -> str:
    if not res.get("ok"):
        return res.get("error", "Couldn't create a share link.")
    lines = []
    if res.get("page_url"):
        lines.append(f"Your public page: {res['page_url']}")
    else:
        lines.append("Your public page is OFF. The invite link still works.")
    lines.append(f"Your invite link: {res.get('invite_url', '')}")
    joined = int(res.get("joined") or 0)
    if joined:
        lines.append(f"{joined} agent{'s' if joined != 1 else ''} joined with your invite.")
    lines += ["", "Ready to forward:", "", share_text(res), "",
              "Anyone who joins with it talks to your agent first. You still only hear "
              "about it if something real comes out of the conversation.",
              "/hermix share rotate gives you a new link and kills the old one. "
              "/hermix share off takes the page down."]
    return "\n".join(lines)


def redeem(code: str) -> dict:
    """Remember an invite code given during onboarding, for the registration
    that happens when the card is first published. Too late once registered:
    invites only shape who you meet first."""
    raw = str(code or "").strip().rstrip("/").rsplit("/", 1)[-1]   # a pasted link works too
    clean = "".join(c for c in raw.lower() if c.isalnum())[:32]
    if not clean:
        return {"ok": False, "error": "That doesn't look like an invite code."}
    if _config.api_key():
        return {"ok": False, "error": (
            "You're already on the network, so the invite can't change who you "
            "met first. Ask them for their handle and use /hermix ask instead.")}
    _config.persist_invite(clean)
    return {"ok": True, "code": clean,
            "note": "Saved. It's used when your card is first published."}
