"""dq_monitor_mcp.py — MCP server for IDMC CDGC data quality monitoring.

Tools:
  - get_dq_scores         : current DQ scorecard for a named asset
  - check_score_trends    : historical scores + degradation detection
  - recommend_remediation : structured failure summary for LLM-side reasoning
  - alert_on_degradation  : register a local monitoring config (no IDMC API
                            exposes alert registration; configs persist to
                            .dq_monitor_alerts.json in the project dir)

Transport: streamable HTTP. Default bind: 127.0.0.1:8768 (override via
DQ_MONITOR_MCP_HOST / DQ_MONITOR_MCP_PORT).

Auth: same pattern as lineage_reporter/glossary_manager — v2 session →
JWT mint at /identity-service/api/v1/jwt/Token with per-request nonce;
JWT cached 29 min, force-refresh on 401.

Reads back DQ scores via CDGC's segment-based asset detail call:
  GET /data360/search/v1/assets/{id}?segments=dataQuality:all
The same call exposes scoreTrend data per the DGC API Reference.

Run locally:
    python dq_monitor_mcp.py
Then add to .vscode/mcp.json at http://127.0.0.1:8768/mcp.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
from idmc_governance.common.paths import ENV_PATH, REPO_ROOT  # repo-root paths (src-layout safe)
ALERTS_PATH = REPO_ROOT / ".dq_monitor_alerts.json"

CDGC_API_BASE = os.getenv("CDGC_API_BASE", "https://cdgc-api.dmp-us.informaticacloud.com")
DEFAULT_ORG_ID = os.getenv("IDMC_ORG_ID")

# Degradation thresholds (used by check_score_trends + recommend_remediation
# when the caller doesn't pass explicit values).
DEFAULT_DEGRADATION_DELTA = float(os.getenv("DQ_DEGRADATION_DELTA", "10.0"))   # >=10 pt drop = degrading
DEFAULT_LOOKBACK_DAYS    = int(os.getenv("DQ_DEFAULT_LOOKBACK_DAYS", "30"))

log = logging.getLogger("dq_monitor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_env_lock = threading.Lock()
_alerts_lock = threading.Lock()
_jwt_lock = threading.Lock()
_jwt_cache: dict[str, Any] = {"token": None, "expires_at": 0.0}
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
# Session + JWT (mirrors lineage_reporter_mcp.py)
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
    """Mint a Bearer JWT for CDGC calls; cache for 29 min."""
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
    headers.setdefault("x-infa-show-association-label", "true")
    headers.setdefault("x-infa-show-custom-attribute-label", "true")
    if "json" in kw or "data" in kw:
        headers.setdefault("Content-Type", "application/json")
    r = httpx.request(method, url, headers=headers, timeout=60, **kw)
    if r.status_code == 401:
        headers["Authorization"] = f"Bearer {_mint_jwt(force=True)}"
        r = httpx.request(method, url, headers=headers, timeout=60, **kw)
    return r


# ---------------------------------------------------------------------------
# CDGC asset + DQ helpers
# ---------------------------------------------------------------------------
def _search_assets(name_query: str, size: int = 25) -> list[dict[str, Any]]:
    url = (f"{CDGC_API_BASE}/data360/search/v1/assets"
           f"?knowledgeQuery={quote(name_query)}&segments=summary,systemAttributes")
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
    hits = _search_assets(asset_name, size=25)
    if not hits:
        raise RuntimeError(f"No CDGC asset found for name: {asset_name!r}")
    name_lc = asset_name.lower()

    def name_of(h: dict[str, Any]) -> str:
        return ((h.get("summary") or {}).get("core.name")
                or h.get("core.name") or h.get("name") or "")

    def id_of(h: dict[str, Any]) -> str | None:
        return (h.get("core.identity") or h.get("id")
                or (h.get("summary") or {}).get("core.identity"))

    exact = [h for h in hits if name_of(h).lower() == name_lc]
    chosen = (exact or hits)[0]
    aid = id_of(chosen)
    if not aid:
        raise RuntimeError(f"search hit for {asset_name!r} missing core.identity: {chosen}")
    return {
        "id": aid,
        "name": name_of(chosen),
        "classType": ((chosen.get("systemAttributes") or {}).get("core.classType")
                      or chosen.get("core.classType")),
        "externalId": chosen.get("core.externalId") or (chosen.get("summary") or {}).get("core.externalId"),
    }


def _get_asset_dq(asset_id: str) -> dict[str, Any]:
    """GET asset details with all DQ segments."""
    segments = "summary,systemAttributes,dataQuality:all"
    url = (f"{CDGC_API_BASE}/data360/search/v1/assets/{asset_id}"
           f"?scheme=internal&segments={segments}")
    r = _request_cdgc("GET", url)
    if r.status_code >= 400:
        raise RuntimeError(f"get asset DQ HTTP {r.status_code}: {r.text[:400]}")
    return r.json() if r.text else {}


def _extract_dq_scores(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the dataQuality segment into a list of score records.

    The CDGC response shape varies by asset type. We probe several common
    locations: ``dataQuality.scores``, ``dataQuality.ruleOccurrences``,
    ``dataQuality.dataQualityScores``. Each yielded record has at least
    ``{rule, dimension, value, total, exception, lastRun}``.
    """
    out: list[dict[str, Any]] = []
    dq = detail.get("dataQuality") or {}

    def coerce(rec: dict[str, Any]) -> dict[str, Any]:
        facts = rec.get("facts") or {}
        ns = "com.infa.ccgf.models.governance."
        return {
            "rule":      rec.get("ruleName") or rec.get("rule") or rec.get("name"),
            "dimension": rec.get("dimension")
                          or facts.get(f"{ns}dimension")
                          or rec.get("ruleDimension"),
            "value":     rec.get("value")
                          or facts.get(f"{ns}value")
                          or rec.get("score"),
            "total":     rec.get("totalCount")
                          or facts.get(f"{ns}totalCount"),
            "exception": rec.get("exception")
                          or facts.get(f"{ns}exception"),
            "lastRun":   rec.get("scannedTime")
                          or facts.get(f"{ns}scannedTime")
                          or rec.get("lastRunTime"),
            "occurrenceId": rec.get("id")
                          or rec.get("ruleOccurrenceId")
                          or rec.get("assetId"),
            "raw_keys":  list(rec.keys())[:8],
        }

    for key in ("scores", "ruleOccurrences", "dataQualityScores", "ruleScores", "items"):
        v = dq.get(key)
        if isinstance(v, list):
            out.extend(coerce(r) for r in v if isinstance(r, dict))
    # Fallback: dataQuality itself may BE the list/score
    if not out and isinstance(dq, list):
        out.extend(coerce(r) for r in dq if isinstance(r, dict))
    return out


def _extract_dq_trend(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract historical-score time series, if the response includes one.

    DGC's `Score trend for a data element` (user-manual Ch 5) implies CDGC
    persists score history per rule occurrence. The response field names
    aren't strictly documented in the API Reference; we look for several
    common keys.
    """
    dq = detail.get("dataQuality") or {}
    for key in ("scoreTrend", "trend", "history", "scoreHistory"):
        v = dq.get(key)
        if isinstance(v, list):
            return v
    # Sometimes trend is nested under each score record
    out: list[dict[str, Any]] = []
    for r in (dq.get("scores") or []) + (dq.get("ruleOccurrences") or []):
        if isinstance(r, dict):
            for key in ("scoreTrend", "history", "trend"):
                v = r.get(key)
                if isinstance(v, list):
                    out.extend({"rule": r.get("ruleName") or r.get("rule"), **pt}
                               for pt in v if isinstance(pt, dict))
    return out


# ---------------------------------------------------------------------------
# Alerts persistence (local JSON; no IDMC API exposes alert registration)
# ---------------------------------------------------------------------------
def _read_alerts() -> list[dict[str, Any]]:
    if not ALERTS_PATH.exists():
        return []
    try:
        return json.loads(ALERTS_PATH.read_text())
    except json.JSONDecodeError:
        return []


def _write_alerts(alerts: list[dict[str, Any]]) -> None:
    with _alerts_lock:
        tmp = ALERTS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(alerts, indent=2))
        tmp.replace(ALERTS_PATH)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="dq_monitor",
    instructions=(
        "IDMC CDGC data-quality monitoring. Use get_dq_scores to read the "
        "current scorecard for an asset, check_score_trends to detect "
        "degradation against history, recommend_remediation to summarize "
        "failures for an LLM to reason over, and alert_on_degradation to "
        "register a local monitoring config (no IDMC API exposes alert "
        "registration — alerts persist to .dq_monitor_alerts.json)."
    ),
)


@mcp.tool()
def get_dq_scores(
    asset_name: str,
    dimension: str | None = None,
) -> dict[str, Any]:
    """Current DQ scorecard for a named CDGC asset.

    Args:
      asset_name: Free-text asset name (matched against CDGC search).
      dimension: Optional filter (e.g. "COMPLETENESS", "VALIDITY"). When
                 set, only scores tagged with that dimension are returned.

    Returns: {asset, scores:[{rule,dimension,value,total,exception,lastRun,occurrenceId}],
              composite, score_count, dimensions}.
    """
    asset = _resolve_asset(asset_name)
    detail = _get_asset_dq(asset["id"])
    scores = _extract_dq_scores(detail)

    if dimension:
        dim_lc = dimension.lower()
        scores = [s for s in scores if (s.get("dimension") or "").lower() == dim_lc]

    # Composite (average of numeric values).
    numeric_values = [float(s["value"]) for s in scores
                       if s.get("value") not in (None, "") and str(s["value"]).replace(".", "", 1).isdigit()]
    composite = (sum(numeric_values) / len(numeric_values)) if numeric_values else None
    dims = sorted({(s.get("dimension") or "UNKNOWN") for s in scores})

    return {
        "asset":       asset,
        "scores":      scores,
        "composite":   composite,
        "score_count": len(scores),
        "dimensions":  dims,
    }


@mcp.tool()
def check_score_trends(
    asset_name: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    degradation_delta: float = DEFAULT_DEGRADATION_DELTA,
) -> dict[str, Any]:
    """Detect DQ score degradation over a lookback window.

    Reads the score-trend series CDGC keeps for each rule occurrence (the
    same data the Score tab in the UI graphs) and compares the newest
    point to the oldest point within the window. A drop of at least
    `degradation_delta` points is flagged.

    Args:
      asset_name: CDGC asset name.
      lookback_days: How far back to look (default 30; clamped to 1-365).
      degradation_delta: Min point drop to flag (default 10.0).

    Returns: {asset, lookback_days, threshold, degrading:[...],
              improving:[...], stable:[...], no_history:[...]}.
    """
    lookback_days = max(1, min(int(lookback_days or DEFAULT_LOOKBACK_DAYS), 365))
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    asset = _resolve_asset(asset_name)
    detail = _get_asset_dq(asset["id"])
    trend = _extract_dq_trend(detail)
    scores = _extract_dq_scores(detail)

    def _parse_time(s: Any) -> datetime | None:
        if not s: return None
        if isinstance(s, (int, float)):
            try:
                return datetime.fromtimestamp(s / 1000 if s > 1e12 else s, tz=timezone.utc)
            except (OSError, ValueError):
                return None
        if isinstance(s, str):
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    # Bucket trend points by rule
    by_rule: dict[str, list[dict[str, Any]]] = {}
    for pt in trend:
        rule = pt.get("rule") or pt.get("ruleName")
        if not rule:
            continue
        t = _parse_time(pt.get("scannedTime") or pt.get("time") or pt.get("date"))
        if not t or t < cutoff:
            continue
        v = pt.get("value") or pt.get("score")
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        by_rule.setdefault(rule, []).append({"time": t, "value": v})

    degrading, improving, stable, no_history = [], [], [], []
    rules_seen = {(s.get("rule") or "?") for s in scores}

    for rule in rules_seen:
        pts = sorted(by_rule.get(rule, []), key=lambda p: p["time"])
        if len(pts) < 2:
            no_history.append({"rule": rule, "points": len(pts)})
            continue
        first, last = pts[0]["value"], pts[-1]["value"]
        delta = last - first
        entry = {"rule": rule, "first": first, "last": last, "delta": delta,
                 "points": len(pts),
                 "first_at": pts[0]["time"].isoformat(),
                 "last_at":  pts[-1]["time"].isoformat()}
        if delta <= -degradation_delta:
            degrading.append(entry)
        elif delta >= degradation_delta:
            improving.append(entry)
        else:
            stable.append(entry)

    return {
        "asset":         asset,
        "lookback_days": lookback_days,
        "threshold":     degradation_delta,
        "degrading":     degrading,
        "improving":     improving,
        "stable":        stable,
        "no_history":    no_history,
    }


@mcp.tool()
def recommend_remediation(asset_name: str) -> dict[str, Any]:
    """Structured failure summary for an asset, suitable for LLM-side reasoning.

    Pulls the current DQ scores and identifies failing rules (by
    `exception` count > 0 or value below 70). For each failing rule,
    surfaces the rule name, dimension, exception count, total rows, and
    the rule's last-run timestamp. The caller (an LLM) typically composes
    a human-readable recommendation from this structured input.

    Args:
      asset_name: CDGC asset to analyze.

    Returns: {asset, summary:{rule_count, failing_rule_count, total_exceptions},
              failing_rules:[{rule,dimension,exception,total,exception_pct,value,
              lastRun,occurrenceId}], healthy_rules:[...], suggestion_seeds:[...]}.
    """
    asset = _resolve_asset(asset_name)
    detail = _get_asset_dq(asset["id"])
    scores = _extract_dq_scores(detail)

    failing, healthy = [], []
    total_exceptions = 0
    for s in scores:
        exc = s.get("exception") or 0
        try:
            exc = int(exc) if exc is not None else 0
        except (TypeError, ValueError):
            exc = 0
        val = s.get("value")
        try:
            val_f = float(val) if val is not None else None
        except (TypeError, ValueError):
            val_f = None
        total = s.get("total") or 0
        try:
            total = int(total) if total is not None else 0
        except (TypeError, ValueError):
            total = 0
        exception_pct = (exc / total * 100.0) if total else None

        record = {
            "rule":          s.get("rule"),
            "dimension":     s.get("dimension"),
            "value":         val_f,
            "total":         total,
            "exception":     exc,
            "exception_pct": round(exception_pct, 2) if exception_pct is not None else None,
            "lastRun":       s.get("lastRun"),
            "occurrenceId":  s.get("occurrenceId"),
        }
        if exc > 0 or (val_f is not None and val_f < 70):
            failing.append(record)
            total_exceptions += exc
        else:
            healthy.append(record)

    # Seed remediation prompts the LLM can expand into specific suggestions.
    seeds: list[dict[str, Any]] = []
    for f in failing:
        dim = (f.get("dimension") or "").upper()
        seed = {"rule": f["rule"], "dimension": dim, "exception": f["exception"]}
        if dim == "COMPLETENESS":
            seed["suggested_focus"] = "investigate upstream nulls / missing values"
        elif dim == "VALIDITY":
            seed["suggested_focus"] = "tighten input validation or upstream type checks"
        elif dim == "UNIQUENESS":
            seed["suggested_focus"] = "deduplicate at ingestion or enforce primary key"
        elif dim == "ACCURACY":
            seed["suggested_focus"] = "cross-check against authoritative reference data"
        elif dim == "CONSISTENCY":
            seed["suggested_focus"] = "audit cross-field constraints / dependent values"
        elif dim == "TIMELINESS":
            seed["suggested_focus"] = "audit ingestion freshness / late-arriving data"
        else:
            seed["suggested_focus"] = "drill into rule occurrence for sample failing rows"
        seeds.append(seed)

    return {
        "asset": asset,
        "summary": {
            "rule_count":          len(scores),
            "failing_rule_count":  len(failing),
            "total_exceptions":    total_exceptions,
        },
        "failing_rules":     failing,
        "healthy_rules":     healthy,
        "suggestion_seeds":  seeds,
    }


@mcp.tool()
def alert_on_degradation(
    asset_name: str,
    threshold: float,
    notify_email: str,
    dimension: str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    note: str = "",
) -> dict[str, Any]:
    """Register a local monitoring config for DQ score degradation.

    IDMC's DGC API does not expose a programmatic alert-registration
    endpoint (notifications are configured in the CDGC UI). This tool
    persists a *local* config to .dq_monitor_alerts.json in the project
    directory; a sibling cron/agent process can poll check_score_trends
    on the configured cadence and act on the email field. The config is
    NOT pushed to IDMC.

    Args:
      asset_name:   CDGC asset to watch.
      threshold:    Minimum point drop in DQ score to trigger an alert.
      notify_email: Address(es) to notify (free-text; tool doesn't send mail).
      dimension:    Optional DQ dimension filter.
      lookback_days: Window for trend comparison (1-365).
      note:         Free-text annotation.

    Returns: {id, registered_at, alerts_path, total_alerts, this_alert}.
    """
    asset = _resolve_asset(asset_name)
    alerts = _read_alerts()
    aid = uuid.uuid4().hex[:12]
    entry = {
        "id":            aid,
        "asset_name":    asset_name,
        "asset_id":      asset["id"],
        "asset_classType": asset.get("classType"),
        "threshold":     float(threshold),
        "notify_email":  notify_email,
        "dimension":     dimension,
        "lookback_days": max(1, min(int(lookback_days or DEFAULT_LOOKBACK_DAYS), 365)),
        "note":          note,
        "registered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    alerts.append(entry)
    _write_alerts(alerts)

    return {
        "id":            aid,
        "registered_at": entry["registered_at"],
        "alerts_path":   str(ALERTS_PATH),
        "total_alerts":  len(alerts),
        "this_alert":    entry,
        "note":          ("Alert persisted locally only — IDMC has no public alert-registration "
                          "API. Run check_score_trends in your own scheduler to act on this."),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _configure_settings() -> None:
    host = os.getenv("DQ_MONITOR_MCP_HOST", "127.0.0.1")
    port = int(os.getenv("DQ_MONITOR_MCP_PORT", "8768"))
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
    log.info("starting dq_monitor MCP server on %s transport", transport)
    try:
        mcp.run(transport=transport)
    except KeyboardInterrupt:
        log.info("shutting down")
        sys.exit(0)
