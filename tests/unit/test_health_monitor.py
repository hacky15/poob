"""Tests for the proactive health heartbeat decision logic.

The 2026-06-02 dark-out went 15.5h unnoticed because the signals were logged
but watched by nothing. These tests pin the decision function that turns
durable health state into owner alerts, with transition de-dup so it never
becomes the new ignored "alive" ping. See
docs/decisions/proactive-health-heartbeat.md.
"""

from __future__ import annotations

import time

import pytest

from poob.quota.persistent_limiter import PersistentKV
from poob.scanner.health_monitor import (
    pending_health_alerts,
    record_delivery,
    record_force_exit,
)


@pytest.fixture
def kv(tmp_path):
    return PersistentKV(db_path=tmp_path / "kv.db")


class TestHealthMonitor:
    def test_force_exit_recovery_alerts_once(self, kv):
        record_force_exit(kv, "watchdog: CDP wedge")
        now = time.time()
        alerts = pending_health_alerts(kv, auth_ok=True, now=now)
        assert any("forced restart" in a and "CDP wedge" in a for a in alerts)
        # Cleared after alerting → no repeat on the next check.
        assert not any(
            "forced restart" in a
            for a in pending_health_alerts(kv, auth_ok=True, now=now)
        )

    def test_stale_force_exit_not_alerted(self, kv):
        record_force_exit(kv, "old")
        kv.set_float("health:last_force_exit_at", time.time() - 99999)
        alerts = pending_health_alerts(kv, auth_ok=True, now=time.time())
        assert not any("forced restart" in a for a in alerts)

    def test_auth_failed_alerts_once_then_recovers(self, kv):
        now = time.time()
        a1 = pending_health_alerts(kv, auth_ok=False, now=now)
        assert any("auth FAILED" in a for a in a1)
        # Sustained failure must NOT re-alert (no spam → no muting).
        assert not any(
            "auth FAILED" in a
            for a in pending_health_alerts(kv, auth_ok=False, now=now)
        )
        # Recovery emits exactly one recovery notice.
        a3 = pending_health_alerts(kv, auth_ok=True, now=now)
        assert any("auth recovered" in a for a in a3)

    def test_dark_out_after_long_silence(self, kv):
        now = time.time()
        record_delivery(kv, when=now - 7 * 3600)  # 7h ago
        alerts = pending_health_alerts(
            kv, auth_ok=True, now=now, no_delivery_alert_s=6 * 3600
        )
        assert any("No deal delivered" in a for a in alerts)
        # de-dup
        assert not any(
            "No deal delivered" in a
            for a in pending_health_alerts(
                kv, auth_ok=True, now=now, no_delivery_alert_s=6 * 3600
            )
        )

    def test_recent_delivery_no_dark_alert(self, kv):
        now = time.time()
        record_delivery(kv, when=now - 60)
        alerts = pending_health_alerts(
            kv, auth_ok=True, now=now, no_delivery_alert_s=6 * 3600
        )
        assert not any("No deal delivered" in a for a in alerts)

    def test_auth_failed_owns_darkout_no_double_alert(self, kv):
        now = time.time()
        record_delivery(kv, when=now - 7 * 3600)
        alerts = pending_health_alerts(
            kv, auth_ok=False, now=now, no_delivery_alert_s=6 * 3600
        )
        assert any("auth FAILED" in a for a in alerts)  # auth path owns it
        assert not any("No deal delivered" in a for a in alerts)  # no double-alert

    def test_healthy_state_is_silent(self, kv):
        record_delivery(kv, when=time.time() - 60)
        assert pending_health_alerts(kv, auth_ok=True, now=time.time()) == []
