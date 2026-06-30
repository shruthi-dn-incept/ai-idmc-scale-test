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
    p = Path(__file__).parent / ".env"
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


async def _govern(request: str) -> dict:
    return await _call(AI_GOVERNANCE_URL, "govern", {"request": request})


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
        return await _govern(
            "show me what schemas and tables are available in the CDGC catalog"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Step 2: Scan ──────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    table: str
    schema: str


@app.post("/api/step/scan")
async def step_scan(req: ScanRequest):
    try:
        scan = await _govern(f"Scan {req.table} from {req.schema}")
        next_actions = scan.get("result", {}).get("next_actions", [])
        columns: list[dict] = []
        for action in next_actions:
            if action.get("tool") == "scan_fetch_columns":
                p = action["params"]
                col = await _call(AI_GOVERNANCE_URL, "scan_fetch_columns", {
                    "table_name": p["table_name"],
                    "table_id":   p["table_id"],
                    "schema":     p.get("schema", ""),
                    "external_id": p.get("external_id", ""),
                })
                columns.append(col)
        return {"scan": scan, "columns": columns}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Step 3: Taxonomy ──────────────────────────────────────────────────────────

@app.post("/api/step/taxonomy")
async def step_taxonomy():
    try:
        return await _govern("Generate a governance taxonomy for the scanned data")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Step 4: Domain Structure ──────────────────────────────────────────────────

@app.post("/api/step/domain_structure/preview")
async def step_domain_structure_preview():
    try:
        return await _govern("Create the domain structure in CDGC")
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
        return await _govern("Create the domain structure in CDGC")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Step 5: Register System & Dataset ────────────────────────────────────────

@app.post("/api/step/system_dataset")
async def step_system_dataset():
    try:
        return await _govern("Register the source system and dataset in CDGC")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Step 6: Curate ────────────────────────────────────────────────────────────

@app.post("/api/step/curate")
async def step_curate():
    try:
        plan = await _govern("Link the columns to their business terms")
        batch_count = plan.get("result", {}).get("batch_count", 1)
        batch_size  = plan.get("result", {}).get("batch_size", 40)
        batches: list[dict] = []
        for i in range(batch_count):
            r = await _call(AI_GOVERNANCE_URL, "curate_batch", {
                "batch_index": i, "batch_size": batch_size,
            })
            batches.append(r)
            if r.get("done"):
                break
        return {"plan": plan, "batches": batches}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Step 7: DQ Rules ──────────────────────────────────────────────────────────

@app.post("/api/step/dq_rules")
async def step_dq_rules():
    try:
        plan = await _govern("Create DQ rules for the scanned table")
        next_actions = plan.get("result", {}).get("next_actions", [])
        rules: dict | None = None
        for action in next_actions:
            if action.get("tool") == "create_generic_dq_rules":
                p = action["params"]
                rules = await _call(GOVERNANCE_ENGINE_URL, "create_generic_dq_rules", {
                    "table_name":     p["table_name"],
                    "column_ids":     p["column_ids"],
                    "catalog_origin": p["catalog_origin"],
                })
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
        plan = await _govern("Propagate the DQ scores to CDGC")
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
        return await _govern("Trigger the MCC Data Quality scan")
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
async def step_data_quality():
    result = {}
    try:
        result["dq_rules"] = await step_dq_rules()
    except Exception as e:
        result["dq_rules"] = {"status": "failed", "error": str(e)}
    try:
        result["scores"] = await step_scores()
    except Exception as e:
        result["scores"] = {"status": "failed", "error": str(e)}
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

app.mount("/", StaticFiles(directory="ui_static", html=True), name="static")

if __name__ == "__main__":
    _port = int(os.getenv("GOVERNANCE_UI_PORT", "8080"))
    uvicorn.run("governance_ui:app", host="127.0.0.1", port=_port, reload=False, log_level="info")
