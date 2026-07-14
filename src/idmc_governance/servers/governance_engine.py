"""governance_engine_mcp.py — MCP server exposing IDMC CDQ governance tools.

Tools:
  - create_dq_rules        : create a rule specification end-to-end
  - list_rule_specifications: enumerate existing rule specs (FRS-filtered)

Transport: streamable HTTP. Default bind: 127.0.0.1:8765 (override via
GOVERNANCE_MCP_HOST / GOVERNANCE_MCP_PORT).

Auth: reads IDMC_USER, IDMC_PASS, IDMC_LOGIN_HOST from .env; mints v2 sessions
on demand and persists IDMC_SESSION_ID / IDMC_SERVER_URL back to .env so the
shell scripts and this server stay in sync. Sessions auto-refresh on HTTP 401.

Run locally:
    python governance_engine_mcp.py
Then point .vscode/mcp.json at http://127.0.0.1:8765/mcp.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
from idmc_governance.common.paths import ENV_PATH, REPO_ROOT, load_env_file  # repo-root paths (src-layout safe)
load_env_file()  # load repo-root .env into os.environ before the constants below read it
SCRIPT_DIR = Path(__file__).resolve().parent

_FRS_HOST  = os.getenv("IDMC_FRS_HOST", "")
_DQ_HOST   = os.getenv("IDMC_DQ_HOST",  "")
if not _FRS_HOST:
    raise RuntimeError(
        "IDMC_FRS_HOST must be set in process environment before starting governance-engine "
        "(e.g. $env:IDMC_FRS_HOST = 'usw3.dm-us.informaticacloud.com'). "
        "Reading from .env is too late — os.getenv() runs at module load time."
    )
FRS_API = f"https://{_FRS_HOST}/frs/api/v1"
FRS_V1  = f"https://{_FRS_HOST}/frs/v1"
RS      = f"https://{_DQ_HOST}/rule-service/api/v1" if _DQ_HOST else ""
UI_BASE = f"https://{_DQ_HOST}/dq-product/cloud/main/rulebuilder" if _DQ_HOST else ""

# Base for IDMC console URLs (mappings, tasks, monitoring). Override via env.
IDMC_UI_BASE = os.getenv("IDMC_UI_BASE", f"https://{_FRS_HOST}")

CDGC_API_BASE = os.getenv("CDGC_API_BASE", "https://cdgc-api.dm-us.informaticacloud.com")

# Default IDMC org id (from v3 login userInfo.orgId). Override via env.
DEFAULT_ORG_ID = os.getenv("IDMC_ORG_ID")

# Identity-service JWT minting endpoint. Mints a Bearer JWT from a v2/v3
# session id (passed via IDS-SESSION-ID header). Requires client_id + nonce
# query params (OIDC convention). Response body: {"jwt_token": "eyJ..."}.
#
# Returned JWTs are typically valid for 30 min (matches the iat/exp window
# we've seen in payloads). We cache for 29 min and refresh proactively.
IDMC_IDENTITY_HOST = os.getenv("IDMC_IDENTITY_HOST", "dmp-us.informaticacloud.com")
JWT_MINT_URL = f"https://{IDMC_IDENTITY_HOST}/identity-service/api/v1/jwt/Token"
JWT_CLIENT_ID = os.getenv("IDMC_JWT_CLIENT_ID", "idmc_api")
JWT_TTL_SECONDS = int(os.getenv("IDMC_JWT_TTL_SECONDS", "1740"))  # 29 min

# Default parent (same as INCEPT_TEST_NULL_CHECK). Override per-call via the
# tool args, or globally via env vars.
DEFAULT_SPACE_ID    = os.getenv("CDQ_SPACE_ID",    "7cCn5thwWFLhiZoSosphKL")
DEFAULT_SPACE_NAME  = os.getenv("CDQ_SPACE_NAME",  "REG")
DEFAULT_PROJECT_ID  = os.getenv("CDQ_PROJECT_ID",  "a3DaqI5cWMAfKahwNbNTcP")
DEFAULT_PROJECT_NAME = os.getenv("CDQ_PROJECT_NAME","Teradyne_CDQ_Training")
DEFAULT_FOLDER_ID   = os.getenv("CDQ_FOLDER_ID")
DEFAULT_FOLDER_NAME = os.getenv("CDQ_FOLDER_NAME")

log = logging.getLogger("governance_engine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_env_lock = threading.Lock()


# ---------------------------------------------------------------------------
# .env read/write
# ---------------------------------------------------------------------------
def _read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not ENV_PATH.exists():
        return env
    for raw in ENV_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def _write_env(env: dict[str, str]) -> None:
    tmp = ENV_PATH.with_name(ENV_PATH.name + ".tmp")
    tmp.write_text("\n".join(f"{k}={v}" for k, v in env.items()) + "\n")
    tmp.chmod(0o600)
    tmp.replace(ENV_PATH)


# ---------------------------------------------------------------------------
# Session minting
# ---------------------------------------------------------------------------
def _login_v2() -> tuple[str, str]:
    """Mint a fresh v2 session. Returns (session_id, server_url)."""
    env = _read_env()
    user = env.get("IDMC_USER")
    pw   = env.get("IDMC_PASS")
    host = env.get("IDMC_LOGIN_HOST", "dmp-us.informaticacloud.com")
    if not user or not pw:
        raise RuntimeError("IDMC_USER and IDMC_PASS must be set in .env")

    url = f"https://{host}/ma/api/v2/user/login"
    body = {"@type": "login", "username": user, "password": pw}
    r = httpx.post(url, json=body, headers={"Accept": "application/json"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"v2 login HTTP {r.status_code}: {r.text[:300]}")
    j = r.json()
    sid = j.get("icSessionId")
    surl = j.get("serverUrl")
    if not sid or not surl:
        raise RuntimeError(f"v2 login response missing icSessionId/serverUrl: {j}")

    with _env_lock:
        env = _read_env()
        env["IDMC_SESSION_ID"] = sid
        env["IDMC_SERVER_URL"] = surl
        _write_env(env)
    log.info("minted fresh v2 session (%s…)", sid[:8])
    return sid, surl


def _current_session() -> str:
    sid = _read_env().get("IDMC_SESSION_ID")
    if sid:
        return sid
    return _login_v2()[0]


def _request(method: str, url: str, **kw) -> httpx.Response:
    """HTTP wrapper that retries once on 401 after refreshing the session.

    Uses IDS-SESSION-ID header (FRS, rule-service, MCP servers).
    """
    headers = dict(kw.pop("headers", {}) or {})
    headers["IDS-SESSION-ID"] = _current_session()
    headers.setdefault("Accept", "application/json")
    if "json" in kw or "data" in kw:
        headers.setdefault("Content-Type", "application/json")

    timeout = kw.pop("timeout", 120)
    r = httpx.request(method, url, headers=headers, timeout=timeout, **kw)
    if r.status_code in (401, 503):
        log.info("HTTP %d from %s — refreshing session and retrying", r.status_code, url)
        sid, _ = _login_v2()
        headers["IDS-SESSION-ID"] = sid
        r = httpx.request(method, url, headers=headers, timeout=timeout, **kw)
    return r


def _login_v3() -> tuple[str, str]:
    """Mint a fresh v3 session. Returns (session_id, base_api_url)."""
    env = _read_env()
    user = env.get("IDMC_USER")
    pw   = env.get("IDMC_PASS")
    host = env.get("IDMC_LOGIN_HOST", "dmp-us.informaticacloud.com")
    if not user or not pw:
        raise RuntimeError("IDMC_USER and IDMC_PASS must be set in .env")

    url = f"https://{host}/saas/public/core/v3/login"
    r = httpx.post(url, json={"username": user, "password": pw},
                   headers={"Accept": "application/json", "Content-Type": "application/json"},
                   timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"v3 login HTTP {r.status_code}: {r.text[:300]}")
    j = r.json() or {}
    # v3 returns session id either in header or in userInfo.sessionId
    sid = (r.headers.get("INFA-SESSION-ID")
           or (j.get("userInfo") or {}).get("sessionId")
           or j.get("sessionId"))
    products = j.get("products") or []
    base = (products[0].get("baseApiUrl") if products else None) or env.get("IDMC_SERVER_URL")
    if not sid or not base:
        raise RuntimeError(f"v3 login missing session id or baseApiUrl: {j}")

    with _env_lock:
        env = _read_env()
        env["IDMC_V3_SESSION_ID"] = sid
        env["IDMC_V3_BASE_URL"]   = base
        _write_env(env)
    log.info("minted fresh v3 session (%s…)", sid[:8])
    return sid, base


def _request_v3(method: str, path_or_url: str, **kw) -> httpx.Response:
    """v3 platform API call using INFA-SESSION-ID header.

    `path_or_url` can be either a full URL or a path starting with `/public/...`
    (joined to IDMC_V3_BASE_URL).
    """
    env = _read_env()
    sid  = env.get("IDMC_V3_SESSION_ID") or _login_v3()[0]
    base = env.get("IDMC_V3_BASE_URL")   or _login_v3()[1]

    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        url = path_or_url
    else:
        url = f"{base.rstrip('/')}/{path_or_url.lstrip('/')}"

    headers = dict(kw.pop("headers", {}) or {})
    headers["INFA-SESSION-ID"] = sid
    headers.setdefault("Accept", "application/json")
    if "json" in kw or ("data" in kw and not isinstance(kw.get("data"), (bytes, bytearray))):
        headers.setdefault("Content-Type", "application/json")

    r = httpx.request(method, url, headers=headers, timeout=120, **kw)
    if r.status_code == 401:
        log.info("v3 HTTP 401 — refreshing v3 session and retrying")
        sid, _ = _login_v3()
        headers["INFA-SESSION-ID"] = sid
        r = httpx.request(method, url, headers=headers, timeout=120, **kw)
    return r


def _request_v2(method: str, path_or_url: str, **kw) -> httpx.Response:
    """v2 IICS API call using icSessionId header.

    `path_or_url` can be either a full URL or a path starting with `/api/v2/...`
    (it'll be joined to IDMC_SERVER_URL).
    """
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        url = path_or_url
    else:
        env = _read_env()
        base = env.get("IDMC_SERVER_URL") or _login_v2()[1]
        url = f"{base.rstrip('/')}/{path_or_url.lstrip('/')}"

    headers = dict(kw.pop("headers", {}) or {})
    headers["icSessionId"] = _current_session()
    headers.setdefault("Accept", "application/json")
    if "json" in kw or "data" in kw:
        headers.setdefault("Content-Type", "application/json")

    r = httpx.request(method, url, headers=headers, timeout=60, **kw)
    if r.status_code == 401:
        log.info("v2 HTTP 401 — refreshing session and retrying")
        sid, _ = _login_v2()
        headers["icSessionId"] = sid
        r = httpx.request(method, url, headers=headers, timeout=60, **kw)
    return r


_v3_to_v2_mapping_cache: dict[str, str] = {}


def _resolve_v2_mapping_id(maybe_v3_id: str) -> str:
    """Return a v2-compatible mapping id for the v2 mtTask endpoint.

    v2 ids are fixed-width (`010<base36>` × 20). v3 catalog GUIDs are 22-char
    base62 (e.g. ``7YeAon6HW06kWgjljr5sii``). The v2 endpoint rejects v3 ids
    with a 403 "is not a valid argument" error, so callers that hand us a
    v3 id need translation.

    Translation: list /api/v2/mapping and match on the ``assetFrsGuid`` field,
    which each v2 mapping carries pointing back at its v3 catalog GUID. The
    matching item's ``id`` is the v2 native form. Results are cached
    per-process so we only pay the list cost once per template.

    Pass-through behavior: if the input already looks v2-shaped, or if it
    doesn't match either format (e.g. a malformed value), we hand it back
    unchanged and let the API surface its own error.
    """
    if re.match(r"^010[A-Z0-9]{17}$", maybe_v3_id):
        return maybe_v3_id
    if not re.match(r"^[A-Za-z0-9]{22}$", maybe_v3_id):
        return maybe_v3_id
    if maybe_v3_id in _v3_to_v2_mapping_cache:
        return _v3_to_v2_mapping_cache[maybe_v3_id]

    r = _request_v2("GET", "/api/v2/mapping")
    if r.status_code != 200:
        raise RuntimeError(f"v3→v2 mapping lookup HTTP {r.status_code}: {r.text[:300]}")
    raw = r.json()
    items = raw if isinstance(raw, list) else (raw.get("value") or [])
    for it in items:
        if it.get("assetFrsGuid") == maybe_v3_id:
            v2_id = it.get("id")
            if not v2_id:
                raise RuntimeError(f"v2 mapping for v3 {maybe_v3_id} missing id field")
            _v3_to_v2_mapping_cache[maybe_v3_id] = v2_id
            log.info("translated v3 mapping id %s → v2 %s", maybe_v3_id, v2_id)
            return v2_id
    # Not found in v2 mapping list — may be an FRS template ID or a non-standard
    # v2 ID format. Pass through and let the CDI API surface its own error.
    log.warning("v3 mapping id %s not found in /api/v2/mapping (%d scanned); passing through as-is",
                maybe_v3_id, len(items))
    return maybe_v3_id


def _run_mapping_task(task_id: str, task_type: str = "MTT") -> dict[str, Any]:
    """Start a CDI mapping task (or other v2 task) via POST /api/v2/job.

    Per IICS v2 docs, the runtime object is the trigger for MTT execution
    even when no parameter file is supplied. Returns the v2 job response
    including the integer runId, which job_management MCP / monitor URLs
    take to surface the run.

    Args:
      task_id:   v2 task id (mtTask id from create_mapping_task).
      task_type: v2 task type code. Default "MTT". Other values:
                 DMASK, DRS, DSS, PCS, WORKFLOW.

    Returns: {runId, taskId, taskType, taskName, runInParallel, http_status}.
    """
    body = {"@type": "job", "taskId": task_id, "taskType": task_type,
            "runtime": {"@type": "mtTaskRuntime"}}
    r = _request_v2("POST", "/api/v2/job", json=body)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"_run_mapping_task HTTP {r.status_code}: {r.text[:500]}")
    j = r.json() if r.text else {}
    return {
        "runId":         j.get("runId"),
        "taskId":        j.get("taskId"),
        "taskType":      j.get("taskType"),
        "taskName":      j.get("taskName"),
        "runInParallel": j.get("runInParallel"),
        "http_status":   r.status_code,
    }


def _jwt_age_seconds() -> int | None:
    """Seconds since the cached JWT was minted, or None if no timestamp."""
    minted = _read_env().get("IDMC_JWT_MINTED_AT")
    if not minted:
        return None
    try:
        return int(time.time() - float(minted))
    except ValueError:
        return None


def _mint_jwt(force: bool = False) -> str | None:
    """Mint a Bearer JWT via identity-service/jwt/Token.

    Caches into .env (IDMC_JWT, IDMC_JWT_MINTED_AT). Returns the cached JWT
    when it's still within TTL unless force=True. Returns None if minting
    fails (caller should fall back to whatever they had).
    """
    env = _read_env()
    cached = env.get("IDMC_JWT")
    age = _jwt_age_seconds()
    if cached and age is not None and age < JWT_TTL_SECONDS and not force:
        return cached

    nonce = uuid.uuid4().hex.upper()
    url = f"{JWT_MINT_URL}?client_id={JWT_CLIENT_ID}&nonce={nonce}"
    try:
        r = httpx.get(
            url,
            headers={"IDS-SESSION-ID": _current_session(), "Accept": "application/json"},
            timeout=30,
        )
    except httpx.HTTPError as e:  # noqa: BLE001
        log.warning("_mint_jwt: request failed: %s", e)
        return cached  # last-known JWT, if any

    if r.status_code == 401:
        # Stale session — refresh and try once more
        _login_v2()
        try:
            r = httpx.get(
                url,
                headers={"IDS-SESSION-ID": _current_session(), "Accept": "application/json"},
                timeout=30,
            )
        except httpx.HTTPError as e:  # noqa: BLE001
            log.warning("_mint_jwt: retry failed: %s", e)
            return cached

    if r.status_code != 200:
        log.warning("_mint_jwt: HTTP %s — %s", r.status_code, r.text[:200])
        return cached

    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        log.warning("_mint_jwt: non-JSON body")
        return cached

    new_jwt = None
    for k in ("jwt_token", "jwt", "token", "idsToken", "id_token", "access_token"):
        v = j.get(k) if isinstance(j, dict) else None
        if isinstance(v, str) and v.startswith("eyJ"):
            new_jwt = v
            break
    if not new_jwt:
        log.warning("_mint_jwt: no JWT found in response: keys=%s",
                    list(j.keys()) if isinstance(j, dict) else type(j).__name__)
        return cached

    with _env_lock:
        env = _read_env()
        env["IDMC_JWT"] = new_jwt
        env["IDMC_JWT_MINTED_AT"] = str(int(time.time()))
        _write_env(env)
    log.info("minted fresh IDMC_JWT (%s..., %d chars)", new_jwt[:16], len(new_jwt))
    return new_jwt


def _current_jwt() -> str | None:
    """Return a valid IDMC JWT, minting if absent or stale."""
    return _mint_jwt(force=False)


def _request_cdgc(method: str, url: str, **kw) -> httpx.Response:
    """CDGC API call. Uses Bearer JWT (from IDMC_JWT env) + IDS-SESSION-ID +
    X-INFA-ORG-ID. On 401, tries to renew the JWT via Renew2 and retries once.

    Caller must have a valid initial JWT in .env (IDMC_JWT=eyJ...). Paste the
    IDS_TOKEN cookie value from the IDMC browser session to bootstrap it.
    """
    headers = dict(kw.pop("headers", {}) or {})
    jwt = _current_jwt()
    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"
    headers["IDS-SESSION-ID"] = _current_session()
    headers["X-INFA-ORG-ID"]  = DEFAULT_ORG_ID
    headers.setdefault("Accept", "application/json")
    if "json" in kw or "data" in kw:
        headers.setdefault("Content-Type", "application/json")

    r = httpx.request(method, url, headers=headers, timeout=60, **kw)
    if r.status_code == 401:
        log.info("CDGC HTTP 401 — force-minting fresh JWT and retrying")
        new_jwt = _mint_jwt(force=True)
        if new_jwt:
            headers["Authorization"] = f"Bearer {new_jwt}"
        sid, _ = _login_v2()
        headers["IDS-SESSION-ID"] = sid
        r = httpx.request(method, url, headers=headers, timeout=60, **kw)
    return r


# ---------------------------------------------------------------------------
# Rule-model construction
# ---------------------------------------------------------------------------
def _default_null_check_rule_model(field_name: str, dimension: str) -> dict[str, Any]:
    """Built-in null-check rule (mirrors create-rule.sh's inline default)."""
    input_uuid = str(uuid.uuid4())
    output_uuid = str(uuid.uuid4())
    return {
        "$$class": "com.informatica.dq.rulebuilder.RuleDefinition",
        "$$IID": "WILL_BE_REPLACED",
        "$$id":  "WILL_BE_REPLACED",
        "name":  "WILL_BE_REPLACED",
        "description": "WILL_BE_REPLACED",
        "outsideValidityMessage": "undefined",
        "validFromDate": "-3600000",
        "validToDate":   "-3600000",
        "$$aggregator": {
            "$$lockedOn": 0, "$$version": 1494513349121,
            "##IID": "U:IGWzIjZXEeefFIPeKxPNig",
            "name": "DATES", "$$lockedBy": "",
            "$$class": 789, "$$property": "contents",
        },
        "tags": [],
        "options": [
            {"$$class": "com.informatica.dq.rulebuilder.StringOption", "name": "DEFAULT_STRING_PRECISION",  "optionValue": "100"},
            {"$$class": "com.informatica.dq.rulebuilder.StringOption", "name": "DEFAULT_DECIMAL_PRECISION", "optionValue": "10"},
            {"$$class": "com.informatica.dq.rulebuilder.StringOption", "name": "DEFAULT_DECIMAL_SCALE",     "optionValue": "4"},
            {"$$class": "com.informatica.dq.rulebuilder.StringOption", "name": "DIMENSION",                 "optionValue": dimension},
            {"$$class": "com.informatica.dq.rulebuilder.StringOption", "name": "EXCEPTION",                 "optionValue": "false"},
        ],
        "fields": [{
            "$$class": "com.informatica.dq.rulebuilder.Field",
            "$$id": "5",
            "$$externalID": input_uuid,
            "precision": "50", "scale": "0",
            "name": field_name,
            "$type": {
                "##SID":  "smd:com.informatica.metadata.seed.platform.Platform.typesystem/string",
                "$$class": "com.informatica.metadata.common.typesystem.DataType",
            },
            "description": "",
        }],
        "outputFields": [],
        "testData": [],
        "topRuleFamily": {
            "$$class": "com.informatica.dq.rulebuilder.RuleFamily",
            "name": "PrimaryRuleSet", "description": "",
            "$$id": "6", "$$externalID": output_uuid,
            "outputs": [], "outputLinks": [],
            "statements": [
                {
                    "$$class": "com.informatica.dq.rulebuilder.Statement",
                    "action": {
                        "$$class": "com.informatica.dq.rulebuilder.Operation",
                        "$$id": "7", "name": "SetField", "description": "", "type": "Valid",
                        "options": [{"$$class": "com.informatica.dq.rulebuilder.StringOption", "name": "Value", "optionValue": "VALID"}],
                        "inputs": [], "suboperations": [], "outputs": [],
                    },
                    "condition": {
                        "$$class": "com.informatica.dq.rulebuilder.Operation",
                        "$$id": "8", "name": "NotEquals", "description": "", "type": "NotEquals",
                        "options": [{"$$class": "com.informatica.dq.rulebuilder.StringOption", "name": "useNull", "optionValue": "true"}],
                        "inputs": [{"name": field_name, "$$class": "com.informatica.dq.rulebuilder.Field", "##id": "5", "##externalID": "undefined"}],
                        "suboperations": [], "outputs": [],
                    },
                },
                {
                    "$$class": "com.informatica.dq.rulebuilder.Statement",
                    "action":    {"$$class": "com.informatica.dq.rulebuilder.Operation", "$$id": "9",  "name": "", "description": "",                          "options": [], "inputs": [], "suboperations": [], "outputs": []},
                    "condition": {"$$class": "com.informatica.dq.rulebuilder.Operation", "$$id": "10", "name": "", "description": "", "type": "DefaultValue", "options": [], "inputs": [], "suboperations": [], "outputs": []},
                },
            ],
            "ruleFamilies": [],
            "fields": [{"name": field_name, "$$class": "com.informatica.dq.rulebuilder.Field", "##id": "5", "##externalID": input_uuid}],
        },
    }


def _collect_external_uuids(obj: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "$$externalID" and isinstance(v, str) and v not in ("", "undefined"):
                out.add(v)
            out |= _collect_external_uuids(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _collect_external_uuids(v)
    return out


def _load_template(path: str, auto_uuid: bool) -> dict[str, Any]:
    p = Path(path)
    if not p.is_absolute():
        p = REPO_ROOT / path  # rule templates (e.g. examples/null-check.json) resolve from repo root
    text = p.read_text()
    if auto_uuid:
        # Parse, find externalIDs, rewrite via textual substitution so that
        # both `$$externalID` and `##externalID` references update together.
        obj = json.loads(text)
        for old in _collect_external_uuids(obj):
            text = text.replace(old, str(uuid.uuid4()))
    return json.loads(text)


def _build_document_blob(rule_model: dict[str, Any]) -> dict[str, Any]:
    """Derive documentBlob (inputFields, outputFields, ruleModel-as-string)."""
    def field_type(f: dict[str, Any]) -> str:
        sid = (f.get("$type") or {}).get("##SID") or ""
        tail = sid.rsplit("/", 1)[-1]
        return tail or "string"

    def to_int(v: Any, default: int) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    input_fields = [
        {
            "name": f.get("name"),
            "type": field_type(f),
            "precision": to_int(f.get("precision"), 50),
            "scale":     to_int(f.get("scale"),     0),
            "id":   f.get("$$externalID"),
        }
        for f in (rule_model.get("fields") or [])
    ]
    trf = rule_model.get("topRuleFamily") or {}
    output_fields = [{
        "name": trf.get("name") or "PrimaryRuleSet",
        "id":   trf.get("$$externalID"),
        "type": "string",
        "precision": "100",
        "scale": 0,
    }]
    return {
        "inputFields":  input_fields,
        "outputFields": output_fields,
        "ruleModel":    json.dumps(rule_model, separators=(",", ":")),
    }


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="governance_engine",
    instructions=(
        "IDMC CDQ governance tools. Use create_dq_rules to create a new rule "
        "specification (null-check by default; pass rule_template for arbitrary "
        "logic). Use list_rule_specifications to enumerate rules."
    ),
)


@mcp.tool()
def create_dq_rules(
    rule_name: str,
    description: str = "Created by governance_engine",
    field_name: str = "Input",
    dimension: str = "COMPLETENESS",
    rule_template: str | None = None,
    auto_uuid: bool = True,
    space_id: str | None = None,
    space_name: str | None = None,
    project_id: str | None = None,
    project_name: str | None = None,
    folder_id: str | None = None,
    folder_name: str | None = None,
) -> dict[str, Any]:
    """Create a new CDQ rule specification end-to-end.

    Flow: POST /frs/v1/Folders('{folder_id}')/Documents (mint id) →
    PATCH /frs/v1/Documents('id') with nativeData.documentBlob containing
    the ruleModel.

    Args:
      rule_name: Display name for the new rule.
      description: Free-text description.
      field_name: Single-input field name. Used only when rule_template is None.
      dimension: Quality dimension (e.g. COMPLETENESS, VALIDITY). When a
                 template is provided, the template's options[DIMENSION] wins.
      rule_template: Path to a ruleModel JSON template (see examples/null-check.json).
                     If omitted, a built-in null-check is used.
      auto_uuid: When using a template, regenerate $$externalID UUIDs so
                 repeated creates don't share field UUIDs across rules.
      space_id, space_name, project_id, project_name: Override the default parent.
      folder_id, folder_name: Target folder within the project (required for write
                 access on this tenant — use CDQ_FOLDER_ID env var to set default).

    Returns: {id, name, dimension, documentState, ui_url, parent}.
    """
    space_id     = space_id    or DEFAULT_SPACE_ID
    space_name   = space_name  or DEFAULT_SPACE_NAME
    project_id   = project_id  or DEFAULT_PROJECT_ID
    project_name = project_name or DEFAULT_PROJECT_NAME
    folder_id    = folder_id   or DEFAULT_FOLDER_ID
    folder_name  = folder_name or DEFAULT_FOLDER_NAME

    if rule_template:
        rule_model = _load_template(rule_template, auto_uuid=auto_uuid)
        rule_model["name"] = rule_name
        rule_model["description"] = description
        for opt in rule_model.get("options", []):
            if opt.get("name") == "DIMENSION":
                dimension = opt.get("optionValue") or dimension
                break
    else:
        rule_model = _default_null_check_rule_model(field_name, dimension)
        rule_model["name"] = rule_name
        rule_model["description"] = description

    # Step 1: POST FRS metadata shell
    doc_body = {
        "documentType": "RULE_SPECIFICATION",
        "name": rule_name,
        "description": description,
        "customAttributes": {
            "stringAttrs": [
                {"name": "DIMENSION",                   "value": dimension},
                {"name": "EXCEPTION",                   "value": "false"},
                {"name": "ReferencedPublishingAllowed", "value": "true"},
            ],
            "numberAttrs": [], "dateAttrs": [],
        },
    }
    r = _request("POST", f"{FRS_V1}/Folders('{folder_id}')/Documents", json=doc_body)
    if r.status_code != 201:
        raise RuntimeError(f"POST Documents failed (HTTP {r.status_code}): {r.text[:400]}")
    new_id = r.json().get("id")
    if not new_id:
        raise RuntimeError(f"POST Documents response missing id: {r.text[:400]}")
    log.info("created FRS shell id=%s", new_id)

    # Substitute id into ruleModel
    rule_model["$$IID"] = new_id
    rule_model["$$id"]  = new_id

    # Step 2: PATCH with rule body
    blob = _build_document_blob(rule_model)
    patch_body = {
        "name": rule_name,
        "description": description,
        "documentType": "RULE_SPECIFICATION",
        "nativeData": {"documentBlob": json.dumps(blob, separators=(",", ":"))},
        "docRef": {"docRefIds": []},
        "customAttributes": {"stringAttrs": [
            {"name": "ReferencedPublishingAllowed", "value": "true"},
            {"name": "DIMENSION",                   "value": dimension},
            {"name": "EXCEPTION",                   "value": "false"},
        ]},
        "documentState": "VALID",
        "id": new_id,
    }
    r = _request("PATCH", f"{FRS_V1}/Documents('{new_id}')", json=patch_body)
    if r.status_code not in (200, 204):
        # Attempt cleanup of the orphan shell so a failed create doesn't leak
        try:
            _request("DELETE", f"{FRS_API}/Documents('{new_id}')")
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"PATCH failed (HTTP {r.status_code}): {r.text[:400]}")

    # Verify
    r = _request("GET", f"{FRS_API}/Documents('{new_id}')")
    doc_state = (r.json() if r.status_code == 200 else {}).get("documentState", "?")

    return {
        "id": new_id,
        "name": rule_name,
        "dimension": dimension,
        "documentState": doc_state,
        "ui_url": f"{UI_BASE}/{new_id}",
        "parent": {"space": space_name, "project": project_name},
    }


@mcp.tool()
def list_rule_specifications(
    top: int = 50,
    name_filter: str | None = None,
) -> dict[str, Any]:
    """List CDQ rule specifications in the tenant.

    Args:
      top: Max rules to return (server-side $top).
      name_filter: Optional case-insensitive substring filter applied client-side.

    Returns: {count, rules:[{id, name, description, createdBy, lastUpdatedTime, dimension}]}.
    """
    # FRS rejects '+'-encoded spaces; force %20 via safe="".
    qs = "$filter=" + quote("documentType eq 'RULE_SPECIFICATION'", safe="")
    if top:
        qs += f"&$top={int(top)}"
    url = f"{FRS_API}/Documents?{qs}"
    r = _request("GET", url)
    if r.status_code != 200:
        raise RuntimeError(f"List failed (HTTP {r.status_code}): {r.text[:400]}")

    items = (r.json() or {}).get("value", []) or []
    if name_filter:
        nf = name_filter.lower()
        items = [it for it in items if nf in (it.get("name") or "").lower()]

    def dim_of(it: dict[str, Any]) -> str | None:
        attrs = ((it.get("customAttributes") or {}).get("stringAttrs") or [])
        for a in attrs:
            if a.get("name") == "DIMENSION":
                return a.get("value")
        return None

    rules = [{
        "id": it.get("id"),
        "name": it.get("name"),
        "description": it.get("description"),
        "createdBy": it.get("createdBy"),
        "lastUpdatedTime": it.get("lastUpdatedTime"),
        "dimension": dim_of(it),
    } for it in items]

    return {"count": len(rules), "rules": rules}


@mcp.tool()
def validate_rule(
    rule_template: str | None = None,
    rule_model: dict[str, Any] | None = None,
    field_name: str = "Input",
    dimension: str = "COMPLETENESS",
) -> dict[str, Any]:
    """Validate a CDQ rule model against the rule-service before creation.

    Sends the ruleModel to POST /rule-service/api/v1/validateRule and returns
    the validation outputs. Useful as a pre-flight check before
    `create_dq_rules` — catches structural problems (missing fields, bad
    operations, type mismatches) without writing anything to FRS.

    Args:
      rule_template: Path to a ruleModel JSON template (e.g.
                     'examples/null-check.json'). Mutually exclusive with
                     rule_model.
      rule_model:    A ruleModel dict (the parsed RuleDefinition object).
                     Use this when validating an in-memory model.
      field_name:    Input field name (only used when neither template nor
                     model is given — falls back to the built-in null-check).
      dimension:     DQ dimension for the fallback model.

    Returns: {valid, output_count, outputs, raw}.
      - valid:        True when the rule-service accepted the request and
                      did not return an `error` field. Empty `outputs` is
                      the normal "no problems" response shape.
      - output_count: Number of validation messages (problems) returned.
      - outputs:      Truncated output list (first 5 entries).
      - raw:          Full response top-level keys for debugging.
    """
    if rule_template and rule_model:
        raise ValueError("pass either rule_template or rule_model, not both")
    if rule_template:
        model = _load_template(rule_template, auto_uuid=False)
    elif rule_model:
        model = rule_model
    else:
        model = _default_null_check_rule_model(field_name, dimension)

    body = {"ruleModel": json.dumps(model, separators=(",", ":"))}
    r = _request("POST", f"{RS}/validateRule", json=body)
    if r.status_code != 200:
        raise RuntimeError(f"validate_rule HTTP {r.status_code}: {r.text[:400]}")
    j = r.json() if r.text else {}
    outputs = j.get("outputs") or []
    return {
        "valid":        j.get("error") is None,
        "output_count": len(outputs),
        "outputs":      outputs[:5],
        "raw":          {"name": j.get("name"), "description": j.get("description"), "error": j.get("error")},
    }


# ---------------------------------------------------------------------------
# v2 CDI tools
# ---------------------------------------------------------------------------
@mcp.tool()
def list_connections(top: int = 50, type_filter: str | None = None) -> dict[str, Any]:
    """List connections defined in the IDMC org.

    Args:
      top: Maximum number to return (client-side trim; the v2 endpoint itself
           returns all connections).
      type_filter: Optional connection type to filter on (e.g. "Oracle",
                   "Snowflake", "TOOLKIT_CCI"). Case-sensitive.

    Returns: {count, total, connections:[{id, name, type, updateTime}]}.
    """
    r = _request_v2("GET", "/api/v2/connection")
    if r.status_code != 200:
        raise RuntimeError(f"list_connections HTTP {r.status_code}: {r.text[:400]}")
    raw = r.json()
    items = raw if isinstance(raw, list) else (raw.get("value") or [])
    if type_filter:
        items = [c for c in items
                 if (c.get("type") == type_filter or c.get("connType") == type_filter)]
    sliced = items[:int(top)] if top else items
    return {
        "count": len(sliced),
        "total": len(items),
        "connections": [{
            "id":         c.get("id"),
            "name":       c.get("name"),
            "type":       c.get("type") or c.get("connType"),
            "updateTime": c.get("updateTime") or c.get("lastUpdatedTime"),
        } for c in sliced],
    }


@mcp.tool()
def list_mapping_tasks(top: int = 50, name_filter: str | None = None) -> dict[str, Any]:
    """List mapping tasks (mtTask) in the IDMC org.

    Args:
      top: Maximum number to return (client-side trim).
      name_filter: Optional case-insensitive substring filter on task name.

    Returns: {count, total, tasks:[{id, name, mappingId, runtimeEnvironmentId,
             scheduleId, updateTime}]}.
    """
    r = _request_v2("GET", "/api/v2/mttask")
    if r.status_code != 200:
        raise RuntimeError(f"list_mapping_tasks HTTP {r.status_code}: {r.text[:400]}")
    raw = r.json()
    items = raw if isinstance(raw, list) else (raw.get("value") or [])
    if name_filter:
        nf = name_filter.lower()
        items = [t for t in items if nf in (t.get("name") or "").lower()]
    sliced = items[:int(top)] if top else items
    return {
        "count": len(sliced),
        "total": len(items),
        "tasks": [{
            "id":                   t.get("id"),
            "name":                 t.get("name"),
            "mappingId":            t.get("mappingId"),
            "runtimeEnvironmentId": t.get("runtimeEnvironmentId"),
            "scheduleId":           t.get("scheduleId"),
            "updateTime":           t.get("updateTime"),
        } for t in sliced],
    }


@mcp.tool()
def create_mapping_task(
    name: str,
    runtime_environment_id: str,
    mapping_id: str,
    description: str = "",
    container_id: str | None = None,
    schedule_id: str | None = None,
    mapping_parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a CDI mapping task (binds a mapping to a runtime environment).

    Args:
      name: Task display name.
      runtime_environment_id: ID of the Secure Agent / serverless runtime to
        execute the task on.
      mapping_id: ID of the existing mapping the task wraps.
      description: Optional description.
      container_id: Optional project/folder id. If omitted, Default folder.
      schedule_id: Optional schedule to attach immediately.
      mapping_parameters: Optional {param_name: value} dict. Bound as
        `parameters:[{name,value},...]` on the v2 mtTask body. Use when the
        wrapped mapping exposes runtime parameters (connections, table
        names, file paths).

    Returns: {id, name, mappingId, runtimeEnvironmentId, containerId,
              parameter_count}.
    """
    body: dict[str, Any] = {
        "@type":                "mtTask",
        "name":                 name,
        "runtimeEnvironmentId": runtime_environment_id,
        "mappingId":            mapping_id,
        "description":          description or "",
    }
    if container_id:
        body["containerId"] = container_id
    if schedule_id:
        body["scheduleId"] = schedule_id
    if mapping_parameters:
        body["parameters"] = [{"name": k, "value": v} for k, v in mapping_parameters.items()]

    r = _request_v2("POST", "/api/v2/mttask/", json=body)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"create_mapping_task HTTP {r.status_code}: {r.text[:500]}")
    j = r.json() if r.text else {}
    return {
        "id":                   j.get("id"),
        "name":                 j.get("name"),
        "mappingId":            j.get("mappingId"),
        "runtimeEnvironmentId": j.get("runtimeEnvironmentId"),
        "containerId":          j.get("containerId"),
        "parameter_count":      len(j.get("parameters") or body.get("parameters") or []),
    }


@mcp.tool()
def run_task(task_id: str, task_type: str = "MTT") -> dict[str, Any]:
    """Trigger a CDI task immediately via POST /api/v2/job.

    Posts {"@type":"job", "taskId":..., "taskType":..., "runtime":
    {"@type":"mtTaskRuntime"}} (the runtime envelope is required for
    MTT/most v2 task types — without it the engine treats the run as
    parameterless even when the task has bindings). Auth: icSessionId.

    Args:
      task_id:   v2 task id (mtTask id from create_mapping_task or
                 generate_dq_mapping_task).
      task_type: v2 task type code. Default "MTT" (mapping task).
                 Other valid values: DMASK, DRS, DSS, PCS, WORKFLOW.

    Returns: {runId, taskId, taskType, taskName, runInParallel,
              http_status}. The integer runId is what get_job_status
              and the IDMC monitor URL take to surface the run.
    """
    return _run_mapping_task(task_id=task_id, task_type=task_type)


# State codes returned by /api/v2/activity. Documented in the IICS REST
# API Reference under Activity log resource.
_JOB_STATE_LABELS = {
    0: "RUNNING",
    1: "SUCCESS",
    2: "FAILED",
    3: "WARNING",
    4: "STOPPED",
    5: "QUEUED",
    6: "STARTING",
}


@mcp.tool()
def get_job_status(run_id: int, task_id: str | None = None) -> dict[str, Any]:
    """Look up the activity entry for a run.

    Hits two v2 endpoints in fallback order:
      1. GET /api/v2/activity/activityMonitor?runId=<run_id>
         — returns entries for jobs that are CURRENTLY running.
      2. GET /api/v2/activity/activityLog?runId=<run_id>
         — returns the historical entry for a job that has completed
         (success / failed / stopped / warning).

    Both use the icSessionId header. When task_id is supplied, results
    are filtered client-side to entries whose objectId matches — useful
    when a single runId fans out across multiple sub-tasks (taskflows
    or parameter-set runs).

    Args:
      run_id:  The integer runId returned by run_task / POST /api/v2/job.
      task_id: Optional. Filter to entries whose objectId matches.

    Returns: {
      run_id, task_id_filter, source ("monitor" or "log"), entry_count,
      entries:[
        {id, name, type, run_id, state, state_label, started_by,
         start_time, end_time, success_source_rows, failed_source_rows,
         success_target_rows, failed_target_rows, error_message,
         object_id, object_name, run_context_type}
      ],
      first: <first entry summary> | null,
    }
    """
    def _fetch(path: str) -> list[dict[str, Any]]:
        r = _request_v2("GET", path)
        if r.status_code >= 400:
            raise RuntimeError(f"get_job_status {path} HTTP {r.status_code}: {r.text[:400]}")
        raw = r.json() if r.text else []
        if isinstance(raw, list):
            return raw
        return raw.get("value") or raw.get("entries") or []

    items = _fetch(f"/api/v2/activity/activityMonitor?runId={int(run_id)}")
    source = "monitor"
    if not items:
        # Fall back to historical log for completed jobs. activityLog
        # rejects runId-only queries with APP_13475 ("Task id cannot be
        # null when run id is provided"), so we must pass taskId as well.
        if task_id:
            items = _fetch(f"/api/v2/activity/activityLog?runId={int(run_id)}&taskId={task_id}")
            source = "log"
        else:
            source = "monitor:empty (pass task_id to query historical activityLog)"

    def coerce(e: dict[str, Any]) -> dict[str, Any]:
        state = e.get("state")
        try:
            state_int = int(state) if state is not None else None
        except (TypeError, ValueError):
            state_int = None
        return {
            "id":                  e.get("id"),
            "name":                e.get("objectName") or e.get("name"),
            "type":                e.get("type"),
            "run_id":              e.get("runId"),
            "state":               state_int,
            "state_label":         _JOB_STATE_LABELS.get(state_int, str(state)),
            "started_by":          e.get("startedBy"),
            "start_time":          e.get("startTime") or e.get("startTimeUtc"),
            "end_time":            e.get("endTime")   or e.get("endTimeUtc"),
            "success_source_rows": e.get("successSourceRows"),
            "failed_source_rows":  e.get("failedSourceRows"),
            "success_target_rows": e.get("successTargetRows"),
            "failed_target_rows":  e.get("failedTargetRows"),
            "error_message":       e.get("errorMsg") or e.get("errorMessage"),
            "object_id":           e.get("objectId"),
            "object_name":         e.get("objectName"),
            "run_context_type":    e.get("runContextType"),
        }

    entries = [coerce(e) for e in items if isinstance(e, dict)]
    if task_id:
        entries = [e for e in entries if e.get("object_id") == task_id]
    return {
        "run_id":          run_id,
        "task_id_filter":  task_id,
        "source":          source,
        "entry_count":     len(entries),
        "entries":         entries,
        "first":           entries[0] if entries else None,
    }


# M_DQ_Generic — our hand-built DQ-execution template mapping. Replaces
# the IDMC-shipped PreviewMapping_RULE_SPECIFICATION (id
# 010YK21700000000006E) which validates via API but fails at runtime
# with "No fields available for the target". M_DQ_Generic has a Source
# → Rule Specification → Target topology and validates end-to-end.
#
# Per the v2 mapping enumeration, three parameters are bound at task-
# create time:
#   $Source$           EXTENDED_SOURCE  source connection + table
#   $Target$           TARGET           bad-records target connection + table
#   $Input_Field_Map$  STRING           "<source_col>=<rule_input_port>"
# The Rule Specification transformation INSIDE the mapping references a
# specific rule at design time — i.e. one M_DQ_Generic per rule. To check
# additional rules, build another template in the UI and pass its
# template_mapping_id. rule_spec_id flows through this tool only as audit
# / task-naming metadata; it is not bound to any mtTaskParameter.
#
# DEFAULT_DQ_TEMPLATE_MAPPING_ID is the v3/FRS GUID; the function auto-
# translates to v2 native form via _resolve_v2_mapping_id().
DEFAULT_DQ_TEMPLATE_MAPPING_ID = os.getenv("IDMC_DQ_TEMPLATE_MAPPING_ID", "")


@mcp.tool()
def generate_dq_mapping_task(
    source_connection_id: str,
    source_table: str,
    target_connection_id: str,
    target_table: str,
    runtime_environment_id: str,
    input_field_mapping: str = "",
    template_mapping_id: str = DEFAULT_DQ_TEMPLATE_MAPPING_ID,
    rule_spec_id: str | None = None,
    task_name: str | None = None,
    description: str = "",
    container_id: str | None = None,
    schedule_id: str | None = None,
    extra_parameters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Instantiate M_DQ_Generic as a DQ-execution mapping task.

    Default template is M_DQ_Generic (FRS id ``0DMDulmZrQvbnss7chDbOc``).
    Per the v2 mapping enumeration its parameters are:

    +-----------------------+--------------------+--------------------------+
    | Parameter             | Type               | Bound to                 |
    +=======================+====================+==========================+
    | ``$Source$``          | EXTENDED_SOURCE    | source_connection_id +   |
    |                       |                    | source_table             |
    +-----------------------+--------------------+--------------------------+
    | ``$Target$``          | TARGET             | target_connection_id +   |
    |                       |                    | target_table             |
    +-----------------------+--------------------+--------------------------+
    | ``$Input_Field_Map$`` | STRING (Fieldmap)  | input_field_mapping      |
    +-----------------------+--------------------+--------------------------+

    The Rule Specification transformation inside the mapping references a
    specific rule at design time, so the rule itself is *baked in* per
    template — i.e. one M_DQ_Generic per rule. To check additional rules,
    build another template in the UI and pass its ``template_mapping_id``.

    ``rule_spec_id`` flows through this tool only as audit metadata used
    in the auto-generated task name and description; it is NOT bound to
    any mtTaskParameter.

    Args:
      source_connection_id:   v2 connection id of the source.
      source_table:           Source table / object name.
      target_connection_id:   v2 connection id for the bad-records target.
      target_table:           Target table name. Created on first run if
                              the template's $Target$ has newObject=true
                              (the M_DQ_Generic default).
      input_field_mapping:    Value bound to $Input_Field_Map$. Format is
                              ``<source_column>=<rule_input_port>``.
                              Example: ``"customer_name=Input"`` maps the
                              source's customer_name column to a rule
                              whose input port is named ``Input``. Cannot
                              be empty — v2 rejects with APP_13506.
      runtime_environment_id: v2 runtime env id (Secure Agent / serverless).
      template_mapping_id:    Override the default M_DQ_Generic template.
                              Accepts v2 (``010...``) or v3 (FRS GUID) ids
                              — auto-translated via _resolve_v2_mapping_id().
      rule_spec_id:           Optional — used only for task name + audit
                              description. Not bound to any parameter.
      task_name:              Override the auto-generated task name.
      description:            Override the default description.
      container_id, schedule_id: Pass-throughs to the mtTask body.
      extra_parameters:       Raw mtTaskParameter dicts for template
                              variants that need additional bindings.

    Returns: {id, name, mappingId, runtimeEnvironmentId, containerId,
              parameter_names}.
    """
    # v2 mtTask only accepts v2-native mapping ids. Translate transparently
    # when given a v3/FRS GUID so the same template_mapping_id works
    # whether sourced from /api/v2/mapping or /public/core/v3/objects.
    template_mapping_id = _resolve_v2_mapping_id(template_mapping_id)

    # Introspect the mapping to learn which parameters it actually exposes.
    # Templates evolve — M_DQ_Generic has been republished with different
    # parameter sets, and the v2 endpoint rejects unknown bindings with
    # APP_13508 ("parameter $X$ … is not defined in the template"). By
    # building only the params the mapping declares, the tool stays
    # compatible with any (re-)publish of M_DQ_Generic without code edits.
    mr = _request_v2("GET", f"/api/v2/mapping/{template_mapping_id}")
    if mr.status_code != 200:
        # A 403 REPO_36004 ("error occurred while loading the mapping") almost
        # always means the configured template id is stale / unpublished /
        # invisible to this org — not a transient repo fault. Surface that
        # actionable hint instead of the raw internal-error text, which
        # otherwise reads as a server-side problem.
        hint = ""
        if mr.status_code in (403, 404) or "REPO_36004" in (mr.text or ""):
            hint = (
                f" — template mapping id '{template_mapping_id}' is not loadable in this org. "
                "It is likely stale, deleted, or unpublished. Set IDMC_DQ_TEMPLATE_MAPPING_ID "
                "to a currently-published M_DQ_Generic mapping (verify with probe_dq_template.py)."
            )
        raise RuntimeError(f"mapping lookup HTTP {mr.status_code}: {mr.text[:300]}{hint}")
    _raw = mr.json() or {}
    if isinstance(_raw, list):
        _raw = _raw[0] if _raw else {}
    template_params = _raw.get("parameters") or []
    template_param_names = {p.get("name") for p in template_params if isinstance(p, dict)}

    # mtTaskParameter shape mirrors what worked for PreviewMapping_RULE_
    # SPECIFICATION (the prior template). $Target$ needs operationType=Insert
    # so the runtime engine actually writes; without it the agent reported
    # "No fields available for the target".
    candidate_params: list[dict[str, Any]] = [
        {
            "@type":              "mtTaskParameter",
            "name":               "$Source$",
            "type":               "EXTENDED_SOURCE",
            "sourceConnectionId": source_connection_id,
            "sourceObject":       source_table,
        },
        {
            "@type":              "mtTaskParameter",
            "name":               "$Target$",
            "type":               "TARGET",
            "targetConnectionId": target_connection_id,
            "targetObject":       target_table,
            "operationType":      "Insert",
        },
    ]
    if input_field_mapping:
        candidate_params.append({
            "@type": "mtTaskParameter",
            "name":  "$Input_Field_Map$",
            "type":  "STRING",
            "text":  input_field_mapping,
        })

    parameters = [p for p in candidate_params if p["name"] in template_param_names]
    skipped = [p["name"] for p in candidate_params if p["name"] not in template_param_names]
    if skipped:
        log.info("generate_dq_mapping_task: template %s does not expose %s; not binding",
                 template_mapping_id, skipped)

    if extra_parameters:
        parameters.extend(extra_parameters)

    safe_table = re.sub(r"[^A-Za-z0-9_]+", "_", source_table)[:24]
    rule_tag   = (rule_spec_id[:8] + "_") if rule_spec_id else ""
    auto_name  = f"mt_dq_{rule_tag}{safe_table}_{int(time.time()) % 100000}"
    name       = task_name or auto_name

    auto_desc = (f"DQ execution via M_DQ_Generic on {source_table}"
                 f"{' (rule ' + rule_spec_id + ')' if rule_spec_id else ''}")

    body: dict[str, Any] = {
        "@type":                "mtTask",
        "name":                 name,
        "description":          description or auto_desc,
        "runtimeEnvironmentId": runtime_environment_id,
        "mappingId":            template_mapping_id,
        "parameters":           parameters,
    }
    if container_id: body["containerId"] = container_id
    if schedule_id:  body["scheduleId"]  = schedule_id

    r = _request_v2("POST", "/api/v2/mttask/", json=body)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"generate_dq_mapping_task HTTP {r.status_code}: {r.text[:600]}")
    j = r.json() if r.text else {}
    return {
        "id":                   j.get("id"),
        "name":                 j.get("name"),
        "mappingId":            j.get("mappingId"),
        "runtimeEnvironmentId": j.get("runtimeEnvironmentId"),
        "containerId":          j.get("containerId"),
        "parameter_names":      [p["name"] for p in parameters],
    }



@mcp.tool()
def create_schedule(
    name: str,
    start_time: str,
    start_time_utc: str,
    interval: str = "Daily",
    frequency: int | None = None,
    description: str = "",
    end_time: str | None = None,
    sun: bool = False, mon: bool = False, tue: bool = False, wed: bool = False,
    thu: bool = False, fri: bool = False, sat: bool = False,
    week_day: bool = False,
    day_of_month: int | None = None,
    week_of_month: str | None = None,
    day_of_week: str | None = None,
    range_start_time: str | None = None,
    range_end_time: str | None = None,
) -> dict[str, Any]:
    """Create a v2 schedule (cron-like recurrence) at /api/v2/schedule.

    Args:
      name: Schedule name (must be unique).
      start_time, start_time_utc: First-run timestamps (ISO 8601).
      interval: One of None, Minutely, Hourly, Daily, Weekly, Biweekly, Monthly.
      frequency: For Minutely (5/10/15/20/30/45), Hourly (1/2/3/4/6/8/12),
                 or Daily (1-30). Ignored for other intervals.
      end_time: Optional stop time. Runs indefinitely if omitted.
      sun..sat: Day-of-week toggles (used with Minutely/Hourly/Weekly/Biweekly).
      week_day: Weekdays only (Daily interval only).
      day_of_month, week_of_month, day_of_week: Monthly-interval specifiers.
      range_start_time, range_end_time: Within-day time window (Minutely/Hourly).

    Returns: {id, name, interval, frequency, startTime}.
    """
    body: dict[str, Any] = {
        "@type":        "schedule",
        "orgId":        DEFAULT_ORG_ID,
        "name":         name,
        "description":  description or "",
        "startTime":    start_time,
        "startTimeUTC": start_time_utc,
        "interval":     interval,
    }
    if frequency is not None: body["frequency"] = frequency
    if end_time:              body["endTime"] = end_time
    if range_start_time:      body["rangeStartTime"] = range_start_time
    if range_end_time:        body["rangeEndTime"]   = range_end_time
    if day_of_month is not None: body["dayOfMonth"] = day_of_month
    if week_of_month:         body["weekOfMonth"]   = week_of_month
    if day_of_week:           body["dayOfWeek"]     = day_of_week
    if week_day:              body["weekDay"] = True
    for d, val in [("sun",sun),("mon",mon),("tue",tue),("wed",wed),
                   ("thu",thu),("fri",fri),("sat",sat)]:
        if val:
            body[d] = True

    r = _request_v2("POST", "/api/v2/schedule/", json=body)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"create_schedule HTTP {r.status_code}: {r.text[:500]}")
    j = r.json() if r.text else {}
    return {
        "id":        j.get("id"),
        "name":      j.get("name"),
        "interval":  j.get("interval"),
        "frequency": j.get("frequency"),
        "startTime": j.get("startTime"),
    }


@mcp.tool()
def create_linear_taskflow(
    name: str,
    tasks: list[dict[str, Any]],
    description: str = "",
    container_id: str | None = None,
    schedule_id: str | None = None,
) -> dict[str, Any]:
    """Create a linear (sequential) taskflow that runs tasks in order.

    Args:
      name: Taskflow name.
      tasks: List of {taskId, type, name, [stopOnError, stopOnWarning]} entries.
             `type` is one of DMASK, DRS, DSS, MTT, PCS.
      description, container_id, schedule_id: Optional.

    Returns: {id, name, taskCount}.
    """
    # IDMC v2 expects an @type discriminator. workflowTask is the per-task class.
    normalized_tasks = [
        {**t, "@type": t.get("@type", "workflowTask")} for t in tasks
    ]
    body: dict[str, Any] = {
        "@type":       "workflow",
        "name":        name,
        "description": description or "",
        "tasks":       normalized_tasks,
    }
    if container_id: body["containerId"] = container_id
    if schedule_id:  body["scheduleId"]  = schedule_id

    r = _request_v2("POST", "/api/v2/workflow", json=body)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"create_linear_taskflow HTTP {r.status_code}: {r.text[:500]}")
    j = r.json() if r.text else {}
    return {
        "id":        j.get("id"),
        "name":      j.get("name"),
        "taskCount": len(j.get("tasks") or []),
    }


# ---------------------------------------------------------------------------
# v3 Export/Import (asset bundle migration)
# ---------------------------------------------------------------------------
def _poll_export(job_id: str, timeout_s: int = 300) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    last: dict[str, Any] = {}
    while time.time() < deadline:
        r = _request_v3("GET", f"/public/core/v3/export/{job_id}")
        if r.status_code != 200:
            raise RuntimeError(f"export status HTTP {r.status_code}: {r.text[:300]}")
        last = r.json() or {}
        state = (last.get("status") or {}).get("state") or last.get("state") or ""
        if state.upper() in ("SUCCESS", "SUCCESSFUL", "FAILED", "CANCELLED"):
            return last
        time.sleep(2)
    raise RuntimeError(f"export {job_id} did not terminate in {timeout_s}s; last state={last}")


def _poll_import(job_id: str, timeout_s: int = 600) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    last: dict[str, Any] = {}
    while time.time() < deadline:
        r = _request_v3("GET", f"/public/core/v3/import/{job_id}")
        if r.status_code != 200:
            raise RuntimeError(f"import status HTTP {r.status_code}: {r.text[:300]}")
        last = r.json() or {}
        state = (last.get("jobStatus") or {}).get("state") or (last.get("status") or {}).get("state") or ""
        if state.upper() in ("SUCCESS", "SUCCESSFUL", "FAILED", "CANCELLED"):
            return last
        time.sleep(2)
    raise RuntimeError(f"import {job_id} did not terminate in {timeout_s}s; last state={last}")


@mcp.tool()
def export_assets(
    object_ids: list[str],
    output_dir: str = "/tmp",
    include_dependencies: bool = True,
    include_tags: bool = False,
    name: str | None = None,
    wait: bool = True,
    timeout_s: int = 300,
) -> dict[str, Any]:
    """Export one or more IDMC assets to a ZIP package.

    Three-step flow under the hood:
      1. POST /public/core/v3/export       — start the export job
      2. poll  /public/core/v3/export/<id> — wait for state=SUCCESS
      3. GET   /public/core/v3/export/<id>/package — stream the ZIP

    Args:
      object_ids: GUIDs of assets/projects/folders to export. Use the
        v3 lookup or search APIs to resolve names to IDs.
      output_dir: Local directory to write the .zip into.
      include_dependencies: Pull in dependent objects per asset (default True).
      include_tags: Add ?includeTagInformation=true to the POST.
      name: Optional export-job name (server picks one if omitted).
      wait: When False, return immediately after the POST with just job_id.
      timeout_s: Polling timeout (seconds) when wait=True.

    Returns: {job_id, state, message, package_path, package_bytes}.
    """
    body: dict[str, Any] = {
        "objects": [{"id": oid, "includeDependencies": include_dependencies} for oid in object_ids]
    }
    if name:
        body["name"] = name
    path = "/public/core/v3/export"
    if include_tags:
        path += "?includeTagInformation=true"

    r = _request_v3("POST", path, json=body)
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"start export HTTP {r.status_code}: {r.text[:400]}")
    started = r.json() or {}
    job_id = started.get("id") or started.get("jobId")
    if not job_id:
        raise RuntimeError(f"start export response missing id: {started}")
    log.info("export job started id=%s", job_id)

    if not wait:
        return {"job_id": job_id, "state": "STARTED", "message": "wait=False",
                "package_path": None, "package_bytes": 0}

    final = _poll_export(job_id, timeout_s=timeout_s)
    state = (final.get("status") or {}).get("state") or final.get("state") or "?"
    msg   = (final.get("status") or {}).get("message") or ""
    if state.upper() not in ("SUCCESS", "SUCCESSFUL"):
        return {"job_id": job_id, "state": state, "message": msg,
                "package_path": None, "package_bytes": 0}

    # Stream the package
    r = _request_v3("GET", f"/public/core/v3/export/{job_id}/package",
                    headers={"Accept": "application/zip"})
    if r.status_code != 200:
        raise RuntimeError(f"download package HTTP {r.status_code}: {r.text[:300]}")

    out = Path(output_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    fname = f"{(name or 'export')}-{job_id}.zip"
    pkg_path = out / fname
    pkg_path.write_bytes(r.content)
    log.info("downloaded export package %s (%d bytes)", pkg_path, len(r.content))

    return {
        "job_id":        job_id,
        "state":         state,
        "message":       msg,
        "package_path":  str(pkg_path),
        "package_bytes": len(r.content),
    }


@mcp.tool()
def import_package(
    zip_path: str,
    relax_checksum: bool = True,
    default_conflict: str = "REUSE",
    include_objects: list[str] | None = None,
    object_overrides: list[dict[str, Any]] | None = None,
    name: str | None = None,
    wait: bool = True,
    timeout_s: int = 600,
) -> dict[str, Any]:
    """Import an asset bundle ZIP into the IDMC org.

    Three-step flow:
      1. POST /public/core/v3/import/package    — upload the ZIP (multipart)
      2. POST /public/core/v3/import/<jobId>    — start the import with spec
      3. poll /public/core/v3/import/<jobId>    — wait for state=SUCCESS

    Args:
      zip_path: Path to the ZIP to import.
      relax_checksum: True to skip checksum validation (REQUIRED when the
        ZIP was edited after export — without this, modified packages fail
        upload with a checksum error).
      default_conflict: "REUSE" (default) keeps existing assets, "OVERWRITE"
        replaces them. The defaults the docs document are OVERWRITE for
        CDI assets and REUSE for connections/runtime envs/folders. Setting
        REUSE here is the safer default for cloning workflows.
      include_objects: Optional list of asset IDs (from the package) to
        import. Default is all.
      object_overrides: Optional per-object conflict-resolution overrides;
        each item is {"id": "...", "conflictResolution": "OVERWRITE"|"REUSE"}.
      name: Optional import job name.
      wait: Block until terminal state.
      timeout_s: Polling timeout (seconds).

    Returns: {upload_job_id, state, message, checksum_valid}.
    """
    p = Path(zip_path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(zip_path)

    # Step 1: upload the package (multipart)
    upload_path = "/public/core/v3/import/package"
    if relax_checksum:
        upload_path += "?relaxChecksum=true"
    with p.open("rb") as fh:
        files = {"package": (p.name, fh, "application/zip")}
        r = _request_v3("POST", upload_path, files=files)
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"upload package HTTP {r.status_code}: {r.text[:400]}")
    uploaded = r.json() or {}
    job_id = uploaded.get("jobId") or uploaded.get("id")
    if not job_id:
        raise RuntimeError(f"upload response missing jobId: {uploaded}")
    checksum_valid = uploaded.get("checksumValid")
    log.info("uploaded package job_id=%s checksum_valid=%s", job_id, checksum_valid)

    # Step 2: start the import
    spec: dict[str, Any] = {"defaultConflictResolution": default_conflict}
    if include_objects:
        spec["includeObjects"] = include_objects
    if object_overrides:
        spec["objectSpecification"] = object_overrides
    body: dict[str, Any] = {"importSpecification": spec}
    if name:
        body["name"] = name
    r = _request_v3("POST", f"/public/core/v3/import/{job_id}", json=body)
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"start import HTTP {r.status_code}: {r.text[:400]}")

    if not wait:
        return {"upload_job_id": job_id, "state": "STARTED", "message": "wait=False",
                "checksum_valid": checksum_valid}

    final = _poll_import(job_id, timeout_s=timeout_s)
    state = ((final.get("jobStatus") or final.get("status") or {}).get("state")) or "?"
    msg   = ((final.get("jobStatus") or final.get("status") or {}).get("message")) or ""
    return {
        "upload_job_id":  job_id,
        "state":          state,
        "message":        msg,
        "checksum_valid": checksum_valid,
    }


# NOTE: clone_mapping (a tool that attempted to export → rewrite → re-import a
# mapping under a new name) was investigated and removed. The IDMC migration
# service returns MigrationSvc_072 ("object types in the package that require
# unchanged checksums have been modified") for any edit to the inner
# DTEMPLATE.zip — even with relaxChecksum=true. Manifest-only edits are
# accepted but the importer reads canonical identity from the unmodified
# inner bundle, so they're inert.
#
# Pivot: use parameterized mapping templates + create_mapping_task with
# per-task parameter values, instead of cloning mappings. Helpers below
# (_new_idmc_guid, _rewrite_zip) are retained for future ZIP-rewrite needs.

def _new_idmc_guid() -> str:
    """Generate a 22-char base62 GUID in the format IDMC uses."""
    import secrets
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(22))


def _rewrite_zip(in_zip: Path, out_zip: Path, replacements: dict[str, str],
                 path_renames: dict[str, str] | None = None,
                 skip_files: set[str] | None = None,
                 recurse_into: tuple[str, ...] = (".DTEMPLATE.zip",)) -> None:
    """Copy `in_zip` to `out_zip` with edits.

    Behavior:
      - Text files (UTF-8 decodable): apply `replacements`.
      - Entry paths: apply `path_renames` (substring match → replace).
      - `skip_files`: drop those entries entirely.
      - Nested ZIPs whose name ends with one of `recurse_into` (default
        `.DTEMPLATE.zip` only) are unpacked, edited, and rezipped.
      - All OTHER nested ZIPs are copied byte-for-byte. The server signs/
        validates connection bundles, so re-zipping breaks them even if
        no content substitution happens (re-deflate produces different
        bytes than the original).
    """
    import zipfile, io
    path_renames = path_renames or {}
    skip_files = skip_files or set()

    def _apply_to_bytes(data: bytes) -> bytes:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return data
        for old, new in replacements.items():
            text = text.replace(old, new)
        return text.encode("utf-8")

    def _rename(name: str) -> str:
        for old, new in path_renames.items():
            if old in name:
                name = name.replace(old, new)
        return name

    def _should_recurse(name: str) -> bool:
        lname = name.lower()
        return any(lname.endswith(s.lower()) for s in recurse_into)

    with zipfile.ZipFile(in_zip, "r") as zin, zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename in skip_files:
                continue
            data = zin.read(item.filename)
            out_name = _rename(item.filename)

            if _should_recurse(item.filename) and not item.is_dir():
                tmp_in  = io.BytesIO(data)
                tmp_out = io.BytesIO()
                with zipfile.ZipFile(tmp_in, "r") as nin, \
                     zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED) as nout:
                    for inner in nin.infolist():
                        if inner.filename in skip_files:
                            continue
                        inner_data = nin.read(inner.filename)
                        inner_data = _apply_to_bytes(inner_data)
                        inner_name = _rename(inner.filename)
                        nout.writestr(inner_name, inner_data)
                data = tmp_out.getvalue()
            elif item.filename.lower().endswith(".zip") and not item.is_dir():
                # Byte-for-byte copy — DO NOT re-deflate (server rejects).
                pass
            else:
                data = _apply_to_bytes(data)

            zout.writestr(out_name, data)


# ---------------------------------------------------------------------------
# CDGC tools
# ---------------------------------------------------------------------------
@mcp.tool()
def upload_dq_scores(
    asset_id: str,
    value: float,
    total_count: int,
    exception: int,
    scanned_time: str | None = None,
) -> dict[str, Any]:
    """Upload a single DQ score for an existing rule occurrence in CDGC.

    Args:
      asset_id: Internal id of the DQ rule occurrence (RuleInstance) the
                score belongs to. This is the GUID in the CDGC URL when you
                open the rule occurrence.
      value:        DQ score (typically 0-100 percent or similar metric).
      total_count:  Total rows evaluated.
      exception:    Rows that failed the rule.
      scanned_time: ISO 8601 timestamp of the scan. Defaults to now (UTC).

    Returns: {http_status, asset_id, value, scanned_time}.

    Note: This endpoint updates an EXISTING rule occurrence. To create the
    rule occurrence in the first place (binding a CDQ rule spec to a CDGC
    data element), use register_in_cdgc.

    Auth: CDGC requires a Bearer JWT. The server mints one automatically via
    /identity-service/api/v1/jwt/Token (using the v2 IDS-SESSION-ID), caches
    it for ~29 minutes, and force-refreshes on any 401. No manual bootstrap
    needed.
    """
    from datetime import datetime, timezone
    if not scanned_time:
        scanned_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    body = {"scores": [{
        "assetId": asset_id,
        "dqscore": {"facts": {
            "com.infa.ccgf.models.governance.value":       value,
            "com.infa.ccgf.models.governance.totalCount":  total_count,
            "com.infa.ccgf.models.governance.exception":   exception,
            "com.infa.ccgf.models.governance.scannedTime": scanned_time,
        }},
    }]}
    url = f"{CDGC_API_BASE}/ccgf-ruleautomation/api/v1/dataQuality/publishScore?refBy=INTERNAL"
    r = _request_cdgc("PATCH", url, json=body)
    if r.status_code not in (200, 201, 202, 204):
        raise RuntimeError(f"upload_dq_scores HTTP {r.status_code}: {r.text[:500]}")
    return {
        "http_status":  r.status_code,
        "asset_id":     asset_id,
        "value":        value,
        "scanned_time": scanned_time,
    }


# Maps CDQ dimension constants (uppercased in rule specs) → CDGC RuleType
# values (title-cased in /publish payloads).
_DIMENSION_TO_RULE_TYPE = {
    "ACCURACY":     "Accuracy",
    "COMPLETENESS": "Completeness",
    "VALIDITY":     "Validity",
    "TIMELINESS":   "Timeliness",
    "CONSISTENCY":  "Consistency",
    "UNIQUENESS":   "Uniqueness",
    "CONFORMITY":   "Conformity",
    "INTEGRITY":    "Integrity",
}


@mcp.tool()
def register_in_cdgc(
    rule_spec_id: str,
    column_id: str,
    occurrence_name: str,
    dimension: str = "Accuracy",
    criticality: str = "Medium",
    target: float = 90,
    threshold: float = 70,
    input_port_name: str = "Input",
    output_port_name: str = "PrimaryRuleSet",
    description: str = "",
    column_identity_type: str = "EXTERNAL",
    catalog_origin: str | None = None,
    frequency: str | None = None,
) -> dict[str, Any]:
    """Create a CDGC rule occurrence binding a CDQ rule spec to a data element.

    POSTs a two-item batch to /ccgf-contentv2/api/v1/publish:
      1. INSERT a RuleInstance (the rule occurrence) with the FRS rule spec id
         in TechnicalRuleReference.
      2. INSERT an asscParentDataElementRuleInstance relationship from the
         column to the new occurrence (referenced by a provisional id).

    The server returns HTTP 207 with per-item statuses and the resolved
    public id (e.g. "DQO-4") plus internal UUID of the new occurrence.

    Args:
      rule_spec_id:        FRS document id of the CDQ rule spec (the value
                           returned by create_dq_rules). Becomes
                           TechnicalRuleReference on the RuleInstance.
      column_id:           Identity of the CDGC data element (column) to
                           attach the occurrence to. By default this is the
                           EXTERNAL id, formatted as
                           "<origin>://schema/table/column~com.infa.odin.models.relational.Column".
                           Pass column_identity_type="INTERNAL" to use the
                           internal UUID instead.
      occurrence_name:     core.name on the new RuleInstance.
      dimension:           Quality dimension. Accepts CDQ-style "ACCURACY"
                           or CDGC-style "Accuracy".
      criticality:         "Low" | "Medium" | "High".
      target, threshold:   DQ score thresholds (0-100).
      input_port_name:     Name of the rule's input field. Must match the
                           Field name in the CDQ rule model (default "Input").
      output_port_name:    Name of the rule's output (default "PrimaryRuleSet").
      description:         Free-text TechnicalDescription.
      column_identity_type: "EXTERNAL" (default) or "INTERNAL".
      catalog_origin:      Catalog source UUID. If omitted, derived from the
                           "<origin>://" prefix of an EXTERNAL column_id.
                           Required when column_identity_type="INTERNAL".
      frequency:           Optional scan frequency string. null by default.

    Returns: {occurrence_id, internal_id, provisional_id, name, http_status,
             items:[{element_type, status_code, message_code, identity,
             internal_identity}]}.
    """
    column_identity_type = column_identity_type.upper()
    if column_identity_type not in ("EXTERNAL", "INTERNAL"):
        raise RuntimeError(f"column_identity_type must be EXTERNAL or INTERNAL, got {column_identity_type!r}")

    rule_type = _DIMENSION_TO_RULE_TYPE.get(dimension.upper(), dimension)

    if not catalog_origin:
        if column_identity_type == "EXTERNAL" and "://" in column_id:
            catalog_origin = column_id.split("://", 1)[0]
        else:
            raise RuntimeError(
                "catalog_origin is required when column_identity_type=INTERNAL "
                "or when column_id is not in '<origin>://path~class' form"
            )

    provisional_id = f"infa-agent{uuid.uuid4().hex[:8]}"

    body = {"items": [
        {
            "elementType":  "OBJECT",
            "operation":    "INSERT",
            "type":         "com.infa.ccgf.models.governance.RuleInstance",
            "identity":     provisional_id,
            "identityType": "PROVISIONAL",
            "attributes": {
                "core.name":                                            occurrence_name,
                "core.origin":                                          catalog_origin,
                "com.infa.ccgf.models.governance.RuleType":             rule_type,
                "com.infa.ccgf.models.governance.MeasuringMethod":      "InformaticaCloudDataQuality",
                "com.infa.ccgf.models.governance.Criticality":          criticality,
                "com.infa.ccgf.models.governance.Target":               target,
                "com.infa.ccgf.models.governance.Threshold":            threshold,
                "com.infa.ccgf.models.governance.Frequency":            frequency,
                "com.infa.ccgf.models.governance.ruleInputPortName":    input_port_name,
                "com.infa.ccgf.models.governance.ruleOutputPortName":   output_port_name,
                "com.infa.ccgf.models.governance.TechnicalDescription": description,
                "com.infa.ccgf.models.governance.TechnicalRuleReference": rule_spec_id,
            },
        },
        {
            "elementType":        "RELATIONSHIP",
            "operation":          "INSERT",
            "type":               "com.infa.ccgf.models.governance.asscParentDataElementRuleInstance",
            "fromIdentity":       column_id,
            "toIdentity":         provisional_id,
            "sourceIdentityType": column_identity_type,
            "targetIdentityType": "PROVISIONAL",
            "attributes":         {},
        },
    ]}

    correlation_id = str(uuid.uuid4())
    url = f"{CDGC_API_BASE}/ccgf-contentv2/api/v1/publish"
    r = _request_cdgc("POST", url, json=body, headers={
        "x-infa-product-id": "CDGC",
        "correlation-id":    correlation_id,
        "operation-id":      correlation_id,
        "x-infa-tid":        correlation_id,
        "x_infa_log_ctx":    f"req_id={correlation_id}",
    })
    if r.status_code not in (200, 201, 207):
        raise RuntimeError(f"register_in_cdgc HTTP {r.status_code}: {r.text[:500]}")

    j = r.json() if r.text else {}
    items = j.get("items") or []

    failures = [
        it for it in items
        if int(it.get("statusCode") or 0) >= 400
        or (it.get("messageCode") and it.get("messageCode") != "CONTENT_SUCCESS")
    ]
    if failures:
        raise RuntimeError(f"register_in_cdgc partial failure: {failures}")

    rule_instance = next(
        (it for it in items
         if it.get("elementType") == "OBJECT"
         and it.get("type") == "com.infa.ccgf.models.governance.RuleInstance"),
        {},
    )

    return {
        "occurrence_id":   rule_instance.get("identity"),
        "internal_id":     rule_instance.get("internalIdentity"),
        "provisional_id":  rule_instance.get("provisionalIdentity") or provisional_id,
        "name":            occurrence_name,
        "rule_spec_id":    rule_spec_id,
        "column_id":       column_id,
        "http_status":     r.status_code,
        "items": [
            {
                "element_type":      it.get("elementType"),
                "operation":         it.get("operation"),
                "status_code":       it.get("statusCode"),
                "message_code":      it.get("messageCode"),
                "identity":          it.get("identity"),
                "internal_identity": it.get("internalIdentity"),
            }
            for it in items
        ],
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def _ui_links(**ids: str | None) -> dict[str, str]:
    """Construct best-guess IDMC UI URLs for created artifacts.

    Caller passes any subset of {rule, mapping, mapping_task, run, schedule,
    occurrence}. Returned dict only contains keys whose IDs are non-empty.
    URL patterns follow the dmp-us pod conventions; override IDMC_UI_BASE
    via env for other pods.
    """
    out: dict[str, str] = {}
    if ids.get("rule"):
        out["rule"] = f"{UI_BASE}/{ids['rule']}"
    if ids.get("mapping"):
        out["mapping"] = f"{IDMC_UI_BASE}/diUI/products/integrationDesign/main/mapping/{ids['mapping']}"
    if ids.get("mapping_task"):
        out["mapping_task"] = f"{IDMC_UI_BASE}/diUI/products/integrationDesign/main/mappingTask/{ids['mapping_task']}"
    if ids.get("run"):
        out["monitor_run"] = f"{IDMC_UI_BASE}/diUI/products/monitor/Activity/runId/{ids['run']}"
    if ids.get("schedule"):
        out["schedule"] = f"{IDMC_UI_BASE}/adminUI/schedule/{ids['schedule']}"
    return out


def _step(name: str, fn, **kwargs) -> dict[str, Any]:
    """Run one pipeline step; capture status/result/error without aborting."""
    started = time.time()
    try:
        result = fn(**kwargs)
        return {
            "step":       name,
            "status":     "SUCCESS",
            "elapsed_ms": int((time.time() - started) * 1000),
            "result":     result,
        }
    except Exception as e:  # noqa: BLE001
        log.warning("step %s failed: %s", name, e)
        return {
            "step":       name,
            "status":     "FAILED",
            "elapsed_ms": int((time.time() - started) * 1000),
            "error":      str(e)[:500],
        }


@mcp.tool()
def run_governance_pipeline(
    rule_name: str,
    runtime_environment_id: str,
    goal: str = "",
    # ---- create_dq_rules ----
    rule_description: str = "Created by run_governance_pipeline",
    rule_field: str = "Input",
    rule_dimension: str = "COMPLETENESS",
    rule_template: str | None = None,
    rule_space_id: str | None = None,
    rule_space_name: str | None = None,
    rule_project_id: str | None = None,
    rule_project_name: str | None = None,
    # ---- generate_dq_mapping_task ----
    template_mapping_id: str = DEFAULT_DQ_TEMPLATE_MAPPING_ID,
    task_name: str | None = None,
    task_description: str = "",
    task_container_id: str | None = None,
    input_field_mapping: str = "",
    # Source/target connections for the mapping task. Default to env vars so
    # callers that only set IDMC_DQ_CONNECTION_ID don't need to pass anything.
    source_connection_id: str | None = None,
    source_table: str | None = None,
    target_connection_id: str | None = None,
    target_table: str | None = None,
    # Pass an existing rule spec id to skip create_dq_rules and reuse it.
    rule_id_override: str | None = None,
    # ---- create_schedule (skip if no schedule_start_time) ----
    schedule_name: str | None = None,
    schedule_start_time: str | None = None,
    schedule_start_time_utc: str | None = None,
    schedule_interval: str = "Daily",
    schedule_frequency: int | None = None,
    # ---- register_in_cdgc (skip if no cdgc_column_id) ----
    cdgc_column_id: str | None = None,
    cdgc_occurrence_name: str | None = None,
    cdgc_dimension: str | None = None,
    cdgc_criticality: str = "Medium",
    cdgc_target: float = 90.0,
    cdgc_threshold: float = 70.0,
    cdgc_input_port_name: str = "Input",
    cdgc_output_port_name: str = "PrimaryRuleSet",
    cdgc_column_identity_type: str = "EXTERNAL",
    cdgc_catalog_origin: str | None = None,
    # ---- run task + upload score ----
    run_now: bool = False,
    score_value: float | None = None,
    score_total_count: int | None = None,
    score_exception: int | None = None,
    score_asset_id: str | None = None,
) -> dict[str, Any]:
    """Master orchestrator: chain create_dq_rules → generate_dq_mapping_task →
    create_schedule → register_in_cdgc → optionally run task → optionally upload score.

    Each step records SUCCESS / SKIPPED / FAILED with elapsed time and result/error.
    A single step failing does NOT abort the pipeline — later steps that depend
    on its output are marked SKIPPED with a dependency reason. The full report
    is always returned so the caller can see what landed and what didn't.

    Required:
      rule_name: Display name for the new CDQ rule spec.
      runtime_environment_id: Secure Agent / runtime id for the mapping task.

    Optional flow controls:
      goal: NL description of what this pipeline is for. Echoed into the
        report; not interpreted. The LLM that calls this tool should already
        have parsed the user goal into the structured params below.
      template_mapping_id: FRS GUID of a parameterized template mapping.
        Defaults to M_DQ_Generic, which bakes Source/Target connections
        into the mapping itself and exposes only Rule_Spec + Input_Field_Map.
      input_field_mapping: ``<source_column>=<rule_input_port>`` (e.g.
        ``customer_name=Input``). Required when the mapping-task step is
        to run; the step SKIPs if absent.
      schedule_start_time / schedule_start_time_utc: If set, create a v2
        schedule with the given cadence (interval/frequency). Reported as a
        sibling artifact; bind it to the task by passing the resulting
        schedule_id back into a subsequent create_mapping_task call.
      cdgc_column_id: Identity of a CDGC data element to bind the rule to as
        a rule occurrence. Skip the CDGC registration step if absent.
      run_now: When True, POST /api/v2/job to trigger the task immediately
        after the rest of the chain. Returns a runId in the report.
      score_value / score_total_count / score_exception / score_asset_id:
        When all four are set, upload a DQ score (typically used after the
        task has run and you've parsed its output). Independent of run_now.

    All other args are pass-throughs to the underlying tool. See the docstring
    of each underlying tool for argument semantics.

    Returns: {goal, summary:{ok, failed, skipped}, artifacts:{...ids},
              ui_urls:{...}, steps:[{step, status, elapsed_ms, result|error|reason}]}.
    """
    artifacts: dict[str, Any] = {}
    steps: list[dict[str, Any]] = []
    log.info("run_governance_pipeline: starting goal=%r rule=%r", goal[:80], rule_name)

    # ---- Step 1: create_dq_rules (or reuse existing rule spec) ----
    if rule_id_override:
        rule_id = rule_id_override
        steps.append({"step": "create_dq_rules", "status": "SKIPPED",
                      "reason": f"rule_id_override supplied: {rule_id_override}"})
    else:
        s = _step(
            "create_dq_rules", create_dq_rules,
            rule_name=rule_name, description=rule_description,
            field_name=rule_field, dimension=rule_dimension,
            rule_template=rule_template,
            space_id=rule_space_id, space_name=rule_space_name,
            project_id=rule_project_id, project_name=rule_project_name,
        )
        steps.append(s)
        if s["status"] == "FAILED":
            return {
                "goal":     goal,
                "summary":  {"ok": 0, "failed": 1, "skipped": 0},
                "artifacts": {},
                "ui_urls":   {},
                "steps":     steps,
            }
        rule_id = s["result"]["id"]
    artifacts["rule_id"] = rule_id

    # ---- Step 2: generate_dq_mapping_task ----
    _src_conn = source_connection_id or os.getenv("IDMC_DQ_CONNECTION_ID", "")
    _src_tbl  = source_table or ""
    _tgt_conn = target_connection_id or _src_conn
    _tgt_tbl  = target_table or ((_src_tbl + "_BAD_RECORDS") if _src_tbl else "")
    _rt_env   = runtime_environment_id or os.getenv("IDMC_DQ_RUNTIME_ENV_ID", "")

    task_id: str | None = None
    if input_field_mapping and _src_conn and _src_tbl:
        s = _step(
            "generate_dq_mapping_task", generate_dq_mapping_task,
            rule_spec_id=rule_id,
            input_field_mapping=input_field_mapping,
            runtime_environment_id=_rt_env,
            template_mapping_id=template_mapping_id,
            source_connection_id=_src_conn,
            source_table=_src_tbl,
            target_connection_id=_tgt_conn,
            target_table=_tgt_tbl,
            task_name=task_name or f"{rule_name}_task",
            description=task_description,
            container_id=task_container_id,
        )
        steps.append(s)
        if s["status"] == "SUCCESS":
            task_id = s["result"].get("id")
            artifacts["task_id"] = task_id
            artifacts["task_name"] = s["result"].get("name")
            artifacts["mapping_id"] = _resolve_v2_mapping_id(template_mapping_id)
            artifacts["parameter_names"] = s["result"].get("parameter_names")
    else:
        steps.append({
            "step":   "generate_dq_mapping_task",
            "status": "SKIPPED",
            "reason": (
                "input_field_mapping is empty" if not input_field_mapping
                else "source_connection_id / source_table not provided and IDMC_DQ_CONNECTION_ID not set"
            ),
        })

    # ---- Step 4: create_schedule ----
    if schedule_start_time and schedule_start_time_utc:
        s = _step(
            "create_schedule", create_schedule,
            name=schedule_name or f"{rule_name}_schedule",
            start_time=schedule_start_time,
            start_time_utc=schedule_start_time_utc,
            interval=schedule_interval,
            frequency=schedule_frequency,
        )
        steps.append(s)
        if s["status"] == "SUCCESS":
            artifacts["schedule_id"] = s["result"].get("id")
            artifacts["schedule_name"] = s["result"].get("name")
    else:
        steps.append({
            "step":   "create_schedule",
            "status": "SKIPPED",
            "reason": "schedule_start_time / schedule_start_time_utc not provided",
        })

    # ---- Step 5: register_in_cdgc ----
    if cdgc_column_id:
        s = _step(
            "register_in_cdgc", register_in_cdgc,
            rule_spec_id=rule_id,
            column_id=cdgc_column_id,
            occurrence_name=cdgc_occurrence_name or f"{rule_name}_occurrence",
            dimension=cdgc_dimension or rule_dimension,
            criticality=cdgc_criticality,
            target=cdgc_target,
            threshold=cdgc_threshold,
            input_port_name=cdgc_input_port_name,
            output_port_name=cdgc_output_port_name,
            column_identity_type=cdgc_column_identity_type,
            catalog_origin=cdgc_catalog_origin,
        )
        steps.append(s)
        if s["status"] == "SUCCESS":
            artifacts["occurrence_id"] = s["result"].get("occurrence_id")
            artifacts["occurrence_internal_id"] = s["result"].get("internal_id")
    else:
        steps.append({
            "step":   "register_in_cdgc",
            "status": "SKIPPED",
            "reason": "cdgc_column_id not provided",
        })

    # ---- Step 6: run task ----
    run_id: int | None = None
    if run_now and task_id:
        s = _step("run_task", _run_mapping_task, task_id=task_id, task_type="MTT")
        steps.append(s)
        if s["status"] == "SUCCESS":
            run_id = s["result"].get("runId")
            artifacts["run_id"] = run_id
    elif run_now:
        steps.append({
            "step":   "run_task",
            "status": "SKIPPED",
            "reason": "run_now=True but no task_id (generate_dq_mapping_task did not run or failed)",
        })
    else:
        steps.append({
            "step":   "run_task",
            "status": "SKIPPED",
            "reason": "run_now=False",
        })

    # ---- Step 7: upload_dq_scores ----
    score_inputs = [score_value, score_total_count, score_exception, score_asset_id]
    if all(v is not None for v in score_inputs):
        s = _step(
            "upload_dq_scores", upload_dq_scores,
            asset_id=score_asset_id,
            value=score_value,
            total_count=score_total_count,
            exception=score_exception,
        )
        steps.append(s)
        if s["status"] == "SUCCESS":
            artifacts["score_uploaded"] = True
            artifacts["score_asset_id"] = score_asset_id
    else:
        steps.append({
            "step":   "upload_dq_scores",
            "status": "SKIPPED",
            "reason": "score_value/total_count/exception/asset_id not all provided",
        })

    ok      = sum(1 for s in steps if s["status"] == "SUCCESS")
    failed  = sum(1 for s in steps if s["status"] == "FAILED")
    skipped = sum(1 for s in steps if s["status"] == "SKIPPED")

    return {
        "goal":      goal,
        "summary":   {"ok": ok, "failed": failed, "skipped": skipped},
        "artifacts": artifacts,
        "ui_urls":   _ui_links(
            rule=artifacts.get("rule_id"),
            mapping=artifacts.get("mapping_id"),
            mapping_task=artifacts.get("task_id"),
            run=str(run_id) if run_id else None,
            schedule=artifacts.get("schedule_id"),
        ),
        "steps":     steps,
    }


def _infer_dimensions_for_column(col_name: str, data_type: str) -> list[str]:
    """Return appropriate DQ dimensions for a column based on its type and name.

    COMPLETENESS is always included. Additional dimensions are chosen by:
      - VARCHAR/TEXT/CHAR  → VALIDITY  (format / non-empty check)
      - NUMBER/INT/DECIMAL
          with ID/KEY/PK in name → UNIQUENESS
          otherwise              → VALIDITY  (range check)
      - TIMESTAMP/DATE/TIME      → TIMELINESS
      - BOOLEAN                  → VALIDITY  (value-domain check)
    """
    dims: list[str] = ["COMPLETENESS"]
    dt = data_type.lower()
    cn = col_name.upper()

    if any(x in dt for x in ("varchar", "text", "string", "char", "nvarchar")):
        dims.append("VALIDITY")
    elif any(x in dt for x in ("number", "numeric", "int", "decimal", "float", "double")):
        if any(x in cn for x in ("_ID", "ID_", "_KEY", "KEY_", "PK_", "_PK", "RULE_ID")):
            dims.append("UNIQUENESS")
        else:
            dims.append("VALIDITY")
    elif any(x in dt for x in ("timestamp", "date", "time")):
        dims.append("TIMELINESS")
    elif any(x in dt for x in ("boolean", "bool")):
        dims.append("VALIDITY")

    return dims


def _delete_existing_rule_occurrences(column_names: list[str], catalog_origin: str) -> int:
    """Delete all CDGC RuleInstance occurrences for the given column names and catalog origin.

    Searches by occurrence name pattern DQ_{col}_{dim} and deletes any matching
    RuleInstance assets. Safe to call before re-creating occurrences.
    Returns number of occurrences deleted.
    """
    _all_dims = ["COMPLETENESS", "VALIDITY", "UNIQUENESS", "TIMELINESS", "ACCURACY", "CONSISTENCY"]
    deleted = 0
    publish_url = f"{CDGC_API_BASE}/ccgf-contentv2/api/v1/publish"

    for col_name in column_names:
        for dim in _all_dims:
            occ_name = f"DQ_{col_name}_{dim}"
            search_url = (
                f"{CDGC_API_BASE}/data360/search/v1/assets"
                f"?knowledgeQuery={quote(occ_name)}&segments=summary,systemAttributes"
            )
            try:
                sr = _request_cdgc("POST", search_url, json={"from": 0, "size": 20})
                if sr.status_code != 200:
                    continue
                for hit in (sr.json() or {}).get("hits", []):
                    ctype = (hit.get("systemAttributes") or {}).get("core.classType") or ""
                    if "RuleInstance" not in ctype:
                        continue
                    hit_name = (hit.get("summary") or {}).get("core.name") or ""
                    if hit_name != occ_name:
                        continue
                    hit_origin = (hit.get("systemAttributes") or {}).get("core.origin") or ""
                    if catalog_origin and hit_origin and hit_origin != catalog_origin:
                        continue
                    internal_id = (hit.get("systemAttributes") or {}).get("core.identity") or hit.get("id") or ""
                    if not internal_id:
                        continue
                    del_body = {"items": [{
                        "elementType":  "OBJECT",
                        "operation":    "DELETE",
                        "type":         "com.infa.ccgf.models.governance.RuleInstance",
                        "identity":     internal_id,
                        "identityType": "INTERNAL",
                    }]}
                    dr = _request_cdgc("POST", publish_url, json=del_body)
                    if dr.status_code in (200, 201, 207):
                        deleted += 1
                        log.info("deleted rule occurrence %s (internal_id=%s)", occ_name, internal_id)
            except Exception as exc:
                log.warning("_delete_existing_rule_occurrences: error for %s/%s: %s", col_name, dim, exc)

    return deleted


@mcp.tool()
def create_generic_dq_rules(
    table_name: str,
    column_ids: list[dict[str, str]],
    catalog_origin: str,
    dimensions: list[str] | None = None,
    criticality: str = "High",
    target: float = 95.0,
    threshold: float = 80.0,
    source_table_path: str | None = None,
    cleanup_existing: bool = False,
) -> dict[str, Any]:
    """Create diversified DQ rules and register occurrences on every column supplied.

    When column_ids entries include a "data_type" key, dimensions are auto-selected
    per column based on data type and name patterns:
      - VARCHAR/TEXT    → COMPLETENESS + VALIDITY
      - NUMBER (ID/key) → COMPLETENESS + UNIQUENESS
      - NUMBER (other)  → COMPLETENESS + VALIDITY
      - TIMESTAMP/DATE  → COMPLETENESS + TIMELINESS
      - BOOLEAN         → COMPLETENESS + VALIDITY

    If dimensions is explicitly passed, it overrides auto-selection for all columns.
    One rule spec is created per (table, dimension) pair and reused across columns
    — avoiding one-rule-per-column sprawl.

    When source_table_path is provided and IDMC_DQ_CONNECTION_ID / IDMC_DQ_RUNTIME_ENV_ID
    env vars are set, an M_DQ_Generic mapping task is created for each (rule, column)
    pair so CDQ can execute the rules against the Snowflake source.

    Args:
      table_name:        Source table name (used in rule naming).
      column_ids:        List of {column_name, column_id[, data_type]} dicts.
      catalog_origin:    CDGC catalog origin (first segment of the table's external_id).
      dimensions:        Override dimension list applied to all columns. When omitted,
                         dimensions are inferred per column from data_type.
      criticality:       CDGC occurrence criticality (default "High").
      target:            Target score % (default 95).
      threshold:         Minimum acceptable score % (default 80).
      source_table_path: Snowflake path (DB/SCHEMA/TABLE) used to bind mapping tasks.
                         When omitted, no mapping tasks are created.
      cleanup_existing:  Delete any pre-existing rule occurrences for these columns
                         before creating new ones (default False — deletion is OFF).
                         Disabled for now: during a CDGC outage the delete can succeed
                         while the recreate fails, silently dropping DQROs. Pass True
                         explicitly to opt back into cleanup of stale/errored occurrences.

    Returns: {rules_created, occurrences_registered, mapping_tasks_created, errors, summary}
    """
    rules_created: list[dict[str, Any]] = []
    occurrences: list[dict[str, Any]] = []
    errors: list[str] = []
    deleted_count = 0

    # Delete stale/errored occurrences from prior runs before creating fresh ones
    if cleanup_existing and column_ids:
        col_names = [c.get("column_name", "") for c in column_ids if c.get("column_name")]
        deleted_count = _delete_existing_rule_occurrences(col_names, catalog_origin)
        log.info("create_generic_dq_rules: deleted %d stale occurrences before re-create", deleted_count)

    # Cache of rule_id per dimension so we create the spec only once per dim
    dim_rule_cache: dict[str, str] = {}

    def _ensure_rule(dim: str) -> str | None:
        if dim in dim_rule_cache:
            return dim_rule_cache[dim]
        rule_name = f"DQ_{table_name}_{dim}"
        try:
            rule = create_dq_rules(
                rule_name=rule_name,
                description=f"Generic {dim.lower()} check for {table_name} columns",
                field_name="Input",
                dimension=dim,
            )
            rule_id = rule.get("id", "")
            rules_created.append({"rule_name": rule_name, "rule_id": rule_id, "dimension": dim})
        except Exception as e:
            err_msg = str(e)
            if "already exists" in err_msg or "FRS_132" in err_msg or "400" in err_msg:
                hits = list_rule_specifications(name_filter=rule_name, top=500)
                match = next((r for r in hits.get("rules", []) if r.get("name") == rule_name), None)
                if match:
                    rule_id = match.get("id", "")
                    rules_created.append({"rule_name": rule_name, "rule_id": rule_id, "dimension": dim, "note": "already exists"})
                else:
                    errors.append(f"create rule {rule_name}: {err_msg[:120]}")
                    return None
            else:
                errors.append(f"create rule {rule_name}: {err_msg[:120]}")
                return None
        dim_rule_cache[dim] = rule_id
        return rule_id

    for col in column_ids:
        col_name  = col.get("column_name", "")
        col_id    = col.get("column_id", "")
        data_type = col.get("data_type", "unknown")

        col_dims = dimensions if dimensions else _infer_dimensions_for_column(col_name, data_type)

        for dim in col_dims:
            rule_id = _ensure_rule(dim)
            if not rule_id:
                continue
            occ_name = f"DQ_{col_name}_{dim}"
            try:
                occ = register_in_cdgc(
                    rule_spec_id=rule_id,
                    column_id=col_id,
                    occurrence_name=occ_name,
                    dimension=dim,
                    criticality=criticality,
                    target=target,
                    threshold=threshold,
                    column_identity_type="INTERNAL",
                    catalog_origin=catalog_origin,
                )
                occurrences.append({
                    "name":          occ_name,
                    "column":        col_name,
                    "data_type":     data_type,
                    "dimension":     dim,
                    "occurrence_id": occ.get("occurrence_id"),
                    "internal_id":   occ.get("internal_id"),
                })
            except Exception as e:
                err_str = str(e)
                # If the occurrence already exists, search CDGC for it and recover its internal_id
                # so that score upload (step 8) still works on re-runs.
                recovered = False
                if any(kw in err_str.upper() for kw in ("ALREADY", "DUPLICATE", "CONTENT_FAILED", "EXISTS")):
                    try:
                        search_url = (
                            f"{CDGC_API_BASE}/data360/search/v1/assets"
                            f"?knowledgeQuery={quote(occ_name)}&segments=summary,systemAttributes"
                        )
                        sr = _request_cdgc("POST", search_url, json={"from": 0, "size": 20})
                        if sr.status_code == 200:
                            for hit in (sr.json() or {}).get("hits", []):
                                ctype = (hit.get("systemAttributes") or {}).get("core.classType") or ""
                                if "RuleInstance" not in ctype:
                                    continue
                                hit_name = (hit.get("summary") or {}).get("core.name") or ""
                                if hit_name != occ_name:
                                    continue
                                internal_id = (hit.get("systemAttributes") or {}).get("core.identity") or hit.get("id") or ""
                                public_id   = (hit.get("summary") or {}).get("core.externalId") or ""
                                if internal_id:
                                    occurrences.append({
                                        "name":          occ_name,
                                        "column":        col_name,
                                        "data_type":     data_type,
                                        "dimension":     dim,
                                        "occurrence_id": public_id,
                                        "internal_id":   internal_id,
                                        "note":          "already exists",
                                    })
                                    recovered = True
                                    break
                    except Exception:
                        pass
                if not recovered:
                    errors.append(f"register {col_name}/{dim}: {err_str[:120]}")

    # ------------------------------------------------------------------
    # Optionally create M_DQ_Generic mapping tasks so CDQ can execute rules
    # ------------------------------------------------------------------
    mapping_tasks_created: list[dict[str, Any]] = []
    if source_table_path:
        _src_conn = os.getenv("IDMC_DQ_CONNECTION_ID", "")
        _rt_env   = os.getenv("IDMC_DQ_RUNTIME_ENV_ID", "")
        _tmpl     = DEFAULT_DQ_TEMPLATE_MAPPING_ID
        if _src_conn and _rt_env:
            _tgt_tbl = source_table_path + "_BAD_RECORDS"
            # One mapping task per (rule_id, column) occurrence
            _seen_tasks: set[tuple[str, str]] = set()
            for occ in occurrences:
                col_name = occ.get("column", "")
                dim      = occ.get("dimension", "")
                rule_id  = dim_rule_cache.get(dim, "")
                if not rule_id or not col_name:
                    continue
                task_key = (rule_id, col_name)
                if task_key in _seen_tasks:
                    continue
                _seen_tasks.add(task_key)
                try:
                    mt = generate_dq_mapping_task(
                        source_connection_id=_src_conn,
                        source_table=source_table_path,
                        target_connection_id=_src_conn,
                        target_table=_tgt_tbl,
                        input_field_mapping=f"{col_name}=Input",
                        runtime_environment_id=_rt_env,
                        template_mapping_id=_tmpl,
                        rule_spec_id=rule_id,
                        task_name=f"mt_{table_name}_{col_name}_{dim}",
                    )
                    mapping_tasks_created.append({
                        "task_id":   mt.get("id", ""),
                        "task_name": mt.get("name", f"mt_{table_name}_{col_name}_{dim}"),
                        "rule_id":   rule_id,
                        "column":    col_name,
                        "dimension": dim,
                    })
                except Exception as e:
                    err_str = str(e)
                    if "already exists" in err_str.lower() or "duplicate" in err_str.lower():
                        mapping_tasks_created.append({
                            "task_name": f"mt_{table_name}_{col_name}_{dim}",
                            "rule_id":   rule_id,
                            "column":    col_name,
                            "dimension": dim,
                            "note":      "already exists",
                        })
                    else:
                        errors.append(f"mapping task {col_name}/{dim}: {err_str[:120]}")

    return {
        "rules_created":          rules_created,
        "occurrences_registered": occurrences,
        "mapping_tasks_created":  mapping_tasks_created,
        "errors":                 errors,
        "summary": {
            "rule_count":         len(rules_created),
            "occurrence_count":   len(occurrences),
            "mapping_task_count": len(mapping_tasks_created),
            "deleted_count":      deleted_count,
            "error_count":        len(errors),
        },
    }


# ---------------------------------------------------------------------------
# Data Profiling — trigger column-profile jobs + fetch results from CDGC
# ---------------------------------------------------------------------------
# Pod-specific. usw1/dmp-us tenant uses usw1-dqprofile.dmp-us.informaticacloud.com.
PROFILING_API_BASE = os.getenv(
    "PROFILING_API_BASE",
    "https://usw1-dqprofile.dmp-us.informaticacloud.com/profiling-service/api/v1",
)


# Snowflake direct-connect defaults (override per call or via .env). The
# compute_profile_from_snowflake tool reads these to bypass IDMC entirely:
# no profile-service round-trip, no CDGC propagation wait.
SNOWFLAKE_DEFAULT_ACCOUNT   = os.getenv("SNOWFLAKE_ACCOUNT",   "ygc42528.us-east-1")
SNOWFLAKE_DEFAULT_USER      = os.getenv("SNOWFLAKE_USER",      "INCEPT_AGENT_USER")
SNOWFLAKE_DEFAULT_WAREHOUSE = os.getenv("SNOWFLAKE_WAREHOUSE", "INCEPT_WH")
SNOWFLAKE_DEFAULT_DATABASE  = os.getenv("SNOWFLAKE_DATABASE",  "INCEPT_GOV_DEV")
SNOWFLAKE_DEFAULT_SCHEMA    = os.getenv("SNOWFLAKE_SCHEMA",    "DQ_TEST")


def _snowflake_connect():
    """Open a Snowflake connection using .env credentials.

    Reads SNOWFLAKE_PASSWORD from .env at call time (not module load) so
    a rotated password is picked up without restarting the server.
    """
    try:
        import snowflake.connector  # noqa: F401  (imported lazily)
    except ImportError as e:
        raise RuntimeError(
            "snowflake-connector-python not installed. "
            "pip install snowflake-connector-python"
        ) from e
    env = _read_env()
    pwd = env.get("SNOWFLAKE_PASSWORD") or os.getenv("SNOWFLAKE_PASSWORD")
    if not pwd:
        raise RuntimeError(
            "SNOWFLAKE_PASSWORD not set in .env. Add it (chmod 600 .env "
            "to keep the file owner-only readable) before calling "
            "compute_profile_from_snowflake."
        )
    return snowflake.connector.connect(
        user=env.get("SNOWFLAKE_USER")      or SNOWFLAKE_DEFAULT_USER,
        password=pwd,
        account=env.get("SNOWFLAKE_ACCOUNT") or SNOWFLAKE_DEFAULT_ACCOUNT,
        warehouse=env.get("SNOWFLAKE_WAREHOUSE") or SNOWFLAKE_DEFAULT_WAREHOUSE,
        database=env.get("SNOWFLAKE_DATABASE")   or SNOWFLAKE_DEFAULT_DATABASE,
        schema=env.get("SNOWFLAKE_SCHEMA")       or SNOWFLAKE_DEFAULT_SCHEMA,
        client_session_keep_alive=False,
        login_timeout=30,
    )


def _quote_ident(ident: str) -> str:
    """Quote a Snowflake identifier safely."""
    return '"' + ident.replace('"', '""') + '"'


_SF_STRING_TYPES = {"TEXT","VARCHAR","STRING","CHAR","CHARACTER","NVARCHAR","NCHAR"}
_SF_DATE_TYPES   = {"DATE","TIMESTAMP","TIMESTAMP_NTZ","TIMESTAMP_LTZ","TIMESTAMP_TZ","DATETIME","TIME"}
_SF_NUMBER_TYPES = {"NUMBER","NUMERIC","DECIMAL","INT","INTEGER","BIGINT","SMALLINT","TINYINT","BYTEINT","FLOAT","DOUBLE","REAL"}


def _infer_simple_type(sf_type: str) -> str:
    t = (sf_type or "").upper().split("(")[0].strip()
    if t in _SF_STRING_TYPES: return "string"
    if t in _SF_NUMBER_TYPES: return "number"
    if t in _SF_DATE_TYPES:   return "date"
    if t in ("BOOLEAN","BOOL"): return "boolean"
    return t.lower() or "unknown"


@mcp.tool()
def compute_profile_from_snowflake(
    object_name: str,
    database: str | None = None,
    schema: str | None = None,
    columns: list[str] | None = None,
    top_n_values: int = 10,
    pattern_sample_size: int = 0,
) -> dict[str, Any]:
    """Compute column profiling stats by querying Snowflake directly.

    Bypasses IDMC entirely — no profile-service round-trip, no CDGC
    propagation wait. Returns results in the exact shape
    recommend_dq_rules expects:

      {
        "total_rows": N,
        "columns": {
          "<COL>": {
            "data_type":       "string|number|date|boolean|…",
            "snowflake_type":  "<raw column type>",
            "null_count":      int,
            "null_pct":        float,   # 0-1
            "distinct_count":  int,
            "blank_count":     int,     # strings only
            "min_value":       <native>,
            "max_value":       <native>,
            "top_values":      [{"value": v, "count": n}, …],
          }, …
        }
      }

    Connection params default to the Snowflake_InceptTest tenant settings
    in .env (SNOWFLAKE_ACCOUNT, USER, PASSWORD, WAREHOUSE, DATABASE,
    SCHEMA). Override database/schema per call when profiling a table in
    a different location.

    Args:
      object_name:        Table name. Case-sensitive (Snowflake stores
                          unquoted identifiers uppercase).
      database, schema:   Override the .env defaults for this call.
      columns:            Optional explicit column subset. When None, the
                          tool queries INFORMATION_SCHEMA for all columns.
      top_n_values:       Per-column top-N value frequency depth (0 to
                          disable). Heavy on wide tables — keep small.
      pattern_sample_size: Reserved. When > 0, sample N rows and derive
                          REGEXP_REPLACE-style pattern distributions.
                          Not implemented in this version; placeholder.

    Returns: profile_results dict (above) ready to pass straight into
    recommend_dq_rules.

    Raises: RuntimeError when SNOWFLAKE_PASSWORD is missing or the
    connection / queries fail.
    """
    db = database or SNOWFLAKE_DEFAULT_DATABASE
    sc = schema   or SNOWFLAKE_DEFAULT_SCHEMA
    full_table = f"{_quote_ident(db)}.{_quote_ident(sc)}.{_quote_ident(object_name)}"
    log.info("compute_profile_from_snowflake: %s.%s.%s", db, sc, object_name)

    conn = _snowflake_connect()
    try:
        cur = conn.cursor()

        # 1) Discover columns + types from INFORMATION_SCHEMA.
        cur.execute(
            "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_CATALOG = %s AND TABLE_SCHEMA = %s AND TABLE_NAME = %s "
            "ORDER BY ORDINAL_POSITION",
            (db, sc, object_name),
        )
        all_cols = [(r[0], r[1]) for r in cur.fetchall()]
        if not all_cols:
            raise RuntimeError(
                f"Table {db}.{sc}.{object_name} not found in INFORMATION_SCHEMA "
                f"(check case + that the role has USAGE on database/schema)."
            )
        if columns:
            wanted = {c.upper() for c in columns}
            cols = [(n, t) for (n, t) in all_cols if n.upper() in wanted]
        else:
            cols = all_cols

        # 2) Single pass for total rows + per-column null/distinct/blank/min/max.
        # Build one aggregate query (efficient — Snowflake will compute all
        # measures in a single scan).
        agg_parts = ["COUNT(*) AS TOTAL_ROWS"]
        col_indices: dict[str, dict[str, int]] = {}
        idx = 1  # 0 = TOTAL_ROWS
        for nm, sf_t in cols:
            q = _quote_ident(nm)
            t = _infer_simple_type(sf_t)
            col_indices[nm] = {}
            agg_parts.append(f"COUNT({q}) AS NN_{idx}");                 col_indices[nm]["non_null"] = idx; idx += 1
            agg_parts.append(f"COUNT(DISTINCT {q}) AS D_{idx}");          col_indices[nm]["distinct"] = idx; idx += 1
            if t in ("string", "number", "date"):
                agg_parts.append(f"MIN({q}) AS MN_{idx}");                col_indices[nm]["min"] = idx; idx += 1
                agg_parts.append(f"MAX({q}) AS MX_{idx}");                col_indices[nm]["max"] = idx; idx += 1
            if t == "string":
                agg_parts.append(f"COUNT_IF(TRIM({q}) = '') AS B_{idx}"); col_indices[nm]["blank"] = idx; idx += 1
        agg_sql = "SELECT " + ", ".join(agg_parts) + f" FROM {full_table}"
        log.info("compute_profile: aggregate SQL has %d measures", len(agg_parts))
        cur.execute(agg_sql)
        row = cur.fetchone()
        total_rows = int(row[0] or 0)

        # 3) Per-column top-N value frequencies (one query each — small).
        result_columns: dict[str, Any] = {}
        for nm, sf_t in cols:
            idxs = col_indices[nm]
            non_null = int(row[idxs["non_null"]] or 0)
            null_count = total_rows - non_null
            distinct  = int(row[idxs["distinct"]] or 0)
            t = _infer_simple_type(sf_t)
            entry: dict[str, Any] = {
                "data_type":      t,
                "snowflake_type": sf_t,
                "null_count":     null_count,
                "null_pct":       (null_count / total_rows) if total_rows else 0.0,
                "distinct_count": distinct,
            }
            if "min" in idxs:
                v = row[idxs["min"]]
                entry["min_value"] = v.isoformat() if hasattr(v, "isoformat") else v
            if "max" in idxs:
                v = row[idxs["max"]]
                entry["max_value"] = v.isoformat() if hasattr(v, "isoformat") else v
            if "blank" in idxs:
                entry["blank_count"] = int(row[idxs["blank"]] or 0)

            if top_n_values > 0:
                q = _quote_ident(nm)
                cur.execute(
                    f"SELECT {q} AS VAL, COUNT(*) AS CNT FROM {full_table} "
                    f"WHERE {q} IS NOT NULL "
                    f"GROUP BY {q} ORDER BY CNT DESC NULLS LAST, VAL "
                    f"LIMIT {int(top_n_values)}"
                )
                tops = []
                for v, n in cur.fetchall():
                    tops.append({
                        "value": v.isoformat() if hasattr(v, "isoformat") else v,
                        "count": int(n),
                    })
                entry["top_values"] = tops

            result_columns[nm] = entry

        cur.close()
    finally:
        conn.close()

    return {
        "total_rows":     total_rows,
        "columns":        result_columns,
        "source": {
            "engine":   "snowflake",
            "database": db,
            "schema":   sc,
            "object":   object_name,
        },
    }


def _v2_connection_federated_id(v2_id: str) -> str:
    """Translate a v2 connection id (010...) to its FRS/federatedId form.

    The profiling service expects FRS-style asset ids on connectionId, not
    the v2 internal id. v2 /api/v2/connection/{id} carries the back-reference
    in the ``federatedId`` field.
    """
    r = _request_v2("GET", f"/api/v2/connection/{v2_id}")
    if r.status_code != 200:
        raise RuntimeError(f"connection lookup HTTP {r.status_code}: {r.text[:300]}")
    fed = (r.json() or {}).get("federatedId")
    if not fed:
        raise RuntimeError(f"v2 connection {v2_id} has no federatedId")
    return fed


def _v2_runtime_federated_id(v2_id: str) -> str:
    """Translate a v2 runtime env id (010YK225...) to its FRS/federatedId.

    The profiling service validates runtimeEnvironmentId against FRS DocRefs
    and returns FRS_143 ("Invalid DocRefsIds:[[010YK2…]]") when handed a
    v2 id. /api/v2/runtimeEnvironment lists every env with its federatedId.
    Pass-through when the supplied id already looks federated (22-char
    base62) so callers can supply either form.
    """
    if not v2_id:
        return v2_id
    if re.match(r"^[A-Za-z0-9]{22}$", v2_id):
        return v2_id  # already federated form
    r = _request_v2("GET", "/api/v2/runtimeEnvironment")
    if r.status_code != 200:
        raise RuntimeError(f"runtime env lookup HTTP {r.status_code}: {r.text[:300]}")
    for it in (r.json() or []):
        if it.get("id") == v2_id:
            fed = it.get("federatedId")
            if not fed:
                raise RuntimeError(f"v2 runtime env {v2_id} has no federatedId")
            return fed
    raise RuntimeError(f"v2 runtime env {v2_id} not found in /api/v2/runtimeEnvironment")


def _find_existing_profile(connection_federated_id: str, object_name: str) -> dict[str, Any] | None:
    """Find a profile matching this (connectionId, source.name) in the org.

    The profiling service stores profile definitions with their source schema
    baked in. We list all profiles and match on connectionId; for any hit we
    GET the full body to read source.name and compare.

    Returns the full profile dict on match, or None.
    """
    r = _request("GET", f"{PROFILING_API_BASE}/profile")
    if r.status_code != 200:
        raise RuntimeError(f"list profiles HTTP {r.status_code}: {r.text[:300]}")
    candidates = [p for p in (r.json() or []) if p.get("connectionId") == connection_federated_id]
    for stub in candidates:
        det = _request("GET", f"{PROFILING_API_BASE}/profile/{stub['id']}")
        if det.status_code != 200:
            continue
        body = det.json() or {}
        if (body.get("source") or {}).get("name") == object_name:
            return body
    return None


# Maps a v2 connection.type code to (dataSourceType, default pcType) for the
# profile body. Verified empirically against an existing PERSON profile —
# Snowflake (TOOLKIT_CCI subtype "snowflakev2") writes "TOOLKIT_CCI" in the
# profile's source.dataSourceType and "NSTRING"/"DECIMAL" pcTypes.
_PROFILE_SOURCE_TYPE_MAP = {
    "TOOLKIT_CCI":   "TOOLKIT_CCI",
    "TOOLKIT":       "TOOLKIT",
    "Oracle":        "Oracle",
    "SqlServer2019": "SqlServer2019",
    "MySQL":         "MySQL",
    "Salesforce":    "Salesforce",
    "CSVFile":       "CSVFile",
    "ODBC":          "ODBC",
}


def _pc_type_for(data_type: str) -> str:
    """Best-effort SQL data_type → IDMC pcType mapping for profile fields."""
    dt = (data_type or "").lower()
    if dt in ("varchar", "string", "char", "text", "nvarchar", "nchar"):
        return "NSTRING"
    if dt in ("number", "numeric", "decimal", "int", "integer", "bigint", "smallint", "tinyint"):
        return "DECIMAL"
    if dt in ("float", "double", "real"):
        return "DOUBLE"
    if dt in ("date",):
        return "DATE"
    if dt in ("timestamp", "datetime", "datetime2", "timestamptz"):
        return "DATETIME"
    if dt in ("boolean", "bool", "bit"):
        return "STRING"
    return "NSTRING"


def _default_precision_for(data_type: str) -> int:
    """Sensible per-type precision when caller doesn't specify one.

    The profiling service rejects 255 for DATE columns with PROFILE_MDL_00006
    ("PowerCenter data type DATE … precision 29 not supported"). Each type
    has a canonical precision the engine accepts.
    """
    dt = (data_type or "").lower()
    if dt == "date":                                                return 19
    if dt in ("timestamp","datetime","datetime2","timestamptz"):    return 26
    if dt in ("number","numeric","decimal","int","integer","bigint"): return 38
    if dt in ("float","double","real"):                             return 15
    if dt in ("smallint","tinyint"):                                return 5
    if dt in ("boolean","bool","bit"):                              return 1
    return 255  # varchar / string / catch-all


def _split_object_path(object_name: str) -> tuple[str, str]:
    """Split "DB/SCHEMA/TABLE" or "DB.SCHEMA.TABLE" into (sourcePath, table).

    Returns ("DB/SCHEMA", "TABLE") when a multi-segment name is supplied, or
    ("", "<table>") for a bare table name.
    """
    parts = re.split(r"[./]", object_name)
    parts = [p for p in parts if p]
    if len(parts) >= 2:
        return "/".join(parts[:-1]), parts[-1]
    return "", parts[-1] if parts else object_name


def _cdgc_columns_for_table(table_name: str) -> list[dict[str, Any]] | None:
    """Best-effort: search CDGC for columns belonging to the named table.

    Returns a list of {name, dataType, precision, scale} dicts, or None when
    nothing usable comes back. Used as a fallback when columns aren't passed
    explicitly and the v2 connection-fields endpoint isn't available (which
    it isn't for Snowflake on this tenant — the Secure Agent lacks the
    connector service).
    """
    from urllib.parse import quote
    url = (f"{CDGC_API_BASE}/data360/search/v1/assets"
           f"?knowledgeQuery={quote(table_name)}&segments=summary,systemAttributes")
    r = _request_cdgc("POST", url, json={"from": 0, "size": 100})
    if r.status_code >= 400:
        return None
    hits = (r.json() or {}).get("hits") or []
    if isinstance(hits, dict):
        hits = hits.get("hits") or []
    cols: list[dict[str, Any]] = []
    for h in hits:
        cls = ((h.get("systemAttributes") or {}).get("core.classType") or "")
        if "Column" not in cls:
            continue
        loc = (h.get("summary") or {}).get("core.location") or ""
        if table_name not in loc:
            continue
        nm = (h.get("summary") or {}).get("core.name")
        if not nm:
            continue
        cols.append({
            "name":      nm,
            "dataType":  "varchar",
            "precision": 255,
            "scale":     0,
        })
    return cols or None


@mcp.tool()
def create_profile(
    connection_id: str,
    object_name: str,
    runtime_environment_id: str,
    columns: list[dict[str, Any]] | None = None,
    profile_name: str | None = None,
    description: str = "",
    project_id: str | None = None,
    folder_id: str | None = None,
    auto_run: bool = False,
) -> dict[str, Any]:
    """Create a Data Profiling profile definition via the profiling-service API.

    Wraps POST /profiling-service/api/v1/profile. The body shape was
    reverse-engineered from a GET on an existing profile (the create body is
    not documented in the public PDF; the manual points to a separate
    "Getting Started with Cloud Data Profiling REST API" guide we don't have).

    Required schema essentials (PROFILE_MDL_00004 if missing):
      - source.fields[]      — full column metadata for the source object
      - profileableFields[]  — sub-list of fields to actually profile
      - source.properties.sourcePath — "<DB>/<SCHEMA>"
      - source.dataSourceType derived from the v2 connection's type code

    Args:
      connection_id:          v2 connection id (e.g. Snowflake_InceptTest
                              "010YK20B000000000044"). Auto-translated to the
                              FRS federatedId the profiling service expects.
      object_name:            Source object — accepts either a bare table
                              name ("CUSTOMER_POSITIONS") or a fully-qualified
                              "<DB>/<SCHEMA>/<TABLE>". When fully-qualified,
                              the DB/SCHEMA become the source.properties.sourcePath.
      runtime_environment_id: v2 runtime env id (Secure Agent / serverless).
      columns:                Optional explicit column list:
                              [{"name":"customer_name","dataType":"varchar",
                                "precision":255,"scale":0}, ...].
                              When omitted, falls back to CDGC search (works
                              if the table is cataloged); raises a clear
                              error when neither path yields columns. The
                              v2 connection /fields endpoint is NOT used —
                              it 403s on Snowflake on this tenant because
                              the Secure Agent lacks the connector service
                              version Profiling expects.
      profile_name:           Default "profile_<table>_<timestamp>".
      description:            Free text.
      project_id, folder_id:  Optional FRS project / folder GUIDs (where
                              the profile asset lands in IDMC). Default
                              folder if both omitted.
      auto_run:               When True, POST /profile/{id}/execute right
                              after create. Useful for "create + run + watch
                              from a single tool call" flows.

    Returns: {profile_id, profile_name, connection_id, object_name,
              column_count, columns_source, profile_run_id, profile_job_id,
              status_url, http_status}.
    """
    fed = _v2_connection_federated_id(connection_id)

    # Look up the connection's type so we can set source.dataSourceType.
    cr = _request_v2("GET", f"/api/v2/connection/{connection_id}")
    if cr.status_code != 200:
        raise RuntimeError(f"connection lookup HTTP {cr.status_code}: {cr.text[:300]}")
    cj = cr.json() or {}
    conn_type   = cj.get("type") or cj.get("connType") or "TOOLKIT_CCI"
    data_source_type = _PROFILE_SOURCE_TYPE_MAP.get(conn_type, conn_type)

    source_path, table_name = _split_object_path(object_name)

    # Resolve column list.
    columns_source = "explicit"
    if not columns:
        cdgc_cols = _cdgc_columns_for_table(table_name)
        if cdgc_cols:
            columns = cdgc_cols
            columns_source = "cdgc"
        else:
            raise RuntimeError(
                f"No columns provided for {object_name!r} and CDGC search returned "
                f"none. Pass columns=[{{'name':'<col>','dataType':'<sql_type>',"
                f"'precision':<int>,'scale':<int>}}, ...] explicitly."
            )

    # Build source.fields and profileableFields. id MUST be present in the
    # JSON but explicitly null — Hibernate keys "transient" off id-is-null;
    # omitting the field produces a 400, supplying any UUID makes Hibernate
    # try UPDATE (PROFILE_SVC_00004 "unsaved-value mapping was incorrect").
    src_fields: list[dict[str, Any]] = []
    pf_fields:  list[dict[str, Any]] = []
    for i, c in enumerate(columns):
        nm = c.get("name")
        if not nm:
            continue
        dt = c.get("dataType") or "varchar"
        prec = int(c["precision"]) if c.get("precision") not in (None, "") else _default_precision_for(dt)
        sc   = int(c.get("scale") or 0)
        pct  = c.get("pcType") or _pc_type_for(dt)
        src_fields.append({
            "id":                 None,
            "name":               nm,
            "dataType":           dt,
            "precision":          prec,
            "scale":              sc,
            "pcType":             pct,
            "order":              i,
            "isDeleted":          False,
            "isMetadataUpdated":  False,
        })
        pf_fields.append({
            "id":         None,
            "isDeleted":  False,
            "appliedBy":  "USER",
            "columnKey":  10000 + i,
            "sourceName": table_name,
            "fieldName":  nm,
            "precision":  prec,
            "scale":      sc,
            "fieldType":  "DATASOURCEFIELD",
        })

    pname = profile_name or f"profile_{table_name}_{int(time.time()) % 100000}"

    # Translate runtime env from v2 native id to FRS federatedId. The
    # profiling service validates runtime via FRS DocRefs and rejects v2
    # ids with FRS_143. Pass-through if caller already supplied federated.
    runtime_fed = _v2_runtime_federated_id(runtime_environment_id) if runtime_environment_id else None

    body: dict[str, Any] = {
        "name":            pname,
        "description":     description,
        "connectionId":    fed,
        "profileType":     "COLUMN_PROFILE",
        "drillDownType":   "ON",
        "isFilterEnabled": False,
        "samplingOptions": {"id": None, "rows": -1, "samplingType": "ALL_ROWS"},
        "runtimeOptions": {
            "id":                       None,
            "scheduleId":               None,
            "runtimeEnvironmentId":     runtime_fed,
            "defaultEmailNotification": True,
            "profileAdvProps": {
                "inferDateTime":              True,
                "detectOutliers":             True,
                "enableClaireAnomalyDetection": False,
                "tracingLevel":               "NORMAL",
            },
        },
        "source": {
            "id":               None,
            "name":             table_name,
            "fields":           src_fields,
            "dataSourceType":   data_source_type,
            "properties": {
                "dataSourceType": data_source_type,
                "sourcePath":     source_path,
            },
            "advancedOptions": {
                "Database":   "", "Role":   "", "Schema": "",
                "Table Name": "", "Warehouse": "",
            },
            "sourceType": "DATASOURCE",
        },
        "profileableFields": pf_fields,
    }
    if project_id: body["frsProjectId"] = project_id
    if folder_id:  body["frsFolderId"]  = folder_id

    r = _request("POST", f"{PROFILING_API_BASE}/profile", json=body)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"create_profile HTTP {r.status_code}: {r.text[:600]}")
    try:
        created = r.json() if r.text else {}
    except (ValueError, json.JSONDecodeError):
        created = {}
    profile_id = created.get("id")
    if not profile_id:
        # Some 200/201 responses come back without the new id in the body
        # (or with non-JSON). Fall back to a name-based lookup.
        log.info("create_profile response had no id (status=%s text=%r); "
                 "looking up by name", r.status_code, r.text[:200])
        list_r = _request("GET", f"{PROFILING_API_BASE}/profile")
        if list_r.status_code == 200:
            for p in (list_r.json() or []):
                if p.get("name") == pname:
                    profile_id = p.get("id")
                    created = p
                    break
        if not profile_id:
            raise RuntimeError(
                f"create_profile HTTP {r.status_code} returned no id and "
                f"name lookup found nothing. body: {r.text[:300]}"
            )

    out: dict[str, Any] = {
        "profile_id":       profile_id,
        "profile_name":     created.get("name") or pname,
        "connection_id":    connection_id,
        "object_name":      object_name,
        "source_path":      source_path,
        "table_name":       table_name,
        "column_count":     len(src_fields),
        "columns_source":   columns_source,
        "data_source_type": data_source_type,
        "http_status":      r.status_code,
    }

    if auto_run:
        rr = _request("POST", f"{PROFILING_API_BASE}/profile/{profile_id}/execute")
        if rr.status_code != 200:
            out["execute_error"] = f"HTTP {rr.status_code}: {rr.text[:300]}"
        else:
            res = rr.json() or {}
            out["profile_run_id"] = res.get("profileRunId")
            out["profile_job_id"] = res.get("profileJobId")
            out["status_url"]     = f"{PROFILING_API_BASE}/job/{out['profile_job_id']}"
    return out


@mcp.tool()
def run_profile(
    connection_id: str,
    object_name: str,
    runtime_environment_id: str,
) -> dict[str, Any]:
    """Trigger a column-profile job on a source table.

    The profiling service stores profile *definitions* (which columns, what
    sampling, what runtime) separately from *runs*. This tool finds an
    existing definition for the given (connection, object) and runs it; if
    none exists, it surfaces a clear error pointing the caller at the IDMC
    UI to define one.

    Flow:
      1. Translate v2 connection_id → federatedId (FRS form) — required by
         the profiling service.
      2. Search existing profiles for (connectionId, source.name) match.
      3. If runtime_environment_id is set and differs from the stored value,
         PATCH the profile's runtimeOptions to use it.
      4. POST /profile/{id}/execute to start the run.

    Args:
      connection_id:          v2 connection id (e.g. Snowflake_InceptTest
                              "010YK20B000000000044").
      object_name:            Source table / object name as recorded on the
                              profile's source.name (e.g. "CUSTOMER_POSITIONS").
      runtime_environment_id: v2 runtime env id (e.g. BenakaHomePC). Used
                              to override the profile's saved runtime when
                              it differs.

    Returns: {profile_id, profile_run_id, profile_job_id, status_url}.

    Raises: RuntimeError when no matching profile exists in the org. Define
            one in IDMC Data Profiling first (Profiling needs an explicit
            column list which can't be auto-discovered through the v2 API
            on Snowflake connections).
    """
    fed = _v2_connection_federated_id(connection_id)
    profile = _find_existing_profile(fed, object_name)
    if not profile:
        raise RuntimeError(
            f"No profile defined for connection={connection_id} "
            f"(federatedId={fed}) + object={object_name!r}. Create one in "
            f"IDMC → Data Profiling → New Profile first; the v2 Snowflake "
            f"connector's metadata-fetch endpoint can't enumerate columns "
            f"for an unattended auto-define."
        )
    profile_id = profile["id"]

    # Override the saved runtime env when the caller specifies a different one.
    # Best-effort: a 5xx here usually means the requested agent group lacks the
    # connector service this profile needs (e.g. Snowflake connector v20+). We
    # log and fall through to running with the saved runtime rather than
    # aborting — running with a known-good runtime beats failing the whole call.
    saved_runtime = (profile.get("runtimeOptions") or {}).get("runtimeEnvironmentId")
    # The profile entity stores runtimeEnvironmentId as the FRS federatedId,
    # not the v2 native id. Translate the caller's v2 id before comparing /
    # PATCHing or the server returns FRS_070 ("Document Artifact ... not
    # found") on the saved v2-id value.
    runtime_fed = (_v2_runtime_federated_id(runtime_environment_id)
                   if runtime_environment_id else None)
    runtime_override_status = "unchanged"
    if runtime_fed and runtime_fed != saved_runtime:
        log.info("patching profile %s runtimeEnvironmentId %s → %s",
                 profile_id, saved_runtime, runtime_fed)
        updated = dict(profile)
        updated.setdefault("runtimeOptions", {})["runtimeEnvironmentId"] = runtime_fed
        r = _request("PUT", f"{PROFILING_API_BASE}/profile/{profile_id}", json=updated)
        if r.status_code in (200, 204):
            runtime_override_status = f"applied ({saved_runtime!r} → {runtime_fed!r})"
        else:
            log.warning("runtime override failed HTTP %s; running with saved runtime %s",
                        r.status_code, saved_runtime)
            runtime_override_status = (
                f"rejected (HTTP {r.status_code}); running with saved runtime "
                f"{saved_runtime!r}. body: {r.text[:200]}"
            )

    r = _request("POST", f"{PROFILING_API_BASE}/profile/{profile_id}/execute")
    if r.status_code != 200:
        raise RuntimeError(f"execute profile HTTP {r.status_code}: {r.text[:300]}")
    res = r.json() or {}
    profile_run_id = res.get("profileRunId")
    profile_job_id = res.get("profileJobId")
    if not profile_job_id:
        raise RuntimeError(f"execute profile response missing profileJobId: {res}")
    return {
        "profile_id":              profile_id,
        "profile_name":            profile.get("name"),
        "profile_run_id":          profile_run_id,
        "profile_job_id":          profile_job_id,
        "status_url":              f"{PROFILING_API_BASE}/job/{profile_job_id}",
        "object_name":             object_name,
        "connection_id":           connection_id,
        "runtime_override_status": runtime_override_status,
    }


def _cdgc_find_asset_by_name(name: str, class_type_substr: str | None = None) -> dict[str, Any] | None:
    """Search CDGC for the most likely match by name.

    When class_type_substr is set (e.g. "Table"), only return a hit whose
    core.classType contains it case-insensitively. The catalog often has
    multiple assets sharing a name (rule spec, mapping, column, table);
    the class filter disambiguates.
    """
    from urllib.parse import quote
    url = (f"{CDGC_API_BASE}/data360/search/v1/assets"
           f"?knowledgeQuery={quote(name)}&segments=summary,systemAttributes")
    r = _request_cdgc("POST", url, json={"from": 0, "size": 25})
    if r.status_code >= 400:
        raise RuntimeError(f"CDGC search HTTP {r.status_code}: {r.text[:300]}")
    hits = (r.json() or {}).get("hits") or []
    if isinstance(hits, dict):
        hits = hits.get("hits") or []
    def cls_of(h: dict[str, Any]) -> str:
        return (h.get("systemAttributes") or {}).get("core.classType") or ""
    if class_type_substr:
        filt = [h for h in hits if class_type_substr.lower() in cls_of(h).lower()]
        return filt[0] if filt else None
    # Prefer exact-name match if multiple hits
    name_lc = name.lower()
    exact = [h for h in hits if ((h.get("summary") or {}).get("core.name") or "").lower() == name_lc]
    return (exact or hits)[0] if (exact or hits) else None


def _cdgc_child_columns(table_identity: str) -> list[dict[str, Any]]:
    """Return immediate children of a table asset (its columns) from CDGC."""
    url = (f"{CDGC_API_BASE}/data360/search/v1/assets/{table_identity}"
           f"?scheme=internal&segments=summary,hierarchy")
    r = _request_cdgc("GET", url)
    if r.status_code >= 400:
        raise RuntimeError(f"CDGC hierarchy HTTP {r.status_code}: {r.text[:300]}")
    j = r.json() or {}
    hierarchy = j.get("hierarchy") or []
    if isinstance(hierarchy, dict):
        hierarchy = hierarchy.get("children") or hierarchy.get("items") or []
    return hierarchy


def _cdgc_column_profile(column_identity: str) -> dict[str, Any]:
    """Fetch dataProfile + summary segments for one column asset."""
    url = (f"{CDGC_API_BASE}/data360/search/v1/assets/{column_identity}"
           f"?scheme=internal&segments=summary,systemAttributes,dataProfile")
    r = _request_cdgc("GET", url)
    if r.status_code >= 400:
        return {"_error": f"HTTP {r.status_code}: {r.text[:200]}"}
    return r.json() or {}


@mcp.tool()
def get_profile_results_direct(
    profile_id: str | None = None,
    profile_name: str | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Read what the profiling-service public API exposes about a profile.

    Bypasses CDGC entirely (unlike get_profile_results). Hits the
    /profiling-service/api/v1/ public surface only — IDS-SESSION-ID auth.

    **Important — column-level stats are NOT returned.** The profiling-
    service public API exposes only definition + job-lifecycle data:

      • /profile/{id}              — profile definition
      • /job/{jobId}               — job status + jobSteps (timing,
                                     errors) but no null counts /
                                     distinct counts / patterns

    Column statistics live at /profiling-service/internal/api/v1/
    columnProfile/{profileKey}/{runKey} which returns 401 to every user-
    auth variant (Bearer JWT, IDS-SESSION-ID, Cookie). That's a
    service-to-service endpoint used by the Profiling UI's backend. The
    only public way to read column stats is via CDGC (see
    get_profile_results), which requires the table to be cataloged.

    What this tool DOES give you:
      • Whether the profile ran at all and when (start/end timestamps)
      • The job's terminal status (RUNNING / COMPLETED / FAILED / etc.)
      • Per-step status and timing — which DTM step failed and why
      • The error message from a failed Secure Agent step

    Resolution flow:
      1. Resolve a profile_id from (profile_name, profile_id, or job_id).
      2. Re-fetch the profile to get its current lastRunKey + lastJob ref.
      3. Probe /job/{jobId} for status. (job_id arg short-circuits this.)

    Args:
      profile_id:   UUID of the profile (preferred). Either this or
                    profile_name is required when job_id isn't given.
      profile_name: Lookup by name. Used when profile_id is omitted.
      job_id:       Skip profile lookup and fetch a specific job by UUID.

    Returns: {profile:{id,name,profileKey,lastRunKey}, job:{id,name,type,
              status,startTime,endTime,errorMessage,jobSteps:[{name,
              jobStepType,sequence,status,startTime,endTime,errorMessage}]},
              note}.
    """
    note = ("Column statistics (null_pct, distinct_count, patterns, etc.) "
            "are NOT available from the profiling-service public API. They "
            "live in /internal/api/v1/columnProfile (service-token auth) "
            "or in CDGC after async propagation. Use get_profile_results "
            "(CDGC-backed) for column stats once the table is cataloged.")
    out: dict[str, Any] = {"note": note}

    # Resolve profile
    profile: dict[str, Any] | None = None
    if profile_id:
        r = _request("GET", f"{PROFILING_API_BASE}/profile/{profile_id}")
        if r.status_code == 200:
            profile = r.json() or {}
    elif profile_name:
        r = _request("GET", f"{PROFILING_API_BASE}/profile")
        if r.status_code != 200:
            raise RuntimeError(f"list profiles HTTP {r.status_code}: {r.text[:300]}")
        for stub in (r.json() or []):
            if stub.get("name") == profile_name:
                pid = stub.get("id")
                if pid:
                    pr = _request("GET", f"{PROFILING_API_BASE}/profile/{pid}")
                    if pr.status_code == 200:
                        profile = pr.json() or {}
                break
        if not profile and not job_id:
            raise RuntimeError(f"No profile found with name={profile_name!r}")
    elif not job_id:
        raise RuntimeError("Pass profile_id, profile_name, or job_id.")

    if profile:
        out["profile"] = {
            "id":          profile.get("id"),
            "name":        profile.get("name"),
            "profileKey":  profile.get("profileKey"),
            "lastRunKey":  profile.get("lastRunKey"),
            "connectionId": profile.get("connectionId"),
            "source_name":  (profile.get("source") or {}).get("name"),
        }

    # Probe the job. Without a job_id we can't resolve one from profile
    # alone — the public API doesn't expose a "list jobs for profile"
    # endpoint (we tried /job?profileId=…, ?profileKey=…, returns 500).
    if not job_id:
        out["job"] = None
        out["job_lookup_note"] = (
            "No job_id supplied and the profiling-service public API has "
            "no documented way to list jobs by profile. Pass job_id from a "
            "prior create_profile(auto_run=True) or run_profile call to "
            "see job status."
        )
        return out

    r = _request("GET", f"{PROFILING_API_BASE}/job/{job_id}")
    if r.status_code != 200:
        raise RuntimeError(f"get job HTTP {r.status_code}: {r.text[:300]}")
    job = r.json() or {}
    steps = job.get("jobSteps") or []
    out["job"] = {
        "id":           job.get("id"),
        "name":         job.get("name"),
        "type":         job.get("type"),
        "status":       job.get("status"),
        "errorMessage": job.get("errorMessage"),
        "startTime":    job.get("startTime"),
        "endTime":      job.get("endTime"),
        "jobSteps": [{
            "sequence":     s.get("sequence"),
            "name":         s.get("name"),
            "jobStepType":  s.get("jobStepType"),
            "status":       s.get("status"),
            "startTime":    s.get("startTime"),
            "endTime":      s.get("endTime"),
            "errorMessage": s.get("errorMessage"),
        } for s in steps],
    }
    return out


@mcp.tool()
def get_profile_results(object_name: str) -> dict[str, Any]:
    """Fetch completed column profile statistics for a catalogued asset.

    Lookup flow:
      1. CDGC search for the asset by name (class filter "Table"); resolve
         core.identity.
      2. Pull the table's immediate children (columns) via segments=hierarchy.
      3. For each column, GET the dataProfile segment (null counts, distinct
         counts, min/max, patterns, value distributions).

    Stats only appear on assets that have been *catalogued* AND *profiled*.
    A profile run via run_profile publishes stats back to CDGC asynchronously;
    expect a few minutes between profile completion and appearance here.

    Args:
      object_name: Table / object name (e.g. "CUSTOMER_POSITIONS"). Matched
                   against CDGC core.name with a class-type filter to disambiguate
                   from same-named columns/rules.

    Returns: {asset:{id,name,classType,location}, column_count,
              profiled_count, columns:{col_name: stats_dict, ...}}.

      stats_dict keys depend on what the profile produced; common ones:
        data_type, precision, scale, null_count, null_pct, distinct_count,
        distinct_pct, min_value, max_value, top_values, patterns,
        avg_length, blank_count.

    Raises: RuntimeError if the table can't be located in CDGC.
    """
    table = _cdgc_find_asset_by_name(object_name, class_type_substr="Table")
    if not table:
        raise RuntimeError(
            f"No table asset found in CDGC for name={object_name!r}. "
            f"Ensure the catalog has harvested this source (CDGC → Catalog "
            f"Sources → run a scan)."
        )
    table_id = table.get("core.identity")
    if not table_id:
        raise RuntimeError(f"CDGC asset for {object_name!r} missing core.identity: {table}")

    children = _cdgc_child_columns(table_id)
    columns: dict[str, Any] = {}
    profiled_count = 0
    for child in children:
        # children entries are CDGC asset stubs with core.identity + core.name
        cid = child.get("core.identity") or child.get("id") or (child.get("summary") or {}).get("core.identity")
        cname = (child.get("summary") or {}).get("core.name") or child.get("core.name") or child.get("name")
        if not cid or not cname:
            continue
        # Class lives in systemAttributes (not requested in the hierarchy call to keep
        # response small) or in the trailing "~<fqcn>" of externalId. Skip non-columns
        # by FQCN suffix so indexes/constraints don't generate per-column GETs.
        ext = child.get("core.externalId") or ""
        fqcn = ext.rsplit("~", 1)[-1] if "~" in ext else ""
        if fqcn and "Column" not in fqcn and "Field" not in fqcn:
            continue
        detail = _cdgc_column_profile(cid)
        profile = detail.get("dataProfile") or detail.get("profileStatistics")
        sys_attrs = detail.get("systemAttributes") or {}
        col_stats: dict[str, Any] = {
            "id":        cid,
            "data_type": sys_attrs.get("core.dataType") or sys_attrs.get("dataType"),
        }
        if profile:
            # Normalize the profile dict keys to flatter names where possible.
            col_stats.update(profile)
            profiled_count += 1
        else:
            col_stats["_no_profile_data"] = True
        columns[cname] = col_stats

    return {
        "asset": {
            "id":         table_id,
            "name":       (table.get("summary") or {}).get("core.name") or object_name,
            "classType":  (table.get("systemAttributes") or {}).get("core.classType"),
            "location":   (table.get("summary") or {}).get("core.location"),
            "externalId": table.get("core.externalId"),
        },
        "column_count":   len(columns),
        "profiled_count": profiled_count,
        "columns":        columns,
    }


# ---------------------------------------------------------------------------
# recommend_dq_rules — pure local analysis of profile stats → rule templates
# ---------------------------------------------------------------------------
_RECOMMENDATION_CONFIG_PATH = REPO_ROOT / "examples" / "profiling-rule-mapping.json"


def _load_recommendation_config() -> dict[str, Any]:
    """Read examples/profiling-rule-mapping.json. Cached per-call (cheap)."""
    return json.loads(_RECOMMENDATION_CONFIG_PATH.read_text())


def _severity_for(affected: int, total: int, cfg: dict[str, Any]) -> str:
    """HIGH / MEDIUM / LOW based on affected/total ratio."""
    if not total or affected <= 0:
        return "LOW"
    ratio = affected / total
    sev = cfg.get("severity", {})
    if ratio >= float(sev.get("high_pct", 0.10)):
        return "HIGH"
    if ratio >= float(sev.get("medium_pct", 0.01)):
        return "MEDIUM"
    return "LOW"


def _column_type(stats: dict[str, Any]) -> str:
    return str(stats.get("data_type") or stats.get("type") or "").lower()


def _type_matches(stats: dict[str, Any], allowed: list[str] | None) -> bool:
    """True if the column's declared type matches one of the allowed prefixes,
    OR if no type is declared (we don't reject — we trust the trigger stat)."""
    if not allowed:
        return True
    t = _column_type(stats)
    if not t:
        return True
    return any(t.startswith(a) for a in allowed)


def _rule_name(prefix: str, column: str, suffix: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", column).strip("_").upper()
    return f"{prefix}_{safe}_{suffix}"


def _check_nulls(col: str, stats: dict[str, Any], total: int,
                 cfg: dict[str, Any], prefix: str) -> dict[str, Any] | None:
    rule = cfg["checks"]["null_check"]
    null_count = int(stats.get("null_count") or 0)
    null_pct = stats.get("null_pct")
    if null_pct is None and total:
        null_pct = null_count / total
    null_pct = float(null_pct or 0)
    if null_pct <= float(rule.get("min_null_pct", 0.0)) and null_count == 0:
        return None
    if null_count == 0:
        return None
    return {
        "column_name":         col,
        "dimension":           rule["dimension"],
        "rule_template":       rule["rule_template"],
        "suggested_rule_name": _rule_name(prefix, col, rule["name_suffix"]),
        "rationale":           f"{col} has {null_pct*100:.1f}% nulls ({null_count} / {total} rows)",
        "severity":            _severity_for(null_count, total, cfg),
        "affected_rows":       null_count,
    }


def _check_blanks(col: str, stats: dict[str, Any], total: int,
                  cfg: dict[str, Any], prefix: str) -> dict[str, Any] | None:
    rule = cfg["checks"]["blank_string_check"]
    if not _type_matches(stats, rule.get("applies_to_types")):
        return None
    blank_count = int(stats.get("blank_count") or stats.get("empty_string_count") or 0)
    if blank_count < int(rule.get("min_blank_count", 1)):
        return None
    pct = blank_count / total if total else 0
    return {
        "column_name":         col,
        "dimension":           rule["dimension"],
        "rule_template":       rule["rule_template"],
        "suggested_rule_name": _rule_name(prefix, col, rule["name_suffix"]),
        "rationale":           f"{col} has {blank_count} blank/whitespace values ({pct*100:.2f}%)",
        "severity":            _severity_for(blank_count, total, cfg),
        "affected_rows":       blank_count,
    }


def _check_range(col: str, stats: dict[str, Any], total: int,
                 cfg: dict[str, Any], prefix: str) -> dict[str, Any] | None:
    rule = cfg["checks"]["range_check"]
    if not _type_matches(stats, rule.get("applies_to_types")):
        return None
    oor = int(stats.get("out_of_range_count") or 0)
    expected_min = stats.get("expected_min")
    expected_max = stats.get("expected_max")
    observed_min = stats.get("min")
    observed_max = stats.get("max")

    # If profile already counted violations, use that. Otherwise derive from
    # observed vs expected bounds if both are present.
    if oor < int(rule.get("min_out_of_range_count", 1)):
        if expected_min is None or expected_max is None:
            return None
        if observed_min is None or observed_max is None:
            return None
        try:
            if float(observed_min) >= float(expected_min) and float(observed_max) <= float(expected_max):
                return None
        except (TypeError, ValueError):
            return None
        oor = max(oor, 1)  # at least signal a violation exists

    bounds = ""
    if expected_min is not None and expected_max is not None:
        bounds = f" (expected {expected_min}..{expected_max}; observed {observed_min}..{observed_max})"
    return {
        "column_name":         col,
        "dimension":           rule["dimension"],
        "rule_template":       rule["rule_template"],
        "suggested_rule_name": _rule_name(prefix, col, rule["name_suffix"]),
        "rationale":           f"{col} has {oor} out-of-range values{bounds}",
        "severity":            _severity_for(oor, total, cfg),
        "affected_rows":       oor,
    }


def _check_format(col: str, stats: dict[str, Any], total: int,
                  cfg: dict[str, Any], prefix: str) -> dict[str, Any] | None:
    rule = cfg["checks"]["format_check"]
    if not _type_matches(stats, rule.get("applies_to_types")):
        return None
    patterns = stats.get("pattern_distribution") or {}
    if not isinstance(patterns, dict) or len(patterns) < int(rule.get("min_pattern_count", 2)):
        return None
    counts = sorted((int(v) for v in patterns.values()), reverse=True)
    pattern_total = sum(counts)
    if pattern_total <= 0:
        return None
    dominant_pct = counts[0] / pattern_total
    if dominant_pct >= float(rule.get("max_dominant_pattern_pct", 0.95)):
        return None
    minority = pattern_total - counts[0]
    top3 = sorted(patterns.items(), key=lambda kv: -int(kv[1]))[:3]
    pattern_summary = ", ".join(f"{p}={c}" for p, c in top3)
    return {
        "column_name":         col,
        "dimension":           rule["dimension"],
        "rule_template":       rule["rule_template"],
        "suggested_rule_name": _rule_name(prefix, col, rule["name_suffix"]),
        "rationale":           f"{col} has {len(patterns)} distinct formats (dominant {dominant_pct*100:.1f}%; {pattern_summary})",
        "severity":            _severity_for(minority, total, cfg),
        "affected_rows":       minority,
    }


def _check_timeliness(col: str, stats: dict[str, Any], total: int,
                      cfg: dict[str, Any], prefix: str) -> dict[str, Any] | None:
    rule = cfg["checks"]["timeliness_check"]
    if not _type_matches(stats, rule.get("applies_to_types")):
        return None
    future = int(stats.get("future_count") or 0)
    stale  = int(stats.get("stale_count")  or 0)
    affected = future + stale
    if affected < 1:
        return None
    parts = []
    if future: parts.append(f"{future} future dates")
    if stale:  parts.append(f"{stale} stale dates (older than {rule.get('max_stale_age_days', 365)}d)")
    return {
        "column_name":         col,
        "dimension":           rule["dimension"],
        "rule_template":       rule["rule_template"],
        "suggested_rule_name": _rule_name(prefix, col, rule["name_suffix"]),
        "rationale":           f"{col} has " + " and ".join(parts),
        "severity":            _severity_for(affected, total, cfg),
        "affected_rows":       affected,
    }


def _check_uniqueness(col: str, stats: dict[str, Any], total: int,
                      cfg: dict[str, Any], prefix: str) -> dict[str, Any] | None:
    rule = cfg["checks"]["uniqueness_check"]
    name = col.lower()
    is_id_like = (
        any(name.endswith(sfx) for sfx in rule.get("id_column_suffixes", []))
        or any(s in name for s in rule.get("id_column_substrings", []))
    )
    if not is_id_like:
        return None
    distinct = stats.get("distinct_count")
    if distinct is None or total <= 0:
        return None
    duplicates = max(0, total - int(distinct))
    if duplicates < int(rule.get("min_duplicate_count", 1)):
        return None
    return {
        "column_name":         col,
        "dimension":           rule["dimension"],
        "rule_template":       rule["rule_template"],
        "suggested_rule_name": _rule_name(prefix, col, rule["name_suffix"]),
        "rationale":           f"{col} looks ID-like but has {duplicates} duplicate values ({distinct}/{total} distinct)",
        "severity":            _severity_for(duplicates, total, cfg),
        "affected_rows":       duplicates,
    }


def _check_consistency(profile: dict[str, Any], total: int,
                       cfg: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    rule = cfg["checks"]["consistency_check"]
    pairs = profile.get("consistency_pairs") or []
    out: list[dict[str, Any]] = []
    for p in pairs:
        start = p.get("start"); end = p.get("end")
        violations = int(p.get("violation_count") or 0)
        if not (start and end):
            continue
        if violations < int(rule.get("min_pair_violations", 1)):
            continue
        pair = f"{start}_VS_{end}"
        out.append({
            "column_name":         f"{start} vs {end}",
            "related_columns":     [start, end],
            "dimension":           rule["dimension"],
            "rule_template":       rule["rule_template"],
            "suggested_rule_name": _rule_name(prefix, pair, rule["name_suffix"]),
            "rationale":           f"{end} precedes {start} in {violations} rows (end < start)",
            "severity":            _severity_for(violations, total, cfg),
            "affected_rows":       violations,
        })
    return out


@mcp.tool()
def recommend_dq_rules(
    profile_results: dict[str, Any],
    rule_name_prefix: str = "DQ",
) -> dict[str, Any]:
    """Map profiling statistics to DQ rule template recommendations.

    Pure local analysis — makes no API calls. Reads thresholds from
    examples/profiling-rule-mapping.json so the rules-of-thumb can be tuned
    without touching code.

    Expected profile_results shape (all keys optional except total_rows or
    per-column null_pct):
      {
        "total_rows": 1000,
        "columns": {
          "CUSTOMER_NAME": {
            "data_type":            "string",
            "null_count":           260,
            "null_pct":             0.26,
            "blank_count":          5,
            "distinct_count":       740,
            "pattern_distribution": {"AAA": 950, "AAAA": 50}
          },
          "EXPOSURE_AMT": {
            "data_type":            "decimal",
            "min": -50, "max": 1_000_000,
            "expected_min": 0, "expected_max": 1_000_000,
            "out_of_range_count":   3
          },
          "TRADE_DT": {
            "data_type": "date",
            "future_count": 2, "stale_count": 5
          }
        },
        "consistency_pairs": [
          {"start": "TRADE_DT", "end": "SETTLEMENT_DT", "violation_count": 2}
        ]
      }

    Args:
      profile_results:  Profiling output (per above schema). Accepts the
                        column dict at top level if "columns" is omitted.
      rule_name_prefix: Prefix for suggested_rule_name. Default "DQ".

    Returns: {total_rows, recommendation_count, recommendations:[
              {column_name, dimension, rule_template, suggested_rule_name,
               rationale, severity, affected_rows}]}.
    """
    cfg = _load_recommendation_config()
    total = int(profile_results.get("total_rows") or 0)
    columns = profile_results.get("columns")
    if not isinstance(columns, dict):
        # Accept the flat form: top-level keys are columns, excluding our
        # reserved metadata fields.
        reserved = {"total_rows", "consistency_pairs", "columns"}
        columns = {k: v for k, v in profile_results.items()
                   if k not in reserved and isinstance(v, dict)}

    recs: list[dict[str, Any]] = []
    for col, stats in columns.items():
        if not isinstance(stats, dict):
            continue
        for fn in (_check_nulls, _check_blanks, _check_range,
                   _check_format, _check_timeliness, _check_uniqueness):
            r = fn(col, stats, total, cfg, rule_name_prefix)
            if r:
                recs.append(r)

    recs.extend(_check_consistency(profile_results, total, cfg, rule_name_prefix))

    severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    recs.sort(key=lambda r: (severity_order.get(r["severity"], 3),
                             -int(r.get("affected_rows") or 0),
                             r["column_name"]))

    return {
        "total_rows":           total,
        "recommendation_count": len(recs),
        "recommendations":      recs,
    }


@mcp.tool()
def profile_and_govern(
    object_name: str,
    connection_id: str = "",
    runtime_environment_id: str = "",
    target_table: str = "",
    template_mapping_id: str = DEFAULT_DQ_TEMPLATE_MAPPING_ID,
    auto_create_rules: bool = False,
    dry_run: bool = True,
    # Two-mode operation: caller can pass profile_results / recommendations
    # directly (current) or rely on run_profile / get_profile_results when
    # those tools land in the server (future).
    profile_results: dict[str, Any] | None = None,
    recommendations: list[dict[str, Any]] | None = None,
    rule_name_prefix: str = "DQ",
    # Source/target table paths default to INCEPT_GOV_DEV/DQ_TEST/<obj>
    # which is the Snowflake_InceptTest convention. Override per call if
    # the schema/database differ.
    source_table_path: str | None = None,
    target_table_path: str | None = None,
    target_connection_id: str | None = None,
    # CDGC rule-occurrence binding. If cdgc_column_id_map[col] resolves a
    # column id, the register_in_cdgc step runs for that recommendation;
    # otherwise the step SKIPs with a clear reason. catalog_origin is
    # required for INTERNAL ids.
    cdgc_column_id_map: dict[str, str] | None = None,
    cdgc_catalog_origin: str | None = None,
    # Rule spec parent (FRS Space/Project). Override the create_dq_rules
    # defaults when posting into a different project.
    rule_space_id: str | None = None,
    rule_space_name: str | None = None,
    rule_project_id: str | None = None,
    rule_project_name: str | None = None,
    # Optional execute-and-score tail.
    run_now: bool = False,
    # Auto-create profile when no existing one matches (Mode C). Requires
    # `columns` so we can build a valid profile body for the source object.
    auto_create_profile: bool = False,
    columns: list[dict[str, Any]] | None = None,
    profile_runtime_environment_id: str | None = None,
    # Mode S — direct Snowflake profiling (the default; avoids the IDMC
    # profile-service / CDGC propagation gap entirely). Disable to fall
    # back to the IDMC paths.
    use_snowflake_direct: bool = True,
    snowflake_database: str | None = None,
    snowflake_schema: str | None = None,
    top_n_values: int = 10,
) -> dict[str, Any]:
    """End-to-end Profile → Recommend → Create → Execute → Register.

    Same error-isolation contract as run_governance_pipeline: each step
    records SUCCESS / SKIPPED / FAILED with elapsed_ms; failures don't
    abort the chain unless they invalidate every downstream step.

    Mode A (today): caller passes ``profile_results`` directly. The
    profiling step is recorded as SKIPPED with reason "profile_results
    supplied by caller".

    Mode B: the orchestrator detects run_profile/get_profile_results in
    module globals and calls them automatically. Triggers an existing
    profile definition for the (connection, object) pair.

    Mode C (auto-create): when no existing profile matches AND
    ``auto_create_profile=True`` AND ``columns`` is supplied, the
    orchestrator calls create_profile(auto_run=True) to build a profile
    on-the-fly, run it, and feed the results into the recommender. This
    is the "completely autonomous" path — zero UI clicks, zero
    pre-existing assets required.

    Args:
      object_name:            Source object (e.g. "CUSTOMER_POSITIONS").
                              Used as a label and to derive default table
                              paths when the explicit ones aren't given.
      connection_id:          v2 source connection id (default
                              Snowflake_InceptTest).
      runtime_environment_id: v2 runtime env id (default BenakaHomePC).
      target_table:           Bare bad-records target table name.
      template_mapping_id:    M_DQ_Generic FRS GUID by default.
      auto_create_rules:      When False, stop after presenting recs.
      dry_run:                When True, stop after recommendations and
                              return what *would* be created.

      profile_results:        Pass to skip the profiling step.
      recommendations:        Pass to skip the recommender step.
      rule_name_prefix:       Forwarded to recommend_dq_rules.

      source_table_path:      Full Snowflake path. Default
                              "INCEPT_GOV_DEV/DQ_TEST/<object_name>".
      target_table_path:      Default "INCEPT_GOV_DEV/DQ_TEST/<target_table>".
      target_connection_id:   Default = connection_id.

      cdgc_column_id_map:     {column_name: cdgc_column_internal_id}.
                              Recommendations whose column isn't in the
                              map skip the CDGC registration step.
      cdgc_catalog_origin:    Required when cdgc_column_id_map is set
                              (the catalog source UUID for INTERNAL ids).

      rule_space_id, rule_space_name, rule_project_id, rule_project_name:
                              Override the FRS Space/Project where new
                              rule specs land.

      run_now:                When True (and not dry_run), POST /api/v2/job
                              for each created task after the bind+register
                              steps complete. Returns runIds in the report.

    Returns: {
      object_name, dry_run, auto_create_rules, summary:{ok,failed,skipped},
      profile_summary:   {column_count, total_rows} | null,
      recommendations:   [...],
      created:           [{rec, rule_id, task_id, occurrence_id, run_id}],
      steps:             [{step, status, elapsed_ms, ...}],
    }.
    """
    # Resolve empty-string defaults from env so no org-specific IDs live in code
    connection_id          = connection_id          or os.getenv("IDMC_DQ_CONNECTION_ID", "")
    runtime_environment_id = runtime_environment_id or os.getenv("IDMC_DQ_RUNTIME_ENV_ID", "")
    template_mapping_id    = template_mapping_id    or os.getenv("IDMC_DQ_TEMPLATE_MAPPING_ID", "")

    _dq_schema = os.getenv("IDMC_DQ_SCHEMA_PATH", "")  # e.g. "INCEPT_GOV_DEV/DQ_TEST"
    src_path = source_table_path or (f"{_dq_schema}/{object_name}" if _dq_schema else object_name)
    tgt_path = target_table_path or (f"{_dq_schema}/{target_table}" if _dq_schema and target_table else target_table or object_name + "_BAD_RECORDS")
    tgt_conn = target_connection_id or connection_id

    steps: list[dict[str, Any]] = []
    artifacts: dict[str, Any] = {
        "object_name":     object_name,
        "dry_run":         dry_run,
        "auto_create_rules": auto_create_rules,
    }
    log.info("profile_and_govern: object=%r dry_run=%s auto_create=%s",
             object_name, dry_run, auto_create_rules)

    # ------------------------------------------------------------------
    # Step 1 — Profile. Resolution order:
    #   A. Caller supplied profile_results dict.
    #   S. (default) Direct Snowflake query via compute_profile_from_snowflake
    #      — fast, self-contained, no IDMC propagation gap. Tried first when
    #      the source is Snowflake (the tenant default).
    #   B. IDMC profiling: run_profile (existing) + get_profile_results
    #      (CDGC-backed). Only used when Mode S is disabled or fails.
    #   C. Auto-create an IDMC profile when no existing one matches.
    # ------------------------------------------------------------------
    if profile_results is not None:
        steps.append({"step": "profile", "status": "SKIPPED",
                      "reason": "profile_results supplied by caller"})
    elif use_snowflake_direct:
        # Mode S — direct SQL via the Snowflake connector. Default ON.
        sf_cols = [c.get("name") for c in (columns or []) if c.get("name")] or None
        ss = _step(
            "compute_profile_from_snowflake", compute_profile_from_snowflake,
            object_name=object_name,
            database=snowflake_database,
            schema=snowflake_schema,
            columns=sf_cols,
            top_n_values=top_n_values,
        )
        steps.append(ss)
        if ss["status"] == "SUCCESS":
            profile_results = ss["result"]
    else:
        run_profile_fn = globals().get("run_profile")
        get_results_fn = globals().get("get_profile_results")
        if not (callable(run_profile_fn) and callable(get_results_fn)):
            steps.append({"step": "profile", "status": "SKIPPED",
                          "reason": "run_profile/get_profile_results not available "
                                    "and use_snowflake_direct=False; pass profile_results explicitly"})
        else:
            s = _step("run_profile", run_profile_fn,
                      connection_id=connection_id, object_name=object_name,
                      runtime_environment_id=runtime_environment_id)
            steps.append(s)
            run_succeeded = s["status"] == "SUCCESS"

            if not run_succeeded and auto_create_profile and columns:
                cs = _step(
                    "create_profile", create_profile,
                    connection_id=connection_id,
                    object_name=src_path or object_name,
                    runtime_environment_id=(profile_runtime_environment_id
                                             or runtime_environment_id),
                    columns=columns,
                    profile_name=f"AutoProfile{int(time.time())}",
                    auto_run=True,
                )
                steps.append(cs)
                if cs["status"] == "SUCCESS":
                    run_succeeded = True
            elif not run_succeeded and auto_create_profile and not columns:
                steps.append({"step": "create_profile", "status": "SKIPPED",
                              "reason": "auto_create_profile=True but `columns` is empty"})

            if run_succeeded:
                # CDGC-backed read — stats propagate async after a run.
                gs = _step("get_profile_results", get_results_fn,
                           object_name=object_name)
                steps.append(gs)
                if gs["status"] == "SUCCESS":
                    profile_results = gs["result"]

    if profile_results is not None:
        cols = profile_results.get("columns") or {}
        if not isinstance(cols, dict):
            cols = {}
        artifacts["profile_summary"] = {
            "total_rows":   profile_results.get("total_rows"),
            "column_count": len(cols),
        }

    # ------------------------------------------------------------------
    # Step 2 — Recommend (skip when caller supplied recs OR no profile)
    # ------------------------------------------------------------------
    if recommendations is not None:
        steps.append({"step": "recommend_dq_rules", "status": "SKIPPED",
                      "reason": "recommendations supplied by caller"})
    elif profile_results is None:
        steps.append({"step": "recommend_dq_rules", "status": "SKIPPED",
                      "reason": "no profile_results to analyze"})
    else:
        s = _step("recommend_dq_rules", recommend_dq_rules,
                  profile_results=profile_results,
                  rule_name_prefix=rule_name_prefix)
        steps.append(s)
        if s["status"] == "SUCCESS":
            recommendations = (s["result"] or {}).get("recommendations") or []

    recommendations = recommendations or []
    artifacts["recommendation_count"] = len(recommendations)
    artifacts["recommendations"] = recommendations

    # ------------------------------------------------------------------
    # Stop early on dry_run or no auto_create_rules
    # ------------------------------------------------------------------
    if dry_run or not auto_create_rules:
        for label, reason in [
            ("create_dq_rules",          "dry_run=True" if dry_run else "auto_create_rules=False"),
            ("generate_dq_mapping_task", "no rules created"),
            ("register_in_cdgc",         "no rules created"),
            ("execute_tasks",            "no rules created" if not run_now else "no rules created (run_now ignored)"),
        ]:
            steps.append({"step": label, "status": "SKIPPED", "reason": reason})
        return _summarize(artifacts, steps)

    # ------------------------------------------------------------------
    # Step 3 — Create rule specs (one per recommendation)
    # ------------------------------------------------------------------
    created: list[dict[str, Any]] = []
    for rec in recommendations:
        col = rec.get("column_name") or "Input"
        cs = _step(
            f"create_dq_rules[{rec.get('suggested_rule_name')}]",
            create_dq_rules,
            rule_name=rec.get("suggested_rule_name") or f"{rule_name_prefix}_{col}",
            description=rec.get("rationale") or "",
            field_name=col,
            dimension=rec.get("dimension") or "COMPLETENESS",
            rule_template=rec.get("rule_template"),
            space_id=rule_space_id, space_name=rule_space_name,
            project_id=rule_project_id, project_name=rule_project_name,
        )
        steps.append(cs)
        entry: dict[str, Any] = {"rec": rec}
        if cs["status"] == "SUCCESS":
            entry["rule_id"] = (cs["result"] or {}).get("id")
            entry["rule_name"] = (cs["result"] or {}).get("name")
            entry["ui_url"] = (cs["result"] or {}).get("ui_url")
        created.append(entry)

    # ------------------------------------------------------------------
    # Step 4 — Bind each rule to a mapping task (M_DQ_Generic per default)
    # ------------------------------------------------------------------
    for entry in created:
        if not entry.get("rule_id"):
            continue
        col = entry["rec"].get("column_name") or "Input"
        ts = _step(
            f"generate_dq_mapping_task[{entry['rule_id']}]",
            generate_dq_mapping_task,
            source_connection_id=connection_id,
            source_table=src_path,
            target_connection_id=tgt_conn,
            target_table=tgt_path,
            input_field_mapping=f"{col}=Input",
            runtime_environment_id=runtime_environment_id,
            template_mapping_id=template_mapping_id,
            rule_spec_id=entry["rule_id"],
            task_name=f"mt_{entry['rec'].get('suggested_rule_name','dq')}_{int(time.time())%100000}",
        )
        steps.append(ts)
        if ts["status"] == "SUCCESS":
            entry["task_id"]   = (ts["result"] or {}).get("id")
            entry["task_name"] = (ts["result"] or {}).get("name")

    # ------------------------------------------------------------------
    # Step 5 — Register rule occurrence in CDGC (per column, if mapped)
    # ------------------------------------------------------------------
    column_map = cdgc_column_id_map or {}
    for entry in created:
        if not entry.get("rule_id"):
            continue
        col = entry["rec"].get("column_name") or ""
        col_id = column_map.get(col) or column_map.get(col.upper()) or column_map.get(col.lower())
        if not col_id:
            steps.append({"step": f"register_in_cdgc[{entry['rule_id']}]",
                          "status": "SKIPPED",
                          "reason": f"no cdgc_column_id_map entry for column {col!r}"})
            continue
        if not cdgc_catalog_origin:
            steps.append({"step": f"register_in_cdgc[{entry['rule_id']}]",
                          "status": "SKIPPED",
                          "reason": "cdgc_catalog_origin required for INTERNAL column ids"})
            continue
        rs = _step(
            f"register_in_cdgc[{entry['rule_id']}]",
            register_in_cdgc,
            rule_spec_id=entry["rule_id"],
            column_id=col_id,
            occurrence_name=f"{object_name}.{col} :: {entry['rec'].get('suggested_rule_name')}",
            dimension=entry["rec"].get("dimension") or "Completeness",
            column_identity_type="INTERNAL",
            catalog_origin=cdgc_catalog_origin,
        )
        steps.append(rs)
        if rs["status"] == "SUCCESS":
            entry["occurrence_id"] = ((rs["result"] or {}).get("occurrence_id")
                                      or (rs["result"] or {}).get("internal_id"))

    # ------------------------------------------------------------------
    # Step 6 — Execute tasks (optional)
    # ------------------------------------------------------------------
    if run_now:
        for entry in created:
            if not entry.get("task_id"):
                continue
            es = _step(
                f"run_mapping_task[{entry['task_id']}]",
                _run_mapping_task,
                task_id=entry["task_id"], task_type="MTT",
            )
            steps.append(es)
            if es["status"] == "SUCCESS":
                entry["run_id"] = (es["result"] or {}).get("runId") or (es["result"] or {}).get("run_id")
    else:
        steps.append({"step": "execute_tasks", "status": "SKIPPED",
                      "reason": "run_now=False"})

    artifacts["created"] = created
    return _summarize(artifacts, steps)


def _summarize(artifacts: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
    ok      = sum(1 for s in steps if s["status"] == "SUCCESS")
    failed  = sum(1 for s in steps if s["status"] == "FAILED")
    skipped = sum(1 for s in steps if s["status"] == "SKIPPED")
    return {
        **artifacts,
        "summary": {"ok": ok, "failed": failed, "skipped": skipped},
        "steps":   steps,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _configure_settings() -> None:
    host = os.getenv("GOVERNANCE_MCP_HOST", "127.0.0.1")
    port = int(os.getenv("GOVERNANCE_MCP_PORT", "8765"))
    # FastMCP exposes settings via .settings (pydantic). Newer versions also
    # accept host/port directly in the constructor — set both for safety.
    try:
        mcp.settings.host = host
        mcp.settings.port = port
    except Exception:  # noqa: BLE001
        log.warning("could not set mcp.settings.host/port on this SDK version")


if __name__ == "__main__":
    # Transport selection. Claude Desktop only supports stdio; VS Code's MCP
    # client and curl-based debugging want HTTP. HTTP stays the default so
    # existing .vscode/mcp.json entries keep working unchanged.
    use_stdio = "--stdio" in sys.argv[1:]
    transport = "stdio" if use_stdio else "streamable-http"
    if not use_stdio:
        _configure_settings()
    log.info("starting governance_engine MCP server on %s transport", transport)
    try:
        mcp.run(transport=transport)
    except KeyboardInterrupt:
        log.info("shutting down")
        sys.exit(0)
