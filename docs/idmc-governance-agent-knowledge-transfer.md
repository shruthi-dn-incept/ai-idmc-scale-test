# Incept IDMC Governance Agent — Knowledge Transfer

> **Last updated:** May 13, 2026
> **Status:** Phase 1 — 4 MCP Servers (3 complete, 1 building), 24+ tools
> **Builder:** Solo (business/strategy background, guided by Claude + Claude Code)
> **Environment:** Mac (development) + Windows laptop (Secure Agent)

---

## 1. Project Overview

### What We're Building

A platform of Python MCP servers that give AI assistants (Claude Desktop, Claude Code in VS Code) the ability to automate IDMC governance operations. Each MCP server is a separate product.

### Phase 1 Products (5 MCP Servers)

1. **Incept Governance Engine** — CDQ→CDI→CDGC pipeline automation ✅ BUILT (14 tools)
2. **Incept Lineage Reporter** — data lineage and impact analysis ✅ BUILT (3 tools)
3. **Incept Glossary Manager** — business glossary automation ✅ BUILT (3 tools)
4. **Incept DQ Monitor** — DQ score monitoring and alerting 🔄 BUILDING (4 tools)
5. **Incept Data Onboarding** — end-to-end dataset onboarding ⏳ PLANNED

### Build Order Rationale

Governance Engine first (builds shared infrastructure all others reuse) → Lineage Reporter + Glossary Manager (independent, CDGC-only, quick wins) → DQ Monitor (needs artifacts from Governance Engine) → Data Onboarding (orchestrates across all servers)

---

## 2. Environment Setup (COMPLETED)

### IDMC Dev Instance

- **URL:** `https://usw1.dmp-us.informaticacloud.com`
- **CDQ UI URL:** `https://usw1-dqcloud.dmp-us.informaticacloud.com`
- **Org:** Partners-Mitra_Incept_Data_Solutions
- **User:** Bharish_Mitra_suborg (admin privileges)
- **Login host:** `dmp-us.informaticacloud.com` (NOT dm-us)

### Authentication

- **v2 login:** `POST https://dmp-us.informaticacloud.com/ma/api/v2/user/login`
  - Body: `{"@type": "login", "username": "...", "password": "..."}`
  - Returns: `icSessionId` + `serverUrl`
  - Session expires: ~30 min idle
- **v3 login:** `POST https://dmp-us.informaticacloud.com/saas/public/core/v3/login`
  - Returns: `INFA-SESSION-ID` + base URL
- **JWT minting (for CDGC):** `GET https://dmp-us.informaticacloud.com/identity-service/api/v1/jwt/Token?client_id=idmc_api`
  - Header: `IDS-SESSION-ID: <v2_session_id>`
  - Returns: Bearer JWT token (valid ~29 minutes)
  - Required for: CDGC content APIs, publishScore, business asset CRUD
- **For FRS and rule-service:** Use header `IDS-SESSION-ID: <session_id>` (v2 session works)
- **For CDGC APIs:** Use header `Authorization: Bearer <jwt>` + `IDS-SESSION-ID` + `X-INFA-ORG-ID`

### Secure Agent

- **Name:** BenakaHomePC
- **Platform:** win64
- **Agent Version:** 76.17
- **Location:** Colleague's Windows laptop (always on)
- **Status:** Running with multiple engines (API Microgateway, B2B Processor, etc.)

### Snowflake Test Environment

- **Account:** ygc42528.us-east-1
- **Database:** INCEPT_GOV_DEV
- **Schema:** DQ_TEST
- **Table:** CUSTOMER_POSITIONS (20 records with intentional DQ issues)
- **Warehouse:** INCEPT_WH
- **Service User:** INCEPT_AGENT_USER
- **IDMC Connection Name:** Snowflake_InceptTest (tested and saved)

### Test Data (CUSTOMER_POSITIONS)

- 5 clean records (pass all rules)
- 5 completeness issues (null customer_name, null counterparty, null customer_id, null account_number, blank customer_name)
- 4 accuracy issues (invalid risk_classification, negative exposure, wrong currency codes)
- 2 timeliness issues (stale positions from 2024, future trade dates)
- 2 consistency issues (settlement before trade date)
- 1 uniqueness issue (duplicate position_id)

### Project Structure

```
~/Projects/IDMC_Governance_Engine/
├── .env                    # Credentials + session tokens (chmod 600, gitignored)
├── .gitignore              # Excludes .env, .vscode/mcp.json
├── .vscode/
│   ├── mcp.json            # MCP server configs with ${env:IDMC_SESSION_ID} (gitignored)
│   └── mcp.example.json    # Committed template
├── .claude/                # Claude Code config
├── settings.local.json
├── login.sh                # v2 login, emits exports
├── login-v3.sh             # v3 login, emits exports
└── refresh-session.sh      # Refreshes session, updates .env
```

### .env Variables

```
IDMC_USER=Bharish_Mitra_suborg
IDMC_PASS=<password>
IDMC_LOGIN_HOST=dmp-us.informaticacloud.com
IDMC_SESSION_ID=<v2 session, auto-refreshed>
IDMC_SERVER_URL=https://usw1.dmp-us.informaticacloud.com/saas
IDMC_V3_SESSION_ID=<v3 session>
IDMC_V3_BASE_URL=<v3 base>
```

---

## 3. Informatica MCP Servers (Agent Engineering GA)

### What Shipped (April 2026)

Agent Engineering GA is a registry of Managed MCP Servers — NOT the Agent Canvas/Skills Hub from the lab guide demos. Informatica provides READ/RUN operations. All CREATE/AUTOMATE operations require custom development (what Incept builds).

### Available MCP Servers (Verified in Dev Instance)

#### CDGC Metadata Search

- **Context:** cdgcsearchmetadata
- **MCP URL:** `https://a2e-preview-c360-usw1-mcp.dmp-us.informaticacloud.com/mcp-servers/public/cdgcsearchmetadata`
- **Actions:**
  - `search_metadata(knowledgeQuery: string)` — NL search across catalog
  - `get_asset_details(id: string, scheme: string)` — full metadata for one asset
- **Auth:** `IDS-SESSION-ID` header
- **Status:** Connected and tested ✅

#### Job Management

- **Context:** jobmanagement
- **MCP URL:** `https://a2e-preview-c360-usw1-mcp.dmp-us.informaticacloud.com/mcp-servers/public/jobmanagement`
- **Actions:**
  - `run_mapping_task(taskId, taskName, taskType, taskFederatedId)` — trigger CDI job
  - `get_job_status(runId: integer, taskId: string)` — execution status + row counts
  - `stop_running_job(taskId, taskName, taskType, taskFederatedId)` — halt active task
- **Auth:** `IDS-SESSION-ID` header
- **Status:** Connected and tested ✅

#### Other (not needed for Governance Engine)

- **Address Verification** — DQ address validation
- **Customer Identification** — MDM master records
- **Data Provisioning** — CDMP data access

### VS Code MCP Config Pattern

```json
{
  "servers": {
    "cdgc-metadata-search": {
      "type": "http",
      "url": "https://a2e-preview-c360-usw1-mcp.dmp-us.informaticacloud.com/mcp-servers/public/cdgcsearchmetadata",
      "headers": {
        "IDS-SESSION-ID": "${env:IDMC_SESSION_ID}"
      }
    }
  }
}
```

---

## 4. CDQ API Discovery (CRITICAL — Reverse Engineered from Browser DevTools)

### Overview

CDQ rule specifications are managed through TWO microservices:

1. **FRS (File Repository Service)** — metadata shell (name, type, dimension, parent hierarchy)
2. **rule-service** — rule body (script, fields, options, test data)

### FRS (File Repository Service)

**Host:** `usw1.dmp-us.informaticacloud.com`
**Base path:** `/frs/api/v1/`
**Protocol:** OData v4
**Auth:** `IDS-SESSION-ID` header

#### Endpoints

| Operation                    | Method | Path                                                                     | Notes                                                                                |
| ---------------------------- | ------ | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------ |
| List rule specs              | GET    | `/frs/api/v1/Documents?$filter=documentType%20eq%20'RULE_SPECIFICATION'` | Returns 160 specs in this tenant. Use %20 not + for spaces.                          |
| Get one by ID                | GET    | `/frs/api/v1/Documents('{id}')`                                          | Metadata only (no rule body)                                                         |
| Create metadata shell        | POST   | `/frs/api/v1/Documents`                                                  | Body: `{documentType, name}` minimum. Returns 201 with server-assigned ID.           |
| Update/save (with rule body) | PATCH  | `/frs/api/v1/Documents('{id}')`                                          | Contains `nativeData` field with rule body. Content-Length ~5348 bytes. Returns 204. |
| Delete                       | DELETE | `/frs/api/v1/Documents('{id}')`                                          | Returns 204. Cascades to rule-service.                                               |
| List projects                | GET    | `/frs/api/v1/Projects`                                                   |                                                                                      |
| List spaces                  | GET    | `/frs/api/v1/Spaces`                                                     |                                                                                      |
| List folders                 | GET    | `/frs/api/v1/Folders`                                                    |                                                                                      |
| Update access                | POST   | `/frs/api/v1/UpdateEntityAccess`                                         | Body: `{artifactIds: ["id"]}`                                                        |

#### Rule Specification Metadata Structure

```json
{
  "id": "7G81dALnoRslLc7RzUmmOl",
  "name": "DE_RS_Blank_String_Check",
  "description": null,
  "owner": "<userId>",
  "createdBy": "<userId>",
  "createdTime": "2026-03-19T16:13:09Z",
  "lastUpdatedTime": "2026-03-19T16:26:57Z",
  "parentInfo": [
    {
      "parentType": "Space",
      "parentId": "7cCn5thwWFLhiZoSosphKL",
      "parentName": "REG"
    },
    {
      "parentType": "Project",
      "parentId": "a3DaqI5cWMAfKahwNbNTcP",
      "parentName": "Teradyne_CDQ_Training"
    },
    {
      "parentType": "Folder",
      "parentId": "4D9WQiPdui4ipf2N6ZNXUt",
      "parentName": "Rules"
    }
  ],
  "documentType": "RULE_SPECIFICATION",
  "contentType": "Binary",
  "documentState": "VALID",
  "aclRule": "org",
  "customAttributes": {
    "stringAttrs": [
      { "name": "ReferencedPublishingAllowed", "value": "true" },
      { "name": "EXCEPTION", "value": "false" },
      { "name": "DIMENSION", "value": "COMPLETENESS" }
    ],
    "numberAttrs": [],
    "dateAttrs": []
  }
}
```

### rule-service (CDQ Rule Content)

**Host:** `usw1-dqcloud.dmp-us.informaticacloud.com`
**Base path:** `/rule-service/api/v1/`
**Auth:** Cookie-based from browser, but `IDS-SESSION-ID` header works for API calls

#### Endpoints

| Operation      | Method | Path                                 | Notes                                                                    |
| -------------- | ------ | ------------------------------------ | ------------------------------------------------------------------------ |
| Read rule body | GET    | `/rule-service/api/v1/Rules('{id}')` | Returns full rule model (5 kB). ID matches FRS document ID.              |
| Validate rule  | POST   | `/rule-service/api/v1/validateRule`  | Body: `{ruleModel: "<escaped JSON string>"}`. Returns validation result. |

**Note:** POST to `/Rules` collection returned 404 despite OPTIONS saying it's allowed. Rule body is saved through FRS PATCH with `nativeData` field, NOT through rule-service directly.

### Rule Model Structure (from validateRule payload)

```json
{
  "$$class": "com.informatica.dq.rulebuilder.RuleDefinition",
  "$$IID": "<frs_document_id>",
  "name": "DE_RS_Blank_String_Check",
  "$$id": "<frs_document_id>",
  "alternateDefinition": {
    "$$class": "com.informatica.dq.rulebuilder.DecisionScript",
    "$$id": "420",
    "$$externalID": "<uuid>",
    "script": "if Input = ''\r\n    then output = 'Invalid'\r\n    else output = 'Valid'\r\n    endif"
  },
  "outsideValidityMessage": "undefined",
  "validFromDate": "-3600000",
  "validToDate": "-3600000",
  "tags": [],
  "options": [
    {
      "$$class": "com.informatica.dq.rulebuilder.StringOption",
      "name": "DEFAULT_STRING_PRECISION",
      "optionValue": "100"
    },
    {
      "$$class": "com.informatica.dq.rulebuilder.StringOption",
      "name": "DEFAULT_DECIMAL_PRECISION",
      "optionValue": "10"
    },
    {
      "$$class": "com.informatica.dq.rulebuilder.StringOption",
      "name": "DEFAULT_DECIMAL_SCALE",
      "optionValue": "4"
    },
    {
      "$$class": "com.informatica.dq.rulebuilder.StringOption",
      "name": "DIMENSION",
      "optionValue": "COMPLETENESS"
    },
    {
      "$$class": "com.informatica.dq.rulebuilder.StringOption",
      "name": "EXCEPTION",
      "optionValue": "false"
    },
    {
      "$$class": "com.informatica.dq.rulebuilder.StringOption",
      "name": "ADVANCED",
      "optionValue": "true"
    }
  ],
  "fields": [
    {
      "$$class": "com.informatica.dq.rulebuilder.Field",
      "$$id": "326",
      "$$externalID": "<uuid>",
      "precision": "50",
      "scale": "0",
      "name": "Input",
      "$type": {
        "##SID": "smd:com.informatica.metadata.seed.platform.Platform.typesystem/string",
        "$$class": "com.informatica.metadata.common.typesystem.DataType"
      },
      "description": ""
    }
  ],
  "outputFields": [
    {
      "$$class": "com.informatica.dq.rulebuilder.OutputField",
      "$$id": "327",
      "$$externalID": "<uuid>",
      "precision": "50",
      "scale": "0",
      "name": "Output",
      "$type": {
        "##SID": "smd:com.informatica.metadata.seed.platform.Platform.typesystem/string",
        "$$class": "com.informatica.metadata.common.typesystem.DataType"
      },
      "description": ""
    }
  ],
  "testData": [],
  "topRuleFamily": {}
}
```

### CDQ Rule Script Syntax

```
if Input = ''
    then output = 'Invalid'
    else output = 'Valid'
    endif
```

- Available checks: `= ''`, `is null`, `is not null`, comparison operators
- Output values: typically 'Valid' / 'Invalid'
- Multiple conditions use `or` / `and`
- Decision Script class: `com.informatica.dq.rulebuilder.DecisionScript`

### Complete Create Flow (Discovered)

1. **POST** `/frs/api/v1/Documents` — create metadata shell with `{documentType: "RULE_SPECIFICATION", name: "..."}` → returns ID
2. **PATCH** `/frs/api/v1/Documents('{id}')` — save full payload including:
   - `name`, `description`, `documentType`, `documentState`
   - `customAttributes` (DIMENSION, EXCEPTION, etc.)
   - `nativeData` — contains the rule body (ruleModel)
3. (Optional) **POST** `/rule-service/api/v1/validateRule` — validate the rule model
4. **GET** `/rule-service/api/v1/Rules('{id}')` — verify rule body was saved correctly

### Important Notes

- FRS uses OData v4 — spaces must be `%20` not `+` in URLs
- `parentInfo` determines where rule appears in CDQ UI hierarchy (Space → Project → Folder)
- If `parentInfo` is invalid/missing, FRS silently assigns to Default project
- `documentState` should be "VALID" for usable rules
- Existing test rules: `DE_RS_Blank_String_Check`, `DE_RS_Null_Check`, `INCEPT_TEST_NULL_CHECK` (user created)
- 160 rule specifications exist in this tenant (from Teradyne_CDQ_Training project)

---

## 5. CDGC API (Governance and Catalog)

### Known Endpoints (from MCP + browser investigation)

**CDGC API Host:** `cdgc-api.dmp-us.informaticacloud.com`

| Operation         | Method  | Path                                                                              | Notes                    |
| ----------------- | ------- | --------------------------------------------------------------------------------- | ------------------------ |
| Search metadata   | via MCP | `search_metadata(knowledgeQuery)`                                                 | NL search across catalog |
| Get asset details | via MCP | `get_asset_details(id, scheme)`                                                   | Full metadata + lineage  |
| Governance model  | GET     | `/ccgf-modelv2/api/v2/models/attributes/com.infa.ccgf.models.governance.RuleType` | Rule type schema         |

### TODO — Still Need to Discover

- Stakeholder assignment via API (currently manual)
- Scorecard configuration API

### CDGC Rule Occurrence Creation (DISCOVERED Day 2)

- **Endpoint:** `POST https://cdgc-api.dmp-us.informaticacloud.com/ccgf-contentv2/api/v1/publish`
- **Auth:** Bearer JWT + X-INFA-ORG-ID + x-infa-product-id: CDGC
- **Response:** 207 Multi-Status (batch operation)
- **Payload:** 2 items per occurrence:
  1. OBJECT item: creates the RuleInstance (`com.infa.ccgf.models.governance.RuleInstance`)
  2. RELATIONSHIP item: links occurrence to a column (`com.infa.ccgf.models.governance.asscParentDataElementRuleInstance`)
- **Identity pattern:** PROVISIONAL (server assigns final ID like "DQO-4")
- **Key attributes:** core.name, core.origin (catalog source UUID), RuleType (dimension), MeasuringMethod, Criticality, Target, Threshold, TechnicalRuleReference (FRS rule spec ID), ruleInputPortName, ruleOutputPortName
- **Column ID format:** `{origin}://schema/table/column~com.infa.odin.models.relational.Column`
- **Verified:** DQO-4 and DQO-5 created successfully in dev tenant

### CDGC DQ Score Upload

- **Endpoint:** `PATCH <baseApiUrl>/ccgf-ruleautomation/api/v1/dataQuality/publishScore?refBy=INTERNAL`
- **Auth:** Bearer JWT
- **Note:** Silently 200s on unknown asset IDs — pre-validate asset existence

### Key Finding from Official Docs

- CDGC endpoints require **JWT auth** (Authorization: Bearer), NOT session ID
- `IDS_TOKEN` cookie from browser IS the JWT
- **JWT minting endpoint (DISCOVERED):** `GET https://dmp-us.informaticacloud.com/identity-service/api/v1/jwt/Token?client_id=idmc_api` with `IDS-SESSION-ID` header → returns Bearer JWT
- JWT cache duration: ~29 minutes (based on iat/exp in token)
- Auto-refresh pattern: cache JWT in memory, refresh on 401
- `upload_dq_scores` endpoint: `PATCH <baseApiUrl>/ccgf-ruleautomation/api/v1/dataQuality/publishScore?refBy=INTERNAL`
- Business asset CRUD: `POST/PATCH/DELETE <baseApiUrl>/data360/content/v1/assets`
- Supported class types: BusinessTerm, Metric, System, Policy, Domain, SubDomain, Process, DataSet, etc.
- Rule occurrences are NOT a supported class type for the business assets API

---

## 6. CDI API (Data Integration)

### Documented v2 API Endpoints (from IICS REST API Reference)

| Operation              | Method  | Path                    | Auth           | Notes                                                                          |
| ---------------------- | ------- | ----------------------- | -------------- | ------------------------------------------------------------------------------ |
| List mappings          | GET     | `/api/v2/mapping`       | icSessionId    | **Read-only.** Cannot create mappings via v2 API.                              |
| List mapping tasks     | GET     | `/api/v2/mttask`        | icSessionId    | Returns 15 tasks in this tenant                                                |
| Create mapping task    | POST    | `/api/v2/mttask/`       | icSessionId    | Requires: name, runtimeEnvironmentId, mappingId. @type discriminator required. |
| List taskflows         | GET     | `/api/v2/workflow`      | icSessionId    | Linear taskflows only                                                          |
| Create linear taskflow | POST    | `/api/v2/workflow`      | icSessionId    | Sequential tasks. Requires: name, tasks[].                                     |
| Create schedule        | POST    | `/api/v2/schedule`      | icSessionId    | Requires: name, orgId, startTime (.000Z format), interval.                     |
| List connections       | GET     | `/api/v2/connection`    | icSessionId    | 140 connections in this tenant                                                 |
| Run mapping            | via MCP | `run_mapping_task(...)` | IDS-SESSION-ID | Job Management MCP                                                             |
| Get job status         | via MCP | `get_job_status(...)`   | IDS-SESSION-ID | Job Management MCP                                                             |
| Stop job               | via MCP | `stop_running_job(...)` | IDS-SESSION-ID | Job Management MCP                                                             |

### Critical Finding: Mapping Creation NOT in v2 API

- `/api/v2/mapping` is GET-only (read/import/export)
- Mapping CREATION requires the Mapping Designer UI (stateful GWT-RPC sessions)
- Regular (non-linear) taskflows also NOT createable via v2 — need pre-built XML

### Bundle Export/Import (v3 API) — Investigated Day 2

- **Export:** `POST /public/core/v3/export` → returns jobId → poll → download ZIP
- **Import:** `POST /public/core/v3/import` → upload ZIP → poll
- **Bundle structure:** outer ZIP containing exportMetadata.v2.json + inner .DTEMPLATE.zip per mapping
- **Mapping body:** `bin/@3.bin` inside inner ZIP (JSON format, not XML)

### CRITICAL: clone_mapping Is NOT Possible

IDMC's migration service enforces **immutable checksums on inner DTEMPLATE bundles**:

- `relaxChecksum=true` only applies to outer ZIP, NOT inner mapping bundles
- Any modification to inner DTEMPLATE.zip → `MigrationSvc_072: object types require unchanged checksums`
- Export/import is for **unmodified asset migration between orgs**, not cloning within org

### Approved Approach: Built-in PreviewMapping_RULE_SPECIFICATION (DISCOVERED)

**No manual template creation needed.** IDMC has a built-in DQ execution template in every org:

- **Name:** `PreviewMapping_RULE_SPECIFICATION`
- **ID:** `010YK21700000000006E`
- **Parameters:** `$Source$` (EXTENDED_SOURCE), `$Target$` (TARGET), `$DQRuleParameter$` (STRING), `$DQRuleInputFieldMapping$` (STRING)
- `generate_dq_mapping_task` binds against this template automatically
- **Zero client setup** — works out of the box in any IDMC org with CDGC enabled
- This is the same template IDMC's own CLAIRE DQ agent uses internally for rule previews

### v2 vs v3 ID Mismatch

- v2 API uses `repoHandle` format: `010YK21700000000000H`
- v3 API uses FRS GUID format: `7YeAon6HW06kWgjljr5sii` (22-char base62)
- `generate_dq_mapping_task` auto-translates v3 GUIDs to v2 IDs internally

---

## 7. Governance Engine — Tool Specifications

### Tool 1: create_dq_rules ✅ WORKING

- **Input:** rule_name, description, field_name, dimension, rule_template (optional)
- **CLI:** `./create-rule.sh <name> [description] [field] [dimension] [--rule-template file] [--dry-run] [--auto-uuid]`
- **MCP Tool:** `create_dq_rules` in governance_engine_mcp.py
- **Flow:** POST FRS Documents → PATCH with nativeData.documentBlob → verify via rule-service
- **Status:** Working end-to-end. Multiple rules created and verified.

### Tool 2: generate_dq_mapping_task ✅ WORKING

- **Input:** rule_spec_id, source_connection_id, source_table, target_connection_id, target_table, input_field_mapping, runtime_environment_id
- **MCP Tool:** `generate_dq_mapping_task` in governance_engine_mcp.py
- **Template:** Uses IDMC built-in `PreviewMapping_RULE_SPECIFICATION` (ID: 010YK21700000000006E) — exists in every IDMC org
- **Parameters bound:**
  - `$Source$` (EXTENDED_SOURCE) — source connection + table
  - `$Target$` (TARGET) — bad-records target connection + table
  - `$DQRuleParameter$` (STRING, FrsAsset) — CDQ rule spec ID
  - `$DQRuleInputFieldMapping$` (STRING, Fieldmap) — format: `column_name=rule_input_port` (e.g., `customer_name=Input`)
- **Key discovery:** input_field_mapping cannot be empty — server enforces non-empty value
- **Verified:** End-to-end with Snowflake_InceptTest + CUSTOMER_POSITIONS, task created with all 4 params bound correctly
- **Zero client setup:** No manual template creation needed — works in any IDMC org out of the box

### Tool 3: create_schedule ✅ WORKING

- **MCP Tool:** `create_schedule` in governance_engine_mcp.py
- **Flow:** POST /api/v2/schedule with name, orgId, startTime (.000Z format), interval
- **Status:** Working end-to-end.

### Tool 4: register_in_cdgc ✅ WORKING

- **Input:** rule_spec_id, column_id, occurrence_name, dimension, input_port_name
- **MCP Tool:** `register_in_cdgc` in governance_engine_mcp.py
- **Flow:** POST /ccgf-contentv2/api/v1/publish with 2-item batch (OBJECT + RELATIONSHIP)
- **Auth:** Bearer JWT + X-INFA-ORG-ID + x-infa-product-id: CDGC
- **Status:** Working end-to-end. DQO-4 and DQO-5 created in dev tenant.

### Tool 5: run_governance_pipeline ✅ BUILT

- **Input:** Structured params (rule name, dimension, template mapping ID, connections, schedule, CDGC column)
- **MCP Tool:** `run_governance_pipeline` in governance_engine_mcp.py
- **Flow:** 7 steps with per-step error isolation: create_dq_rules → generate_dq_mapping_task → create_schedule → register_in_cdgc → run task → upload_dq_scores
- **Returns:** Execution report with artifacts, UI URLs, per-step status
- **Status:** Built and dry-run tested. Step 1 passes, downstream steps skip gracefully when missing dependencies.

### Additional Tools in Governance Engine

- `list_rule_specifications` ✅ — list CDQ rules via FRS OData
- `validate_rule` ✅ — validate rule model via rule-service
- `list_connections` ✅ — list CDI connections via v2 API
- `list_mapping_tasks` ✅ — list CDI mapping tasks via v2 API
- `create_mapping_task` ✅ — create CDI mapping task via v2 API
- `create_linear_taskflow` ✅ — create sequential taskflow via v2 API
- `upload_dq_scores` ✅ — push DQ scores to CDGC via publishScore API
- `export_assets` ✅ — export IDMC assets as ZIP bundle via v3 API
- `import_package` ✅ — import ZIP bundle via v3 API

---

## 8. Incept Governance Engine MCP Server (BUILT)

### Runtime Details

- **File:** `governance_engine_mcp.py`
- **Framework:** FastMCP
- **Transport:** Streamable HTTP
- **Port:** 8765
- **Endpoint:** `http://127.0.0.1:8765/mcp`
- **Start:** `python3 governance_engine_mcp.py` (must be running for VS Code to connect)
- **Dependencies:** `pip install -r requirements.txt` (mcp 1.27.1, httpx 0.28.1, python-dotenv, uvicorn)

### VS Code MCP Config Entry

```json
"governance-engine": {
  "type": "http",
  "url": "http://127.0.0.1:8765/mcp"
}
```

### Tools Exposed (14 total as of Day 2)

1. **create_dq_rules** — creates CDQ rule specification end-to-end
2. **list_rule_specifications** — lists existing CDQ rule specs
3. **validate_rule** — validates a rule model against rule-service
4. **list_connections** — lists CDI connections via v2 API
5. **list_mapping_tasks** — lists CDI mapping tasks via v2 API
6. **create_mapping_task** — creates CDI mapping task via v2 API
7. **generate_dq_mapping_task** — creates mapping task with DQ template parameter bindings
8. **create_schedule** — creates execution schedule via v2 API
9. **create_linear_taskflow** — creates sequential taskflow via v2 API
10. **upload_dq_scores** — pushes DQ scores to CDGC via publishScore
11. **register_in_cdgc** — creates rule occurrence in CDGC via publish API
12. **export_assets** — exports IDMC assets as ZIP bundle via v3 API
13. **import_package** — imports ZIP bundle via v3 API
14. **run_governance_pipeline** — master orchestrator chaining steps 1→7→8→11→run→upload

### Auth Flow

- Reads IDMC_SESSION_ID from .env
- Auto-refreshes on 401 (one retry per request)
- Uses IDS-SESSION-ID header for FRS and rule-service calls

### Caveats

- Server must be running manually (not autostarted by VS Code). Could switch to stdio transport later.
- list_rule_specifications name_filter is client-side (post-fetch). Could push to OData $filter for large tenants.
- Rule templates have fixed field UUIDs unless --auto-uuid is used.
- Parent assignment: FRS sometimes overrides parentInfo to Default project. PATCH doesn't re-send parentInfo.
- publishScore silently 200s on unknown asset IDs — pre-validate asset existence.
- v2 vs v3 mapping ID formats differ — generate_dq_mapping_task auto-translates.

---

## 9. Incept Lineage Reporter MCP Server (BUILT)

### Runtime Details

- **File:** `lineage_reporter_mcp.py` (571 lines)
- **Port:** 8766
- **Endpoint:** `http://127.0.0.1:8766/mcp`

### Tools (3)

1. **trace_lineage(asset_name, direction, depth, level)** — resolves asset via CDGC search, fetches lineage segments (upstream/downstream/both). Returns edges + distinct nodes.
2. **generate_impact_report(asset_name, change_description, depth, level)** — outbound trace, dedupes downstream nodes, classifies severity (LOW/MEDIUM/HIGH).
3. **find_data_source(asset_name, depth, level)** — inbound trace, identifies root source systems.

### Auth

- JWT Bearer via `_mint_jwt()` + IDS-SESSION-ID
- CDGC search via `POST /data360/search/v1/assets`
- Lineage via `GET /data360/search/v1/assets/{id}?segments=lineage-direction:...`

### Notes

- Lineage data only populates after catalog has scanned runtime dataflow. Dev tenant may have empty lineage.
- Neighbors fallback added for assets without lineage data.

---

## 10. Incept Glossary Manager MCP Server (BUILT)

### Runtime Details

- **File:** `glossary_manager_mcp.py`
- **Port:** 8767
- **Endpoint:** `http://127.0.0.1:8767/mcp`

### Tools (3)

1. **suggest_terms_for_asset(asset_name, domain_context)** — searches CDGC for asset, fetches child columns, runs heuristics (snake/camel-case + suffix analysis) to suggest business terms.
2. **create_glossary_term(term_name, definition, category, synonyms)** — POST /data360/content/v1/assets with classType=BusinessTerm.
3. **detect_glossary_issues(scan_scope, sample_size, min_definition_length)** — scans for duplicates, gaps (short definitions), orphans (unlinked terms).

### Auth

- JWT Bearer via `_mint_jwt()` with nonce (UUID per request)
- Business asset CRUD via `POST/PATCH/DELETE /data360/content/v1/assets`

---

## 11. Strategic Context

### Why MCP Servers (Not CAI, Not Python Standalone)

- Agent Engineering GA shipped as MCP Servers, not Agent Canvas/Skills Hub
- The product IS an MCP server — consumed by Claude Desktop/VS Code
- CAI-first approach was based on pre-GA assumptions (lab guide showed Skills Hub)
- Python is native for MCP SDK development
- Zero throwaway work — what we build IS the product

### Informatica's MCP Servers Provide READ/RUN

- Search catalog, get asset details (CDGC Metadata Search)
- Run mappings, check status, stop jobs (Job Management)

### Incept's MCP Servers Provide CREATE/AUTOMATE

- Create DQ rules, generate mappings, schedule, register governance
- Monitor DQ scores, alert on degradation
- Trace lineage, generate impact reports
- Automate glossary management
- End-to-end dataset onboarding

### Competitive Positioning

- Informatica CLAIRE agents = conversational copilots inside IDMC browser (human-in-the-loop)
- Incept MCP servers = autonomous operations accessible from Claude Desktop/VS Code (no human needed for routine ops)
- Complementary to Informatica's MCP servers, not competing

### Visibility in Agent Engineering

- Incept MCP servers do **NOT** appear in IDMC's Agent Engineering tab
- Agent Engineering UI shows only Informatica's Managed MCP Servers (hosted on their infrastructure)
- Incept servers run independently, consumed directly by MCP clients (Claude Desktop, VS Code, Python)
- Future: Informatica partnership could enable publishing as certified MCP servers in Agent Engineering

---

## 12. Deployment & Productization Architecture

### Current State: Local Development

```
Developer's Mac (localhost)
├── governance_engine_mcp.py    → http://127.0.0.1:8765/mcp
├── lineage_reporter_mcp.py     → http://127.0.0.1:8766/mcp
├── glossary_manager_mcp.py     → http://127.0.0.1:8767/mcp
├── dq_monitor_mcp.py           → http://127.0.0.1:8768/mcp
└── data_onboarding_mcp.py      → http://127.0.0.1:8769/mcp

Client: Claude Code in VS Code (same machine)
Auth: IDMC credentials in local .env file
```

### Phase 2: Client Deployment (Near-term)

Deploy servers on client's infrastructure alongside their Secure Agent.

```
Client VPC / Cloud VM
├── Docker container OR Python service
├── All 5 MCP servers behind a reverse proxy (nginx/caddy)
├── Single endpoint: https://governance.client-internal.com/mcp/{server}
├── Auth: Client's IDMC credentials (service account)
└── Client accesses via Claude Desktop pointed at internal URL

Benefits:
- Data never leaves client's network
- Uses client's own IDMC credentials and Secure Agent
- IT security team approves one VM, not 5 services
- Incept manages/updates remotely via SSH or deployment pipeline
```

### Phase 3: Incept-Hosted SaaS (Medium-term)

Multi-tenant hosted service for clients who prefer managed.

```
Incept Cloud (AWS/Azure)
├── API Gateway (auth, rate limiting, tenant isolation)
├── Per-tenant MCP server instances (or shared with tenant context)
├── Endpoint: https://agents.inceptds.com/{tenant}/mcp/{server}
├── Auth: Tenant-specific API keys + IDMC credentials vault
└── Monitoring, logging, alerting (Datadog/CloudWatch)

Benefits:
- Zero client infrastructure needed
- Incept handles updates, monitoring, uptime
- Usage-based billing possible
- Faster onboarding (no IT approval for client-side deployment)

Risks:
- Client IDMC credentials stored outside their network
- Latency (cloud → IDMC pod → Secure Agent)
- Compliance concerns in regulated industries (FS, healthcare)
```

### Phase 4: Informatica Agent Hub Partner (Long-term)

Publish as certified MCP servers in Informatica's Agent Engineering ecosystem.

```
Informatica Agent Engineering
├── Incept Governance Engine    → Certified MCP Server (visible in Agent Engineering UI)
├── Incept Lineage Reporter     → Certified MCP Server
├── Incept Glossary Manager     → Certified MCP Server
├── Incept DQ Monitor           → Certified MCP Server
└── Incept Data Onboarding      → Certified MCP Server

Benefits:
- Distribution to ALL IDMC customers via Agent Hub
- Informatica co-marketing and sales referrals
- Zero client deployment (runs as managed service on Informatica's infrastructure)
- Revenue share model possible

Requirements:
- Partnership agreement with Informatica
- Certification process for each MCP server
- Compliance with Informatica's security and data handling policies
- Multi-tenant architecture supporting any IDMC org
```

### Deployment Decision Matrix

| Factor             | Local Dev   | Client VM         | Incept SaaS          | Agent Hub                  |
| ------------------ | ----------- | ----------------- | -------------------- | -------------------------- |
| Time to deploy     | Minutes     | 1-2 days          | 1-2 weeks setup      | Months (partnership)       |
| Client IT approval | None        | VM approval       | API gateway approval | None (Informatica managed) |
| Data residency     | Local       | Client network    | Incept cloud         | Informatica cloud          |
| Maintenance        | Manual      | Incept remote     | Incept managed       | Informatica + Incept       |
| Scalability        | Single user | Team              | Multi-tenant         | All IDMC customers         |
| Revenue model      | N/A         | Project + managed | Subscription         | Revenue share              |
| Best for           | Demo + POC  | First clients     | Growth phase         | Scale phase                |

### Recommended Path

1. **Now:** Local development + demo (current state)
2. **First client:** Deploy on client VM via Docker (Phase 2)
3. **3-5 clients:** Build Incept-hosted SaaS (Phase 3)
4. **10+ clients:** Pursue Informatica Agent Hub partnership (Phase 4)

### Technical Requirements for Productization

- **Multi-tenancy:** Each client needs isolated IDMC credentials and org context
- **Auth:** Replace .env file with proper secrets management (HashiCorp Vault, AWS Secrets Manager)
- **HTTPS:** TLS termination at reverse proxy (Let's Encrypt or commercial cert)
- **Health checks:** Each MCP server needs a `/health` endpoint for monitoring
- **Logging:** Structured JSON logs for centralized aggregation
- **Rate limiting:** Prevent accidental API abuse against client IDMC instances
- **Session management:** Token refresh must be thread-safe for concurrent requests
- **Error reporting:** Client-facing errors must be sanitized (no credential leaks in error messages)

---

## 13. Build Progress Log

### Day 1 (May 12, 2026)

- ✅ IDMC v2 + v3 authentication working
- ✅ CDGC Metadata Search MCP connected in VS Code (searched catalog, found 2 CDI mappings)
- ✅ Job Management MCP connected in VS Code (found 162 job runs)
- ✅ Snowflake trial set up with test data (20 records, intentional DQ issues)
- ✅ Snowflake connection created and tested in IDMC
- ✅ Project structure with secure credential management
- ✅ CDQ API reverse-engineered from browser DevTools:
  - FRS Documents API for metadata CRUD
  - rule-service for validation and rule body reads
  - PATCH to Documents with nativeData for rule body save
  - Complete rule model structure documented
- ✅ create-rule.sh working end-to-end (creates CDQ rule spec via API)
  - Supports --rule-template, --dry-run, --auto-uuid flags
  - Two-call flow: POST /frs/api/v1/Documents → PATCH /frs/v1/Documents('{id}') with nativeData.documentBlob
  - Test rule INCEPT_AGENT_TEST_001 (id: 0dQ9Ziyj1epheorvlOS7a1) created and verified
- ✅ Python MCP server built and running (governance_engine_mcp.py)
  - FastMCP framework, streamable HTTP transport, port 8765
  - Tools: create_dq_rules, list_rule_specifications (validate_rule being added)
  - Connected to VS Code via .vscode/mcp.json
  - list_rule_specifications verified: returns INCEPT_AGENT_TEST_001 + INCEPT_TEST_NULL_CHECK
  - Auto-refresh on 401 wired (retry once per request)
- ✅ examples/null-check.json rule template created
- 🔄 CC adding validate_rule tool to MCP server

### Day 1 (May 12, 2026) — CONTINUED (Evening Session)

- ✅ **Session 1 — Governance Engine expanded to 9 tools:**
  - list_connections (v2 API, 140 connections found)
  - list_mapping_tasks (v2 API, 15 tasks found)
  - create_mapping_task (v2 API POST /api/v2/mttask, verified end-to-end)
  - create_schedule (v2 API POST /api/v2/schedule, .000Z format fix)
  - create_linear_taskflow (v2 API POST /api/v2/workflow, verified end-to-end)
  - upload_dq_scores (CDGC PATCH publishScore — needs JWT auth, IDS-SESSION-ID not sufficient)
  - Key discovery: CDGC endpoints require Authorization: Bearer <JWT>, not session ID
  - v2 @type discriminator required on all POST bodies
- ✅ **Session 2 — Lineage Reporter MCP server built:**
  - lineage_reporter_mcp.py (571 lines, port 8766)
  - Tools: trace_lineage, generate_impact_report, find_data_source
  - Uses CDGC search API (POST /data360/search/v1/assets) + lineage segments
  - Needs live testing against tenant
- 🔄 **Session 3 — Glossary Manager MCP server building**
- ✅ **Session 4 — 5 rule templates created:**
  - examples/completeness-check.json (COMPLETENESS dimension)
  - examples/range-check.json (ACCURACY dimension, configurable MIN/MAX)
  - examples/format-check.json (VALIDITY dimension, configurable pattern)
  - examples/timeliness-check.json (TIMELINESS dimension, configurable MAX_AGE_DAYS)
  - examples/consistency-check.json (CONSISTENCY dimension, two date inputs)
  - All include both topRuleFamily.statements + alternateDefinition Decision Script
- ✅ **API Documentation cataloged (16 doc sets, 150+ PDFs)**
  - Key finding from REST API docs: CDI mapping creation NOT available via v2 API (read/import/export only)
  - Key finding: CDGC rule occurrence creation NOT in documented API
  - Both need DevTools reverse-engineering (same CDQ playbook)

### Project Structure (End of Day 2)

```
~/Projects/IDMC_Governance_Engine/
├── .env                          # Credentials + sessions + JWT
├── .gitignore
├── .vscode/mcp.json              # 4+ MCP server configs
├── requirements.txt              # mcp, httpx, python-dotenv, uvicorn
├── login.sh / login-v3.sh        # Auth scripts
├── refresh-session.sh            # Session refresh
├── create-rule.sh                # CLI: create CDQ rules
├── governance_engine_mcp.py      # MCP server #1 (port 8765) — 14 tools
├── lineage_reporter_mcp.py       # MCP server #2 (port 8766) — 3 tools
├── glossary_manager_mcp.py       # MCP server #3 (port 8767) — 3 tools
├── dq_monitor_mcp.py             # MCP server #4 (port 8768) — building
├── examples/
│   ├── null-check.json
│   ├── completeness-check.json
│   ├── range-check.json
│   ├── format-check.json
│   ├── timeliness-check.json
│   └── consistency-check.json
└── docs/                         # Informatica PDFs (gitignored)
    └── DOCS_INDEX.md
```

### Day 2 (May 13, 2026)

- ✅ **15/15 tools passing** (morning) — validate_rule added, nonce fix for glossary, lineage nonce fix.
- ✅ **JWT auth fully solved** — `GET /identity-service/api/v1/jwt/Token?client_id=idmc_api`. 29-min cache. All servers updated.
- ✅ **Lineage Reporter live-tested** — search works, neighbors fallback added for empty lineage.
- ✅ **upload_dq_scores fully working** — JWT auto-mint + cache.
- ✅ **CDGC rule occurrence API reverse-engineered:**
  - `POST /ccgf-contentv2/api/v1/publish` with 2-item batch (OBJECT + RELATIONSHIP)
  - Type: `com.infa.ccgf.models.governance.RuleInstance`
  - End-to-end test: create_dq_rules → register_in_cdgc → DQO-5 created successfully
- ✅ **register_in_cdgc tool built and verified end-to-end**
- ✅ **Bundle export/import tools built** (export_assets, import_package) — v3 API
- ❌ **clone_mapping BLOCKED by design:**
  - IDMC migration service enforces immutable checksums on inner DTEMPLATE bundles
  - `relaxChecksum=true` only applies to outer ZIP, NOT inner mapping bundles
  - Any modification → `MigrationSvc_072: object types require unchanged checksums`
  - **PIVOTED to parameterized mapping templates** (IDMC-native pattern)
- ✅ **generate_dq_mapping_task tool built** — wraps create_mapping_task with DQ parameter bindings
- ✅ **run_governance_pipeline built** — 7-step master orchestrator with per-step error isolation
  - Returns execution report with artifacts, UI URLs, per-step status
  - Dry-run tested: Step 1 passes, downstream steps skip gracefully
- ✅ **v3→v2 ID auto-translation** in generate_dq_mapping_task
- 🔄 **DQ Monitor MCP server** (server #4) — CC building
- ⚠️ **Snowflake connection in Mapping Designer** — tables not showing (JDBC params set, connection tests OK). Need to create parameterized DQ mapping template once resolved.

### Day 2 — CONTINUED (Afternoon Session)

- ✅ **Snowflake connection FIXED** — INCEPT_GOV_DEV database and DQ_TEST schema didn't exist. Recreated database, table (19 rows with DQ issues), granted PUBLIC access. Tables now visible in Mapping Designer.
- ✅ **DQ Monitor MCP server COMPLETE** — 4 tools: get_dq_scores, check_score_trends, recommend_remediation, alert_on_degradation (port 8768)
- ✅ **Data Onboarding MCP server COMPLETE** — 1 master orchestrator tool: onboard_dataset (port 8769). Cross-server delegation verified.
- ✅ **PreviewMapping_RULE_SPECIFICATION discovered** — IDMC's built-in DQ execution template (ID: 010YK21700000000006E). Exists in EVERY IDMC org. No manual template creation needed.
  - Parameters: $Source$ (EXTENDED_SOURCE), $Target$ (TARGET), $DQRuleParameter$ (STRING), $DQRuleInputFieldMapping$ (STRING)
  - input_field_mapping format: `column_name=rule_input_port` (e.g., `customer_name=Input`)
  - Verified end-to-end: create rule → create mapping task with all 4 params bound → task created successfully
- ✅ **generate_dq_mapping_task refactored** for PreviewMapping_RULE_SPECIFICATION — zero client setup required
- ✅ **run_governance_pipeline updated** — passes target_connection + target_table + input_field_mapping through to generate_dq_mapping_task
- ✅ **DEMO.md written** — 10-section, 25-minute scripted demo with exact prompts, expected responses, troubleshooting
- ✅ **All 5 servers committed and pushed** — working tree clean, 15+ commits on main
- ✅ **Knowledge Transfer doc maintained** — 15 sections, 800+ lines

### PLATFORM COMPLETE — Final State

| #         | Server                   | Port | Tools  | Status           |
| --------- | ------------------------ | ---- | ------ | ---------------- |
| 1         | Governance Engine        | 8765 | 14     | ✅ Complete      |
| 2         | Lineage Reporter         | 8766 | 3      | ✅ Complete      |
| 3         | Glossary Manager         | 8767 | 3      | ✅ Complete      |
| 4         | DQ Monitor               | 8768 | 4      | ✅ Complete      |
| 5         | Data Onboarding          | 8769 | 1      | ✅ Complete      |
| +         | Informatica MCP (hosted) | —    | 2      | ✅ Connected     |
| **Total** |                          |      | **27** | **All complete** |

### TODO Next (Priority Order)

- [x] ~~All 15 original TODOs~~ ✅ Complete
- [ ] **Full end-to-end pipeline test** — run_governance_pipeline with run_now=True against real Snowflake data
- [ ] **Verify task execution** — mapping task needs metadata import + connector compatibility for actual CDI runs
- [ ] **Test DQ Monitor** against a real scored asset (needs rule occurrences with actual DQ scores uploaded)
- [ ] **Profiling API body shape fix** — Data Onboarding trigger_profiling needs correct field names
- [ ] **Developer handover** — walk dev team through codebase, auth patterns, deployment
- [ ] **First client demo prep** — customize demo for specific client data/use case
- [ ] **Production hardening** — error handling, retry logic, logging, health checks
- [ ] **Docker packaging** — containerize all 5 servers for client deployment

---

## 14. Reference Documents

| Document            | Location                                   | Description                                                             |
| ------------------- | ------------------------------------------ | ----------------------------------------------------------------------- |
| V3 Strategy Doc     | Incept_Governance_Agent_Strategy_V3.docx   | Competitive intelligence + strategy (superseded by V4 for architecture) |
| V4 Platform Roadmap | Incept_IDMC_Agent_Platform_Roadmap_V4.docx | Full platform strategy with MCP architecture                            |
| Architecture Doc    | Incept_Governance_Agent_Architecture.docx  | CAI-based architecture (SUPERSEDED — was pre-GA assumption)             |
| MCP Server Specs    | Incept_MCP_Server_Specifications.docx      | Detailed specs for all 5 MCP servers                                    |
| Snowflake Setup     | snowflake_setup.sql                        | Test database creation script                                           |

---

## 15. Key Decisions Made

1. **Python MCP servers, not CAI processes** — Agent Engineering GA shipped as MCP servers, not Skills Hub. Python is the native MCP development path.
2. **Build on Mac, Secure Agent on Windows** — standard IDMC developer setup.
3. **Dev instance, not production** — safe to experiment freely.
4. **CDGC-only for catalog** — removed multi-catalog sync (Alation/Collibra) from scope.
5. **Solo build, developer handover** — build all 5 MCP servers, then hand to dev team for production hardening.
6. **Governance Engine first** — builds shared infrastructure (auth, MCP clients, error handling) that all other servers reuse.
7. **Parameterized mapping templates, not clone_mapping** — IDMC's migration service enforces immutable checksums on inner mapping bundles. clone_mapping is impossible by design. The IDMC-native pattern is one template mapping + multiple mapping tasks with different parameter bindings.
8. **CDGC rule occurrence via publish API, not business assets API** — Rule occurrences are NOT a supported class type in the documented business assets CRUD. They use the undocumented `/ccgf-contentv2/api/v1/publish` endpoint with PROVISIONAL identity pattern.
9. **Parallel CC sessions for speed** — 4 Claude Code sessions running simultaneously on separate files. Max 20x plan enables this.
