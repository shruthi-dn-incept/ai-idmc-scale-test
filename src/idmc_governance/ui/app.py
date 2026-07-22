"""governance_ui.py — Branded wizard UI for the IDMC governance pipeline.

Serves a React frontend on http://127.0.0.1:8080 and bridges REST calls
to the two MCP servers:
  ai-governance    :8770
  governance-engine :8765

Usage:
  python governance_ui.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time as _time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel

AI_GOVERNANCE_URL     = os.getenv("AI_GOVERNANCE_URL",     "http://127.0.0.1:8770/mcp")
GOVERNANCE_ENGINE_URL = os.getenv("GOVERNANCE_ENGINE_URL", "http://127.0.0.1:8765/mcp")

def _read_env_file() -> dict[str, str]:
    env: dict[str, str] = {}
    p = Path(__file__).resolve().parents[3] / ".env"  # repo-root .env
    if not p.exists():
        return env
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("governance_ui")

app = FastAPI(title="INCEPT Data Governance")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


# ── MCP helpers ───────────────────────────────────────────────────────────────

def _unwrap_exception(exc: BaseException) -> BaseException:
    """Recursively unwrap ExceptionGroup to get the root cause."""
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    return exc


async def _call(server_url: str, tool: str, args: dict) -> Any:
    try:
        async with streamablehttp_client(server_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, args)
                if result.isError:
                    raise RuntimeError(str(result.content))
                text = result.content[0].text if result.content else "{}"
                return json.loads(text)
    except* Exception as eg:
        raise _unwrap_exception(eg.exceptions[0]) from None


async def _govern(request: str, step: str | None = None) -> dict:
    # `step` forces deterministic dispatch on the server (no LLM step-inference), so a
    # stale persisted state can never misroute an explicit UI action to the wrong step.
    args: dict = {"request": request}
    if step:
        args["step"] = step
    return await _call(AI_GOVERNANCE_URL, "govern", args)


@app.post("/api/reset")
async def reset_session():
    """Clear server-side pipeline state so 'New Session' truly starts fresh."""
    try:
        return await _call(AI_GOVERNANCE_URL, "reset_pipeline", {})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Config ────────────────────────────────────────────────────────────────────

@app.get("/api/config")
async def get_config():
    env = _read_env_file()
    return {
        "dmp_collection_id": env.get("DMP_COLLECTION_ID", ""),
    }


# ── Step 1: Discover ──────────────────────────────────────────────────────────

@app.post("/api/step/discover")
async def step_discover():
    try:
        raw = await _call(AI_GOVERNANCE_URL, "list_catalog_tables", {
            "max_results":     5000,
            "group_by_source": True,
        })
        catalog_sources = raw.get("catalog_sources_grouped", [])
        tables_for_selection = [
            {"name": t["name"], "schema": s["schema"], "source": cs["source"]}
            for cs in catalog_sources
            for s in cs.get("schemas", [])
            for t in s.get("tables", [])
        ]
        raw["tables_for_selection"]     = tables_for_selection
        raw["awaiting_table_selection"] = True
        return {
            "step":      "list_catalog",
            "reasoning": "Listing GOVTEST catalog sources with sample tables from CDGC",
            "result":    raw,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Step 2: Scan ──────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    table: str
    schema: str
    scan_all: bool = False
    table_names: list[str] = []       # pre-resolved names from discover results (for scan_all)
    total_tables_in_schema: int = 0   # total tables in the selected schema (for time estimate)


@app.post("/api/step/scan")
async def step_scan(req: ScanRequest):
    try:
        # Resolve table names for this schema — bypass LLM govern routing to prevent
        # session state from a previous scan overriding the user's current selection.
        if req.scan_all or not req.table:
            # Use pre-resolved table names sent by the frontend from discover results —
            # avoids a second CDGC round-trip that can return 0 due to session/relevance caps.
            table_names = req.table_names[:10] if req.table_names else []
        else:
            table_names = [req.table]

        if not table_names:
            return {
                "scan": {"step": "scan", "result": {"found_count": 0, "missing": [], "tables": [], "next_actions": []}},
                "columns": [],
            }

        t0 = _time.monotonic()

        find_result = await _call(AI_GOVERNANCE_URL, "scan_find_tables", {
            "table_names": table_names,
            "schema_hint": req.schema,
        })
        fetch_actions = [
            a for a in find_result.get("next_actions", [])
            if a.get("tool") == "scan_fetch_columns"
        ]

        async def _fetch_one(p: dict) -> dict:
            return await _call(AI_GOVERNANCE_URL, "scan_fetch_columns", {
                "table_name":  p["table_name"],
                "table_id":    p["table_id"],
                "schema":      p.get("schema", ""),
                "external_id": p.get("external_id", ""),
            })

        columns = list(await asyncio.gather(*[_fetch_one(a["params"]) for a in fetch_actions]))

        elapsed         = round(_time.monotonic() - t0, 1)
        tables_scanned  = len([c for c in columns if c])
        total_in_schema = req.total_tables_in_schema or 0
        per_table_s     = (elapsed / tables_scanned) if tables_scanned > 0 else 0
        est_full_min    = round(per_table_s * total_in_schema / 60, 1) if total_in_schema > 0 else 0

        return {
            "scan":                  {"step": "scan", "result": find_result},
            "columns":               columns,
            "elapsed_seconds":       elapsed,
            "tables_scanned":        tables_scanned,
            "total_in_schema":       total_in_schema,
            "estimated_full_minutes": est_full_min,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Full-schema time estimate (shared by steps 2-6) ───────────────────────────
def _estimate_block(elapsed: float, sample_tables: int, total_tables: int,
                    fixed_cost: bool = False) -> dict:
    """Full-schema runtime estimate from a sample run.

    Per-table steps (scan/taxonomy/curate/dq): full = (elapsed / sample) * total.
    Fixed-cost steps (domain structure — a one-time hierarchy write that does NOT
    grow with table count): the full-schema time is just the observed elapsed, and
    fixed_cost=True tells the UI to show elapsed only (no table extrapolation).
    Returned fields match the Scan step so the UI renders them uniformly.
    """
    if fixed_cost:
        return {
            "elapsed_seconds":        round(elapsed, 1),
            "sample_tables":          sample_tables,
            "total_in_schema":        total_tables,
            "estimated_full_minutes": round(elapsed / 60, 1),
            "fixed_cost":             True,
        }
    per = (elapsed / sample_tables) if sample_tables > 0 else 0.0
    est_min = round(per * total_tables / 60, 1) if (total_tables > 0 and sample_tables > 0) else 0
    return {
        "elapsed_seconds":        round(elapsed, 1),
        "sample_tables":          sample_tables,
        "total_in_schema":        total_tables,
        "estimated_full_minutes": est_min,
    }


class EstimateRequest(BaseModel):
    sample_tables: int = 0        # tables in the scanned sample (for time estimate)
    total_in_schema: int = 0      # full-schema table count (for time estimate)


# ── Step 3: Taxonomy ──────────────────────────────────────────────────────────

class TaxonomyRequest(BaseModel):
    table_names: list[str] = []   # scanned table names — loaded from cache server-side
    sample_tables: int = 0        # tables in the scanned sample (for time estimate)
    total_in_schema: int = 0      # full-schema table count (for time estimate)

@app.post("/api/step/taxonomy")
async def step_taxonomy(req: TaxonomyRequest = TaxonomyRequest()):
    try:
        t0 = _time.monotonic()
        out = await _call(AI_GOVERNANCE_URL, "generate_governance_taxonomy", {
            "table_names": req.table_names or [],
        })
        out = dict(out) if isinstance(out, dict) else {"result": out}
        # Prefer the actual count of tables the tool processed (from the scan cache) —
        # the frontend's sample_tables can be 0 if the scan wasn't run this session.
        processed = ((out.get("_summary") or {}).get("tables_processed")
                     or (out.get("result") or {}).get("_summary", {}).get("tables_processed"))
        sample = processed or req.sample_tables or len(req.table_names or [])
        out.update(_estimate_block(_time.monotonic() - t0, sample, req.total_in_schema))
        return out
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Step 4: Domain Structure ──────────────────────────────────────────────────

@app.post("/api/step/domain_structure/preview")
async def step_domain_structure_preview(req: EstimateRequest = EstimateRequest()):
    try:
        t0 = _time.monotonic()
        out = await _govern("Create the domain structure in CDGC", step="domain_structure")
        out = dict(out) if isinstance(out, dict) else {"result": out}
        # Fixed-cost: writing the domain hierarchy is a one-time op, not per-table.
        out.update(_estimate_block(_time.monotonic() - t0, req.sample_tables,
                                   req.total_in_schema, fixed_cost=True))
        return out
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ApproveDomainRequest(BaseModel):
    approved_names: list[str]
    renames: dict[str, str] | None = None


@app.post("/api/step/domain_structure/approve")
async def step_domain_structure_approve(req: ApproveDomainRequest):
    try:
        args: dict = {"approved_names": req.approved_names}
        if req.renames:
            args["renames"] = req.renames
        return await _call(AI_GOVERNANCE_URL, "approve_domain_structure", args)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/step/domain_structure")
async def step_domain_structure():
    try:
        return await _govern("Create the domain structure in CDGC", step="domain_structure")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Step 5: Register System & Dataset ────────────────────────────────────────

@app.post("/api/step/system_dataset")
async def step_system_dataset():
    try:
        return await _govern("Register the source system and dataset in CDGC", step="system_dataset")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Step 6: Curate ────────────────────────────────────────────────────────────

@app.post("/api/step/curate")
async def step_curate(req: EstimateRequest = EstimateRequest()):
    try:
        t0 = _time.monotonic()
        plan = await _govern("Link the columns to their business terms", step="curate")
        plan_error = plan.get("error") or (plan.get("result") or {}).get("error")
        if plan_error:
            raise HTTPException(status_code=400, detail=f"Curate plan failed: {plan_error}")
        batch_count = plan.get("result", {}).get("batch_count", 0)
        batch_size  = plan.get("result", {}).get("batch_size", 40)
        if batch_count == 0:
            raise HTTPException(status_code=400, detail="No columns found to curate. Ensure scan completed successfully.")
        batches: list[dict] = []
        for i in range(batch_count):
            r = await _call(AI_GOVERNANCE_URL, "curate_batch", {
                "batch_index": i, "batch_size": batch_size,
            })
            if r.get("error"):
                raise HTTPException(status_code=400, detail=f"curate_batch[{i}] error: {r['error']}")
            batches.append(r)
            if r.get("done"):
                break
        return {"plan": plan, "batches": batches,
                **_estimate_block(_time.monotonic() - t0, req.sample_tables, req.total_in_schema)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Step 7: DQ Rules ──────────────────────────────────────────────────────────

@app.post("/api/step/dq_rules")
async def step_dq_rules():
    try:
        plan = await _govern("Create DQ rules for the scanned table", step="dq_rules")
        next_actions = plan.get("result", {}).get("next_actions", [])
        rules: dict | None = None
        for action in next_actions:
            if action.get("tool") == "create_generic_dq_rules":
                p = action["params"]
                call_params = {
                    "table_name":     p["table_name"],
                    "column_ids":     p["column_ids"],
                    "catalog_origin": p["catalog_origin"],
                }
                if p.get("source_table_path"):
                    call_params["source_table_path"] = p["source_table_path"]
                rules = await _call(GOVERNANCE_ENGINE_URL, "create_generic_dq_rules", call_params)
        if rules:
            occurrences = rules.get("occurrences_registered", [])
            if occurrences:
                await _call(AI_GOVERNANCE_URL, "set_dq_occurrences", {"occurrences": occurrences})
        return {"plan": plan, "rules": rules}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Step 8: Propagate Scores ──────────────────────────────────────────────────

@app.post("/api/step/scores")
async def step_scores():
    try:
        plan = await _govern("Propagate the DQ scores to CDGC", step="propagate_scores")
        next_actions = plan.get("result", {}).get("next_actions", [])
        scores: list[dict] = []
        for action in next_actions:
            if action.get("tool") == "upload_dq_scores":
                p = action["params"]
                r = await _call(GOVERNANCE_ENGINE_URL, "upload_dq_scores", {
                    "asset_id":    p["asset_id"],
                    "value":       p.get("value", 95),
                    "total_count": p.get("total_count", 100),
                    "exception":   p.get("exception", 5),
                })
                r["name"]      = p.get("name", "")
                r["column"]    = p.get("column", "")
                r["dimension"] = p.get("dimension", "")
                scores.append(r)
        return {"plan": plan, "scores": scores, "pushed": len(scores)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Step 9: MCC Scan ─────────────────────────────────────────────────────────

@app.post("/api/step/mcc_scan")
async def step_mcc_scan():
    try:
        return await _govern("Trigger the MCC Data Quality scan", step="mcc_scan")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Step 10: Publish to Marketplace ──────────────────────────────────────────

# ── Steps 10–13: Informatica Data Marketplace ────────────────────────────────

@app.post("/api/step/cdmp_category")
async def step_cdmp_category():
    try:
        return await _call(AI_GOVERNANCE_URL, "create_cdmp_category", {})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/step/cdmp_data_asset")
async def step_cdmp_data_asset():
    try:
        return await _call(AI_GOVERNANCE_URL, "create_cdmp_data_asset", {})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/step/cdmp_collection")
async def step_cdmp_collection():
    try:
        return await _call(AI_GOVERNANCE_URL, "create_cdmp_data_collection", {})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/step/publish_marketplace")
async def step_publish_marketplace():
    try:
        return await _call(AI_GOVERNANCE_URL, "publish_cdmp_collection", {})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/step/usage_context")
async def step_usage_context():
    try:
        return await _call(AI_GOVERNANCE_URL, "create_cdmp_usage_contexts", {})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/step/delivery_template")
async def step_delivery_template():
    try:
        return await _call(AI_GOVERNANCE_URL, "create_delivery_template", {})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/step/terms_of_use")
async def step_terms_of_use():
    try:
        return await _call(AI_GOVERNANCE_URL, "create_terms_of_use", {})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/step/delivery_target")
async def step_delivery_target():
    try:
        return await _call(AI_GOVERNANCE_URL, "create_delivery_target", {})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/step/data_quality")
async def step_data_quality(req: EstimateRequest = EstimateRequest()):
    result = {}
    t0 = _time.monotonic()
    try:
        result["dq_rules"] = await step_dq_rules()
    except Exception as e:
        result["dq_rules"] = {"status": "failed", "error": str(e)}
    try:
        result["scores"] = await step_scores()
    except Exception as e:
        result["scores"] = {"status": "failed", "error": str(e)}
    result.update(_estimate_block(_time.monotonic() - t0, req.sample_tables, req.total_in_schema))
    return result


@app.post("/api/step/create_collection")
async def step_create_collection():
    result = {}
    for tool, key in [
        ("create_cdmp_category",        "category"),
        ("create_cdmp_data_asset",      "data_asset"),
        ("create_cdmp_data_collection", "collection"),
    ]:
        try:
            result[key] = await _call(AI_GOVERNANCE_URL, tool, {})
        except Exception as e:
            result[key] = {"status": "failed", "error": str(e)}
    return result


@app.post("/api/step/publish_marketplace_full")
async def step_publish_marketplace_full():
    result = {}
    for tool, key in [
        ("create_cdmp_category",       "category"),
        ("create_cdmp_data_asset",     "data_asset"),
        ("create_cdmp_data_collection","collection"),
        ("publish_cdmp_collection",    "publish"),
    ]:
        try:
            result[key] = await _call(AI_GOVERNANCE_URL, tool, {})
        except Exception as e:
            result[key] = {"status": "failed", "error": str(e)}
    return result


@app.post("/api/step/configure_delivery")
async def step_configure_delivery():
    result = {}
    for tool, key in [
        ("create_cdmp_usage_contexts", "usage_context"),
        ("create_delivery_template",   "delivery_template"),
        ("create_terms_of_use",        "terms_of_use"),
        ("create_delivery_target",     "delivery_target"),
    ]:
        try:
            result[key] = await _call(AI_GOVERNANCE_URL, tool, {})
        except Exception as e:
            result[key] = {"status": "failed", "error": str(e)}
    return result


@app.post("/api/step/consumer_access")
async def step_consumer_access():
    try:
        return await _call(AI_GOVERNANCE_URL, "create_consumer_access", {})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/step/auto_approve_access")
async def step_auto_approve_access():
    try:
        order = await _call(AI_GOVERNANCE_URL, "create_consumer_access", {})
        approve = await _call(AI_GOVERNANCE_URL, "approve_consumer_order", {})
        return {**order, "approved": approve, "auto_approved": True, "status": approve.get("status", order.get("status", "FULFILLED"))}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/step/approve_order")
async def step_approve_order():
    try:
        return await _call(AI_GOVERNANCE_URL, "approve_consumer_order", {})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/step/verify_access")
async def step_verify_access():
    try:
        return await _call(AI_GOVERNANCE_URL, "verify_consumer_access", {})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/step/withdraw_access")
async def step_withdraw_access():
    try:
        return await _call(AI_GOVERNANCE_URL, "withdraw_consumer_access", {})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Static files ──────────────────────────────────────────────────────────────

app.mount("/", StaticFiles(directory=str(Path(__file__).parent / "static"), html=True), name="static")

def main():
    _port = int(os.getenv("GOVERNANCE_UI_PORT", "8080"))
    _host = os.getenv("GOVERNANCE_UI_HOST", "127.0.0.1")
    uvicorn.run("idmc_governance.ui.app:app", host=_host, port=_port, reload=False, log_level="info")


if __name__ == "__main__":
    main()
