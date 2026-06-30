"""
run_scale_test.py
Drives the governance agent against GOVERNANCE_SCALE_TEST tables in tiers,
captures throughput / resource / cost metrics, and writes a results table.

Usage:
  python run_scale_test.py                    # full 4k-table run
  python run_scale_test.py --tier 100         # smoke test 100 tables
  python run_scale_test.py --tiers 100,500,1000,4000   # multi-tier benchmark
"""
from __future__ import annotations
import argparse, csv, json, os, sys, time, logging
from datetime import datetime, timezone
from dotenv import load_dotenv
import httpx
import snowflake.connector

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
GOVERNANCE_ENGINE_URL = os.getenv("GOVERNANCE_ENGINE_URL", "http://127.0.0.1:9765/mcp")
AI_GOVERNANCE_URL     = os.getenv("AI_GOVERNANCE_URL",     "http://127.0.0.1:9770/mcp")

DB      = os.getenv("SNOWFLAKE_GOVTEST_DB", "GOVERNANCE_SCALE_TEST")
SCHEMAS = ["GOVTEST_CLAIMS", "GOVTEST_CLINICAL", "GOVTEST_MEMBER", "GOVTEST_PROVIDER"]

RESULTS_FILE = "scale_test_results.csv"


# ── Snowflake helpers ──────────────────────────────────────────────────────────

def _sf_conn():
    return snowflake.connector.connect(
        account=os.getenv("SNOWFLAKE_ACCOUNT"),
        user=os.getenv("SNOWFLAKE_USER"),
        password=os.getenv("SNOWFLAKE_PASSWORD"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE", "INCEPT_WH"),
        role=os.getenv("SNOWFLAKE_ROLE", "ACCOUNTADMIN"),
        database=DB,
    )


def get_tables(limit: int) -> list[dict]:
    """Return up to `limit` tables that have rows loaded."""
    conn = _sf_conn()
    cur  = conn.cursor()
    schemas_csv = ", ".join(f"'{s}'" for s in SCHEMAS)
    cur.execute(
        f"SELECT TABLE_SCHEMA, TABLE_NAME, ROW_COUNT "
        f"FROM {DB}.INFORMATION_SCHEMA.TABLES "
        f"WHERE TABLE_SCHEMA IN ({schemas_csv}) AND ROW_COUNT > 0 "
        f"ORDER BY TABLE_SCHEMA, TABLE_NAME "
        f"LIMIT {limit}"
    )
    rows = [{"schema": r[0], "table": r[1], "rows": r[2]} for r in cur.fetchall()]
    conn.close()
    return rows


def get_warehouse_credits(warehouse: str, since_ts: str) -> float:
    """Credits consumed by the warehouse since since_ts (ISO UTC string)."""
    try:
        conn = _sf_conn()
        cur  = conn.cursor()
        cur.execute(
            "SELECT SUM(CREDITS_USED) "
            "FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY "
            "WHERE WAREHOUSE_NAME = %s AND START_TIME >= %s",
            (warehouse.upper(), since_ts)
        )
        val = cur.fetchone()[0] or 0.0
        conn.close()
        return float(val)
    except Exception:
        return 0.0


# ── MCP call helper ────────────────────────────────────────────────────────────

def call_govern(schema: str, table: str) -> dict:
    """
    Call the ai-governance MCP server's 'govern' tool for one table.
    Returns {"ok": bool, "elapsed_s": float, "detail": str}.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "govern",
            "arguments": {
                "schema": schema,
                "table":  table,
                "database": DB,
            }
        }
    }
    t0 = time.time()
    try:
        r = httpx.post(AI_GOVERNANCE_URL, json=payload, timeout=300)
        r.raise_for_status()
        body = r.json()
        elapsed = time.time() - t0
        if "error" in body:
            return {"ok": False, "elapsed_s": elapsed, "detail": str(body["error"])}
        return {"ok": True, "elapsed_s": elapsed, "detail": ""}
    except Exception as e:
        return {"ok": False, "elapsed_s": time.time() - t0, "detail": str(e)[:120]}


# ── Tier runner ────────────────────────────────────────────────────────────────

def run_tier(tier_size: int, wh_name: str) -> dict:
    tables = get_tables(tier_size)
    actual = len(tables)
    if actual == 0:
        log.warning(f"Tier {tier_size}: no loaded tables found — skipping")
        return {}

    log.info(f"=== Tier {tier_size} ({actual} tables) ===")
    wh_start_ts = datetime.now(timezone.utc).isoformat()
    tier_start  = time.time()

    ok_count = err_count = 0
    table_times: list[float] = []

    for i, t in enumerate(tables, 1):
        result = call_govern(t["schema"], t["table"])
        table_times.append(result["elapsed_s"])
        if result["ok"]:
            ok_count += 1
        else:
            err_count += 1
            log.warning(f"  FAIL {t['schema']}.{t['table']}: {result['detail']}")
        if i % 50 == 0 or i == actual:
            elapsed = time.time() - tier_start
            rate    = i / elapsed if elapsed else 0
            log.info(f"  {i}/{actual} | {rate:.2f} tbl/s | ok={ok_count} err={err_count}")

    elapsed_total = time.time() - tier_start
    credits       = get_warehouse_credits(wh_name, wh_start_ts)
    tph           = actual / (elapsed_total / 3600) if elapsed_total else 0
    avg_s         = sum(table_times) / len(table_times) if table_times else 0

    result = {
        "tier":            tier_size,
        "tables_attempted": actual,
        "tables_ok":       ok_count,
        "tables_err":      err_count,
        "wall_clock_s":    round(elapsed_total, 1),
        "tables_per_hour": round(tph, 0),
        "avg_s_per_table": round(avg_s, 2),
        "sf_credits_used": round(credits, 4),
        "cost_usd_est":    round(credits * 3.0, 4),  # ~$3/credit on-demand
        "extrapolated_25k_hours": round(25000 / tph, 1) if tph else None,
    }
    log.info(f"Tier {tier_size} done: {elapsed_total:.0f}s | {tph:.0f} tbl/hr | credits={credits:.4f}")
    return result


# ── Results writer ─────────────────────────────────────────────────────────────

def write_results(results: list[dict]):
    if not results:
        return
    fields = list(results[0].keys())
    with open(RESULTS_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(results)
    log.info(f"Results written to {RESULTS_FILE}")
    print("\n=== SCALE TEST RESULTS ===")
    header = f"{'Tier':>6}  {'Tables':>7}  {'OK':>5}  {'Err':>4}  {'Wall(s)':>8}  {'Tbl/hr':>7}  {'Avg s/tbl':>9}  {'Credits':>8}  {'Est $':>7}  {'25k ETA(hr)':>11}"
    print(header)
    print("-" * len(header))
    for r in results:
        eta = f"{r['extrapolated_25k_hours']:>11.1f}" if r.get("extrapolated_25k_hours") else "         N/A"
        print(
            f"{r['tier']:>6}  {r['tables_attempted']:>7}  {r['tables_ok']:>5}  "
            f"{r['tables_err']:>4}  {r['wall_clock_s']:>8.1f}  {r['tables_per_hour']:>7.0f}  "
            f"{r['avg_s_per_table']:>9.2f}  {r['sf_credits_used']:>8.4f}  "
            f"{r['cost_usd_est']:>7.4f}  {eta}"
        )
    print()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier",  type=int, help="Run a single tier of N tables")
    ap.add_argument("--tiers", type=str, default="100,500,1000,4000",
                    help="Comma-separated tier sizes (default: 100,500,1000,4000)")
    args = ap.parse_args()

    wh_name = os.getenv("SNOWFLAKE_WAREHOUSE", "INCEPT_WH")
    tiers   = [args.tier] if args.tier else [int(x) for x in args.tiers.split(",")]

    log.info(f"Scale test starting | tiers={tiers} | warehouse={wh_name}")
    log.info(f"Governance engine : {AI_GOVERNANCE_URL}")

    all_results = []
    for t in tiers:
        r = run_tier(t, wh_name)
        if r:
            all_results.append(r)

    write_results(all_results)


if __name__ == "__main__":
    main()
