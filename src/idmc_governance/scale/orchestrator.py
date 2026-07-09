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
def _delete_asset(aid):
    try:
        r = gem._request_cdgc("DELETE", f"{gem.CDGC_API_BASE}/data360/content/v1/assets/{aid}")
        return r.status_code in (200, 202, 204)
    except Exception:
        return False


def clean(structure=False):
    # Delete prior DQ_* rule occurrences (the only assets that duplicate on re-run)
    hits = aim._cdgc_search_paged("DQ_", max_results=40000)
    dqros = [aim._id_of(h) for h in hits
             if (aim._name_of(h) or "").startswith("DQ_")
             and "RuleInstance" in ((h.get("systemAttributes") or {}).get("core.classType") or "")]
    deleted = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        for ok in ex.map(_delete_asset, dqros):
            deleted += 1 if ok else 0
    out = {"dqros_found": len(dqros), "dqros_deleted": deleted}
    if structure:
        struct = []
        for cls in ("BusinessTerm", "SubDomain", "DataSet", "System"):
            for h in aim._cdgc_search_paged(cls, class_type=cls, max_results=500):
                struct.append(aim._id_of(h))
        sdel = sum(1 for ok in ThreadPoolExecutor(max_workers=8).map(_delete_asset, struct) if ok)
        out["structure_deleted"] = sdel
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
def system_ds():
    out = {}
    for sch in SCHEMAS:
        ds = sch.replace("GOVTEST_", "").title() + " Dataset"
        r = aim.create_system_and_dataset(system_name=ORIGIN, dataset_name=ds,
                                          description=f"{sch} dataset", domain_name="Healthcare")
        out[sch] = (r.get("dataset") or {}).get("id", "")
    json.dump(out, open(str(STATE_DIR / "system_dataset.json"), "w"), indent=1)
    return {"system": 1, "datasets": len([v for v in out.values() if v])}


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
    out = _sh(f"{sys.executable} -m idmc_governance.scale.curate --batch 50 --workers 14")
    m = re.search(r"DONE linked=(\d+) err=(\d+)", out)
    mi = re.search(r"link_items=(\d+)", out)
    return {"link_items": int(mi.group(1)) if mi else None,
            "linked": int(m.group(1)) if m else None, "errors": int(m.group(2)) if m else None}


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
