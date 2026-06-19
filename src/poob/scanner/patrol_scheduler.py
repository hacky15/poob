"""PatrolScheduler - adaptive timing scheduler for patrol cycles.

Adjusts polling frequency based on time of day:
- Peak (4-9 PM):      120s (2 min) - highest FB activity
- Moderate (8 AM-4 PM): 300s (5 min)
- Off-peak (evening/early morning): 600s (10 min)
- Dead (midnight-6 AM):  900s (15 min)

Applies Gaussian jitter (±20%, clamped to ±40%) to avoid detection.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from poob.utils import voice_activity
from poob.utils.logging import get_logger

if TYPE_CHECKING:
    from poob.config import AppConfig
    from poob.scanner.patrol_engine import PatrolEngine

log = get_logger("scanner.patrol_scheduler")


def _read_cgroup_memory_fraction(root: str = "/sys/fs/cgroup") -> float | None:
    """Return container memory usage as a fraction of its cgroup limit.

    Supports cgroup v2 (``memory.current`` / ``memory.max``) and v1
    (``memory/memory.usage_in_bytes`` / ``memory.limit_in_bytes``). Returns
    None when the limit is unset/unlimited or the files are unavailable (e.g.
    running outside a container) so the caller can skip the guard gracefully.
    """
    base = Path(root)
    # cgroup v2
    try:
        mx = (base / "memory.max").read_text().strip()
        if mx != "max":
            limit = int(mx)
            if limit > 0:
                cur = int((base / "memory.current").read_text().strip())
                return cur / limit
    except (OSError, ValueError):
        pass
    # cgroup v1
    try:
        limit = int((base / "memory" / "memory.limit_in_bytes").read_text().strip())
        # v1 "unlimited" is a near-2^63 sentinel — treat as no limit.
        if 0 < limit < (1 << 62):
            cur = int((base / "memory" / "memory.usage_in_bytes").read_text().strip())
            return cur / limit
    except (OSError, ValueError):
        pass
    return None


class PatrolScheduler:
    """Manages periodic execution of patrol cycles with adaptive timing.

    Same public interface as ScanScheduler (for ScanningCog compatibility):
    start(), stop(), pause(), resume(), trigger_now(),
    is_running, is_paused, last_scan_time, next_scan_time.

    Args:
        engine: The PatrolEngine to drive.
        config: Application configuration with patrol timing fields.
    """

    def __init__(self, engine: PatrolEngine, config: AppConfig) -> None:
        self._engine = engine
        self._config = config
        self._is_running = False
        self._is_paused = False
        self._task: asyncio.Task | None = None
        self._trigger_event = asyncio.Event()
        self._last_scan_time: datetime | None = None
        self._next_scan_time: datetime | None = None
        # Durable health store: record WHY we force-exit so the next boot can
        # DM the owner (an in-process alert can't fire — os._exit is a hard
        # kill). Best-effort; never blocks the recovery restart.
        self._health_kv = None
        from pathlib import Path as _Path

        _qdb = getattr(config, "quota_db_path", None)
        if isinstance(_qdb, (str, _Path)):
            try:
                from poob.quota.persistent_limiter import PersistentKV

                self._health_kv = PersistentKV(db_path=_Path(_qdb))
            except Exception:
                self._health_kv = None
        # Reliability watchdog: a wedged CDP get_page() does not honor
        # asyncio.wait_for cancellation, so the per-cycle timeout abandons the
        # cycle but cannot recover the browser — every subsequent cycle re-wedges
        # (observed: ~18.5h of zero output, 2026-05-31). After this many
        # CONSECUTIVE failed cycles the scheduler force-exits so the container
        # restart policy brings up a fresh session.
        _mcf = getattr(config, "patrol_max_consecutive_failures", 3)
        self._max_consecutive_failures = _mcf if isinstance(_mcf, int) and _mcf > 0 else 3
        self._consecutive_failures = 0
        # Memory-pressure guard: proactively restart the process before the
        # chromium leak OOMs the container or degrades CDP into a wedge.
        _mrp = getattr(config, "patrol_memory_restart_pct", 0.85)
        self._memory_restart_pct = float(_mrp) if isinstance(_mrp, (int, float)) else 0.85
        _mru = getattr(config, "patrol_memory_restart_min_uptime_s", 600.0)
        self._memory_restart_min_uptime_s = (
            float(_mru) if isinstance(_mru, (int, float)) and _mru >= 0 else 600.0
        )
        self._started_monotonic = time.monotonic()
        # Voice-priority backoff: poob runs the voice pipeline + this scanner
        # in one process on a CPU-only box. While users are actively in a voice
        # channel, a chromium patrol cycle starves real-time voice inference,
        # so we skip the cycle when the voice-activity beacon is recent. The
        # scanner resumes (full-tilt, no throughput loss) once voice goes quiet
        # for the window. See docs/decisions/patrol-backoff-during-voice.md.
        _svd = getattr(config, "patrol_skip_during_voice", True)
        self._skip_during_voice = bool(_svd)
        _vw = getattr(config, "patrol_voice_activity_window_s", 120.0)
        self._voice_activity_window_s = (
            float(_vw) if isinstance(_vw, (int, float)) and _vw > 0 else 120.0
        )
        self._voice_skips = 0  # consecutive cycles skipped for voice (telemetry)

    def _should_skip_for_voice(self) -> bool:
        """True if this patrol cycle should be deferred because the voice
        pipeline is actively processing audio (CPU priority to real-time
        voice). Resumes automatically once voice is quiet for the window.
        See docs/decisions/patrol-backoff-during-voice.md.
        """
        return self._skip_during_voice and voice_activity.is_active(
            self._voice_activity_window_s
        )

    @property
    def is_running(self) -> bool:
        """Whether the scheduler loop is active."""
        return self._is_running

    @property
    def is_paused(self) -> bool:
        """Whether patrolling is paused."""
        return self._is_paused

    @property
    def last_scan_time(self) -> datetime | None:
        """Timestamp of the last completed patrol cycle."""
        return self._last_scan_time

    @property
    def next_scan_time(self) -> datetime | None:
        """Estimated time of the next patrol cycle."""
        return self._next_scan_time

    async def start(self) -> None:
        """Start the patrol scheduler loop."""
        if self._is_running:
            return
        self._is_running = True
        self._task = asyncio.create_task(self._loop())
        log.info("Patrol scheduler started")

    async def stop(self) -> None:
        """Stop the patrol scheduler loop."""
        self._is_running = False
        if self._task:
            self._trigger_event.set()
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("Patrol scheduler stopped")

    def pause(self) -> None:
        """Pause patrolling (loop continues but skips cycles)."""
        self._is_paused = True
        log.info("Patrol scheduler paused")

    def resume(self) -> None:
        """Resume patrolling."""
        self._is_paused = False
        log.info("Patrol scheduler resumed")

    async def trigger_now(self) -> None:
        """Trigger an immediate patrol cycle, starting the scheduler if needed."""
        if not self._is_running:
            log.info("Scheduler not running — starting it now")
            await self.start()
        log.info("Immediate patrol triggered")
        self._trigger_event.set()

    def _record_cycle_outcome(self, *, succeeded: bool) -> None:
        """Track consecutive cycle failures; force a process restart on a wedge.

        A wedged CDP ``get_page()`` does not honor ``asyncio.wait_for``
        cancellation, so the per-cycle timeout abandons the cycle but cannot
        recover the browser — in-cycle timeouts are insufficient and the only
        reliable recovery is restarting the process. After
        ``_max_consecutive_failures`` consecutive failures (hang-abandon or
        error) the scheduler force-exits; the container restart policy
        (unless-stopped) brings up a fresh browser session. Bounds worst-case
        zero-output time to N x the cycle timeout instead of unbounded.
        See docs/incidents/main-browser-cdp-wedge-infinite-hang.md.
        """
        if succeeded:
            self._consecutive_failures = 0
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._max_consecutive_failures:
            log.critical(
                "Patrol failed consecutively — browser/CDP appears wedged and "
                "in-cycle timeouts cannot cancel it. Force-exiting so the "
                "container restart policy recovers a fresh session.",
                consecutive_failures=self._consecutive_failures,
                threshold=self._max_consecutive_failures,
            )
            if self._health_kv is not None:
                from poob.scanner.health_monitor import record_force_exit
                record_force_exit(self._health_kv, "watchdog: CDP wedge")
            sys.stderr.flush()
            os._exit(1)

    def _maybe_restart_on_memory_pressure(self) -> None:
        """Force a process restart when container memory nears the cgroup cap.

        Chromium leaks renderer processes over hours; browser-use spawns them
        internally, so we cannot safely reap individual processes without
        risking the live browser. Instead, when memory crosses the configured
        fraction of the cgroup limit, force-exit between cycles so the container
        restart policy brings up a fresh process with fresh chromium (memory
        reset). Skipped during the first ``_memory_restart_min_uptime_s`` so a
        startup spike can't loop. See
        docs/incidents/main-browser-cdp-wedge-infinite-hang.md.
        """
        if self._memory_restart_pct <= 0:
            return
        if (time.monotonic() - self._started_monotonic) < self._memory_restart_min_uptime_s:
            return
        frac = _read_cgroup_memory_fraction()
        if frac is None or frac < self._memory_restart_pct:
            return
        log.critical(
            "Container memory above restart threshold — force-exiting to reclaim "
            "leaked chromium memory before OOM; restart policy recovers a fresh session.",
            memory_pct=round(frac * 100, 1),
            threshold_pct=round(self._memory_restart_pct * 100, 1),
        )
        if self._health_kv is not None:
            from poob.scanner.health_monitor import record_force_exit
            record_force_exit(self._health_kv, "memory-pressure restart")
        sys.stderr.flush()
        os._exit(1)

    def _calculate_base_interval(self, hour: int) -> int:
        """Calculate the base interval in seconds for the given hour.

        Args:
            hour: Hour of day (0-23).

        Returns:
            Base interval in seconds.
        """
        peak_start = self._config.patrol_peak_hours_start
        peak_end = self._config.patrol_peak_hours_end

        if peak_start <= hour < peak_end:
            return self._config.patrol_peak_interval_seconds
        if 8 <= hour < peak_start:
            return self._config.patrol_moderate_interval_seconds
        if 0 <= hour < 6:
            return self._config.patrol_dead_interval_seconds
        # Off-peak: 6-8 AM and peak_end+
        return self._config.patrol_offpeak_interval_seconds

    def _apply_jitter(self, base_seconds: int) -> float:
        """Apply Gaussian jitter to interval, clamped to ±40%.

        Args:
            base_seconds: The base interval in seconds.

        Returns:
            Jittered interval in seconds.
        """
        # Gaussian with std dev = 20% of base
        jitter = random.gauss(0, base_seconds * 0.2)
        # Clamp to ±40%
        max_jitter = base_seconds * 0.4
        jitter = max(-max_jitter, min(max_jitter, jitter))
        return base_seconds + jitter

    async def _loop(self) -> None:
        """Main scheduler loop. Waits for adaptive interval or trigger, then patrols."""
        while self._is_running:
            now = datetime.now(timezone.utc)
            local_tz = ZoneInfo(self._config.display_timezone)
            local_hour = now.astimezone(local_tz).hour
            base_interval = self._calculate_base_interval(local_hour)
            interval_seconds = self._apply_jitter(base_interval)

            # Check if trigger was already set (e.g., trigger_now() called before
            # the loop task started — race between create_task and event.set).
            if self._trigger_event.is_set():
                log.info("Patrol triggered manually (immediate)")
                self._trigger_event.clear()
            else:
                self._next_scan_time = now + timedelta(seconds=interval_seconds)
                log.info(
                    "Waiting for next patrol",
                    base_interval=base_interval,
                    jittered_interval=round(interval_seconds, 1),
                    next_at=self._next_scan_time.strftime("%H:%M:%S"),
                )

                try:
                    await asyncio.wait_for(
                        self._trigger_event.wait(),
                        timeout=interval_seconds,
                    )
                    log.info("Patrol triggered manually")
                except asyncio.TimeoutError:
                    log.info("Scheduled patrol starting")
                self._trigger_event.clear()

            if not self._is_running:
                break

            if self._is_paused:
                continue

            # Voice-priority backoff: don't start a CPU-heavy chromium cycle
            # while users are actively in a voice channel — it starves
            # real-time voice (wake/STT/TTS) on the shared CPU. Skipping here
            # (before browser-recycle + memory-guard + the cycle) also avoids a
            # memory-pressure restart firing mid-VC and killing the voice
            # session. The scanner resumes once voice is quiet for the window;
            # no functionality is lost. See patrol-backoff-during-voice.md.
            if self._should_skip_for_voice():
                self._voice_skips += 1
                log.info(
                    "Patrol cycle skipped — active voice (CPU priority to voice)",
                    idle_required_s=self._voice_activity_window_s,
                    consecutive_voice_skips=self._voice_skips,
                )
                continue
            self._voice_skips = 0

            # First line of defense against the chromium leak: recycle the
            # browsers IN-PROCESS (reclaims renderer memory, keeps Discord/voice
            # up). Only if that can't help (non-browser memory) does the
            # whole-process os._exit guard below fire as a last resort.
            try:
                await self._engine.maybe_recycle_browsers(
                    memory_frac=_read_cgroup_memory_fraction(),
                )
            except Exception as exc:
                log.warning("Browser recycle check failed", error=str(exc)[:120])
            self._maybe_restart_on_memory_pressure()

            # Per-cycle hard timeout. Without this a single hung browser
            # call can silently kill the entire scheduler (observed in
            # prod April 25 — one hung anonymous_browser.get_page() ate
            # 51h of patrols).
            #
            # 600s (10 min) accommodates a full-volume local cycle: 50
            # detail-page enrichments at ~5s each with 2-tab parallelism
            # is ~125s, plus triage + VLM evaluation on the survivors
            # can take another 2-4 min. The previous 300s ceiling was
            # set when CA-bias was rejecting all but 0-3 listings per
            # cycle; once b628f7d shipped the WI-anchored URL and 50+
            # listings reach enrichment, 300s became too tight.
            try:
                result = await asyncio.wait_for(
                    self._engine.run_patrol_cycle(),
                    timeout=600.0,
                )
                self._last_scan_time = datetime.now(timezone.utc)
                log.info(
                    "Patrol cycle finished",
                    new_listings=result.new_listings,
                    deals=result.deals_found,
                    duration=f"{result.duration_seconds:.1f}s",
                )
                self._record_cycle_outcome(succeeded=True)
            except asyncio.TimeoutError:
                log.error(
                    "Patrol cycle hung past 600s — abandoned, "
                    "scheduler continues to next interval",
                )
                self._record_cycle_outcome(succeeded=False)
            except Exception as exc:
                log.error("Patrol cycle error in scheduler", error=str(exc))
                self._record_cycle_outcome(succeeded=False)
