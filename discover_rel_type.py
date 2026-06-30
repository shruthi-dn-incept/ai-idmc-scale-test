"""
Test user-provided endpoint: POST /access/2/catalog/data/relationships
and POST /access/2/catalog/data/relationships/termToColumn
"""
import sys, json
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding='utf-8')

import httpx
from ai_governance_mcp import _cdgc_headers, CDGC_API_BASE, _v2_session_id, _login_v2

TERM_ID     = "77dc33d6-4edb-472f-85c6-0a688ce41f67"
TERM_EXT    = "BT-41"
COL_ID      = "ec7c3187-c474-486e-af6d-61760537fb8e"
COL_EXT     = "5a339132-345d-3716-a5a9-a1f429835cc6://FUSION_ERP_DEV/FINANCE_ERP_DQ/SUPPLIER_SITE_STAGE/VENDOR_SITE_ID~com.infa.odin.models.relational.Column"

def show(r, label=""):
    print(f"\n--- {label} → HTTP {r.status_code} ---")
    try:
        d = r.json()
        print(json.dumps(d, indent=2)[:1500])
    except Exception:
        print(r.text[:1500])

# Build access/2 headers (uses v2 session-based auth)
def access2_headers():
    # Get v2 session
    try:
        sid, pod = _login_v2()
    except Exception:
        from ai_governance_mcp import _v2_session_id
        sid = _v2_session_id()
        pod = None

    # The access/2 API uses a different base URL - it's served from the IDMC login host
    # Headers for access/2 catalog API
    import os
    from ai_governance_mcp import _read_env
    env = _read_env()
    org_id = env.get("IDMC_ORG_ID", "")

    return {
        "INFA-SESSION-ID": sid,
        "IDS-SESSION-ID":  sid,
        "Accept":          "application/json",
        "Content-Type":    "application/json",
        "icSessionId":     sid,
    }

# Also try with CDGC-style JWT headers
def try_both_headers(method, path, body=None, label=""):
    # Try 1: CDGC JWT headers
    h_cdgc = _cdgc_headers()
    kw = {"headers": h_cdgc, "timeout": 30}
    if body is not None:
        kw["json"] = body
    r1 = getattr(httpx, method.lower())(CDGC_API_BASE + path, **kw)
    print(f"\n{label} [JWT @ cdgc-api]: HTTP {r1.status_code} - {r1.text[:300]}")

    # Try 2: CDGC JWT headers at different base URLs
    for base in [
        "https://dm-us.informaticacloud.com",
        "https://usw3.dm-us.informaticacloud.com",
    ]:
        kw2 = {"headers": h_cdgc, "timeout": 30}
        if body is not None:
            kw2["json"] = body
        r2 = getattr(httpx, method.lower())(base + path, **kw2)
        print(f"{label} [JWT @ {base.split('//')[1][:20]}]: HTTP {r2.status_code} - {r2.text[:200]}")

print("=" * 60)
print("Testing POST /access/2/catalog/data/relationships endpoints")

# Different body formats to try
bodies = [
    {"sourceObjectId": TERM_ID, "targetObjectId": COL_ID,
     "sourceObjectType": "BusinessTerm", "targetObjectType": "Column"},

    {"end1Id": TERM_ID, "end2Id": COL_ID,
     "classType": "asscBusinessTermTechnicalAsset"},

    {"businessTermId": TERM_ID, "assetId": COL_ID},

    {"businessTerm": {"id": TERM_ID}, "asset": {"id": COL_ID}},

    {"termId": TERM_ID, "columnId": COL_ID},
]

for path in [
    "/access/2/catalog/data/relationships",
    "/access/2/catalog/data/relationships/termToColumn",
    "/access/2/catalog/data/businessterms/relationships",
]:
    for body in bodies[:2]:  # Just first 2 body formats per path
        try_both_headers("POST", path, body,
                         f"POST {path} body={json.dumps(body)[:60]}")

print("=" * 60)
print("GET on /access/2 to discover available endpoints")
for path in [
    "/access/2/catalog/data",
    "/access/2/catalog/data/relationships",
    "/access/2/catalog",
    "/access/2/catalog/data/businessterms",
    "/access/2/catalog/data/businessterms/relationships",
]:
    try_both_headers("GET", path, None, f"GET {path}")
