#!/usr/bin/env python
"""Whole-catalog taxonomy: feed the LLM the COMPLETE distinct vocabulary of the
catalog (all unique column names + table-name tokens) so the resulting glossary
provably covers every concept across all ~4000 tables — not a 30-table sample.

Because the catalog reuses ~108 unique column names across 137k instances, the
LLM maps each UNIQUE column name -> business term once; curate then applies that
map to every instance (deterministic, no per-column LLM).

Output:
  taxonomy.json   — {domains:[{name,description,subdomains:[{name,description,
                     business_terms:[{name,definition,synonyms,columns:[...]}]}]}]}
  colterm_map.json — {COLUMN_NAME_UPPER: term_name}   (drives scale curate)
"""
from idmc_governance.common.paths import STATE_DIR
import glob
import json
import os
import re
from collections import Counter

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".env"))
from idmc_governance.servers import ai_governance as aim

CACHE = ".scan_cache"


def main():
    cols = Counter(); toks = Counter(); tables = []
    for f in glob.glob(f"{CACHE}/*.json"):   # per-run cache is already scoped; DB-agnostic
        d = json.load(open(f))
        tables.append(d["name"])
        for c in d.get("columns", []):
            cols[c["name"].upper()] += 1
        for t in re.split(r"_", d["name"].upper()):
            if t:
                toks[t] += 1
    col_names = sorted(cols)
    tok_names = sorted(toks)
    print(f"tables={len(tables)} unique_columns={len(col_names)} table_tokens={len(tok_names)}")

    system = """You are a senior data governance architect for a US health plan.
Build a COMPLETE business glossary taxonomy in JSON from the catalog's full vocabulary.
Rules:
- Organize into a small number of Domains, each with Subdomains.
- Create Business Terms with a one-sentence definition and synonyms.
- EVERY column name provided MUST be assigned to exactly one business term via its
  "columns" list (use the column names verbatim, uppercase). Do not invent columns.
- Group technical/audit columns (RECORD_ID, LOAD_DATE, BATCH_ID, IS_*, SOURCE_SYSTEM)
  under a "Data Operations / Audit" subdomain.
Return ONLY JSON:
{"domains":[{"name":"","description":"","subdomains":[{"name":"","description":"",
"business_terms":[{"name":"","definition":"","synonyms":[""],"columns":[""]}]}]}]}"""

    user = (
        f"Table-name tokens (concepts) across {len(tables)} tables:\n{json.dumps(tok_names)}\n\n"
        f"ALL {len(col_names)} unique column names to assign to business terms:\n{json.dumps(col_names)}"
    )

    tax = aim._llm_json(system, user, model=aim._MODEL_QUALITY)
    if not isinstance(tax, dict) or not tax.get("domains"):
        print("FAILED:", str(tax)[:300]); return 1

    # Build column -> term map and verify coverage
    colterm = {}
    nterms = 0
    for d in tax["domains"]:
        for s in d.get("subdomains", []):
            for bt in s.get("business_terms", []):
                nterms += 1
                for c in bt.get("columns", []):
                    colterm[c.upper()] = bt["name"]
    covered = [c for c in col_names if c in colterm]
    missing = [c for c in col_names if c not in colterm]

    json.dump(tax, open(str(STATE_DIR / "taxonomy.json"), "w"), indent=1)
    json.dump(colterm, open(str(STATE_DIR / "colterm_map.json"), "w"), indent=1)

    print(f"\nDomains={len(tax['domains'])} terms={nterms}")
    for d in tax["domains"]:
        subs = d.get("subdomains", [])
        print(f"  DOMAIN {d['name']}: {len(subs)} subdomains")
        for s in subs:
            print(f"    - {s['name']}: {[b['name'] for b in s.get('business_terms',[])]}")
    print(f"\ncolumn coverage: {len(covered)}/{len(col_names)} mapped; missing={missing}")
    print("saved taxonomy.json + colterm_map.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
