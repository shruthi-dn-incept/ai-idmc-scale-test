# Rule Templates

CDQ rule specification templates consumed by `governance_engine_mcp.create_dq_rules`
(via `--rule-template <path>` on `create-rule.sh`, or via the `rule_template`
parameter on the MCP tool). Each file is a complete `ruleModel` payload —
`create_dq_rules` injects `$$IID`, `$$id`, `name`, and `description` at
creation time and posts the result through FRS.

`recommend_dq_rules` reads stats from a profiling run and maps each finding to
exactly one of these templates. The mapping (and its thresholds) lives in
[`profiling-rule-mapping.json`](profiling-rule-mapping.json) so it can be
retuned without touching code.

## Template catalogue

| File | Dimension | Inputs | Configurable options | Profiling trigger |
|---|---|---|---|---|
| [`null-check.json`](null-check.json) | COMPLETENESS | `Input` (string) | — | `null_count > 0` or `null_pct > min_null_pct` |
| [`completeness-check.json`](completeness-check.json) | COMPLETENESS | `Input` (string) | — | `blank_count ≥ min_blank_count` on string-typed columns |
| [`range-check.json`](range-check.json) | ACCURACY | `Input` (decimal) | `MIN_VALUE`, `MAX_VALUE` | `out_of_range_count ≥ 1`, or observed min/max outside `expected_min`/`expected_max` |
| [`format-check.json`](format-check.json) | VALIDITY | `Input` (string) | `PATTERN` (SQL `LIKE` pattern) | `pattern_distribution` has ≥`min_pattern_count` patterns and no pattern accounts for `max_dominant_pattern_pct` or more of rows |
| [`timeliness-check.json`](timeliness-check.json) | TIMELINESS | `Input` (dateTime) | `MAX_AGE_DAYS` | `future_count + stale_count ≥ 1` on date-typed columns |
| [`consistency-check.json`](consistency-check.json) | CONSISTENCY | `Date_A`, `Date_B` (dateTime) | — | `consistency_pairs[*].violation_count ≥ min_pair_violations` |
| [`uniqueness-check.json`](uniqueness-check.json) | UNIQUENESS | `Input` (string), `DuplicateCount` (decimal) | `MAX_OCCURRENCES` | ID-shaped column name AND `total_rows − distinct_count ≥ min_duplicate_count` |

All templates emit `output` ∈ {`Valid`, `Invalid`} via `PrimaryRuleSet`. The
default rule fires `Invalid` whenever the input is null.

## How `recommend_dq_rules` picks a template

For each column in a profile, the tool walks the heuristics in this order and
collects every match (one column can trigger multiple templates):

1. **null** → `null-check.json` if any rows are null.
2. **blank** → `completeness-check.json` if a string column has empty/whitespace values.
3. **range** → `range-check.json` if a numeric column has out-of-range values
   or observed bounds fall outside the profile's expected bounds.
4. **format** → `format-check.json` if a string column's `pattern_distribution`
   is fragmented (multiple patterns, no clear dominant one).
5. **timeliness** → `timeliness-check.json` if a date column has future or
   stale values.
6. **uniqueness** → `uniqueness-check.json` if the column name looks
   ID-shaped (`*_id`, `*_key`, `uuid`, `guid`, …) and duplicates exist.

Plus, separately:

7. **consistency** → `consistency-check.json` per entry in `consistency_pairs`
   where `end < start` violations exist.

Severity (HIGH/MEDIUM/LOW) comes from `affected_rows / total_rows` against the
bands in `profiling-rule-mapping.json → severity`. Defaults: HIGH ≥ 10%,
MEDIUM ≥ 1%, otherwise LOW.

## Field naming when the template is created

`create_dq_rules` keeps the field name from the template verbatim. If your
real column isn't called `Input` (or `Date_A`/`Date_B`/`DuplicateCount`), the
CDI mapping that calls the rule will need to alias the source column on the
way in. The MCP server doesn't rename rule-spec fields per call — the rule
spec is reusable across many bindings.

## Notes per template

### `null-check.json`
Minimal completeness check. Empty `alternateDefinition.script` — the rule is
evaluated entirely from the `PrimaryRuleSet` statement tree: VALID when
`Input ≠ null`.

### `completeness-check.json`
Stricter completeness check: rejects both null AND empty strings (`Input = ''`).
Use this on free-text fields where the upstream system may store `''` instead
of `null`.

### `range-check.json`
Half-open interval `(MIN_VALUE, MAX_VALUE]` — VALID when `Input > MIN_VALUE`
AND `Input <= MAX_VALUE`. Default bounds `(0, 1,000,000,000]`. Tune via the
`MIN_VALUE` / `MAX_VALUE` options at creation time or edit the template before
upload.

### `format-check.json`
Uses SQL `LIKE` syntax (NOT regex). The default pattern `[A-Z][A-Z][A-Z]`
matches 3 uppercase letters (e.g. ISO-3 country codes). Override the `PATTERN`
option for your column's expected shape, e.g. `[A-Z][A-Z]` for state codes or
`[0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][0-9][0-9]` for SSNs.

### `timeliness-check.json`
VALID when `Input ≥ sysdate − MAX_AGE_DAYS`. Default `MAX_AGE_DAYS=30`.
Does NOT cap future dates — combine with a separate validity rule if you need
to reject `Input > sysdate`.

### `consistency-check.json`
Two date inputs `Date_A` and `Date_B`. VALID when both are non-null AND
`Date_A < Date_B`. Pattern: bind `trade_date` to `Date_A` and `settlement_date`
to `Date_B` in the CDI mapping.

### `uniqueness-check.json`
Per-row evaluation can't count occurrences across rows on its own. This
template expects a precomputed `DuplicateCount` input — wire an upstream
Aggregator transformation in the CDI mapping that groups by `Input` and
emits its count. VALID when `Input ≠ null` AND `DuplicateCount ≤ MAX_OCCURRENCES`
(default 1). For composite keys, concatenate the parts before passing them
into `Input`.

## Authoring a new template

1. Start from the closest existing template.
2. Update `options.DIMENSION` to one of `COMPLETENESS | ACCURACY | VALIDITY |
   TIMELINESS | CONSISTENCY | UNIQUENESS | CONFORMITY | INTEGRITY`.
3. Update `alternateDefinition.script` (human-readable form) AND the
   `topRuleFamily.statements` tree (executable form) — both must agree.
4. Use stable placeholder UUIDs in `$$externalID` (`00000000-0000-4000-…`).
   `create_dq_rules` regenerates them per creation when `auto_uuid=True` (the
   default) so multiple rules made from one template don't collide.
5. Leave `$$IID`, `$$id`, `name`, `description` as `WILL_BE_INJECTED` — the
   server overwrites them.
6. Add a row to the catalogue table above and a heuristic in
   `profiling-rule-mapping.json` if you want `recommend_dq_rules` to suggest
   the new template automatically.
