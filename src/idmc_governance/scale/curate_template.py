#!/usr/bin/env python
"""Curate (link scanned columns -> their business Data Set) via CDGC bulk IMPORT.

This is the fast, propagation-cap-free curation path. Instead of the per-publish
``asscDataSetDataElement`` loop (curate.py, which hits the tenant's 20 in-flight
propagation-job cap -> HTTP 429 storms, ~0.4 batches/min), it builds one
"Technical Data element" import file per schema with the ``Business Dataset``
attribute set + ``Operation=Update`` and bulk-imports it. A single import job
links a whole schema's DQ'd columns in ~8 min with no 429s.

Proven 2026-07-10: Provider 2997 columns -> "Provider Dataset | DS-23" imported
COMPLETED in 8m40s, zero failures.

Per column the import requires (validation does NOT catch these — only the import
job gates on them):
  * ``core_Origin``  = the source origin UUID
  * ``Identity``     = the column's INTERNAL asset UUID (resolved from CDGC)
External ``Reference ID`` alone is not sufficient for import.

Row geometry fields (mirrors a real CDGC export so the import matcher is happy):
  Reference ID, Name, Catalog Source Name/Type, Parent, Asset Type, Lifecycle,
  Business Dataset, Position, Reference, HierarchicalPath, Operation,
  core_Location, core_Origin, Identity.
"""
import argparse
import glob
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import openpyxl

from idmc_governance.common.paths import STATE_DIR, GOVERNANCE_SYSTEM_NAME
from idmc_governance.scale import bulk_import as imp
from idmc_governance.scale.generate_dqro import select_key_columns
from idmc_governance.servers import ai_governance as aim
from idmc_governance.servers import governance_engine as gem

ORIGIN = GOVERNANCE_SYSTEM_NAME   # single source of truth (common.paths)
COL_CLASS = "com.infa.odin.models.relational.Column"
MAX_COLS = int(os.environ.get("MAX_COLS", "3"))
IDENT_WORKERS = int(os.environ.get("CURATE_IDENT_WORKERS", "12"))

# Exact 40-column header of the CDGC "Technical Data element" asset sheet.
HEADERS = ["Reference ID", "Name", "Catalog Source Name", "Catalog Source Type",
           "Parent: Technical Data Set", "Asset Type", "Description", "Technical Description",
           "Lifecycle", "Business Name", "Business Dataset", "Glossaries: Recommended",
           "Glossaries: Accepted", "Automatic Assignment", "Glossaries: Rejected",
           "Generated Classifications: Recommended", "Generated Classifications: Rejected",
           "Data Classifications: Accepted", "Data Classifications: Rejected", "Position",
           "Reference", "Stakeholder: Governance Owner", "Stakeholder: Governance Administrator",
           "Stakeholder: Domain Owner", "Stakeholder: Business Data Steward",
           "Stakeholder: Business_Steward", "Stakeholder: Source System Owner",
           "Stakeholder: Technical Data Steward", "Stakeholders Type", "Referenced Glossary",
           "Reference Data", "HierarchicalPath", "Operation", "Created On", "Created By",
           "Modified On", "Modified By", "Identity", "core_Location", "core_Origin"]
_IDX = {h: i for i, h in enumerate(HEADERS)}


def _dataset_business_value(schema: str) -> tuple[str, str]:
    """Return (dataset_name, "<dataset_name> | <core.externalId>") for a schema's Data Set.

    The Data Set is the schema's dataset under the GOVERNANCE_SCALE_TEST system;
    its business reference (e.g. "DS-20") is the asset's top-level core.externalId.
    """
    dataset_name = schema.replace("GOVTEST_", "").title() + " Dataset"
    system_hits = aim._cdgc_search(ORIGIN, size=5)
    system_id = next((aim._id_of(h) for h in system_hits
                      if aim._name_of(h).lower() == ORIGIN.lower()
                      and "System" in ((h.get("systemAttributes") or {}).get("core.classType") or "")), "")
    if not system_id:
        raise RuntimeError(f"system {ORIGIN!r} not found")
    hier = aim._cdgc_get_asset(system_id, segments="summary,hierarchy").get("hierarchy") or []
    if isinstance(hier, dict):
        hier = hier.get("children") or hier.get("items") or []
    ds = next((h for h in hier
               if ((h.get("summary") or {}).get("core.name") or aim._name_of(h)) == dataset_name), None)
    if not ds:
        raise RuntimeError(f"dataset {dataset_name!r} not found under {ORIGIN}")
    ds_full = aim._cdgc_get_asset(aim._id_of(ds), segments="summary,systemAttributes")
    ext = ds_full.get("core.externalId") or ""
    if not ext:
        raise RuntimeError(f"dataset {dataset_name!r} has no core.externalId (business ref)")
    return dataset_name, f"{dataset_name} | {ext}"


def _selected_columns(schema: str) -> list[dict]:
    """DQ'd column selection for a schema (same rule as DQROs), from local .scan_cache.

    Match cache files by the schema segment of each external_id path, and derive the
    database name from that path rather than assuming it equals ORIGIN. This lets sources
    in ANY database work — the original four in GOVERNANCE_SCALE_TEST and the newer
    catalogs in GOVERNANCE_SCALE_TEST_C. For the original DB (db == ORIGIN) the generated
    core_Location / HierarchicalPath are byte-identical to the previous behavior.
    """
    rows = []
    for cf in sorted(glob.glob(".scan_cache/*.json")):
        d = json.load(open(cf))
        ext = d.get("external_id", "")
        if "~" not in ext or "://" not in ext:
            continue
        base = ext.split("~")[0]                 # <origin_uuid>://DB/SCHEMA/TABLE
        origin = base.split("://", 1)[0]
        parts = base.split("://", 1)[1].split("/")
        if len(parts) < 3 or parts[1].upper() != schema.upper():
            continue
        db, table = parts[0], parts[-1]
        all_cols = d.get("columns", [])
        pos_of = {c.get("name"): i + 1 for i, c in enumerate(all_cols)}
        cols = [c for c in all_cols if not (c.get("name") or "").startswith("SYS_")]
        for col in select_key_columns(cols, max_cols=MAX_COLS):
            cn = col["name"]
            rows.append({"refid": f"{base}/{cn}~{COL_CLASS}", "name": cn, "table": table,
                         "origin": origin,
                         "location": f"{origin}://{origin}/{db}/{schema}/{table}/{cn}",
                         "hpath": f"{schema}/{db}/{schema}/{table}/{cn}",
                         "position": str(pos_of.get(cn, ""))})
    return rows


def _resolve_identities(schema: str, tables: set[str]) -> dict[str, dict[str, str]]:
    """Map {table_name: {column_name: internal core.identity}} via CDGC hierarchy browse."""
    browsed = aim._browse_all_tables_in_schema(schema)

    def _tname(h):
        e = (h.get("summary") or {}).get("core.externalId") or h.get("core.externalId") or ""
        return e.split("~")[0].rsplit("/", 1)[-1]

    tid = {_tname(t): aim._id_of(t) for t in browsed}

    def fetch(tn):
        det = aim._cdgc_get_asset(tid[tn], segments="summary,systemAttributes,hierarchy")
        hier = det.get("hierarchy") or []
        if isinstance(hier, dict):
            hier = hier.get("children") or hier.get("items") or []
        m = {}
        for c in hier:
            nm = (c.get("summary") or {}).get("core.name") or c.get("core.name")
            if nm:
                m[nm] = aim._id_of(c)
        return tn, m

    out: dict[str, dict[str, str]] = {}
    with ThreadPoolExecutor(max_workers=IDENT_WORKERS) as ex:
        futs = {ex.submit(fetch, tn): tn for tn in tables if tn in tid}
        for f in as_completed(futs):
            tn, m = f.result()
            out[tn] = m
    return out


def build_curate_file(schema: str, out_path: str) -> dict:
    """Build a Technical Data element import file linking a schema's DQ'd columns to its Data Set."""
    dataset_name, business_value = _dataset_business_value(schema)
    rows = _selected_columns(schema)
    if not rows:
        raise RuntimeError(f"no DQ'd columns found in .scan_cache for {schema}")
    ident = _resolve_identities(schema, {r["table"] for r in rows})
    missing = [r for r in rows if not ident.get(r["table"], {}).get(r["name"])]

    wb = openpyxl.Workbook()
    toc = wb.active
    toc.title = "Table of Contents"
    toc.append(["Sheet", "Asset Type", "Asset Count"])
    toc.append(["Technical Data element", "core.DataElement", f"Column({len(rows)})"])
    ws = wb.create_sheet("Technical Data element")
    ws.append(HEADERS)
    for x in rows:
        r = [None] * len(HEADERS)
        r[_IDX["Reference ID"]] = x["refid"]
        r[_IDX["Name"]] = x["name"]
        r[_IDX["Catalog Source Name"]] = schema
        r[_IDX["Catalog Source Type"]] = "Snowflake"
        r[_IDX["Parent: Technical Data Set"]] = x["table"]
        r[_IDX["Asset Type"]] = COL_CLASS
        r[_IDX["Lifecycle"]] = "Published"
        r[_IDX["Business Dataset"]] = business_value
        r[_IDX["Position"]] = x["position"]
        r[_IDX["Reference"]] = "false"
        r[_IDX["HierarchicalPath"]] = x["hpath"]
        r[_IDX["Operation"]] = "Update"
        r[_IDX["core_Location"]] = x["location"]
        r[_IDX["core_Origin"]] = x["origin"]
        r[_IDX["Identity"]] = ident.get(x["table"], {}).get(x["name"], "")
        ws.append(r)
    wb.save(out_path)
    return {"rows": len(rows), "tables": len({r["table"] for r in rows}),
            "missing_identity": len(missing), "business_dataset": business_value}


def curate_schema(schema: str, out_dir: str = "state") -> dict:
    """Build + bulk-import one schema's curation file; poll to terminal status."""
    out_path = os.path.join(out_dir, f"curate_{schema}.xlsx")
    built = build_curate_file(schema, out_path)
    val = imp.validate_upload(out_path)
    s = (val.get("summary") or [{}])[0]
    sub = imp.submit_import(val["fileId"], f"curate_{schema}", "STOP_ON_ERROR")
    jid = sub.get("jobId")
    final = imp.poll_job(jid, timeout_s=5400, interval_s=20)
    status = final.get("status") or final.get("lifecycleStatus")
    return {"schema": schema, **built,
            "validated_update": s.get("updateCount"),
            "job_id": jid, "final_status": status}


def run(schemas: list[str]) -> dict:
    results = {}
    for sch in schemas:
        t0 = time.time()
        try:
            r = curate_schema(sch)
            r["seconds"] = int(time.time() - t0)
            print(f"  {sch}: rows={r['rows']} missing_id={r['missing_identity']} "
                  f"status={r['final_status']} ({r['seconds']}s)")
        except Exception as e:
            r = {"schema": sch, "error": str(e)[:300]}
            print(f"  {sch}: ERROR {e}")
        results[sch] = r
    json.dump(results, open(str(STATE_DIR / "curate_template.json"), "w"), indent=1)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--schemas", nargs="*",
                    default=["GOVTEST_CLAIMS", "GOVTEST_MEMBER", "GOVTEST_CLINICAL", "GOVTEST_PROVIDER"])
    ap.add_argument("--build-only", action="store_true", help="build files + validate, do not import")
    args = ap.parse_args()
    if args.build_only:
        for sch in args.schemas:
            out = os.path.join("state", f"curate_{sch}.xlsx")
            b = build_curate_file(sch, out)
            v = imp.validate_upload(out)
            s = (v.get("summary") or [{}])[0]
            print(f"{sch}: built {b['rows']} rows (missing_id={b['missing_identity']}) "
                  f"-> validate update={s.get('updateCount')} bd={b['business_dataset']!r}")
        return 0
    run(args.schemas)
    return 0


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    sys.exit(main())
