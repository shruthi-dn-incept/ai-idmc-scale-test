#!/usr/bin/env python
"""List existing CDQ rule specs and emit a dimension -> rule_spec_id map.

Read-only (GET against FRS), so it works locally even though FRS *writes* 503.
Reuses governance_engine_mcp's login/session helpers exactly like probe_dq_template.py.

Output: rule_map.json  ->  {"Completeness": "<frs_id>", "Validity": "<frs_id>", ...}
Picks the most-recently-updated rule spec per dimension.

Run:  python probe_rule_specs.py
"""
import json
import os
import sys
from urllib.parse import quote

# Env must be in os.environ BEFORE importing gem (it reads at module load).
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

import governance_engine_mcp as gem

DIMS = ["Completeness", "Validity", "Uniqueness", "Timeliness", "Accuracy", "Consistency"]


def _dim_of(it):
    attrs = ((it.get("customAttributes") or {}).get("stringAttrs") or [])
    for a in attrs:
        if a.get("name") == "DIMENSION":
            return (a.get("value") or "").strip()
    return None


def main() -> int:
    qs = "$filter=" + quote("documentType eq 'RULE_SPECIFICATION'", safe="") + "&$top=500"
    url = f"{gem.FRS_API}/Documents?{qs}"
    r = gem._request("GET", url)
    print(f"GET rule specs -> HTTP {r.status_code}")
    if r.status_code != 200:
        print(r.text[:500])
        return 1
    items = (r.json() or {}).get("value", []) or []
    print(f"total rule specs: {len(items)}")

    # dimension -> list of (lastUpdatedTime, id, name)
    by_dim = {}
    for it in items:
        dim = _dim_of(it)
        key = None
        if dim:
            for d in DIMS:
                if d.upper() == dim.upper():
                    key = d
                    break
        by_dim.setdefault(key, []).append(
            (it.get("lastUpdatedTime") or "", it.get("id"), it.get("name"))
        )

    print("\n--- coverage by dimension ---")
    rule_map = {}
    for d in DIMS:
        cand = sorted(by_dim.get(d, []), reverse=True)
        if cand:
            rule_map[d] = cand[0][1]
            print(f"  {d:14s}: {len(cand):3d} specs  -> using {cand[0][1]}  ({cand[0][2]})")
        else:
            print(f"  {d:14s}:   0 specs  -> MISSING (needs creation)")

    unclassified = by_dim.get(None, [])
    if unclassified:
        print(f"\n  (+{len(unclassified)} rule specs with no/unknown DIMENSION attr)")
        for _, rid, name in unclassified[:10]:
            print(f"      {rid}  {name}")

    with open("rule_map.json", "w") as f:
        json.dump(rule_map, f, indent=2)
    print(f"\nwrote rule_map.json  ({len(rule_map)}/{len(DIMS)} dimensions covered)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
