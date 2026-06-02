"""Proactive health heartbeat — never let the scanner go dark unnoticed.

The 2026-06-02 dark-out (15.5h, zero notifications) was invisible: every
signal needed to detect it was LOGGED but watched by nothing, and the only
owner-facing ping was a "Poob is alive" startup DM that fired 6s after auth
failed. This module turns the durable health signals into a proactive owner DM.

Design constraints learned the hard way:
- Recovery is via ``os._exit(1)`` (a hard kill), so an in-process alert task
  cannot be relied on to fire. The signal MUST be written to durable
  ``PersistentKV`` BEFORE exit and surfaced on the NEXT boot.
- The auth=failed-after-restart case is the loudest and the one the bot
  CANNOT self-heal (needs an operator cookie re-seed), so it is actionable
  and runbook-linked.
- Alerts must de-dup on state transitions so they never become the new
  ignored "alive" ping.

DM delivery lives in the Discord workstream; this module emits the decision
(``pending_health_alerts``) and the caller (main.py) supplies a ``dm_owner``
callback, so nothing here couples to discord_bot/.
See docs/decisions/proactive-health-heartbeat.md.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from poob.utils.logging import get_logger

if TYPE_CHECKING:
    from poob.quota.persistent_limiter import PersistentKV

log = get_logger("scanner.health_monitor")

# PersistentKV keys (survive os._exit / container restart).
KV_LAST_FORCE_EXIT = "health:last_force_exit_reason"
KV_LAST_FORCE_EXIT_AT = "health:last_force_exit_at"
KV_LAST_DELIVERY_AT = "health:last_delivery_at"
KV_AUTH_ALERTED = "health:auth_failed_alerted"
KV_DARK_ALERTED = "health:dark_out_alerted"


def record_force_exit(kv: PersistentKV, reason: str) -> None:
    """Persist the reason for an imminent os._exit so the NEXT boot can alert.

    Called immediately before os._exit in the scheduler. Best-effort: a KV
    write failure must never block the recovery restart.
    """
    try:
        kv.set(KV_LAST_FORCE_EXIT, reason)
        kv.set_float(KV_LAST_FORCE_EXIT_AT, time.time())
    except Exception as exc:  # noqa: BLE001 — never block the os._exit path
        log.warning("Failed to persist force-exit reason", error=str(exc)[:100])


def record_delivery(kv: PersistentKV, when: float | None = None) -> None:
    """Persist the timestamp of a successful deal notification (durable)."""
    try:
        kv.set_float(KV_LAST_DELIVERY_AT, when if when is not None else time.time())
    except Exception as exc:  # noqa: BLE001 — telemetry must not break delivery
        log.debug("Failed to persist delivery ts", error=str(exc)[:100])


def pending_health_alerts(
    kv: PersistentKV,
    *,
    auth_ok: bool,
    now: float,
    no_delivery_alert_s: float = 21600.0,
    force_exit_recent_s: float = 900.0,
) -> list[str]:
    """Return owner-alert messages for the current health state (pure-ish).

    Transition-de-duped via KV flags so a sustained bad state alerts ONCE on
    entry and once on recovery — never every check. Reads/writes only the KV;
    no network, no CDP. Safe to call from a periodic task or at startup.

    Conditions (in priority order):
      1. Recovered from a forced restart (watchdog/memory) since last boot.
      2. FB auth FAILED — fresh feed dark; needs an operator cookie re-seed
         (the one thing the bot cannot self-heal). Loudest, runbook-linked.
      3. No deal delivered in a long window — coarse dark-out backstop.
    """
    alerts: list[str] = []

    # 1) Forced-restart recovery (write happened before the prior os._exit).
    fx_reason = kv.get(KV_LAST_FORCE_EXIT)
    if fx_reason:
        fx_at = kv.get_float(KV_LAST_FORCE_EXIT_AT, 0.0)
        if now - fx_at <= force_exit_recent_s:
            alerts.append(
                f"⚠️ Poob auto-recovered from a forced restart (reason: {fx_reason}). "
                "The reliability watchdog did its job; flagging so it's not silent."
            )
        # Clear regardless so it alerts once, not every boot.
        kv.delete(KV_LAST_FORCE_EXIT)
        kv.delete(KV_LAST_FORCE_EXIT_AT)

    # 2) Auth failed — transition-de-duped.
    if not auth_ok:
        if not kv.get_bool(KV_AUTH_ALERTED, False):
            alerts.append(
                "🔴 FB auth FAILED — the authenticated just-listed feed is DARK, so "
                "deals will be sparse and mostly age-gated. Re-import cookies "
                "(runbook: facebook-cookie-import). This is the one thing I can't self-heal."
            )
            kv.set_bool(KV_AUTH_ALERTED, True)
    elif kv.get_bool(KV_AUTH_ALERTED, False):
        alerts.append("✅ FB auth recovered — fresh feed is back online.")
        kv.set_bool(KV_AUTH_ALERTED, False)

    # 3) Long delivery silence (coarse backstop; the precise age-gated-incredible
    #    fingerprint is the fast-follow). Only when auth is OK (an auth=failed
    #    dark-out is already covered by #2 and would double-alert otherwise).
    if auth_ok:
        last_delivery = kv.get_float(KV_LAST_DELIVERY_AT, 0.0)
        if last_delivery > 0 and (now - last_delivery) > no_delivery_alert_s:
            if not kv.get_bool(KV_DARK_ALERTED, False):
                hrs = (now - last_delivery) / 3600.0
                alerts.append(
                    f"🟠 No deal delivered in ~{hrs:.0f}h despite auth being healthy — "
                    "feed may be rate-limited/shadow-banned or the marketplace is quiet."
                )
                kv.set_bool(KV_DARK_ALERTED, True)
        elif kv.get_bool(KV_DARK_ALERTED, False):
            kv.set_bool(KV_DARK_ALERTED, False)

    return alerts
