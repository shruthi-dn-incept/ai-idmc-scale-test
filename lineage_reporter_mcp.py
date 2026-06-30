"""lineage_reporter_mcp.py — MCP server exposing IDMC CDGC lineage tools.

Tools:
  - trace_lineage          : upstream/downstream lineage for a named asset
  - generate_impact_report : downstream lineage + impact severity classification
  - find_data_source       : trace upstream to root source systems

Transport: streamable HTTP. Default bind: 127.0.0.1:8766 (override via
LINEAGE_MCP_HOST / LINEAGE_MCP_PORT).

Auth: identical pattern to governance_engine_mcp.py — reads IDMC_USER,
IDMC_PASS, IDMC_LOGIN_HOST from .env; mints v2 sessions on demand and
persists IDMC_SESSION_ID/IDMC_SERVER_URL back to .env. Sessions auto-refresh
on HTTP 401. CDGC calls send IDS-SESSION-ID + X-INFA-ORG-ID headers.

Run locally:
    python lineage_reporter_mcp.py
Then point .vscode/mcp.json at http://127.0.0.1:8766/mcp.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"

# CDGC API base. Same convention as governance_engine_mcp.py — this tenant
# lives on the dmp-us pod; override via env if pointing at another POD.
CDGC_API_BASE = os.getenv("CDGC_API_BASE", "https://cdgc-api.dmp-us.informaticacloud.com")

# Default IDMC org id (from v3 login userInfo.orgId). Override via env.
DEFAULT_ORG_ID = os.getenv("IDMC_ORG_ID")

# Impact severity thresholds (count of distinct downstream assets).
SEVERITY_LOW_MAX = int(os.getenv("LINEAGE_SEVERITY_LOW_MAX", "5"))
SEVERITY_MEDIUM_MAX = int(os.getenv("LINEAGE_SEVERITY_MEDIUM_MAX", "20"))

# Max hop depth we'll request in a single GET call.
DEFAULT_DEPTH = int(os.getenv("LINEAGE_DEFAULT_DEPTH", "5"))
MAX_DEPTH = int(os.getenv("LINEAGE_MAX_DEPTH", "20"))

log = logging.getLogger("lineage_reporter")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_env_lock = threading.Lock()
_jwt_lock = threading.Lock()
_jwt_cache: dict[str, Any] = {"token": None, "expires_at": 0.0}
# JWT TTL is 30 min per docs; refresh a minute early.
_JWT_TTL_SECONDS = 29 * 60


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
# Session minting (mirrors governance_engine_mcp.py)
# ---------------------------------------------------------------------------
def _login_v2() -> tuple[str, str]:
    env = _read_env()
    user = env.get("IDMC_USER")
    pw = env.get("IDMC_PASS")
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


def _mint_jwt(force: bool = False) -> str:
    """Mint a JWT access token for CDGC API calls.

    Exchanges the v2 session ID for a JWT at
    ``<login_host>/identity-service/api/v1/jwt/Token?client_id=idmc_api``.
    JWTs expire after 30 minutes, so we cache the result with a 29-minute TTL.
    """
    with _jwt_lock:
        now = time.time()
        if not force and _jwt_cache.get("token") and _jwt_cache.get("expires_at", 0) > now:
            return _jwt_cache["token"]
        host = _read_env().get("IDMC_LOGIN_HOST", "dmp-us.informaticacloud.com")
        sid = _current_session()
        # Per-request UUID nonce — IDMC doesn't currently enforce uniqueness,
        # but a fresh nonce avoids breakage if they tighten OIDC checks later.
        url = (f"https://{host}/identity-service/api/v1/jwt/Token"
               f"?client_id=idmc_api&nonce={uuid.uuid4().hex.upper()}")
        r = httpx.get(
            url,
            headers={"IDS-SESSION-ID": sid, "cookie": f"USER_SESSION={sid}", "Accept": "application/json"},
            timeout=30,
        )
        if r.status_code == 401:
            # session itself is stale — refresh and retry once.
            sid, _ = _login_v2()
            r = httpx.get(
                url,
                headers={"IDS-SESSION-ID": sid, "cookie": f"USER_SESSION={sid}", "Accept": "application/json"},
                timeout=30,
            )
        if r.status_code != 200:
            raise RuntimeError(f"JWT mint HTTP {r.status_code}: {r.text[:300]}")
        tok = (r.json() or {}).get("jwt_token")
        if not tok:
            raise RuntimeError(f"JWT mint response missing jwt_token: {r.text[:300]}")
        _jwt_cache["token"] = tok
        _jwt_cache["expires_at"] = now + _JWT_TTL_SECONDS
        log.info("minted fresh CDGC JWT (exp in %ds)", _JWT_TTL_SECONDS)
        return tok


def _request_cdgc(method: str, url: str, **kw) -> httpx.Response:
    """CDGC API call using Authorization: Bearer JWT + X-INFA-ORG-ID."""
    headers = dict(kw.pop("headers", {}) or {})
    headers["Authorization"] = f"Bearer {_mint_jwt()}"
    headers["X-INFA-ORG-ID"] = DEFAULT_ORG_ID
    headers.setdefault("Accept", "application/json")
    headers.setdefault("x-infa-show-association-label", "true")
    headers.setdefault("x-infa-show-custom-attribute-label", "true")
    if "json" in kw or "data" in kw:
        headers.setdefault("Content-Type", "application/json")

    r = httpx.request(method, url, headers=headers, timeout=60, **kw)
    if r.status_code == 401:
        log.info("CDGC HTTP 401 — refreshing JWT and retrying")
        headers["Authorization"] = f"Bearer {_mint_jwt(force=True)}"
        r = httpx.request(method, url, headers=headers, timeout=60, **kw)
    return r


# ---------------------------------------------------------------------------
# CDGC search + lineage helpers
# ---------------------------------------------------------------------------
def _search_assets(name_query: str, size: int = 25) -> list[dict[str, Any]]:
    """POST /data360/search/v1/assets — returns the raw hit list.

    The CDGC endpoint requires ``knowledgeQuery`` as a URL parameter and
    returns ``{summary, hits: [...]}``. Each hit carries ``core.identity``,
    ``core.externalId``, plus the requested segments inline (summary,
    systemAttributes).
    """
    from urllib.parse import quote
    url = (
        f"{CDGC_API_BASE}/data360/search/v1/assets"
        f"?knowledgeQuery={quote(name_query)}&segments=summary,systemAttributes"
    )
    body = {"from": 0, "size": max(1, min(int(size), 100))}
    r = _request_cdgc("POST", url, json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"search assets HTTP {r.status_code}: {r.text[:400]}")
    j = r.json() if r.text else {}
    hits = j.get("hits") or []
    if isinstance(hits, dict):
        hits = hits.get("hits") or []
    return hits


def _resolve_asset(asset_name: str) -> dict[str, Any]:
    """Find the best-matching asset for a free-text name.

    Returns the first hit that matches case-insensitively on core.name; falls
    back to the top hit if no exact match. Raises if nothing comes back.
    """
    hits = _search_assets(asset_name, size=25)
    if not hits:
        raise RuntimeError(f"No CDGC asset found for name: {asset_name!r}")

    name_lc = asset_name.lower()

    def name_of(h: dict[str, Any]) -> str:
        return (
            (h.get("summary") or {}).get("core.name")
            or h.get("core.name")
            or h.get("name")
            or ""
        )

    def id_of(h: dict[str, Any]) -> str | None:
        return (
            h.get("core.identity")
            or h.get("id")
            or (h.get("summary") or {}).get("core.identity")
        )

    exact = [h for h in hits if name_of(h).lower() == name_lc]
    chosen = (exact or hits)[0]
    asset_id = id_of(chosen)
    if not asset_id:
        raise RuntimeError(f"search hit for {asset_name!r} missing core.identity: {chosen}")
    return {
        "id": asset_id,
        "name": name_of(chosen),
        "classType": (
            (chosen.get("systemAttributes") or {}).get("core.classType")
            or chosen.get("core.classType")
        ),
        "externalId": chosen.get("core.externalId") or (chosen.get("summary") or {}).get("core.externalId"),
        "location": (chosen.get("summary") or {}).get("core.location"),
        "raw": chosen,
    }


def _get_asset_lineage(
    asset_id: str,
    direction: str = "all",
    depth: int = DEFAULT_DEPTH,
    level: str = "dataset",
) -> dict[str, Any]:
    """GET /data360/search/v1/assets/{id} with lineage segments.

    direction: 'inbound' (upstream), 'outbound' (downstream), or 'all'.
    level:     'dataset' (table-level) or 'dataelement' (column-level).
    depth:     hop limit (lineage-distance).
    """
    dir_norm = (direction or "all").lower()
    if dir_norm in {"upstream", "up", "in"}:
        dir_norm = "inbound"
    elif dir_norm in {"downstream", "down", "out"}:
        dir_norm = "outbound"
    elif dir_norm in {"both", "all", "*"}:
        dir_norm = "all"
    if dir_norm not in {"inbound", "outbound", "all"}:
        raise ValueError(f"direction must be inbound/outbound/all (got {direction!r})")

    depth = max(1, min(int(depth or DEFAULT_DEPTH), MAX_DEPTH))
    level_norm = (level or "dataset").lower()
    if level_norm not in {"dataset", "dataelement"}:
        level_norm = "dataset"

    segments = ",".join([
        "summary",
        "systemAttributes",
        f"lineage-direction:{dir_norm}",
        f"lineage-level:{level_norm}",
        f"lineage-distance:{depth}",
    ])
    url = (
        f"{CDGC_API_BASE}/data360/search/v1/assets/{asset_id}"
        f"?scheme=internal&segments={segments}"
    )
    r = _request_cdgc("GET", url)
    if r.status_code >= 400:
        raise RuntimeError(f"get asset lineage HTTP {r.status_code}: {r.text[:400]}")
    return r.json() if r.text else {}


def _flatten_lineage_hops(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk the `lineage` array from get_asset_details into flat hop edges.

    Each output entry: {direction, distance, from, fromId, fromType, fromLocation,
    to, toId, toType, toLocation, associationKind}.
    """
    edges: list[dict[str, Any]] = []
    for branch in detail.get("lineage") or []:
        direction = branch.get("direction") or "unknown"
        for hop in branch.get("hops") or []:
            distance = hop.get("distance")
            for item in hop.get("items") or []:
                attrs = item.get("attributes") or {}
                fp = item.get("fromProperties") or {}
                tp = item.get("toProperties") or {}
                edges.append({
                    "direction": direction,
                    "distance": distance,
                    "from": item.get("from") or fp.get("core.name"),
                    "fromId": fp.get("core.identity"),
                    "fromType": item.get("fromType") or fp.get("core.classType"),
                    "fromLocation": item.get("fromLocation") or fp.get("core.location"),
                    "to": item.get("to") or tp.get("core.name"),
                    "toId": tp.get("core.identity"),
                    "toType": item.get("toType") or tp.get("core.classType"),
                    "toLocation": item.get("toLocation") or tp.get("core.location"),
                    "associationKind": attrs.get("core.associationKind") or attrs.get("__meta.association"),
                })
    return edges


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="lineage_reporter",
    instructions=(
        "IDMC CDGC lineage tools. Use trace_lineage to walk upstream or "
        "downstream relationships for a named asset. Use generate_impact_report "
        "to summarize downstream blast radius with severity. Use "
        "find_data_source to walk upstream to root source systems."
    ),
)


@mcp.tool()
def trace_lineage(
    asset_name: str,
    direction: str = "all",
    depth: int = DEFAULT_DEPTH,
    level: str = "dataset",
) -> dict[str, Any]:
    """Trace lineage for an asset by name.

    Args:
      asset_name: Free-text asset name to look up in CDGC.
      direction: 'upstream'/'inbound', 'downstream'/'outbound', or 'all'.
      depth: Max hop distance to traverse (1..MAX_DEPTH).
      level: 'dataset' (table/file level) or 'dataelement' (column level).

    Returns: {asset:{id,name,classType,...}, direction, depth, edges:[...],
              edge_count, distinct_nodes}.
    """
    asset = _resolve_asset(asset_name)
    detail = _get_asset_lineage(asset["id"], direction=direction, depth=depth, level=level)
    edges = _flatten_lineage_hops(detail)

    node_ids: set[str] = set()
    for e in edges:
        if e.get("fromId"):
            node_ids.add(e["fromId"])
        if e.get("toId"):
            node_ids.add(e["toId"])
    node_ids.discard(asset["id"])

    return {
        "asset": {
            "id": asset["id"],
            "name": asset["name"],
            "classType": asset["classType"],
            "externalId": asset["externalId"],
            "location": asset["location"],
        },
        "direction": direction,
        "depth": depth,
        "level": level,
        "edge_count": len(edges),
        "distinct_nodes": len(node_ids),
        "edges": edges,
    }


def _classify_severity(distinct_nodes: int, edges: list[dict[str, Any]]) -> str:
    """Bucket impact severity from downstream blast radius.

    LOW    < SEVERITY_LOW_MAX nodes
    MEDIUM < SEVERITY_MEDIUM_MAX nodes
    HIGH   otherwise, or any time a BI/report/dashboard asset is downstream.
    """
    bi_signals = {"report", "dashboard", "businessterm", "metric", "kpi"}
    has_bi = any(
        any(sig in (e.get("toType") or "").lower() for sig in bi_signals)
        for e in edges
    )
    if has_bi and distinct_nodes >= SEVERITY_LOW_MAX:
        return "HIGH"
    if distinct_nodes < SEVERITY_LOW_MAX:
        return "LOW"
    if distinct_nodes < SEVERITY_MEDIUM_MAX:
        return "MEDIUM"
    return "HIGH"


@mcp.tool()
def generate_impact_report(
    asset_name: str,
    change_description: str,
    depth: int = DEFAULT_DEPTH,
    level: str = "dataset",
) -> dict[str, Any]:
    """Trace downstream lineage from an asset and classify impact severity.

    Severity buckets (configurable via env): LOW (<5 nodes), MEDIUM (<20),
    HIGH (>=20 or any BI/report/glossary asset downstream).

    Args:
      asset_name: Free-text asset name to look up in CDGC.
      change_description: Free-text description of the proposed change (echoed
        into the report; not interpreted).
      depth: Max hop distance to traverse downstream (1..MAX_DEPTH).
      level: 'dataset' (table/file level) or 'dataelement' (column level).

    Returns: {asset, change_description, severity, distinct_downstream_assets,
              affected_by_type:{...}, top_affected:[...], edges:[...]}.
    """
    asset = _resolve_asset(asset_name)
    detail = _get_asset_lineage(asset["id"], direction="outbound", depth=depth, level=level)
    edges = _flatten_lineage_hops(detail)

    affected: dict[str, dict[str, Any]] = {}
    by_type: dict[str, int] = {}
    for e in edges:
        tid = e.get("toId")
        if not tid or tid == asset["id"]:
            continue
        if tid not in affected:
            affected[tid] = {
                "id": tid,
                "name": e.get("to"),
                "classType": e.get("toType"),
                "location": e.get("toLocation"),
                "min_distance": e.get("distance"),
            }
        else:
            d = e.get("distance")
            cur = affected[tid].get("min_distance")
            if d is not None and (cur is None or d < cur):
                affected[tid]["min_distance"] = d
        t = e.get("toType") or "unknown"
        by_type[t] = by_type.get(t, 0) + 1

    distinct = len(affected)
    severity = _classify_severity(distinct, edges)
    top_affected = sorted(
        affected.values(),
        key=lambda a: (a.get("min_distance") if a.get("min_distance") is not None else 999, a.get("name") or ""),
    )[:20]

    return {
        "asset": {
            "id": asset["id"],
            "name": asset["name"],
            "classType": asset["classType"],
            "externalId": asset["externalId"],
            "location": asset["location"],
        },
        "change_description": change_description,
        "severity": severity,
        "distinct_downstream_assets": distinct,
        "edge_count": len(edges),
        "affected_by_type": by_type,
        "top_affected": top_affected,
        "edges": edges,
    }


def _origin_from_location(loc: str | None) -> str | None:
    """Pull the source-system origin from a CDGC core.location string.

    Locations look like:
      <originId>://<originId>/<SOURCE_NAME>/<schema>/<table>/<column>
    The 3rd path segment is usually the resource/source name registered in
    Metadata Command Center. Returns it, or None if the format is unfamiliar.
    """
    if not loc or "://" not in loc:
        return None
    try:
        _, rest = loc.split("://", 1)
        parts = [p for p in rest.split("/") if p]
        # parts[0] is the origin uuid; parts[1] is typically the source/resource name
        if len(parts) >= 2:
            return parts[1]
    except Exception:  # noqa: BLE001
        return None
    return None


@mcp.tool()
def find_data_source(
    asset_name: str,
    depth: int = DEFAULT_DEPTH,
    level: str = "dataset",
) -> dict[str, Any]:
    """Walk upstream lineage to find the root source system(s) for an asset.

    A "root" is any upstream node that has no further upstream edge in the
    returned lineage. Source-system labels are extracted from the CDGC
    `core.location` of each root.

    Args:
      asset_name: Free-text asset name to look up in CDGC.
      depth: Max hop distance to traverse upstream (1..MAX_DEPTH).
      level: 'dataset' (table/file level) or 'dataelement' (column level).

    Returns: {asset, depth, root_sources:[{id,name,classType,location,
              source_system,max_distance}], source_systems:[...], edges:[...]}.
    """
    asset = _resolve_asset(asset_name)
    detail = _get_asset_lineage(asset["id"], direction="inbound", depth=depth, level=level)
    edges = _flatten_lineage_hops(detail)

    # An upstream edge flows from -> to where `to` is closer to the searched
    # asset. Roots are nodes that appear as `from` but never as `to` in the
    # inbound graph.
    appears_as_to: set[str] = set()
    nodes: dict[str, dict[str, Any]] = {}
    for e in edges:
        fid, tid = e.get("fromId"), e.get("toId")
        if tid:
            appears_as_to.add(tid)
        if fid and fid not in nodes:
            nodes[fid] = {
                "id": fid,
                "name": e.get("from"),
                "classType": e.get("fromType"),
                "location": e.get("fromLocation"),
                "max_distance": e.get("distance"),
            }
        elif fid:
            d = e.get("distance")
            cur = nodes[fid].get("max_distance")
            if d is not None and (cur is None or d > cur):
                nodes[fid]["max_distance"] = d

    roots = [n for nid, n in nodes.items() if nid not in appears_as_to]
    for r in roots:
        r["source_system"] = _origin_from_location(r.get("location"))

    source_systems = sorted({
        r["source_system"] for r in roots if r.get("source_system")
    })

    return {
        "asset": {
            "id": asset["id"],
            "name": asset["name"],
            "classType": asset["classType"],
            "externalId": asset["externalId"],
            "location": asset["location"],
        },
        "depth": depth,
        "level": level,
        "root_count": len(roots),
        "root_sources": sorted(
            roots,
            key=lambda r: (-(r.get("max_distance") or 0), r.get("name") or ""),
        ),
        "source_systems": source_systems,
        "edge_count": len(edges),
        "edges": edges,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _configure_settings() -> None:
    host = os.getenv("LINEAGE_MCP_HOST", "127.0.0.1")
    port = int(os.getenv("LINEAGE_MCP_PORT", "8766"))
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
    log.info("starting lineage_reporter MCP server on %s transport", transport)
    try:
        mcp.run(transport=transport)
    except KeyboardInterrupt:
        log.info("shutting down")
        sys.exit(0)
