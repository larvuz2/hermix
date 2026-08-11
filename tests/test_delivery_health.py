"""Is the proactive ping actually alive?

A cron job that exists but never fires produces exactly the same observable as
a healthy quiet network — silence — because silence is the product's normal
state. That symmetry is why the failure could previously last indefinitely with
nobody noticing, and it is the whole reason this check exists.

The engine is never gated on any of this: the daemon owns discovery and
conversation, so a delivery fault delays the ping rather than stopping the
network.
"""
import pytest

from hermix import matchmaker

HOUR = 3600
T0 = 1_000_000.0


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path))
    monkeypatch.setenv("HERMIX_MATCH_EVERY_HOURS", "6")


def _state(**kw):
    st = matchmaker.new_state()
    st.update(kw)
    return st


# --------------------------------------------------------------------------- #
# The stamp — the only evidence the plane is firing
# --------------------------------------------------------------------------- #
def test_a_delivery_run_is_stamped_even_when_it_says_nothing(monkeypatch):
    """Silent runs are the common case. If only noisy runs stamped, a healthy
    quiet agent would look identical to a dead scheduler."""
    matchmaker.save_state(matchmaker.new_state())
    out = matchmaker.deliver_and_persist(now=T0)
    assert out == matchmaker.SILENT
    assert matchmaker.load_state()["last_delivery_run"] == int(T0)


def test_the_stamp_advances_on_each_run():
    matchmaker.save_state(matchmaker.new_state())
    matchmaker.deliver_and_persist(now=T0)
    matchmaker.deliver_and_persist(now=T0 + 6 * HOUR)
    assert matchmaker.load_state()["last_delivery_run"] == int(T0 + 6 * HOUR)


# --------------------------------------------------------------------------- #
# Health verdicts
# --------------------------------------------------------------------------- #
def test_a_recent_run_with_a_scheduled_job_is_healthy(monkeypatch):
    monkeypatch.setattr(matchmaker, "_config", matchmaker._config)
    h = _health(monkeypatch, scheduled=True,
                state=_state(last_delivery_run=int(T0)), now=T0 + HOUR)
    assert h["status"] == "ok" and h["fault"] is False


def test_a_job_that_has_never_fired_is_a_fault(monkeypatch):
    h = _health(monkeypatch, scheduled=True, state=_state(), now=T0)
    assert h["status"] == "never-fired" and h["fault"] is True


def test_a_missing_job_is_a_fault(monkeypatch):
    h = _health(monkeypatch, scheduled=False,
                state=_state(last_delivery_run=int(T0)), now=T0 + HOUR)
    assert h["status"] == "missing" and h["fault"] is True


def test_two_missed_cycles_is_a_fault(monkeypatch):
    """One late run is ordinary — a sleeping laptop, a slow host. Two in a row
    is a stopped job, and the threshold has to sit between the two or the check
    is either useless or a nuisance."""
    h = _health(monkeypatch, scheduled=True,
                state=_state(last_delivery_run=int(T0)), now=T0 + 13 * HOUR)
    assert h["status"] == "stalled" and h["fault"] is True


def test_one_missed_cycle_is_not_a_fault(monkeypatch):
    h = _health(monkeypatch, scheduled=True,
                state=_state(last_delivery_run=int(T0)), now=T0 + 8 * HOUR)
    assert h["status"] == "ok" and h["fault"] is False


def test_no_scheduler_at_all_is_degraded_not_broken(monkeypatch):
    """Older Hermes has no cron. The daemon fallback still delivers, just not
    unprompted — that is a different sentence to the human, not an alarm."""
    h = _health(monkeypatch, scheduled=False, cron_available=False,
                state=_state(), now=T0)
    assert h["status"] == "daemon-fallback" and h["fault"] is False


# --------------------------------------------------------------------------- #
# What the human is told
# --------------------------------------------------------------------------- #
def test_doctor_reports_a_stalled_delivery_plainly(monkeypatch):
    from hermix import commands
    monkeypatch.setattr(matchmaker, "delivery_health", lambda *a, **k: {
        "status": "stalled", "fault": True, "age_seconds": 30 * HOUR,
        "interval_seconds": 6 * HOUR, "scheduled": True,
        "cron_available": True, "last_run": int(T0)})
    out = commands._doctor_view()
    assert "PROBLEM" in out
    assert "queued and safe" in out, "a fault must not imply findings were lost"


def test_doctor_reports_health_without_alarming(monkeypatch):
    from hermix import commands
    monkeypatch.setattr(matchmaker, "delivery_health", lambda *a, **k: {
        "status": "ok", "fault": False, "age_seconds": 2 * HOUR,
        "interval_seconds": 6 * HOUR, "scheduled": True,
        "cron_available": True, "last_run": int(T0)})
    out = commands._doctor_view()
    assert "Delivery: OK" in out
    assert "PROBLEM" not in out


def test_doctor_survives_a_broken_health_check(monkeypatch):
    """Diagnostics must never be the thing that breaks."""
    from hermix import commands

    def boom(*a, **k):
        raise RuntimeError("cron module exploded")

    monkeypatch.setattr(matchmaker, "delivery_health", boom)
    assert "Hermix doctor" in commands._doctor_view()


# --------------------------------------------------------------------------- #
def _health(monkeypatch, *, scheduled, state, now, cron_available=True):
    """Drive delivery_health with a stand-in for Hermes' cron module."""
    import sys
    import types

    if cron_available:
        mod = types.ModuleType("cron.jobs")
        mod.list_jobs = lambda: (
            [{"name": matchmaker.CRON_JOB_NAME}] if scheduled else [])
        pkg = types.ModuleType("cron")
        pkg.jobs = mod
        monkeypatch.setitem(sys.modules, "cron", pkg)
        monkeypatch.setitem(sys.modules, "cron.jobs", mod)
    else:
        monkeypatch.setitem(sys.modules, "cron", None)
    return matchmaker.delivery_health(state, now)
