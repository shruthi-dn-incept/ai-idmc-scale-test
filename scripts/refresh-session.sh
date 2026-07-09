#!/usr/bin/env bash
# Refresh IDMC session and persist it into .env.
#
# Usage:
#   eval "$(./refresh-session.sh)"
#
# After eval, IDMC_SESSION_ID and IDMC_SERVER_URL are exported in your shell
# AND written to .env so a fresh shell (or VS Code launched from this terminal)
# picks them up via the .env loader of your choice. VS Code's MCP config in
# .vscode/mcp.json reads ${env:IDMC_SESSION_ID} from process env, so launch
# VS Code from a shell that has run `eval "$(./refresh-session.sh)"`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"

# Capture exports from login.sh
LOGIN_OUTPUT="$("${SCRIPT_DIR}/login.sh")"

# Parse out values without eval'ing untrusted output back into this shell.
# login.sh emits:  export IDMC_SESSION_ID=...\nexport IDMC_SERVER_URL=...
SESSION_ID="$(printf '%s\n' "$LOGIN_OUTPUT" \
  | awk -F'=' '/^export IDMC_SESSION_ID=/ {print $2}')"
SERVER_URL="$(printf '%s\n' "$LOGIN_OUTPUT" \
  | awk -F'=' '/^export IDMC_SERVER_URL=/ {sub(/^export IDMC_SERVER_URL=/,""); print}')"

# %q-quoted values may have surrounding quotes; strip a single layer if present.
SESSION_ID="${SESSION_ID%\'}"; SESSION_ID="${SESSION_ID#\'}"
SERVER_URL="${SERVER_URL%\'}"; SERVER_URL="${SERVER_URL#\'}"

if [[ -z "$SESSION_ID" || -z "$SERVER_URL" ]]; then
  echo "error: could not parse session id / server url from login.sh output" >&2
  printf '%s\n' "$LOGIN_OUTPUT" >&2
  exit 1
fi

# Atomically rewrite .env, preserving non-session keys and updating
# IDMC_SESSION_ID / IDMC_SERVER_URL.
TMP="$(mktemp "${ENV_FILE}.XXXXXX")"
trap 'rm -f "$TMP"' EXIT

awk -v sid="$SESSION_ID" -v surl="$SERVER_URL" '
  BEGIN { sid_seen=0; surl_seen=0 }
  /^IDMC_SESSION_ID=/  { print "IDMC_SESSION_ID=" sid;  sid_seen=1;  next }
  /^IDMC_SERVER_URL=/  { print "IDMC_SERVER_URL=" surl; surl_seen=1; next }
  { print }
  END {
    if (!sid_seen)  print "IDMC_SESSION_ID=" sid
    if (!surl_seen) print "IDMC_SERVER_URL=" surl
  }
' "$ENV_FILE" > "$TMP"

mv "$TMP" "$ENV_FILE"
chmod 600 "$ENV_FILE"
trap - EXIT

# Emit exports for eval-ing into the caller's shell.
printf 'export IDMC_SESSION_ID=%q\n' "$SESSION_ID"
printf 'export IDMC_SERVER_URL=%q\n' "$SERVER_URL"
