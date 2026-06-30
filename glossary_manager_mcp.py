"""glossary_manager_mcp.py — MCP server for IDMC CDGC business glossary automation.

Tools:
  - suggest_terms_for_asset : analyze an asset's columns and suggest glossary terms
  - create_glossary_term    : POST a BusinessTerm to CDGC via /data360/content/v1/assets
  - detect_glossary_issues  : scan glossary for duplicates, orphans, and definition gaps

Transport: streamable HTTP. Default bind: 127.0.0.1:8767 (override via
GLOSSARY_MCP_HOST / GLOSSARY_MCP_PORT).

Auth: reads IDMC_USER, IDMC_PASS, IDMC_LOGIN_HOST from .env; mints v2 sessions
on demand and persists IDMC_SESSION_ID / IDMC_SERVER_URL back to .env.
Sessions auto-refresh on HTTP 401. CDGC requests carry IDS-SESSION-ID +
X-INFA-ORG-ID headers.

Run locally:
    python glossary_manager_mcp.py
Then point .vscode/mcp.json at http://127.0.0.1:8767/mcp.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from collections import defaultdict
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

# CDGC content + search APIs. Published docs use `idmc-api.dm-us` but this
# tenant exposes `cdgc-api.dmp-us`. Override via env if needed.
CDGC_API_BASE = os.getenv("CDGC_API_BASE", "https://cdgc-api.dmp-us.informaticacloud.com")

# CDGC Metadata Search MCP — used by suggest_terms_for_asset to enumerate
# columns of an asset. Falls back to the search API if unset.
CDGC_SEARCH_MCP = os.getenv(
    "CDGC_SEARCH_MCP",
    "https://a2e-preview-c360-usw1-mcp.dmp-us.informaticacloud.com/mcp-servers/public/cdgcsearchmetadata",
)

DEFAULT_ORG_ID = os.getenv("IDMC_ORG_ID")

BUSINESS_TERM_CLASS = "com.infa.ccgf.models.governance.BusinessTerm"

# JWT mint endpoint. CDGC calls use Authorization: Bearer <jwt> instead of
# the raw IDS-SESSION-ID. The token is fetched by trading an active session
# at /identity-service/api/v1/jwt/Token. Token lifetime is ~30 minutes; we
# cache for 29 to leave a refresh margin.
JWT_TOKEN_URL = os.getenv(
    "IDMC_JWT_TOKEN_URL",
    "https://dmp-us.informaticacloud.com/identity-service/api/v1/jwt/Token?client_id=idmc_api",
)
JWT_TTL_SECONDS = int(os.getenv("IDMC_JWT_TTL_SECONDS", str(29 * 60)))

log = logging.getLogger("glossary_manager")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_env_lock = threading.Lock()
_jwt_lock = threading.Lock()
_jwt_cache: dict[str, float | str] = {"token": "", "expires_at": 0.0}


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


def _login_v2() -> tuple[str, str]:
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


def _mint_jwt() -> str:
    """Trade the current IDS session for a Bearer JWT.

    Endpoint: GET /identity-service/api/v1/jwt/Token?client_id=idmc_api
    Headers:  IDS-SESSION-ID: <v2 session>

    Response may be either the raw token as text or JSON like
    {"jwt_token": "..."} / {"token": "..."} / {"access_token": "..."}.
    Refreshes the IDS session once on 401.
    """
    # IDMC's /jwt/Token requires a per-request nonce (OIDC). Without it,
    # the endpoint returns IDS_345 "A nonce is required to get a JWT."
    import uuid as _uuid
    def _attempt(sid: str) -> httpx.Response:
        sep = "&" if "?" in JWT_TOKEN_URL else "?"
        url = f"{JWT_TOKEN_URL}{sep}nonce={_uuid.uuid4().hex.upper()}"
        return httpx.get(
            url,
            headers={"IDS-SESSION-ID": sid, "Accept": "application/json"},
            timeout=30,
        )

    r = _attempt(_current_session())
    if r.status_code == 401:
        log.info("JWT mint HTTP 401 — refreshing IDS session and retrying")
        sid, _ = _login_v2()
        r = _attempt(sid)
    if r.status_code != 200:
        raise RuntimeError(f"mint JWT HTTP {r.status_code}: {r.text[:300]}")

    body = r.text.strip()
    token = ""
    if body.startswith("{"):
        try:
            j = r.json()
            for k in ("jwt_token", "token", "access_token", "jwtToken"):
                if isinstance(j.get(k), str) and j[k]:
                    token = j[k]
                    break
        except ValueError:
            pass
    if not token:
        # Treat the raw body as the token, stripping optional surrounding quotes.
        token = body.strip('"').strip()
    if not token:
        raise RuntimeError(f"mint JWT: empty token in response: {r.text[:300]}")
    log.info("minted fresh CDGC JWT (%s…)", token[:12])
    return token


def _current_jwt(force_refresh: bool = False) -> str:
    """Return a cached JWT, minting a new one if expired or force_refresh=True."""
    with _jwt_lock:
        now = time.time()
        token = _jwt_cache.get("token") or ""
        expires_at = float(_jwt_cache.get("expires_at") or 0)
        if not force_refresh and token and now < expires_at:
            return str(token)
        new_token = _mint_jwt()
        _jwt_cache["token"] = new_token
        _jwt_cache["expires_at"] = now + JWT_TTL_SECONDS
        return new_token


def _request_cdgc(method: str, url: str, **kw) -> httpx.Response:
    """CDGC API call using Authorization: Bearer <jwt> + X-INFA-ORG-ID.

    On 401, force-refresh the JWT (re-minting from the IDS session, refreshing
    that too if needed) and retry once.
    """
    headers = dict(kw.pop("headers", {}) or {})
    headers["Authorization"] = f"Bearer {_current_jwt()}"
    headers["X-INFA-ORG-ID"] = DEFAULT_ORG_ID
    headers.setdefault("Accept", "application/json")
    if "json" in kw or "data" in kw:
        headers.setdefault("Content-Type", "application/json")

    r = httpx.request(method, url, headers=headers, timeout=60, **kw)
    if r.status_code == 401:
        log.info("CDGC HTTP 401 — refreshing JWT and retrying")
        headers["Authorization"] = f"Bearer {_current_jwt(force_refresh=True)}"
        r = httpx.request(method, url, headers=headers, timeout=60, **kw)
    return r


# ---------------------------------------------------------------------------
# Column-name → glossary-term heuristics
# ---------------------------------------------------------------------------
_COMMON_SUFFIXES = ("_id", "_dt", "_ts", "_cd", "_amt", "_qty", "_num", "_no", "_pct", "_flag", "_ind")
_STOPWORDS = {"id", "key", "code", "cd", "no", "num", "type", "ind", "flag"}


def _humanize(column: str) -> str:
    """Turn snake_case / camelCase / PascalCase column names into Title Case."""
    s = re.sub(r"[_\-\.]+", " ", column).strip()
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.title() if s else column


def _suggest_term_from_column(column: str, domain_context: str | None) -> dict[str, Any]:
    """Heuristic: derive a candidate business term from a column name."""
    base = column
    suffix = ""
    for sfx in _COMMON_SUFFIXES:
        if column.lower().endswith(sfx):
            base = column[: -len(sfx)]
            suffix = sfx
            break

    name = _humanize(base)
    tokens = [t for t in re.split(r"\s+", name) if t]
    meaningful = [t for t in tokens if t.lower() not in _STOPWORDS]
    if meaningful:
        name = " ".join(meaningful)

    role = {
        "_id":   "unique identifier",
        "_dt":   "date",
        "_ts":   "timestamp",
        "_cd":   "code value",
        "_amt":  "amount",
        "_qty":  "quantity",
        "_num":  "numeric value",
        "_no":   "number",
        "_pct":  "percentage",
        "_flag": "boolean flag",
        "_ind":  "indicator",
    }.get(suffix, "")
    role_phrase = f" — typically a {role}" if role else ""
    domain_phrase = f" in the {domain_context} domain" if domain_context else ""

    return {
        "suggested_term": name,
        "source_column":  column,
        "definition":     f"{name}{role_phrase}{domain_phrase}.",
        "synonyms":       sorted({column, name}) if column != name else [name],
        "confidence":     "high" if meaningful and len(name) >= 3 else "low",
    }


# ---------------------------------------------------------------------------
# CDGC search helpers
# ---------------------------------------------------------------------------
def _search_assets(
    knowledge_query: str = "*",
    class_type: str | None = None,
    size: int = 50,
    segments: str = "all",
    extra_filters: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """POST /data360/search/v1/assets and return the hits array."""
    url = (
        f"{CDGC_API_BASE}/data360/search/v1/assets"
        f"?knowledgeQuery={quote(knowledge_query, safe='')}&segments={quote(segments, safe=':,')}"
    )
    filters: list[dict[str, Any]] = []
    if class_type:
        filters.append({"type": "simple", "attribute": "core.classType", "values": [class_type]})
    if extra_filters:
        filters.extend(extra_filters)
    body: dict[str, Any] = {"from": 0, "size": int(size)}
    if filters:
        body["filterSpec"] = filters

    r = _request_cdgc("POST", url, json=body)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"search assets HTTP {r.status_code}: {r.text[:400]}")
    j = r.json() or {}
    # Different API revs use `hits` or `value` for the list.
    hits = j.get("hits") or j.get("value") or j.get("results") or []
    if isinstance(hits, dict):
        hits = hits.get("hits") or hits.get("value") or []
    return hits


def _get_asset_details(asset_id: str, scheme: str = "internal", segments: str = "all") -> dict[str, Any]:
    """GET /data360/search/v1/assets/<id>?scheme=...&segments=..."""
    url = (
        f"{CDGC_API_BASE}/data360/search/v1/assets/{asset_id}"
        f"?scheme={scheme}&segments={segments}"
    )
    r = _request_cdgc("GET", url)
    if r.status_code != 200:
        raise RuntimeError(f"get asset HTTP {r.status_code}: {r.text[:400]}")
    return r.json() or {}


def _extract_columns(asset: dict[str, Any]) -> list[str]:
    """Pull child-column names from an asset details response.

    CDGC exposes child columns either via `hierarchy.hits[*].summary.core.name`
    or `descendants[*].summary.core.name` depending on the segments requested.
    """
    names: list[str] = []
    for key in ("hierarchy", "descendants"):
        node = asset.get(key)
        if not node:
            continue
        items = node.get("hits") if isinstance(node, dict) else node
        if not isinstance(items, list):
            continue
        for it in items:
            n = ((it.get("summary") or {}).get("core.name")) or it.get("core.name") or it.get("name")
            if n:
                names.append(str(n))
    return names


def _name_of(hit: dict[str, Any]) -> str:
    return str(
        ((hit.get("summary") or {}).get("core.name"))
        or hit.get("core.name")
        or hit.get("name")
        or ""
    )


def _description_of(hit: dict[str, Any]) -> str:
    return str(
        ((hit.get("summary") or {}).get("core.description"))
        or hit.get("core.description")
        or hit.get("description")
        or ""
    )


def _id_of(hit: dict[str, Any]) -> str:
    sys_attrs = hit.get("systemAttributes") or {}
    return str(
        sys_attrs.get("core.identity")
        or hit.get("core.identity")
        or hit.get("id")
        or ""
    )


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="glossary_manager",
    instructions=(
        "IDMC CDGC business glossary automation. Use suggest_terms_for_asset "
        "to propose glossary terms from a technical asset's columns. Use "
        "create_glossary_term to materialise a BusinessTerm in CDGC. Use "
        "detect_glossary_issues to scan the glossary for duplicates, orphans, "
        "and definition gaps."
    ),
)


@mcp.tool()
def suggest_terms_for_asset(
    asset_name: str,
    domain_context: str | None = None,
) -> dict[str, Any]:
    """Suggest business glossary terms for a technical asset.

    Looks up the asset by name in CDGC, enumerates its columns, then derives
    candidate business terms from each column name using snake/camel-case
    splitting and common suffix heuristics (_id, _dt, _amt, etc).

    Args:
      asset_name: Name (or substring) of the technical asset (table/view/file).
      domain_context: Optional domain hint (e.g. "Finance", "Customer") used
                      to flavour the candidate definitions.

    Returns: {asset, columns, suggestions:[{suggested_term, source_column,
             definition, synonyms, confidence}]}.
    """
    hits = _search_assets(knowledge_query=asset_name, size=10, segments="summary,systemAttributes")
    table_like = [
        h for h in hits
        if "Table" in str((h.get("systemAttributes") or {}).get("core.classType") or "")
        or "View"  in str((h.get("systemAttributes") or {}).get("core.classType") or "")
        or "DataSet" in str((h.get("systemAttributes") or {}).get("core.classType") or "")
    ]
    target = table_like[0] if table_like else (hits[0] if hits else None)
    if not target:
        return {
            "asset": asset_name,
            "columns": [],
            "suggestions": [],
            "note": f"No assets found matching '{asset_name}'.",
        }

    asset_id = _id_of(target)
    asset_display = _name_of(target) or asset_name

    columns: list[str] = []
    if asset_id:
        try:
            details = _get_asset_details(asset_id, scheme="internal", segments="hierarchy:summary")
            columns = _extract_columns(details)
        except Exception as e:  # noqa: BLE001
            log.warning("failed to fetch hierarchy for %s: %s", asset_id, e)

    if not columns:
        # Fallback: pull data elements related to this asset via search.
        try:
            related = _search_assets(
                knowledge_query=f"data elements related to {asset_display}",
                size=50,
                segments="summary",
            )
            columns = [_name_of(h) for h in related if _name_of(h)]
        except Exception as e:  # noqa: BLE001
            log.warning("fallback search for columns failed: %s", e)

    seen: set[str] = set()
    unique_columns: list[str] = []
    for c in columns:
        if c not in seen:
            seen.add(c)
            unique_columns.append(c)

    suggestions = [_suggest_term_from_column(c, domain_context) for c in unique_columns]

    return {
        "asset":       asset_display,
        "asset_id":    asset_id,
        "domain":      domain_context,
        "columns":     unique_columns,
        "suggestions": suggestions,
    }


@mcp.tool()
def create_glossary_term(
    term_name: str,
    definition: str,
    category: str | None = None,
    synonyms: list[str] | None = None,
) -> dict[str, Any]:
    """Create a BusinessTerm in CDGC.

    Posts to /data360/content/v1/assets with core.classType set to
    com.infa.ccgf.models.governance.BusinessTerm.

    Args:
      term_name:  Display name of the new business term.
      definition: Business-facing description.
      category:   Optional parent category. Treated as the parent's external
                  ID (e.g. "Financial Metrics"). Internal IDs are supported by
                  prefixing the value with "id:".
      synonyms:   Optional list of alias names. Stored in
                  com.infa.ccgf.models.governance.AliasNames.

    Returns: {id, name, classType, http_status}.
    """
    body: dict[str, Any] = {
        "core.classType": BUSINESS_TERM_CLASS,
        "summary": {
            "core.name":        term_name,
            "core.description": definition,
        },
        "selfAttributes": {
            "com.infa.ccgf.models.governance.FormatType": "Text",
            "com.infa.ccgf.models.governance.isCDE":      False,
            "com.infa.ccgf.models.governance.AliasNames": list(synonyms or []),
        },
    }
    if category:
        if category.startswith("id:"):
            body["parent"] = {"core.identity": category[3:]}
        else:
            body["parent"] = {"core.externalId": category}

    url = f"{CDGC_API_BASE}/data360/content/v1/assets"
    r = _request_cdgc("POST", url, json=body)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"create_glossary_term HTTP {r.status_code}: {r.text[:500]}")

    j = r.json() if r.text else {}
    new_id = (
        j.get("core.identity")
        or j.get("id")
        or (j.get("summary") or {}).get("core.identity")
    )
    return {
        "id":          new_id,
        "name":        term_name,
        "classType":   BUSINESS_TERM_CLASS,
        "category":    category,
        "synonyms":    list(synonyms or []),
        "http_status": r.status_code,
    }


@mcp.tool()
def detect_glossary_issues(
    scan_scope: str = "all",
    sample_size: int = 200,
    min_definition_length: int = 20,
) -> dict[str, Any]:
    """Scan the business glossary for quality issues.

    Pulls BusinessTerm assets from CDGC and reports:
      - duplicates: terms whose names match case-insensitively (possible merges)
      - orphans:    terms with no incoming/outgoing relationships
      - gaps:       terms with missing or too-short definitions

    Args:
      scan_scope: One of "all", "duplicates", "orphans", "gaps".
      sample_size: Max terms to inspect (page size).
      min_definition_length: Threshold under which a definition is a "gap".

    Returns: {term_count, duplicates, orphans, gaps}. Sections not in scope
    are returned as empty lists.
    """
    wants = {"all", scan_scope}
    terms = _search_assets(
        knowledge_query="*",
        class_type=BUSINESS_TERM_CLASS,
        size=sample_size,
        segments="summary,systemAttributes,selfAttributes",
    )

    duplicates: list[dict[str, Any]] = []
    orphans:    list[dict[str, Any]] = []
    gaps:       list[dict[str, Any]] = []

    if "duplicates" in wants or "all" in wants:
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for t in terms:
            n = _name_of(t).strip().lower()
            if n:
                buckets[n].append({"id": _id_of(t), "name": _name_of(t)})
        duplicates = [
            {"name": entries[0]["name"], "count": len(entries), "ids": [e["id"] for e in entries]}
            for entries in buckets.values()
            if len(entries) > 1
        ]

    if "gaps" in wants or "all" in wants:
        for t in terms:
            desc = _description_of(t).strip()
            if len(desc) < int(min_definition_length):
                gaps.append({
                    "id":   _id_of(t),
                    "name": _name_of(t),
                    "definition_length": len(desc),
                    "reason": "missing definition" if not desc else "definition too short",
                })

    if "orphans" in wants or "all" in wants:
        # Check neighborhood for a sample of terms. Bulk-checking all is
        # expensive; we cap at the first 50 to keep latency reasonable.
        for t in terms[:50]:
            aid = _id_of(t)
            if not aid:
                continue
            try:
                detail = _get_asset_details(aid, scheme="internal", segments="neighborhood")
            except Exception as e:  # noqa: BLE001
                log.warning("neighborhood lookup failed for %s: %s", aid, e)
                continue
            neigh = detail.get("neighborhood") or {}
            items = neigh.get("hits") if isinstance(neigh, dict) else neigh
            if not items:
                orphans.append({"id": aid, "name": _name_of(t)})

    return {
        "scan_scope":  scan_scope,
        "term_count":  len(terms),
        "duplicates":  duplicates,
        "orphans":     orphans,
        "gaps":        gaps,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _configure_settings() -> None:
    host = os.getenv("GLOSSARY_MCP_HOST", "127.0.0.1")
    port = int(os.getenv("GLOSSARY_MCP_PORT", "8767"))
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
    log.info("starting glossary_manager MCP server on %s transport", transport)
    try:
        mcp.run(transport=transport)
    except KeyboardInterrupt:
        log.info("shutting down")
        sys.exit(0)
