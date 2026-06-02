---
type: incident
status: resolved
date: 2026-06-02
tags: [scanner, facebook, authentication, boot, cpu-bound]
related: [[fb-session-cookie-persistence]] [[facebook-remote-login]] [[adaptive-public-freshness-when-auth-down]]
---

# FB auth boot-render race: valid sessions false-negatived on a fixed-delay probe

## Symptom
Operator completed a fresh, fully-valid Facebook login (remote noVNC capture —
6 cookies incl. `c_user` + `xs`, confirmed). The patrol imported them on the
next boot but reported `status=failed`:

```
Imported FB cookies into session count=6
FB auth signals  has_login_form=False checkpoint=False has_app_chrome=False logged_in=False phase=post_cookies
Imported FB cookies did not yield a logged-in session (expired/invalid?) — staying anonymous
FB authentication result status=failed
```

Note `has_login_form=False` — FB was NOT bouncing to a login page; it simply
hadn't rendered the logged-in chrome yet. A standalone diagnostic loading the
same cookies on an *idle* container returned `loggedIn=True` at t=0s with the
user's name in the DOM — proving the session was valid.

## Root cause
`ensure_logged_in` navigated to `facebook.com`, waited a **fixed 2500ms**, then
took a single snapshot of the auth signals. FB home is an async-hydrated SPA.
The boot auth check runs ~17s into container startup — while a **CPU-only**
homelab is saturated loading wake-word / embedding / TTS models — so the
logged-in chrome (`[role="banner"]`, nav, marketplace link) had not hydrated
within 2500ms. The probe saw "no login form, no chrome" → inconclusive →
classified `FAILED`. Because `is_authenticated` is a set-once boot flag, the
patrol then stayed anonymous until the next restart, which re-raced the same
way. This silently defeated cookie import on essentially every restart under
load — a fixed sleep timer masquerading as readiness detection (the exact
anti-pattern the project bans).

## Fix
`src/poob/sites/facebook/auth.py` — `_detect_state` now **polls** the auth
signals until a conclusive verdict instead of trusting one delayed snapshot:

- LOGGED_IN / CHECKPOINT → return immediately (conclusive positive).
- A visible login form → return immediately (conclusive negative; chrome isn't
  coming).
- "Neither form nor chrome" → still hydrating; sleep `_AUTH_POLL_S` and re-poll
  until `settle_timeout_s`.

`ensure_logged_in` drops the 2500ms pre-detect wait to 800ms and gives the
post-cookie check a generous `_AUTH_SETTLE_POST_S` (25s, env-tunable) settle
budget — comfortably inside the existing 120s `wait_for`. This is real
DOM-readiness detection: fast hosts return in <1s, boot-contended hosts wait out
hydration instead of false-negativing.

## Validation
- `tests/unit/test_facebook_auth.py` — 24 pass, incl. new
  `test_import_succeeds_after_slow_hydration` (login-form home → two inconclusive
  post frames → chrome) and `TestDetectStatePolling` (poll-through,
  login-form/checkpoint short-circuit, inconclusive timeout). Existing tests
  unchanged — their mocked signals are all immediately conclusive, so the poll
  exits on the first frame.
- Watch on next deploy: `FB authentication result status=logged_in` with
  `settled_s` > 2.5 in the `post_cookies` signal line, then `Persisted live FB
  session cookies` each healthy cycle.

## Follow-ups
- **Fix A (per-cycle auth re-validation):** even with polling, an extreme boot
  stall could miss the window and leave the patrol anonymous until restart.
  Re-validating auth at cycle start (and re-importing cookies if the session
  dropped) removes the restart dependency entirely. Tracked as the heartbeat-
  protected hardening fast-follow.
