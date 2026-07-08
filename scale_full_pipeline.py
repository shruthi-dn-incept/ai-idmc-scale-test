"""
scale_full_pipeline.py  —  hardened full-catalog governance runner

Design rules (avoid Snowflake<->CDGC drift by construction):
  1. SCOPE COMES FROM CDGC, never Snowflake. The governable table list is read
     from the CDGC catalog, so we can never create assets for an un-cataloged
     table (it isn't in the working set).
  2. RESUMABLE + INCREMENTAL. A checkpoint records finished tables; re-runs skip
     them and pick up stragglers. No blocking coverage gate — governance runs on
     whatever CDGC contains now; a coverage REPORT is printed (non-blocking).

Hardening: bounded parallelism (workers), retry-with-exponential-backoff on
503/429/timeout, per-table checkpoint.

Pipeline:
  A. Coverage report  — CDGC governable count vs Snowflake source count.
  B. Taxonomy + domain (ONCE) — suggest domains/subdomains/business terms from a
     representative sample and create the structure in CDGC.
  C. DQ rules + DQROs (ALL cataloged tables) — parallel, resumable, backoff.
  D. MCC scan (ONCE) — trigger native DQ execution on the catalog.

Usage:
  python scale_full_pipeline.py --workers 8 [--limit N] [--skip-taxonomy] [--skip-mcc]
"""
from __future__ import annotations
import argparse, asyncio, base64, csv, json, os, re, time
from datetime import datetime, timezone

try:
    import psutil
except ImportError:
    psutil = None
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
CHECKPOINT = "scale_checkpoint.txt"
RESULTS    = "scale_full_results.csv"
CACHE_DIR  = ".scan_cache"


def log(m: str):
    print(f"{datetime.now().strftime('%H:%M:%S')} {m}", flush=True)


# ── retry-with-backoff MCP call ───────────────────────────────────────────────
async def call(url: str, tool: str, args: dict, retries: int = 5):
    delay = 2.0
    last = None
    for _ in range(retries):
        try:
            async with streamablehttp_client(url) as (r, w, _c):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    res = await s.call_tool(tool, args)
                    if res.isError:
                        raise RuntimeError(str(res.content)[:200])
                    txt = res.content[0].text if res.content else "{}"
                    try:
                        return json.loads(txt)
                    except Exception:
                        return {"_raw": txt}
        except Exception as e:
            last = e
            m = str(e).lower()
            if any(k in m for k in ("503", "429", "timeout", "unavailable", "temporarily")):
                await asyncio.sleep(delay); delay = min(delay * 2, 30); continue
            raise
    raise last


# ── Snowflake source count (for coverage report only — NOT for scope) ─────────
def snowflake_count() -> int:
    try:
        k = serialization.load_pem_private_key(base64.b64decode(os.getenv("SNOWFLAKE_PRIVATE_KEY_B64")), password=None)
        der = k.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        con = snowflake.connector.connect(account=os.getenv("SNOWFLAKE_ACCOUNT"), user=os.getenv("SNOWFLAKE_USER"),
                                          role=os.getenv("SNOWFLAKE_ROLE"), warehouse=WH, database=DB, private_key=der)
        cur = con.cursor()
        scsv = ", ".join(f"'{s}'" for s in SCHEMAS)
        cur.execute(f"SELECT COUNT(*) FROM {DB}.INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA IN ({scsv}) AND ROW_COUNT > 0")
        n = cur.fetchone()[0]; con.close(); return int(n)
    except Exception as e:
        log(f"  (snowflake_count failed: {str(e)[:80]})"); return -1


def wh_credits(since_iso: str) -> float:
    try:
        k = serialization.load_pem_private_key(base64.b64decode(os.getenv("SNOWFLAKE_PRIVATE_KEY_B64")), password=None)
        der = k.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        con = snowflake.connector.connect(account=os.getenv("SNOWFLAKE_ACCOUNT"), user=os.getenv("SNOWFLAKE_USER"),
                                          role=os.getenv("SNOWFLAKE_ROLE"), warehouse=WH, private_key=der)
        cur = con.cursor()
        cur.execute("SELECT COALESCE(SUM(CREDITS_USED),0) FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY "
                    "WHERE WAREHOUSE_NAME=%s AND START_TIME >= %s", (WH.upper(), since_iso))
        v = cur.fetchone()[0] or 0.0; con.close(); return float(v)
    except Exception:
        return 0.0


# ── SCOPE: governable tables come from CDGC (rule #1) ─────────────────────────
async def cdgc_tables(limit: int | None):
    r = await call(AI_URL, "list_catalog_tables", {"max_results": 6000, "group_by_source": True})
    out = []
    for cs in r.get("catalog_sources_grouped", []):
        src = cs.get("source", "") or ""
        if not src.startswith("GOVTEST"):
            continue
        for sc in cs.get("schemas", []):
            for t in sc.get("tables", []):
                out.append({"schema": sc.get("schema", ""), "table": t.get("name", "")})
    out.sort(key=lambda x: (x["schema"], x["table"]))
    return out[:limit] if limit else out


# ── per-table: scan columns + create DQ rules/DQROs ───────────────────────────
async def govern_dq_one(schema: str, table: str) -> dict:
    t0 = time.time()
    find = await call(AI_URL, "scan_find_tables", {"table_names": [table], "schema_hint": schema})
    acts = [a for a in (find.get("next_actions") or []) if a.get("tool") == "scan_fetch_columns"]
    if not acts:  # not in CDGC -> not governable (rule #1: skip, never orphan)
        return {"ok": False, "stage": "not_cataloged", "elapsed": time.time() - t0, "cols": 0, "rules": 0}
    p = acts[0]["params"]
    await call(AI_URL, "scan_fetch_columns", {"table_name": p.get("table_name", table), "table_id": p.get("table_id", ""),
                                              "schema": p.get("schema", schema), "external_id": p.get("external_id", "")})
    cf = os.path.join(CACHE_DIR, re.sub(r"[^\w]", "_", table.upper()) + ".json")
    cols, ext = [], p.get("external_id", "")
    if os.path.exists(cf):
        d = json.load(open(cf)); cols = d.get("columns") or []; ext = d.get("external_id") or ext
    origin = ext.split("://")[0] if "://" in ext else ext.split("/")[0]
    cids = [{"column_name": c.get("name"), "column_id": c.get("internal_id"), "data_type": c.get("data_type") or ""}
            for c in cols if c.get("name") and c.get("internal_id")]
    if not cids:
        return {"ok": False, "stage": "no_columns", "elapsed": time.time() - t0, "cols": 0, "rules": 0}
    dq = await call(ENGINE_URL, "create_generic_dq_rules", {"table_name": table, "column_ids": cids, "catalog_origin": origin})
    n = len(dq.get("occurrences_registered") or []) + len(dq.get("rules_created") or [])
    return {"ok": True, "elapsed": time.time() - t0, "cols": len(cids), "rules": n}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--skip-taxonomy", action="store_true")
    ap.add_argument("--skip-mcc", action="store_true")
    args = ap.parse_args()

    since = datetime.now(timezone.utc).isoformat()
    t_start = time.time()

    # resume checkpoint
    done = set()
    if os.path.exists(CHECKPOINT):
        done = set(x for x in open(CHECKPOINT).read().splitlines() if x)
    def mark(key):
        with open(CHECKPOINT, "a") as f:
            f.write(key + "\n")

    # ── A. Coverage report (non-blocking) ──
    tables = await cdgc_tables(args.limit)
    sf_n = snowflake_count()
    log(f"=== COVERAGE: CDGC governable={len(tables)} | Snowflake source={sf_n} | already-done(checkpoint)={len(done)} ===")

    todo = [t for t in tables if f"{t['schema']}.{t['table']}" not in done]
    log(f"=== to process this run: {len(todo)} (workers={args.workers}) ===")

    # ── B. Taxonomy + domain ONCE (suggest domains/subdomains/terms) ──
    if not args.skip_taxonomy and todo:
        sample = [t["table"] for t in tables[:8]] + [t["table"] for t in tables[-8:]]
        sample = list(dict.fromkeys(sample))[:16]
        log(f"=== taxonomy+domain (once) on {len(sample)} representative tables ===")
        try:
            tax = await call(AI_URL, "onboard_and_govern",
                             {"table_names": sample, "skip_steps": ["curate", "system_dataset"]})
            summ = tax.get("summary") or {}
            log(f"  taxonomy/domain done: {json.dumps(summ)[:200]}")
        except Exception as e:
            log(f"  taxonomy/domain WARN: {str(e)[:160]} (continuing)")

    # ── C. DQ rules/DQROs for ALL cataloged tables — parallel + resumable ──
    sem = asyncio.Semaphore(args.workers)
    stats = {"ok": 0, "err": 0, "not_cat": 0, "cols": 0, "rules": 0}
    peak_mb = [0.0]; done_ct = [0]; n = len(todo)

    async def worker(t):
        async with sem:
            key = f"{t['schema']}.{t['table']}"
            try:
                r = await govern_dq_one(t["schema"], t["table"])
            except Exception as e:
                r = {"ok": False, "stage": "exc", "detail": str(e)[:100], "elapsed": 0, "cols": 0, "rules": 0}
            if r["ok"]:
                stats["ok"] += 1; stats["cols"] += r["cols"]; stats["rules"] += r["rules"]; mark(key)
            elif r.get("stage") == "not_cataloged":
                stats["not_cat"] += 1
            else:
                stats["err"] += 1
                if stats["err"] <= 15:
                    log(f"  FAIL {key} [{r.get('stage')}]: {r.get('detail','')}")
            done_ct[0] += 1
            if psutil:
                tot = sum((p.info["memory_info"].rss for p in psutil.process_iter(["name", "memory_info"])
                           if "python" in (p.info["name"] or "").lower()), 0)
                peak_mb[0] = max(peak_mb[0], tot / 1024 / 1024)
            if done_ct[0] % 25 == 0 or done_ct[0] == n:
                el = time.time() - t_start
                log(f"  {done_ct[0]}/{n} | {done_ct[0]/el*3600:.0f} tbl/hr | ok={stats['ok']} err={stats['err']} "
                    f"not_cat={stats['not_cat']} rules={stats['rules']} peak_mem={peak_mb[0]:.0f}MB")

    await asyncio.gather(*[worker(t) for t in todo])

    # ── D. MCC scan (once) ──
    if not args.skip_mcc:
        log("=== triggering MCC scan (native DQ execution) ===")
        for src in SCHEMAS:
            try:
                r = await call(AI_URL, "run_mcc_scan", {"source_name": src})
                log(f"  MCC {src}: {json.dumps(r)[:160]}")
            except Exception as e:
                log(f"  MCC {src} WARN: {str(e)[:140]}")

    # ── results ──
    el = time.time() - t_start
    tph = (len(todo) / (el / 3600)) if el else 0
    credits = wh_credits(since)
    row = {
        "cdgc_governable": len(tables), "snowflake_source": sf_n,
        "processed_this_run": len(todo), "ok": stats["ok"], "errors": stats["err"],
        "not_cataloged_skipped": stats["not_cat"], "columns_scanned": stats["cols"],
        "dq_assets_created": stats["rules"], "wall_clock_s": round(el, 1),
        "tables_per_hour": round(tph), "avg_s_per_table": round(el / len(todo), 2) if todo else 0,
        "peak_mem_mb": round(peak_mb[0]), "sf_credits": round(credits, 4), "cost_usd_est": round(credits * 3, 2),
        "workers": args.workers,
    }
    with open(RESULTS, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys())); w.writeheader(); w.writerow(row)
    log("=== RESULTS ===")
    for k, v in row.items():
        log(f"  {k:24}: {v}")


if __name__ == "__main__":
    asyncio.run(main())
