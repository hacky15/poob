---
type: runbook
status: active
date: 2026-05-29
tags: [scanner, facebook, authentication, cookies, operations]
related: [[authenticated-session-no-human]] [[main-browser-headful-in-headless-container]]
---

# Runbook: import a Facebook session into the patrol browser

The patrol "main" browser authenticates by adopting your **real FB session
cookies** (exported once from a browser where you're logged in). FB hardens
credential login against automation, so we do NOT automate login — cookie
import is the reliable, account-safe path. The cookies persist in the
`poob_poob-browser` Docker volume, so this is a rare (~weeks) task, not ongoing.

## When to do this

- First-time setup (no session yet), or
- Logs show `Imported FB cookies did not yield a logged-in session` or
  `FB authentication result status=no_session|failed` (session expired).

## Step 1 — export cookies (on a machine where you're logged into FB)

1. Install a cookie-export extension, e.g. **Cookie-Editor** (Chrome/Firefox).
2. Go to `https://www.facebook.com/` while logged in.
3. Open Cookie-Editor → **Export** → **Export as JSON** (copies a JSON array
   of cookie objects to your clipboard).

The export looks like:
```json
[
  {"name":"c_user","value":"100xxxxxxxxxxxx","domain":".facebook.com","path":"/","secure":true,"httpOnly":true,"expirationDate":1900000000,"sameSite":"no_restriction"},
  {"name":"xs","value":"...","domain":".facebook.com", ...},
  ...
]
```
The two that matter are `c_user` and `xs` (the session). `datr`, `sb`, etc.
help it look like your normal device. The loader keeps only `*.facebook.com`
cookies and ignores the rest, so exporting everything is fine.

**Treat this JSON like a password** — `xs` is a live session token.

## Step 2 — place the file in the volume

Target path (inside the container): `/app/browser_profiles/facebook/cookies.json`
→ host volume `poob_poob-browser` at
`/var/lib/docker/volumes/poob_poob-browser/_data/facebook/cookies.json`.

Any of these gets it there:
- Paste the JSON to the assistant, who writes it via SSH (it is never logged).
- Or write it yourself on the homelab:
  ```bash
  docker exec -i poob sh -c 'mkdir -p browser_profiles/facebook && cat > browser_profiles/facebook/cookies.json' < your-export.json
  ```

The format is flexible — the parser accepts a Cookie-Editor JSON array OR a
Playwright `storage_state` `{"cookies":[...]}` object, as a file or string.

## Step 3 — apply

Restart the container so startup re-runs auth and imports the cookies:
```bash
docker restart poob
```
(Or it applies on the next deploy.)

## Step 4 — verify

```bash
docker logs poob 2>&1 | grep -E "FB auth signals|FB authentication result|Imported FB cookies"
```
Success looks like:
```
Imported FB cookies into session count=N
FB auth signals phase=post_cookies ... logged_in=True
FB authentication result status=logged_in
```
`status=logged_in` ⇒ the main browser now sees the authenticated (fresher)
feed; the patrol engine prefers it automatically. `status=failed` ⇒ cookies
were stale/invalid — re-export (Step 1) with a freshly-logged-in session.

## Notes

- **No CAPTCHA risk from import** — we only inject cookies; there is no login
  event. (A login event is the #1 checkpoint trigger; that's why we avoid it.)
- **Refresh cadence:** FB sessions last weeks. When it expires, repeat Steps
  1–3. Until then it's fully hands-off.
- **Fallback is intact:** if there's no valid session, the scanner runs on the
  anonymous path (no breakage), just with the staler anon feed.
- **Never solve a CAPTCHA programmatically** — if FB ever shows a checkpoint,
  the scanner reports it and stays anonymous (per standing legality rules).
