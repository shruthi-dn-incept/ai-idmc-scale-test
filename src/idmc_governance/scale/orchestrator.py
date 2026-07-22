#!/usr/bin/env python
"""End-to-end scale pipeline orchestrator — runs every governance step across the
full catalog and collects per-phase stats. Designed to run as ONE Azure ACA job
(all steps execute in-container; CDGC/Snowflake/agent reached over the network).

Phases (each timed into stats.json):
  1  extract       CDGC hierarchy + Snowflake types -> .scan_cache
  2  taxonomy      whole-catalog LLM glossary -> taxonomy.json
  3  colterm       unique cols -> term names -> colterm_map.json
  4  domain        create Domain + SubDomains + Business Terms; resolve term_ids.json
  5  system_ds     1 System + N Datasets (per schema)
  6  gen_dqro      generate CDGC_DQRO_FULL.xlsx
  7  import_dqro   3-step bulk import (validate -> submit -> poll)
  8  curate        link columns -> business Data Set
  9  scan          trigger MCC Data Quality scan on all sources
  10 stats         Snowflake credits + write stats.json + results doc

There is deliberately no cleanup phase. Scoped teardown is done with a
delete-operation bulk-import file (generate_dqro --operation Delete), never a
global purge.

Usage (local or in-container):
  python -m idmc_governance.scale.orchestrator                 # full run
  python -m idmc_governance.scale.orchestrator --from 4        # resume from a phase
  python -m idmc_governance.scale.orchestrator --skip scan     # skip phase(s)
  python -m idmc_governance.scale.orchestrator --discover      # auto-find schemas
"""
from idmc_governance.common.paths import STATE_DIR, GOVERNANCE_SYSTEM_NAME
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".env"))
from idmc_governance.servers import ai_governance as aim
from idmc_governance.common import snowflake as snowflake_types

# Catalog list is env-overridable (PIPELINE_SCHEMAS=comma,separated). Default = the
# original four proof-point schemas, so unset behavior is unchanged.
SCHEMAS = [s.strip() for s in os.getenv(
    "PIPELINE_SCHEMAS",
    "GOVTEST_CLAIMS,GOVTEST_MEMBER,GOVTEST_CLINICAL,GOVTEST_PROVIDER",
).split(",") if s.strip()]
ORIGIN = GOVERNANCE_SYSTEM_NAME   # single source of truth (common.paths)
DQRO_FILE = "templates/CDGC_DQRO_FULL.xlsx"
MAX_COLS = int(os.getenv("MAX_COLS", "3"))   # shared with curate_template (same env var)
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


# NOTE: the old phase-0 cleanup (clean()/--clean/--clean-structure) was removed.
# It deleted DQ_* rule instances (and governance assets) GLOBALLY across the whole
# catalog — a footgun that could wipe unrelated catalogs. Scoped teardown is now done
# with a delete-operation bulk-import file instead:
#   python -m idmc_governance.scale.generate_dqro --schemas <SCHEMA...> \
#          --operation Delete --out delete.xlsx
#   # then import delete.xlsx (idmc_governance.scale.bulk_import) — deletes exactly
#   # the listed rows, nothing else.


# ── phase 1: extract ────────────────────────────────────────────────────────
def extract():
    out = _sh(f"{sys.executable} -m idmc_governance.scale.extract_columns --workers 16")
    m = re.search(r"tables_ok=(\d+).*total_columns_in_cache=(\d+)", out)
    n = len(glob.glob(".scan_cache/*.json"))
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
    for f in glob.glob(".scan_cache/*.json"):
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
    # Match cache files by the schema segment of each external_id (DB-agnostic) rather
    # than a hardcoded database-name prefix — works for any source database.
    for cf in glob.glob(".scan_cache/*.json"):
        d = json.load(open(cf))
        ext = d.get("external_id", "")
        if "~" not in ext or "://" not in ext:
            continue
        base = ext.split("~")[0]
        parts = base.split("://", 1)[1].split("/")
        if len(parts) < 2 or parts[1].upper() != schema.upper():
            continue
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
    out = _sh(f"{sys.executable} -m idmc_governance.scale.generate_dqro "
              f"--schemas {' '.join(SCHEMAS)} --max-cols {MAX_COLS} --out {DQRO_FILE}")
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
    status = {s: r.get("final_status") or r.get("error") for s, r in results.items()}
    # Fail the phase if any schema did not link — curate errors used to be swallowed
    # (each schema's exception was caught in ct.run and never re-raised), so the
    # pipeline reported success while 0 columns were linked. Surface it loudly instead.
    if len(ok) < len(SCHEMAS):
        failed = {s: status[s] for s in SCHEMAS if s not in ok}
        raise RuntimeError(f"curate linked {len(ok)}/{len(SCHEMAS)} schemas; failed: {failed}")
    return {"schemas_ok": len(ok), "schemas": len(SCHEMAS),
            "rows": {s: r.get("rows") for s, r in results.items()},
            "status": status}


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


ALL = [("extract", extract), ("taxonomy", taxonomy), ("colterm", colterm),
       ("domain", domain), ("system_ds", system_ds), ("rule_map", rule_map), ("gen_dqro", gen_dqro),
       ("import_dqro", import_dqro), ("curate", curate), ("scan", scan), ("costs", costs)]


def discover_schemas(name_filter: str = "GOVTEST_") -> list[str]:
    """Auto-discover catalog-source/schema names that have catalogued base tables.

    Enumerates CDGC catalog sources, keeps those whose name starts with `name_filter`
    (case-insensitive) AND that expose >0 base Table assets, and skips the rest. This
    means:
      * a freshly-registered source, or one pointed at a views-only database (e.g. the
        GOVTEST_ENROLLMENT → SNOWFLAKE-system-DB mistake), is skipped until it actually
        has tables — no empty governance runs;
      * unrelated connectors (Databricks, S3, ADLS, …) are excluded by the name filter.
    Set name_filter="" to consider ALL sources (use with care).
    """
    nf = (name_filter or "").upper()
    names = sorted({(s.get("name") or "") for s in aim._list_catalog_sources()
                    if (s.get("name") or "").upper().startswith(nf)})
    out = []
    for n in names:
        if not n:
            continue
        try:
            ntables = len(aim._browse_all_tables_in_schema(n))
        except Exception as e:
            print(f"  discover: {n} → browse error ({e!r}); skipping")
            continue
        print(f"  discover: {n} → {ntables} tables {'✓' if ntables else '(skipped)'}")
        if ntables > 0:
            out.append(n)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", type=int, default=1, help="start phase index (1-based over ALL)")
    ap.add_argument("--skip", nargs="*", default=[], help="phase names to skip")
    ap.add_argument("--discover", action="store_true",
                    help="auto-discover schemas with tables (overrides PIPELINE_SCHEMAS)")
    ap.add_argument("--schema-filter", default=os.getenv("PIPELINE_SCHEMA_FILTER", "GOVTEST_"),
                    help="name-prefix filter for --discover (default GOVTEST_; '' = all sources)")
    args = ap.parse_args()

    if args.discover:
        global SCHEMAS
        print(f"=== discovering schemas (filter={args.schema_filter!r}) ===")
        SCHEMAS = discover_schemas(args.schema_filter)
        if not SCHEMAS:
            print("=== discovery found no schemas with tables; nothing to do ===")
            sys.exit(1)
        # Propagate to the extract subprocess (reads PIPELINE_SCHEMAS) so it browses the
        # same set the write phases will operate on.
        os.environ["PIPELINE_SCHEMAS"] = ",".join(SCHEMAS)
        print(f"=== discovered {len(SCHEMAS)} schema(s): {SCHEMAS} ===")

    print("=== SCALE PIPELINE start ===")
    for i, (name, fn) in enumerate(ALL, start=1):
        if i < args.start or name in args.skip:
            print(f"-- skip {name}")
            continue
        phase(name, fn)

    STATS["ended"] = int(time.time())
    STATS["total_seconds"] = STATS["ended"] - STATS["started"]
    _save_stats()
    print(f"\n=== PIPELINE DONE in {STATS['total_seconds']}s ===")
    print(json.dumps({k: v.get("seconds") for k, v in STATS["phases"].items()}, indent=1))

    # Exit non-zero if ANY phase failed. Phases are individually fault-tolerant (a
    # failure is recorded and the run continues to collect what it can), but the
    # process must NOT report overall success when a phase errored — otherwise ACA/CI
    # shows the job as Succeeded and silent failures (e.g. curate linking 0 columns)
    # go unnoticed. STOP_ON_ERROR semantics live here, at the exit code.
    failed = [name for name, info in STATS["phases"].items() if not info.get("ok")]
    if failed:
        print(f"=== PIPELINE FAILED: {len(failed)} phase(s) errored: {failed} ===")
        sys.exit(1)


if __name__ == "__main__":
    main()
