#!/usr/bin/env python
"""Scale curate: link every column instance to its business term via the CDGC
publish API (IClassTechnicalGlossaryBase), using the whole-catalog column->term
map (colterm_map.json) + term ids (term_ids.json).

Deterministic: the 108 unique column names were mapped to terms once by the LLM;
here we apply that map to all ~137k column instances. Batched + parallel publish.

Usage:
  python curate_scale.py --limit-tables 2      # validate
  python curate_scale.py                        # full catalog
"""
import argparse
import glob
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
import governance_engine_mcp as gem

CACHE = ".scan_cache"
REL_TYPE = "com.infa.ccgf.models.governance.IClassTechnicalGlossaryBase"
COL_CLASS = "com.infa.odin.models.relational.Column"


def _publish(items):
    cid = str(uuid.uuid4())
    url = f"{gem.CDGC_API_BASE}/ccgf-contentv2/api/v1/publish"
    r = gem._request_cdgc("POST", url, json={"items": items}, headers={
        "x-infa-product-id": "CDGC", "correlation-id": cid,
        "operation-id": cid, "x-infa-tid": cid, "x_infa_log_ctx": f"req_id={cid}",
    })
    if r.status_code not in (200, 201, 207):
        return 0, len(items), f"HTTP {r.status_code}: {r.text[:150]}"
    ok = err = 0
    for it in (r.json().get("items") if r.text else []) or []:
        code = int(it.get("statusCode") or 0)
        msg = it.get("messageCode") or ""
        if code in (200, 201) or msg in ("CONTENT_SUCCESS", "RELATIONSHIP_ALREADY_EXISTS"):
            ok += 1
        else:
            err += 1
    return ok, err, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-tables", type=int, default=0)
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()

    colterm = json.load(open("colterm_map.json"))
    term_ids = json.load(open("term_ids.json"))
    files = sorted(glob.glob(f"{CACHE}/GOVERNANCE_SCALE_TEST*.json"))
    if args.limit_tables:
        files = files[:args.limit_tables]

    # Build all link items
    items = []
    no_term = 0
    for cf in files:
        d = json.load(open(cf))
        ext = d.get("external_id", "")
        if "~" not in ext:
            continue
        base = ext.split("~")[0]
        for col in d.get("columns", []):
            cname = col.get("name")
            if not cname:
                continue
            term_name = colterm.get(cname.upper())
            term_id = term_ids.get(term_name) if term_name else None
            if not term_id:
                no_term += 1
                continue
            col_ext = f"{base}/{cname}~{COL_CLASS}"
            items.append({
                "elementType": "RELATIONSHIP",
                "fromIdentity": col_ext,
                "toIdentity": term_id,
                "operation": "INSERT",
                "type": REL_TYPE,
                "sourceIdentityType": "EXTERNAL",
                "targetIdentityType": "INTERNAL",
                "attributes": {"core.curationStatus": "ACCEPTED", "core.inferred": False,
                               "core.channels": ["MANUAL"]},
            })
    print(f"tables={len(files)} link_items={len(items)} skipped_no_term={no_term}")

    batches = [items[i:i + args.batch] for i in range(0, len(items), args.batch)]
    ok = err = 0
    errs = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_publish, b): i for i, b in enumerate(batches)}
        done = 0
        for fut in as_completed(futs):
            o, e, msg = fut.result()
            ok += o; err += e
            if msg:
                errs.append(msg)
            done += 1
            if done % 50 == 0 or done == len(batches):
                print(f"  {done}/{len(batches)} batches | linked={ok} err={err}")
    print(f"\nDONE linked={ok} err={err} skipped_no_term={no_term}")
    if errs:
        print("sample errors:", errs[:3])


if __name__ == "__main__":
    main()
