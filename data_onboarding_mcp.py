"""data_onboarding_mcp.py — MCP server orchestrating end-to-end dataset onboarding.

One master tool:
  - onboard_dataset(source_connection, source_object, data_domain,
                    compliance_framework, auto_provision, runtime_environment)

Flow (each step records SUCCESS / SKIPPED / FAILED; failures don't abort):
  1. CDGC search    — is the asset already cataloged?
  2. Profiling      — POST /profiling-service/api/v1/profile (best-effort;
                      profiling REST API isn't fully documented in our PDFs
                      so the call may 4xx — surface the response).
  3. Classification — read existing classifications from CDGC. Auto-
                      classification is a CDGC background job; we can't
                      "trigger" it via API, only read what it has produced.
  4. Create DQ rules— delegate to governance-engine via MCP HTTP, one
                      rule per compliance dimension implied by the framework
                      (GDPR → COMPLETENESS+VALIDITY, CCPA → similar, HIPAA
                      → COMPLETENESS+VALIDITY+CONSISTENCY).
  5. Register in CDGC — delegate to governance-engine.register_in_cdgc.
                       Skipped if no column id is resolvable (UI step often
                       still required).
  6. Glossary terms — delegate to glossary-manager.suggest_terms_for_asset.
  7. Auto-provision — when auto_provision=True, POST the asset to a Data
                      Marketplace data collection (requires DMP_COLLECTION_ID
                      env or arg). DMP base URL: <pod>-cdmp.dmp-us...

Transport: streamable HTTP. Default bind: 127.0.0.1:8769. Override via
DATA_ONBOARDING_MCP_HOST / DATA_ONBOARDING_MCP_PORT.

Sibling MCP servers (called by URL — they must be running):
  governance-engine : http://127.0.0.1:8765/mcp
  glossary-manager  : http://127.0.0.1:8767/mcp
"""

from __future__ import annotations

import json
import logging
import os
import re as _re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"

CDGC_API_BASE = os.getenv("CDGC_API_BASE", "https://cdgc-api.dmp-us.informaticacloud.com")
DEFAULT_ORG_ID = os.getenv("IDMC_ORG_ID")

# Profiling service base. Pod-specific; default matches usw1/dmp-us tenant.
PROFILING_API_BASE = os.getenv(
    "PROFILING_API_BASE",
    "https://usw1-dqprofile.dmp-us.informaticacloud.com/profiling-service/api/v1",
)

# Data Marketplace API base. Pod-specific; the docs use ${CDMP_URL} as a
# placeholder, so let it be overridden cleanly.
CDMP_API_BASE = os.getenv(
    "CDMP_API_BASE",
    "https://usw1-cdmp.dmp-us.informaticacloud.com",
)

# Default DMP collection (where auto-provisioned assets land). Required for
# the provisioning step; if absent, the step skips with a clear reason.
DEFAULT_DMP_COLLECTION_ID = os.getenv("DMP_COLLECTION_ID", "")

# Sibling MCP server URLs. Each must be running for delegated calls.
GOVERNANCE_MCP_URL = os.getenv("GOVERNANCE_MCP_URL", "http://127.0.0.1:8765/mcp")
GLOSSARY_MCP_URL   = os.getenv("GLOSSARY_MCP_URL",   "http://127.0.0.1:8767/mcp")

log = logging.getLogger("data_onboarding")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_env_lock = threading.Lock()
_jwt_lock = threading.Lock()
_jwt_cache: dict[str, Any] = {"token": None, "expires_at": 0.0}
_JWT_TTL_SECONDS = 29 * 60


# ---------------------------------------------------------------------------
# .env + session + JWT (mirrors lineage_reporter/dq_monitor)
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


def _login_v2() -> tuple[str, str]:
    env = _read_env()
    user = env.get("IDMC_USER")
    pw = env.get("IDMC_PASS")
    host = env.get("IDMC_LOGIN_HOST", "dmp-us.informaticacloud.com")
    if not user or not pw:
        raise RuntimeError("IDMC_USER and IDMC_PASS must be set in .env")
    url = f"https://{host}/ma/api/v2/user/login"
    r = httpx.post(url, json={"@type": "login", "username": user, "password": pw},
                   headers={"Accept": "application/json"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"v2 login HTTP {r.status_code}: {r.text[:300]}")
    j = r.json()
    sid, surl = j.get("icSessionId"), j.get("serverUrl")
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
    return _read_env().get("IDMC_SESSION_ID") or _login_v2()[0]


def _mint_jwt(force: bool = False) -> str:
    with _jwt_lock:
        now = time.time()
        if not force and _jwt_cache.get("token") and _jwt_cache.get("expires_at", 0) > now:
            return _jwt_cache["token"]
        host = _read_env().get("IDMC_LOGIN_HOST", "dmp-us.informaticacloud.com")
        sid = _current_session()
        url = (f"https://{host}/identity-service/api/v1/jwt/Token"
               f"?client_id=idmc_api&nonce={uuid.uuid4().hex.upper()}")
        r = httpx.get(url, headers={"IDS-SESSION-ID": sid, "Accept": "application/json"}, timeout=30)
        if r.status_code == 401:
            sid, _ = _login_v2()
            r = httpx.get(url, headers={"IDS-SESSION-ID": sid, "Accept": "application/json"}, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"JWT mint HTTP {r.status_code}: {r.text[:300]}")
        tok = (r.json() or {}).get("jwt_token")
        if not tok:
            raise RuntimeError(f"JWT mint response missing jwt_token: {r.text[:300]}")
        _jwt_cache["token"] = tok
        _jwt_cache["expires_at"] = now + _JWT_TTL_SECONDS
        return tok


def _request_cdgc(method: str, url: str, **kw) -> httpx.Response:
    headers = dict(kw.pop("headers", {}) or {})
    headers["Authorization"] = f"Bearer {_mint_jwt()}"
    headers["X-INFA-ORG-ID"] = DEFAULT_ORG_ID
    headers.setdefault("Accept", "application/json")
    if "json" in kw or "data" in kw:
        headers.setdefault("Content-Type", "application/json")
    r = httpx.request(method, url, headers=headers, timeout=60, **kw)
    if r.status_code == 401:
        headers["Authorization"] = f"Bearer {_mint_jwt(force=True)}"
        r = httpx.request(method, url, headers=headers, timeout=60, **kw)
    return r


def _request_ids(method: str, url: str, **kw) -> httpx.Response:
    """Generic IDS-SESSION-ID header path (FRS, profiling, DMP)."""
    headers = dict(kw.pop("headers", {}) or {})
    headers["IDS-SESSION-ID"] = _current_session()
    headers.setdefault("Accept", "application/json")
    if "json" in kw or "data" in kw:
        headers.setdefault("Content-Type", "application/json")
    r = httpx.request(method, url, headers=headers, timeout=60, **kw)
    if r.status_code == 401:
        sid, _ = _login_v2()
        headers["IDS-SESSION-ID"] = sid
        r = httpx.request(method, url, headers=headers, timeout=60, **kw)
    return r


# ---------------------------------------------------------------------------
# Cross-server MCP call helper
# ---------------------------------------------------------------------------
def _call_mcp_tool(server_url: str, tool_name: str, args: dict[str, Any],
                   timeout_s: float = 120.0) -> dict[str, Any]:
    """Call a tool on a sibling MCP server over streamable HTTP.

    Does the full handshake (initialize → notifications/initialized → tools/call)
    and parses the SSE response. Returns the parsed tool result (or raises
    on transport / protocol errors).
    """
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    init = httpx.post(server_url, headers=headers,
                      json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18",
                                       "capabilities": {},
                                       "clientInfo": {"name": "data_onboarding",
                                                      "version": "0.1"}}},
                      timeout=30)
    if init.status_code != 200:
        raise RuntimeError(f"MCP initialize {server_url} HTTP {init.status_code}")
    sid_hdr = init.headers.get("mcp-session-id")
    if not sid_hdr:
        raise RuntimeError(f"MCP initialize {server_url} missing session id")
    headers["Mcp-Session-Id"] = sid_hdr
    httpx.post(server_url, headers=headers,
               json={"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=10)

    r = httpx.post(server_url, headers=headers,
                   json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": tool_name, "arguments": args}},
                   timeout=timeout_s)
    if r.status_code != 200:
        raise RuntimeError(f"MCP tools/call {tool_name} HTTP {r.status_code}: {r.text[:300]}")
    m = _re.search(r"data: (.*?)(?:\n\nevent:|\Z)", r.text, _re.S)
    if not m:
        raise RuntimeError(f"MCP tools/call {tool_name} — no SSE data: {r.text[:300]}")
    payload = json.loads(m.group(1).strip())
    result = payload.get("result") or {}
    if result.get("isError"):
        raise RuntimeError(f"MCP {tool_name} returned error: "
                           f"{(result.get('content') or [{}])[0].get('text','')[:300]}")
    text = (result.get("content") or [{}])[0].get("text", "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


# ---------------------------------------------------------------------------
# Step helpers
# ---------------------------------------------------------------------------
def _step(step_name: str, fn, **kw) -> dict[str, Any]:
    """Run a sub-step; capture SUCCESS/FAILED/SKIPPED + elapsed ms."""
    t0 = time.time()
    try:
        result = fn(**kw)
        elapsed = int((time.time() - t0) * 1000)
        if isinstance(result, dict) and result.get("_skipped"):
            return {"step": step_name, "status": "SKIPPED",
                    "reason": result.get("_skipped"), "elapsed_ms": elapsed}
        return {"step": step_name, "status": "SUCCESS", "result": result, "elapsed_ms": elapsed}
    except Exception as e:  # noqa: BLE001
        elapsed = int((time.time() - t0) * 1000)
        return {"step": step_name, "status": "FAILED", "error": str(e)[:600], "elapsed_ms": elapsed}


def _cdgc_search_for_asset(name: str) -> dict[str, Any]:
    """Look up an asset in CDGC by name. Returns {found:bool, hits:[...]}."""
    url = (f"{CDGC_API_BASE}/data360/search/v1/assets"
           f"?knowledgeQuery={quote(name)}&segments=summary,systemAttributes")
    r = _request_cdgc("POST", url, json={"from": 0, "size": 10})
    if r.status_code >= 400:
        raise RuntimeError(f"CDGC search HTTP {r.status_code}: {r.text[:300]}")
    j = r.json() if r.text else {}
    hits = j.get("hits") or []
    if isinstance(hits, dict):
        hits = hits.get("hits") or []
    summary = [{
        "id":     h.get("core.identity") or (h.get("summary") or {}).get("core.identity"),
        "name":   (h.get("summary") or {}).get("core.name") or h.get("core.name"),
        "class":  (h.get("systemAttributes") or {}).get("core.classType"),
    } for h in hits[:10]]
    return {"found": len(hits) > 0, "hit_count": len(hits), "top_hits": summary}


def _trigger_profiling(connection: str, source_object: str,
                       profile_name: str) -> dict[str, Any]:
    """Best-effort POST to /profiling-service/api/v1/profile.

    The full Profiling REST API isn't documented in our PDFs (the manual
    points to a separate "Getting Started" guide we don't have). We try a
    reasonable body shape; whatever comes back is returned to the caller
    so they can iterate. If the host doesn't resolve / 4xx's, treat as
    SKIPPED rather than FAILED.
    """
    url = f"{PROFILING_API_BASE}/profile"
    body = {
        "name":             profile_name,
        "description":      f"Auto-profile by data_onboarding ({datetime.now(timezone.utc).isoformat()})",
        "sourceConnection": connection,
        "sourceObject":     source_object,
    }
    try:
        r = _request_ids("POST", url, json=body)
    except httpx.HTTPError as e:
        return {"_skipped": f"profiling service unreachable: {e}"}
    if r.status_code in (200, 201, 202):
        return {"http_status": r.status_code, "response_head": r.text[:300]}
    return {"_skipped": f"profiling POST HTTP {r.status_code}: {r.text[:200]}"}


def _read_classifications(asset_id: str) -> dict[str, Any]:
    """Read existing column classifications from CDGC.

    Auto-classification is a CDGC background process; we can't trigger it
    via REST, only observe what it has produced.
    """
    segments = "summary,systemAttributes,dataClassification:all"
    url = (f"{CDGC_API_BASE}/data360/search/v1/assets/{asset_id}"
           f"?scheme=internal&segments={segments}")
    r = _request_cdgc("GET", url)
    if r.status_code >= 400:
        raise RuntimeError(f"CDGC asset get HTTP {r.status_code}: {r.text[:300]}")
    j = r.json() if r.text else {}
    dc = j.get("dataClassification") or {}
    items: list[dict[str, Any]] = []
    if isinstance(dc, list):
        items = dc
    elif isinstance(dc, dict):
        for k in ("items", "classifications", "all", "scores"):
            v = dc.get(k)
            if isinstance(v, list):
                items = v
                break
    return {"asset_id": asset_id,
            "classification_count": len(items),
            "samples": items[:5]}


# Compliance framework → DQ dimensions matrix
COMPLIANCE_DIMENSIONS: dict[str, list[str]] = {
    "GDPR":      ["COMPLETENESS", "VALIDITY"],
    "CCPA":      ["COMPLETENESS", "VALIDITY"],
    "HIPAA":     ["COMPLETENESS", "VALIDITY", "CONSISTENCY"],
    "SOX":       ["COMPLETENESS", "ACCURACY", "CONSISTENCY"],
    "PCI-DSS":   ["COMPLETENESS", "VALIDITY"],
    "":          ["COMPLETENESS"],
    "NONE":      ["COMPLETENESS"],
}


def _create_rules_for_framework(source_object: str, data_domain: str,
                                framework: str) -> dict[str, Any]:
    """For each dimension implied by the framework, call governance-engine
    create_dq_rules via MCP HTTP. Returns the rule ids that landed."""
    fw = (framework or "").upper().strip()
    dims = COMPLIANCE_DIMENSIONS.get(fw) or COMPLIANCE_DIMENSIONS[""]
    short = _re.sub(r"[^A-Za-z0-9_]+", "_", source_object)[:30]
    created: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for dim in dims:
        rule_name = f"INCEPT_ONBOARD_{short}_{dim}_{int(time.time())%100000}"
        try:
            res = _call_mcp_tool(
                GOVERNANCE_MCP_URL,
                "create_dq_rules",
                {
                    "rule_name":   rule_name,
                    "description": f"Auto-created by data_onboarding for {data_domain or 'unknown'}/"
                                   f"{source_object} (framework={fw or 'none'})",
                    "field_name":  "Input",
                    "dimension":   dim,
                    "auto_uuid":   True,
                },
                timeout_s=120,
            )
            created.append({"dimension": dim, "rule_id": res.get("id"),
                             "rule_name": res.get("name"),
                             "documentState": res.get("documentState")})
        except Exception as e:  # noqa: BLE001
            errors.append({"dimension": dim, "error": str(e)[:300]})
    return {"framework": fw or "(none)", "dimensions": dims,
            "created": created, "errors": errors}


def _suggest_glossary_terms(asset_name: str, data_domain: str) -> dict[str, Any]:
    return _call_mcp_tool(
        GLOSSARY_MCP_URL,
        "suggest_terms_for_asset",
        {"asset_name": asset_name, "domain_context": data_domain or ""},
        timeout_s=60,
    )


def _provision_to_dmp(asset: dict[str, Any], collection_id: str) -> dict[str, Any]:
    """POST the asset to a Data Marketplace data collection.

    Endpoint (DMP API ref): POST /api/v2/data-collections/<id>/data-assets
    """
    if not collection_id:
        return {"_skipped": "no DMP_COLLECTION_ID env / arg — cannot provision"}
    url = f"{CDMP_API_BASE}/api/v2/data-collections/{collection_id}/data-assets"
    body = {
        "name":    asset.get("name"),
        "assetId": asset.get("id"),
        "type":    asset.get("class") or "DATA_ASSET",
    }
    try:
        r = _request_ids("POST", url, json=body)
    except httpx.HTTPError as e:
        return {"_skipped": f"DMP unreachable: {e}"}
    if r.status_code in (200, 201, 202):
        return {"http_status": r.status_code,
                "response_head": r.text[:300]}
    return {"_skipped": f"DMP POST HTTP {r.status_code}: {r.text[:200]}"}


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="data_onboarding",
    instructions=(
        "End-to-end dataset onboarding orchestrator. One tool: "
        "onboard_dataset. Each step (CDGC search → profiling → "
        "classification → DQ rules → CDGC registration → glossary terms "
        "→ optional DMP publish) reports SUCCESS / SKIPPED / FAILED "
        "with elapsed time and a result/error/reason. Failures are "
        "non-fatal — later steps still try."
    ),
)


@mcp.tool()
def onboard_dataset(
    source_connection: str,
    source_object: str,
    data_domain: str = "",
    compliance_framework: str = "",
    auto_provision: bool = False,
    runtime_environment: str = "",
    dmp_collection_id: str | None = None,
    catalog_origin: str | None = None,
) -> dict[str, Any]:
    """End-to-end onboarding for a new dataset.

    Args:
      source_connection:   IDMC connection name (e.g. "Snowflake_InceptTest").
      source_object:       Source table/object name (e.g. "INCEPT_GOV_DEV.DQ_TEST.CUSTOMER_POSITIONS").
      data_domain:         Free-text domain label ("Customer", "Finance") — used
                           in DQ rule descriptions and glossary search.
      compliance_framework: GDPR / CCPA / HIPAA / SOX / PCI-DSS / empty. Drives
                           which DQ dimensions get a starter rule.
      auto_provision:      When True, publish the asset to Data Marketplace
                           (requires DMP_COLLECTION_ID env or dmp_collection_id).
      runtime_environment: Reserved for future task scheduling steps.
      dmp_collection_id:   Override DMP_COLLECTION_ID env var per call.

    Returns: {goal, summary:{ok, failed, skipped}, artifacts:{asset_id,
              rule_ids, ...}, steps:[{step, status, elapsed_ms, ...}]}.
    """
    log.info("onboard_dataset start: %s.%s domain=%r framework=%r auto=%s",
             source_connection, source_object, data_domain, compliance_framework,
             auto_provision)
    artifacts: dict[str, Any] = {}
    steps: list[dict[str, Any]] = []

    # 1) CDGC: already cataloged?
    s = _step("cdgc_search", _cdgc_search_for_asset, name=source_object)
    steps.append(s)
    found_asset: dict[str, Any] | None = None
    if s["status"] == "SUCCESS" and (s["result"].get("top_hits") or []):
        found_asset = s["result"]["top_hits"][0]
        artifacts["asset_id"]   = found_asset.get("id")
        artifacts["asset_name"] = found_asset.get("name")

    # 2) Trigger profiling
    profile_name = f"profile_{_re.sub(r'[^A-Za-z0-9_]+', '_', source_object)[:30]}_{int(time.time())%100000}"
    s = _step("trigger_profiling",
              _trigger_profiling,
              connection=source_connection,
              source_object=source_object,
              profile_name=profile_name)
    steps.append(s)
    if s["status"] == "SUCCESS":
        artifacts["profile_name"] = profile_name

    # 3) Classification — only meaningful when we found the asset
    if found_asset and found_asset.get("id"):
        s = _step("read_classifications", _read_classifications,
                  asset_id=found_asset["id"])
        steps.append(s)
        if s["status"] == "SUCCESS":
            artifacts["classification_count"] = s["result"].get("classification_count")
    else:
        steps.append({"step": "read_classifications", "status": "SKIPPED",
                      "reason": "asset not found in CDGC"})

    # 4) Create DQ rules per compliance framework dimensions
    s = _step("create_dq_rules", _create_rules_for_framework,
              source_object=source_object, data_domain=data_domain,
              framework=compliance_framework)
    steps.append(s)
    if s["status"] == "SUCCESS":
        rule_ids = [r.get("rule_id") for r in (s["result"].get("created") or [])
                    if r.get("rule_id")]
        artifacts["rule_ids"] = rule_ids
        artifacts["rules_attempted"] = len(s["result"].get("dimensions") or [])
        artifacts["rules_created"]   = len(rule_ids)

    # 5) Register in CDGC — only meaningful when the resolved asset is a
    # data element (column-like). Rule specifications, projects, and
    # business assets aren't bindable here. Internal-id binding also
    # requires a catalog_origin (or an EXTERNAL id, which we don't have
    # from search).
    rule_ids = artifacts.get("rule_ids") or []
    asset_class = (found_asset or {}).get("class") or ""
    looks_like_column = asset_class.endswith(".Column") or "DataElement" in asset_class
    if not (found_asset and found_asset.get("id")):
        steps.append({"step": "register_in_cdgc", "status": "SKIPPED",
                      "reason": "asset_id missing (CDGC search returned no hit)"})
    elif not rule_ids:
        steps.append({"step": "register_in_cdgc", "status": "SKIPPED",
                      "reason": "no rule_ids from create_dq_rules"})
    elif not looks_like_column:
        steps.append({"step": "register_in_cdgc", "status": "SKIPPED",
                      "reason": f"asset class {asset_class!r} is not a data element; "
                                "rule occurrences bind to columns, not rule specs or business assets"})
    elif not catalog_origin:
        steps.append({"step": "register_in_cdgc", "status": "SKIPPED",
                      "reason": "catalog_origin not provided (required for INTERNAL column id binding)"})
    else:
        first_rule = rule_ids[0]
        try:
            reg = _call_mcp_tool(
                GOVERNANCE_MCP_URL,
                "register_in_cdgc",
                {
                    "rule_spec_id":         first_rule,
                    "column_id":            found_asset["id"],
                    "occurrence_name":      f"{source_object} :: rule {first_rule[:8]}",
                    "dimension":            (COMPLIANCE_DIMENSIONS.get((compliance_framework or "").upper())
                                             or ["COMPLETENESS"])[0],
                    "column_identity_type": "INTERNAL",
                    "catalog_origin":       catalog_origin,
                },
                timeout_s=60,
            )
            steps.append({"step": "register_in_cdgc", "status": "SUCCESS", "result": reg})
            artifacts["rule_occurrence_id"] = reg.get("id") or reg.get("occurrence_id")
        except Exception as e:  # noqa: BLE001
            steps.append({"step": "register_in_cdgc", "status": "FAILED",
                          "error": str(e)[:400]})

    # 6) Glossary suggestions
    if found_asset and found_asset.get("name"):
        s = _step("suggest_glossary_terms", _suggest_glossary_terms,
                  asset_name=found_asset["name"], data_domain=data_domain)
        steps.append(s)
        if s["status"] == "SUCCESS":
            sugs = s["result"].get("suggestions") or []
            artifacts["glossary_suggestion_count"] = len(sugs)
    else:
        steps.append({"step": "suggest_glossary_terms", "status": "SKIPPED",
                      "reason": "asset not found in CDGC"})

    # 7) Optional DMP provisioning
    if auto_provision:
        if found_asset and found_asset.get("id"):
            s = _step("provision_to_dmp", _provision_to_dmp,
                      asset=found_asset,
                      collection_id=dmp_collection_id or DEFAULT_DMP_COLLECTION_ID)
            steps.append(s)
        else:
            steps.append({"step": "provision_to_dmp", "status": "SKIPPED",
                          "reason": "no asset id to publish"})
    else:
        steps.append({"step": "provision_to_dmp", "status": "SKIPPED",
                      "reason": "auto_provision=False"})

    ok      = sum(1 for s in steps if s["status"] == "SUCCESS")
    failed  = sum(1 for s in steps if s["status"] == "FAILED")
    skipped = sum(1 for s in steps if s["status"] == "SKIPPED")

    return {
        "goal":      f"onboard {source_object} from {source_connection} (domain={data_domain}, framework={compliance_framework})",
        "summary":   {"ok": ok, "failed": failed, "skipped": skipped},
        "artifacts": artifacts,
        "steps":     steps,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _configure_settings() -> None:
    host = os.getenv("DATA_ONBOARDING_MCP_HOST", "127.0.0.1")
    port = int(os.getenv("DATA_ONBOARDING_MCP_PORT", "8769"))
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
    log.info("starting data_onboarding MCP server on %s transport", transport)
    try:
        mcp.run(transport=transport)
    except KeyboardInterrupt:
        log.info("shutting down")
        sys.exit(0)
