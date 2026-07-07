"""
setup_cdgc_source.py
--------------------
1. Creates a Snowflake connection in IDMC Administrator (v2 API)
2. Creates an MCC catalog source in CDGC pointing to GOVERNANCE_SCALE_TEST
3. Optionally triggers the initial metadata scan

Usage:
  python setup_cdgc_source.py                  # dry-run: show what would be created
  python setup_cdgc_source.py --create         # create connection + catalog source
  python setup_cdgc_source.py --create --scan  # create + immediately trigger scan
  python setup_cdgc_source.py --scan-only      # only trigger scan (source already exists)
  python setup_cdgc_source.py --list           # list existing connections and catalog sources

Env vars read from .env (or environment):
  IDMC_USER, IDMC_PASS, IDMC_LOGIN_HOST, IDMC_SERVER_URL
  SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PRIVATE_KEY_B64 (or SNOWFLAKE_PASSWORD)
  SNOWFLAKE_WAREHOUSE, SNOWFLAKE_ROLE, SNOWFLAKE_GOVTEST_DB
  IDMC_DQ_RUNTIME_ENV_ID   — runtime environment to run the scan on
  CDGC_API_BASE
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
load_dotenv(Path(__file__).parent / ".env.docker", override=False)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
IDMC_USER       = os.getenv("IDMC_USER", "")
IDMC_PASS       = os.getenv("IDMC_PASS", "")
IDMC_LOGIN_HOST = os.getenv("IDMC_LOGIN_HOST", "dmp-us.informaticacloud.com")
IDMC_SERVER_URL = os.getenv("IDMC_SERVER_URL", "")
CDGC_API_BASE   = os.getenv("CDGC_API_BASE", "https://cdgc-api.dmp-us.informaticacloud.com")

SF_ACCOUNT  = os.getenv("SNOWFLAKE_ACCOUNT", "ygc42528.us-east-1")
SF_USER     = os.getenv("SNOWFLAKE_USER", "GOVTEST_SVC_ACCOUNT")
SF_PASSWORD = os.getenv("SNOWFLAKE_PASSWORD", "")
SF_PK_B64   = os.getenv("SNOWFLAKE_PRIVATE_KEY_B64", "")
SF_WH       = os.getenv("SNOWFLAKE_WAREHOUSE", "INCEPT_WH")
SF_ROLE     = os.getenv("SNOWFLAKE_ROLE", "ACCOUNTADMIN")
SF_DB       = os.getenv("SNOWFLAKE_GOVTEST_DB", "GOVERNANCE_SCALE_TEST")

RUNTIME_ENV_ID  = os.getenv("IDMC_DQ_RUNTIME_ENV_ID", "")
IDMC_ORG_ID     = os.getenv("IDMC_ORG_ID", "")
JWT_CLIENT_ID   = os.getenv("IDMC_JWT_CLIENT_ID", "idmc_api")
IDMC_IDENTITY_HOST = os.getenv("IDMC_IDENTITY_HOST", "dmp-us.informaticacloud.com")

# Names used when creating
CONNECTION_NAME  = "GOVTEST-SNOWFLAKE"
CATALOG_SRC_NAME = "GOVTEST-GOVERNANCE-SCALE-TEST"

SCHEMAS = ["GOVTEST_CLAIMS", "GOVTEST_CLINICAL", "GOVTEST_MEMBER", "GOVTEST_PROVIDER"]

# ── Auth helpers ──────────────────────────────────────────────────────────────
_session_cache: dict = {}


def _login_v2() -> tuple[str, str]:
    """Login to IDMC v2 and return (session_id, base_url)."""
    if _session_cache.get("v2_sid") and _session_cache.get("v2_base"):
        return _session_cache["v2_sid"], _session_cache["v2_base"]

    r = httpx.post(
        f"https://{IDMC_LOGIN_HOST}/ma/api/v2/user/login",
        json={"@type": "login", "username": IDMC_USER, "password": IDMC_PASS},
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"IDMC v2 login failed HTTP {r.status_code}: {r.text[:300]}")
    j   = r.json()
    sid = j.get("icSessionId") or j.get("userInfo", {}).get("sessionId") or r.headers.get("INFA-SESSION-ID", "")
    if not sid:
        raise RuntimeError(f"No session ID in v2 login response: {j}")
    base = j.get("serverUrl") or IDMC_SERVER_URL or f"https://{IDMC_LOGIN_HOST}/saas"
    _session_cache["v2_sid"]  = sid
    _session_cache["v2_base"] = base.rstrip("/")
    log.info("v2 session minted (%s...)", sid[:8])
    return sid, base.rstrip("/")


def _v2_headers() -> dict:
    sid, _ = _login_v2()
    return {"INFA-SESSION-ID": sid, "Content-Type": "application/json", "Accept": "application/json"}


def _get_jwt() -> str:
    """Mint a CDGC Bearer JWT from a v2 session."""
    if _session_cache.get("jwt") and time.time() < _session_cache.get("jwt_expires", 0):
        return _session_cache["jwt"]
    sid, _ = _login_v2()
    nonce = uuid.uuid4().hex.upper()
    r = httpx.get(
        f"https://{IDMC_IDENTITY_HOST}/identity-service/api/v1/jwt/Token",
        params={"client_id": JWT_CLIENT_ID, "nonce": nonce},
        headers={"IDS-SESSION-ID": sid},
        timeout=30,
    )
    if r.status_code == 401:
        _session_cache.clear()
        sid, _ = _login_v2()
        r = httpx.get(
            f"https://{IDMC_IDENTITY_HOST}/identity-service/api/v1/jwt/Token",
            params={"client_id": JWT_CLIENT_ID, "nonce": nonce},
            headers={"IDS-SESSION-ID": sid},
            timeout=30,
        )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"JWT mint HTTP {r.status_code}: {r.text[:300]}")
    token = r.json().get("jwt_token") or r.json().get("token") or ""
    if not token:
        raise RuntimeError(f"Empty JWT in response: {r.text[:300]}")
    _session_cache["jwt"]         = token
    _session_cache["jwt_expires"] = time.time() + 1740  # 29 min
    log.info("JWT minted")
    return token


def _cdgc_headers() -> dict:
    sid, _ = _login_v2()
    return {
        "Authorization": f"Bearer {_get_jwt()}",
        "IDS-SESSION-ID": sid,
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }


def _cdgc(method: str, path: str, **kw) -> httpx.Response:
    url = f"{CDGC_API_BASE}/{path.lstrip('/')}"
    kw.setdefault("timeout", 60)
    r = httpx.request(method, url, headers=_cdgc_headers(), **kw)
    if r.status_code == 401:
        _session_cache.clear()
        r = httpx.request(method, url, headers=_cdgc_headers(), **kw)
    return r


# ── Step 1: Snowflake connection ───────────────────────────────────────────────

def _sf_host() -> str:
    """Convert account ID to Snowflake hostname."""
    account = SF_ACCOUNT.replace("_", "-")
    if ".snowflakecomputing.com" not in account:
        return f"{account}.snowflakecomputing.com"
    return account


def list_connections() -> list[dict]:
    """List existing IDMC connections via v2 API."""
    _, base = _login_v2()
    r = httpx.get(
        f"{base}/api/v2/connection",
        headers=_v2_headers(),
        params={"limit": 200},
        timeout=30,
        follow_redirects=True,
    )
    if r.status_code >= 400:
        log.warning("list_connections HTTP %s: %s", r.status_code, r.text[:200])
        return []
    if not r.text.strip():
        log.warning("list_connections: empty response")
        return []
    data = r.json()
    if isinstance(data, list):
        return data
    return data.get("connection") or data.get("data") or []


def find_connection(name: str) -> dict | None:
    """Return existing connection dict if found by name, else None."""
    for c in list_connections():
        if c.get("name", "").lower() == name.lower():
            return c
    return None


def create_snowflake_connection(dry_run: bool = False) -> dict:
    """Create Snowflake connection in IDMC. Returns connection dict."""
    existing = find_connection(CONNECTION_NAME)
    if existing:
        log.info("Connection '%s' already exists (id=%s) — skipping create", CONNECTION_NAME, existing.get("id"))
        return existing

    # Snowflake connection properties for IDMC v2 REST
    # Note: property keys match what IDMC Snowflake connector expects.
    props = {
        "Host":      _sf_host(),
        "Username":  SF_USER,
        "Warehouse": SF_WH,
        "Database":  SF_DB,
        "Role":      SF_ROLE,
    }
    # Password auth (key-pair isn't supported via IDMC connection API for CDQ/MCC)
    if SF_PASSWORD:
        props["Password"] = SF_PASSWORD
    elif SF_PK_B64:
        # Encode the PEM so the connector can use it
        props["AuthMode"]   = "Keypair"
        props["PrivateKey"] = SF_PK_B64
    else:
        log.warning("No SF_PASSWORD or SF_PK_B64 — connection may fail auth")

    body = {
        "name":                 CONNECTION_NAME,
        "description":          "Scale test connection to GOVERNANCE_SCALE_TEST (4k tables)",
        "type":                 "Snowflake",
        "runtimeEnvironmentId": RUNTIME_ENV_ID,
        "properties":           props,
    }

    log.info("Creating connection '%s' (dry_run=%s)", CONNECTION_NAME, dry_run)
    if dry_run:
        print(json.dumps({"action": "CREATE_CONNECTION", "body": body}, indent=2))
        return {"id": "DRY-RUN", "name": CONNECTION_NAME}

    _, base = _login_v2()
    r = httpx.post(
        f"{base}/api/v2/connection",
        headers=_v2_headers(),
        json=body,
        timeout=60,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Create connection HTTP {r.status_code}: {r.text[:500]}")
    conn = r.json()
    log.info("Connection created: id=%s", conn.get("id"))
    return conn


# ── Step 2: MCC catalog source ─────────────────────────────────────────────────

def list_catalog_sources() -> list[dict]:
    """List MCC catalog sources from CDGC."""
    # Try MCC-specific endpoint first
    for path in [
        "data360/content/v1/catalogsources",
        "mcc/api/v1/sources",
        "data360/search/v1/assets?knowledgeQuery=GOVERNANCE_SCALE_TEST&segments=summary,systemAttributes",
    ]:
        r = _cdgc("GET", path)
        if r.status_code < 400:
            data = r.json() or {}
            if isinstance(data, list):
                return data
            for key in ("catalogSources", "sources", "hits", "items", "data"):
                if isinstance(data.get(key), list):
                    return data[key]
    # Fall back to searching via CDGC assets
    r = _cdgc("POST", "data360/search/v1/assets",
              json={"from": 0, "size": 100},
              params={"knowledgeQuery": "Resource", "segments": "summary,systemAttributes"})
    if r.status_code >= 400:
        return []
    hits = (r.json() or {}).get("hits", [])
    if isinstance(hits, dict):
        hits = hits.get("hits", [])
    return [h for h in hits if "Resource" in str(h.get("systemAttributes", {}).get("core.classType", ""))]


def find_catalog_source(name: str) -> dict | None:
    """Return existing catalog source dict if found by name, else None."""
    for s in list_catalog_sources():
        src_name = (s.get("summary") or {}).get("core.name") or s.get("name") or ""
        if src_name.lower() == name.lower():
            return s
    return None


def create_catalog_source(connection_id: str, dry_run: bool = False) -> dict:
    """
    Create an MCC catalog source in CDGC that points to the Snowflake connection.
    Tries the catalogsources endpoint first; falls back to CDGC asset creation.
    """
    existing = find_catalog_source(CATALOG_SRC_NAME)
    if existing:
        ext_id = (existing.get("systemAttributes") or {}).get("core.externalId") or existing.get("id") or ""
        src_id = ext_id.split("://")[0] if "://" in ext_id else ext_id
        log.info("Catalog source '%s' already exists (id=%s)", CATALOG_SRC_NAME, src_id)
        return existing

    # Payload shape for MCC catalog source creation
    body = {
        "name":         CATALOG_SRC_NAME,
        "description":  "Scale test — all 4 GOVERNANCE_SCALE_TEST schemas (4,000 tables)",
        "connectionId": connection_id,
        "sourceType":   "Snowflake",
        "configuration": {
            "database": SF_DB,
            "schemas":  SCHEMAS,
            "includeAllObjects": True,
        },
        "runtimeEnvironmentId": RUNTIME_ENV_ID,
        "capabilities": ["Metadata", "Data Quality"],
    }

    log.info("Creating catalog source '%s' (dry_run=%s)", CATALOG_SRC_NAME, dry_run)
    if dry_run:
        print(json.dumps({"action": "CREATE_CATALOG_SOURCE", "body": body}, indent=2))
        return {"id": "DRY-RUN", "name": CATALOG_SRC_NAME}

    # Try MCC-specific endpoint first
    for path in ["data360/content/v1/catalogsources", "mcc/api/v1/sources"]:
        r = _cdgc("POST", path, json=body)
        if r.status_code in (200, 201):
            src = r.json() or {}
            log.info("Catalog source created via %s: %s", path, json.dumps(src)[:200])
            return src
        if r.status_code not in (404, 405):
            log.warning("POST %s HTTP %s: %s", path, r.status_code, r.text[:300])

    # Fallback: create as CDGC core.Resource asset
    log.info("Falling back to CDGC asset creation (core.Resource)")
    asset_body = {
        "core.classType": "core.Resource",
        "summary": {
            "core.name":        CATALOG_SRC_NAME,
            "core.description": body["description"],
            "sourceType":       "Snowflake",
            "connectionId":     connection_id,
            "database":         SF_DB,
            "schemas":          ",".join(SCHEMAS),
        },
    }
    r = _cdgc("POST", "data360/content/v1/assets", json=asset_body)
    if r.status_code in (200, 201):
        src = r.json() or {}
        log.info("Catalog source created as asset: %s", json.dumps(src)[:200])
        return src
    raise RuntimeError(
        f"Could not create catalog source. HTTP {r.status_code}: {r.text[:400]}\n\n"
        "Please create it manually in IDMC Metadata Command Center:\n"
        "  1. Go to IDMC → Metadata Command Center → Sources\n"
        "  2. New Source → Snowflake\n"
        f"  3. Connection: {CONNECTION_NAME}\n"
        f"  4. Database: {SF_DB}\n"
        f"  5. Schemas: {', '.join(SCHEMAS)}\n"
        "  6. Save, then run: python setup_cdgc_source.py --scan-only"
    )


# ── Step 3: Trigger scan ──────────────────────────────────────────────────────

def get_catalog_source_id(source: dict) -> str | None:
    """Extract the MCC catalog source UUID from a source/asset dict."""
    for key in ("id", "catalogSourceId", "sourceId"):
        val = source.get(key)
        if val and val != "DRY-RUN":
            return str(val)
    ext_id = (source.get("systemAttributes") or {}).get("core.externalId", "")
    if ext_id:
        return ext_id.split("://")[0]
    return None


def trigger_scan(source_id: str, capabilities: list[str] | None = None, wait: bool = False) -> dict:
    """Trigger MCC metadata scan on the catalog source."""
    caps = capabilities or ["Metadata", "Data Quality"]
    url  = f"{CDGC_API_BASE}/data360/executable/v1/catalogsource/{source_id}"
    log.info("Triggering scan on source %s | caps=%s", source_id, caps)
    r = _cdgc("POST", url.replace(CDGC_API_BASE + "/", ""),
              json={"capabilityNames": caps})
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"Scan trigger HTTP {r.status_code}: {r.text[:400]}")
    job = r.json() or {}
    job_id = job.get("jobId") or job.get("id") or ""
    log.info("Scan job started: job_id=%s", job_id)

    if wait and job_id:
        log.info("Waiting for scan to complete (may take 10-60 min for 4k tables)...")
        status_url = f"data360/observable/v1/jobs/{job_id}?expandChildren=TASK-HIERARCHY"
        while True:
            time.sleep(30)
            sr = _cdgc("GET", status_url)
            if sr.status_code >= 400:
                log.warning("Status check HTTP %s", sr.status_code)
                continue
            status = (sr.json() or {}).get("status") or (sr.json() or {}).get("state") or "UNKNOWN"
            log.info("  Scan status: %s", status)
            if status.upper() in ("COMPLETED", "SUCCESS", "FAILED", "ERROR", "CANCELLED"):
                job = sr.json() or job
                break

    return {"job_id": job_id, "job_response": job}


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Set up CDGC catalog source for GOVERNANCE_SCALE_TEST")
    ap.add_argument("--create",    action="store_true", help="Create connection + catalog source")
    ap.add_argument("--scan",      action="store_true", help="Trigger scan after creation")
    ap.add_argument("--scan-only", action="store_true", help="Only trigger scan (source already exists)")
    ap.add_argument("--list",      action="store_true", help="List existing connections and catalog sources")
    ap.add_argument("--wait",      action="store_true", help="Wait for scan to complete")
    args = ap.parse_args()

    dry_run = not (args.create or args.scan_only)

    if args.list or dry_run:
        print("\n=== Existing IDMC Connections ===")
        conns = list_connections()
        sf_conns = [c for c in conns if "snowflake" in (c.get("type") or "").lower()]
        for c in sf_conns:
            print(f"  {c.get('name')} | id={c.get('id')} | env={c.get('runtimeEnvironmentId')}")
        if not sf_conns:
            print("  (no Snowflake connections found)")

        print("\n=== Existing CDGC Catalog Sources ===")
        sources = list_catalog_sources()
        for s in sources[:20]:
            name = (s.get("summary") or {}).get("core.name") or s.get("name") or "(unnamed)"
            src_id = get_catalog_source_id(s) or "(no id)"
            print(f"  {name} | id={src_id}")
        if not sources:
            print("  (no catalog sources found)")

        if dry_run:
            print(f"\n=== DRY RUN — would create ===")
            print(f"  Connection : {CONNECTION_NAME}")
            print(f"    Host     : {_sf_host()}")
            print(f"    User     : {SF_USER}")
            print(f"    Database : {SF_DB}")
            print(f"    Warehouse: {SF_WH}")
            print(f"    Runtime  : {RUNTIME_ENV_ID}")
            print(f"\n  Catalog Source: {CATALOG_SRC_NAME}")
            print(f"    Schemas : {', '.join(SCHEMAS)}")
            print(f"\nRun with --create to apply, or --create --scan to apply + trigger scan.")
            return

    conn = create_snowflake_connection(dry_run=False)
    conn_id = conn.get("id", "")
    print(f"\n✓ Connection: {CONNECTION_NAME} (id={conn_id})")

    source = create_catalog_source(conn_id, dry_run=False)
    src_id = get_catalog_source_id(source) or ""
    print(f"✓ Catalog source: {CATALOG_SRC_NAME} (id={src_id})")

    if (args.scan or args.scan_only) and src_id:
        result = trigger_scan(src_id, wait=args.wait)
        print(f"✓ Scan triggered: job_id={result.get('job_id')}")
        if args.wait:
            status = (result.get("job_response") or {}).get("status", "?")
            print(f"  Final status: {status}")
    elif args.scan or args.scan_only:
        print("ERROR: Could not determine catalog source ID — trigger scan manually.")
        sys.exit(1)


if __name__ == "__main__":
    main()
