# Onboarding / KT — AI-IDMC Data Governance

> **Audience:** a new engineer joining this project.
> **Goal:** by the end of this doc you understand *what* this system does, *how* the
> pieces fit, *how to run it*, and *where the landmines are* — enough to make your
> first change with confidence.
> **Time to first run:** ~30–45 min (mostly waiting on `.env` + credentials).

---

## 1. What this project is (in one paragraph)

We use **Claude (an LLM)** to automate the manual work of governing a data catalog in
**Informatica IDMC / CDGC** (Cloud Data Governance & Catalog). A human normally has to
hand-build a business glossary, tag every column with a business term, define data-quality
rules, and wire up DQ monitoring — for thousands of tables. This project does all of that
programmatically: the LLM designs the taxonomy, and code pushes it into CDGC over the API.
It is **proven at scale** — a full run governs **~4,000 tables** end-to-end on Azure.

There are **two ways to drive it**:

1. **Web UI** — a guided wizard for demos and single-source onboarding (human in the loop).
2. **Scale pipeline** — a headless, 10-phase batch job that governs the entire catalog
   (runs as one Azure Container Apps job).

Both sit on top of the same set of **MCP servers**.

---

## 2. The big-picture architecture

```
                 ┌─────────────────────────────────────────────┐
                 │  Two entrypoints (pick one)                   │
                 │                                               │
   Web UI  ──────┤  src/idmc_governance/ui/app.py  (FastAPI)     │
   (wizard)      │      → http://localhost:9080                  │
                 │                                               │
   Scale   ──────┤  scale/orchestrator.py  (10 phases)           │
   pipeline      │      → one Azure ACA job                      │
                 └───────────────────┬───────────────────────────┘
                                     │  both call the MCP servers
                                     ▼
        ┌────────────────────────── MCP servers (src/idmc_governance/servers/) ─────────────┐
        │  ai_governance      :8770   ← the "brain": LLM taxonomy + orchestration           │
        │  governance_engine  :8765   ← CDGC/CDQ/CDI writes: rules, publish, curate, import  │
        │  lineage_reporter   :8766   glossary_manager :8767   dq_monitor :8768              │
        │  data_onboarding    :8769                                                          │
        └───────────────────────────────────┬────────────────────────────────────────────┘
                                             │  HTTPS (JWT / session auth)
                                             ▼
        ┌──────────────────────────── Informatica IDMC / CDGC ──────────────────────────────┐
        │  CDGC (catalog + governance)   CDQ (rules)   CDI (mappings)   MCC (metadata scans)  │
        └───────────────────────────────────┬────────────────────────────────────────────┘
                                             │  Secure Agent (on a VM)
                                             ▼
                                    Snowflake (the governed data)
```

**Mental model:** `ai_governance` is the *decision-maker* (asks Claude what the glossary
should be). `governance_engine` is the *hands* (makes the CDGC/CDQ/CDI API calls that
actually write assets). Everything else is a specialized read/monitor server.

The two servers that matter 90% of the time are **`ai_governance` (:8770)** and
**`governance_engine` (:8765)**.

---

## 3. The end-to-end flow (what actually happens)

This is the core of the KT. The **scale pipeline** is the canonical end-to-end; the UI
wizard does the same steps for one source with a human clicking "next".

Phases, from [scale/orchestrator.py](src/idmc_governance/scale/orchestrator.py):

| # | Phase | Module | What it does | LLM? |
|---|-------|--------|--------------|------|
| 1 | `extract` | [extract_columns.py](src/idmc_governance/scale/extract_columns.py) | Read every table's columns from CDGC + Snowflake types → `.scan_cache/*.json`. Read-only, parallel across tables. | no |
| 2 | `taxonomy` | [taxonomy.py](src/idmc_governance/scale/taxonomy.py) | Feed Claude the **complete distinct vocabulary** (~108 unique column names) → a domain/subdomain/business-term tree → `taxonomy.json`. | **yes** |
| 3 | `colterm` | taxonomy.py | Map each unique column name → one business term → `colterm_map.json`. | (from taxonomy) |
| 4 | `domain` | ai_governance | Create the Domain + SubDomains + Business Terms in CDGC; resolve `term_ids.json`. | no |
| 5 | `system_ds` | ai_governance | Create 1 System + N Datasets (one per schema). | no |
| 6 | `gen_dqro` | [generate_dqro.py](src/idmc_governance/scale/generate_dqro.py) | Generate the DQ-rule-occurrence bulk file `CDGC_DQRO_FULL.xlsx`. | no |
| 7 | `import_dqro` | [bulk_import.py](src/idmc_governance/scale/bulk_import.py) | 3-step CDGC bulk import (validate → submit → poll). | no |
| 8 | `curate` | [curate.py](src/idmc_governance/scale/curate.py) | Link every column **instance** (~137k) to its business term via the publish API. Deterministic — applies the phase-3 map, no per-column LLM. | no |
| 9 | `scan` | ai_governance | Trigger an MCC (Metadata Command Center) data-quality scan on all sources. | no |
| 10 | `stats` | [stats.py](src/idmc_governance/scale/stats.py) | Snowflake credit usage + write `stats.json` + a results doc. | no |

**The key scaling trick (read this twice):** the catalog reuses ~108 unique column names
across ~137k column instances. So the LLM maps each *unique name* to a term **exactly once**
(phase 2/3), and phase 8 deterministically applies that map to every instance. No
per-column LLM calls → the whole catalog finishes in minutes and stays cheap.

Run it:

```bash
python -m idmc_governance.scale.orchestrator              # full run
python -m idmc_governance.scale.orchestrator --from 4     # resume from phase 4
python -m idmc_governance.scale.orchestrator --skip scan  # skip a phase
python -m idmc_governance.scale.orchestrator --discover   # auto-find schemas
```

Per-phase timings and outcomes land in `state/stats.json`. Each phase is isolated: one
failing phase records its error and the run continues.

> **⚠️ There is NO cleanup phase, by design.** The old `--clean` deleted DQ rule instances
> *globally* across the whole catalog — a footgun that could wipe unrelated catalogs. To
> tear down, generate a **delete-operation** bulk file and import it (deletes exactly the
> listed rows, nothing else):
> ```bash
> python -m idmc_governance.scale.generate_dqro --schemas <SCHEMA...> --operation Delete --out delete.xlsx
> python -m idmc_governance.scale.bulk_import delete.xlsx
> ```

---

## 4. Getting it running locally (do this first)

```bash
# 1. Credentials — copy the template and fill it in (see §5 for what each var is)
cp .env.example .env

# 2. Install the package editable (deps come from pyproject.toml)
pip install -e .

# 3a. Run the UI (starts both MCP servers + the web UI)
docker compose up               # → http://localhost:9080
#    …or without Docker:
bash scripts/start-ui.sh

# 3b. …or run the scale pipeline headless
python -m idmc_governance.scale.orchestrator
```

**Prereqs:** Python 3.11+, a populated `.env`, and (for the pipeline) a Snowflake key
(base64 in `.env`; raw keys live in `secrets/`, which is gitignored).

**Good first smoke tests** (read-only, safe, no writes to CDGC):

```bash
python -m idmc_governance.scale.extract_columns --limit 3   # pull 3 tables into .scan_cache
python -m idmc_governance.scale.rule_map                    # list rule specs → rule_map.json
```

---

## 5. Environment & auth (the part that breaks first)

Config lives in **`.env`** (gitignored). Start from `.env.example`. The important groups:

- **IDMC session:** `IDMC_USER`, `IDMC_PASS`, `IDMC_LOGIN_HOST` → used to mint sessions.
- **CDGC:** `CDGC_API_BASE`, `IDMC_ORG_ID` (tenant org UUID).
- **Snowflake:** account + key (base64) — the governed data + credit accounting.
- **Anthropic:** `ANTHROPIC_API_KEY` — the LLM behind the taxonomy phase.

**Auth model (this trips everyone up):**

- Login mints a **session ID** (`IDS-SESSION-ID`). This works for FRS and rule-service.
- **CDGC content APIs require a JWT Bearer token**, *not* the session ID. The code mints
  a JWT from the session (`GET /identity-service/api/v1/jwt/Token?client_id=idmc_api`),
  caches it ~29 min, and auto-refreshes on 401.
- CDGC writes also need `X-INFA-ORG-ID` and `x-infa-product-id: CDGC` headers.

If you see 401s: the session/JWT expired — the servers retry once automatically, but a
stale `.env` session won't self-heal. Re-run the login/refresh scripts in `scripts/`.

---

## 6. Deployment (Azure)

The scale pipeline and UI run on **Azure Container Apps**. Deploy scripts in `deploy/`:

| Script | Deploys |
|---|---|
| `deploy/azure_deploy_pipeline_job.ps1` | Scale pipeline as an ACA **job** (runs to completion) |
| `deploy/azure_deploy_import_job.ps1` | Bulk-import as an ACA job |
| `deploy/azure_deploy_ui.ps1` | The web UI as an ACA app |
| `deploy/setup_agent_vm.ps1` | The Secure Agent VM |

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) and
[docs/azure-reference-architecture.md](docs/azure-reference-architecture.md) for the full
picture. Measured scale results are in [docs/SCALE_RESULTS.md](docs/SCALE_RESULTS.md).

---

## 7. Known landmines (learned the hard way)

These are real issues that have bitten us — check them before spending hours debugging:

1. **Secure Agent `libidn` blocker** — profiling/DQ jobs fail because `pmdtm` needs
   `libidn.so.11`, which is missing on the Ubuntu 22.04 agent VM. Install the shim before
   expecting scans to run.
2. **Enrollment source misconfiguration** — the governed source must point at the real
   Snowflake DB (`GOVERNANCE_SCALE_TEST_C`), *not* the `SNOWFLAKE` system DB (which has
   views and 0 tables). Verify the source before governing.
3. **The `--clean` footgun** — see §3. Never do a global purge; use delete-operation bulk
   files scoped to specific schemas.
4. **Curation propagation cap (429s)** — linking columns → Data Set via the normal path
   hits a propagation rate cap. The bulk-import route (set Business Dataset +
   `Operation=Update`) bypasses it.
5. **Asset delete quirk** — delete by `core.externalId` + `X-INFA-PRODUCT-ID` header. The
   orchestrator's old delete-by-`core.identity` path 404s.
6. **Profiling is overhead-bound, not compute-bound** — throughput knobs are
   `MaximumConcurrentJobs` (IDMC-side), Snowflake warehouse `max_clusters`, and agent VM
   memory. Throwing a bigger warehouse alone won't help.

*(These are captured in more detail in the project's running notes — ask the team for the
memory/notes if you need the exact commands.)*

---

## 8. Repository map (where to look)

```
src/idmc_governance/
├── servers/     6 MCP servers — ai_governance + governance_engine are the active pair
├── ui/          FastAPI web UI (app.py) + static React frontend
├── scale/       the 10-phase pipeline: orchestrator.py + one module per phase
├── common/      shared helpers (paths.py, snowflake.py)
└── setup/       one-time data load / catalog-source setup
scripts/         entrypoints (start-ui, start_scale_pipeline, start_bulk_import) + auth
docker/          Dockerfile (scale/servers) + Dockerfile.ui
deploy/          Azure deploy scripts
docs/            architecture, deployment, demo scripts, scale results, this project's docs
examples/        CDQ rule templates      templates/  CDGC/marketplace import templates
tests/           pytest      secrets/ state/  gitignored (keys / generated artifacts)
```

**Further reading, in order:**
1. [README.md](README.md) — quickstart + entrypoints (start here after this doc).
2. [docs/DEMO_V2.md](docs/DEMO_V2.md) — a scripted walkthrough of the UI flow.
3. [docs/idmc-governance-agent-knowledge-transfer.md](docs/idmc-governance-agent-knowledge-transfer.md)
   — deep API-level KT (the reverse-engineered CDQ/CDGC endpoints). **Note:** this is from
   the earlier 5-server local-dev build (May 2026) — the API details are still gold, but
   the "current state" / project-structure sections are historical.
4. [docs/DOCS_INDEX.md](docs/DOCS_INDEX.md) — which Informatica PDF covers which API.

---

## 9. First-week checklist for the new joiner

- [ ] Get `.env` populated (ask for credentials — IDMC service account, Snowflake key, Anthropic key).
- [ ] `pip install -e .` and run the two read-only smoke tests in §4.
- [ ] Bring up the UI (`docker compose up`) and click through the wizard once against a small source.
- [ ] Read `scale/orchestrator.py` top-to-bottom — it's the map of the whole system.
- [ ] Run the pipeline against **one or two schemas** (`--discover` or a small `PIPELINE_SCHEMAS`) and read `state/stats.json`.
- [ ] Skim §7 landmines so you recognize them when they hit.
- [ ] Do a scoped teardown with a delete bulk file so you understand the (only) safe cleanup path.

---

*Questions this doc doesn't answer? The API-level detail is in the deep KT
([docs/idmc-governance-agent-knowledge-transfer.md](docs/idmc-governance-agent-knowledge-transfer.md));
everything else, ask the team.*
