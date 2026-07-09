#!/usr/bin/env python
"""Bulk-import a CDGC asset file (DQRO .xlsx) via the real 3-step CDGC import API,
then poll the import job to completion. Contract reverse-engineered from the
CDGC UI's own network calls.

Flow:
  1. POST ccgf-contentv2/api/v1/import/validation   (multipart: file + codepage)
        -> {fileId, summary:[{insertCount, updateCount, deleteCount, ...}]}
  2. POST ccgf-contentv2/api/v1/import?jobName=...   (JSON, references fileId)
        -> {jobStatus, jobId}
  3. GET  data360/observable/v1/jobs/{jobId}         (poll to terminal state)

Auth: governance_engine_mcp._request_cdgc (IDMC JWT Bearer + session + 401 renew).
Writes hit CDGC (meant to run from Azure; validation/read work locally too).

Usage:
  python cdgc_bulk_import.py templates/CDGC_DQRO_FULL.xlsx
  python cdgc_bulk_import.py <file> --policy STOP_ON_ERROR --validate-only
"""
import argparse
import json
import os
import sys
import time

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".env"))
except Exception:
    pass

from idmc_governance.servers import governance_engine as gem

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PRODUCT_HDR = {"X-INFA-PRODUCT-ID": "CDGC"}

# validationPolicy on{Error,Warning} values the UI sends.
POLICY = {
    "STOP_ON_ERROR":            {"onError": "Stop",     "onWarning": "Continue"},
    "CONTINUE_ON_ERROR_WARNING": {"onError": "Continue", "onWarning": "Continue"},
}


def validate_upload(file_path: str) -> dict:
    """Step 1: upload the file for validation; returns {fileId, summary:[...]}."""
    filename = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        file_bytes = f.read()
    files = {
        "file": (filename, file_bytes, XLSX_MIME),
        "codepage": (None, "UTF-8"),   # (None, value) => plain multipart form field
    }
    url = f"{gem.CDGC_API_BASE}/ccgf-contentv2/api/v1/import/validation"
    r = gem._request_cdgc("POST", url, files=files, headers=dict(PRODUCT_HDR))
    if r.status_code not in (200, 201):
        raise RuntimeError(f"validation failed HTTP {r.status_code}: {r.text[:600]}")
    return r.json() if r.text else {}


def submit_import(file_id: str, job_name: str, policy: str) -> dict:
    """Step 2: start the import job referencing the uploaded fileId."""
    pol = POLICY[policy]
    body = {
        "files": [{"filehandle": file_id, "items": [], "codepage": "UTF-8"}],
        "validationPolicy": [{
            "name": "string", "validationType": "string",
            "onError": pol["onError"], "onWarning": pol["onWarning"],
        }],
    }
    from urllib.parse import quote
    url = f"{gem.CDGC_API_BASE}/ccgf-contentv2/api/v1/import?jobName={quote(job_name)}"
    r = gem._request_cdgc("POST", url, json=body, headers=dict(PRODUCT_HDR))
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"import submit failed HTTP {r.status_code}: {r.text[:600]}")
    return r.json() if r.text else {}


def poll_job(job_id: str, timeout_s: int = 7200, interval_s: int = 15) -> dict:
    """Step 3: poll the observable jobs API until terminal."""
    deadline = time.time() + timeout_s
    url = f"{gem.CDGC_API_BASE}/data360/observable/v1/jobs/{job_id}"
    last = {}
    TERMINAL = ("SUCCESS", "SUCCEEDED", "COMPLETED", "FAILED", "ERROR", "CANCELLED")
    while time.time() < deadline:
        r = gem._request_cdgc("GET", url)
        if r.status_code < 400 and r.text:
            last = r.json() or {}
            state = (last.get("status") or last.get("lifecycleStatus") or "").upper()
            print(f"  job {job_id}: {state}")
            if state in TERMINAL:
                return last
        else:
            print(f"  poll HTTP {r.status_code}")
        time.sleep(interval_s)
    return {"status": "TIMEOUT", "last": last}


def _print_summary(val: dict):
    for s in val.get("summary", []):
        print(f"  [{s.get('name')}] insert={s.get('insertCount')} "
              f"update={s.get('updateCount')} delete={s.get('deleteCount')} "
              f"source={s.get('source')} validationType={s.get('validationType')}")
        if s.get("validations"):
            print(f"    validations: {json.dumps(s['validations'])[:500]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--policy", default="CONTINUE_ON_ERROR_WARNING", choices=list(POLICY))
    ap.add_argument("--validate-only", action="store_true",
                    help="Run only step 1 (upload+validate); create nothing")
    ap.add_argument("--no-poll", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.file):
        print(f"file not found: {args.file}"); return 2

    t0 = time.time()
    print(f"=== [1/3] validating {args.file} ===")
    val = validate_upload(args.file)
    file_id = val.get("fileId") or val.get("filehandle")
    print(f"fileId={file_id}")
    _print_summary(val)
    if not file_id:
        print("no fileId returned; aborting"); return 1
    if args.validate_only:
        print(f"validate-only done in {time.time()-t0:.1f}s"); return 0

    job_name = f"{os.path.basename(args.file)}_import"
    print(f"=== [2/3] submitting import (policy={args.policy}) ===")
    sub = submit_import(file_id, job_name, args.policy)
    job_id = sub.get("jobId")
    print(f"jobStatus={sub.get('jobStatus')} jobId={job_id}")
    if args.no_poll or not job_id:
        return 0

    print("=== [3/3] polling job ===")
    final = poll_job(job_id)
    print(f"\nfinal state after {time.time()-t0:.0f}s: "
          f"{final.get('status') or final.get('lifecycleStatus')}")
    print(json.dumps(final, indent=1)[:1200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
