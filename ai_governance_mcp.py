"""ai_governance_mcp.py — LLM-powered data governance automation server.

Automates the manual CDGC onboarding steps using Claude AI:
  - scan_mcc_source            : Discover table+column metadata via CDGC search
  - generate_governance_taxonomy: LLM → domain/subdomain/business-term tree
  - create_domain_structure    : POST Domain + SubDomain + BusinessTerms to CDGC
  - create_system_and_dataset  : POST System + Dataset assets in CDGC
  - curate_assets_with_glossary: LLM matches columns → BTs; POST term links
  - run_mcc_scan               : Trigger Metadata Command Center catalog scan
  - propagate_dq_score         : Push DQ scores back to CDGC assets
  - onboard_and_govern         : Master orchestrator — runs all 8 steps end-to-end

Relies on CLAIRE/IDMC rule spec + DQRO creation (governance_engine_mcp.py:8765).

Transport: streamable HTTP. Default bind: 127.0.0.1:8770.
Override via AI_GOVERNANCE_MCP_HOST / AI_GOVERNANCE_MCP_PORT.

Required env vars (in .env):
  ANTHROPIC_API_KEY    — Claude API key
  CDGC_API_BASE        — https://cdgc-api.dm-us.informaticacloud.com
  IDMC_ORG_ID          — tenant org UUID
  IDMC_USER / IDMC_PASS / IDMC_LOGIN_HOST — for session minting
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
SCRIPT_DIR  = Path(__file__).resolve().parent
ENV_PATH    = SCRIPT_DIR / ".env"
SCAN_CACHE_DIR = Path(os.getenv("SCAN_CACHE_DIR", str(SCRIPT_DIR / ".scan_cache")))
SCAN_CACHE_TTL      = int(os.getenv("SCAN_CACHE_TTL_SECONDS", str(3600)))  # 1 hour
SCAN_THREAD_WORKERS = int(os.getenv("SCAN_THREAD_WORKERS", "20"))  # parallel column fetches
BROWSE_THRESHOLD    = 500   # only run hierarchy browse for sources with this many keyword hits
BROWSE_CACHE_TTL    = 1800  # seconds — cache browse results for 30 min
_browse_cache: dict[str, tuple[float, list]] = {}  # schema_name → (ts, hits)

CDGC_API_BASE = os.getenv("CDGC_API_BASE", "https://cdgc-api.dm-us.informaticacloud.com")
DEFAULT_ORG_ID = os.getenv("IDMC_ORG_ID")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CDMP_API_BASE               = os.getenv("CDMP_API_BASE", "https://cdgc-api.dm-us.informaticacloud.com/cdmp-marketplace")
CDMP_DELIVERY_TEMPLATE_ID   = os.getenv("CDMP_DELIVERY_TEMPLATE_ID", "")
CDMP_TERMS_OF_USE_ID         = os.getenv("CDMP_TERMS_OF_USE_ID", "")

IDMC_IDENTITY_HOST = os.getenv("IDMC_IDENTITY_HOST", "dm-us.informaticacloud.com")
JWT_MINT_URL = f"https://{IDMC_IDENTITY_HOST}/identity-service/api/v1/jwt/Token"
JWT_TTL_SECONDS = int(os.getenv("IDMC_JWT_TTL_SECONDS", "1740"))

# Governance engine sibling (for rule spec + DQRO delegation)
GOVERNANCE_MCP_URL = os.getenv("GOVERNANCE_MCP_URL", "http://127.0.0.1:8765/mcp")

# CDGC class-type constants
CLASS_DOMAIN       = "com.infa.ccgf.models.governance.Domain"
CLASS_SUBDOMAIN    = "com.infa.ccgf.models.governance.SubDomain"
CLASS_DATASET      = "com.infa.ccgf.models.governance.DataSet"
CLASS_BUSINESS_TERM = "com.infa.ccgf.models.governance.BusinessTerm"
CLASS_TABLE        = "com.infa.odin.models.relational.Table"
CLASS_VIEW         = "com.infa.odin.models.relational.View"
CLASS_COLUMN       = "com.infa.odin.models.relational.Column"

# Relationship class for column ↔ business-term
REL_TERM_ASSET     = "asscBusinessTermTechnicalAsset"

log = logging.getLogger("ai_governance")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_env_lock = threading.Lock()
_jwt_lock = threading.Lock()
_jwt_cache: dict[str, Any] = {"token": None, "expires_at": 0.0}

# ---------------------------------------------------------------------------
# .env helpers
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
# Auth: V2 session (IDS-SESSION-ID) + JWT (Bearer)
# ---------------------------------------------------------------------------
def _login_v2() -> tuple[str, str]:
    env  = _read_env()
    user = env.get("IDMC_USER")
    pw   = env.get("IDMC_PASS")
    host = env.get("IDMC_LOGIN_HOST", "dm-us.informaticacloud.com")
    if not user or not pw:
        raise RuntimeError("IDMC_USER / IDMC_PASS missing from .env")
    r = httpx.post(
        f"https://{host}/ma/api/v2/user/login",
        json={"@type": "login", "username": user, "password": pw},
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"v2 login HTTP {r.status_code}: {r.text[:300]}")
    j = r.json()
    sid  = j.get("icSessionId") or j.get("userInfo", {}).get("sessionId", "")
    base = j.get("serverUrl", "")
    if not sid:
        raise RuntimeError("v2 login: no session id in response")
    with _env_lock:
        env = _read_env()
        env["IDMC_SESSION_ID"]  = sid
        env["IDMC_SERVER_URL"]  = base
        _write_env(env)
    log.info("minted fresh v2 session (%s…)", sid[:8])
    return sid, base


def _login_v3() -> tuple[str, str]:
    env  = _read_env()
    user = env.get("IDMC_USER")
    pw   = env.get("IDMC_PASS")
    host = env.get("IDMC_LOGIN_HOST", "dm-us.informaticacloud.com")
    if not user or not pw:
        raise RuntimeError("IDMC_USER / IDMC_PASS missing from .env")
    r = httpx.post(
        f"https://{host}/saas/public/core/v3/login",
        json={"username": user, "password": pw},
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"v3 login HTTP {r.status_code}: {r.text[:300]}")
    j    = r.json()
    sid  = (r.headers.get("INFA-SESSION-ID")
            or (j.get("userInfo") or {}).get("sessionId", ""))
    base = (j.get("products") or [{}])[0].get("baseApiUrl", "") if j.get("products") else ""
    if not sid:
        raise RuntimeError(f"v3 login missing session id: {j}")
    with _env_lock:
        env = _read_env()
        env["IDMC_V3_SESSION_ID"] = sid
        if base:
            env["IDMC_V3_BASE_URL"] = base
        _write_env(env)
    log.info("minted fresh v3 session (%s…)", sid[:8])
    return sid, base


def _v2_session_id() -> str:
    env = _read_env()
    return env.get("IDMC_SESSION_ID") or _login_v2()[0]


def _cdmp_request(method: str, path: str, **kw) -> "httpx.Response":
    """CDMP REST API call using CDGC auth (Bearer JWT + IDS-SESSION-ID), with one 401 retry."""
    url = f"{CDMP_API_BASE}/{path.lstrip('/')}"
    kw.setdefault("timeout", 30)
    r = httpx.request(method, url, headers=_cdgc_headers(), **kw)
    if r.status_code == 401:
        log.info("CDMP 401 — refreshing JWT and retrying")
        with _jwt_lock:
            _jwt_cache["token"] = None
            _jwt_cache["expires_at"] = 0.0
        r = httpx.request(method, url, headers=_cdgc_headers(), **kw)
    return r


def _v3_session_id() -> str:
    env = _read_env()
    return env.get("IDMC_V3_SESSION_ID") or _login_v3()[0]
def _v3_base_url() -> str:
    env = _read_env()
    url = env.get("IDMC_V3_BASE_URL") or env.get("IDMC_SERVER_URL", "")
    if not url:
        raise RuntimeError("IDMC_V3_BASE_URL (or IDMC_SERVER_URL) not set in .env — run v3 login first.")
    return url


def _get_jwt() -> str:
    with _jwt_lock:
        now = time.time()
        if _jwt_cache["token"] and now < float(_jwt_cache.get("expires_at", 0)):
            return str(_jwt_cache["token"])
        env = _read_env()
        cached_jwt = env.get("IDMC_JWT", "")
        minted_at  = float(env.get("IDMC_JWT_MINTED_AT") or "0")
        if cached_jwt and (now - minted_at) < JWT_TTL_SECONDS:
            _jwt_cache["token"] = cached_jwt
            _jwt_cache["expires_at"] = minted_at + JWT_TTL_SECONDS
            return cached_jwt
        nonce    = uuid.uuid4().hex.upper()
        client   = env.get("IDMC_JWT_CLIENT_ID", "idmc_api")
        sid      = _v2_session_id()
        r = httpx.get(
            JWT_MINT_URL,
            params={"client_id": client, "nonce": nonce},
            headers={"IDS-SESSION-ID": sid},
            timeout=30,
        )
        if r.status_code == 401:
            sid = _login_v2()[0]
            r = httpx.get(JWT_MINT_URL, params={"client_id": client, "nonce": nonce},
                          headers={"IDS-SESSION-ID": sid}, timeout=30)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"JWT mint HTTP {r.status_code}: {r.text[:300]}")
        token = r.json().get("jwt_token") or r.json().get("token") or ""
        if not token:
            raise RuntimeError("JWT mint: no token in response")
        _jwt_cache["token"] = token
        _jwt_cache["expires_at"] = now + JWT_TTL_SECONDS
        with _env_lock:
            env = _read_env()
            env["IDMC_JWT"]            = token
            env["IDMC_JWT_MINTED_AT"]  = str(int(now))
            _write_env(env)
        return token


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _cdgc_headers() -> dict[str, str]:
    env    = _read_env()
    org_id = env.get("IDMC_ORG_ID") or DEFAULT_ORG_ID or ""
    return {
        "Authorization":  f"Bearer {_get_jwt()}",
        "X-INFA-ORG-ID":  org_id,
        "IDS-SESSION-ID": _v2_session_id(),
        "Accept":         "application/json",
        "Content-Type":   "application/json",
    }


def _request_cdgc(method: str, url: str, **kw) -> httpx.Response:
    kw.setdefault("timeout", 30)
    r = httpx.request(method, url, headers=_cdgc_headers(), **kw)
    if r.status_code == 401:
        log.info("CDGC 401 — refreshing JWT and retrying")
        with _jwt_lock:
            _jwt_cache["token"] = None
            _jwt_cache["expires_at"] = 0.0
        r = httpx.request(method, url, headers=_cdgc_headers(), **kw)
    if r.status_code >= 400:
        log.warning("CDGC %s %s -> %d BODY: %s", method, url.split("?")[0][-60:], r.status_code, r.text[:300])
    return r


def _request_v3(method: str, path_or_url: str, **kw) -> httpx.Response:
    kw.setdefault("timeout", 30)
    base = _v3_base_url()
    url  = path_or_url if path_or_url.startswith("http") else base + path_or_url
    sid  = _v3_session_id()
    headers = kw.pop("headers", {})
    headers["INFA-SESSION-ID"] = sid
    headers.setdefault("Accept", "application/json")
    headers.setdefault("Content-Type", "application/json")
    r = httpx.request(method, url, headers=headers, **kw)
    if r.status_code == 401:
        log.info("V3 401 — refreshing v3 session and retrying")
        sid = _login_v3()[0]
        headers["INFA-SESSION-ID"] = sid
        r = httpx.request(method, url, headers=headers, **kw)
    return r


# ---------------------------------------------------------------------------
# LLM helpers — Claude via Anthropic API
# ---------------------------------------------------------------------------
_MODEL_FAST   = "claude-haiku-4-5-20251001"   # routing + taxonomy — speed priority
_MODEL_QUALITY = "claude-sonnet-4-6"           # curate / complex reasoning — quality priority

def _llm_call(system_prompt: str, user_msg: str, temperature: float = 0.2,
              model: str | None = None) -> str:
    """Call Claude. model defaults to _MODEL_FAST (Haiku). Returns raw text response."""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic package not installed — run: pip install anthropic")

    api_key = _read_env().get("ANTHROPIC_API_KEY") or ANTHROPIC_API_KEY
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY missing from .env")

    chosen = model or _MODEL_FAST
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=chosen,
        max_tokens=8192,
        temperature=temperature,
        system=system_prompt,
        messages=[{"role": "user", "content": user_msg}],
    )
    return msg.content[0].text


_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _clean_json_text(text: str) -> str:
    """Strip markdown fences and trailing commas from LLM JSON output."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if "```" in text:
            text = text[: text.rfind("```")]
    text = text.strip()
    # Remove trailing commas before } or ] (common LLM mistake)
    text = _TRAILING_COMMA_RE.sub(r"\1", text)
    return text


def _llm_json(system_prompt: str, user_msg: str, model: str | None = None) -> Any:
    """Call LLM and parse the response as JSON. Retries once on parse failure."""
    raw = _llm_call(system_prompt + "\n\nAlways respond with valid JSON only.", user_msg, model=model)
    text = _clean_json_text(raw)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        fix_prompt = f"The following is not valid JSON. Return ONLY valid JSON:\n\n{raw}"
        raw2 = _llm_call("You are a JSON formatter. Return only valid JSON.", fix_prompt, model=model)
        text2 = _clean_json_text(raw2)
        return json.loads(text2)


# ---------------------------------------------------------------------------
# CDGC search helpers
# ---------------------------------------------------------------------------
def _cdgc_search(name: str, class_type: str | None = None, size: int = 10) -> list[dict[str, Any]]:
    url = (f"{CDGC_API_BASE}/data360/search/v1/assets"
           f"?knowledgeQuery={quote(name)}&segments=summary,systemAttributes")
    r = _request_cdgc("POST", url, json={"from": 0, "size": size})
    if r.status_code >= 400:
        return []
    hits = (r.json() or {}).get("hits", [])
    if isinstance(hits, dict):
        hits = hits.get("hits", [])
    if class_type:
        hits = [h for h in hits
                if class_type in ((h.get("systemAttributes") or {}).get("core.classType") or "")]
    return hits or []


def _cdgc_search_paged(query: str, class_type: str | None = None, max_results: int = 500) -> list[dict[str, Any]]:
    """Search CDGC with pagination, deduplicating by core.identity."""
    all_hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    page_size = 100  # CDGC enforces max 100 per page
    offset = 0
    url = (f"{CDGC_API_BASE}/data360/search/v1/assets"
           f"?knowledgeQuery={quote(query)}&segments=summary,systemAttributes")
    while len(all_hits) < max_results:
        r = _request_cdgc("POST", url, json={"from": offset, "size": page_size})
        if r.status_code >= 400:
            break
        hits = (r.json() or {}).get("hits", [])
        if isinstance(hits, dict):
            hits = hits.get("hits", [])
        if not hits:
            break
        for h in hits:
            tid = _id_of(h)
            if class_type and class_type not in ((h.get("systemAttributes") or {}).get("core.classType") or ""):
                continue
            if tid and tid not in seen:
                seen.add(tid)
                all_hits.append(h)
        if len(hits) < page_size:
            break
        offset += page_size
    return all_hits[:max_results]


def _cdgc_get_asset(asset_id: str, segments: str = "summary,systemAttributes,hierarchy") -> dict[str, Any]:
    url = f"{CDGC_API_BASE}/data360/search/v1/assets/{asset_id}?scheme=internal&segments={segments}"
    r = _request_cdgc("GET", url)
    if r.status_code >= 400:
        return {}
    return r.json() or {}


def _browse_all_tables_in_schema(schema_name: str) -> list[dict[str, Any]]:
    """Return ALL table assets in a named schema via hierarchy browse (not keyword search).

    This bypasses the knowledgeQuery relevance cap and returns the complete set.
    Results are cached for BROWSE_CACHE_TTL seconds to keep discover fast on re-runs.
    """
    import time as _t
    cached = _browse_cache.get(schema_name)
    if cached:
        ts, hits = cached
        if _t.time() - ts < BROWSE_CACHE_TTL:
            log.info("_browse_all_tables_in_schema: cache hit for %s (%d hits)", schema_name, len(hits))
            return hits

    hits = _cdgc_search(schema_name, class_type="Schema", size=10)
    if not hits:
        hits = _cdgc_search(schema_name, size=10)
    if not hits:
        return []
    name_upper = schema_name.upper()
    exact = [h for h in hits if _name_of(h).upper() == name_upper]
    schema_hit = (exact or hits)[0]
    schema_id  = _id_of(schema_hit)
    if not schema_id:
        return []
    details = _cdgc_get_asset(schema_id, segments="summary,systemAttributes,hierarchy")
    hier = details.get("hierarchy") or []
    if isinstance(hier, dict):
        hier = hier.get("children") or hier.get("items") or []

    def _child_class_type(h: dict[str, Any]) -> str:
        # Hierarchy children do NOT carry systemAttributes; their classType is the
        # "~"-suffix of core.externalId (e.g. ".../MY_TABLE~com.infa...relational.Table").
        # Fall back to systemAttributes for any entries that do include it.
        eid = h.get("core.externalId") or (h.get("summary") or {}).get("core.externalId") or ""
        if "~" in eid:
            return eid.rsplit("~", 1)[-1]
        return (h.get("systemAttributes") or {}).get("core.classType", "")

    result = [h for h in hier if _child_class_type(h).endswith("Table")]
    _browse_cache[schema_name] = (_t.time(), result)
    log.info("_browse_all_tables_in_schema: %s → %d tables (cached)", schema_name, len(result))
    return result


def _fetch_column_detail(col_hit: dict[str, Any]) -> dict[str, Any] | None:
    """Fetch data-type and identity for one column hit. Designed for thread-pool use."""
    cname = _name_of(col_hit) or (col_hit.get("summary") or {}).get("core.name", "")
    ctype = (col_hit.get("systemAttributes") or {}).get("core.classType", "")
    if ctype and "Column" not in ctype:
        return None
    col_id     = _id_of(col_hit)
    col_detail = _cdgc_get_asset(col_id, segments="summary,systemAttributes,selfAttributes")
    sa    = col_detail.get("selfAttributes") or {}
    dtype = (sa.get("com.infa.odin.models.relational.Datatype")
             or sa.get("core.dataType")
             or sa.get("dataType")
             or "unknown")
    return {"name": cname, "data_type": dtype, "internal_id": col_id}


def _fetch_columns_parallel(hier: list[dict[str, Any]], table_name: str = "") -> list[dict[str, Any]]:
    """Fetch column details for a list of hierarchy hits using a thread pool."""
    if not hier:
        return []
    results: list[dict[str, Any] | None] = [None] * len(hier)
    with ThreadPoolExecutor(max_workers=SCAN_THREAD_WORKERS) as ex:
        future_to_idx = {ex.submit(_fetch_column_detail, col): i for i, col in enumerate(hier)}
        done_count = 0
        for fut in as_completed(future_to_idx):
            idx         = future_to_idx[fut]
            results[idx] = fut.result()
            done_count  += 1
            if done_count % 20 == 0:
                log.info("scan: %s — %d/%d columns fetched", table_name, done_count, len(hier))
    return [r for r in results if r is not None]


def _columns_with_existing_terms(table_internal_id: str) -> set[str]:
    """Return the set of column internal_ids that already have a business term linked in CDGC."""
    url = (
        f"{CDGC_API_BASE}/data360/search/v1/assets/{table_internal_id}"
        "?scheme=internal&segments=summary,systemAttributes,relationships"
    )
    r = _request_cdgc("GET", url)
    if r.status_code >= 400:
        return set()
    data = r.json() or {}
    relationships = data.get("relationships") or {}
    # CDGC stores column→term links under various relationship keys
    covered: set[str] = set()
    for rel_key, rel_items in relationships.items():
        if not isinstance(rel_items, list):
            continue
        for item in rel_items:
            col_id = (item.get("systemAttributes") or {}).get("core.identity") or item.get("core.identity") or ""
            term_rels = item.get("relationships") or {}
            for tkey, tvals in term_rels.items():
                if "glossary" in tkey.lower() or "BusinessTerm" in tkey or "term" in tkey.lower():
                    if tvals:
                        covered.add(col_id)
                        break
    return covered


def _id_of(hit: dict[str, Any]) -> str:
    return hit.get("core.identity") or (hit.get("systemAttributes") or {}).get("core.identity") or ""


def _name_of(hit: dict[str, Any]) -> str:
    return (hit.get("summary") or {}).get("core.name") or ""


# ---------------------------------------------------------------------------
# CDGC write helpers
# ---------------------------------------------------------------------------
def _cdgc_create_asset(
    class_type: str,
    name: str,
    description: str = "",
    parent_id: str | None = None,
    extra_summary: dict[str, Any] | None = None,
    extra_self: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST a new asset to CDGC /data360/content/v1/assets. Returns response dict."""
    summary: dict[str, Any] = {
        "core.name": name,
        **({"core.description": description} if description else {}),
        **(extra_summary or {}),
    }
    body: dict[str, Any] = {
        "core.classType": class_type,
        "summary": summary,
    }
    if extra_self:
        body["selfAttributes"] = extra_self
    if parent_id:
        body["parent"] = {"core.identity": parent_id}

    url = f"{CDGC_API_BASE}/data360/content/v1/assets"
    r = _request_cdgc("POST", url, json=body)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"create_asset({name!r}) HTTP {r.status_code}: {r.text[:400]}")
    return r.json() or {}


def _cdgc_link_term_to_asset(term_id: str, asset_id: str) -> dict[str, Any]:
    """Link a BusinessTerm to a technical asset via ccgf-contentv2 publish API."""
    url = f"{CDGC_API_BASE}/ccgf-contentv2/api/v1/publish"
    body = {
        "items": [
            {
                "elementType": "RELATIONSHIP",
                "fromIdentity": asset_id,
                "toIdentity": term_id,
                "operation": "INSERT",
                "type": "com.infa.ccgf.models.governance.IClassTechnicalGlossaryBase",
                "identityType": "INTERNAL",
                "attributes": {
                    "core.curationStatus": "ACCEPTED",
                    "core.inferred": False,
                    "core.channels": ["MANUAL"],
                },
            }
        ]
    }
    headers = {**_cdgc_headers(), "X-INFA-PRODUCT-ID": "cdgc"}
    r = httpx.post(url, headers=headers, json=body, timeout=30)
    if r.status_code not in (200, 201, 207):
        log.warning("publish link %s → %s: HTTP %s %s", asset_id, term_id, r.status_code, r.text[:400])
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    return r.json() or {"status": "linked"}


# ---------------------------------------------------------------------------
# MCC helpers (Metadata Command Center — CDGC catalog scan API)
# ---------------------------------------------------------------------------
def _list_catalog_sources() -> list[dict[str, Any]]:
    """List catalog source assets from CDGC via search API."""
    seen: set = set()
    sources = []

    # Only keep assets whose class type signals a catalog source / resource.
    # Mappings, taskflows, tables, schemas, columns, etc. are excluded.
    _SOURCE_TYPE_KEYWORDS = ("Source", "Resource", "Catalog", "Connector")

    def _add_hits(hits: list) -> None:
        for h in hits:
            sa      = h.get("systemAttributes") or {}
            summary = h.get("summary") or {}
            cls     = sa.get("core.classType", "")
            # Skip anything that isn't recognisably a catalog source/resource
            if cls and not any(k in cls for k in _SOURCE_TYPE_KEYWORDS):
                continue
            identity = h.get("core.identity") or sa.get("core.identity") or ""
            if identity in seen:
                continue
            seen.add(identity)
            full_ext = sa.get("core.externalId") or ""
            ext_id = full_ext.split("://")[0]
            conn_from_ext = full_ext.split("://", 1)[1].split("/")[0] if "://" in full_ext else ""
            sources.append({
                "id":         identity,
                "sourceId":   sa.get("core.origin"),
                "externalId": ext_id,
                "connection": conn_from_ext,
                "name":       summary.get("core.name"),
                "type":       cls,
                "updateTime": sa.get("core.modifiedOn"),
            })

    # Broad class-type searches
    _add_hits(_cdgc_search("Resource",     class_type="core.Resource",    size=100))
    _add_hits(_cdgc_search("CatalogSource",                               size=100))
    # Common source name keywords to catch all connector types
    for kw in ["GOVTEST", "Databricks", "ADLS", "S3", "Snowflake", "IICS",
               "Informatica", "DQ_", "Azure", "GCP", "Oracle", "Salesforce"]:
        _add_hits(_cdgc_search(kw, size=50))
    return sources


def _get_catalog_source_uuid(system_name: str) -> str | None:
    """Return catalog source UUID (core.externalId prefix) for a given system name.

    Mirrors the production pattern:
      knowledgeQuery = "catalog source related to (system with name '{system}')"
      catalog_id = hits[0]['core.externalId'].split('://')[0]
    """
    query = f"catalog source related to (system with name '{system_name}')"
    url = (f"{CDGC_API_BASE}/data360/search/v1/assets"
           f"?knowledgeQuery={quote(query)}&segments=summary,systemAttributes")
    r = _request_cdgc("POST", url, json={"from": 0, "size": 10})
    if r.status_code >= 400:
        log.warning("catalog UUID lookup HTTP %s for system '%s'", r.status_code, system_name)
        return None
    hits = (r.json() or {}).get("hits") or []
    if not hits:
        return None
    ext_id = (hits[0].get("systemAttributes") or {}).get("core.externalId") or ""
    return ext_id.split("://")[0] if ext_id else None


def _list_mcc_catalog_sources() -> list[dict[str, Any]]:
    """List catalog sources registered in MCC for execution scanning.

    Tries multiple MCC API paths. Returns list of {id, name, type, ...}.
    Falls back to empty list on failure (caller uses CDGC-search UUID instead).
    """
    candidate_urls = [
        # Correct MCC execution listing endpoint (returns {"catalogSources": [...]}
        # with the executable source ids that /executable/v1/catalogsource/{id}
        # expects). The config/observable paths below 404 on current pods and are
        # kept only as defensive fallbacks.
        f"{CDGC_API_BASE}/data360/executable/v1/catalogsources",
        f"{CDGC_API_BASE}/data360/config/v1/catalogsources",
        f"{CDGC_API_BASE}/data360/observable/v1/catalogsources",
        f"{CDGC_API_BASE}/data360/config/v1/datasources",
    ]
    for url in candidate_urls:
        try:
            r = _request_cdgc("GET", url)
            if r.status_code == 200:
                data = r.json() or {}
                items = data if isinstance(data, list) else (
                    data.get("items") or data.get("catalogSources") or
                    data.get("dataSources") or data.get("hits") or []
                )
                log.info("_list_mcc_catalog_sources: %d sources from %s", len(items), url)
                return items
            log.info("_list_mcc_catalog_sources: HTTP %d from %s", r.status_code, url)
        except Exception as exc:
            log.info("_list_mcc_catalog_sources: %s error: %s", url, exc)
    return []


def _resolve_mcc_source_id(external_id_uuid: str, source_name: str) -> str:
    """Resolve the MCC execution catalog source ID.

    Tries:
    1. MCC observable/config API — list registered sources, match by UUID or name
    2. Falls back to the external_id UUID prefix (may work if source is registered)

    Returns the best-guess ID string (never raises).
    """
    mcc_sources = _list_mcc_catalog_sources()
    if mcc_sources:
        # Match by UUID
        for s in mcc_sources:
            sid = s.get("id") or s.get("sourceId") or s.get("catalogSourceId") or ""
            if sid and sid == external_id_uuid:
                log.info("_resolve_mcc_source_id: matched by UUID %s", sid)
                return sid
        # Match by name (case-insensitive substring)
        name_lc = source_name.lower()
        for s in mcc_sources:
            sname = (s.get("name") or s.get("sourceName") or "").lower()
            sid = s.get("id") or s.get("sourceId") or s.get("catalogSourceId") or ""
            if name_lc and sid and (name_lc in sname or sname in name_lc):
                log.info("_resolve_mcc_source_id: matched by name '%s' → id=%s", sname, sid)
                return sid
        # Log what we got so we can debug further
        log.info("_resolve_mcc_source_id: no match for uuid=%s name=%s; sources=%s",
                 external_id_uuid, source_name,
                 [{"id": s.get("id"), "name": s.get("name")} for s in mcc_sources[:10]])
    return external_id_uuid


def _get_mcc_source_name(source_id: str, fallback: str = "") -> str:
    """Return the registered MCC catalog-source *name* for a given executable id.

    Used for display so the UI shows the real catalog source (e.g. GOVTEST_PROVIDER)
    rather than the scanned table name. Falls back to `fallback` when the id can't
    be matched (e.g. the listing endpoint is unavailable)."""
    for s in _list_mcc_catalog_sources():
        sid = s.get("id") or s.get("sourceId") or s.get("catalogSourceId") or ""
        if sid and sid == source_id:
            return s.get("name") or s.get("sourceName") or fallback
    return fallback


def _trigger_mcc_scan(catalog_source_id: str, capabilities: list[str] | None = None) -> dict[str, Any]:
    """Trigger MCC catalog scan.

    Confirmed endpoint (requires both Bearer JWT and IDS-SESSION-ID):
      POST {CDGC_API_BASE}/data360/executable/v1/catalogsource/{catalog_source_id}
      Body: {"capabilityNames": [...]}
    Returns full job response dict (jobId, status, taskGroups, trackingURI).
    """
    url = f"{CDGC_API_BASE}/data360/executable/v1/catalogsource/{catalog_source_id}"

    caps = capabilities or ["Data Quality"]
    body = {"capabilityNames": caps}
    r = _request_cdgc("POST", url, json=body)
    log.info("_trigger_mcc_scan: caps=%s → HTTP %d %s", caps, r.status_code, r.text[:300])
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"MCC scan trigger HTTP {r.status_code}: {r.text[:600]}")
    return r.json() or {}


def _get_mcc_scan_status(job_id: str) -> dict[str, Any]:
    """Check status of an MCC scan job."""
    url = f"{CDGC_API_BASE}/data360/observable/v1/jobs/{job_id}?expandChildren=TASK-HIERARCHY"
    r = _request_cdgc("GET", url)
    if r.status_code >= 400:
        return {"status": "UNKNOWN", "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    return r.json() or {}


# ---------------------------------------------------------------------------
# MCP server setup
# ---------------------------------------------------------------------------
mcp = FastMCP("ai-governance")

# ---------------------------------------------------------------------------
# Tool 0: list_catalog_sources
# ---------------------------------------------------------------------------
@mcp.tool()
def list_catalog_sources(type_filter: str | None = None) -> dict[str, Any]:
    """List MCC catalog source connections registered in CDGC.

    Use this to discover available catalog sources (e.g. Snowflake, Oracle)
    and their IDs before calling scan_mcc_source or run_mcc_scan.

    Args:
      type_filter: Optional case-insensitive substring to filter by source type
                   or name (e.g. "snowflake", "oracle").

    Returns: {count, names: [...]}
    """
    sources = _list_catalog_sources()
    if type_filter:
        tf = type_filter.lower()
        sources = [
            s for s in sources
            if tf in (s.get("type") or "").lower()
            or tf in (s.get("name") or "").lower()
        ]
    return {
        "count": len(sources),
        "names": [s.get("name") for s in sources],
    }


# ---------------------------------------------------------------------------
# Tool 0: list_catalog_tables
# ---------------------------------------------------------------------------
@mcp.tool()
def list_catalog_tables(
    schema_filter: str | None = None,
    max_results: int = 300,
    group_by_source: bool = False,
) -> dict[str, Any]:
    """List all tables available in the CDGC catalog, grouped by schema or catalog source.

    Call this first to discover what tables exist before running onboard_and_govern.

    Args:
      schema_filter:   Optional substring filter on schema name (case-insensitive).
      max_results:     Max tables to return across all schemas (default 300).
      group_by_source: If True, group tables by catalog source name instead of schema path.

    Returns: {schemas: [{schema, connection, tables: [{name, id, external_id}]}],
              total_tables, catalog_sources}
    """
    # Collect catalog source names and their UUIDs for filtering
    sources = _list_catalog_sources()
    source_names = [s.get("name", "") for s in sources if s.get("name")]
    # UUID → source name (from core.externalId prefix before "://")
    uuid_to_source_name = {s.get("externalId", ""): s.get("name", "") for s in sources if s.get("externalId") and s.get("name")}
    # Connection name → source name (from the host/connection part after "://")
    connection_to_source_name = {s.get("connection", ""): s.get("name", "") for s in sources if s.get("connection") and s.get("name")}
    source_connections = [s.get("connection", "") for s in sources if s.get("connection")]
    # Internal identity UUID → source name (covers UUID-prefixed externalIds)
    identity_to_source_name: dict[str, str] = {s.get("id", ""): s.get("name", "") for s in sources if s.get("id") and s.get("name")}
    # Detect raw UUID strings — never use as display names
    _uuid_re = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-', re.IGNORECASE)
    def _readable_id(s: str) -> str | None:
        return s if s and not _uuid_re.match(s) else None

    # Source queries always run to completion; generic queries stop at max_results
    source_query_set = set(source_names + source_connections)
    generic_queries  = ["_", "TABLE", "STAGE", "FACT", "DIM", "VW", "VIEW"]
    source_queries   = list(dict.fromkeys(source_names + source_connections))

    SOURCE_QUERY_LIMIT = 2000

    def _fetch_query_hits(query: str, limit: int) -> list[dict[str, Any]]:
        return (_cdgc_search_paged(query, class_type="Table", max_results=limit)
                + _cdgc_search_paged(query, class_type="View",  max_results=limit))

    all_tables: dict[str, list[dict[str, Any]]] = {}
    seen_ids: set[str] = set()

    # Run all source queries in parallel — collects all hits first, then processes
    workers = min(len(source_queries), SCAN_THREAD_WORKERS)
    source_hits_ordered: list[list[dict[str, Any]]] = [[] for _ in source_queries]
    if workers > 0:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_fetch_query_hits, q, SOURCE_QUERY_LIMIT): i
                       for i, q in enumerate(source_queries)}
            for fut in as_completed(futures):
                source_hits_ordered[futures[fut]] = fut.result()

    # Build UUID→source_name from actual table hits: when we queried "Databricks" and
    # got tables whose externalId starts with "266c2b7a-...", that UUID IS the Databricks
    # source identifier. Map it so group headers show "Databricks" not the UUID.
    source_name_set = set(source_names)
    hit_uuid_to_source: dict[str, str] = {}
    for q, hits in zip(source_queries, source_hits_ordered):
        if q not in source_name_set:
            continue  # skip connection-string queries — only use clean source names
        for hit in hits:
            sa  = hit.get("systemAttributes") or {}
            eid = (hit.get("core.externalId") or sa.get("core.externalId") or sa.get("core.origin") or "")
            u   = eid.split("://")[0] if "://" in eid else ""
            if u and _uuid_re.match(u):
                hit_uuid_to_source.setdefault(u, q)  # first source query to claim this UUID wins

    # Seed UUID→source for sources whose schema name = source name (e.g. Snowflake: GOVTEST_MEMBER).
    # Source query table hits fail for these because table names don't contain the source name.
    # Browsing the schema by source name gives a sample table whose externalId reveals the UUID.
    def _seed_uuid_for_source(src_name: str) -> tuple[str, str] | None:
        for h in _browse_all_tables_in_schema(src_name)[:3]:
            sa  = h.get("systemAttributes") or {}
            eid = sa.get("core.externalId") or ""
            u   = eid.split("://")[0] if "://" in eid else ""
            if u and _uuid_re.match(u):
                return (u, src_name)
        return None

    seed_workers = min(len(source_names), SCAN_THREAD_WORKERS)
    if seed_workers > 0:
        with ThreadPoolExecutor(max_workers=seed_workers) as ex:
            for pair in ex.map(_seed_uuid_for_source, source_names):
                if pair:
                    u, sname = pair
                    hit_uuid_to_source.setdefault(u, sname)
    log.info("hit_uuid_to_source seeded: %d entries for %d sources", len(hit_uuid_to_source), len(source_names))

    for hits in source_hits_ordered:
        for hit in hits:
            tid = _id_of(hit)
            if not tid or tid in seen_ids:
                continue

            sa     = hit.get("systemAttributes") or {}
            ext_id = (hit.get("core.externalId")
                      or sa.get("core.externalId")
                      or sa.get("core.origin") or "")

            asset_uuid = (ext_id.split("://")[0] if "://" in ext_id else "") if ext_id else ""

            seen_ids.add(tid)

            schema     = ""
            database   = ""
            connection = ""
            if ext_id and "://" in ext_id:
                path_part = ext_id.split("://", 1)[1].split("~")[0]
                parts = path_part.split("/")
                if len(parts) >= 4:
                    connection = parts[0]
                    database   = parts[1]
                    schema     = parts[-2]
                elif len(parts) >= 3:
                    schema     = parts[-2]
                    connection = parts[0]
                elif len(parts) == 2:
                    schema     = parts[0]

            if schema_filter and schema_filter.lower() not in schema.lower():
                continue

            if group_by_source:
                cat_name = (uuid_to_source_name.get(asset_uuid)
                            or connection_to_source_name.get(connection)
                            or identity_to_source_name.get(asset_uuid)
                            or hit_uuid_to_source.get(asset_uuid)
                            or _readable_id(asset_uuid)
                            or schema
                            or "(unknown)")
                key = (cat_name, database or "", schema or "(no schema)")
            else:
                key = f"{connection}/{schema}" if connection else schema or "(unknown)"
            all_tables.setdefault(key, []).append({
                "name":        _name_of(hit),
                "id":          tid,
                "external_id": ext_id,
            })

    # Generic catch-all queries run sequentially, capped at max_results total
    for gq in generic_queries:
        if gq in source_query_set:
            continue
        if len(seen_ids) >= max_results:
            break
        for hit in _fetch_query_hits(gq, max_results):
            if len(seen_ids) >= max_results:
                break
            tid = _id_of(hit)
            if not tid or tid in seen_ids:
                continue
            sa     = hit.get("systemAttributes") or {}
            ext_id = (hit.get("core.externalId") or sa.get("core.externalId") or sa.get("core.origin") or "")
            asset_uuid = (ext_id.split("://")[0] if "://" in ext_id else "") if ext_id else ""
            seen_ids.add(tid)
            schema = database = connection = ""
            if ext_id and "://" in ext_id:
                parts = ext_id.split("://", 1)[1].split("~")[0].split("/")
                if len(parts) >= 4:
                    connection, database, schema = parts[0], parts[1], parts[-2]
                elif len(parts) >= 3:
                    schema, connection = parts[-2], parts[0]
                elif len(parts) == 2:
                    schema = parts[0]
            if schema_filter and schema_filter.lower() not in schema.lower():
                continue
            if group_by_source:
                cat_name = (uuid_to_source_name.get(asset_uuid) or connection_to_source_name.get(connection) or identity_to_source_name.get(asset_uuid) or hit_uuid_to_source.get(asset_uuid) or _readable_id(asset_uuid) or schema or "(unknown)")
                key = (cat_name, database or "", schema or "(no schema)")
            else:
                key = f"{connection}/{schema}" if connection else schema or "(unknown)"
            all_tables.setdefault(key, []).append({"name": _name_of(hit), "id": tid, "external_id": ext_id})

    # ── Schema hierarchy browse: get ALL tables, bypassing knowledgeQuery relevance cap ──
    # Only browse sources that already have BROWSE_THRESHOLD+ tables from keyword search —
    # small sources (accuweather: 24, bakehouse: 12, etc.) are already complete and don't need it.
    tables_per_source: dict[str, int] = {}
    for key_t, tbls_t in all_tables.items():
        src_t = key_t[0] if isinstance(key_t, tuple) else key_t.split("/")[0]
        tables_per_source[src_t] = tables_per_source.get(src_t, 0) + len(tbls_t)
    # Use all distinct source names from discovered tables (not just _list_catalog_sources)
    all_source_names = list(tables_per_source.keys())
    browse_names = [n for n in all_source_names if n and tables_per_source.get(n, 0) >= BROWSE_THRESHOLD]
    log.info("browse supplement: %d of %d sources qualify (>=%d tables)",
             len(browse_names), len(source_names), BROWSE_THRESHOLD)
    if browse_names:
        def _process_browse_hit(hit: dict[str, Any], src_name: str) -> tuple[Any, dict] | None:
            tid = _id_of(hit)
            if not tid or tid in seen_ids:
                return None
            sa     = hit.get("systemAttributes") or {}
            ext_id = (hit.get("core.externalId") or sa.get("core.externalId") or sa.get("core.origin") or "")
            asset_uuid = (ext_id.split("://")[0] if "://" in ext_id else "") if ext_id else ""
            schema_     = ""
            database_   = ""
            connection_ = ""
            if ext_id and "://" in ext_id:
                parts = ext_id.split("://", 1)[1].split("~")[0].split("/")
                if len(parts) >= 4:
                    connection_, database_, schema_ = parts[0], parts[1], parts[-2]
                elif len(parts) >= 3:
                    schema_     = parts[-2]
                    connection_ = parts[0]
                elif len(parts) == 2:
                    schema_     = parts[0]
            if schema_filter and schema_filter.lower() not in schema_.lower():
                return None
            if group_by_source:
                cat_name = (uuid_to_source_name.get(asset_uuid)
                            or connection_to_source_name.get(connection_)
                            or identity_to_source_name.get(asset_uuid)
                            or hit_uuid_to_source.get(asset_uuid)
                            or src_name
                            or _readable_id(asset_uuid)
                            or schema_ or "(unknown)")
                key = (cat_name, database_ or "", schema_ or "(no schema)")
            else:
                key = f"{connection_}/{schema_}" if connection_ else schema_ or "(unknown)"
            return (tid, key, {"name": _name_of(hit), "id": tid, "external_id": ext_id})

        def _browse_one_source(src_name: str) -> list[tuple[Any, dict]]:
            hits = _browse_all_tables_in_schema(src_name)
            out  = []
            for h in hits:
                r = _process_browse_hit(h, src_name)
                if r:
                    out.append(r)
            return out

        browse_workers = min(len(browse_names), SCAN_THREAD_WORKERS)
        with ThreadPoolExecutor(max_workers=browse_workers) as ex:
            browse_futures = {ex.submit(_browse_one_source, n): n for n in browse_names}
            for fut in as_completed(browse_futures):
                for tid, key, row in fut.result():
                    if tid not in seen_ids:   # double-check after parallel writes
                        seen_ids.add(tid)
                        all_tables.setdefault(key, []).append(row)
        log.info("browse supplement: seen_ids now %d", len(seen_ids))

    if group_by_source:
        # Build nested: catalog source → database → schema → tables
        nested: dict[str, dict[str, dict[str, list]]] = {}
        for (cat_name, db_name, schema_name), tbls in all_tables.items():
            nested.setdefault(cat_name, {}).setdefault(db_name, {}).setdefault(schema_name, []).extend(tbls)

        catalog_sources_out = []
        for cat_name, db_dict in sorted(nested.items()):
            databases_out: list[dict] = []
            schemas_flat:  list[dict] = []  # flat list for backward-compat (scan dropdowns)
            total = 0
            for db_name, schema_dict in sorted(db_dict.items()):
                schemas_in_db = [
                    {
                        "schema": schema_name,
                        "tables": sorted(tbls, key=lambda x: x["name"]),
                    }
                    for schema_name, tbls in sorted(schema_dict.items())
                ]
                databases_out.append({"database": db_name, "schemas": schemas_in_db})
                schemas_flat.extend(schemas_in_db)
                total += sum(len(s["tables"]) for s in schemas_in_db)
            catalog_sources_out.append({
                "source":      cat_name,
                "databases":   databases_out,
                "schemas":     schemas_flat,   # flat — used by scan dropdowns
                "total_tables": total,
            })
        return {
            "catalog_sources_grouped": catalog_sources_out,
            "total_tables":            sum(s["total_tables"] for s in catalog_sources_out),
            "catalog_source_names":    source_names,
        }

    schemas_out = [
        {
            "schema":     key.split("/", 1)[-1] if "/" in key else key,
            "connection": key.split("/", 1)[0] if "/" in key else "",
            "tables":     sorted(tbls, key=lambda x: x["name"]),
        }
        for key, tbls in sorted(all_tables.items())
    ]

    return {
        "schemas":         schemas_out,
        "total_tables":    sum(len(s["tables"]) for s in schemas_out),
        "catalog_sources": source_names,
    }


# ---------------------------------------------------------------------------
# Tool 1a: scan_find_tables  (fast — finds table IDs only, no columns)
# ---------------------------------------------------------------------------
@mcp.tool()
def scan_find_tables(
    table_names: list[str],
    schema_hint: str | None = None,
) -> dict[str, Any]:
    """Find table IDs in CDGC without fetching columns. Returns in ~3 seconds.

    Use this as the first half of a two-phase scan:
      1. scan_find_tables  → discover which tables exist and get their IDs
      2. scan_fetch_columns → fetch column metadata for each table (call once per table)

    Args:
      table_names: Tables to locate (e.g. ["SUPPLIER_SITE_STAGE"]).
      schema_hint: Optional schema name to disambiguate duplicate table names.

    Returns: {found_count, tables:[{name, internal_id, external_id, schema, connection}],
              missing:[...], next_actions:[{tool, params}]}
    """
    SCAN_CACHE_DIR.mkdir(exist_ok=True)
    found: list[dict[str, Any]] = []
    missing: list[str] = []

    def _lookup_one(tname: str) -> dict[str, Any] | None:
        """Resolve one table name to its CDGC asset. Returns None if not found."""
        query = f"{schema_hint}.{tname}" if schema_hint else tname
        hits  = _cdgc_search(query, class_type="Table", size=5)
        if not hits:
            hits = _cdgc_search(tname, class_type="Table", size=5)
        if not hits:
            hits = _cdgc_search(query, class_type="View", size=5)
        if not hits:
            hits = _cdgc_search(tname, class_type="View", size=5)
        if not hits:
            return None

        name_lc    = tname.lower()
        exact      = [h for h in hits if _name_of(h).lower() == name_lc]
        candidates = exact or hits
        # Prefer candidate whose externalId contains schema_hint to avoid picking a
        # same-named table from a different schema.
        if schema_hint and len(candidates) > 1:
            sh_up = schema_hint.upper()
            schema_matched = [
                h for h in candidates
                if sh_up in (
                    h.get("core.externalId")
                    or (h.get("systemAttributes") or {}).get("core.externalId")
                    or ""
                ).upper()
            ]
            if schema_matched:
                candidates = schema_matched
        hit    = candidates[0]
        tid    = _id_of(hit)
        ext_id = (hit.get("core.externalId")
                  or (hit.get("systemAttributes") or {}).get("core.externalId")
                  or "")
        schema_    = ""
        connection = ""
        if ext_id and "://" in ext_id:
            parts = ext_id.split("://", 1)[1].split("~")[0].split("/")
            if len(parts) >= 3:
                schema_    = parts[-2]
                connection = parts[0]
        log.info("scan_find_tables: found %s (id=%s)", tname, (tid or "?")[:12])
        return {
            "name":        _name_of(hit) or tname,
            "internal_id": tid,
            "external_id": ext_id,
            "schema":      schema_,
            "connection":  connection,
        }

    # Run all table lookups in parallel — each is an independent CDGC search
    workers = min(len(table_names), SCAN_THREAD_WORKERS)
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            lookup_results = list(ex.map(_lookup_one, table_names))
    else:
        lookup_results = [_lookup_one(t) for t in table_names]

    for tname, result in zip(table_names, lookup_results):
        if result:
            found.append(result)
        else:
            missing.append(tname)

    # Persist stubs so scan_fetch_columns + govern("taxonomy") can reconstruct full state.
    # Only overwrite scan_pending if we actually found tables — never wipe valid prior state
    # with an empty result (avoids erasing a successful scan when govern re-dispatches scan).
    state = _load_govern_state()
    if found:
        found_names = [t["name"] for t in found]
        # State is sharded per table: scanning a different table loads that table's own
        # slot (resuming its prior progress if any) instead of carrying this table's
        # downstream state across. Re-scanning the same table keeps its slot intact.
        if _table_key(found_names) != _table_key(state.get("table_names")):
            state = _load_govern_state(key=_table_key(found_names))
        state["scan_pending"] = {"tables": found, "schema_hint": schema_hint}
        state["table_names"]  = found_names
        _save_govern_state(state)

    return {
        "found_count": len(found),
        "missing":     missing,
        "tables":      found,
        "next_actions": [
            {
                "tool": "scan_fetch_columns",
                "params": {
                    "table_name":  t["name"],
                    "table_id":    t["internal_id"],
                    "schema":      t["schema"],
                    "external_id": t["external_id"],
                },
            }
            for t in found
        ],
    }


# ---------------------------------------------------------------------------
# Tool 1b: scan_fetch_columns  (per-table column fetch, ~15s, cached)
# ---------------------------------------------------------------------------
@mcp.tool()
def scan_fetch_columns(
    table_name: str,
    table_id: str,
    schema: str = "",
    external_id: str = "",
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Fetch column metadata for one table and cache it to disk. (~15 seconds)

    Call this once per table returned by scan_find_tables. Each call completes
    independently and shows visible progress in Claude Code before the next begins.
    Results are cached — re-runs return instantly from disk.

    Args:
      table_name:    Table name (used for cache key and logging).
      table_id:      Internal CDGC ID from scan_find_tables.
      schema:        Schema name (from scan_find_tables result).
      external_id:   External ID string (from scan_find_tables result).
      force_refresh: Bypass disk cache and re-fetch from CDGC.

    Returns: {table, column_count, columns_preview:[first 10], source, message}
    """
    SCAN_CACHE_DIR.mkdir(exist_ok=True)
    cache_key  = re.sub(r"[^\w]", "_", table_name.upper())
    cache_file = SCAN_CACHE_DIR / f"{cache_key}.json"

    if not force_refresh and cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < SCAN_CACHE_TTL:
            log.info("scan_fetch_columns: cache hit for %s (age %.0fs)", table_name, age)
            record = json.loads(cache_file.read_text())
            _mark_scan_pending_fetched(table_name)
            return {
                "table":           table_name,
                "column_count":    len(record.get("columns", [])),
                "source":          "cache",
                "columns_preview": [c["name"] for c in record.get("columns", [])[:10]],
                "message":         f"Loaded {len(record.get('columns', []))} columns from cache for {table_name}.",
            }

    log.info("scan_fetch_columns: fetching columns for %s (id=%s) with %d threads",
             table_name, (table_id or "?")[:12], SCAN_THREAD_WORKERS)
    columns: list[dict[str, Any]] = []
    if table_id:
        details = _cdgc_get_asset(table_id, segments="summary,systemAttributes,hierarchy")
        hier = details.get("hierarchy") or []
        if isinstance(hier, dict):
            hier = hier.get("children") or hier.get("items") or []
        columns = _fetch_columns_parallel(hier, table_name=table_name)

    record = {
        "name":        table_name,
        "internal_id": table_id,
        "external_id": external_id,
        "schema":      schema,
        "columns":     columns,
    }
    cache_file.write_text(json.dumps(record))
    _mark_scan_pending_fetched(table_name)

    log.info("scan_fetch_columns: %s complete — %d columns cached", table_name, len(columns))
    return {
        "table":           table_name,
        "column_count":    len(columns),
        "source":          "fetched",
        "columns_preview": [c["name"] for c in columns[:10]],
        "message":         f"Fetched and cached {len(columns)} columns for {table_name}.",
    }


def _mark_scan_pending_fetched(table_name: str) -> None:
    """Mark a table as columns-fetched in scan_pending state."""
    state   = _load_govern_state()
    pending = state.get("scan_pending") or {}
    for t in pending.get("tables", []):
        if t["name"].upper() == table_name.upper():
            t["columns_fetched"] = True
            break
    state["scan_pending"] = pending
    _save_govern_state(state)


def _reconstruct_scan_from_cache(pending: dict[str, Any]) -> dict[str, Any] | None:
    """Build a full scan result from per-table cache files written by scan_fetch_columns."""
    tables = []
    for pt in pending.get("tables", []):
        cache_key  = re.sub(r"[^\w]", "_", pt["name"].upper())
        cache_file = SCAN_CACHE_DIR / f"{cache_key}.json"
        if cache_file.exists():
            tables.append(json.loads(cache_file.read_text()))
        else:
            return None  # not all tables cached yet
    if not tables:
        return None
    return {"tables": tables, "discovered_count": len(tables), "missing": []}


# ---------------------------------------------------------------------------
# Tool 1: scan_mcc_source  (original single-call scan, kept for direct use)
# ---------------------------------------------------------------------------
@mcp.tool()
def scan_mcc_source(
    table_names: list[str],
    connection_id: str | None = None,
    schema_hint: str | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Discover table and column metadata from the CDGC catalog.

    Searches CDGC for each named table, fetches its column hierarchy, and
    returns a structured metadata snapshot ready for LLM taxonomy generation.

    Args:
      table_names:   List of table names to discover (e.g. ["SUPPLIER_STAGE", "ORDERS"]).
      connection_id: Optional IDMC connection ID to narrow results.
      schema_hint:   Optional schema name to disambiguate duplicate table names.
      force_refresh: Bypass disk cache and re-fetch from CDGC.

    Returns: {tables: [{name, internal_id, external_id, schema, columns:[{name, data_type}]}],
              discovered_count, missing:[...]}
    """
    SCAN_CACHE_DIR.mkdir(exist_ok=True)
    discovered = []
    missing    = []

    for tname in table_names:
        # Disk cache — skip expensive column fetch if recently scanned
        cache_key  = re.sub(r"[^\w]", "_", tname.upper())
        cache_file = SCAN_CACHE_DIR / f"{cache_key}.json"
        if not force_refresh and cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < SCAN_CACHE_TTL:
                log.info("scan_mcc_source: cache hit for %s (age %.0fs)", tname, age)
                discovered.append(json.loads(cache_file.read_text()))
                continue

        query = f"{schema_hint}.{tname}" if schema_hint else tname
        hits  = _cdgc_search(query, class_type="Table", size=5)
        if not hits:
            hits = _cdgc_search(tname, class_type="Table", size=5)
        if not hits:
            missing.append(tname)
            continue

        # Prefer exact-name match
        name_lc = tname.lower()
        exact = [h for h in hits if _name_of(h).lower() == name_lc]
        hit   = (exact or hits)[0]
        tid   = _id_of(hit)

        columns: list[dict[str, Any]] = []
        if tid:
            details = _cdgc_get_asset(tid, segments="summary,systemAttributes,hierarchy")
            hier = details.get("hierarchy") or []
            if isinstance(hier, dict):
                hier = hier.get("children") or hier.get("items") or []
            columns = _fetch_columns_parallel(hier, table_name=tname)

        ext_id = hit.get("core.externalId") or ""
        # Extract schema from externalId path: origin://DB/SCHEMA/TABLE~classType
        schema = ""
        if ext_id and "://" in ext_id:
            path_part = ext_id.split("://", 1)[1].split("~")[0]
            parts = path_part.split("/")
            if len(parts) >= 3:
                schema = parts[-2]

        table_record = {
            "name":        _name_of(hit) or tname,
            "internal_id": tid,
            "external_id": ext_id,
            "schema":      schema,
            "columns":     columns,
        }
        try:
            cache_file.write_text(json.dumps(table_record))
        except Exception:
            pass
        discovered.append(table_record)

    return {
        "discovered_count": len(discovered),
        "missing":          missing,
        "tables":           discovered,
    }


# ---------------------------------------------------------------------------
# Tool 2: generate_governance_taxonomy
# ---------------------------------------------------------------------------
@mcp.tool()
def generate_governance_taxonomy(
    table_metadata: list[dict[str, Any]] | None = None,
    domain_hint: str | None = None,
    organization_context: str | None = None,
    table_names: list[str] | None = None,
) -> dict[str, Any]:
    """Use Claude AI to generate a domain/subdomain/business-term taxonomy.

    Analyzes table names and column names to produce a structured CDGC
    governance taxonomy: one or more domains, subdomains per domain, and
    business terms with definitions and column-mappings.

    Args:
      table_metadata:       List of table dicts with 'name' and 'columns' keys.
                            If omitted, pass table_names and columns are loaded from cache.
      table_names:          Alternative to table_metadata — load column data from scan cache.
      domain_hint:          Optional top-level domain name (e.g. "Finance",
                            "Supply Chain"). LLM infers one if absent.
      organization_context: Optional 1-2 sentence business context to help the
                            LLM write better definitions.

    Returns: {domains:[{name, description, subdomains:[{name, description,
              business_terms:[{name, definition, synonyms, columns:[...]}]}]}]}
    """
    # Resolve table_metadata from cache if only names provided
    if not table_metadata and table_names:
        SCAN_CACHE_DIR.mkdir(exist_ok=True)
        table_metadata = []
        for tname in table_names:
            cache_key  = re.sub(r"[^\w]", "_", tname.upper())
            cache_file = SCAN_CACHE_DIR / f"{cache_key}.json"
            if cache_file.exists():
                table_metadata.append(json.loads(cache_file.read_text()))
            else:
                table_metadata.append({"name": tname, "columns": []})

    if not table_metadata:
        # Fall back to session state scan_pending
        state   = _load_govern_state()
        pending = state.get("scan_pending", {})
        scan    = _reconstruct_scan_from_cache(pending)
        table_metadata = scan.get("tables", []) if scan else []

    if not table_metadata and SCAN_CACHE_DIR.exists():
        # Last resort: load ALL cached table files from disk
        table_metadata = [
            json.loads(f.read_text())
            for f in sorted(SCAN_CACHE_DIR.glob("*.json"))
            if f.is_file()
        ]

    log.info("generate_governance_taxonomy: %d tables loaded for LLM", len(table_metadata or []))
    MAX_TABLES      = 30   # cap tables — too many causes LLM response truncation
    MAX_COLS_PER_TABLE = 15  # fewer cols per table to stay well within token budget
    table_metadata = (table_metadata or [])[:MAX_TABLES]
    tables_summary = []
    for t in table_metadata:
        col_names = [
            c if isinstance(c, str) else c.get("name", "")
            for c in (t.get("columns") or [])
        ]
        tables_summary.append({
            "table":   t["name"],
            "columns": col_names[:MAX_COLS_PER_TABLE],
        })

    system_prompt = """You are a senior data governance architect.
Given a list of database tables and their columns, produce a structured business
glossary taxonomy in JSON. Rules:
- Group related tables into logical domains and subdomains.
- For each unique concept represented by a column (or group of columns), create
  a BusinessTerm with a clear, jargon-free definition (2-3 sentences).
- Map each BusinessTerm to the specific column(s) it represents.
- Return ONLY valid JSON matching this exact schema:
{
  "domains": [
    {
      "name": "...",
      "description": "...",
      "subdomains": [
        {
          "name": "...",
          "description": "...",
          "business_terms": [
            {
              "name": "...",
              "definition": "...",
              "synonyms": ["..."],
              "source_columns": ["TABLE.COLUMN", ...]
            }
          ]
        }
      ]
    }
  ]
}"""

    user_msg_parts = [
        f"Tables to analyze:\n{json.dumps(tables_summary, indent=2)}",
    ]
    if domain_hint:
        user_msg_parts.append(f"\nTop-level domain: {domain_hint}")
    if organization_context:
        user_msg_parts.append(f"\nOrganization context: {organization_context}")

    try:
        result = _llm_json(system_prompt, "\n".join(user_msg_parts))
    except Exception as e:
        log.warning("generate_governance_taxonomy: LLM JSON parse failed (%s) — retrying with fewer tables", e)
        # Retry with half the tables to avoid response truncation
        half = max(1, len(tables_summary) // 2)
        reduced_msg = f"Tables to analyze:\n{json.dumps(tables_summary[:half], indent=2)}"
        if domain_hint:
            reduced_msg += f"\nTop-level domain: {domain_hint}"
        result = _llm_json(system_prompt, reduced_msg)

    # Normalize to expected shape
    if "domains" not in result and isinstance(result, list):
        result = {"domains": result}

    total_terms = sum(
        len(sd.get("business_terms", []))
        for d in result.get("domains", [])
        for sd in d.get("subdomains", [])
    )
    result["_summary"] = {
        "domain_count":   len(result.get("domains", [])),
        "subdomain_count": sum(len(d.get("subdomains", [])) for d in result.get("domains", [])),
        "term_count":     total_terms,
    }
    # Persist to govern state so downstream steps (domain_structure, curate) can read it
    state = _load_govern_state()
    state["taxonomy"] = result
    _save_govern_state(state)
    return result


def _flatten_taxonomy_for_approval(taxonomy: dict) -> list[dict]:
    """Flatten taxonomy tree into a flat list of items for user selection."""
    items: list[dict] = []
    for domain in taxonomy.get("domains", []):
        items.append({"type": "Domain", "name": domain["name"], "parent": None})
        for sd in domain.get("subdomains", []):
            items.append({"type": "SubDomain", "name": sd["name"], "parent": domain["name"]})
            for bt in sd.get("business_terms", []):
                items.append({"type": "BusinessTerm", "name": bt["name"], "parent": sd["name"]})
    return items


def _filter_taxonomy_by_names(taxonomy: dict, approved: set) -> dict:
    """Return taxonomy containing only approved items.

    A Domain/SubDomain is included if it or any of its descendants are approved
    (selecting a BusinessTerm implicitly includes its parent Domain and SubDomain).
    """
    filtered_domains: list[dict] = []
    for domain in taxonomy.get("domains", []):
        filtered_sds: list[dict] = []
        for sd in domain.get("subdomains", []):
            bts = [bt for bt in sd.get("business_terms", []) if bt["name"] in approved]
            # Include subdomain if it is approved OR has any approved business terms
            if sd["name"] in approved or bts:
                filtered_sds.append({**sd, "business_terms": bts})
        # Include domain if it is approved OR has any included subdomains
        if domain["name"] in approved or filtered_sds:
            filtered_domains.append({**domain, "subdomains": filtered_sds})
    return {"domains": filtered_domains}


# ---------------------------------------------------------------------------
# Tool 3: create_domain_structure
# ---------------------------------------------------------------------------
@mcp.tool()
def create_domain_structure(
    taxonomy: dict[str, Any],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create Domain, SubDomain, and BusinessTerm assets in CDGC.

    Takes the taxonomy output from generate_governance_taxonomy and creates
    the full hierarchy in CDGC via the content API.

    Args:
      taxonomy: Output from generate_governance_taxonomy.
      dry_run:  When True, return what would be created without calling CDGC.

    Returns: {created:[{type, name, id}], skipped:[...], errors:[...]}
    """
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors:  list[dict[str, Any]] = []

    for domain in taxonomy.get("domains", []):
        dname = domain["name"]
        ddesc = domain.get("description", "")

        # Check if domain already exists
        hits = _cdgc_search(dname, class_type="Domain", size=5)
        exact = [h for h in hits if _name_of(h).lower() == dname.lower()]

        if dry_run:
            created.append({"type": "Domain", "name": dname, "id": "(dry_run)"})
        elif exact:
            domain_id = _id_of(exact[0])
            skipped.append({"type": "Domain", "name": dname, "id": domain_id, "reason": "already exists"})
        else:
            try:
                resp    = _cdgc_create_asset(CLASS_DOMAIN, dname, ddesc)
                domain_id = resp.get("core.identity") or resp.get("id") or ""
                created.append({"type": "Domain", "name": dname, "id": domain_id})
            except Exception as e:
                errors.append({"type": "Domain", "name": dname, "error": str(e)})
                domain_id = ""

        if not dry_run and not domain_id:
            domain_id = _id_of(exact[0]) if exact else ""

        for subdomain in domain.get("subdomains", []):
            sdname = subdomain["name"]
            sddesc = subdomain.get("description", "")

            sd_hits = _cdgc_search(sdname, class_type="SubDomain", size=5)
            sd_exact = [h for h in sd_hits if _name_of(h).lower() == sdname.lower()]

            if dry_run:
                subdomain_id = "(dry_run)"
                created.append({"type": "SubDomain", "name": sdname, "id": subdomain_id, "parent": dname})
            elif sd_exact:
                subdomain_id = _id_of(sd_exact[0])
                skipped.append({"type": "SubDomain", "name": sdname, "id": subdomain_id, "reason": "already exists"})
            else:
                try:
                    resp = _cdgc_create_asset(
                        CLASS_SUBDOMAIN, sdname, sddesc,
                        parent_id=domain_id if domain_id else None,
                    )
                    subdomain_id = resp.get("core.identity") or resp.get("id") or ""
                    created.append({"type": "SubDomain", "name": sdname, "id": subdomain_id, "parent": dname})
                except Exception as e:
                    errors.append({"type": "SubDomain", "name": sdname, "error": str(e)})
                    subdomain_id = ""

            if not dry_run and not subdomain_id:
                subdomain_id = _id_of(sd_exact[0]) if sd_exact else ""

            for term in subdomain.get("business_terms", []):
                tname   = term["name"]
                tdef    = term.get("definition", "")
                tsyns   = term.get("synonyms", [])

                t_hits  = _cdgc_search(tname, class_type="BusinessTerm", size=5)
                t_exact = [h for h in t_hits if _name_of(h).lower() == tname.lower()]

                if dry_run:
                    term_id = "(dry_run)"
                    created.append({"type": "BusinessTerm", "name": tname, "id": term_id, "subdomain": sdname})
                elif t_exact:
                    term_id = _id_of(t_exact[0])
                    skipped.append({"type": "BusinessTerm", "name": tname, "id": term_id, "reason": "already exists"})
                else:
                    try:
                        resp = _cdgc_create_asset(
                            CLASS_BUSINESS_TERM, tname, tdef,
                            parent_id=subdomain_id if subdomain_id else None,
                            extra_self={
                                "com.infa.ccgf.models.governance.FormatType": "Text",
                                "com.infa.ccgf.models.governance.isCDE":      False,
                                "com.infa.ccgf.models.governance.AliasNames": tsyns,
                            },
                        )
                        term_id = resp.get("core.identity") or resp.get("id") or ""
                        created.append({"type": "BusinessTerm", "name": tname, "id": term_id, "subdomain": sdname})
                    except Exception as e:
                        errors.append({"type": "BusinessTerm", "name": tname, "error": str(e)})

    return {
        "dry_run": dry_run,
        "created": created,
        "skipped": skipped,
        "errors":  errors,
        "summary": {
            "created_count": len(created),
            "skipped_count": len(skipped),
            "error_count":   len(errors),
        },
    }


# ---------------------------------------------------------------------------
# Tool 3b: approve_domain_structure
# ---------------------------------------------------------------------------
@mcp.tool()
def approve_domain_structure(
    approved_names: list[str],
    renames: dict[str, str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create selected domain structure items in CDGC after user multi-select approval.

    Call this after govern() returns awaiting_approval for the domain_structure step.
    Pass the list of item names the user approved via multi-select.

    Args:
      approved_names: Names of Domains / SubDomains / BusinessTerms to create in CDGC.
                      Use the ORIGINAL names (before any renames).
      renames:        Optional map of {original_name: new_name} for items the user renamed.
      dry_run:        If True, show what would be created without calling CDGC.

    Returns: {created, skipped, errors, summary}
    """
    state = _load_govern_state()
    taxonomy = state.get("taxonomy")
    if not taxonomy:
        return {"error": "No taxonomy in state. Run 'taxonomy' step first."}

    # Build reverse map: new_name → original_name so we can match approved_names
    # which may contain either original or renamed names.
    rev = {v: k for k, v in (renames or {}).items()}

    # Normalise approved_names to original names for taxonomy filtering
    original_approved = set()
    for n in approved_names:
        original_approved.add(rev.get(n, n))  # map back if renamed

    filtered = _filter_taxonomy_by_names(taxonomy, original_approved)

    # Apply renames to the filtered taxonomy before creating in CDGC
    if renames:
        def _rename_node(name: str) -> str:
            return renames.get(name, name)

        renamed_domains = []
        for d in filtered.get("domains", []):
            renamed_sds = []
            for sd in d.get("subdomains", []):
                renamed_bts = [
                    {**bt, "name": _rename_node(bt["name"])}
                    for bt in sd.get("business_terms", [])
                ]
                renamed_sds.append({**sd, "name": _rename_node(sd["name"]), "business_terms": renamed_bts})
            renamed_domains.append({**d, "name": _rename_node(d["name"]), "subdomains": renamed_sds})
        filtered = {"domains": renamed_domains}

    result = create_domain_structure(filtered, dry_run=dry_run)
    state["domain_structure"] = result
    state.pop("domain_structure_pending_items", None)
    _save_govern_state(state)
    return result


# ---------------------------------------------------------------------------
# Tool 4: create_system_and_dataset
# ---------------------------------------------------------------------------
@mcp.tool()
def create_system_and_dataset(
    system_name: str,
    dataset_name: str,
    description: str = "",
    domain_name: str | None = None,
    connection_id: str | None = None,
    table_ids: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create a System and Dataset asset pair in CDGC.

    A System represents the source platform (e.g. "Oracle ERP Cloud").
    A Dataset is the logical grouping of related tables (e.g. "Supplier Data").

    Args:
      system_name:   Display name for the source system.
      dataset_name:  Display name for the logical dataset.
      description:   Shared description for both.
      domain_name:   Optional Domain to link the Dataset to.
      connection_id: Optional IDMC connection ID (stored in selfAttributes).
      table_ids:     Optional list of catalog table internal IDs to link as
                     data elements on the dataset.
      dry_run:       Return planned actions without calling CDGC.

    Returns: {system:{id,name}, dataset:{id,name}, linked_domain, data_elements, dry_run}
    """
    results: dict[str, Any] = {"dry_run": dry_run}

    # Resolve domain id if given
    domain_id = None
    if domain_name:
        d_hits = _cdgc_search(domain_name, class_type="Domain", size=5)
        d_exact = [h for h in d_hits if _name_of(h).lower() == domain_name.lower()]
        domain_id = _id_of(d_exact[0]) if d_exact else None
        results["linked_domain"] = {"name": domain_name, "id": domain_id}

    if dry_run:
        results["system"]  = {"id": "(dry_run)", "name": system_name}
        results["dataset"] = {"id": "(dry_run)", "name": dataset_name}
        return results

    # Create (or find existing) System
    sys_hits  = _cdgc_search(system_name, size=5)
    sys_exact = [h for h in sys_hits
                 if _name_of(h).lower() == system_name.lower()
                 and "System" in ((h.get("systemAttributes") or {}).get("core.classType") or "")]
    if sys_exact:
        system_id   = _id_of(sys_exact[0])
        results["system"] = {"id": system_id, "name": system_name, "note": "already exists"}
    else:
        try:
            resp = _cdgc_create_asset(
                "com.infa.ccgf.models.governance.System",
                system_name, description,
                parent_id=None,
            )
            system_id = resp.get("core.identity") or resp.get("id") or ""
            results["system"] = {"id": system_id, "name": system_name}
        except Exception as e:
            results["system"] = {"error": str(e), "name": system_name}
            system_id = None

    # Create (or find existing) Dataset, always parented under the system
    dataset_id: str | None = None
    ds_hits   = _cdgc_search(dataset_name, class_type="DataSet", size=5)
    ds_exact  = [h for h in ds_hits if _name_of(h).lower() == dataset_name.lower()]
    if ds_exact:
        dataset_id = _id_of(ds_exact[0])
        results["dataset"] = {"id": dataset_id, "name": dataset_name, "note": "already exists"}
        # Re-parent to the system if the system was just resolved/created
        if system_id:
            try:
                url = f"{CDGC_API_BASE}/data360/content/v1/assets/{dataset_id}"
                _request_cdgc("PATCH", url, json={"parent": {"core.identity": system_id}})
                results["dataset"]["reparented_to"] = system_id
            except Exception as e:
                results["dataset"]["reparent_error"] = str(e)
    else:
        try:
            resp = _cdgc_create_asset(
                CLASS_DATASET, dataset_name, description,
                parent_id=system_id or None,
            )
            dataset_id = resp.get("core.identity") or resp.get("id") or ""
            results["dataset"] = {"id": dataset_id, "name": dataset_name}
        except Exception as e:
            results["dataset"] = {"error": str(e), "name": dataset_name}

    # Link catalog assets as data elements on the dataset.
    # CDGC limits propagation jobs to 20 per batch — chunk accordingly.
    if table_ids and dataset_id:
        import uuid as _uuid
        linked, errors = [], []
        url = f"{CDGC_API_BASE}/ccgf-contentv2/api/v1/publish"
        _LINK_BATCH = 20
        for chunk_start in range(0, len(table_ids), _LINK_BATCH):
            chunk = table_ids[chunk_start : chunk_start + _LINK_BATCH]
            headers = {**_cdgc_headers(), "x-infa-product-id": "CDGC", "correlation-id": str(_uuid.uuid4())}
            body = {"items": [
                {
                    "elementType":  "RELATIONSHIP",
                    "fromIdentity": dataset_id,
                    "toIdentity":   tid,
                    "operation":    "INSERT",
                    "type":         "com.infa.ccgf.models.governance.asscDataSetDataElement",
                    "identityType": "INTERNAL",
                    "attributes":   {},
                }
                for tid in chunk
            ]}
            r = httpx.post(url, headers=headers, json=body, timeout=30)
            if r.status_code in (200, 201, 207):
                try:
                    resp_items = r.json().get("items", [])
                    for idx, item in enumerate(resp_items):
                        tid = chunk[idx] if idx < len(chunk) else chunk[-1]
                        code = item.get("statusCode", 200)
                        msg  = item.get("messageCode", "")
                        nested_codes = {
                            r.get("messageCode", "")
                            for v in (item.get("validations") or [])
                            for r in (v.get("results") or [])
                        }
                        # INVALID_RELATIONSHIP_CLASS_TYPE = raw catalog column not yet curated;
                        # will link automatically after curate step — treat as skipped, not error.
                        _skip_codes = {"RELATIONSHIP_ALREADY_EXISTS", "INVALID_RELATIONSHIP_CLASS_TYPE"}
                        if code in (200, 201) or _skip_codes & (set([msg]) | nested_codes):
                            linked.append(tid)
                        else:
                            errors.append({"table_id": tid, "error": str(item)})
                    if not resp_items:
                        linked.extend(chunk)
                except Exception:
                    linked.extend(chunk)
            else:
                errors.append({"chunk_start": chunk_start, "error": f"HTTP {r.status_code}: {r.text[:300]}"})
        results["data_elements"] = {"linked": linked, "errors": errors}

    return results


# ---------------------------------------------------------------------------
# Tool 5: curate_assets_with_glossary
# ---------------------------------------------------------------------------
@mcp.tool()
def curate_assets_with_glossary(
    table_metadata: list[dict[str, Any]],
    business_terms: list[dict[str, Any]] | None = None,
    domain_name: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Use Claude AI to match technical columns to business terms, then link them in CDGC.

    Args:
      table_metadata:  Output from scan_mcc_source (list of tables with column ids).
      business_terms:  Optional list of {name, id} dicts to match against. When
                       omitted, terms are searched from CDGC by domain_name or all.
      domain_name:     Filter business terms to this domain when business_terms omitted.
      dry_run:         Return matches without writing links to CDGC.

    Returns: {matches:[{table, column, column_id, term_name, term_id, confidence}],
              linked_count, skipped_count, errors:[...]}
    """
    # Build term list if not provided
    if not business_terms:
        hint = domain_name or "business"
        hits = _cdgc_search(hint, class_type="BusinessTerm", size=50)
        business_terms = [{"name": _name_of(h), "id": _id_of(h)} for h in hits if _name_of(h)]

    if not business_terms:
        return {"error": "No business terms found. Create terms first with create_domain_structure."}

    # Build column inventory from table_metadata
    columns_list = []
    for t in table_metadata:
        for col in t.get("columns", []):
            columns_list.append({
                "table":        t["name"],
                "column":       col["name"],
                "column_id":    col.get("internal_id", ""),
                "data_type":    col.get("data_type", ""),
            })

    if not columns_list:
        return {"error": "No columns found in table_metadata. Run scan_mcc_source first."}

    # LLM matching — batch columns to avoid max_tokens truncation
    term_names = [t["name"] for t in business_terms]
    system_prompt = """You are a data cataloging specialist.
Match each technical column to the most appropriate business term from the provided list.
Only match if there is a clear semantic relationship (confidence >= 0.6).
Return JSON array:
[{"table":"...","column":"...","term_name":"...","confidence":0.0}]
Include ONLY columns you can match with confidence >= 0.6. Skip unmatched columns. No rationale."""

    matches_raw: list[dict[str, Any]] = []
    batch_size = 60
    for i in range(0, len(columns_list), batch_size):
        batch = columns_list[i: i + batch_size]
        user_msg = (
            f"Available business terms:\n{json.dumps(term_names)}\n\n"
            f"Columns to match:\n{json.dumps([{'table': c['table'], 'column': c['column'], 'type': c['data_type']} for c in batch])}"
        )
        batch_result = _llm_json(system_prompt, user_msg, model=_MODEL_QUALITY)
        if isinstance(batch_result, dict) and "matches" in batch_result:
            batch_result = batch_result["matches"]
        if isinstance(batch_result, list):
            matches_raw.extend(batch_result)

    # Build term lookup by name
    term_lookup = {t["name"].lower(): t for t in business_terms}
    # Build column lookup by table+column
    col_lookup = {(c["table"].upper(), c["column"].upper()): c for c in columns_list}

    matches: list[dict[str, Any]] = []
    errors:  list[dict[str, Any]] = []
    linked  = 0
    skipped = 0

    for m in matches_raw:
        tname  = m.get("term_name", "")
        table  = m.get("table", "").upper()
        col    = m.get("column", "").upper()
        conf   = float(m.get("confidence", 0.0))

        term_entry = term_lookup.get(tname.lower())
        col_entry  = col_lookup.get((table, col))

        if not term_entry or not col_entry:
            skipped += 1
            continue

        term_id   = term_entry["id"]
        col_id    = col_entry["column_id"]
        match_rec = {
            "table":      m.get("table"),
            "column":     m.get("column"),
            "column_id":  col_id,
            "term_name":  tname,
            "term_id":    term_id,
            "confidence": conf,
            "rationale":  m.get("rationale", ""),
        }

        if dry_run or not col_id or not term_id:
            skipped += 1
            match_rec["link_status"] = "dry_run" if dry_run else "skipped_no_id"
        else:
            try:
                _cdgc_link_term_to_asset(term_id, col_id)
                match_rec["link_status"] = "linked"
                linked += 1
            except Exception as e:
                match_rec["link_status"] = f"error: {e}"
                errors.append({"term": tname, "column": f"{table}.{col}", "error": str(e)})
                skipped += 1

        matches.append(match_rec)

    return {
        "dry_run":      dry_run,
        "match_count":  len(matches),
        "linked_count": linked,
        "skipped_count": skipped,
        "errors":       errors,
        "matches":      matches,
    }


# ---------------------------------------------------------------------------
# Tool 5b: curate_batch  (process one batch of column-to-term links, ~20s)
# ---------------------------------------------------------------------------
@mcp.tool()
def curate_batch(
    batch_index: int,
    batch_size: int = 40,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Process one batch of column-to-business-term links. (~20 seconds per batch)

    Call this sequentially for each batch after govern("link columns to business terms")
    returns the batch plan. Pass batch_index=0, 1, 2, ... until done==true.

    Args:
      batch_index: Zero-based batch number.
      batch_size:  Columns per batch (default 40). Must match across calls.
      dry_run:     Match without writing links to CDGC.

    Returns: {batch_index, columns_processed, linked, skipped, total_linked_so_far,
              progress, batches_remaining, done}
    """
    state  = _load_govern_state()
    tables = (state.get("scan") or {}).get("tables", [])
    if not tables:
        pending = state.get("scan_pending") or {}
        if pending:
            reconstructed = _reconstruct_scan_from_cache(pending)
            if reconstructed:
                tables = reconstructed["tables"]
                state["scan"] = reconstructed
                _save_govern_state(state)
                log.info("curate_batch: reconstructed scan state from cache (%d tables)", len(tables))
    if not tables:
        return {"error": "No scan result in state. Run scan first."}

    domain_result = state.get("domain_structure")
    if not domain_result:
        return {"error": "No domain structure in state. Run domain_structure step first."}

    # Gather business terms from domain_structure result
    bt_list: list[dict[str, Any]] = [
        {"name": item["name"], "id": item["id"]}
        for item in (domain_result.get("created", []) + domain_result.get("skipped", []))
        if item.get("type") == "BusinessTerm" and item.get("id") and item["id"] != "(dry_run)"
    ]
    if not bt_list:
        hint = state.get("domain_hint") or "business"
        hits = _cdgc_search(hint, class_type="BusinessTerm", size=50)
        bt_list = [{"name": _name_of(h), "id": _id_of(h)} for h in hits if _name_of(h)]

    # Build flat column list
    all_columns: list[dict[str, Any]] = []
    for t in tables:
        for col in t.get("columns", []):
            all_columns.append({
                "table":     t["name"],
                "column":    col["name"],
                "column_id": col.get("internal_id", ""),
                "data_type": col.get("data_type", ""),
            })

    total      = len(all_columns)
    start      = batch_index * batch_size
    end        = min(start + batch_size, total)
    batch_count = (total + batch_size - 1) // batch_size

    if start >= total:
        return {
            "batch_index": batch_index,
            "status":      "complete",
            "message":     f"All {total} columns have been processed.",
            "done":        True,
        }

    batch      = all_columns[start:end]
    term_names = [t["name"] for t in bt_list]

    system_prompt = (
        "You are a data cataloging specialist.\n"
        "Match each technical column to the most appropriate business term from the provided list.\n"
        "Only match if there is a clear semantic relationship (confidence >= 0.6).\n"
        "Return JSON array:\n"
        '[{"table":"...","column":"...","term_name":"...","confidence":0.0}]\n'
        "Include ONLY columns you can match with confidence >= 0.6. Skip unmatched columns."
    )
    user_msg = (
        f"Available business terms:\n{json.dumps(term_names)}\n\n"
        f"Columns to match:\n"
        f"{json.dumps([{'table': c['table'], 'column': c['column'], 'type': c['data_type']} for c in batch])}"
    )

    matches_raw = _llm_json(system_prompt, user_msg, model=_MODEL_QUALITY)
    if isinstance(matches_raw, dict) and "matches" in matches_raw:
        matches_raw = matches_raw["matches"]
    if not isinstance(matches_raw, list):
        matches_raw = []

    term_lookup = {t["name"].lower(): t for t in bt_list}
    col_lookup  = {(c["table"].upper(), c["column"].upper()): c for c in batch}

    linked  = 0
    skipped = 0
    errors: list[dict[str, Any]] = []

    matched_cols: set[tuple[str, str]] = set()

    for m in matches_raw:
        tname      = m.get("term_name", "")
        table_up   = m.get("table", "").upper()
        col_up     = m.get("column", "").upper()
        term_entry = term_lookup.get(tname.lower())
        col_entry  = col_lookup.get((table_up, col_up))

        if not term_entry or not col_entry:
            skipped += 1
            continue

        matched_cols.add((table_up, col_up))
        term_id = term_entry["id"]
        col_id  = col_entry["column_id"]

        if dry_run or not col_id or not term_id:
            skipped += 1
        else:
            try:
                _cdgc_link_term_to_asset(term_id, col_id)
                linked += 1
            except Exception as e:
                errors.append({"term": tname, "column": f"{table_up}.{col_up}", "error": str(e)})
                skipped += 1

    # Columns the LLM returned no match for at all
    skipped += sum(
        1 for c in batch
        if (c["table"].upper(), c["column"].upper()) not in matched_cols
    )

    # Accumulate progress in state
    prog = state.get("curate_progress") or {"linked": 0, "skipped": 0, "batches_done": []}
    prog["linked"]      += linked
    prog["skipped"]     += skipped
    if batch_index not in prog["batches_done"]:
        prog["batches_done"].append(batch_index)
    state["curate_progress"] = prog

    all_done = len(prog["batches_done"]) >= batch_count
    if all_done:
        state["curate"] = {
            "linked_count":  prog["linked"],
            "skipped_count": prog["skipped"],
            "total_columns": total,
        }

    _save_govern_state(state)

    return {
        "batch_index":          batch_index,
        "columns_processed":    f"{start + 1}–{end} of {total}",
        "linked":               linked,
        "skipped":              skipped,
        "errors":               errors,
        "total_linked_so_far":  prog["linked"],
        "total_skipped_so_far": prog["skipped"],
        "progress":             f"{end}/{total} columns ({round(end / total * 100)}%)",
        "batches_remaining":    batch_count - len(prog["batches_done"]),
        "done":                 all_done,
    }


# ---------------------------------------------------------------------------
# Tool 6: run_mcc_scan
# ---------------------------------------------------------------------------
@mcp.tool()
def run_mcc_scan(
    catalog_source_name: str | None = None,
    catalog_source_id:   str | None = None,
    system_name:         str | None = None,
    capabilities:        list[str] | None = None,
    wait_seconds: int = 0,
) -> dict[str, Any]:
    """Trigger a Metadata Command Center (MCC) catalog scan job.

    Endpoint: POST {CDGC_API_BASE}/data360/executable/v1/catalogsource/{CS-ID}
    Auth: Bearer JWT + IDS-SESSION-ID (both required).

    Supported capabilities (pass any subset):
      "Metadata Extraction", "Data Profiling", "Data Classification",
      "Data Quality", "Relationship Discovery", "Glossary Association"

    Default: ["Data Quality"] — runs CDQ rule specs linked to DQROs against live
    Snowflake data (5000 rows per catalog config) and publishes scores automatically.

    ID resolution order:
      1. catalog_source_id — use directly (the core.externalId UUID before ://)
      2. system_name — search CDGC "catalog source related to (system with name '...')"
      3. catalog_source_name — fuzzy-match against listed catalog sources

    Args:
      catalog_source_name: e.g. "CDGC-SNOWFLAKE-TERDEV"
      catalog_source_id:   Catalog source UUID, e.g. "c46c0515-d520-37a1-a76c-325cb5cfe6ae"
      system_name:         CDGC system name, e.g. "FUSION_ERP_DEV"
      capabilities:        List of capability names (default: ["Data Quality"])
      wait_seconds:        Poll for completion if > 0

    Returns: {catalog_source, job_id, status, task_groups, tracking_uri}
    """
    trigger_id   = catalog_source_id
    source_label = catalog_source_id or catalog_source_name or system_name or "unknown"

    if not trigger_id and system_name:
        trigger_id = _get_catalog_source_uuid(system_name)
        if trigger_id:
            log.info("run_mcc_scan: resolved '%s' → %s", system_name, trigger_id)

    if not trigger_id and catalog_source_name:
        sources = _list_catalog_sources()
        name_lc = catalog_source_name.lower()
        for s in sources:
            if name_lc in (s.get("name") or "").lower():
                trigger_id   = s.get("externalId") or s.get("id") or ""
                source_label = s.get("name") or trigger_id
                break

    if not trigger_id:
        sources = _list_catalog_sources()
        mcc_sources = _list_mcc_catalog_sources()
        return {
            "error": "Could not resolve catalog source.",
            "available_sources": [
                {"externalId": s.get("externalId"), "id": s.get("id"), "name": s.get("name")}
                for s in sources[:20]
            ],
            "mcc_registered_sources": [
                {"id": s.get("id") or s.get("sourceId"), "name": s.get("name") or s.get("sourceName")}
                for s in mcc_sources[:20]
            ],
        }

    # Resolve the actual MCC execution ID (may differ from CDGC metadata UUID)
    trigger_id = _resolve_mcc_source_id(trigger_id, source_label)
    log.info("run_mcc_scan: final trigger_id=%s", trigger_id)

    try:
        job_resp = _trigger_mcc_scan(trigger_id, capabilities)
    except RuntimeError as e:
        return {"error": str(e), "catalog_source_id": trigger_id}

    job_id = job_resp.get("jobId") or job_resp.get("id") or ""
    result: dict[str, Any] = {
        "catalog_source": {"id": trigger_id, "name": source_label},
        "job_id":         job_id,
        "status":         job_resp.get("status", "SUBMITTED"),
        "task_groups":    job_resp.get("taskGroups", []),
        "tracking_uri":   job_resp.get("trackingURI", ""),
    }

    if wait_seconds > 0 and job_id:
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            time.sleep(15)
            status_data = _get_mcc_scan_status(job_id)
            status = status_data.get("status") or status_data.get("state") or "UNKNOWN"
            result["status"] = status
            result["job_detail"] = status_data
            if status.upper() in ("COMPLETED", "SUCCESS", "FAILED", "ERROR", "CANCELLED"):
                break

    return result


# ---------------------------------------------------------------------------
# Tool 6b: set_dq_occurrences — save DQRO internal IDs to govern state
# ---------------------------------------------------------------------------
@mcp.tool()
def set_dq_occurrences(
    occurrences: list[dict[str, Any]],
) -> dict[str, Any]:
    """Save DQ rule occurrence IDs to govern state for use in propagate_scores step.

    Call this immediately after create_generic_dq_rules returns, passing its
    'occurrences_registered' list. Stores the internal_id of each occurrence so
    govern('propagate DQ scores') can push scores without needing to re-search CDGC.

    Args:
      occurrences: The 'occurrences_registered' list from create_generic_dq_rules output.
                   Each entry must have 'internal_id', 'column', and 'dimension' keys.

    Returns: {saved_count, table}
    """
    state = _load_govern_state()
    dq    = state.get("dq_rules") or {}
    stored = [
        {
            "internal_id": o.get("internal_id") or o.get("occurrence_id", ""),
            "name":        o.get("name", ""),
            "column":      o.get("column", ""),
            "dimension":   o.get("dimension", ""),
        }
        for o in (occurrences or [])
        if o.get("internal_id") or o.get("occurrence_id")
    ]
    dq["occurrences"]     = stored
    dq["occurrence_count"] = len(stored)
    state["dq_rules"]     = dq
    _save_govern_state(state)
    return {"saved_count": len(stored), "table": dq.get("table", "")}


# ---------------------------------------------------------------------------
# Tool 7: propagate_dq_score
# ---------------------------------------------------------------------------
@mcp.tool()
def propagate_dq_score(
    asset_name: str,
    score: float,
    rule_occurrence_id: str | None = None,
    run_date: str | None = None,
    dimension: str = "Accuracy",
    passed_rows: int | None = None,
    failed_rows: int | None = None,
    total_rows: int | None = None,
) -> dict[str, Any]:
    """Push a DQ score to a CDGC asset (DQRO or column).

    Finds the asset by name in CDGC if no rule_occurrence_id is provided,
    then PATCHes the quality score via the ruleautomation API.

    SCORE COMPUTATION (always done by the calling LLM, not by code):
      Derive score from the profile stats returned by compute_profile_from_snowflake
      that are still in context. For a COMPLETENESS/null check:
        score = round((1 - null_pct) * 100, 1)   # null_pct is 0-1
        total_rows  = profile_results["total_rows"]
        failed_rows = null_count
      For a VALIDITY check use the column's actual invalid-value count as failed_rows
      and compute score accordingly. Never use the placeholder value 95 when profile
      stats are available in context.

    Args:
      asset_name:          Name of the asset to update (table, column, or DQRO).
      score:               DQ score 0-100. Compute from profile null_pct (see above).
      rule_occurrence_id:  Internal UUID of the DQRO (preferred). Auto-resolved
                           from asset_name if omitted.
      run_date:            ISO date string (e.g. "2025-05-26"). Defaults to today.
      dimension:           DQ dimension (Accuracy, Completeness, etc).
      passed_rows:         Optional passed row count.
      failed_rows:         Optional failed row count (= null_count for null checks).
      total_rows:          Optional total row count (= profile total_rows).

    Returns: {http_status, asset_id, score, response}
    """
    import datetime

    if not rule_occurrence_id:
        hits = _cdgc_search(asset_name, size=20)
        occurrence_ids: list[str] = []
        rule_spec_id:   str       = ""
        for h in hits:
            ctype = (h.get("systemAttributes") or {}).get("core.classType") or ""
            hid   = _id_of(h)
            if not hid:
                continue
            if "RuleInstance" in ctype or "Occurrence" in ctype or "DQRO" in ctype.upper():
                occurrence_ids.append(hid)
            elif "RuleSpecification" in ctype or "RuleSpec" in ctype:
                rule_spec_id = hid

        # If we found a rule spec, fetch its linked occurrences via the relationships API
        if rule_spec_id and not occurrence_ids:
            rel_url = (
                f"{CDGC_API_BASE}/ccgf-contentv2/api/v1/search"
                f"?q=*&classType=com.infa.ccgf.models.governance.DataQualityRuleOccurrence"
                f"&size=200"
            )
            r2 = _request_cdgc("GET", rel_url)
            if r2.status_code == 200:
                for item in (r2.json().get("hits") or {}).get("hits", []):
                    src = item.get("_source", {})
                    linked_spec = (src.get("systemAttributes") or {}).get(
                        "com.infa.ccgf.models.governance.asscRuleSpecificationRuleOccurrence", ""
                    )
                    if linked_spec == rule_spec_id:
                        occurrence_ids.append(src.get("id") or item.get("_id") or "")

        # Bulk push: if multiple occurrences found, push to all and aggregate
        if len(occurrence_ids) > 1:
            from datetime import datetime as _dt2, timezone as _tz2
            _scanned = (
                run_date + "T00:00:00Z" if run_date
                else _dt2.now(_tz2.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            )
            env       = _read_env()
            cdgc_base = env.get("CDGC_API_BASE", CDGC_API_BASE)
            pushed, errors = [], []
            for oid in occurrence_ids:
                _facts: dict[str, Any] = {
                    "com.infa.ccgf.models.governance.value":       score,
                    "com.infa.ccgf.models.governance.scannedTime": _scanned,
                }
                if total_rows  is not None: _facts["com.infa.ccgf.models.governance.totalCount"] = total_rows
                if failed_rows is not None: _facts["com.infa.ccgf.models.governance.exception"]  = failed_rows
                _payload = {"scores": [{"assetId": oid, "dqscore": {"facts": _facts}}]}
                _url = f"{cdgc_base}/ccgf-ruleautomation/api/v1/dataQuality/publishScore?refBy=INTERNAL"
                _r   = _request_cdgc("PATCH", _url, json=_payload)
                if _r.status_code in (200, 201, 204):
                    pushed.append(oid)
                else:
                    errors.append({"id": oid, "status": _r.status_code, "body": _r.text[:200]})
            return {
                "asset_name":         asset_name,
                "occurrences_pushed": len(pushed),
                "occurrences_failed": len(errors),
                "score":              score,
                "errors":             errors,
            }

        if occurrence_ids:
            rule_occurrence_id = occurrence_ids[0]
        elif hits:
            rule_occurrence_id = _id_of(hits[0])
        if not rule_occurrence_id:
            return {"error": f"Could not resolve asset '{asset_name}' to a DQRO or column."}

    from datetime import datetime as _dt, timezone as _tz
    scanned_time = (
        run_date + "T00:00:00Z" if run_date
        else _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    env       = _read_env()
    cdgc_base = env.get("CDGC_API_BASE", CDGC_API_BASE)

    _total   = total_rows  if total_rows  is not None else 100
    _failed  = failed_rows if failed_rows is not None else int(round(_total * (1 - score / 100)))
    if passed_rows is not None and total_rows is not None:
        _failed = total_rows - passed_rows
    facts: dict[str, Any] = {
        "com.infa.ccgf.models.governance.value":       score,
        "com.infa.ccgf.models.governance.scannedTime": scanned_time,
        "com.infa.ccgf.models.governance.totalCount":  _total,
        "com.infa.ccgf.models.governance.exception":   _failed,
    }

    payload = {"scores": [{"assetId": rule_occurrence_id, "dqscore": {"facts": facts}}]}

    url = f"{cdgc_base}/ccgf-ruleautomation/api/v1/dataQuality/publishScore?refBy=INTERNAL"
    r   = _request_cdgc("PATCH", url, json=payload)

    return {
        "http_status": r.status_code,
        "asset_id":    rule_occurrence_id,
        "asset_name":  asset_name,
        "score":       score,
        "response":    r.json() if r.text else {},
    }


# ---------------------------------------------------------------------------
# Tool 8: onboard_and_govern — master orchestrator
# ---------------------------------------------------------------------------
@mcp.tool()
def onboard_and_govern(
    table_names: list[str],
    domain_hint: str | None = None,
    organization_context: str | None = None,
    system_name: str | None = None,
    dataset_name: str | None = None,
    schema_hint: str | None = None,
    dry_run: bool = False,
    skip_steps: list[str] | None = None,
) -> dict[str, Any]:
    """End-to-end LLM-powered data governance onboarding.

    Chains all 7 tools in sequence. Each step records SUCCESS / SKIPPED / FAILED
    and the pipeline continues even when a step fails.

    Steps:
      1. scan_mcc_source          — discover table+column metadata from CDGC
      2. generate_governance_taxonomy — LLM → domain/subdomain/BT tree
      3. create_domain_structure  — POST domain hierarchy + BTs to CDGC
      4. create_system_and_dataset— POST System + Dataset in CDGC
      5. curate_assets_with_glossary — LLM matches columns → BTs + link in CDGC

    For rule spec + DQRO creation (steps 6-7), use governance_engine_mcp.py tools:
      6. create_dq_rules          — CLAIRE rule spec creation (run separately)
      7. register_in_cdgc         — DQRO registration (run separately)

    Args:
      table_names:           Tables to onboard.
      domain_hint:           Top-level domain name.
      organization_context:  Business context for LLM.
      system_name:           Source system display name (default: inferred from tables).
      dataset_name:          Logical dataset name (default: inferred from domain_hint).
      schema_hint:           Schema prefix for CDGC search disambiguation.
      dry_run:               Show what would be created without writing to CDGC.
      skip_steps:            List of step names to skip (e.g. ["curate", "system"]).

    Returns: {pipeline_steps:[{step, status, result}], summary}
    """
    skip = set(skip_steps or [])
    steps: list[dict[str, Any]] = []

    def _record(step_name: str, fn, *args, **kwargs) -> Any:
        if step_name in skip:
            steps.append({"step": step_name, "status": "SKIPPED", "reason": "in skip_steps"})
            return None
        try:
            result = fn(*args, **kwargs)
            steps.append({"step": step_name, "status": "SUCCESS", "result": result})
            return result
        except Exception as e:
            log.exception("onboard_and_govern step %s failed", step_name)
            steps.append({"step": step_name, "status": "FAILED", "error": str(e)})
            return None

    # Step 1 — discover metadata
    table_meta = _record("scan", scan_mcc_source, table_names, schema_hint=schema_hint)
    tables = (table_meta or {}).get("tables", [])
    if not tables:
        # Use stub metadata so downstream steps can still run
        tables = [{"name": t, "columns": []} for t in table_names]

    # Step 2 — LLM taxonomy
    taxonomy = _record(
        "taxonomy",
        generate_governance_taxonomy,
        tables,
        domain_hint=domain_hint,
        organization_context=organization_context,
    )

    # Step 3 — create domain structure
    domain_result = _record(
        "domain_structure",
        create_domain_structure,
        taxonomy or {"domains": []},
        dry_run=dry_run,
    )

    # Extract BusinessTerm {name, id} from step 3 result to avoid CDGC index lag in step 5.
    # Both "created" and "skipped" (already-existing) terms are included.
    bt_from_step3: list[dict[str, Any]] | None = None
    if domain_result and not dry_run:
        bt_from_step3 = [
            {"name": item["name"], "id": item["id"]}
            for item in (domain_result.get("created", []) + domain_result.get("skipped", []))
            if item.get("type") == "BusinessTerm"
            and item.get("id")
            and item["id"] != "(dry_run)"
        ] or None

    # Step 4 — system + dataset
    inferred_system  = system_name  or (table_names[0].split("_")[0] + " System" if table_names else "Source System")
    inferred_dataset = dataset_name or (domain_hint + " Dataset" if domain_hint else table_names[0] + " Dataset")
    _record(
        "system_dataset",
        create_system_and_dataset,
        inferred_system,
        inferred_dataset,
        description=organization_context or "",
        domain_name=domain_hint,
        dry_run=dry_run,
    )

    # Step 5 — curate assets with glossary
    # Pass terms directly from step 3 to avoid CDGC search-index lag.
    _record(
        "curate",
        curate_assets_with_glossary,
        tables,
        business_terms=bt_from_step3,
        domain_name=domain_hint,
        dry_run=dry_run,
    )

    successes = sum(1 for s in steps if s["status"] == "SUCCESS")
    failures  = sum(1 for s in steps if s["status"] == "FAILED")

    return {
        "pipeline_steps": steps,
        "summary": {
            "table_count":    len(table_names),
            "steps_run":      len(steps),
            "succeeded":      successes,
            "failed":         failures,
            "skipped":        len(steps) - successes - failures,
            "dry_run":        dry_run,
            "next_steps": [
                "Run create_dq_rules in governance_engine_mcp for rule spec creation",
                "Run register_in_cdgc in governance_engine_mcp for DQRO creation",
                "Run propagate_dq_score after DQ jobs complete",
            ],
        },
    }


# ---------------------------------------------------------------------------
# Govern step-state cache helpers
# ---------------------------------------------------------------------------
_GOVERN_STATE_FILE = SCAN_CACHE_DIR / "govern_state.json"

# Keys that are global to the CDGC catalog, not scoped to a single table. Everything
# else in the govern state (scan_pending, scan, taxonomy, dq_rules, occurrences, …) is
# per-table and lives under container["tables"][<key>].
_GOVERN_SHARED_KEYS = ("catalog",)


def _table_key(table_names: Any) -> str:
    """Stable per-table state key derived from the scanned table name(s).

    Returns "" when no table is in context yet (e.g. before the first scan), which
    maps to a default slot that only ever holds shared keys.
    """
    if not table_names:
        return ""
    if isinstance(table_names, str):
        table_names = [table_names]
    return "|".join(sorted(str(n).lower() for n in table_names if n))


def _load_container() -> dict[str, Any]:
    """Load the raw sharded container, migrating the legacy flat format if needed."""
    if not _GOVERN_STATE_FILE.exists():
        return {"tables": {}, "_active": ""}
    try:
        data = json.loads(_GOVERN_STATE_FILE.read_text())
    except Exception:
        return {"tables": {}, "_active": ""}
    if isinstance(data, dict) and "tables" in data and "_active" in data:
        return data
    # Legacy flat state {catalog, scan_pending, taxonomy, dq_rules, …} → wrap into a slot.
    data = data if isinstance(data, dict) else {}
    shared = {k: data.pop(k) for k in _GOVERN_SHARED_KEYS if k in data}
    key = _table_key(data.get("table_names"))
    container: dict[str, Any] = {"tables": {key: data} if data else {}, "_active": key}
    container.update(shared)
    return container


def _write_container(container: dict[str, Any]) -> None:
    SCAN_CACHE_DIR.mkdir(exist_ok=True)
    _GOVERN_STATE_FILE.write_text(json.dumps(container, indent=2))


def _load_govern_state(key: str | None = None) -> dict[str, Any]:
    """Return the flat state for one table (the active table unless `key` is given),
    merged with shared catalog keys. Callers see the same flat dict as before."""
    container = _load_container()
    active = key if key is not None else container.get("_active", "")
    merged: dict[str, Any] = dict(container.get("tables", {}).get(active, {}))
    for k in _GOVERN_SHARED_KEYS:
        if k in container:
            merged[k] = container[k]
    return merged


def _save_govern_state(state: dict[str, Any]) -> None:
    """Persist a flat state dict into its table's slot. The slot key is derived from
    state['table_names']; shared keys are split back out to the container root so a
    write for one table never clobbers another table's pipeline progress."""
    container = _load_container()
    for k in _GOVERN_SHARED_KEYS:
        if k in state:
            container[k] = state[k]
    per_table = {k: v for k, v in state.items() if k not in _GOVERN_SHARED_KEYS}
    key = _table_key(per_table.get("table_names")) or container.get("_active", "")
    container.setdefault("tables", {})[key] = per_table
    container["_active"] = key
    _write_container(container)


# ---------------------------------------------------------------------------
# NLP step dispatcher: govern
# ---------------------------------------------------------------------------

_PIPELINE_STEPS = [
    {
        "name":        "list_catalog",
        "description": "Discover all schemas and tables available in the CDGC catalog. "
                       "Use this first when you don't know which tables exist.",
        "next":        "scan",
    },
    {
        "name":        "scan",
        "description": "Scan metadata (columns, data types) for one or more specific tables. "
                       "Requires table names. Results are cached to disk.",
        "requires":    ["table_names"],
        "next":        "taxonomy",
    },
    {
        "name":        "taxonomy",
        "description": "Use LLM to generate a governance taxonomy (domain, subdomains, "
                       "business terms) from scanned table metadata.",
        "requires":    ["scan_result"],
        "next":        "domain_structure",
    },
    {
        "name":        "domain_structure",
        "description": "Create the domain hierarchy and business terms in CDGC.",
        "requires":    ["taxonomy_result"],
        "next":        "system_dataset",
    },
    {
        "name":        "system_dataset",
        "description": "Register the source system and logical dataset asset in CDGC.",
        "requires":    ["taxonomy_result"],
        "next":        "curate",
    },
    {
        "name":        "curate",
        "description": "Link each column in the scanned tables to the matching business term in CDGC.",
        "requires":    ["scan_result", "domain_structure_result"],
        "next":        "dq_rules",
    },
    {
        "name":        "dq_rules",
        "description": "Create diversified DQ rules and register occurrences in CDGC. "
                       "Dimensions are auto-selected per column from scan data types: "
                       "COMPLETENESS always; UNIQUENESS for ID/key columns; VALIDITY for VARCHAR/BOOLEAN; "
                       "TIMELINESS for TIMESTAMP/DATE. No parameters needed — reads from scan state.",
        "requires":    ["scan_result"],
        "next":        "propagate_scores",
    },
    {
        "name":        "propagate_scores",
        "description": "Push interim DQ scores (95% placeholder) to CDGC for every registered "
                       "rule occurrence so scores are visible immediately. "
                       "Call this after DQ rules are created. Returns one upload_dq_scores "
                       "action per registered occurrence. No parameters needed — reads from state.",
        "requires":    ["dq_rules_result"],
        "next":        "mcc_scan",
    },
    {
        "name":        "mcc_scan",
        "description": "Trigger the MCC catalog scan with Data Quality capability. "
                       "Runs CDQ rule specs against live Snowflake data (5000 rows) and "
                       "overwrites the interim scores with real values in CDGC automatically. "
                       "Catalog source is resolved dynamically from CDGC at runtime.",
        "requires":    ["propagate_scores_result"],
        "next":        "create_cdmp_category",
    },
    {
        "name":        "create_cdmp_category",
        "description": "Create (or reuse) a category in Informatica Data Marketplace that will "
                       "group the published data collection. Auto-derives the category name from "
                       "the governance domain created in the taxonomy step. No parameters needed.",
        "requires":    ["mcc_scan_result"],
        "next":        "create_cdmp_data_asset",
    },
    {
        "name":        "create_cdmp_data_asset",
        "description": "Create a Data Asset in Informatica Data Marketplace representing the "
                       "governed table. Reads column names, business terms, and DQ score from "
                       "pipeline state. No parameters needed.",
        "requires":    ["create_cdmp_category_result"],
        "next":        "create_cdmp_collection",
    },
    {
        "name":        "create_cdmp_collection",
        "description": "Create a Data Collection in Informatica Data Marketplace that bundles "
                       "the Data Asset into a publishable product for data consumers. "
                       "No parameters needed.",
        "requires":    ["create_cdmp_data_asset_result"],
        "next":        "publish_marketplace",
    },
    {
        "name":        "publish_marketplace",
        "description": "Publish the Data Collection to the Informatica Data Marketplace so "
                       "data consumers can discover and request access to the governed dataset. "
                       "No parameters needed.",
        "requires":    ["create_cdmp_collection_result"],
        "next":        "create_delivery_template",
    },
    {
        "name":        "create_delivery_template",
        "description": "Create a Delivery Template in Data Marketplace that defines how consumers "
                       "receive the data (e.g. file download, API access). Attached to the published "
                       "collection so consumers can place orders. No parameters needed.",
        "requires":    ["marketplace_result"],
        "next":        "create_terms_of_use",
    },
    {
        "name":        "create_terms_of_use",
        "description": "Create and attach Terms of Use to the published Data Collection. Consumers "
                       "must accept these terms before their order is fulfilled. No parameters needed.",
        "requires":    ["delivery_template_result"],
        "next":        "create_delivery_target",
    },
    {
        "name":        "create_delivery_target",
        "description": "Create a Delivery Target and link it to the Data Collection. Defines the "
                       "destination where data lands once a consumer order is approved "
                       "(e.g. a Snowflake schema or S3 path). No parameters needed.",
        "requires":    ["terms_of_use_result"],
        "next":        "create_consumer_access",
    },
    {
        "name":        "create_consumer_access",
        "description": "Provision Consumer Access for the published Data Collection — simulates a "
                       "consumer placing an order and being granted access. Completes the full "
                       "producer → consumer loop. No parameters needed.",
        "requires":    ["delivery_target_result"],
        "next":        "done",
    },
]

_STEP_ORDER = [s["name"] for s in _PIPELINE_STEPS]

_ID_PATTERNS   = ("_ID", "ID_", "_KEY", "KEY_", "PK_", "_PK", "CODE", "_NO", "NO_", "NBR", "_NUM", "NUM_")
_DATE_PATTERNS = ("_DATE", "DATE_", "_DT", "DT_", "_TIME", "TIME_", "CREATED", "MODIFIED", "UPDATED")
_TYPE_PATTERNS = ("_TYPE", "TYPE_", "_STATUS", "STATUS_", "_NAME", "NAME_", "_DESC", "DESC_", "_CAT", "CAT_")


def _select_key_columns(
    columns: list[dict],
    max_cols: int = 7,
) -> list[dict]:
    """Return a curated subset of columns for DQ rule creation.

    Priority order (stops when max_cols reached):
      1. Exact ID/KEY/PK columns  → dimension: UNIQUENESS
      2. DATE/TIMESTAMP columns    → dimension: TIMELINESS
      3. STATUS/TYPE/NAME columns  → dimension: VALIDITY  (business-important descriptors)
      4. NUMBER columns            → dimension: VALIDITY
      5. Remaining VARCHAR columns (first N to fill quota)

    Skips columns with unknown data_type (CDGC won't accept them).
    """
    buckets: dict[int, list[dict]] = {1: [], 2: [], 3: [], 4: [], 5: []}
    for col in columns:
        dt = col.get("data_type", "unknown").lower()
        if dt == "unknown" or not col.get("column_id") or not col.get("column_name"):
            continue
        cn = col["column_name"].upper()
        dt_lower = dt

        if any(p in cn for p in _ID_PATTERNS):
            buckets[1].append(col)
        elif any(x in dt_lower for x in ("timestamp", "date", "time")) or any(p in cn for p in _DATE_PATTERNS):
            buckets[2].append(col)
        elif any(p in cn for p in _TYPE_PATTERNS):
            buckets[3].append(col)
        elif any(x in dt_lower for x in ("number", "numeric", "int", "decimal", "float", "double")):
            buckets[4].append(col)
        else:
            buckets[5].append(col)

    selected: list[dict] = []
    for priority in sorted(buckets):
        for col in buckets[priority]:
            if len(selected) >= max_cols:
                break
            selected.append(col)
        if len(selected) >= max_cols:
            break
    return selected


@mcp.tool()
def govern(
    request: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """NLP-driven single-step governance pipeline.

    Call this once per step. Describe what you want in plain English — the tool
    figures out which pipeline step to run, resolves parameters from your request
    and prior step state, runs that one step, and tells you what comes next.

    Pipeline order:
      1. list_catalog      — discover schemas + tables in CDGC; user picks a table
      2. scan              — find table IDs (fast); returns next_actions list for scan_fetch_columns
         → call scan_fetch_columns per table (~15s each, cached after first run)
         → then call govern("generate taxonomy") to continue
      3. taxonomy          — LLM generates domain/subdomain/business-term tree
      4. domain_structure  — create domain hierarchy + terms in CDGC
      5. system_dataset    — register source system + dataset + column links in CDGC
      6. curate            — returns batch plan; then call curate_batch(batch_index=0,1,...) per batch
      7. dq_rules          — returns params for create_generic_dq_rules (auto-selects dimensions per col)
      8. propagate_scores  — uploads interim 95% scores so DQROs show values immediately
      9. mcc_scan          — triggers MCC Data Quality scan; CDQ rules run on live data and overwrite interim scores

    Examples:
      govern("show me what schemas and tables are in the catalog")
      govern("scan the supplier site tables in the finance ERP schema")
        → then call scan_fetch_columns for each table in next_actions
      govern("generate a taxonomy for the scanned supplier data")
      govern("create the domain structure in CDGC")
      govern("link the columns to their business terms")
        → then call curate_batch(batch_index=0), curate_batch(batch_index=1), ...

    State from each step is cached to disk (.scan_cache/govern_state.json) so
    the next step can pick it up automatically.

    Args:
      request:  Plain-English description of what you want to do next.
      dry_run:  If True, show what would happen without writing to CDGC (for
                domain_structure, system_dataset, curate steps).

    Returns: {step, reasoning, result, next_step, next_step_hint}
    """
    state = _load_govern_state()

    # Summarise current state for the LLM so it knows what's already been done
    state_summary_parts: list[str] = []
    if state.get("catalog"):
        schemas = state["catalog"].get("schemas", [])
        names = [f"{s['schema']} ({len(s['tables'])} tables)" for s in schemas[:10]]
        state_summary_parts.append(f"list_catalog: done — {', '.join(names)}")
    # scan_pending (fully fetched) is always newer than state["scan"] — prefer it
    _sp = state.get("scan_pending") or {}
    _sp_tables = _sp.get("tables", [])
    _sp_fetched = bool(_sp_tables) and all(t.get("columns_fetched") for t in _sp_tables)
    if _sp_fetched:
        _scan_names = [t["name"] for t in _sp_tables]
        state_summary_parts.append(f"scan: done — tables {_scan_names}")
    elif state.get("scan"):
        tables = [t["name"] for t in state["scan"].get("tables", [])]
        state_summary_parts.append(f"scan: done — tables {tables}")
    if state.get("taxonomy"):
        doms = [d.get("name") for d in (state["taxonomy"].get("domains") or [])[:3]]
        state_summary_parts.append(f"taxonomy: done — domains {doms}")
    if state.get("domain_structure"):
        n = len((state["domain_structure"].get("created") or []))
        state_summary_parts.append(f"domain_structure: done — {n} assets created")
    if state.get("system_dataset"):
        state_summary_parts.append("system_dataset: done")
    if state.get("curate"):
        state_summary_parts.append("curate: done")
    if state.get("dq_rules"):
        n = state["dq_rules"].get("occurrence_count", 0)
        state_summary_parts.append(f"dq_rules: done — {n} occurrences registered")
    if state.get("propagate_scores"):
        dims = state["propagate_scores"].get("dimensions", [])
        state_summary_parts.append(f"propagate_scores: done — dimensions {dims}")
    if state.get("mcc_scan"):
        job_id = state["mcc_scan"].get("job_id", "")
        status = state["mcc_scan"].get("status", "")
        state_summary_parts.append(f"mcc_scan: done — job_id {job_id} status {status}")
    if state.get("cdmp_category"):
        state_summary_parts.append(f"create_cdmp_category: done — id {state['cdmp_category'].get('id')}")
    if state.get("cdmp_data_asset"):
        state_summary_parts.append(f"create_cdmp_data_asset: done — id {state['cdmp_data_asset'].get('id')}")
    if state.get("cdmp_collection"):
        state_summary_parts.append(f"create_cdmp_collection: done — id {state['cdmp_collection'].get('id')}")
    if state.get("marketplace"):
        state_summary_parts.append(f"publish_marketplace: done — published={state['marketplace'].get('published')}")

    if state.get("awaiting_table_selection"):
        state_summary_parts.append("AWAITING TABLE SELECTION — user must choose a table before scan proceeds")
    state_text = "\n".join(state_summary_parts) if state_summary_parts else "(no steps completed yet)"

    step_descriptions = "\n".join(
        f"  {s['name']}: {s['description']}" for s in _PIPELINE_STEPS
    )

    # Build catalog context if available
    catalog_text = "(catalog not yet fetched)"
    if state.get("catalog"):
        lines = []
        for s in state["catalog"].get("schemas", []):
            tnames = [t["name"] for t in s.get("tables", [])]
            prefix = f"{s.get('connection','')}/{s['schema']}" if s.get("connection") else s["schema"]
            lines.append(f"  {prefix}: {', '.join(tnames)}")
        catalog_text = "\n".join(lines) or catalog_text

    system_prompt = (
        "You are a data governance assistant controlling a step-by-step onboarding pipeline.\n\n"
        "Available pipeline steps:\n" + step_descriptions + "\n\n"
        "Given the user request and current pipeline state, decide which ONE step to run next "
        "and extract all parameters needed for it.\n\n"
        "Return JSON with exactly these keys:\n"
        "  step                (str)        — one of: list_catalog, scan, taxonomy, "
        "domain_structure, system_dataset, curate, dq_rules, propagate_scores, mcc_scan, "
        "create_cdmp_category, create_cdmp_data_asset, create_cdmp_collection, publish_marketplace\n"
        "  table_names         (list[str]|null) — for scan step: tables to scan\n"
        "  schema_hint         (str|null)   — schema for disambiguation\n"
        "  domain_hint         (str|null)   — top-level domain name\n"
        "  organization_context(str|null)   — business context sentence\n"
        "  system_name         (str|null)   — source system name (null to auto-derive from scan)\n"
        "  schema_filter       (str|null)   — for list_catalog: filter by schema substring\n"
        "  reasoning           (str)        — one sentence explaining your choice\n\n"
        "Rules:\n"
        "- Pick the step that best matches the user's request.\n"
        "- For 'scan', extract table names from the request or infer from catalog state.\n"
        "- For 'taxonomy'/'domain_structure'/'system_dataset'/'curate', reuse table/domain "
        "context from prior state if not specified in the request.\n"
        "- If catalog hasn't been fetched yet and the user asks about tables, choose list_catalog.\n"
        "- If state contains 'AWAITING TABLE SELECTION', choose 'scan' ONLY when the user's "
        "request explicitly names a table. Otherwise choose 'list_catalog' to re-display options."
    )

    user_msg = (
        f"User request: {request}\n\n"
        f"Current pipeline state:\n{state_text}\n\n"
        f"Available catalog:\n{catalog_text}"
    )

    log.info("govern: calling LLM to dispatch step for: %s", request[:120])
    resolved = _llm_json(system_prompt, user_msg)
    step      = resolved.get("step", "")
    reasoning = resolved.get("reasoning", "")

    if step not in _STEP_ORDER:
        return {
            "error":    f"LLM returned unknown step '{step}'. Valid steps: {_STEP_ORDER}",
            "resolved": resolved,
        }

    log.info("govern: dispatching step=%s", step)
    result: Any = None

    # ---- dispatch ----
    if step == "list_catalog":
        result = list_catalog_tables(
            schema_filter=resolved.get("schema_filter"),
            max_results=300,
        )
        state["catalog"] = result
        state["awaiting_table_selection"] = True
        # Build a structured table list for the user to choose from
        tables_for_selection = [
            {"name": t["name"], "schema": s["schema"], "connection": s.get("connection", "")}
            for s in result.get("schemas", [])
            for t in s.get("tables", [])
        ]
        result["tables_for_selection"] = tables_for_selection
        result["awaiting_table_selection"] = True
        result["next_step_hint"] = (
            f"Found {result.get('total_tables', 0)} table(s) above. "
            "Ask the user which table they want to govern, then call "
            "govern('scan <TABLE_NAME> from <SCHEMA_NAME>') with their choice."
        )

    elif step == "scan":
        table_names = resolved.get("table_names") or []
        if not table_names:
            # No table specified — surface the catalog so the user can pick
            catalog = state.get("catalog")
            if catalog:
                tables_for_selection = [
                    {"name": t["name"], "schema": s["schema"], "connection": s.get("connection", "")}
                    for s in catalog.get("schemas", [])
                    for t in s.get("tables", [])
                ]
                return {
                    "step":                    step,
                    "reasoning":               reasoning,
                    "awaiting_table_selection": True,
                    "tables_for_selection":    tables_for_selection,
                    "next_step_hint":          (
                        "Please tell me which table to scan. "
                        "Call govern('scan <TABLE_NAME> from <SCHEMA_NAME>') with your choice."
                    ),
                }
            return {
                "step":      step,
                "reasoning": reasoning,
                "error":     "No table names specified. Run list_catalog first to see available tables.",
            }
        # Phase 1 (fast, ~3s): find table IDs. Column fetch is delegated to scan_fetch_columns
        # so each table shows as its own visible tool call in Claude Code.
        find_result = scan_find_tables(
            table_names=table_names,
            schema_hint=resolved.get("schema_hint"),
        )
        state = _load_govern_state()   # reload — scan_find_tables already saved scan_pending
        state["schema_hint"] = resolved.get("schema_hint")
        # Clear awaiting_table_selection — user has now picked a table
        state.pop("awaiting_table_selection", None)
        # Only record tables the user explicitly asked to scan — not all catalog tables.
        # This scopes every downstream step (taxonomy, DQ rules, DQRO) to the selected table(s).
        found_names = [t["name"] for t in find_result.get("tables", [])]
        if found_names:
            state["table_names"] = found_names
            # If the user re-scanned a different table, clear stale downstream state so
            # Steps 3-13 are forced to re-run against the new table.
            old_tables = [(t.get("name") or "") for t in (state.get("scan") or {}).get("tables", [])]
            if set(found_names) != set(old_tables):
                for stale_key in ("scan", "taxonomy", "domain_hint", "domain_structure",
                                  "system_dataset", "curate_progress", "curate",
                                  "dq_rules", "propagate_scores", "mcc_scan",
                                  "cdmp_category", "cdmp_data_asset", "cdmp_collection"):
                    state.pop(stale_key, None)
                log.info("scan: table changed %s→%s, cleared downstream state", old_tables, found_names)
        _save_govern_state(state)
        result = {
            **find_result,
            "next_step_instruction": (
                f"Call scan_fetch_columns for each table above ({len(find_result.get('tables', []))} table(s)), "
                "then run govern('generate governance taxonomy') to continue."
            ),
        }

    elif step == "taxonomy":
        # Prefer scan_pending when it is fully fetched — it represents the user's most
        # recent table selection. state["scan"] may be stale from a prior run on a
        # different table, so checking it first would silently govern the wrong table.
        pending = state.get("scan_pending") or {}
        pending_tables = pending.get("tables", [])
        all_fetched = bool(pending_tables) and all(t.get("columns_fetched") for t in pending_tables)
        if all_fetched:
            reconstructed = _reconstruct_scan_from_cache(pending)
            if reconstructed:
                tables = reconstructed["tables"]
                state["scan"] = reconstructed
                _save_govern_state(state)
                log.info("taxonomy: using scan_pending tables (%d tables)", len(tables))
            else:
                tables = []
        else:
            tables = (state.get("scan") or {}).get("tables", [])
        if not tables and pending:
            # Fallback: scan_fetch_columns was used but columns_fetched flag may be missing
            reconstructed = _reconstruct_scan_from_cache(pending)
            if reconstructed:
                tables = reconstructed["tables"]
                state["scan"] = reconstructed
                _save_govern_state(state)
                log.info("taxonomy: reconstructed scan state from cache (%d tables)", len(tables))
        if not tables:
            return {
                "step":      step,
                "reasoning": reasoning,
                "error":     "No scan result in state. Run 'scan' then 'scan_fetch_columns' first.",
            }
        # Filter out columns that already have a business term linked in CDGC
        filtered_tables = []
        for tbl in tables:
            already_covered = _columns_with_existing_terms(tbl.get("internal_id", ""))
            uncovered_cols = [
                c for c in tbl.get("columns", [])
                if c.get("internal_id") not in already_covered
            ]
            if uncovered_cols:
                filtered_tables.append({**tbl, "columns": uncovered_cols})
            log.info(
                "taxonomy pre-filter: table=%s total=%d covered=%d uncovered=%d",
                tbl.get("name"), len(tbl.get("columns", [])),
                len(already_covered), len(uncovered_cols),
            )
        if not filtered_tables:
            result = {"domains": [], "skipped_reason": "all columns already have business terms linked"}
        else:
            result = generate_governance_taxonomy(
                filtered_tables,
                domain_hint=resolved.get("domain_hint") or state.get("domain_hint"),
                organization_context=resolved.get("organization_context") or state.get("organization_context"),
            )
        state["taxonomy"]             = result
        state["domain_hint"]          = resolved.get("domain_hint") or state.get("domain_hint")
        state["organization_context"] = resolved.get("organization_context") or state.get("organization_context")

    elif step == "domain_structure":
        taxonomy = state.get("taxonomy")
        if not taxonomy:
            return {
                "step":     step,
                "reasoning": reasoning,
                "error":    "No taxonomy in state. Run 'taxonomy' first.",
            }
        # Return preview for user multi-select approval.
        # Actual CDGC creation happens via approve_domain_structure().
        items = _flatten_taxonomy_for_approval(taxonomy)
        state["domain_structure_pending_items"] = items
        result = {
            "awaiting_approval": True,
            "items":             items,
            "total_items":       len(items),
            "domains":           [d["name"] for d in taxonomy.get("domains", [])],
            "next_step_instruction": (
                "Present these items to the user for multi-select review. "
                "Call approve_domain_structure(approved_names=[...]) with the approved names."
            ),
        }
        # state["domain_structure"] is intentionally NOT set here —
        # only approve_domain_structure() sets it, after the user approves.

    elif step == "system_dataset":
        table_names   = state.get("table_names", [])
        domain_hint   = resolved.get("domain_hint") or state.get("domain_hint")
        # Derive system name from the catalog connection embedded in the scan's external_id
        # Format: origin://CONNECTION/SCHEMA/TABLE~classType
        scan_tables   = (state.get("scan") or {}).get("tables", [])
        # Fallback: reconstruct from scan_pending cache (same pattern as curate/mcc_scan)
        if not scan_tables:
            pending = state.get("scan_pending") or {}
            if pending:
                reconstructed = _reconstruct_scan_from_cache(pending)
                if reconstructed:
                    scan_tables = reconstructed["tables"]
                    state["scan"] = reconstructed
                    _save_govern_state(state)
                    log.info("system_dataset: reconstructed scan state from cache (%d tables)", len(scan_tables))
        connection_name = ""
        schema_name     = ""
        if scan_tables:
            ext_id = scan_tables[0].get("external_id", "")
            after_origin = ext_id.split("://", 1)[-1] if "://" in ext_id else ""
            parts = [p for p in after_origin.split("~")[0].split("/") if p]
            connection_name = parts[0] if parts else ""
            schema_name     = parts[1] if len(parts) > 1 else ""
        # Prefer scan-derived connection name over LLM-resolved — the LLM often hallucinates
        # the table name as the system name when the request doesn't explicitly name the system.
        system_name   = connection_name or resolved.get("system_name") or "Source System"
        # Always derive dataset name from the scanned table — never from the LLM (it hallucinates).
        # One dataset per governed table; schema name is the fallback only when table_names is empty.
        dataset_name  = (table_names[0] if table_names else schema_name) or (domain_hint + " Dataset" if domain_hint else "Dataset")
        log.info("system_dataset: system_name=%s dataset_name=%s scan_tables=%d",
                 system_name, dataset_name, len(scan_tables))
        # asscDataSetDataElement requires column-level IDs — table IDs are rejected.
        scan_table_ids = [
            col["internal_id"]
            for t in scan_tables
            for col in t.get("columns", [])
            if col.get("internal_id")
        ]
        log.info("system_dataset: linking %d column IDs", len(scan_table_ids))
        result = create_system_and_dataset(
            system_name,
            dataset_name,
            description=resolved.get("organization_context") or state.get("organization_context") or "",
            domain_name=domain_hint,
            table_ids=scan_table_ids or None,
            dry_run=dry_run,
        )
        state["system_dataset"] = result

    elif step == "curate":
        tables = (state.get("scan") or {}).get("tables", [])
        if not tables:
            # Fallback: reconstruct from scan_pending cache (same as taxonomy step)
            pending = state.get("scan_pending") or {}
            if pending:
                reconstructed = _reconstruct_scan_from_cache(pending)
                if reconstructed:
                    tables = reconstructed["tables"]
                    state["scan"] = reconstructed
                    _save_govern_state(state)
                    log.info("curate: reconstructed scan state from cache (%d tables)", len(tables))
        if not tables:
            return {
                "step":      step,
                "reasoning": reasoning,
                "error":     "No scan result in state. Run 'scan' first.",
            }
        # Count columns to plan batches
        total_columns = sum(len(t.get("columns", [])) for t in tables)
        batch_size    = 40
        batch_count   = (total_columns + batch_size - 1) // batch_size

        # Reset batch progress so a fresh curate run starts clean
        state["curate_progress"] = {"linked": 0, "skipped": 0, "batches_done": []}
        _save_govern_state(state)

        result = {
            "status":        "ready_to_curate",
            "total_columns": total_columns,
            "batch_size":    batch_size,
            "batch_count":   batch_count,
            "next_actions":  [
                {"tool": "curate_batch", "params": {"batch_index": i, "batch_size": batch_size}}
                for i in range(batch_count)
            ],
            "next_step_instruction": (
                f"Call curate_batch for each of the {batch_count} batches above "
                f"(batch_index 0 to {batch_count - 1}). Each call processes {batch_size} columns "
                "and returns visible progress."
            ),
        }

    elif step == "dq_rules":
        tables = (state.get("scan") or {}).get("tables", [])
        if not tables:
            # scan bypass sets scan_pending, not scan — reconstruct from disk cache
            pending = state.get("scan_pending", {})
            scan_from_cache = _reconstruct_scan_from_cache(pending)
            tables = scan_from_cache.get("tables", []) if scan_from_cache else []
        if not tables:
            return {"step": step, "reasoning": reasoning, "error": "No scan result. Run scan first."}

        table       = tables[0]
        table_name  = table["name"]
        external_id = table.get("external_id", "")
        catalog_origin = external_id.split("://", 1)[0] if "://" in external_id else ""

        # Build full column list from scan cache.
        # Exclude columns with unknown data_type — CDQ rule profiling fails when
        # rule occurrences exist on columns without proper CDGC data type metadata.
        all_columns = [
            {
                "column_name": col["name"],
                "column_id":   col["internal_id"],
                "data_type":   col.get("data_type", "unknown"),
            }
            for col in table.get("columns", [])
            if col.get("internal_id") and col.get("data_type", "unknown") != "unknown"
        ]

        # Select a targeted subset: key IDs, dates, status/type cols, then
        # numeric, then a few varchar — keeps the rule count demo-friendly
        column_ids = _select_key_columns(all_columns, max_cols=7)

        skipped = len(all_columns) - len(column_ids)
        selection_note = (
            f"Selected {len(column_ids)} of {len(all_columns)} columns based on column name/type analysis "
            f"(ID/KEY → UNIQUENESS, DATE → TIMELINESS, STATUS/TYPE/NAME → VALIDITY). "
            f"{skipped} lower-priority column(s) skipped to keep rule count demo-friendly."
        )

        # Derive source_table_path from external_id so CDQ executor uses the
        # correct Snowflake database/schema/table (e.g. GOVERNANCE_SCALE_TEST/GOVTEST_MEMBER/TABLE)
        # rather than falling back to the generic IDMC_DQ_SCHEMA_PATH env var.
        source_table_path = ""
        if "://" in external_id:
            path_part = external_id.split("://", 1)[1].split("~")[0]  # DB/SCHEMA/TABLE
            source_table_path = path_part
        log.info("dq_rules: source_table_path=%s", source_table_path)

        dq_params: dict[str, Any] = {
            "table_name":        table_name,
            "column_ids":        column_ids,
            "catalog_origin":    catalog_origin,
        }
        if source_table_path:
            dq_params["source_table_path"] = source_table_path

        result = {
            "status":           "ready_to_create_dq_rules",
            "table":            table_name,
            "total_columns":    len(all_columns),
            "selected_columns": len(column_ids),
            "selection_note":   selection_note,
            "catalog_origin":   catalog_origin,
            "source_table_path": source_table_path,
            "next_actions":     [{
                "tool": "create_generic_dq_rules",
                "params": dq_params,
            }],
            "next_step_instruction": (
                f"1. Call create_generic_dq_rules with the params above. "
                f"Dimensions are auto-selected per column: ID/KEY → UNIQUENESS, "
                f"DATE/TIME → TIMELINESS, others → COMPLETENESS + VALIDITY. "
                f"2. After it returns, immediately call set_dq_occurrences with the "
                f"'occurrences_registered' list from the result so scores can be propagated in step 8."
            ),
        }
        # Merge (don't replace) so a re-plan of this step never drops occurrences that
        # set_dq_occurrences already stored for this table.
        state["dq_rules"] = {
            **(state.get("dq_rules") or {}),
            "table":          table_name,
            "column_count":   len(column_ids),
            "column_ids":     column_ids,
            "catalog_origin": catalog_origin,
        }

    elif step == "propagate_scores":
        dq = state.get("dq_rules") or {}
        table_name  = dq.get("table") or ((state.get("scan") or {}).get("tables") or [{}])[0].get("name", "")
        occurrences = dq.get("occurrences", [])
        if not table_name:
            return {"step": step, "reasoning": reasoning, "error": "No DQ rules in state. Run dq_rules first."}
        if not occurrences:
            return {
                "step":      step,
                "reasoning": reasoning,
                "error":     (
                    "No DQ occurrences stored. After calling create_generic_dq_rules, "
                    "call set_dq_occurrences with its 'occurrences_registered' list, then retry."
                ),
                "hint": "Call set_dq_occurrences(occurrences=<occurrences_registered from create_generic_dq_rules>)",
            }

        propagate_actions = [
            {
                "tool": "upload_dq_scores",
                "params": {
                    "asset_id":    occ["internal_id"],
                    "value":       95,
                    "total_count": 100,
                    "exception":   5,
                    "name":        occ.get("name", ""),
                    "column":      occ.get("column", ""),
                    "dimension":   occ.get("dimension", ""),
                },
            }
            for occ in occurrences
            if occ.get("internal_id")
        ]
        dims = sorted({o["dimension"] for o in occurrences})
        result = {
            "status":          "ready_to_propagate",
            "table":           table_name,
            "occurrence_count": len(occurrences),
            "dimensions":      dims,
            "next_actions":    propagate_actions,
            "next_step_instruction": (
                f"Call upload_dq_scores (governance-engine) for each of the {len(propagate_actions)} occurrence(s) above. "
                "Replace value=95/total_count=100/exception=5 with actual stats from profile results if available."
            ),
        }
        state["propagate_scores"] = {"table": table_name, "dimensions": dims}

    elif step == "mcc_scan":
        # Priority 1: use catalog_origin from DQ rules/scores state — the exact source
        # on which DQ scores were computed, so we scan only that catalog.
        ext_uuid = ""
        catalog_source_name = ""
        dq_result = state.get("dq_rules") or {}
        dq_origin = (
            dq_result.get("catalog_origin")
            or (dq_result.get("rules") or {}).get("catalog_origin")
            or ""
        )
        if dq_origin:
            ext_uuid = dq_origin
            catalog_source_name = dq_result.get("table") or dq_result.get("source_name") or "DQ source"
            log.info("mcc_scan: using catalog_origin from dq_rules state: %s", ext_uuid)

        # Priority 2: extract UUID from scanned table's external_id
        if not ext_uuid:
            scan_tables = (state.get("scan") or {}).get("tables", [])
            if not scan_tables:
                pending = state.get("scan_pending") or {}
                if pending:
                    reconstructed = _reconstruct_scan_from_cache(pending)
                    if reconstructed:
                        scan_tables = reconstructed["tables"]
            for tbl in scan_tables:
                ext = tbl.get("external_id", "")
                log.info("mcc_scan: table=%s external_id=%s", tbl.get("name"), ext)
                if "://" in ext:
                    ext_uuid = ext.split("://")[0]
                    catalog_source_name = tbl.get("connection") or tbl.get("schema") or tbl["name"]
                    break
            if not ext_uuid and scan_tables:
                ext_uuid = _get_catalog_source_uuid(scan_tables[0]["name"]) or ""
                catalog_source_name = scan_tables[0]["name"]

        log.info("mcc_scan: ext_uuid=%s name=%s", ext_uuid, catalog_source_name)
        if not ext_uuid:
            return {"step": step, "reasoning": reasoning, "error": "Could not resolve MCC catalog source UUID from scanned table external IDs."}
        # `catalog_source_name` above is really the scanned *table* name. Keep it
        # for context, but resolve the actual catalog-source name for display.
        scanned_table = catalog_source_name
        # Resolve actual MCC execution ID (may differ from CDGC asset UUID)
        catalog_source_id = _resolve_mcc_source_id(ext_uuid, catalog_source_name)
        display_source = _get_mcc_source_name(catalog_source_id, fallback=catalog_source_name)
        log.info("mcc_scan: final catalog_source_id=%s display=%s", catalog_source_id, display_source)
        try:
            job_resp = _trigger_mcc_scan(catalog_source_id, ["Data Quality"])
            job_id = job_resp.get("jobId") or job_resp.get("id") or ""
            result = {
                "status":         job_resp.get("status", "SUBMITTED"),
                "job_id":         job_id,
                "catalog_source": display_source,
                "table":          scanned_table,
                "catalog_source_id": catalog_source_id,
                "capabilities":   ["Data Quality"],
                "task_groups":    job_resp.get("taskGroups", []),
                "tracking_uri":   job_resp.get("trackingURI", ""),
                "note": (
                    "MCC Data Quality scan submitted. CDQ rules will run against live Snowflake data "
                    "(5000 rows) and publish real scores to DQROs, overwriting the interim scores."
                ),
            }
            state["mcc_scan"] = {"job_id": job_id, "status": job_resp.get("status", "SUBMITTED")}
        except RuntimeError as e:
            # The MCC scan trigger failed. Surface the actual API error verbatim
            # instead of a canned explanation — the cause varies (source not
            # enabled for Data Quality, DTM execution failure on the agent, etc.).
            log.warning("mcc_scan: trigger failed for %s: %s", display_source, e)
            result = {
                "status": "not_triggered",
                "catalog_source": display_source,
                "table":          scanned_table,
                "catalog_source_id": catalog_source_id,
                "trigger_error": str(e),
                "note": (
                    "The MCC Data Quality scan could not be triggered — see the error above for the exact reason. "
                    "Interim DQ scores (95%) from the previous step are already published to CDGC and remain visible."
                ),
            }
            state["mcc_scan"] = {"status": "not_triggered", "trigger_error": str(e)}

    elif step == "create_cdmp_category":
        result = create_cdmp_category()
        state["cdmp_category"] = result

    elif step == "create_cdmp_data_asset":
        result = create_cdmp_data_asset()
        state["cdmp_data_asset"] = result

    elif step == "create_cdmp_collection":
        result = create_cdmp_data_collection()
        state["cdmp_collection"] = result

    elif step == "publish_marketplace":
        result = publish_cdmp_collection()
        state["marketplace"] = result

    elif step == "create_delivery_template":
        result = create_delivery_template()
        state["delivery_template"] = result

    elif step == "create_terms_of_use":
        result = create_terms_of_use()
        state["terms_of_use"] = result

    elif step == "create_delivery_target":
        result = create_delivery_target()
        state["delivery_target"] = result

    elif step == "create_consumer_access":
        result = create_consumer_access()
        state["consumer_access"] = result

    _save_govern_state(state)

    # Determine next step
    idx      = _STEP_ORDER.index(step)
    next_step = _STEP_ORDER[idx + 1] if idx + 1 < len(_STEP_ORDER) else None
    next_hint = (
        next((s["description"] for s in _PIPELINE_STEPS if s["name"] == next_step), "")
        if next_step else "Pipeline complete."
    )

    return {
        "step":           step,
        "reasoning":      reasoning,
        "dry_run":        dry_run,
        "result":         result,
        "next_step":      next_step,
        "next_step_hint": next_hint,
    }


# ---------------------------------------------------------------------------
# Tools: Informatica Data Marketplace (Steps 10–13)
# ---------------------------------------------------------------------------

@mcp.tool()
def create_cdmp_category(
    name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Step 10 — Create (or reuse) a category in Informatica Data Marketplace.

    Auto-derives the category name from the governance domain in pipeline state.
    Checks whether the category already exists before creating a new one.

    Args:
      name:        Category name. Defaults to the top-level domain from taxonomy step.
      description: Category description. Defaults to a generated string.

    Returns: {id, name, status, http_status}
    """
    state = _load_govern_state()

    if not name:
        domains = (state.get("taxonomy") or {}).get("domains", [])
        name = domains[0].get("name", "Governed Data") if domains else "Governed Data"
    if not description:
        description = f"Governed data assets for the {name} domain."

    # Check if category already exists
    try:
        r = _cdmp_request("GET", f"api/v2/categories?name={name}&limit=10")
        if r.status_code == 200:
            items = r.json().get("items") or r.json().get("data") or []
            for item in items:
                if (item.get("name") or "").lower() == name.lower():
                    log.info("create_cdmp_category: reusing existing id=%s", item.get("id"))
                    result = {"id": item["id"], "name": item["name"], "status": "existing", "http_status": 200}
                    state["cdmp_category"] = result
                    _save_govern_state(state)
                    return result
    except Exception as exc:
        log.warning("create_cdmp_category: GET check failed: %s", exc)

    # Create new category
    r = _cdmp_request("POST", "api/v2/categories", json={"name": name, "description": description, "status": "ACTIVE"})
    created = r.status_code in (200, 201)
    body = {}
    try:
        body = r.json()
    except Exception:
        pass

    if not created:
        return {"status": "failed", "http_status": r.status_code, "error": r.text[:400]}

    cat_id = body.get("id") or body.get("categoryId") or ""
    log.info("create_cdmp_category: created id=%s name=%s", cat_id, name)
    result = {"id": cat_id, "name": name, "status": "created", "http_status": r.status_code}
    state["cdmp_category"] = result
    _save_govern_state(state)
    return result


def _get_current_user_id() -> str:
    """Extract the IDMC user_id from the JWT in the env file."""
    import base64
    try:
        env = _read_env()
        jwt = env.get("IDMC_JWT", "")
        if not jwt:
            return ""
        payload_b64 = jwt.split(".")[1]
        # Add padding
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode())
        return payload.get("user_id", "")
    except Exception:
        return ""


def _assign_collection_stakeholders(collection_id: str) -> dict[str, Any]:
    """Assign stakeholders to a CDMP Data Collection.

    Uses CDMP_STAKEHOLDER_IDS from env (comma-separated IDMC user IDs).
    Falls back to the current JWT user if env is empty.
    Endpoint: PATCH api/v1/objects/{id}/stakeholders
    Payload:  {"stakeholdersDetail":[{"roleId":"...","stakeholderId":"...","operation":"ADD"}]}
    """
    env = _read_env()
    raw = env.get("CDMP_STAKEHOLDER_IDS", "").strip()
    user_ids = [u.strip() for u in raw.split(",") if u.strip()] if raw else []

    # Auto-include current user from JWT
    current_user = _get_current_user_id()
    if current_user and current_user not in user_ids:
        user_ids.insert(0, current_user)

    if not user_ids:
        return {"status": "skipped", "note": "No stakeholder IDs available"}

    # Fetch available roles to find the owner role ID
    role_id = ""
    try:
        rr = _cdmp_request("GET", f"api/v1/objects/{collection_id}/availableRoles")
        if rr.status_code == 200:
            roles_body = rr.json()
            roles = roles_body if isinstance(roles_body, list) else (roles_body.get("objects") or roles_body.get("items") or [])
            owner_role = next((r for r in roles if "owner" in (r.get("name") or r.get("roleName") or "").lower()), None)
            if owner_role:
                role_id = owner_role.get("id") or owner_role.get("roleId") or ""
            log.info("_assign_collection_stakeholders: role_id=%s from %d roles", role_id, len(roles))
    except Exception as exc:
        log.warning("_assign_collection_stakeholders: GET roles error %s", exc)

    details = [
        {"roleId": role_id, "stakeholderId": uid, "operation": "ADD"}
        for uid in user_ids
    ]
    payload = {"stakeholdersDetail": details}

    try:
        r = _cdmp_request("PATCH", f"api/v1/objects/{collection_id}/stakeholders", json=payload)
        if r.status_code in (200, 201, 204):
            log.info("_assign_collection_stakeholders: assigned %d stakeholders", len(user_ids))
            return {"assigned": user_ids, "failed": [], "status": "done"}
        elif r.status_code == 400 and "already" in r.text.lower():
            return {"assigned": user_ids, "failed": [], "status": "done"}
        else:
            log.warning("_assign_collection_stakeholders: http=%s %s", r.status_code, r.text[:300])
            return {"assigned": [], "failed": user_ids, "status": "failed", "http_status": r.status_code, "error": r.text[:300]}
    except Exception as exc:
        log.warning("_assign_collection_stakeholders: error %s", exc)
        return {"assigned": [], "failed": user_ids, "status": "error", "error": str(exc)}


def _sync_data_elements(asset_id: str, state: dict) -> dict[str, Any]:
    """Create data elements on a CDMP Data Asset from scanned columns. Skips existing ones."""
    if not asset_id:
        return {"created": 0, "skipped": 0, "failed": 0}

    # Get columns from scan state
    scan = state.get("scan") or {}
    tables = scan.get("tables", [])
    columns = tables[0].get("columns", []) if tables else []
    if not columns:
        return {"created": 0, "skipped": 0, "failed": 0, "note": "no columns in scan state"}

    # Fetch existing data elements to avoid duplicates
    existing_names: set[str] = set()
    try:
        r = _cdmp_request("GET", f"api/v1/dataAssets/{asset_id}/dataElements", params={"limit": 1000})
        if r.status_code == 200:
            body = r.json()
            items = body if isinstance(body, list) else (body.get("objects") or body.get("items") or body.get("data") or [])
            existing_names = {(i.get("name") or "").lower() for i in items}
            log.info("_sync_data_elements: %d existing elements found", len(existing_names))
    except Exception as exc:
        log.warning("_sync_data_elements: GET existing error %s", exc)

    created, skipped, failed = 0, 0, 0
    for col in columns:
        col_name = col.get("name", "")
        if not col_name:
            continue
        if col_name.lower() in existing_names:
            skipped += 1
            continue

        payload = {
            "name":   col_name,
            "status": "ENABLED",
            "type":   col.get("data_type", "VARCHAR"),
        }
        try:
            r = _cdmp_request("POST", f"api/v1/dataAssets/{asset_id}/dataElements", json=payload)
            if r.status_code in (200, 201):
                created += 1
            elif r.status_code == 400 and "already" in r.text.lower():
                skipped += 1
            else:
                failed += 1
                log.warning("_sync_data_elements: %s http=%s %s", col_name, r.status_code, r.text[:200])
        except Exception as exc:
            failed += 1
            log.warning("_sync_data_elements: %s error %s", col_name, exc)

    log.info("_sync_data_elements: created=%d skipped=%d failed=%d", created, skipped, failed)
    return {"created": created, "skipped": skipped, "failed": failed, "total": len(columns)}


@mcp.tool()
def create_cdmp_data_asset(
    asset_name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Step 11 — Search for an existing Data Asset in Data Marketplace (auto-synced from CDGC).

    The asset is auto-synced from CDGC to CDMP via the Integration Source connection.
    This step searches CDMP for the asset by table name and stores its ID for Step 12.
    Steps 1–9 must have run first so the asset exists in CDGC and has been synced.

    Args:
      asset_name:  Asset name to search for. Defaults to the scanned table name from state.
      description: Unused (kept for signature compatibility). Ignored.

    Returns: {name, id, status: "found"|"not_found", source, type}
    """
    state = _load_govern_state()

    scan = state.get("scan") or {}
    tables = scan.get("tables", [])
    table_row = tables[0] if tables else {}

    if not asset_name:
        asset_name = table_row.get("name", "Governed Dataset")

    domains = (state.get("taxonomy") or {}).get("domains", [])
    domain_name = domains[0].get("name", "") if domains else ""

    # Use the CDGC internal_id directly — it's already registered in CDMP via
    # the Integration Source and carries DQ scores from CDGC automatically.
    cdgc_id = table_row.get("internal_id", "")
    if cdgc_id:
        log.info("create_cdmp_data_asset: using CDGC internal_id=%s for %s", cdgc_id, asset_name)
        result = {
            "name":   asset_name,
            "id":     cdgc_id,
            "domain": domain_name,
            "status": "found",
            "source": "cdgc_internal_id",
            "type":   "Table",
        }
        result["data_elements"] = _sync_data_elements(cdgc_id, state)
        state["cdmp_data_asset"] = result
        _save_govern_state(state)
        return result

    # Fallback: search CDMP for the asset by name (prefer Table type for DQ linkage)
    log.info("create_cdmp_data_asset: no CDGC id, searching CDMP for %s", asset_name)
    try:
        r = _cdmp_request("GET", "api/v1/dataAssets", params={"search": asset_name, "fields": "NAME,ID,TYPE"})
        if r.status_code == 200:
            body = r.json()
            items = body if isinstance(body, list) else (body.get("objects") or body.get("items") or body.get("data") or [])
            # Prefer Table type (CDGC-sourced), then any name match
            match = next((i for i in items if (i.get("name") or "").lower() == asset_name.lower() and (i.get("type") or "").lower() == "table"), None)
            if not match:
                match = next((i for i in items if (i.get("name") or "").lower() == asset_name.lower()), None)
            if match:
                asset_id = match.get("id") or ""
                result = {
                    "name":   match.get("name", asset_name),
                    "id":     asset_id,
                    "domain": domain_name,
                    "status": "found",
                    "source": "cdmp_search",
                    "type":   match.get("type", "Dataset"),
                }
                result["data_elements"] = _sync_data_elements(asset_id, state)
                state["cdmp_data_asset"] = result
                _save_govern_state(state)
                return result
    except Exception as exc:
        log.warning("create_cdmp_data_asset: search error %s", exc)

    result = {
        "name":   asset_name,
        "id":     "",
        "domain": domain_name,
        "status": "not_found",
        "source": "cdmp_search",
        "type":   "Dataset",
        "error":  "Asset not found in CDMP and no CDGC internal_id available.",
    }
    state["cdmp_data_asset"] = result
    _save_govern_state(state)
    return result


@mcp.tool()
def create_cdmp_data_collection(
    name: str | None = None,
    description: str | None = None,
    category_id: str | None = None,
) -> dict[str, Any]:
    """Step 12 — Create and publish a Data Collection in Informatica Data Marketplace.

    Creates a PUBLISHED Data Collection under the category from Step 10.
    All parameters are auto-resolved from pipeline state.

    Args:
      name:        Collection name. Defaults to "<table_name> — Governed Dataset".
      description: Collection description.
      category_id: CDMP category id. Defaults to the one created in Step 10.

    Returns: {id, externalId, name, status, http_status, marketplace_url}
    """
    state = _load_govern_state()

    if not category_id:
        category_id = (state.get("cdmp_category") or {}).get("id", "")
    if not category_id:
        return {"status": "failed", "error": "No category_id — run create_cdmp_category first."}

    asset_name = (state.get("cdmp_data_asset") or {}).get("name", "")
    if not asset_name:
        scan = state.get("scan") or {}
        tables = scan.get("tables", [])
        asset_name = tables[0].get("name", "Governed Dataset") if tables else "Governed Dataset"

    if not name:
        name = f"{asset_name} — Governed Dataset"
    if not description:
        dq_dims = (state.get("propagate_scores") or {}).get("dimensions", [])
        score_note = f"DQ scored on {', '.join(dq_dims)}." if dq_dims else ""
        description = (
            f"Curated, quality-scored dataset governed in CDGC. "
            f"Business terms linked, DQROs registered. {score_note}".strip()
        )

    asset_id = (state.get("cdmp_data_asset") or {}).get("id", "")

    # Check if collection already exists in state or in CDMP by name
    existing_id = (state.get("cdmp_collection") or {}).get("id", "")
    if not existing_id:
        try:
            rs = _cdmp_request("GET", "api/v2/data-collections", params={"name": name, "limit": 10})
            if rs.status_code == 200:
                rj = rs.json()
                items = rj if isinstance(rj, list) else (rj.get("objects") or rj.get("items") or rj.get("data") or [])
                match = next((i for i in items if (i.get("name") or "").lower() == name.lower()), None)
                if match:
                    existing_id = match.get("id", "")
                    log.info("create_cdmp_data_collection: found existing id=%s", existing_id)
        except Exception as exc:
            log.warning("create_cdmp_data_collection: existence check error %s", exc)

    if existing_id:
        collection_id = existing_id
        external_id = (state.get("cdmp_collection") or {}).get("externalId", "")
        log.info("create_cdmp_data_collection: reusing existing collection id=%s", collection_id)
    else:
        body: dict[str, Any] = {
            "name":        name,
            "description": description,
            "status":      "PUBLISHED",
            "categoryId":  category_id,
        }
        r = _cdmp_request("POST", "api/v2/data-collections", json=body)
        if r.status_code not in (200, 201):
            return {"status": "failed", "http_status": r.status_code, "error": r.text[:400]}
        resp_body: dict[str, Any] = {}
        try:
            resp_body = r.json()
        except Exception:
            pass
        collection_id = resp_body.get("id") or ""
        external_id   = resp_body.get("externalId") or ""
        log.info("create_cdmp_data_collection: created id=%s externalId=%s", collection_id, external_id)

    # Link Data Asset to Collection via API
    asset_linked = False
    asset_link_status: dict[str, Any] = {}
    if collection_id and asset_id:
        link_body = [{"assetId": asset_id, "operation": "ADD"}]
        lr = _cdmp_request("PUT", f"api/v1/dataCollections/{collection_id}/dataAssets", json=link_body)
        asset_linked = lr.status_code in (200, 201, 204)
        asset_link_status = {
            "http_status": lr.status_code,
            "status": "linked" if asset_linked else "failed",
            **({"error": lr.text[:200]} if not asset_linked else {}),
        }
        log.info("create_cdmp_data_collection: asset-collection link http=%s", lr.status_code)
    elif not asset_id:
        asset_link_status = {"status": "skipped", "note": "No asset_id in state — run create_cdmp_data_asset first."}

    marketplace_url = f"https://cdmp-app.dm-us.informaticacloud.com/data-collections/{collection_id}"

    # Assign stakeholders (current JWT user + any from CDMP_STAKEHOLDER_IDS env)
    stakeholders_result = _assign_collection_stakeholders(collection_id)

    result = {
        "id":              collection_id,
        "externalId":      external_id,
        "name":            name,
        "asset_linked":          asset_linked,
        "asset_collection_link": asset_link_status,
        "stakeholders":          stakeholders_result,
        "status":                "existing" if existing_id else "created",
        "marketplace_url":       marketplace_url,
    }
    state["cdmp_collection"] = result
    _save_govern_state(state)
    return result


@mcp.tool()
def publish_cdmp_collection(
    collection_id: str | None = None,
) -> dict[str, Any]:
    """Step 13 — Publish the Data Collection to Informatica Data Marketplace.

    Flips the collection from DRAFT to PUBLISHED so data consumers can
    discover and request access to the governed dataset.

    Args:
      collection_id: CDMP collection id. Defaults to the one created in the previous step.

    Returns: {collection_id, status, published, http_status, marketplace_url}
    """
    state = _load_govern_state()

    if not collection_id:
        collection_id = (state.get("cdmp_collection") or {}).get("id", "")
    if not collection_id:
        return {"status": "failed", "error": "No collection_id — run create_cdmp_data_collection first."}

    r = _cdmp_request("GET", f"api/v2/data-collections/{collection_id}")
    log.info("publish_cdmp_collection: verify id=%s http=%s", collection_id, r.status_code)

    ok = r.status_code == 200
    resp_body: dict[str, Any] = {}
    try:
        resp_body = r.json()
    except Exception:
        pass

    external_id    = resp_body.get("externalId") or (state.get("cdmp_collection") or {}).get("externalId", "")
    marketplace_url = f"https://cdmp-app.dm-us.informaticacloud.com/data-collections/{collection_id}"

    return {
        "collection_id":   collection_id,
        "external_id":     external_id,
        "status":          resp_body.get("status", "PUBLISHED") if ok else "unknown",
        "published":       ok,
        "http_status":     r.status_code,
        "marketplace_url": marketplace_url,
        **({"error": r.text[:400]} if not ok else {}),
    }


@mcp.tool()
def create_cdmp_usage_contexts(
    contexts: list[str] | None = None,
) -> dict[str, Any]:
    """Step 14 — Create Usage Contexts in Informatica Data Marketplace.

    Usage Contexts are required for consumers to place orders. This step
    creates standard usage context options (Analytics, Reporting, Finance,
    Operations) so the checkout Usage Context dropdown is populated.

    Args:
      contexts: List of context names. Defaults to standard business contexts.

    Returns: {created, skipped, failed, contexts}
    """
    if not contexts:
        contexts = ["Analytics", "Reporting", "Finance", "Operations", "Compliance", "Data Science"]

    created, skipped, failed = [], [], []
    context_ids: dict[str, str] = {}

    # Fetch existing contexts once
    existing_items: list[dict] = []
    try:
        r = _cdmp_request("GET", "api/v1/usageContext", params={"limit": 1000})
        if r.status_code == 200:
            body = r.json()
            existing_items = body if isinstance(body, list) else (body.get("objects") or body.get("items") or body.get("data") or [])
            log.info("create_cdmp_usage_contexts: GET returned %d items", len(existing_items))
    except Exception:
        pass

    # Build map of existing name -> id
    existing_map: dict[str, str] = {
        (i.get("name") or "").lower(): i.get("id", "") for i in existing_items
    }

    for ctx_name in contexts:
        lower = ctx_name.lower()
        if lower in existing_map:
            skipped.append(ctx_name)
            context_ids[ctx_name] = existing_map[lower]
            continue

        # Create
        r = _cdmp_request("POST", "api/v1/usageContext", json={"name": ctx_name, "status": "ACTIVE", "color": "#6366F1"})
        if r.status_code in (200, 201):
            created.append(ctx_name)
            try:
                ctx_id = r.json().get("id", "")
                context_ids[ctx_name] = ctx_id
            except Exception:
                context_ids[ctx_name] = ""
        elif r.status_code == 400 and "already" in r.text.lower():
            # Already exists — try to get ID from existing_map or re-fetch
            skipped.append(ctx_name)
            if lower in existing_map:
                context_ids[ctx_name] = existing_map[lower]
            else:
                # Re-fetch to find it
                try:
                    r2 = _cdmp_request("GET", "api/v1/usageContext", params={"limit": 1000})
                    body2 = r2.json()
                    items2 = body2 if isinstance(body2, list) else (body2.get("objects") or body2.get("items") or [])
                    match = next((i for i in items2 if (i.get("name") or "").lower() == lower), None)
                    context_ids[ctx_name] = match.get("id", "") if match else ""
                except Exception:
                    context_ids[ctx_name] = ""
            log.info("create_cdmp_usage_contexts: %s already exists, id=%s", ctx_name, context_ids.get(ctx_name))
        else:
            failed.append({"name": ctx_name, "http_status": r.status_code, "error": r.text[:200]})

    log.info("create_cdmp_usage_contexts: created=%s skipped=%s failed=%s", created, skipped, failed)
    result = {
        "created":     created,
        "skipped":     skipped,
        "failed":      failed,
        "contexts":    contexts,
        "context_ids": context_ids,
        "status":      "done" if not failed else "partial",
    }
    state = _load_govern_state()
    state["cdmp_usage_contexts"] = result
    _save_govern_state(state)
    return result


@mcp.tool()
def create_delivery_template(
    name: str | None = None,
    method: str = "DOWNLOAD",
) -> dict[str, Any]:
    """Step 15 — Create Delivery Format, Method, and Delivery Template in Data Marketplace.

    Creates (or reuses) a CSV Delivery Format and DOWNLOAD Delivery Method, then
    creates a Manual Delivery Template with Vivek as owner.

    Args:
      name:   Template name. Defaults to "<TABLE> — Download Delivery".
      method: Delivery method name. Defaults to DOWNLOAD.

    Returns: {name, id, format_id, method_id, collection_id, status}
    """
    state = _load_govern_state()

    collection_id = (state.get("cdmp_collection") or {}).get("id", "")
    if not collection_id:
        return {"status": "failed", "error": "No collection_id — run create_cdmp_data_collection first."}

    ctx = _cdmp_asset_context(state)
    asset_name = ctx["asset_name"]
    connection = ctx["connection"]
    schema = ctx["schema"]
    table_name = ctx["table_name"]
    source_path = ctx["source_path"]

    if not name:
        name = f"{asset_name} — Download Delivery"

    # --- a. Check/Create Delivery Format (CSV) ---
    format_id = ""
    format_name = "CSV"
    try:
        r = _cdmp_request("GET", "api/v1/provisioning/deliveryFormats")
        log.warning("create_delivery_template: deliveryFormats GET1 raw=%s", r.text[:600])
        if r.status_code == 200:
            body = r.json()
            items = body if isinstance(body, list) else (body.get("objects") or body.get("items") or body.get("data") or body.get("content") or [])
            match = next((i for i in items if (i.get("name") or "").lower() == format_name.lower()), None)
            if match:
                format_id = match.get("id") or match.get("formatId") or match.get("uuid") or ""
                log.info("create_delivery_template: reused deliveryFormat id=%s", format_id)
    except Exception as exc:
        log.warning("create_delivery_template: GET deliveryFormats error %s", exc)

    if not format_id:
        r = _cdmp_request("POST", "api/v1/provisioning/deliveryFormats", json={"name": format_name, "status": "ACTIVE"})
        if r.status_code in (200, 201):
            try:
                rj = r.json()
                format_id = rj.get("id") or rj.get("formatId") or rj.get("uuid") or ""
            except Exception:
                pass
            log.info("create_delivery_template: created deliveryFormat id=%s", format_id)
        elif r.status_code == 400 and "already exist" in r.text.lower():
            # Exists but not returned by GET — search again with larger limit
            try:
                r2 = _cdmp_request("GET", "api/v1/provisioning/deliveryFormats", params={"limit": 1000})
                log.warning("create_delivery_template: deliveryFormats GET2 raw=%s", r2.text[:600])
                rj2 = r2.json()
                items2 = rj2 if isinstance(rj2, list) else (rj2.get("objects") or rj2.get("items") or rj2.get("data") or rj2.get("content") or [])
                log.warning("create_delivery_template: deliveryFormats raw sample=%s", str(items2[:2])[:400])
                match = next((i for i in items2 if (i.get("name") or "").lower() == format_name.lower()), None)
                if match:
                    format_id = match.get("id") or match.get("formatId") or match.get("uuid") or ""
            except Exception as exc2:
                log.warning("create_delivery_template: deliveryFormats GET2 error %s", exc2)
            log.info("create_delivery_template: deliveryFormat already exists, id=%s", format_id)
        else:
            return {"status": "failed", "step": "deliveryFormat", "http_status": r.status_code, "error": r.text[:400]}

    # --- b. Check/Create Delivery Method (DOWNLOAD) ---
    method_id = ""
    try:
        r = _cdmp_request("GET", "api/v1/provisioning/deliveryMethods")
        if r.status_code == 200:
            body = r.json()
            items = body if isinstance(body, list) else (body.get("objects") or body.get("items") or body.get("data") or body.get("content") or [])
            match = next((i for i in items if (i.get("name") or "").lower() == method.lower()), None)
            if match:
                method_id = match.get("id") or match.get("methodId") or match.get("uuid") or ""
                log.info("create_delivery_template: reused deliveryMethod id=%s", method_id)
    except Exception as exc:
        log.warning("create_delivery_template: GET deliveryMethods error %s", exc)

    if not method_id:
        r = _cdmp_request("POST", "api/v1/provisioning/deliveryMethods", json={"name": method, "status": "ACTIVE"})
        if r.status_code in (200, 201):
            try:
                method_id = r.json().get("id", "")
            except Exception:
                pass
            log.info("create_delivery_template: created deliveryMethod id=%s", method_id)
        elif r.status_code == 400 and "already exist" in r.text.lower():
            try:
                r2 = _cdmp_request("GET", "api/v1/provisioning/deliveryMethods", params={"limit": 1000})
                rj2 = r2.json()
                items2 = rj2 if isinstance(rj2, list) else (rj2.get("objects") or rj2.get("items") or rj2.get("data") or rj2.get("content") or [])
                match = next((i for i in items2 if (i.get("name") or "").lower() == method.lower()), None)
                if match:
                    method_id = match.get("id") or match.get("methodId") or match.get("uuid") or ""
            except Exception as exc2:
                log.warning("create_delivery_template: deliveryMethods GET2 error %s", exc2)
            log.info("create_delivery_template: deliveryMethod already exists, id=%s", method_id)
        else:
            return {"status": "failed", "step": "deliveryMethod", "http_status": r.status_code, "error": r.text[:400]}

    # --- c. Create/update Delivery Template ---
    template_body: dict[str, Any] = {
        "name":                    name,
        "description":             f"<p>Download delivery template for {asset_name}</p>",
        "status":                  "ACTIVE",
        "managedAccess":           "DISABLED",
        "deliveryType":            "MANUAL",
        "targetSystemReference":   connection,
        "defaultPhysicalLocation": source_path,
        "deliveryMethodIds":       [method_id],
        "deliveryFormatIds":       [format_id],
    }

    # Pre-check: find existing template by name to avoid duplicate-name JSON parse error
    delivery_template_id = ""
    try:
        r_list = _cdmp_request("GET", "api/v1/provisioning/deliveryTemplates", params={"limit": 1000})
        if r_list.status_code == 200:
            list_body = r_list.json()
            items_list = list_body if isinstance(list_body, list) else (list_body.get("objects") or list_body.get("items") or list_body.get("data") or [])
            existing = next((i for i in items_list if (i.get("name") or "").lower() == name.lower()), None)
            if existing:
                delivery_template_id = existing.get("id", "")
                log.info("create_delivery_template: found existing id=%s, updating via PUT", delivery_template_id)
                _cdmp_request("PUT", f"api/v1/provisioning/deliveryTemplates/{delivery_template_id}", json=template_body)
    except Exception as exc:
        log.warning("create_delivery_template: pre-check error %s", exc)

    if not delivery_template_id:
        log.info("create_delivery_template: POST body=%s", template_body)
        r = _cdmp_request("POST", "api/v1/provisioning/deliveryTemplates", json=template_body)
        log.info("create_delivery_template: POST status=%s body=%s", r.status_code, r.text[:400])
        if r.status_code in (200, 201):
            try:
                delivery_template_id = r.json().get("id", "")
            except Exception:
                pass
        else:
            return {"status": "failed", "step": "deliveryTemplate", "http_status": r.status_code, "error": r.text[:400]}

    log.info("create_delivery_template: created template id=%s format_id=%s method_id=%s",
             delivery_template_id, format_id, method_id)

    result: dict[str, Any] = {
        "name":                 name,
        "id":                   delivery_template_id,
        "format_id":            format_id,
        "method_id":            method_id,
        "collection_id":        collection_id,
        "status":               "created",
    }
    state["delivery_template"] = result
    _save_govern_state(state)
    return result


@mcp.tool()
def create_terms_of_use(
    name: str | None = None,
    content: str | None = None,
) -> dict[str, Any]:
    """Step 16 — Create Terms of Use and link it to the Data Collection in Data Marketplace.

    Creates a PERMISSIVE Terms of Use record then links it to the collection via PUT.

    Args:
      name:    Terms name. Defaults to "<TABLE> — Terms of Use".
      content: Legal text. Defaults to a standard governed-data usage clause.

    Returns: {name, id, collection_id, linked, status}
    """
    state = _load_govern_state()

    _dt = state.get("delivery_template") or {}
    collection_id = (state.get("cdmp_collection") or {}).get("id", "") or _dt.get("collection_id", "")
    if not collection_id:
        return {"status": "failed", "error": "No collection_id — run create_cdmp_data_collection first."}

    asset_name  = (state.get("cdmp_data_asset") or {}).get("name", "Governed Dataset")
    domain_name = (state.get("cdmp_data_asset") or {}).get("domain", "")

    if not name:
        name = f"{asset_name} — Terms of Use"
    if not content:
        content = (
            f"By accessing the {asset_name} dataset from the {domain_name} domain, you agree to: "
            "(1) use this data solely for authorised business purposes; "
            "(2) not share or redistribute the data outside your organisation without approval; "
            "(3) comply with all applicable data privacy and governance policies. "
            "Access is subject to periodic review and may be revoked for non-compliance."
        )

    # --- a. Create Terms of Use ---
    tou_body = {
        "name":            name,
        "description":     f"<p>{content}</p>",
        "status":          "ENABLED",
        "type":            "PERMISSIVE",
        "acknowledgement": True,
        "referenceLink":   "",
    }
    r = _cdmp_request("POST", "api/v1/termsOfUse", json=tou_body)
    tou_id = ""
    if r.status_code in (200, 201):
        try:
            tou_id = r.json().get("id", "")
        except Exception:
            pass
        log.info("create_terms_of_use: created tou id=%s", tou_id)
    elif r.status_code == 400 and "already exist" in r.text.lower():
        try:
            r2 = _cdmp_request("GET", "api/v1/termsOfUse", params={"limit": 1000})
            rj2 = r2.json()
            items2 = rj2 if isinstance(rj2, list) else (rj2.get("objects") or rj2.get("items") or rj2.get("data") or [])
            match = next((i for i in items2 if (i.get("name") or "").lower() == name.lower()), None)
            if match:
                tou_id = match.get("id", "")
        except Exception as exc:
            log.warning("create_terms_of_use: GET existing error %s", exc)
        log.info("create_terms_of_use: already exists, id=%s", tou_id)
    else:
        return {"status": "failed", "step": "termsOfUse", "http_status": r.status_code, "error": r.text[:400]}

    # --- b. Link ToU → Collection (skip if already linked) ---
    linked = False
    link_status: dict[str, Any] = {}
    if tou_id:
        already_linked = False
        try:
            existing_r = _cdmp_request("GET", f"api/v1/dataCollections/{collection_id}/termsOfUse")
            existing_items = existing_r.json() if existing_r.status_code == 200 else []
            if not isinstance(existing_items, list):
                existing_items = existing_items.get("objects") or existing_items.get("items") or []
            already_linked = any((i.get("id") or i.get("termsOfUseId")) == tou_id for i in existing_items)
        except Exception:
            pass
        if already_linked:
            linked = True
            link_status = {"status": "already_linked"}
            log.info("create_terms_of_use: tou already linked, skipping ADD")
        else:
            link_body = [{"termsOfUseId": tou_id, "operation": "ADD"}]
            lr = _cdmp_request("PUT", f"api/v1/dataCollections/{collection_id}/termsOfUse", json=link_body)
            linked = lr.status_code in (200, 201, 204)
            link_status = {
                "http_status": lr.status_code,
                "status": "linked" if linked else "failed",
                **({"error": lr.text[:200]} if not linked else {}),
            }
            log.info("create_terms_of_use: link http=%s", lr.status_code)

    result: dict[str, Any] = {
        "name":          name,
        "id":            tou_id,
        "collection_id": collection_id,
        "linked":        linked,
        "link_status":   link_status,
        "status":        "created",
    }
    state["terms_of_use"] = result
    _save_govern_state(state)
    return result


@mcp.tool()
def create_delivery_target(
    name: str | None = None,
    target_type: str = "SNOWFLAKE",
) -> dict[str, Any]:
    """Step 17 — Create a Delivery Target in Data Marketplace via API.

    Creates a provisioning Delivery Target linked to the collection, delivery
    template, method, and format from previous steps.

    Args:
      name:        Target name. Defaults to "<TABLE> — Download Target".
      target_type: Destination type (informational only). Defaults to SNOWFLAKE.

    Returns: {name, id, collection_id, delivery_template_id, status}
    """
    state = _load_govern_state()

    delivery_template = state.get("delivery_template") or {}
    delivery_template_id = delivery_template.get("id", "")
    collection_id = (state.get("cdmp_collection") or {}).get("id", "") or delivery_template.get("collection_id", "")
    if not collection_id:
        return {"status": "failed", "error": "No collection_id — run create_cdmp_data_collection first."}
    method_id = delivery_template.get("method_id", "")
    format_id = delivery_template.get("format_id", "")
    if not delivery_template_id:
        return {"status": "failed", "error": "No delivery_template_id — run create_delivery_template first."}

    ctx = _cdmp_asset_context(state)
    asset_name = ctx["asset_name"]
    connection = ctx["connection"]
    source_path = ctx["source_path"]

    if not name:
        name = f"{asset_name} — Download Target"

    target_body = {
        "name":                  name,
        "description":           f"<p>Download target for {asset_name}</p>",
        "status":                "ACTIVE",
        "targetSystemReference": connection,
        "physicalLocation":      source_path,
        "deliveryTemplateId":    delivery_template_id,
        "deliveryMethodId":      method_id,
        "deliveryFormatId":      format_id,
        "dataCollectionId":      collection_id,
        "deliveryOwners":        [{"id": "90phiCbIePOkIqwBx6eDeB"}],
    }
    r = _cdmp_request("POST", "api/v1/provisioning/deliveryTargets", json=target_body)
    delivery_target_id = ""
    if r.status_code in (200, 201):
        try:
            delivery_target_id = r.json().get("id", "")
        except Exception:
            pass
        log.info("create_delivery_target: created id=%s", delivery_target_id)
    elif r.status_code == 400 and "already exist" in r.text.lower():
        # Fall back to ID already saved in state (prevents re-runs from wiping the ID)
        delivery_target_id = (state.get("delivery_target") or {}).get("id", "")
        if delivery_target_id:
            log.info("create_delivery_target: already exists, reusing state id=%s", delivery_target_id)
        else:
            # Try collection endpoint as last resort
            try:
                r2 = _cdmp_request("GET", f"api/v1/dataCollections/{collection_id}/deliveryTargets", params={"limit": 1000})
                rj2 = r2.json()
                items2 = rj2 if isinstance(rj2, list) else (rj2.get("objects") or rj2.get("items") or rj2.get("data") or [])
                log.info("create_delivery_target: collection targets sample=%s", str(items2[:1])[:300])
                match = next((i for i in items2 if (i.get("name") or "").lower() == name.lower()), None)
                if not match and items2:
                    match = items2[0]
                if match:
                    delivery_target_id = match.get("id", "")
            except Exception as exc:
                log.warning("create_delivery_target: GET existing error %s", exc)
            log.info("create_delivery_target: already exists, id=%s", delivery_target_id)
    else:
        return {"status": "failed", "step": "deliveryTarget", "http_status": r.status_code, "error": r.text[:400]}

    result: dict[str, Any] = {
        "name":                 name,
        "id":                   delivery_target_id,
        "collection_id":        collection_id,
        "delivery_template_id": delivery_template_id,
        "status":               "created",
    }
    state["delivery_target"] = result
    _save_govern_state(state)
    return result


@mcp.tool()
def create_consumer_access(
    consumer_email: str | None = None,
) -> dict[str, Any]:
    """Step 18 — Place an order for consumer access to the published Data Collection.

    Places a CDMP order against the collection using the Analytics usage context
    (or first available) and the provisioned delivery target from Step 17.

    Args:
      consumer_email: Email of the intended consumer (informational). Defaults to IDMC_USER from .env.

    Returns: {order_id, order_ref, collection_id, status, marketplace_url}
    """
    state = _load_govern_state()

    _dt = state.get("delivery_template") or {}
    collection_id = (state.get("cdmp_collection") or {}).get("id", "") or _dt.get("collection_id", "")
    if not collection_id:
        return {"status": "failed", "error": "No collection_id — run create_cdmp_data_collection first."}

    delivery_target_id = (state.get("delivery_target") or {}).get("id", "")
    if not delivery_target_id:
        log.warning("create_consumer_access: no delivery_target_id, proceeding without it")

    # Get usage_context_id — prefer "Analytics", fall back to first available
    context_ids: dict[str, str] = (state.get("cdmp_usage_contexts") or {}).get("context_ids", {})
    usage_context_id = context_ids.get("Analytics") or (next(iter(context_ids.values()), "") if context_ids else "")
    if not usage_context_id:
        return {"status": "failed", "error": "No usage_context_id — run create_cdmp_usage_contexts first."}

    if not consumer_email:
        env = _read_env()
        consumer_email = env.get("IDMC_USER", "consumer@example.com")

    marketplace_url = (state.get("cdmp_collection") or {}).get("marketplace_url", "")

    order_body: dict[str, Any] = {
        "dataCollectionIds": [collection_id],
        "usageContextId":    usage_context_id,
        "justification":     "Governed dataset access requested via AI pipeline",
        "customFields":      [],
    }
    if delivery_target_id:
        order_body["requestedProvisionedTargetRef"] = delivery_target_id
    r = _cdmp_request("POST", "api/v1/orders", json=order_body, timeout=60)
    if r.status_code not in (200, 201):
        return {
            "status":        "failed",
            "step":          "order",
            "http_status":   r.status_code,
            "error":         r.text[:400],
            "collection_id": collection_id,
            "marketplace_url": marketplace_url,
        }

    order_resp: dict[str, Any] = {}
    try:
        order_resp = r.json()
    except Exception:
        pass

    order_id  = order_resp.get("id", "")
    order_ref = order_resp.get("refId", order_resp.get("referenceId", ""))
    order_status = order_resp.get("status", "SUBMITTED")

    log.info("create_consumer_access: order id=%s ref=%s status=%s", order_id, order_ref, order_status)

    # Look up consumerAccess ID now (available once order is placed) for later withdrawal
    consumer_access_id = ""
    try:
        r_ca = _cdmp_request("GET", "api/v1/consumerAccess", params={"limit": 200}, timeout=60)
        if r_ca.status_code == 200:
            ca_items = r_ca.json()
            ca_items = ca_items if isinstance(ca_items, list) else (ca_items.get("objects") or ca_items.get("items") or ca_items.get("data") or [])
            ca_match = next((i for i in ca_items if (i.get("dataCollection") or {}).get("id") == collection_id), None)
            if ca_match:
                consumer_access_id = ca_match.get("id", "")
                log.info("create_consumer_access: consumerAccess id=%s ref=%s", consumer_access_id, ca_match.get("refId"))
    except Exception as exc:
        log.warning("create_consumer_access: consumerAccess lookup error %s", exc)

    result = {
        "order_id":           order_id,
        "order_ref":          order_ref,
        "collection_id":      collection_id,
        "consumer":           consumer_email,
        "status":             order_status,
        "marketplace_url":    marketplace_url,
        "consumer_access_id": consumer_access_id,
    }
    state["consumer_access"] = result
    _save_govern_state(state)
    return result


@mcp.tool()
def approve_consumer_order() -> dict[str, Any]:
    """Step 19 — Approve and fulfill the pending consumer order.

    Calls PUT api/v1/orders/{id}/approve then PUT api/v1/orders/{id}/fulfill
    using the order placed in Step 18. Completes the full producer→consumer loop.

    Returns: {order_id, order_ref, approve, fulfill, status, marketplace_url}
    """
    state = _load_govern_state()

    access = state.get("consumer_access") or {}
    order_id  = access.get("order_id", "")
    order_ref = access.get("order_ref", "")
    marketplace_url = access.get("marketplace_url", "")

    if not order_id:
        return {"status": "failed", "error": "No order_id — run create_consumer_access (Step 18) first."}

    delivery_target_id = (state.get("delivery_target") or {}).get("id", "")
    _dt = state.get("delivery_template") or {}
    if not delivery_target_id:
        delivery_target_id = _dt.get("delivery_target_id", "")

    # Approve
    ra = _cdmp_request("PUT", f"api/v1/orders/{order_id}/approve",
                       json={"costCenter": "", "customFields": []})
    approve_status = "approved" if ra.status_code in (200, 201, 204) else f"failed:{ra.status_code}:{ra.text[:200]}"
    log.info("approve_consumer_order: approve http=%s", ra.status_code)

    # Fulfill
    fulfill_status = ""
    final_status = "APPROVED"
    if ra.status_code in (200, 201, 204):
        rf = _cdmp_request("PUT", f"api/v1/orders/{order_id}/fulfill",
                           json={"deliveryTargetId": delivery_target_id,
                                 "costCenter": "", "customFields": []})
        fulfill_status = "fulfilled" if rf.status_code in (200, 201, 204) else f"failed:{rf.status_code}:{rf.text[:200]}"
        log.info("approve_consumer_order: fulfill http=%s", rf.status_code)
        if rf.status_code in (200, 201, 204):
            final_status = "FULFILLED"

    result = {
        "order_id":        order_id,
        "order_ref":       order_ref,
        "approve":         approve_status,
        "fulfill":         fulfill_status,
        "status":          final_status,
        "marketplace_url": marketplace_url,
    }
    access.update(result)
    state["consumer_access"] = access
    _save_govern_state(state)
    return result


@mcp.tool()
def verify_consumer_access() -> dict[str, Any]:
    """Step 19 — Check the live status of the consumer order placed in Step 18."""
    state = _load_govern_state()
    access = state.get("consumer_access") or {}
    order_id  = access.get("order_id", "")
    order_ref = access.get("order_ref", "")
    marketplace_url = access.get("marketplace_url", "")
    if not order_id:
        return {"status": "failed", "error": "No order_id — run create_consumer_access (Step 18) first."}
    r = _cdmp_request("GET", f"api/v1/orders/{order_id}")
    if r.status_code != 200:
        return {"status": "failed", "http_status": r.status_code, "error": r.text[:300]}
    data = r.json()
    order_status = data.get("status", "UNKNOWN")
    fulfilled    = order_status in ("FULFILLED", "COMPLETE", "ACCESS_GRANTED")
    result = {
        "order_id":        order_id,
        "order_ref":       order_ref,
        "status":          order_status,
        "fulfilled":       fulfilled,
        "marketplace_url": marketplace_url,
    }
    access.update(result)
    state["consumer_access"] = access
    _save_govern_state(state)
    return result


@mcp.tool()
def withdraw_consumer_access() -> dict[str, Any]:
    """Step 14 — Withdraw the consumer's access to the data collection."""
    state = _load_govern_state()
    access = state.get("consumer_access") or {}
    order_id  = access.get("order_id", "")
    order_ref = access.get("order_ref", "")
    marketplace_url = access.get("marketplace_url", "")
    if not order_id:
        return {"status": "failed", "error": "No order_id — run create_consumer_access (Step 12) first."}

    # Find consumerAccess ID — the real withdraw endpoint is PUT /api/v1/consumerAccess/{id}/status
    collection_id = access.get("collection_id", "")
    consumer_access_id = access.get("consumer_access_id", "")
    if not consumer_access_id:
        try:
            r_list = _cdmp_request("GET", "api/v1/consumerAccess", params={"limit": 200}, timeout=60)
            if r_list.status_code == 200:
                items = r_list.json()
                items = items if isinstance(items, list) else (items.get("objects") or items.get("items") or items.get("data") or [])
                match = next((i for i in items if (i.get("dataCollection") or {}).get("id") == collection_id), None)
                if match:
                    consumer_access_id = match.get("id", "")
                    log.info("withdraw_consumer_access: found consumerAccess id=%s ref=%s", consumer_access_id, match.get("refId"))
        except Exception as exc:
            log.warning("withdraw_consumer_access: consumerAccess lookup error %s", exc)

    if consumer_access_id:
        rw = _cdmp_request("PUT", f"api/v1/consumerAccess/{consumer_access_id}/status",
                           json={"status": "WITHDRAWN"}, timeout=60)
        log.info("withdraw_consumer_access: PUT status=%s body=%s", rw.status_code, rw.text[:200])
        if rw.status_code in (200, 201, 204):
            result = {
                "order_id":            order_id,
                "order_ref":           order_ref,
                "consumer_access_id":  consumer_access_id,
                "status":              "WITHDRAWN",
                "withdrawn":           True,
                "marketplace_url":     marketplace_url,
            }
            state["consumer_access"] = {**access, **result}
            _save_govern_state(state)
            return result
        else:
            return {"status": "failed", "http_status": rw.status_code, "error": rw.text[:300],
                    "order_ref": order_ref, "marketplace_url": marketplace_url}

    # No consumerAccess ID found — return manual instructions with marketplace link
    result = {
        "order_id":        order_id,
        "order_ref":       order_ref,
        "status":          access.get("status", "UNKNOWN"),
        "marketplace_url": marketplace_url,
        "withdraw_action": "manual",
        "instructions":    "Open the Marketplace link → Consumers tab → find this order → click Withdraw.",
    }
    state["consumer_access"] = {**access, **result}
    _save_govern_state(state)
    return result


# ---------------------------------------------------------------------------
# CDMP CSV template generation helpers
# ---------------------------------------------------------------------------

def _cdmp_asset_context(state: dict) -> dict[str, str]:
    """Extract commonly needed values from pipeline state for CSV generation."""
    scan    = state.get("scan") or {}
    tables  = scan.get("tables", [])
    tbl     = tables[0] if tables else {}

    asset_info  = state.get("cdmp_data_asset") or {}
    asset_name  = asset_info.get("name") or tbl.get("name", "Governed Dataset")
    description = asset_info.get("description", "")

    connection  = tbl.get("connection", "") or tbl.get("schema", "")
    schema      = tbl.get("schema", "")
    table_name  = tbl.get("name", "")
    source_path = f"{schema}/{table_name}" if schema and table_name else table_name

    return {
        "asset_name":  asset_name,
        "description": description,
        "connection":  connection,
        "schema":      schema,
        "table_name":  table_name,
        "source_path": source_path,
    }


def _generate_data_asset_csv(state: dict) -> str:
    """Generate an Informatica CDMP Data Asset CSV template string."""
    ctx = _cdmp_asset_context(state)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='\n')
    writer.writerow([
        "Reference ID", "Name", "Description", "Data Source", "Description Source",
        "Type", "Source Path", "Source Path Description", "Technical Data Asset",
        "URI", "Status", "Linked Data Collections",
    ])
    writer.writerow([
        ctx["asset_name"],          # Reference ID
        ctx["asset_name"],          # Name
        ctx["description"],         # Description
        ctx["connection"],          # Data Source
        "",                         # Description Source
        "Dataset",                  # Type
        ctx["source_path"],         # Source Path
        "",                         # Source Path Description
        ctx["table_name"],          # Technical Data Asset
        "",                         # URI
        "Active",                   # Status
        "",                         # Linked Data Collections
    ])
    return buf.getvalue()


def _generate_data_element_csv(state: dict) -> str:
    """Generate an Informatica CDMP Data Element CSV template string (one row per column)."""
    ctx = _cdmp_asset_context(state)
    asset_name = ctx["asset_name"]

    scan   = state.get("scan") or {}
    tables = scan.get("tables", [])
    columns: list[dict] = []
    for tbl in tables:
        columns.extend(tbl.get("columns", []))

    # Build col -> BT definition map
    col_to_bt: dict[str, str] = {}
    domains = (state.get("taxonomy") or {}).get("domains", [])
    for domain in domains:
        for subdomain in domain.get("subdomains", []):
            for bt in subdomain.get("business_terms", []):
                for src_col in bt.get("source_columns", []):
                    col_key = src_col.split(".")[-1].upper()
                    col_to_bt[col_key] = bt.get("definition", bt.get("name", ""))

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='\n')
    writer.writerow([
        "Reference ID", "Name", "Description", "Data Asset Name",
        "URI", "Technical Type", "Technical Name", "Type", "Status",
    ])
    for col in columns:
        col_name  = col.get("name", "")
        data_type = col.get("data_type", "VARCHAR")
        bt_def    = col_to_bt.get(col_name.upper(), "")
        desc      = bt_def if bt_def else f"{col_name} ({data_type})"
        writer.writerow([
            f"{asset_name}_{col_name}",  # Reference ID
            col_name,                    # Name
            desc,                        # Description
            asset_name,                  # Data Asset Name
            "",                          # URI
            data_type,                   # Technical Type
            col_name,                    # Technical Name
            "Column",                    # Type
            "Active",                    # Status
        ])
    return buf.getvalue()


def _generate_asset_collection_link_csv(state: dict) -> str:
    """Generate an Informatica CDMP Data Asset - Data Collection CSV template string."""
    ctx = _cdmp_asset_context(state)
    asset_name      = ctx["asset_name"]
    collection_name = f"{asset_name} — Governed Dataset"

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='\n')
    writer.writerow(["Data Asset Name", "Data Collection Name"])
    writer.writerow([asset_name, collection_name])
    return buf.getvalue()


def _generate_delivery_template_csv(state: dict) -> str:
    """Generate an Informatica CDMP Delivery Template CSV template string."""
    ctx = _cdmp_asset_context(state)
    asset_name  = ctx["asset_name"]
    connection  = ctx["connection"]
    source_path = ctx["source_path"]

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='\n')
    writer.writerow([
        "Reference ID", "Name", "Description", "Status", "Delivery Type",
        "Delivery Process", "System", "Location", "Delivery Formats",
        "Delivery Methods", "Delivery Template Owners", "Color",
    ])
    writer.writerow([
        f"{asset_name}-delivery",                    # Reference ID
        f"{asset_name} — Download Delivery",         # Name
        f"Download delivery template for {asset_name}",  # Description
        "Active",                                    # Status
        "Automated",                                 # Delivery Type
        "",                                          # Delivery Process
        connection,                                  # System
        source_path,                                 # Location
        "CSV",                                       # Delivery Formats
        "Download",                                  # Delivery Methods
        "",                                          # Delivery Template Owners
        "",                                          # Color
    ])
    return buf.getvalue()


def _generate_terms_of_use_csv(state: dict) -> str:
    """Generate an Informatica CDMP Terms of Use CSV template string."""
    ctx = _cdmp_asset_context(state)
    asset_name = ctx["asset_name"]

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='\n')
    writer.writerow([
        "Reference ID", "Name", "Description", "Status", "Type", "URI",
    ])
    writer.writerow([
        f"{asset_name}-terms",              # Reference ID
        f"{asset_name} — Terms of Use",     # Name
        f"Usage terms for {asset_name}",    # Description
        "Active",                           # Status
        "Standard",                         # Type
        "",                                 # URI
    ])
    return buf.getvalue()


def _generate_tou_collection_link_csv(state: dict) -> str:
    """Generate an Informatica CDMP Terms of Use - Data Collection CSV template string."""
    ctx = _cdmp_asset_context(state)
    asset_name      = ctx["asset_name"]
    collection_name = f"{asset_name} — Governed Dataset"
    tou_name        = f"{asset_name} — Terms of Use"

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='\n')
    writer.writerow(["Terms Of Use Name", "Data Collection Name"])
    writer.writerow([tou_name, collection_name])
    return buf.getvalue()


def _generate_delivery_target_csv(state: dict) -> str:
    """Generate an Informatica CDMP Delivery Target - Data Collection CSV template string."""
    ctx = _cdmp_asset_context(state)
    asset_name      = ctx["asset_name"]
    connection      = ctx["connection"]
    source_path     = ctx["source_path"]
    collection_name = f"{asset_name} — Governed Dataset"
    delivery_name   = f"{asset_name} — Download Delivery"
    target_name     = f"{asset_name} — Download Target"

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='\n')
    writer.writerow([
        "Data Collection Name", "Delivery Template", "Delivery Target Name",
        "Description", "Status", "System", "Location",
        "Delivery Format", "Delivery Method",
    ])
    writer.writerow([
        collection_name,                        # Data Collection Name
        delivery_name,                          # Delivery Template
        target_name,                            # Delivery Target Name
        f"Download target for {asset_name}",    # Description
        "Active",                               # Status
        connection,                             # System
        source_path,                            # Location
        "CSV",                                  # Delivery Format
        "Download",                             # Delivery Method
    ])
    return buf.getvalue()


_CDMP_TEMPLATE_DIR = Path(__file__).parent / ".scan_cache" / "cdmp_templates"


def _cdmp_save_template_locally(csv_content: str, filename: str) -> str:
    """Save a CSV template to .scan_cache/cdmp_templates/ and return the local path."""
    _CDMP_TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _CDMP_TEMPLATE_DIR / filename
    path.write_text(csv_content, encoding="utf-8")
    return str(path)


def _cdmp_upload_template(csv_content: str, filename: str) -> dict:
    """Upload a CSV template to CDMP via bulk import, with local-save fallback.

    Tries three API endpoints in order:
      1. POST api/v2/import
      2. POST api/v2/bulk-import
      3. POST api/v1/import

    Always saves the CSV locally to .scan_cache/cdmp_templates/ so it can be
    manually imported via the CDMP UI if all API endpoints return 404.

    Returns {"status": "uploaded"|"saved_locally", "http_status": ..., "filename": ..., "local_path": ...}
    """
    local_path = _cdmp_save_template_locally(csv_content, filename)

    endpoints = ["api/v2/import", "api/v2/bulk-import", "api/v1/import"]
    headers = _cdgc_headers()
    headers.pop("Content-Type", None)

    last_status = 0
    last_error  = ""
    for endpoint in endpoints:
        url = f"{CDMP_API_BASE}/{endpoint}"
        try:
            r = httpx.request(
                "POST",
                url,
                headers=headers,
                files={"file": (filename, csv_content.encode("utf-8"), "text/csv")},
                timeout=60,
            )
            if r.status_code in (200, 201, 202):
                log.info("_cdmp_upload_template: %s → HTTP %s", endpoint, r.status_code)
                return {"status": "uploaded", "http_status": r.status_code, "filename": filename, "endpoint": endpoint, "local_path": local_path}
            last_status = r.status_code
            last_error  = r.text[:400]
            log.info("_cdmp_upload_template: %s → HTTP %s (trying next)", endpoint, r.status_code)
        except Exception as exc:
            last_error = str(exc)[:400]
            log.warning("_cdmp_upload_template: %s raised %s", endpoint, exc)

    log.info("_cdmp_upload_template: all endpoints failed — saved locally at %s", local_path)
    return {"status": "saved_locally", "http_status": last_status, "local_path": local_path, "filename": filename, "note": "Upload via CDMP UI: Administration → Import → choose this file"}


@mcp.tool()
def link_asset_to_collection(
    asset_name: str | None = None,
) -> dict[str, Any]:
    """Step 12b — Upload a Data Asset - Data Collection link CSV to Data Marketplace.

    Generates the Informatica CDMP 'Data Asset - Data Collection' template CSV
    and uploads it via bulk import, establishing the relationship between the
    governed asset and the Data Collection created in Step 12.

    Args:
      asset_name: Asset name override. Defaults to the name from pipeline state.

    Returns: {asset_name, collection_name, csv_upload}
    """
    state = _load_govern_state()

    collection_id = (state.get("cdmp_collection") or {}).get("id", "")
    if not collection_id:
        return {"status": "failed", "error": "No collection_id — run create_cdmp_data_collection first."}

    ctx = _cdmp_asset_context(state)
    if asset_name:
        # Override asset name in context for CSV generation
        asset_info = dict(state.get("cdmp_data_asset") or {})
        asset_info["name"] = asset_name
        state = dict(state)
        state["cdmp_data_asset"] = asset_info

    ctx   = _cdmp_asset_context(state)
    aname = ctx["asset_name"]
    cname = f"{aname} — Governed Dataset"

    link_csv   = _generate_asset_collection_link_csv(state)
    csv_upload = _cdmp_upload_template(link_csv, f"{aname}_asset_collection_link.csv")

    log.info("link_asset_to_collection: asset=%s collection=%s upload=%s", aname, cname, csv_upload.get("status"))
    return {
        "asset_name":      aname,
        "collection_name": cname,
        "collection_id":   collection_id,
        "csv_upload":      csv_upload,
        "status":          csv_upload.get("status", "failed"),
    }


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------
def main() -> None:
    host = os.getenv("AI_GOVERNANCE_MCP_HOST", "127.0.0.1")
    port = int(os.getenv("AI_GOVERNANCE_MCP_PORT", "8770"))
    log.info("Starting ai-governance MCP server on %s:%s", host, port)

    try:
        mcp.settings.host = host
        mcp.settings.port = port
    except Exception:
        log.warning("could not set mcp.settings.host/port — using defaults")

    use_stdio = "--stdio" in sys.argv[1:]
    transport = "stdio" if use_stdio else "streamable-http"
    try:
        mcp.run(transport=transport)
    except KeyboardInterrupt:
        log.info("shutting down")
        sys.exit(0)


if __name__ == "__main__":
    main()
