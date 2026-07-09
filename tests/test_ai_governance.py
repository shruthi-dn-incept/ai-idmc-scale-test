"""test_ai_governance.py — sequential live test of all ai_governance_mcp tools.

Usage:
  python test_ai_governance.py [--step N]  # run from step N onward (default: 1)
  python test_ai_governance.py --step 2    # skip scan, start at taxonomy

Steps:
  1  scan_mcc_source            (Tool 1)
  2  generate_governance_taxonomy (Tool 2)
  3  create_domain_structure    (Tool 3, dry_run then live)
  4  create_system_and_dataset  (Tool 4)
  5  curate_assets_with_glossary (Tool 5, dry_run)
  6  run_mcc_scan               (Tool 6, list-sources only)
  7  propagate_dq_score         (Tool 7, dry-run via non-existent asset)
  8  onboard_and_govern         (Tool 8, dry_run)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Bootstrap .env before importing the module
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"


def _load_env():
    if not ENV_PATH.exists():
        return
    for raw in ENV_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env()

# Import module functions directly (no MCP transport needed for tests)
sys.path.insert(0, str(SCRIPT_DIR))
from idmc_governance.servers import ai_governance as ag  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
TABLE_NAMES   = ["SUPPLIER_SITE_STAGE"]
SCHEMA_HINT   = "FINANCE_ERP_DQ"
DOMAIN_HINT   = "Supply Chain"
ORG_CONTEXT   = "Oracle Fusion ERP source data — supplier and procurement master data."
SYSTEM_NAME   = "Oracle ERP Cloud"
DATASET_NAME  = "Supplier Data"

SEPARATOR = "-" * 70


def _ok(label: str, result):
    print(f"\n{'='*70}")
    print(f"PASS  {label}")
    print(json.dumps(result, indent=2, default=str)[:3000])
    return result


def _fail(label: str, exc: Exception):
    print(f"\n{'='*70}")
    print(f"FAIL  {label}: {exc}")
    return None


def _step(n: int, label: str, fn, *args, **kwargs):
    print(f"\n{SEPARATOR}\nSTEP {n}: {label}\n{SEPARATOR}")
    try:
        result = fn(*args, **kwargs)
        return _ok(label, result)
    except Exception as exc:
        return _fail(label, exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--step", type=int, default=1, help="Start from this step")
    args = parser.parse_args()
    start = args.step

    scan_result = None
    taxonomy    = None

    # -----------------------------------------------------------------------
    # Step 1 — scan_mcc_source
    # -----------------------------------------------------------------------
    if start <= 1:
        scan_result = _step(
            1, "scan_mcc_source",
            ag.scan_mcc_source,
            TABLE_NAMES, schema_hint=SCHEMA_HINT,
        )
        if not scan_result or not scan_result.get("tables"):
            print("\nERROR: no tables returned from scan — cannot continue.\n")
            sys.exit(1)
    else:
        # Try to load from scan cache
        cache_file = ag.SCAN_CACHE_DIR / "SUPPLIER_SITE_STAGE.json"
        if cache_file.exists():
            print(f"\nStep 1 skipped — loading scan cache from {cache_file}")
            table_record = json.loads(cache_file.read_text())
            scan_result  = {"tables": [table_record], "discovered_count": 1, "missing": []}
            print(f"  {len(table_record['columns'])} columns loaded from cache")
        else:
            print("\nStep 1 skipped but no cache file found — running scan anyway")
            scan_result = _step(
                1, "scan_mcc_source (forced — no cache)",
                ag.scan_mcc_source,
                TABLE_NAMES, schema_hint=SCHEMA_HINT,
            )

    tables = (scan_result or {}).get("tables", [])

    # -----------------------------------------------------------------------
    # Step 2 — generate_governance_taxonomy
    # -----------------------------------------------------------------------
    if start <= 2:
        taxonomy = _step(
            2, "generate_governance_taxonomy",
            ag.generate_governance_taxonomy,
            tables,
            domain_hint=DOMAIN_HINT,
            organization_context=ORG_CONTEXT,
        )
        if not taxonomy or not taxonomy.get("domains"):
            print("\nERROR: taxonomy returned no domains — cannot continue.\n")
            sys.exit(1)
        # Persist taxonomy for later steps in case of restart
        tax_cache = SCRIPT_DIR / ".scan_cache" / "taxonomy.json"
        tax_cache.write_text(json.dumps(taxonomy))
        summary = taxonomy.get("_summary", {})
        print(f"\n  domains={summary.get('domain_count')}  "
              f"subdomains={summary.get('subdomain_count')}  "
              f"terms={summary.get('term_count')}")
    else:
        tax_cache = SCRIPT_DIR / ".scan_cache" / "taxonomy.json"
        if tax_cache.exists():
            print(f"\nStep 2 skipped — loading taxonomy cache from {tax_cache}")
            taxonomy = json.loads(tax_cache.read_text())
        else:
            print("\nStep 2 skipped but no taxonomy cache — regenerating")
            taxonomy = _step(
                2, "generate_governance_taxonomy (forced)",
                ag.generate_governance_taxonomy,
                tables,
                domain_hint=DOMAIN_HINT,
                organization_context=ORG_CONTEXT,
            )

    # -----------------------------------------------------------------------
    # Step 3 — create_domain_structure (dry_run first, then live)
    # -----------------------------------------------------------------------
    domain_result = None
    if start <= 3:
        dry = _step(
            3, "create_domain_structure [dry_run]",
            ag.create_domain_structure,
            taxonomy, dry_run=True,
        )
        if dry and not dry.get("errors"):
            domain_result = _step(
                3, "create_domain_structure [live]",
                ag.create_domain_structure,
                taxonomy, dry_run=False,
            )
        else:
            print("\n  dry_run had errors — skipping live run")

    # -----------------------------------------------------------------------
    # Step 4 — create_system_and_dataset
    # -----------------------------------------------------------------------
    if start <= 4:
        _step(
            4, "create_system_and_dataset",
            ag.create_system_and_dataset,
            SYSTEM_NAME, DATASET_NAME,
            description=ORG_CONTEXT,
            domain_name=DOMAIN_HINT,
        )

    # -----------------------------------------------------------------------
    # Step 5 — curate_assets_with_glossary (dry_run to verify matching)
    # -----------------------------------------------------------------------
    # Build term list by running a skipped-only step 3 pass if step 3 was not run above.
    # This avoids CDGC index lag (newly created terms may not be searchable yet).
    bt_list = None
    if domain_result:
        source = domain_result
    elif taxonomy:
        print("\n  Collecting term IDs via quick skip-check against CDGC (step 3 was skipped)...")
        source = ag.create_domain_structure(taxonomy, dry_run=False)
    else:
        source = None

    if source:
        bt_list = [
            {"name": item["name"], "id": item["id"]}
            for item in (source.get("created", []) + source.get("skipped", []))
            if item.get("type") == "BusinessTerm"
            and item.get("id")
            and item["id"] != "(dry_run)"
        ] or None
        if bt_list:
            print(f"\n  Step 5: using {len(bt_list)} terms directly (bypassing CDGC search)")

    if start <= 5:
        curate_dry = _step(
            5, "curate_assets_with_glossary [dry_run]",
            ag.curate_assets_with_glossary,
            tables,
            business_terms=bt_list,
            domain_name=DOMAIN_HINT,
            dry_run=True,
        )
        if curate_dry and curate_dry.get("match_count", 0) > 0:
            print(f"\n  {curate_dry['match_count']} matches found — running live link")
            _step(
                5, "curate_assets_with_glossary [live]",
                ag.curate_assets_with_glossary,
                tables,
                business_terms=bt_list,
                domain_name=DOMAIN_HINT,
                dry_run=False,
            )
        else:
            print("\n  No matches from dry_run — skipping live link")

    # -----------------------------------------------------------------------
    # Step 6 — run_mcc_scan (list sources only, no actual scan trigger)
    # -----------------------------------------------------------------------
    if start <= 6:
        _step(
            6, "run_mcc_scan [list sources]",
            ag.run_mcc_scan,
            # no name/id → returns available_sources list
        )

    # -----------------------------------------------------------------------
    # Step 7 — propagate_dq_score (uses a real or dummy asset name)
    # -----------------------------------------------------------------------
    if start <= 7:
        _step(
            7, "propagate_dq_score [test score on SUPPLIER_SITE_STAGE]",
            ag.propagate_dq_score,
            "SUPPLIER_SITE_STAGE",
            score=87.5,
            dimension="Completeness",
            passed_rows=880,
            failed_rows=120,
            total_rows=1000,
        )

    # -----------------------------------------------------------------------
    # Step 8 — onboard_and_govern (dry_run — full pipeline smoke test)
    # -----------------------------------------------------------------------
    if start <= 8:
        _step(
            8, "onboard_and_govern [dry_run]",
            ag.onboard_and_govern,
            TABLE_NAMES,
            domain_hint=DOMAIN_HINT,
            organization_context=ORG_CONTEXT,
            system_name=SYSTEM_NAME,
            dataset_name=DATASET_NAME,
            schema_hint=SCHEMA_HINT,
            dry_run=True,
        )

    print(f"\n{'='*70}")
    print("All requested steps complete.")


if __name__ == "__main__":
    main()
