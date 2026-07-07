#!/usr/bin/env python
"""Diagnose the DQ mapping-task 403 (REPO_36004) seen in the Data Quality step.

Reuses governance_engine_mcp's own v2 login/session helpers, so it hits IDMC
exactly the way the app does. Distinguishes the three likely causes:

  1. Stale / wrong IDMC_DQ_TEMPLATE_MAPPING_ID  -> list works, GET-by-id 403/404
  2. Session / permission problem               -> even /api/v2/mapping list fails
  3. Template exists but repo can't load it      -> id resolves & is in list, GET 403

Run:  python probe_dq_template.py
"""
import json
import os
import sys

import governance_engine_mcp as gem


def _short(resp):
    body = resp.text or ""
    return body[:400]


def main() -> int:
    tmpl_env = os.getenv("IDMC_DQ_TEMPLATE_MAPPING_ID", "")
    print("=" * 70)
    print("DQ template mapping probe")
    print("=" * 70)
    print(f"IDMC_DQ_TEMPLATE_MAPPING_ID (raw) : {tmpl_env!r}")
    print(f"IDMC_DQ_CONNECTION_ID             : {os.getenv('IDMC_DQ_CONNECTION_ID','')!r}")
    print(f"IDMC_DQ_RUNTIME_ENV_ID            : {os.getenv('IDMC_DQ_RUNTIME_ENV_ID','')!r}")
    print(f"gem.DEFAULT_DQ_TEMPLATE_MAPPING_ID: {gem.DEFAULT_DQ_TEMPLATE_MAPPING_ID!r}")
    if not tmpl_env:
        print("\n[FATAL] IDMC_DQ_TEMPLATE_MAPPING_ID is empty. Nothing to probe.")
        return 2

    # --- Step 1: can we even list mappings? (session / permission check) -----
    print("\n[1] GET /api/v2/mapping  (session + list access)")
    try:
        lst = gem._request_v2("GET", "/api/v2/mapping")
    except Exception as e:
        print(f"    EXCEPTION during list: {e!r}")
        print("    -> Cause #2: v2 login/session failed. Check IDMC creds/base URL.")
        return 1
    print(f"    HTTP {lst.status_code}")
    if lst.status_code != 200:
        print(f"    body: {_short(lst)}")
        print("    -> Cause #2: session valid-login but list denied. Permission/user issue.")
        return 1

    raw = lst.json()
    items = raw if isinstance(raw, list) else (raw.get("value") or [])
    print(f"    OK — {len(items)} mappings visible to this session.")

    # --- Step 2: resolve v3 GUID -> v2 native id (same path the app uses) ----
    print("\n[2] _resolve_v2_mapping_id() (v3 FRS GUID -> v2 native id)")
    try:
        v2_id = gem._resolve_v2_mapping_id(tmpl_env)
    except Exception as e:
        print(f"    EXCEPTION: {e!r}")
        print("    -> Cause #1: template id not found in the v2 mapping list (stale/deleted).")
        return 1
    print(f"    resolved v2 id: {v2_id!r}")
    resolved_changed = v2_id != tmpl_env
    print(f"    (translation {'applied' if resolved_changed else 'not needed / passthrough'})")

    # Is the resolved id actually present in the enumerated list?
    def _ident(it):
        return {it.get("id"), it.get("assetFrsGuid")}
    match = next((it for it in items if v2_id in _ident(it) or tmpl_env in _ident(it)), None)
    if match:
        print(f"    found in list: id={match.get('id')!r} "
              f"name={match.get('name')!r} frsGuid={match.get('assetFrsGuid')!r}")
    else:
        print("    NOT found in the enumerated mapping list.")
        print("    -> Cause #1: the configured id doesn't correspond to any visible mapping.")

    # --- Step 3: reproduce the failing GET-by-id ----------------------------
    print(f"\n[3] GET /api/v2/mapping/{v2_id}  (the call that 403s in the app)")
    got = gem._request_v2("GET", f"/api/v2/mapping/{v2_id}")
    print(f"    HTTP {got.status_code}")
    print(f"    body: {_short(got)}")
    if got.status_code == 200:
        params = (got.json() or {}).get("parameters") or []
        names = [p.get("name") for p in params if isinstance(p, dict)]
        print(f"    OK — template loads. Parameters: {names}")
        print("\n    => Template is fine now. The 403 was likely transient/session-scoped;")
        print("       re-run the Data Quality step.")
        return 0

    print("\n    => Reproduced the failure.")
    if match:
        print("       Cause #3: id is valid & listed, but the repo can't load the object.")
        print("       Usually a Secure Agent / repo-side issue, or the mapping is")
        print("       unpublished/broken. Re-publish M_DQ_Generic in IDMC.")
    else:
        print("       Cause #1: id is stale/inaccessible. Update IDMC_DQ_TEMPLATE_MAPPING_ID")
        print("       to a valid, published M_DQ_Generic mapping in this org.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
