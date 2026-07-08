"""
scale_scan_dq.py  —  Phase-B scale runner (corrected, MCP-client based)

Drives the governance agent over N Snowflake tables doing SCAN + CREATE DQ ASSETS
(the mail's exact claim), via the proper MCP streamable-HTTP client. Captures the
three demo metrics: throughput (tables/hr), resource envelope (CPU/mem), cost
(Snowflake credits), and extrapolates to 7k / 25k.

Replaces run_scale_test.py, which used a raw JSON-RPC POST (HTTP 406) and a
non-existent govern(schema,table) signature.

Usage:
  python scale_scan_dq.py --limit 5              # validation
  python scale_scan_dq.py --all                  # full catalog
  python scale_scan_dq.py --limit 1000
"""
from __future__ import annotations
import argparse, asyncio, base64, csv, json, os, re, threading, time
from datetime import datetime, timezone

try:
    import psutil
except ImportError:
    psutil = None   # resource sampling degrades gracefully if unavailable
import snowflake.connector
from cryptography.hazmat.primitives import serialization
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

load_dotenv()

AI_URL     = os.getenv("AI_GOVERNANCE_URL",     "http://127.0.0.1:9770/mcp")
ENGINE_URL = os.getenv("GOVERNANCE_ENGINE_URL", "http://127.0.0.1:9765/mcp")
DB      = os.getenv("SNOWFLAKE_GOVTEST_DB", "GOVERNANCE_SCALE_TEST")
SCHEMAS = ["GOVTEST_CLAIMS", "GOVTEST_CLINICAL", "GOVTEST_MEMBER", "GOVTEST_PROVIDER"]
WH      = os.getenv("SNOWFLAKE_WAREHOUSE", "INCEPT_WH")
RESULTS = "scale_scan_dq_results.csv"
PROGRESS = "scale_scan_dq_progress.log"


def _sf():
    k = serialization.load_pem_private_key(base64.b64decode(os.getenv("SNOWFLAKE_PRIVATE_KEY_B64")), password=None)
    der = k.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return snowflake.connector.connect(
        account=os.getenv("SNOWFLAKE_ACCOUNT"), user=os.getenv("SNOWFLAKE_USER"),
        role=os.getenv("SNOWFLAKE_ROLE"), warehouse=WH, database=DB, private_key=der)


def get_tables(limit: int | None):
    con = _sf(); cur = con.cursor()
    schemas_csv = ", ".join(f"'{s}'" for s in SCHEMAS)
    q = (f"SELECT TABLE_SCHEMA, TABLE_NAME FROM {DB}.INFORMATION_SCHEMA.TABLES "
         f"WHERE TABLE_SCHEMA IN ({schemas_csv}) AND ROW_COUNT > 0 ORDER BY TABLE_SCHEMA, TABLE_NAME")
    if limit:
        q += f" LIMIT {limit}"
    cur.execute(q)
    rows = [{"schema": r[0], "table": r[1]} for r in cur.fetchall()]
    con.close()
    return rows


def wh_credits(since_iso: str) -> float:
    try:
        con = _sf(); cur = con.cursor()
        cur.execute("SELECT COALESCE(SUM(CREDITS_USED),0) FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY "
                    "WHERE WAREHOUSE_NAME=%s AND START_TIME >= %s", (WH.upper(), since_iso))
        v = cur.fetchone()[0] or 0.0; con.close(); return float(v)
    except Exception:
        return 0.0


async def _call(url: str, tool: str, args: dict):
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()
            r = await s.call_tool(tool, args)
            if r.isError:
                raise RuntimeError(str(r.content)[:200])
            txt = r.content[0].text if r.content else "{}"
            try:
                return json.loads(txt)
            except Exception:
                return {"_raw": txt}


async def scan_and_dq_one(schema: str, table: str) -> dict:
    """Scan one table's columns, then create DQ rule assets. Returns timing + counts."""
    t0 = time.time()
    # 1. find the table asset
    find = await _call(AI_URL, "scan_find_tables", {"table_names": [table], "schema_hint": schema})
    acts = [a for a in (find.get("next_actions") or []) if a.get("tool") == "scan_fetch_columns"]
    if not acts:
        return {"ok": False, "stage": "find", "detail": "not found in catalog", "elapsed": time.time() - t0, "cols": 0, "rules": 0}
    p = acts[0]["params"]
    # 2. fetch columns
    await _call(AI_URL, "scan_fetch_columns", {
        "table_name": p.get("table_name", table), "table_id": p.get("table_id", ""),
        "schema": p.get("schema", schema), "external_id": p.get("external_id", "")})
    # scan_fetch_columns caches full column metadata (with IDs) to .scan_cache but
    # only returns a preview; read the cache file to get column IDs for DQ rules.
    ext_id = p.get("external_id") or ""
    cache_file = os.path.join(".scan_cache", re.sub(r"[^\w]", "_", table.upper()) + ".json")
    columns = []
    if os.path.exists(cache_file):
        cdata = json.load(open(cache_file))
        columns = cdata.get("columns") or []
        ext_id = cdata.get("external_id") or ext_id
    origin = ext_id.split("://")[0] if "://" in ext_id else ext_id.split("/")[0]
    col_ids = [{"column_name": c.get("name"), "column_id": c.get("internal_id"),
                "data_type": c.get("data_type") or ""} for c in columns]
    col_ids = [c for c in col_ids if c["column_name"] and c["column_id"]]
    if not col_ids:
        return {"ok": False, "stage": "scan", "detail": "no column ids in cache", "elapsed": time.time() - t0, "cols": 0, "rules": 0}
    # 3. create DQ rule assets (assets mode: no source_table_path -> no mapping tasks/execution)
    dq = await _call(ENGINE_URL, "create_generic_dq_rules", {
        "table_name": table, "column_ids": col_ids, "catalog_origin": origin})
    n_rules = len(dq.get("occurrences_registered") or []) + len(dq.get("rules_created") or [])
    return {"ok": True, "elapsed": time.time() - t0, "cols": len(col_ids), "rules": n_rules}


class ResSampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True); self.stop = False; self.peak_mb = 0; self.cpu = []
    def run(self):
        if psutil is None:
            return
        psutil.cpu_percent(interval=None)
        while not self.stop:
            tot = 0
            for pr in psutil.process_iter(["name", "memory_info"]):
                try:
                    if "python" in (pr.info["name"] or "").lower():
                        tot += pr.info["memory_info"].rss
                except Exception:
                    pass
            self.peak_mb = max(self.peak_mb, tot / 1024 / 1024)
            self.cpu.append(psutil.cpu_percent(interval=1.0))


def log(msg: str):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(PROGRESS, "a") as f:
        f.write(line + "\n")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    limit = None if args.all else (args.limit or 5)

    tables = get_tables(limit)
    n = len(tables)
    log(f"=== scale_scan_dq START | {n} tables | scan + create DQ assets ===")
    since = datetime.now(timezone.utc).isoformat()
    sampler = ResSampler(); sampler.start()

    ok = err = 0; total_cols = total_rules = 0; times = []
    t0 = time.time()
    for i, t in enumerate(tables, 1):
        try:
            r = await scan_and_dq_one(t["schema"], t["table"])
        except Exception as e:
            r = {"ok": False, "stage": "exc", "detail": str(e)[:120], "elapsed": 0, "cols": 0, "rules": 0}
        times.append(r["elapsed"]); total_cols += r["cols"]; total_rules += r["rules"]
        if r["ok"]:
            ok += 1
        else:
            err += 1
            log(f"  FAIL {t['schema']}.{t['table']} [{r.get('stage')}]: {r.get('detail')}")
        if i % 25 == 0 or i == n:
            el = time.time() - t0; rate = i / el * 3600 if el else 0
            log(f"  {i}/{n} | {rate:.0f} tbl/hr | ok={ok} err={err} | cols={total_cols} rules={total_rules} | peak_mem={sampler.peak_mb:.0f}MB")

    el = time.time() - t0
    sampler.stop = True; sampler.join(timeout=3)
    credits = wh_credits(since)
    tph = n / (el / 3600) if el else 0
    avg_cpu = sum(sampler.cpu) / len(sampler.cpu) if sampler.cpu else 0

    row = {
        "tables": n, "ok": ok, "err": err, "wall_clock_s": round(el, 1),
        "tables_per_hour": round(tph, 0), "avg_s_per_table": round(el / n, 2) if n else 0,
        "columns_scanned": total_cols, "dq_assets_created": total_rules,
        "peak_mem_mb": round(sampler.peak_mb), "avg_cpu_pct": round(avg_cpu),
        "sf_credits": round(credits, 4), "cost_usd_est": round(credits * 3.0, 2),
        "eta_7k_hours": round(7000 / tph, 1) if tph else None,
        "eta_25k_hours": round(25000 / tph, 1) if tph else None,
    }
    with open(RESULTS, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys())); w.writeheader(); w.writerow(row)
    log("=== RESULTS ===")
    for k, v in row.items():
        log(f"  {k:20}: {v}")
    log(f"=== written to {RESULTS} ===")


if __name__ == "__main__":
    asyncio.run(main())
