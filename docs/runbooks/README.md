---
type: moc
status: active
tags: [index]
---

# Runbooks — Map of Content

Operational procedures. "How do I do X" where X is a recurring task: verify a deploy, pull logs, restart a service, rotate a key.

Each runbook is copy-pasteable. If a step has a surprise, link out to a gotcha or reference rather than inlining a warning.

## Entries

- [[deploy-flow]] — `git push origin main` is the deploy; verification + rollback
- [[production-log-access]] — reading live container logs on homelab via Tailscale + SSH
