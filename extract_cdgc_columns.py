#!/usr/bin/env python
"""Extract every catalogued table's columns (name + data_type) from CDGC into
.scan_cache/*.json — the same format generate_dqro_import.py consumes.

Read-only (CDGC search + asset GETs), so it runs locally even though writes 503.
Parallelizes ACROSS tables so the full ~4000-table catalog finishes in minutes,
not the ~15s/table the per-table profiling scan would take.

Usage:
  python extract_cdgc_columns.py --limit 3          # smoke test
  python extract_cdgc_columns.py                    # full catalog
  python extract_cdgc_columns.py --schema GOVTEST_CLAIMS
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

import ai_governance_mcp as aim
import snowflake_types

CACHE = ".scan_cache"
TYPE_MAP: dict = {}   # {(SCHEMA, TABLE, COLUMN): DATA_TYPE} from Snowflake metadata


def _safe_name(external_id: str, table: str) -> str:
    # DB/SCHEMA/TABLE from origin://DB/SCHEMA/TABLE~class -> flat cache key
    path = external_id.split("://", 1)[-1].split("~")[0]
    key = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_") or table
    return key[:150]


# Numeric-signal name tokens (amounts, counts, numeric ids) -> "NUMBER"
# Substring-matched, so avoid tokens that hide inside common words
# (e.g. "COUNT" in COUNTRY/ACCOUNT). Keep only low-collision numeric signals.
_NUM_TOKENS  = ("_ID", "ID_", "_KEY", "KEY_", "_PK", "PK_", "_NBR", "NBR_", "_NUM", "NUM_",
                "_CNT", "CNT_", "AMOUNT", "_AMT", "AMT_", "_QTY", "QTY_",
                "_PCT", "PCT_", "PRICE", "BALANCE")
_DATE_TOKENS = ("_DATE", "DATE_", "_DT", "DT_", "_TIME", "TIME_", "_TS", "TS_",
                "CREATED", "MODIFIED", "UPDATED", "TIMESTAMP")


def _infer_dtype(cname: str) -> str:
    """Infer a coarse data_type from the column name so the generator's
    name-driven selection/dimension logic produces UI-equivalent output
    without a per-column API GET."""
    n = cname.upper()
    if any(t in n for t in _DATE_TOKENS):
        return "TIMESTAMP"
    if any(t in n for t in _NUM_TOKENS):
        return "NUMBER"
    return "VARCHAR"


def _extract_one(table_hit):
    """Return (cache_key, record) or (None, error). One hierarchy GET per table;
    column names + refs come free from the hierarchy children (no per-column GET)."""
    ext = table_hit.get("core.externalId") \
        or (table_hit.get("summary") or {}).get("core.externalId") \
        or (table_hit.get("systemAttributes") or {}).get("core.externalId") or ""
    tid = aim._id_of(table_hit)
    tname = (table_hit.get("summary") or {}).get("core.name") or aim._name_of(table_hit) or ""
    if not tname and "~" in ext:
        tname = ext.split("~")[0].rsplit("/", 1)[-1]
    if not ext or "~" not in ext or not tid:
        return None, f"skip (no external_id/id): {tname}"
    # Schema/table from external_id path: origin://DB/SCHEMA/TABLE
    path = ext.split("://", 1)[-1].split("~")[0]
    parts = path.split("/")
    schema = parts[1].upper() if len(parts) >= 2 else ""
    tbl_up = parts[2].upper() if len(parts) >= 3 else tname.upper()

    detail = aim._cdgc_get_asset(tid, segments="summary,hierarchy")
    hier = detail.get("hierarchy") or []
    if isinstance(hier, dict):
        hier = hier.get("children") or hier.get("items") or []
    columns = []
    for h in hier:
        ceid = h.get("core.externalId") or (h.get("summary") or {}).get("core.externalId") or ""
        if "~" not in ceid or not ceid.rsplit("~", 1)[-1].endswith("Column"):
            continue
        cname = (h.get("summary") or {}).get("core.name") or ceid.split("~")[0].rsplit("/", 1)[-1]
        if not cname:
            continue
        # Real Snowflake data_type; fall back to name inference only if absent.
        dt = TYPE_MAP.get((schema, tbl_up, cname.upper())) or _infer_dtype(cname)
        columns.append({"name": cname, "data_type": dt})
    rec = {"external_id": ext, "name": tname, "columns": columns}
    return _safe_name(ext, tname), rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = all tables")
    ap.add_argument("--schema", default=None, help="restrict to one schema")
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()

    os.makedirs(CACHE, exist_ok=True)

    all_schemas = ["GOVTEST_CLINICAL", "GOVTEST_MEMBER", "GOVTEST_CLAIMS", "GOVTEST_PROVIDER"]
    schemas = [args.schema] if args.schema else all_schemas

    global TYPE_MAP
    print("Loading real column data_types from Snowflake INFORMATION_SCHEMA ...")
    try:
        TYPE_MAP = snowflake_types.load_type_map(schemas)
        print(f"  loaded {len(TYPE_MAP)} column types from Snowflake")
    except Exception as e:
        print(f"  WARN: Snowflake type load failed ({e!r}); falling back to name inference")
        TYPE_MAP = {}

    print(f"Enumerating tables via schema browse: {schemas}")
    tables = []
    for s in schemas:
        hits = aim._browse_all_tables_in_schema(s)
        print(f"  {s}: {len(hits)} tables")
        tables.extend(hits)
    # de-dupe by external_id/id
    seen, uniq = set(), []
    for t in tables:
        eid = (t.get("core.externalId") or (t.get("summary") or {}).get("core.externalId")
               or (t.get("systemAttributes") or {}).get("core.externalId") or "")
        k = eid or aim._id_of(t)
        if k and k not in seen:
            seen.add(k); uniq.append(t)
    tables = uniq
    if args.limit:
        tables = tables[:args.limit]
    print(f"tables to extract: {len(tables)}  (schemas: {len(schemas)})")
    if not tables:
        print("No tables found. Check catalog / auth.")
        return 1

    t0 = time.time()
    ok = err = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_extract_one, t): i for i, t in enumerate(tables)}
        for done, fut in enumerate(as_completed(futs), 1):
            try:
                key, rec = fut.result()
            except Exception as e:
                err += 1; print(f"  ERR {e!r}"); continue
            if not key:
                err += 1; continue
            with open(os.path.join(CACHE, f"{key}.json"), "w") as f:
                json.dump(rec, f)
            ok += 1
            if done % 25 == 0 or done == len(tables):
                rate = done / max(time.time() - t0, 0.001)
                print(f"  {done}/{len(tables)}  ok={ok} err={err}  "
                      f"{rate:.1f} tbl/s  eta {int((len(tables)-done)/max(rate,0.01))}s")

    dt = time.time() - t0
    total_cols = 0
    for f in os.listdir(CACHE):
        if f.endswith(".json"):
            try:
                total_cols += len(json.load(open(os.path.join(CACHE, f))).get("columns", []))
            except Exception:
                pass
    print(f"\nDONE in {dt:.0f}s  tables_ok={ok}  errors={err}  total_columns_in_cache={total_cols}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
