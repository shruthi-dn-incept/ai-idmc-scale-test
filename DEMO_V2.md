# IDMC AI Governance — Demo V2 Walkthrough

NLP-driven, end-to-end data governance onboarding using the `ai_governance_mcp.py`
server and the `govern` tool.

**Total time: ~20 minutes**

---

## First-Time Setup (VS Code — run once)

> Do this once after cloning the repo on a new machine. After this, just use Step 0 every demo.

### 1. Run the install script (as Administrator)

```powershell
.\deploy\install.ps1
```

This installs Python dependencies and opens Windows Firewall port 8080. Right-click PowerShell and choose **Run as Administrator** before running.

### 2. Populate `.env`

Copy `.env.example` to `.env` and fill in your values:

```powershell
Copy-Item .env.example .env
```

Required fields (see `.env.example` for comments on where to find each value):

```
IDMC_USER=<your IDMC login email>
IDMC_PASS=<your IDMC login password>
IDMC_LOGIN_HOST=dm-us.informaticacloud.com
IDMC_ORG_ID=<found in IDMC → Admin → Organisation Details>
IDMC_SERVER_URL=<https://<pod>.dm-us.informaticacloud.com/saas>
IDMC_FRS_HOST=<same pod as IDMC_SERVER_URL>
CDGC_API_BASE=https://cdgc-api.dm-us.informaticacloud.com
CDQ_FOLDER_ID=<CDQ → Rule Specification folder → URL bar>
ANTHROPIC_API_KEY=sk-ant-...
```

Auto-managed vars (`IDMC_SESSION_ID`, `IDMC_JWT`, etc.) are written at runtime — leave them blank.

### 3. Trust the VS Code MCP config (first time only)

Open VS Code in the project root. When prompted to allow `.vscode/mcp.json`, click **Allow**.
If not prompted: `Ctrl+Shift+P` → **MCP: List Servers** — both servers should appear.

---

## Prerequisites (every demo run)

| Requirement | Check |
|---|---|
| VS Code + Claude Code extension | Installed and signed in |
| This repo cloned | `c:\...\idmc-governance-engine` |
| `.env` populated | `IDMC_USER`, `IDMC_PASS`, `IDMC_LOGIN_HOST`, `IDMC_ORG_ID`, `CDGC_API_BASE`, `ANTHROPIC_API_KEY` |
| Dependencies installed | `pip install -r requirements.txt` |

---

## 0. Start the servers (~2 min)

Open a PowerShell terminal in the project root and run the full block:

```powershell
# Create logs folder if it doesn't exist
New-Item -ItemType Directory -Force logs | Out-Null

# Load env vars from .env into the current process
Get-Content .env | Where-Object { $_ -match "^\s*[^#]\S+=\S" } | ForEach-Object {
    $k, $v = $_ -split "=", 2
    [System.Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), "Process")
}

# Start all three servers in the background
Start-Process python -ArgumentList "governance_engine_mcp.py" -RedirectStandardOutput "logs\governance-engine.log" -RedirectStandardError "logs\governance-engine.err" -WindowStyle Hidden
Start-Process python -ArgumentList "ai_governance_mcp.py"     -RedirectStandardOutput "logs\ai-governance.log"     -RedirectStandardError "logs\ai-governance.err"     -WindowStyle Hidden
Start-Process python -ArgumentList "governance_ui.py"         -RedirectStandardOutput "logs\governance-ui.log"     -RedirectStandardError "logs\governance-ui.err"      -WindowStyle Hidden

# Verify all three are up
Start-Sleep 4
foreach ($port in 8765, 8770, 8080) {
  try   { Invoke-WebRequest "http://127.0.0.1:$port/" -Method GET -TimeoutSec 3 | Out-Null; "port $port — UP" }
  catch { if ($_.Exception.Response.StatusCode.value__) { "port $port — UP" } else { "port $port — DOWN" } }
}
```

Expected output:
```
port 8765 — UP    ← governance-engine (DQ rules)
port 8770 — UP    ← ai-governance (NLP pipeline)
port 8080 — UP    ← branded wizard UI
```

> **Kill a server before restarting** (if a port is already in use):
> ```powershell
> $p = (netstat -ano | Select-String ":8770.*LISTENING" | ForEach-Object { ($_ -split "\s+")[-1] } | Select-Object -First 1)
> Stop-Process -Id $p -Force
> ```
> Replace `8770` with `8765` or `8080` as needed.

Reload the VS Code window so it picks up `.vscode/mcp.json`:
`Ctrl+Shift+P` → **Developer: Reload Window**

> **Open the UI:** [http://127.0.0.1:8080](http://127.0.0.1:8080)

> **Logs:** `logs\ai-governance.err` / `logs\governance-engine.err` / `logs\governance-ui.err`

---

## Running the demo — two modes

| Mode | How | Best for |
|---|---|---|
| **Branded UI** | Open http://127.0.0.1:8080, click through wizard | Customer-facing demos |
| **Claude Code** | Type prompts directly in VS Code chat | Developer / technical demos |

Both modes call the same MCP servers. Steps below show both the UI button label and the Claude Code prompt.

---

## 1. Discover the catalog (~2 min)

**UI:** Click **Run Step** on "Discover Catalog"

**Claude Code:**
> **"Show me what schemas and tables are available in the CDGC catalog."**

Dispatches: `govern(...)` → `list_catalog_tables()`

**Expected output:**
```json
{
  "step": "list_catalog",
  "result": {
    "schemas": [
      { "schema": "FINANCE_ERP_DQ",    "tables": ["CUSTOMER_ADDRESSES_FINAL_CLASSIFIED_VALIDATED_WITHID", "..."] },
      { "schema": "FINANCE_ERP_RAW",   "tables": ["PBNA_DIAGNOSTIC", "RAW_BUSINESS_UNIT"] },
      { "schema": "FINANCE_ERP_STAGE", "tables": ["SUPPLIER_ADD_STAGE", "SUPPLIER_SITE_STAGE", "SUPPLIER_STAGE"] }
    ],
    "total_tables": 9,
    "awaiting_table_selection": true
  },
  "next_step": "scan"
}
```

**Talking points:**
- "No parameters — the tool lists what exists and waits for you to pick."
- "`awaiting_table_selection: true` — pipeline will not auto-proceed until a specific table is named."
- "Table list is live from the CDGC catalog — grows as new sources are onboarded."

---

## 2a. Pick and scan a table (~5 seconds)

**UI:** Select table + schema from dropdowns (populated from Step 1), click **Run Step**

**Claude Code:**
> **"Scan CUSTOMER_ADDRESSES_FINAL_CLASSIFIED_VALIDATED_WITHID from FINANCE_ERP_DQ."**

Dispatches: `govern(...)` → `scan_find_tables(...)`

The selected table is saved to state — every downstream step is scoped to this table only.

## 2b. Fetch columns (~15 seconds, instant on re-runs from cache)

Automatically called after scan. Dispatches: `scan_fetch_columns(...)`

**Expected output:**
```json
{
  "table": "CUSTOMER_ADDRESSES_FINAL_CLASSIFIED_VALIDATED_WITHID",
  "column_count": 15,
  "source": "fetched",
  "columns_preview": ["ID", "STATE", "MELISSA_CODES", "MELISSA_LOCALITY", "MELISSA_VALID", "..."],
  "message": "Fetched and cached 15 columns."
}
```

**Talking points:**
- "Two visible tool calls — audience sees progress, not a silent wait."
- "Cached to `.scan_cache/<TABLE>.json` — re-runs are instant."

---

## 3. Generate governance taxonomy (~2 min)

**UI:** Click **Run Step** on "Generate Taxonomy"

**Claude Code:**
> **"Generate a governance taxonomy for the scanned customer address data."**

Dispatches: `govern(...)` → `generate_governance_taxonomy(uncovered_columns_only)`

**Key behaviour:** Only columns not already linked to a business term are sent to the LLM — no duplicate terms on re-runs.

**Expected output:**
```json
{
  "step": "taxonomy",
  "result": {
    "domains": [{ "name": "Supply Chain", "subdomains": [...] }],
    "_summary": { "domain_count": 1, "subdomain_count": 3, "term_count": 14 }
  },
  "next_step": "domain_structure"
}
```

**Talking points:**
- "Pure LLM — no hardcoded rules. Domain, subdomains, and business terms generated from column names and data types."
- "Only generates terms for columns not already governed — idempotent on re-runs."

---

## 4. Create domain structure in CDGC (~2 min)

**UI:** Click **Run Step** on "Create Domain Structure"

**Claude Code:**
> **"Create the domain structure in CDGC."**

Dispatches: `govern(...)` → `create_domain_structure(taxonomy)`

**Expected output:**
```json
{
  "step": "domain_structure",
  "result": {
    "created": [
      { "type": "SubDomain",    "name": "Customer Address",              "id": "..." },
      { "type": "BusinessTerm", "name": "Validated Address Status",      "id": "..." }
    ],
    "skipped": [ "... terms that already existed" ],
    "summary": { "created_count": 7, "skipped_count": 11, "error_count": 0 }
  },
  "next_step": "system_dataset"
}
```

**Talking points:**
- "Already-existing assets are skipped, not duplicated — safe to re-run."
- "Open CDGC → Business Glossary → Supply Chain: domain, subdomains, and terms are live now."

---

## 5. Register system and dataset (~1 min)

**UI:** Click **Run Step** on "Register System & Dataset"

**Claude Code:**
> **"Register the source system and dataset in CDGC."**

Dispatches: `govern(...)` → `create_system_and_dataset(...)`

**Key behaviours:**
- **System name** derived from the catalog connection (`FUSION_ERP_DEV`).
- **Dataset name** = scanned table name — one dataset per governed table, no duplicates.
- **Column links** — all column internal IDs linked to the dataset as data elements in batches of 20.

**Expected output:**
```json
{
  "step": "system_dataset",
  "result": {
    "system":        { "name": "FUSION_ERP_DEV", "id": "...", "note": "already exists" },
    "dataset":       { "name": "CUSTOMER_ADDRESSES_FINAL_CLASSIFIED_VALIDATED_WITHID", "id": "..." },
    "linked_domain": { "name": "Supply Chain", "id": "..." },
    "data_elements": { "linked": ["<col_id_1>", "...", "<col_id_15>"], "errors": [] }
  },
  "next_step": "curate"
}
```

---

## 6. Curate — link columns to business terms (~20 s per batch)

**UI:** Click **Run Step** on "Curate Columns"

**Claude Code:**
> **"Link the columns to their business terms."**

Dispatches: `govern(...)` → batch plan → `curate_batch(0, 1, ...)` per batch

**Expected output (per batch):**
```json
{
  "batch_index": 0,
  "columns_processed": "1–15 of 15",
  "linked": 15,
  "skipped": 0,
  "total_linked_so_far": 15,
  "progress": "15/15 columns (100%)",
  "done": true
}
```

**Talking points:**
- "Discrete visible tool calls with running progress — audience sees it working in real time."
- "Business terms passed directly from step 4 output — bypasses CDGC's 2-min search-index lag."

---

## 7. Create DQ rules (~1 min) *(skippable)*

**UI:** Click **Run Step** — or click **Skip** to bypass this step for the demo.

**Claude Code:**
> **"Create DQ rules for the scanned table."**

Dispatches: `govern(...)` → `create_generic_dq_rules(table, column_ids, catalog_origin)` → `set_dq_occurrences(...)`

**Column selection** — picks up to 7 representative columns by priority:

| Priority | Pattern | Dimension assigned |
|---|---|---|
| 1 | `_ID`, `_KEY`, `CODE`, `_NO`, `NBR` | COMPLETENESS + VALIDITY (VARCHAR) or UNIQUENESS (NUMBER) |
| 2 | TIMESTAMP / DATE, or `_DATE`, `CREATED` | COMPLETENESS + TIMELINESS |
| 3 | `_TYPE`, `_STATUS`, `_NAME`, `_DESC` | COMPLETENESS + VALIDITY |
| 4 | NUMBER columns not already selected | COMPLETENESS + VALIDITY |
| 5 | Remaining VARCHAR (fills quota to max 7) | COMPLETENESS + VALIDITY |

**Expected output:**
```json
{
  "rules_created": [
    { "rule_name": "DQ_<TABLE>_COMPLETENESS", "dimension": "COMPLETENESS" },
    { "rule_name": "DQ_<TABLE>_VALIDITY",     "dimension": "VALIDITY" }
  ],
  "occurrences_registered": [
    { "column": "ID",            "dimension": "COMPLETENESS", "internal_id": "..." },
    { "column": "MELISSA_CODES", "dimension": "VALIDITY",     "internal_id": "..." },
    "... (~14 total)"
  ],
  "summary": { "rule_count": 2, "occurrence_count": 14, "error_count": 0 }
}
```

**Talking points:**
- "Pipeline analyses column names and types — ID columns, dates, and descriptors each get targeted rules."
- "2 rule specs, ~14 occurrences — targeted coverage, not a rule-per-column flood."
- "Open CDGC → Data Quality tab on any selected column — DQOs are live."

---

## 8. Propagate interim DQ scores (~30 s)

**UI:** Click **Run Step** on "Propagate Scores"

**Claude Code:**
> **"Propagate the DQ scores to CDGC."**

Dispatches: `govern(...)` → `upload_dq_scores(asset_id, value, total_count, exception)` per occurrence

Reads stored occurrence IDs from pipeline state (saved in step 7 via `set_dq_occurrences`).
Uses `governance-engine :8765` → `upload_dq_scores` for reliable score delivery.

**Expected output (per occurrence):**
```json
{ "http_status": 200, "asset_id": "<dqro_internal_id>", "value": 95, "scanned_time": "2026-05-27T..." }
```

**Talking points:**
- "Interim 95% scores pushed immediately — DQROs are visible in CDGC right away."
- "Step 9 will overwrite these with real scores from live data."

---

## 9. Run MCC Data Quality scan (~1–3 min)

**UI:** Click **Run Step** on "Run MCC Scan"

**Claude Code:**
> **"Trigger the MCC Data Quality scan."**

Dispatches: `govern(...)` → `_trigger_mcc_scan("c46c0515-...", ["Data Quality"])`

Calls: `POST {CDGC_API_BASE}/data360/executable/v1/catalogsource/{CS-ID}`
Body: `{"capabilityNames": ["Data Quality"]}`
Auth: Bearer JWT **+** IDS-SESSION-ID (both required).

**Expected output:**
```json
{
  "status": "SUBMITTED",
  "job_id": "8d8df0ce-e516-43c2-a303-2d53ff051159",
  "catalog_source": "CDGC-SNOWFLAKE-TERDEV",
  "capabilities": ["Data Quality"],
  "note": "MCC Data Quality scan submitted. CDQ rules will run against live Snowflake data (5000 rows) and publish real scores to DQROs, overwriting the interim scores."
}
```

**Talking points:**
- "MCC runs all CDQ rule specs linked to the DQROs against live Snowflake data — 5000 rows sampled."
- "Scores are automatically published back to CDGC. No CDI mapping tasks needed."
- "Open Data Quality tab on RAW_AUDIT_LOG after ~2 min — real scores replace the 95% placeholders."
- "Example: `DQ_BATCH_ID_UNIQUENESS` → 73.42% (55 failed / 207 rows) — genuine data quality insight."

---

## 10. Create Marketplace category (~5 s)

**UI:** Click **Run Step** on "Create Category"

**Claude Code:**
> **"Create a Data Marketplace category for the governed data."**

Dispatches: `govern(...)` → `create_cdmp_category()`

Auto-derives the category name from the governance domain created in Step 3.

**Expected output:**
```json
{
  "id": "abc123...",
  "name": "Organization Management",
  "status": "created",
  "http_status": 201
}
```

**Talking points:**
- "Category name derived from the governance domain — no manual entry."
- "Already-existing categories are reused, not duplicated — safe to re-run."

---

## 11. Prepare Data Asset (~2 s)

**UI:** Click **Run Step** on "Prepare Data Asset"

**Claude Code:**
> **"Prepare the data asset for Marketplace publishing."**

Dispatches: `govern(...)` → `create_cdmp_data_asset()`

Creates the Data Asset via CDMP API, then registers each governed column as a Data Element enriched with its business term definition from Step 3. Consumers see the full schema and term descriptions before placing an order.

**Expected output:**
```json
{
  "id":                       "asset-abc...",
  "name":                     "RAW_BUSINESS_UNIT",
  "description":              "Governed dataset from the Organization Management domain. Catalogued, business-term linked, and quality-scored in CDGC. DQ dimensions: COMPLETENESS, TIMELINESS, VALIDITY.",
  "table_id":                 "12e2f161-...",
  "domain":                   "Organization Management",
  "column_count":             13,
  "data_elements_registered": 13,
  "data_elements_errors":     [],
  "status":                   "created",
  "http_status":              201
}
```

**Talking points:**
- "Each column registered as a Data Element — consumers see the full schema before ordering."
- "Business term definitions from Step 3 flow through as Data Element descriptions — governed context reaches the Marketplace."

---

## 12. Create Data Collection (~5 s)

**UI:** Click **Run Step** on "Create Data Collection"

**Claude Code:**
> **"Create a Data Collection in Marketplace."**

Dispatches: `govern(...)` → `create_cdmp_data_collection()`

Creates a PUBLISHED Data Collection under the category from Step 10, bundling the asset from Step 11.

**Expected output:**
```json
{
  "id":              "coll-xyz...",
  "externalId":      "ext-abc...",
  "name":            "RAW_BUSINESS_UNIT — Governed Dataset",
  "asset_linked":    true,
  "status":          "created",
  "http_status":     201,
  "marketplace_url": "https://cdmp-app.dm-us.informaticacloud.com/data-collections/coll-xyz..."
}
```

**Talking points:**
- "Collection is PUBLISHED on creation — consumers can discover it immediately."
- "`asset_linked: true` confirms the Data Asset — Data Collection relationship is registered."
- "Name auto-generated: `<TABLE> — Governed Dataset`."

---

## 13. Publish to Marketplace (~3 s)

**UI:** Click **Run Step** on "Publish to Marketplace"

**Claude Code:**
> **"Publish the data collection to the Marketplace."**

Dispatches: `govern(...)` → `publish_cdmp_collection()`

Verifies the collection is live and returns the direct Marketplace URL for the consumer.

**Expected output:**
```json
{
  "collection_id": "coll-xyz...",
  "external_id":   "ext-abc...",
  "status":        "PUBLISHED",
  "published":     true,
  "http_status":   200,
  "marketplace_url": "https://cdmp-app.dm-us.informaticacloud.com/data-collections/coll-xyz..."
}
```

**Talking points:**
- "Open the `marketplace_url` — consumers can now discover and request access."
- "Full producer loop complete: catalogued → governed → quality-scored → published to Marketplace."

---

## 14. Create Delivery Template (~5 s)

**UI:** Click **Run Step** on "Create Delivery Template"

**Claude Code:**
> **"Create a delivery template for the Marketplace collection."**

Dispatches: `govern(...)` → `create_delivery_template()`

Creates a Delivery Template (default: `DOWNLOAD`) and attaches it to the published collection. Consumers see this when placing an order.

**Expected output:**
```json
{
  "id":            "tmpl-abc...",
  "name":          "RAW_BUSINESS_UNIT — Download Delivery",
  "method":        "DOWNLOAD",
  "collection_id": "coll-xyz...",
  "attached":      true,
  "http_status":   201
}
```

**Talking points:**
- "Delivery method defaults to DOWNLOAD — can be overridden to API_ACCESS or SNOWFLAKE_SHARE."
- "Attached to the collection automatically — consumers see the delivery option when ordering."

---

## 15. Create Terms of Use (~5 s)

**UI:** Click **Run Step** on "Create Terms of Use"

**Claude Code:**
> **"Create terms of use for the Marketplace collection."**

Dispatches: `govern(...)` → `create_terms_of_use()`

Creates a Terms of Use agreement auto-generated from the governed domain and table, then attaches it to the collection. Consumers must accept before their order is fulfilled.

**Expected output:**
```json
{
  "id":            "tou-def...",
  "name":          "RAW_BUSINESS_UNIT — Terms of Use",
  "collection_id": "coll-xyz...",
  "attached":      true,
  "http_status":   201
}
```

**Talking points:**
- "Terms auto-generated from domain name and asset — no manual writing."
- "Consumers must accept before access is granted — enforces governance at the consumption layer."
- "Full loop complete: governed → published → orderable → terms-protected."

---

## 16. Create Delivery Target (~5 s)

**UI:** Click **Run Step** on "Create Delivery Target"

**Claude Code:**
> **"Create a delivery target for the Marketplace collection."**

Dispatches: `govern(...)` → `create_delivery_target()`

Creates a Delivery Target (default: Snowflake) and links it to the collection. Defines where data lands once a consumer order is approved — completing the `Delivery Target — Data Collection` relationship.

**Expected output:**
```json
{
  "id":            "tgt-abc...",
  "name":          "RAW_BUSINESS_UNIT — Snowflake Target",
  "target_type":   "SNOWFLAKE",
  "collection_id": "coll-xyz...",
  "linked":        true,
  "http_status":   201
}
```

**Talking points:**
- "Target type defaults to Snowflake — can be overridden to S3 or ADLS."
- "Linked to the collection automatically — consumers see where data will be delivered when ordering."

---

## 17. Provision Consumer Access (~5 s)

**UI:** Click **Run Step** on "Provision Consumer Access"

**Claude Code:**
> **"Provision consumer access for the Marketplace collection."**

Dispatches: `govern(...)` → `create_consumer_access()`

Places an approved order on behalf of a consumer (defaults to `IDMC_USER` from `.env`) and provisions access. Completes the full producer → consumer loop.

**Expected output:**
```json
{
  "order_id":       "ord-def...",
  "collection_id":  "coll-xyz...",
  "consumer":       "analyst@example.com",
  "status":         "APPROVED",
  "access_granted": true,
  "http_status":    201,
  "marketplace_url": "https://cdmp-app.dm-us.informaticacloud.com/data-collections/coll-xyz..."
}
```

**Talking points:**
- "Full loop complete: data governed in CDGC → packaged → published → ordered → access granted."
- "Consumer email defaults to `IDMC_USER` from `.env` — override to any user for the demo."
- "Open the `marketplace_url` to show the live collection with active consumer access."

---

## Full pipeline recap

| # | UI label | Plain-English prompt (Claude Code) | Tool(s) dispatched |
|---|---|---|---|
| 1 | Discover Catalog | "Show me what schemas and tables are in the catalog" | `list_catalog_tables` |
| 2a | Scan Table | "Scan TABLE from SCHEMA" | `scan_find_tables` (~5s) |
| 2b | *(auto)* | — | `scan_fetch_columns` (~15s, cached on re-runs) |
| 3 | Generate Taxonomy | "Generate a governance taxonomy for the scanned data" | `generate_governance_taxonomy` (uncovered cols only) |
| 4 | Create Domain Structure | "Create the domain structure in CDGC" | `create_domain_structure` |
| 5 | Register System & Dataset | "Register the source system and dataset" | `create_system_and_dataset` (column links batched ×20) |
| 6 | Curate Columns | "Link the columns to their business terms" | `govern` returns batch plan → `curate_batch(0, 1, …)` |
| 7 | Create DQ Rules *(skippable)* | "Create DQ rules for the scanned table" | `create_generic_dq_rules` → `set_dq_occurrences` |
| 8 | Propagate Interim Scores | "Propagate the DQ scores to CDGC" | `upload_dq_scores` per occurrence (95% placeholder) |
| 9 | Run MCC Scan | "Trigger the MCC Data Quality scan" | `_trigger_mcc_scan` → MCC runs CDQ rules on live data → real scores auto-published |
| 10 | Create Category | "Create a Data Marketplace category for the governed data" | `create_cdmp_category` (reuses existing if found) |
| 11 | Prepare Data Asset | "Prepare the data asset for Marketplace publishing" | `create_cdmp_data_asset` (state bundle — no API call) |
| 12 | Create Data Collection | "Create a Data Collection in Marketplace" | `create_cdmp_data_collection` → PUBLISHED on creation |
| 13 | Publish to Marketplace | "Publish the data collection to the Marketplace" | `publish_cdmp_collection` → returns live Marketplace URL |
| 14 | Create Delivery Template | "Create a delivery template for the Marketplace collection" | `create_delivery_template` → attaches to collection |
| 15 | Create Terms of Use | "Create terms of use for the Marketplace collection" | `create_terms_of_use` → attaches to collection |
| 16 | Create Delivery Target | "Create a delivery target for the Marketplace collection" | `create_delivery_target` → links Delivery Target to Collection |
| 17 | Provision Consumer Access | "Provision consumer access for the Marketplace collection" | `create_consumer_access` → places order + grants access |

State flows automatically between steps via `.scan_cache/govern_state.json` — no parameters to pass between calls.

---

## Architecture

```
Browser (http://127.0.0.1:8080)
       │  REST API
       ▼
governance-ui :8080  (governance_ui.py — FastAPI wizard UI)
       │
       ├──────────────────────────────────────────────────┐
       ▼                                                  ▼
ai-governance :8770                          governance-engine :8765
  govern(request)  ← NLP dispatcher            create_generic_dq_rules()
    │                                           set_dq_occurrences()
    ├── list_catalog_tables()   → CDGC search   upload_dq_scores()  → CDGC ruleautomation
    ├── scan_find_tables()      → CDGC search
    ├── scan_fetch_columns()    → CDGC asset API  (cached to .scan_cache/)
    ├── generate_governance_taxonomy() → Anthropic Claude API
    │     └── pre-filters columns already linked in CDGC
    ├── create_domain_structure()  → CDGC content API
    ├── create_system_and_dataset() → CDGC content API (column links in batches of 20)
    ├── curate_batch()             → CDGC publish API
    ├── _trigger_mcc_scan()        → CDGC executable API (POST /data360/executable/v1/catalogsource/{id})
    │                                 MCC runs CDQ rules on live Snowflake data → real scores → DQROs
    ├── create_cdmp_category()     → Data Marketplace API (category)
    ├── create_cdmp_data_asset()   → state bundle (no API call)
    ├── create_cdmp_data_collection() → Data Marketplace API (collection, PUBLISHED)
    └── publish_cdmp_collection()  → Data Marketplace API (verify + return URL)

Also usable directly via Claude Code (VS Code) — same MCP servers, no UI needed.
```

| Server | Port | Role |
|---|---|---|
| governance-ui | 8080 | Branded wizard UI — start here for customer demos |
| ai-governance | 8770 | NLP pipeline: steps 1–9, 10–13 (incl. Data Marketplace) |
| governance-engine | 8765 | DQ rules + DQRO registration + score upload: steps 7–8 |

---

## Auth layers

| Surface | Auth header | How it's obtained |
|---|---|---|
| CDGC search + content APIs | `Authorization: Bearer <JWT>` | `_get_jwt()` / `_mint_jwt()` — 29-min cache, auto-refreshes on 401 |
| IDMC v2 (login, CDI) | `IDS-SESSION-ID` | `_login_v2()` — auto-minted, persisted to `.env` |
| IDMC v3 platform | `INFA-SESSION-ID` | `_login_v3()` — minted on demand |
| Anthropic (LLM calls) | `x-api-key` | `ANTHROPIC_API_KEY` from `.env` |
| FRS (rule specs) | `IDS-SESSION-ID` | Same v2 session — auto-refreshes on 401 |

> **Important:** `IDMC_FRS_HOST`, `CDQ_FOLDER_ID`, and related vars must be set as
> **process environment variables** before starting governance-engine — they are read via
> `os.getenv()` at module load time, not from `.env`.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| UI page blank after loading | `export default` in Babel standalone context | Fixed in latest — use hard-reload (Ctrl+Shift+R) |
| UI shows "port 8080 — DOWN" | `governance_ui.py` not started | Run Step 0 block again; check `logs\governance-ui.err` |
| Pipeline auto-scans wrong table after catalog | `awaiting_table_selection` flag not set | Upgrade to latest `ai_governance_mcp.py` |
| `generate_governance_taxonomy` crashes on columns | Columns passed as strings not dicts | Fixed in latest — call via `govern`, not directly |
| Step 5 `data_elements.errors` with `RELATIONSHIP_ALREADY_EXISTS` | Re-run on already-linked columns | Expected/safe — treated as success in latest |
| Step 5 429 rate limit on columns | >20 column links in one request | Fixed — batches in groups of 20 |
| `propagate_dq_score` returns 500 `responseCode: 501` | ai-governance JWT doesn't satisfy ruleautomation API | Fixed — step 8 now dispatches `upload_dq_scores` via governance-engine :8765 instead |
| `govern` returns `error: No table names specified` | Ambiguous scan request | Explicitly name the table: "Scan TABLE_NAME from SCHEMA_NAME" |
| HTTP 401 on CDGC calls mid-run | JWT expired (~29 min) | Auto-refreshes — retry the call |
| HTTP 401 on FRS/DQ calls | FRS session expired | Restart governance-engine with env vars set (see step 0) |
| `Folders('None')/Documents` error | `CDQ_FOLDER_ID` not in process env | Set env vars before `Start-Process` (see step 0) |
| Multiple datasets in CDGC for same table | Old behaviour — dataset name came from LLM | Fixed — dataset name always = scanned table name |
| Taxonomy generates duplicate terms | Old behaviour | Fixed — pre-filters columns already linked to a BT |

---

## Resetting state between demo runs

```powershell
# Clear govern step state (forces re-discovery next run)
Remove-Item ".scan_cache\govern_state.json" -ErrorAction SilentlyContinue

# Clear a specific table's column cache (forces re-fetch from CDGC)
Remove-Item ".scan_cache\CUSTOMER_ADDRESSES_FINAL_CLASSIFIED_VALIDATED_WITHID.json" -ErrorAction SilentlyContinue
```
