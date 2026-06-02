---
type: runbook
status: active
date: 2026-06-02
tags: [scanner, facebook, authentication, login, novnc, operations]
related: [[fb-session-cookie-persistence]] [[facebook-cookie-import]] [[watchlist-sweep-unprotected-cdp-wedge]]
---

# Runbook: one-time remote Facebook login (no cookie files)

The "perfection" auth path: log into Facebook **once** through a remote screen,
and the patrol adopts the session and keeps it alive indefinitely (Fix B
persists Facebook's rolled token each healthy cycle; Fix C prevents the CDP
wedge that used to freeze it). No cookie exports, no repeats. This replaces the
[[facebook-cookie-import]] flow for operators who don't want to handle cookie
JSON — both still work; this one is just a human logging in on a screen.

**Do this when** the patrol logs `FB authentication result status=failed` (the
heartbeat also DMs you), i.e. there is no live session and you want the fresh
(<10 min) just-listed feed back. It is rare (~weeks-to-months once seeded,
unless Facebook hard-invalidates).

## Step 1 — start the remote login (on the homelab)

`poob.login_server` brings up a virtual display + VNC bridge + headful Chromium
and binds noVNC to **localhost only**. Run it as a one-shot container sharing
the patrol's browser volume so the captured session lands where the patrol
reads it:

```bash
docker run --rm --name poob-login \
  -p 127.0.0.1:6080:6080 \
  -v poob_poob-browser:/app/browser_profiles \
  --env-file /path/to/poob.env \
  ghcr.io/hacky15/poob:latest \
  python -m poob.login_server
```

It logs a `Remote login ready` line. (The patrol can keep running — the login
browser uses its own profile dir, `browser_profiles/login/`, and only writes the
shared `facebook/cookies.json` at the end.)

## Step 2 — reach the screen (over SSH/Tailscale, never an exposed port)

From your machine:

```bash
ssh -L 6080:localhost:6080 <homelab>
```

Then open **http://localhost:6080/vnc.html** in your browser and click Connect.
You'll see Chromium on Facebook.

## Step 3 — log in (once)

Log into Facebook normally in that screen — including any 2FA / checkpoint. This
is a real human session, not automation, so there is no anti-bot risk. The
moment a session cookie (`xs`) appears, `login_server` captures all FB cookies
to `browser_profiles/facebook/cookies.json` and logs:

```
Captured Facebook session — patrol will adopt it on next cycle ... count=N
```

It then exits (the one-shot container stops). You're done — close the SSH tunnel.

## Step 4 — confirm the patrol adopted it

The patrol imports the captured cookies on its next cycle:

```bash
docker logs poob 2>&1 | grep -E "FB authentication result|Persisted live FB session"
```

`status=logged_in` ⇒ the fresh feed is back and the strict <10-min "just-listed"
gate is active again. From here Fix B re-persists the rolled token each healthy
cycle (`Persisted live FB session cookies count=N`), so it self-maintains.

## Notes

- **Security:** VNC (5900) and noVNC (6080) bind to localhost in the container;
  the published port is `127.0.0.1:6080` on the homelab, reached only through the
  SSH tunnel. Never publish to `0.0.0.0`.
- **No automated login, ever** — this is a human logging in once; we never script
  credentials (ToS / account-safety, per the standing legality rules).
- **If it still dies within days** despite a fresh login, that points to
  server-side invalidation (not local expiry) — flag it; the remedy there is the
  deferred residential-proxy/identity work, not another login.
- **Timeout:** the login window is `POOB_LOGIN_TIMEOUT_S` (default 1800s / 30 min);
  re-run Step 1 if you need longer.
