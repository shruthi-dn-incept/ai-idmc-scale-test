"""
collect_scale_stats.py
Phase-B metrics wrapper: runs run_scale_test.py against tier(s) while sampling
the local agent's CPU/memory envelope, then prints a consolidated stats block.

Answers the three demo questions in one artifact:
  - Will it die at volume?  -> wall-clock, completion, tables/hour (from run_scale_test)
  - What infra does it need? -> peak memory + CPU envelope (sampled here)
  - What does it cost?       -> Snowflake credits/$ (from run_scale_test)

Runs in ASSETS mode by default (scan + DQ-rule creation, no mapping-task
execution) by clearing the DQ mapping env vars for the child process.

Usage:
  python collect_scale_stats.py --tiers 10
  python collect_scale_stats.py --tiers 25,50,100
  python collect_scale_stats.py --tiers 100 --execute   # keep DQ execution vars
"""
import argparse, csv, os, subprocess, sys, time
import psutil


def total_python_rss() -> int:
    """Sum RSS of all python processes (runner + the 3 MCP servers) = agent footprint."""
    tot = 0
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            nm = (p.info["name"] or "").lower()
            if "python" in nm:
                tot += p.info["memory_info"].rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="10", help="comma-separated tier sizes")
    ap.add_argument("--execute", action="store_true",
                    help="keep DQ mapping/execution env vars (default: assets-only)")
    args = ap.parse_args()

    env = os.environ.copy()
    mode = "execute" if args.execute else "assets"
    if not args.execute:
        # Assets-only: no M_DQ_Generic mapping tasks / execution
        env.pop("IDMC_DQ_CONNECTION_ID", None)
        env.pop("IDMC_DQ_RUNTIME_ENV_ID", None)

    print(f"=== collect_scale_stats | tiers={args.tiers} | dq_mode={mode} ===", flush=True)
    psutil.cpu_percent(interval=None)  # prime
    t0 = time.time()
    proc = subprocess.Popen([sys.executable, "run_scale_test.py", "--tiers", args.tiers], env=env)

    peak_rss = 0
    cpu_samples = []
    while proc.poll() is None:
        peak_rss = max(peak_rss, total_python_rss())
        cpu_samples.append(psutil.cpu_percent(interval=1.0))
        time.sleep(1)
    dur = time.time() - t0

    peak_mb = peak_rss / 1024 / 1024
    avg_cpu = (sum(cpu_samples) / len(cpu_samples)) if cpu_samples else 0
    max_cpu = max(cpu_samples) if cpu_samples else 0
    ncpu = psutil.cpu_count()

    print("\n================ RESOURCE ENVELOPE (local agent) ================")
    print(f"  Peak python RSS   : {peak_mb:,.0f} MB")
    print(f"  CPU (system)      : avg {avg_cpu:.0f}% , peak {max_cpu:.0f}%  of {ncpu} cores")
    print(f"  Wrapper wall-clock: {dur:.0f}s")

    # Echo the throughput/cost table run_scale_test wrote
    if os.path.exists("scale_test_results.csv"):
        print("\n================ THROUGHPUT / COST (per tier) ==================")
        with open("scale_test_results.csv") as f:
            for row in csv.DictReader(f):
                print(f"  tier={row['tier']:>5}  ok={row['tables_ok']}/{row['tables_attempted']}  "
                      f"wall={row['wall_clock_s']}s  {row['tables_per_hour']} tbl/hr  "
                      f"credits={row['sf_credits_used']}  ${row['cost_usd_est']}  "
                      f"25k_ETA={row.get('extrapolated_25k_hours','?')}h")
    print("\n=== done ===", flush=True)


if __name__ == "__main__":
    main()
