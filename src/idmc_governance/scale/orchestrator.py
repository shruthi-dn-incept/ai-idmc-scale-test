#!/usr/bin/env python
"""End-to-end scale pipeline orchestrator — runs every governance step across the
full catalog and collects per-phase stats. Designed to run as ONE Azure ACA job
(all steps execute in-container; CDGC/Snowflake/agent reached over the network).

Phases (each timed into stats.json):
  0  cleanup       (optional --clean)  delete prior DQROs [+ --clean-structure for the 63 assets]
  1  extract       CDGC hierarchy + Snowflake types -> .scan_cache
  2  taxonomy      whole-catalog LLM glossary -> taxonomy.json
  3  colterm       108 unique cols -> term names -> colterm_map.json
  4  domain        create Domain + SubDomains + Business Terms; resolve term_ids.json
  5  system_ds     1 System + N Datasets (per schema)
  6  gen_dqro      generate CDGC_DQRO_FULL.xlsx
  7  import_dqro   3-step bulk import (validate -> submit -> poll)
  8  curate        publish ~136k column->term links
  9  scan          trigger MCC Data Quality scan on all sources
  10 stats         Snowflake credits + write stats.json + results doc

Usage (local or in-container):
  python run_scale_pipeline.py --clean
  python run_scale_pipeline.py --from 4         # resume from a phase
  python run_scale_pipeline.py --skip scan      # skip phase(s)
"""
from idmc_governance.common.paths import STATE_DIR
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".env"))
from idmc_governance.servers import ai_governance as aim
from idmc_governance.servers import governance_engine as gem
from idmc_governance.common import snowflake as snowflake_types

SCHEMAS = ["GOVTEST_CLAIMS", "GOVTEST_MEMBER", "GOVTEST_CLINICAL", "GOVTEST_PROVIDER"]
ORIGIN = "GOVERNANCE_SCALE_TEST"
DQRO_FILE = "templates/CDGC_DQRO_FULL.xlsx"
MAX_COLS = 3
STATS = {"phases": {}, "started": int(time.time())}


def _sh(cmd):
    """Run a script step; stream+capture stdout; return combined text."""
    print(f"    $ {cmd}")
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    out = (p.stdout or "") + (p.stderr or "")
    tail = "\n".join(l for l in out.splitlines()
                     if "INFO httpx" not in l and "INFO mcp" not in l)[-1500:]
    print(tail)
    if p.returncode != 0:
        raise RuntimeError(f"step failed ({p.returncode}): {cmd}")
    return out


def phase(name, fn):
    print(f"\n=== PHASE {name} ===")
    t0 = time.time()
    try:
        info = fn() or {}
        STATS["phases"][name] = {"ok": True, "seconds": round(time.time() - t0, 1), **info}
    except Exception as e:
        STATS["phases"][name] = {"ok": False, "seconds": round(time.time() - t0, 1), "error": str(e)[:300]}
        print(f"    PHASE {name} ERROR: {e}")
    _save_stats()
    print(f"=== {name} done in {STATS['phases'][name]['seconds']}s ===")


def _save_stats():
    json.dump(STATS, open(str(STATE_DIR / "stats.json"), "w"), indent=1)


# ── phase 0: cleanup ────────────────────────────────────────────────────────
# CDGC deletes must target core.externalId (DQO-/BT-/SDOM-/DOM-/DS-…) WITH the
# X-INFA-PRODUCT-ID:CDGC header. The old path deleted by core.identity (UUID) → 404,
# so --clean never actually purged and assets accumulated across every run.
_PROD_HDR = {"X-INFA-PRODUCT-ID": "CDGC"}
_GOV_CLASSES = {
    "BusinessTerm": "com.infa.ccgf.models.governance.BusinessTerm",
    "SubDomain":    "com.infa.ccgf.models.governance.SubDomain",
    "Domain":       "com.infa.ccgf.models.governance.Domain",
    "DataSet":      "com.infa.ccgf.models.governance.DataSet",
}


def _ext_of(h):
    sa = h.get("systemAttributes") or {}
    return h.get("core.externalId") or sa.get("core.externalId") or ""


def _delete_asset(ext_id):
    """Delete one asset by core.externalId. 201 (with a 'deleted' messageCode) = success."""
    if not ext_id:
        return False
    try:
        r = gem._request_cdgc(
            "DELETE",
            f"{gem.CDGC_API_BASE}/data360/content/v1/assets/{ext_id}",
            headers=_PROD_HDR,
        )
        return r.status_code in (200, 201, 202, 204)
    except Exception:
        return False


def _list_externalids(class_type, max_results=40000):
    """All core.externalIds of a class via server-side filterSpec — avoids the
    relevance-keyword miss (e.g. 'BusinessTerm' matches no term names) and the
    10k wildcard deep-pagination cap (filtered result sets stay well under it)."""
    url = (f"{gem.CDGC_API_BASE}/data360/search/v1/assets"
           f"?knowledgeQuery=*&segments=summary,systemAttributes")
    out, seen, offset, page = [], set(), 0, 100
    while len(out) < max_results:
        body = {"from": offset, "size": page,
                "filterSpec": [{"type": "simple", "attribute": "core.classType",
                                "values": [class_type]}]}
        r = gem._request_cdgc("POST", url, json=body)
        if r.status_code >= 400:
            break
        hits = (r.json() or {}).get("hits", [])
        if isinstance(hits, dict):
            hits = hits.get("hits", [])
        if not hits:
            break
        for h in hits:
            eid = _ext_of(h)
            if eid and eid not in seen:
                seen.add(eid)
                out.append(eid)
        if len(hits) < page:
            break
        offset += page
    return out


def _delete_many(ext_ids, workers=10):
    if not ext_ids:
        return 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return sum(1 for ok in ex.map(_delete_asset, ext_ids) if ok)


def clean(structure=False):
    # DQ_* rule instances always (they duplicate every run). All named DQ_* and
    # under 10k, so the keyword search is reliable here.
    dq = [_ext_of(h) for h in aim._cdgc_search_paged("DQ_", max_results=40000)
          if (aim._name_of(h) or "").startswith("DQ_")
          and "RuleInstance" in ((h.get("systemAttributes") or {}).get("core.classType") or "")]
    out = {"dqros_found": len(dq), "dqros_deleted": _delete_many(dq, workers=12)}
    if structure:
        # children-first so a parent isn't blocked by a live child link. System is
        # deliberately NOT purged — those are catalog source systems the scan needs.
        for label in ("BusinessTerm", "SubDomain", "Domain", "DataSet"):
            ids = _list_externalids(_GOV_CLASSES[label])
            out[f"{label}_found"] = len(ids)
            out[f"{label}_deleted"] = _delete_many(ids, workers=8)
            print(f"  clean {label}: found {len(ids)}, deleted {out[f'{label}_deleted']}")
    return out


# ── phase 1: extract ────────────────────────────────────────────────────────
def extract():
    out = _sh(f"{sys.executable} -m idmc_governance.scale.extract_columns --workers 16")
    m = re.search(r"tables_ok=(\d+).*total_columns_in_cache=(\d+)", out)
    n = len(glob.glob(".scan_cache/GOVERNANCE_SCALE_TEST*.json"))
    return {"tables": int(m.group(1)) if m else n, "columns": int(m.group(2)) if m else None,
            "cache_files": n}


# ── phase 2: taxonomy ───────────────────────────────────────────────────────
def taxonomy():
    _sh(f"{sys.executable} -m idmc_governance.scale.taxonomy")
    tax = json.load(open(str(STATE_DIR / "taxonomy.json")))
    nt = sum(len(s.get("business_terms", [])) for d in tax["domains"] for s in d.get("subdomains", []))
    return {"domains": len(tax["domains"]), "terms": nt}


# ── phase 3: colterm map ────────────────────────────────────────────────────
def colterm():
    tax = json.load(open(str(STATE_DIR / "taxonomy.json")))
    terms = [b["name"] for d in tax["domains"] for s in d.get("subdomains", [])
             for b in s.get("business_terms", [])]
    cols = set()
    for f in glob.glob(".scan_cache/GOVERNANCE_SCALE_TEST*.json"):
        for c in json.load(open(f)).get("columns", []):
            cols.add(c["name"].upper())
    cols = sorted(cols)
    sysp = ("You map technical column names to business term names. Return ONLY JSON object "
            "{COLUMN_NAME: term_name}. Assign EVERY column to the single best-fitting term. "
            "Use exact term names from the list.")
    usr = f"Business terms:\n{json.dumps(terms)}\n\nColumns:\n{json.dumps(cols)}"
    m = aim._llm_json(sysp, usr, model=aim._MODEL_QUALITY)
    if isinstance(m, dict) and "mappings" in m:
        m = m["mappings"]
    m = {k.upper(): v for k, v in m.items()}
    json.dump(m, open(str(STATE_DIR / "colterm_map.json"), "w"), indent=1)
    return {"unique_columns": len(cols), "mapped": len([c for c in cols if c in m])}


# ── phase 4: domain structure ───────────────────────────────────────────────
def domain():
    tax = json.load(open(str(STATE_DIR / "taxonomy.json")))
    res = aim.create_domain_structure(tax, dry_run=False)
    created = Counter(c["type"] for c in res.get("created", []))
    # resolve term ids
    terms = [b["name"] for d in tax["domains"] for s in d.get("subdomains", [])
             for b in s.get("business_terms", [])]
    allbt = aim._cdgc_search_paged("term", class_type="BusinessTerm", max_results=2000)
    byname = {}
    for h in allbt:
        n = (aim._name_of(h) or "").lower()
        byname.setdefault(n, aim._id_of(h))
    termids = {}
    for name in terms:
        tid = byname.get(name.lower())
        if not tid:
            resp = aim._cdgc_create_asset(aim.CLASS_BUSINESS_TERM, name, f"{name} business term")
            tid = resp.get("core.identity") or resp.get("id") or ""
        if tid:
            termids[name] = tid
    json.dump(termids, open(str(STATE_DIR / "term_ids.json"), "w"), indent=1)
    return {"created": dict(created), "term_ids": len(termids)}


# ── phase 5: system + datasets ──────────────────────────────────────────────
_COL_CLASS = "com.infa.odin.models.relational.Column"


def _dq_column_refs(schema):
    """External ids of the DQ'd (potential) columns for a schema (same selection as DQROs)."""
    from idmc_governance.scale.generate_dqro import select_key_columns
    refs = []
    for cf in glob.glob(f".scan_cache/GOVERNANCE_SCALE_TEST_{schema}*.json"):
        d = json.load(open(cf))
        ext = d.get("external_id", "")
        if "~" not in ext:
            continue
        base = ext.split("~")[0]
        for col in select_key_columns(
                [c for c in d.get("columns", []) if not (c.get("name") or "").startswith("SYS_")],
                max_cols=MAX_COLS):
            refs.append(f'{base}/{col["name"]}~{_COL_CLASS}')
    return refs


def system_ds():
    # Create the System + one Dataset per schema (STRUCTURE ONLY). Column->dataset
    # linking is deferred to the curate phase, which uses the fast bulk-import path
    # (curate_template). Passing table_ids here would link via the publish linker,
    # which hits the propagation-cap 429s (~0.4 batches/min) — redundant + slow now
    # that curate does the same linking in one managed job per schema.
    out = {}
    for sch in SCHEMAS:
        ds = sch.replace("GOVTEST_", "").title() + " Dataset"
        r = aim.create_system_and_dataset(system_name=ORIGIN, dataset_name=ds,
                                          description=f"{sch} dataset", domain_name="Healthcare",
                                          table_ids=None)
        dsid = (r.get("dataset") or {}).get("id", "")
        out[sch] = dsid
        print(f"  {sch}: dataset={str(dsid)[:12]} (structure only; linking deferred to curate)")
    json.dump(out, open(str(STATE_DIR / "system_dataset.json"), "w"), indent=1)
    return {"system": 1, "datasets": len([v for v in out.values() if v]),
            "note": "columns linked in curate phase (bulk import)"}


# ── phase 5b: rule map (rule-spec ids per dimension -> state/rule_map.json) ──
# Required by gen_dqro (Technical Rule Reference). Read-only CDGC query.
def rule_map():
    out = _sh(f"{sys.executable} -m idmc_governance.scale.rule_map")
    m = re.search(r"wrote rule_map\.json  \((\d+)/(\d+) dimensions", out)
    return {"dimensions_covered": int(m.group(1)) if m else None}


# ── phase 6: generate DQRO file ─────────────────────────────────────────────
def gen_dqro():
    out = _sh(f"{sys.executable} -m idmc_governance.scale.generate_dqro --origin-filter {ORIGIN} "
              f"--max-cols {MAX_COLS} --out {DQRO_FILE}")
    m = re.search(r"wrote (\d+) DQRO rows", out)
    return {"dqro_rows": int(m.group(1)) if m else None}


# ── phase 7: bulk import DQROs ──────────────────────────────────────────────
def import_dqro():
    from idmc_governance.scale import bulk_import as imp
    val = imp.validate_upload(DQRO_FILE)
    fid = val.get("fileId")
    ins = sum(int(s.get("insertCount", 0)) for s in val.get("summary", []))
    sub = imp.submit_import(fid, f"{os.path.basename(DQRO_FILE)}_import", "STOP_ON_ERROR")
    jid = sub.get("jobId")
    final = imp.poll_job(jid, timeout_s=5400, interval_s=20)
    return {"validated_insert": ins, "job_id": jid,
            "final_status": final.get("status") or final.get("lifecycleStatus")}


# ── phase 8: curate ─────────────────────────────────────────────────────────
def curate():
    # Template/bulk-import path: build a "Technical Data element" file per schema
    # (Business Dataset + Operation=Update) and bulk-import it. One managed job per
    # schema, no propagation-cap 429s — vs the old publish linker (~0.4 batches/min,
    # 429 storms). See idmc_governance.scale.curate_template.
    from idmc_governance.scale import curate_template as ct
    results = ct.run(SCHEMAS)
    ok = [s for s, r in results.items()
          if str(r.get("final_status", "")).upper() in {"COMPLETED", "SUCCESS", "SUCCEEDED"}]
    return {"schemas_ok": len(ok), "schemas": len(SCHEMAS),
            "rows": {s: r.get("rows") for s, r in results.items()},
            "status": {s: r.get("final_status") or r.get("error") for s, r in results.items()}}


# ── phase 9: trigger scans ──────────────────────────────────────────────────
def scan():
    jobs = {}
    for sch in SCHEMAS:
        r = aim.run_mcc_scan(catalog_source_name=sch, capabilities=["Data Quality"])
        jobs[sch] = {"job_id": r.get("job_id"), "status": r.get("status"), "error": r.get("error")}
    json.dump(jobs, open(str(STATE_DIR / "mcc_scan_jobs.json"), "w"), indent=1)
    return {"scans": jobs}


# ── phase 10: cost stats ────────────────────────────────────────────────────
def costs():
    try:
        conn = snowflake_types.make_conn(); cur = conn.cursor()
        cur.execute("""SELECT ROUND(SUM(CREDITS_USED),3)
            FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
            WHERE START_TIME >= DATEADD('hour',-6,CURRENT_TIMESTAMP())""")
        credits = cur.fetchone()[0]; conn.close()
        return {"snowflake_credits_6h": float(credits) if credits is not None else 0.0,
                "note": "ACCOUNT_USAGE has ~3h latency; final DQ-scan credits accrue post-scan"}
    except Exception as e:
        return {"error": str(e)[:200]}


ALL = [("cleanup", None), ("extract", extract), ("taxonomy", taxonomy), ("colterm", colterm),
       ("domain", domain), ("system_ds", system_ds), ("rule_map", rule_map), ("gen_dqro", gen_dqro),
       ("import_dqro", import_dqro), ("curate", curate), ("scan", scan), ("costs", costs)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="run cleanup phase 0 (delete prior DQROs)")
    ap.add_argument("--clean-structure", action="store_true", help="also delete 63 structural assets")
    ap.add_argument("--from", dest="start", type=int, default=1, help="start phase index (1-based over ALL[1:])")
    ap.add_argument("--skip", nargs="*", default=[], help="phase names to skip")
    args = ap.parse_args()

    print(f"=== SCALE PIPELINE start (clean={args.clean}) ===")
    if args.clean:
        phase("cleanup", lambda: clean(structure=args.clean_structure))

    steps = ALL[1:]  # skip cleanup entry (handled above)
    for i, (name, fn) in enumerate(steps, start=1):
        if i < args.start or name in args.skip:
            print(f"-- skip {name}")
            continue
        phase(name, fn)

    STATS["ended"] = int(time.time())
    STATS["total_seconds"] = STATS["ended"] - STATS["started"]
    _save_stats()
    print(f"\n=== PIPELINE DONE in {STATS['total_seconds']}s ===")
    print(json.dumps({k: v.get("seconds") for k, v in STATS["phases"].items()}, indent=1))


if __name__ == "__main__":
    main()
