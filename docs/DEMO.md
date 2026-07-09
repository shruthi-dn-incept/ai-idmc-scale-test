# IDMC Governance Platform — Demo Walkthrough

A 20–30 minute live walkthrough of the platform for developers. Every step
below shows the exact Claude Code prompt to type, the MCP tool it invokes,
and the expected outcome.

Prereqs: VS Code with Claude Code, this repo cloned, `.env` populated with
real `IDMC_USER` / `IDMC_PASS` / `IDMC_LOGIN_HOST`, and `pip install -r
requirements.txt` done. Mac/Linux paths assumed.

---

## 0. Setup (~3 min) — start the five local servers

```bash
cd ~/Projects/IDMC_Governance_Engine

# Mint a fresh v2 session and persist into .env
eval "$(./refresh-session.sh)"

# Start all five MCP servers in the background
GOVERNANCE_MCP_PORT=8765    python3 governance_engine_mcp.py  > /tmp/gov.log 2>&1 &
LINEAGE_MCP_PORT=8766       python3 lineage_reporter_mcp.py    > /tmp/lin.log 2>&1 &
GLOSSARY_MCP_PORT=8767      python3 glossary_manager_mcp.py    > /tmp/glo.log 2>&1 &
DQ_MONITOR_MCP_PORT=8768    python3 dq_monitor_mcp.py          > /tmp/dqm.log 2>&1 &
DATA_ONBOARDING_MCP_PORT=8769 python3 data_onboarding_mcp.py   > /tmp/onb.log 2>&1 &

# Confirm all five are bound
for p in 8765 8766 8767 8768 8769; do
  pid=$(lsof -ti:$p 2>/dev/null)
  [[ -n "$pid" ]] && echo "port $p: alive PID $pid" || echo "port $p: NOT RUNNING"
done
```

Open VS Code at this folder, then **reload window** so it re-reads
`.vscode/mcp.json`. The bottom-right status bar should list **7 servers**
(2 Informatica hosted + 5 ours).

> **First-time pitfall**: if a server "dies on startup," tail its log
> (`tail /tmp/<x>.log`) — usually a missing dependency (`pip install -r
> requirements.txt`) or stale `.env`.

---

## 1. Open the platform (~2 min)

Type in Claude Code (in this workspace):

> **"List every MCP tool you have access to, grouped by server."**

Claude will enumerate the tools. Expected: **24 tools** across
governance-engine (13), lineage-reporter (3), glossary-manager (3),
dq-monitor (4), data-onboarding (1), plus the two Informatica hosted
servers (`cdgc-metadata-search`, `job-management`).

---

## 2. Read-only tour — inventory the tenant (~3 min)

These prompts hit the v2/v3/CDGC APIs we wrapped. No side effects.

> **"How many connections do we have? Group them by type."**

Hits `list_connections` (governance-engine). Expected: ~140 total,
dominated by `TOOLKIT_CCI` (custom connectors) and `Oracle`.

> **"List the 5 most recently updated mapping tasks."**

Hits `list_mapping_tasks`. Sorts client-side. Useful sanity check that
the v2 path is alive.

> **"How many CDQ rule specifications are in the tenant? Show 10 with their
> dimensions."**

Hits `list_rule_specifications`. Expected: ~160 rule specs, mostly
`DE_RS_*` (training rules) plus our `INCEPT_TEST_*` / `INCEPT_AGENT_TEST_*`.

> **"Search the catalog for customer-related assets and tell me the top 5."**

Hits the Informatica-hosted `cdgc-metadata-search.search_metadata`. Shows
the difference between **read tools we built** vs **read tools Informatica
ships** — both are MCP, both work the same way for the user.

---

## 3. Write something small — create a DQ rule (~2 min)

> **"Create a CDQ rule called `DEMO_NOT_NULL_CUSTOMER_NAME` that checks
> the customer_name field is not null. COMPLETENESS dimension."**

Hits `create_dq_rules`. Returns a rule id, the FRS document state
(`VALID`), and a clickable IDMC UI URL. Click the URL — the rule is
**real**, visible in `https://usw1-dqcloud.dmp-us.informaticacloud.com/dq-product/...`.

> **"Validate that rule against the rule-service before we use it."**

Hits `validate_rule`. Returns `{valid: true, output_count: 0,
raw.error: null}` — meaning rule-service accepts the model.

---

## 4. CDGC graph queries (~3 min)

> **"Trace the downstream lineage of `m_FactoryDimension` to depth 3.
> How many distinct assets are affected?"**

Hits `trace_lineage` (lineage-reporter). For real assets with mappings
this returns a graph; for assets without lineage it returns `edge_count:
0` with a clear note.

> **"Generate an impact report: if we change the schema of `customer_name`
> in the Snowflake test table, what breaks?"**

Hits `generate_impact_report`. Returns severity (low/medium/high) by
distinct downstream count, plus a flat list of affected assets.

> **"Find the root data source for `m_Cust_Warehouse_Load`."**

Hits `find_data_source`. Walks lineage **upstream** to "source systems"
(no further inbound edges).

---

## 5. Glossary checks (~2 min)

> **"Are there any duplicate or orphaned business terms in the glossary?
> Sample 50."**

Hits `detect_glossary_issues`. Returns counts of duplicates / orphans /
definition-too-short for the sampled terms.

> **"Suggest business glossary terms relevant to `INCEPT_TEST_NULL_CHECK`
> in the customer-data domain."**

Hits `suggest_terms_for_asset`. For most assets this returns matches from
the existing glossary; for cleanroom test assets the result is often
empty — that's expected.

---

## 6. DQ Monitor — observability layer (~3 min)

Note: the `INCEPT_*` test assets are rule specs themselves, so they have
no scores. Pick a real Snowflake column for meaningful output (e.g.
`CUSTOMER_POSITIONS.customer_name` once you've bound a rule to it).

> **"Get the current DQ scorecard for `<real_column_asset>` in
> COMPLETENESS dimension only."**

Hits `get_dq_scores`. Returns score records, composite score, and the
set of dimensions present on that asset.

> **"Check for DQ score degradation on `<real_column_asset>` over the last
> 30 days. Threshold 10 points."**

Hits `check_score_trends`. Buckets each rule into degrading / improving
/ stable / no_history.

> **"Analyze any failing DQ rules on `<real_column_asset>` and tell me
> where to focus remediation."**

Hits `recommend_remediation`. Returns structured failure info + per-
dimension **suggestion seeds** (e.g. *"investigate upstream nulls"*).
The LLM expands these into human-readable advice.

> **"Register an alert for `<real_column_asset>`: notify me at
> demo@example.com when the score drops by 5 points."**

Hits `alert_on_degradation`. Persists to a **local** `.dq_monitor_alerts.json`
file. **Heads-up to the audience**: IDMC's API doesn't expose alert
registration — alerts are UI-only — so we persist locally for a sibling
scheduler to act on. This is the only "platform doesn't quite reach the
service" gap we surface in the demo.

---

## 7. CDI / scheduling layer (~3 min)

> **"Create a daily schedule called `DEMO_DAILY` that starts tomorrow at
> 8am UTC."**

Hits `create_schedule`. Returns a schedule id. **Show the gotcha**:
v2 schedule timestamps need millisecond precision (`.000Z`) — without
millis you get `UI_10601`. The tool's docstring documents this.

> **"List the 10 most recent CDI mapping tasks. Then create a linear
> taskflow named `DEMO_TASKFLOW` that runs the first one."**

Hits `list_mapping_tasks` + `create_linear_taskflow`. Show the two-step
chaining — the LLM picks an id from step 1, passes it to step 2.

---

## 8. The orchestrator — end-to-end pipeline (~5 min)

This is the centerpiece — one prompt that fans out to ~5 tools.

> **"Run the full governance pipeline: create a CDQ rule called
> `DEMO_PIPELINE_RUN` (null-check on `customer_name`, COMPLETENESS),
> then create a mapping task using template `0DMDulmZrQvbnss7chDbOc`
> (M_DQ_Generic). Bind it explicitly: source connection
> `010YK20B000000000044` table `CUSTOMER_POSITIONS`; target connection
> `010YK20B000000000044` table `CUSTOMER_POSITIONS_BAD_RECORDS`;
> input_field_mapping `customer_name=Input`; runtime
> `010YK2250000000000DY`. Schedule it daily at 8am UTC starting
> tomorrow. Don't register in CDGC yet."**

Hits `run_governance_pipeline` on governance-engine, which fans out to
`create_dq_rules` → `generate_dq_mapping_task` → `create_schedule` →
`register_in_cdgc` (SKIPPED) → optional run / score upload. Each step
records SUCCESS / SKIPPED / FAILED + elapsed ms; a single failure does
not abort the chain.

The five new bindings on `generate_dq_mapping_task` map to M_DQ_Generic's
mtTaskParameter list as follows:

| Parameter             | Type              | Bound to                                         |
|-----------------------|-------------------|--------------------------------------------------|
| `$Source$`            | EXTENDED_SOURCE   | `source_connection_id` + `source_table`          |
| `$Target$`            | TARGET            | `target_connection_id` + `target_table`          |
| `$Input_Field_Map$`   | STRING (Fieldmap) | `input_field_mapping` (`<src_col>=<rule_port>`)  |

The Rule Specification transformation inside M_DQ_Generic is baked in
at design time, so `rule_spec_id` is audit metadata only (it flows into
the task name and description, not a parameter binding).

**Talking points while it runs:**
- "We never wrote curl. The LLM picks tool names and bindings from the
  docstrings — including the M_DQ_Generic parameter table."
- "The five-param binding is the whole reason we keep M_DQ_Generic
  parameterized — one template, N (rule × source) tasks."
- "Each step records why it skipped — `register_in_cdgc` is SKIPPED
  because we passed *don't register*, not because of an error."
- "End-to-end against real IDMC: ~6 seconds."

---

## 9. Dataset onboarding — orchestrate across servers (~3 min)

> **"Onboard `CUSTOMER_POSITIONS` from Snowflake_InceptTest into our
> Customer domain. Apply GDPR DQ checks. Don't publish to Data
> Marketplace yet."**

Hits `onboard_dataset` (data-onboarding). This tool **calls other MCP
servers over HTTP** — show the audience the cross-server call helper.
Expected step table:

```
[SUCCESS] cdgc_search             (resolves Snowflake column in CDGC)
[SKIPPED] trigger_profiling       (body shape needs the docs-gap fix)
[SUCCESS] read_classifications    (returns any auto-classified PII tags)
[SUCCESS] create_dq_rules         (creates COMPLETENESS+VALIDITY for GDPR)
[SUCCESS] register_in_cdgc        (only if catalog_origin is set; see KT)
[SUCCESS] suggest_glossary_terms  (delegates to glossary-manager)
[SKIPPED] provision_to_dmp        (auto_provision=False)
```

---

## 10. Cleanup (~2 min)

> **"Find every rule specification with a name starting `DEMO_` or
> `__AGENT_TEST_` and delete them all."**

The LLM will list them via `list_rule_specifications` then issue per-id
DELETEs to the FRS endpoint. **Read the deletes back** before
approving the batch — make sure no production rules slip in.

For mtTasks / schedules / workflows created by step 7/8, give Claude the
ids it returned and ask:

> **"Delete the mapping task, schedule, and linear taskflow we created
> earlier. Here are the ids: …"**

Then stop the servers:

```bash
for p in 8765 8766 8767 8768 8769; do
  pid=$(lsof -ti:$p 2>/dev/null)
  [[ -n "$pid" ]] && kill "$pid"
done
```

---

## 11. Profile and recommend DQ rules (~5 min)

This scenario walks the audience through the **profile → recommend →
create** flow. It pairs `recommend_dq_rules` (a pure-local analyzer on
governance-engine) with the rule-creation tool we already demoed in
section 3. The point: no human decides which rules to write — the LLM
picks them from profiling stats, then asks for batch approval.

**Step 1 — profile the source.** Profiling itself isn't a first-class
governance-engine tool (IDMC's Profiling REST surface is undocumented
in the bundles we have, see KT §6), so we feed `recommend_dq_rules` a
stats blob instead. In the demo, paste the JSON inline; in production
this comes from a Snowflake query or a CDGC profile run.

> **"Here are profiling stats for `CUSTOMER_POSITIONS` from
> Snowflake_InceptTest. Run `recommend_dq_rules` over them and show me
> the top recommendations grouped by severity:"**
>
> ```json
> {
>   "total_rows": 20,
>   "columns": {
>     "customer_name":   {"data_type": "string",  "null_count": 2, "null_pct": 0.10, "blank_count": 1, "distinct_count": 17},
>     "customer_id":     {"data_type": "string",  "null_count": 1, "null_pct": 0.05, "distinct_count": 19},
>     "exposure":        {"data_type": "decimal", "null_count": 0, "min": -50000, "max": 1500000, "expected_min": 0, "expected_max": 1000000000, "out_of_range_count": 1},
>     "currency_code":   {"data_type": "string",  "null_count": 0, "distinct_count": 5, "pattern_distribution": {"AAA": 17, "AAAA": 2, "Aa": 1}},
>     "trade_dt":        {"data_type": "date",    "null_count": 0, "future_count": 1, "stale_count": 2},
>     "position_id":     {"data_type": "string",  "null_count": 0, "distinct_count": 19}
>   },
>   "consistency_pairs": [
>     {"start": "trade_dt", "end": "settlement_dt", "violation_count": 2}
>   ]
> }
> ```

Hits `recommend_dq_rules` (no API calls — pure local analysis against
`examples/profiling-rule-mapping.json`). Expected: ~7 recommendations
covering null/blank, range, format, timeliness, uniqueness, and
consistency, each tagged HIGH/MEDIUM/LOW by affected-row ratio.

**Step 2 — inspect the rationale.** Talking point: every recommendation
carries a `rationale` string explaining *why* (`"customer_name has 10.0%
nulls (2 / 20 rows)"`). That's what the LLM needs to summarize for the
data-steward review without us writing prompt scaffolding.

> **"Pick the top 3 HIGH-severity recommendations. For each, tell me
> what to do, and show me the suggested rule names and templates."**

**Step 3 — create the rules.** Have Claude batch-create the top picks.
Show the diff between the recommendation's `rule_template` (a path under
`examples/`) and the `create_dq_rules` `rule_template` argument — same
field name, drops in directly.

> **"Create the top 2 HIGH-severity recommendations as real CDQ rules.
> Use the suggested_rule_name and rule_template from each
> recommendation. Don't bind them to anything yet."**

Hits `create_dq_rules` twice (once per pick), using the template paths
from `examples/`. Each returns a `ui_url` you can click to confirm the
rule landed in CDQ.

**Talking points while it runs:**
- "Thresholds and template mappings live in
  `examples/profiling-rule-mapping.json` — tune without touching code."
- "Severity is a ratio band, not a magic number. `affected_rows / total`
  ≥ 10% → HIGH, ≥ 1% → MEDIUM, else LOW. Audit-friendly."
- "The same flow plugs into `run_governance_pipeline` for create + bind
  + schedule + register. Demo section 8 is what scaling this looks like."

---

## Architecture cheat sheet for the audience

```
┌──────────────────────────────────────────────────────────────────────┐
│                          Claude Code (VS Code)                       │
│                       7 MCP servers in mcp.json                      │
└────┬──────────────────────┬──────────────────────────┬───────────────┘
     │ Informatica-hosted   │ Local (this repo)        │
     ▼                      ▼                          ▼
  cdgc-metadata-search    governance-engine (8765)  data-onboarding (8769)
  job-management          lineage-reporter  (8766)        ▲
                          glossary-manager  (8767) ───────┤
                          dq-monitor        (8768) ───────┤
                                                          │ HTTP (cross-server)
                                                          └─ orchestrates
```

| Server | Tools | Surfaces it owns |
|---|---|---|
| governance-engine | 13 | FRS (CDQ rules), rule-service, v2 CDI, CDGC publishScore + rule occurrences, v3 export/import |
| lineage-reporter | 3 | CDGC `/data360/search/v1/assets` (lineage segments) |
| glossary-manager | 3 | CDGC `/data360/content/v1/assets` (business terms) |
| dq-monitor | 4 | CDGC dataQuality segments + local alerts JSON |
| data-onboarding | 1 master | Orchestrates the others via MCP-over-HTTP |

## Auth layers in one slide

| Surface | Header | Source |
|---|---|---|
| FRS, rule-service, MCP hosts | `IDS-SESSION-ID` | v2 session in `.env` (auto-mint via `refresh-session.sh`) |
| v2 CDI (`/api/v2/...`) | `icSessionId` | same v2 session |
| v3 platform | `INFA-SESSION-ID` | v3 login (auto-mint on demand) |
| CDGC (Bearer) | `Authorization: Bearer <JWT>` | `_mint_jwt()` exchanges v2 session at `/identity-service/api/v1/jwt/Token?client_id=idmc_api&nonce=…` (29-min cache) |

## Three gotchas to demo confidently

1. **`@type` discriminator** — every v2 POST body needs `@type` (`mtTask`,
   `schedule`, `workflow`). Without it the server returns generic 400
   HTML instead of a parseable error. All our tools set it.
2. **JWT nonce** — IDMC's `/jwt/Token` rejects requests without a `nonce`
   query param. Each server passes a fresh UUID per request.
3. **`exportPackage.chksum` is required** even with `relaxChecksum=true`
   — and the inner DTEMPLATE.zip is immutable (server-blocked by
   `MigrationSvc_072`). That's why we don't have a `clone_mapping`
   tool; the pivot is parameterized template mappings + `create_mapping_task`
   per (rule × source) combo.

## When something fails during the demo

| Symptom | Likely cause | Quick fix |
|---|---|---|
| Tool errors with 401 | Session expired | `eval "$(./refresh-session.sh)"`; tools auto-refresh on next call too |
| CDGC tool errors with `TokenParseError` | JWT cache stale | Tool force-refreshes on 401; just retry once |
| Snowflake browser shows only `SNOWFLAKE_*` DBs | Role missing `USAGE` on the database | Grant USAGE on database + schema + warehouse to the connector's role |
| Tool not visible in VS Code | Server not running, or VS Code hasn't reloaded mcp.json | `lsof -ti:<port>` and reload window |
| `MigrationSvc_072` on import | You modified an immutable bundle | Don't try to clone via export/import — use parameterized templates |
