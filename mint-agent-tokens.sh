#!/usr/bin/env bash
#
# mint-agent-tokens — mint a short-lived GitHub App installation token per agent
# and write them to .idle-loop/agents.env, so each idle-loop agent comments under
# its own GitHub App identity (issue #29 / the IdentityRouter from #41).
#
# Reads app credentials from .idle-loop/agents-apps.json (gitignored — it holds
# app ids + private-key paths; NEVER commit it). For each agent it signs a short
# RS256 JWT with the app's private key and exchanges it for an installation token
# via the GitHub API, then writes `export IDLE_GH_TOKEN_<AGENT>=<token>` lines.
#
# Installation tokens last ~1 hour, so re-run this before a session (and the long-
# running daemon should re-mint hourly — tracked as a follow-up).
#
# Usage:
#   ./mint-agent-tokens.sh            # mint all agents -> .idle-loop/agents.env
#   ./mint-agent-tokens.sh --check    # validate config + report each bot login, no write
#   source .idle-loop/agents.env      # then run your idle-loop command
#
# Requires: openssl, curl, python3 (all stdlib-level; no pip deps).
# Apps are owned by your GitHub account/org — create them in viksuper555 (see README).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$REPO_DIR/.idle-loop"
APPS="$STATE/agents-apps.json"
OUT="$STATE/agents.env"
CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

if [ ! -f "$APPS" ]; then
  echo "error: $APPS not found." >&2
  echo "Create the GitHub Apps (see README → 'Per-agent GitHub identities'), then" >&2
  echo "copy examples/agents-apps.example.json to $APPS and fill it in." >&2
  exit 1
fi
for bin in openssl curl python3; do
  command -v "$bin" >/dev/null 2>&1 || { echo "error: '$bin' not on PATH" >&2; exit 127; }
done

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

# Build a short-lived RS256 App JWT and exchange it for an installation token.
mint_token() {  # $1=app_id $2=installation_id $3=private_key_path -> prints token
  local app_id="$1" inst="$2" key="$3" now header payload unsigned sig jwt
  [ -f "$key" ] || { echo "  private key not found: $key" >&2; return 1; }
  now=$(date +%s)
  header=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
  # exp must be <= 10 min out; iat back-dated 60s for clock skew.
  payload=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((now - 60))" "$((now + 540))" "$app_id" | b64url)
  unsigned="${header}.${payload}"
  sig=$(printf '%s' "$unsigned" | openssl dgst -sha256 -sign "$key" -binary | b64url)
  jwt="${unsigned}.${sig}"
  curl -fsS -X POST \
    -H "Authorization: Bearer ${jwt}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/app/installations/${inst}/access_tokens" \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["token"])'
}

# Report the login a token posts as (e.g. "idle-planner[bot]").
token_login() {  # $1=token -> prints login
  curl -fsS -H "Authorization: token $1" -H "Accept: application/vnd.github+json" \
    "https://api.github.com/user" 2>/dev/null \
    | python3 -c 'import sys, json; d=json.load(sys.stdin); print(d.get("login","?"))' 2>/dev/null \
    || echo "(login lookup unavailable — installation tokens may 404 on /user; that is fine)"
}

mkdir -p "$STATE"
agents=$(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1])).keys()))' "$APPS")
[ -n "$agents" ] || { echo "error: no agents in $APPS" >&2; exit 1; }

tmp="$OUT.tmp.$$"
: > "$tmp"
trap 'rm -f "$tmp"' EXIT
rc=0
for agent in $agents; do
  read -r app_id inst key < <(python3 -c '
import json, sys
a = json.load(open(sys.argv[1]))[sys.argv[2]]
print(a["app_id"], a["installation_id"], a["private_key"])
' "$APPS" "$agent")
  printf 'minting %s (app %s, installation %s)... ' "$agent" "$app_id" "$inst"
  if ! token=$(mint_token "$app_id" "$inst" "$key"); then
    echo "FAILED"; rc=1; continue
  fi
  env_var="IDLE_GH_TOKEN_$(printf '%s' "$agent" | tr '[:lower:]' '[:upper:]')"
  if [ "$CHECK" -eq 1 ]; then
    echo "ok -> posts as $(token_login "$token")"
  else
    printf 'export %s=%s\n' "$env_var" "$token" >> "$tmp"
    echo "ok -> $env_var (expires ~1h)"
  fi
done

if [ "$CHECK" -eq 1 ]; then
  echo "--check only; no file written."
  exit "$rc"
fi

mv "$tmp" "$OUT"
chmod 600 "$OUT"
trap - EXIT
echo
echo "wrote $OUT (chmod 600, gitignored). Now run:"
echo "    source $OUT && python idle_loop.py --max-tickets N"
echo "Tokens expire in ~1 hour — re-run this script for a new session."
exit "$rc"
