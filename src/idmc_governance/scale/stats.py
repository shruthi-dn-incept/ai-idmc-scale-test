#!/usr/bin/env python
"""Collect proper end-to-end stats for the Sameer email from ground truth:
verified counts (from each step's authoritative output), exact measured timings
(from Azure execution + process times this session), live DQ-scan status, and
Snowflake credits. Writes stats.json.
"""
from idmc_governance.common.paths import STATE_DIR
import json
import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".env"))
from idmc_governance.servers import ai_governance as aim
from idmc_governance.common import snowflake as snowflake_types

# ── verified counts (authoritative source in parentheses) ───────────────────
counts = {
    "schemas": 4,
    "tables": 3999,                 # extractor DONE tables_ok
    "columns": 137637,              # extractor total_columns_in_cache
    "unique_column_names": 108,     # vocabulary
    "domain": 1, "subdomains": 8, "business_terms": 49,   # taxonomy
    "system": 1, "datasets": 4,
    "dqros": 24120,                 # DQRO import validated insertCount
    "curate_links_submitted": 135960,   # curate_scale link_items
}

# exact measured wall-clock (seconds) — ALL from the single-environment Azure run
# (govtest-pipeline-job-g9ne81i) except import which ran as its own Azure job.
timings = {
    "extract_columns": 159,         # Azure: "extract done in 158.6s" (local was 691s -> 4.4x faster)
    "snowflake_type_map": 1,        # INFORMATION_SCHEMA single query
    "taxonomy": 60,                 # LLM (Anthropic API, location-independent)
    "domain_structure": 121,        # Azure: "domain done in 120.9s"
    "system_datasets": 12,          # Azure: "system_ds done in 11.7s"
    "generate_dqro": 5,
    "dqro_validate": 11,            # validation insert=24120
    "dqro_import_azure": 1333,      # Azure job 13:44:16 -> 14:06:29 UTC = 22.2 min
    "curate": 2481,                 # Azure: "curate done in 2481.0s" (publish-bound; ~ local)
}

infra = {
    "orchestrator": "Azure Container Apps Job (govtest-env, East US 2), 2 vCPU / 4 GB",
    "agent_vm": "govtest-agent-vm, Standard_D8s_v3 (8 vCPU / 32 GB), East US",
    "registry": "govtestscaleacr.azurecr.io (East US)",
    "snowflake": "INCEPT_WH, account ygc42528.us-east-1",
    "informatica": "CDGC + MCC, US region (dmp-us)",
    "subscription": "Pay-As-You-Go 7a42e0f2-3b2f-4b16-8bf2-458746103d58",
}


def scan_status():
    jobs = json.load(open(str(STATE_DIR / "mcc_scan_jobs.json")))
    out = {}
    for src, jid in jobs.items():
        if isinstance(jid, dict):
            jid = jid.get("job_id")
        try:
            r = aim._request_cdgc("GET", f"{aim.CDGC_API_BASE}/data360/observable/v1/jobs/{jid}")
            j = r.json() if r.text else {}
            out[src] = {"job_id": jid, "status": j.get("status"),
                        "start": str(j.get("startTime"))[:19], "end": str(j.get("endTime"))[:19]}
        except Exception as e:
            out[src] = {"job_id": jid, "error": str(e)[:100]}
    return out


def credits():
    try:
        conn = snowflake_types.make_conn(); cur = conn.cursor()
        cur.execute("""SELECT ROUND(SUM(CREDITS_USED),3)
            FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
            WHERE WAREHOUSE_NAME='INCEPT_WH' AND START_TIME >= DATEADD('hour',-12,CURRENT_TIMESTAMP())""")
        c = cur.fetchone()[0]; conn.close()
        return {"credits_12h": float(c) if c is not None else 0.0,
                "note": "ACCOUNT_USAGE ~3h latency; DQ-scan credits still accruing"}
    except Exception as e:
        return {"error": str(e)[:150]}


def main():
    stats = {
        "counts": counts,
        "timings_seconds": timings,
        "active_pipeline_minutes": round(sum(timings.values()) / 60, 1),
        "infra": infra,
        "dq_scans": scan_status(),
        "snowflake": credits(),
        "throughput": {
            "extract_tables_per_min": round(counts["tables"] / (timings["extract_columns"] / 60)),
            "dqro_per_min_import": round(counts["dqros"] / (timings["dqro_import_azure"] / 60)),
            "curate_links_per_min": round(counts["curate_links_submitted"] / (timings["curate"] / 60)),
        },
    }
    json.dump(stats, open(str(STATE_DIR / "stats.json"), "w"), indent=1)
    print(json.dumps(stats, indent=1))


if __name__ == "__main__":
    main()
