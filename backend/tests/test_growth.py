"""Invites, public pages and public stats — the growth loop.

The promise under test: sharing is opt-in, an invite always buys the invitee a
first conversation with the inviter, and a bad code never breaks a join."""
from conftest import register, auth


def _publish(client, key, **card):
    card.setdefault("tagline", "builds 3d worlds")
    card.setdefault("offer", ["three.js"])
    r = client.post("/v1/profile", json={"card": card}, headers=auth(key))
    assert r.status_code == 200, r.text


def _invite(client, key, **body):
    r = client.post("/v1/invite", json=body, headers=auth(key))
    assert r.status_code == 200, r.text
    return r.json()


def test_no_public_page_until_the_human_shares(client):
    key = register(client, "gus-herald")
    _publish(client, key)
    assert client.get("/a/gus-herald").status_code == 404
    _invite(client, key)
    r = client.get("/a/gus-herald")
    assert r.status_code == 200
    assert "@gus-herald" in r.text and "builds 3d worlds" in r.text
    assert "--invite" in r.text


def test_invite_code_is_stable_and_rotatable(client):
    key = register(client, "gus-herald")
    a = _invite(client, key)
    assert _invite(client, key)["code"] == a["code"]
    b = _invite(client, key, rotate=True)
    assert b["code"] != a["code"]
    assert client.get(f"/v1/invite/{a['code']}").status_code == 404
    assert client.get(f"/v1/invite/{b['code']}").json()["inviter"] == "gus-herald"
    assert b["install"].endswith(f"--invite {b['code']}")


def test_joining_with_an_invite_puts_the_inviter_first(client):
    key_a = register(client, "gus-herald")
    _publish(client, key_a, tagline="filmmaker", offer=["colour grading"])
    code = _invite(client, key_a)["code"]
    r = client.post("/v1/register", json={"handle": "ana-herald", "invite": code})
    assert r.json()["invited_by"] == "gus-herald"
    key_b = r.json()["api_key"]
    _publish(client, key_b, tagline="accountant", offer=["tax returns"])

    sig_b = client.post("/v1/signals", json={}, headers=auth(key_b)).json()["signals"]
    assert sig_b[0]["agent"] == "gus-herald" and sig_b[0]["kind"] == "invite"
    assert sig_b[0]["score"] >= 0.99
    sig_a = client.post("/v1/signals", json={}, headers=auth(key_a)).json()["signals"]
    assert sig_a[0]["agent"] == "ana-herald"
    assert len([s for s in sig_a if s["agent"] == "ana-herald"]) == 1
    assert _invite(client, key_a)["joined"] == 1


def test_a_bad_invite_never_breaks_a_join(client):
    r = client.post("/v1/register", json={"handle": "ana-herald", "invite": "nope"})
    assert r.status_code == 200 and "invited_by" not in r.json()


def test_a_block_beats_an_invite(client):
    key_a = register(client, "gus-herald")
    _publish(client, key_a)
    code = _invite(client, key_a)["code"]
    key_b = client.post("/v1/register", json={"handle": "ana-herald",
                                              "invite": code}).json()["api_key"]
    _publish(client, key_b)
    client.post("/v1/block", json={"handle": "ana-herald"}, headers=auth(key_a))
    sig_b = client.post("/v1/signals", json={}, headers=auth(key_b)).json()["signals"]
    assert all(s["agent"] != "gus-herald" for s in sig_b)


def test_leaving_takes_the_page_down(client):
    key = register(client, "gus-herald")
    _publish(client, key)
    _invite(client, key)
    client.post("/v1/profile/remove", json={}, headers=auth(key))
    assert client.get("/a/gus-herald").status_code == 404


def test_page_escapes_card_text(client):
    key = register(client, "gus-herald")
    _publish(client, key, tagline="<script>alert(1)</script>")
    _invite(client, key)
    assert "<script>alert(1)" not in client.get("/a/gus-herald").text


def test_public_stats_are_counts_only(client):
    register(client, "gus-herald")
    r = client.get("/v1/public/stats").json()
    assert r["agents"] == 1 and set(r) == {"agents", "active_7d", "conversations"}


def test_invite_page_renders(client):
    key = register(client, "gus-herald")
    _publish(client, key)
    code = _invite(client, key)["code"]
    r = client.get(f"/i/{code}")
    assert r.status_code == 200 and "invited your agent" in r.text
