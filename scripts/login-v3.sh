#!/usr/bin/env bash
# IDMC v3 login. Mints an INFA-SESSION-ID and (when present) discovers per-product
# base URLs returned in the login response. Persists IDMC_V3_SESSION_ID and
# IDMC_V3_BASE_URL into .env alongside the existing v2 vars.
#
# Usage:
#   eval "$(./login-v3.sh)"
#
# Output (stdout, for eval'ing into the caller's shell):
#   export IDMC_V3_SESSION_ID=...
#   export IDMC_V3_BASE_URL=...   # baseApiUrl for the first product entry, if any
#
# v3 login docs:
#   POST https://{login-host}/saas/public/core/v3/login
#   Body: {"username":"...","password":"..."}
#   Response body: { userInfo, products:[{name, baseApiUrl}], ... }
#   Session id is returned EITHER in the response body field `sessionId` /
#   `userInfo.sessionId`, OR in a response header `INFA-SESSION-ID` —
#   the script accepts either source.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "error: .env not found at $ENV_FILE" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${IDMC_USER:?IDMC_USER not set in .env}"
: "${IDMC_PASS:?IDMC_PASS not set in .env}"
: "${IDMC_LOGIN_HOST:?IDMC_LOGIN_HOST not set in .env (e.g. dmp-us.informaticacloud.com)}"

URL="https://${IDMC_LOGIN_HOST}/saas/public/core/v3/login"

BODY="$(jq -n --arg u "$IDMC_USER" --arg p "$IDMC_PASS" \
  '{username:$u, password:$p}')"

HDR_FILE="$(mktemp -t idmc-v3-hdrs.XXXXXX)"
trap 'rm -f "$HDR_FILE"' EXIT

RESP="$(curl -sS -X POST "$URL" \
  -D "$HDR_FILE" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  -w $'\n%{http_code}' \
  -d "$BODY")"

HTTP_CODE="${RESP##*$'\n'}"
JSON="${RESP%$'\n'*}"

if [[ "$HTTP_CODE" != "200" ]]; then
  echo "error: v3 login failed (HTTP $HTTP_CODE)" >&2
  echo "$JSON" >&2
  exit 1
fi

# Pull the session id from a response header first (case-insensitive),
# then fall back to common JSON locations.
SESSION_ID="$(awk -F': ' 'tolower($1)=="infa-session-id" {sub(/\r$/,"",$2); print $2; exit}' "$HDR_FILE")"
if [[ -z "${SESSION_ID:-}" ]]; then
  SESSION_ID="$(echo "$JSON" | jq -r '
    .sessionId // .userInfo.sessionId // .infaSessionId // empty
  ')"
fi

if [[ -z "${SESSION_ID:-}" ]]; then
  echo "error: v3 session id not present in response headers or body" >&2
  echo "--- response headers ---" >&2
  cat "$HDR_FILE" >&2
  echo "--- response body ---" >&2
  echo "$JSON" >&2
  exit 1
fi

# baseApiUrl for the first product (commonly the one you want for subsequent
# v3 calls). Pretty-printed name + URL pairs go to stderr for visibility.
BASE_URL="$(echo "$JSON" | jq -r '.products[0].baseApiUrl // empty')"

{
  echo "--- v3 login OK ---"
  echo "$JSON" | jq -r '
    if (.products // []) | length > 0
    then .products[] | "  product: \(.name // "?") -> \(.baseApiUrl // "?")"
    else "  (no products array in response)" end
  '
} >&2

# Persist into .env (idempotent rewrite).
TMP="$(mktemp "${ENV_FILE}.XXXXXX")"
awk -v sid="$SESSION_ID" -v base="$BASE_URL" '
  BEGIN { sid_seen=0; base_seen=0 }
  /^IDMC_V3_SESSION_ID=/ { print "IDMC_V3_SESSION_ID=" sid;  sid_seen=1;  next }
  /^IDMC_V3_BASE_URL=/   { print "IDMC_V3_BASE_URL=" base;   base_seen=1; next }
  { print }
  END {
    if (!sid_seen)  print "IDMC_V3_SESSION_ID=" sid
    if (base != "" && !base_seen) print "IDMC_V3_BASE_URL=" base
  }
' "$ENV_FILE" > "$TMP"

mv "$TMP" "$ENV_FILE"
chmod 600 "$ENV_FILE"

# Exports for the caller's shell.
printf 'export IDMC_V3_SESSION_ID=%q\n' "$SESSION_ID"
[[ -n "$BASE_URL" ]] && printf 'export IDMC_V3_BASE_URL=%q\n' "$BASE_URL"
