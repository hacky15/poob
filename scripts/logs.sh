#!/usr/bin/env bash
#
# Query Poob container logs on homelab over SSH+Tailscale.
#
# Wraps `ssh homelab "docker logs ..."` so any agent (Claude, etc.) or
# operator can pull production logs from the repo root with one command.
# Forwards every flag `docker logs` accepts — this script is intentionally
# thin so the underlying behavior is the docker CLI's.
#
# Usage:
#   scripts/logs.sh                              # poob, last 200 lines, with timestamps
#   scripts/logs.sh <container>                  # other container, same defaults
#   scripts/logs.sh <container> --tail N         # last N lines
#   scripts/logs.sh <container> --since 10m      # last 10 minutes
#   scripts/logs.sh <container> --since 2026-04-21T01:40:00Z
#   scripts/logs.sh <container> --since T1 --until T2
#   scripts/logs.sh <container> -f               # live stream
#   scripts/logs.sh --list                       # list running containers
#   scripts/logs.sh --help                       # this help
#
# Containers: poob, poob-ollama, poob-searxng, komodo-core,
# komodo-mongo, komodo-periphery.
#
# Retention: Docker keeps 5 x 50MB rolling files per container (~250MB).
# Queries past the rotation horizon return nothing.
#
# Prerequisite: `ssh homelab` must work from the calling shell — i.e. Tailscale
# is up and the homelab SSH config (key auth, Host alias) is loaded.

set -euo pipefail

# Shell-side argument parsing ONLY. Everything past `container` is forwarded
# verbatim to docker logs on the remote, so we don't enumerate docker's flags.

print_help() {
    sed -n '3,/^set -euo/p' "$0" | sed -E 's/^# ?//; /^set -euo/d; s/^!\/usr\/bin\/env bash$//'
}

case "${1:-}" in
    -h|--help)
        print_help
        exit 0
        ;;
    -l|--list)
        ssh homelab "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'"
        exit 0
        ;;
esac

container="${1:-poob}"
# Reject obviously bad container names early — pure defense, not a security boundary
# (docker logs would also reject them, but this keeps the error message readable).
if [[ ! "$container" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]]; then
    echo "error: invalid container name '$container'" >&2
    echo "       container names must match [a-zA-Z0-9][a-zA-Z0-9_.-]*" >&2
    exit 2
fi
shift

# Default to the most useful tail when no docker-logs flags supplied.
if [[ $# -eq 0 ]]; then
    set -- --tail 200 --timestamps
fi

# Build the remote command. Quoting matters: each forwarded arg is wrapped in
# single quotes (escape any single-quote inside an arg via '\'') so spaces and
# shell metacharacters in --since values like "2026-04-21T01:40:00Z" survive.
remote_cmd="docker logs"
for arg in "$@"; do
    escaped="${arg//\'/\'\\\'\'}"
    remote_cmd+=" '$escaped'"
done
remote_cmd+=" '${container//\'/\'\\\'\'}'"

exec ssh homelab "$remote_cmd"
