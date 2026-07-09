"""
Load 1,000 synthetic healthcare rows into every table in GOVERNANCE_SCALE_TEST.
Uses Snowflake's server-side GENERATOR — no data transfer over the wire.
Runs with thread-level concurrency for speed.
"""
from __future__ import annotations
import os, time, logging
from collections import defaultdict
from dotenv import load_dotenv
import snowflake.connector

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB        = "GOVERNANCE_SCALE_TEST"
WAREHOUSE = "INCEPT_WH"
ROLE      = os.getenv("SNOWFLAKE_ROLE", "ACCOUNTADMIN")
SCHEMAS   = ["GOVTEST_CLAIMS", "GOVTEST_CLINICAL", "GOVTEST_MEMBER", "GOVTEST_PROVIDER"]
ROWS      = 1000

# ── Column-value expressions ───────────────────────────────────────────────────

# Text columns: pattern → Snowflake SQL expression
TEXT_EXPRS: list[tuple[str, str]] = [
    ("MEMBER_ID",          "'MBR' || LPAD(ABS(UNIFORM(1,9999999,RANDOM()))::TEXT, 9, '0')"),
    ("PROVIDER_NPI",       "LPAD(ABS(UNIFORM(1000000000,1999999999,RANDOM()))::TEXT, 10, '0')"),
    ("PROVIDER_ID",        "'PRV' || LPAD(ABS(UNIFORM(1,999999,RANDOM()))::TEXT, 6, '0')"),
    ("PROVIDER_NAME",      "CASE MOD(ABS(UNIFORM(0,5,RANDOM())),5) WHEN 0 THEN 'CVS Health' WHEN 1 THEN 'Aetna Medical Group' WHEN 2 THEN 'MinuteClinic' WHEN 3 THEN 'Oak Street Health' ELSE 'Caremark Rx' END"),
    ("CLAIM_ID",           "'CLM' || LPAD(ABS(UNIFORM(1,999999999,RANDOM()))::TEXT, 9, '0')"),
    ("ENCOUNTER_ID",       "'ENC' || LPAD(ABS(UNIFORM(1,999999999,RANDOM()))::TEXT, 9, '0')"),
    ("AUTHORIZATION_ID",   "'AUTH' || LPAD(ABS(UNIFORM(1,99999999,RANDOM()))::TEXT, 8, '0')"),
    ("REFERRAL_ID",        "'REF' || LPAD(ABS(UNIFORM(1,99999999,RANDOM()))::TEXT, 8, '0')"),
    ("SUBSCRIBER_ID",      "'SUB' || LPAD(ABS(UNIFORM(1,9999999,RANDOM()))::TEXT, 7, '0')"),
    ("PATIENT_ACCOUNT_NBR","'PAT' || LPAD(ABS(UNIFORM(1,9999999,RANDOM()))::TEXT, 7, '0')"),
    ("BATCH_ID",           "'BAT' || LPAD(ABS(UNIFORM(1,99999,RANDOM()))::TEXT, 5, '0')"),
    ("CONTRACT_ID",        "'CTR' || LPAD(ABS(UNIFORM(1,99999,RANDOM()))::TEXT, 5, '0')"),
    ("GROUP_ID",           "'GRP' || LPAD(ABS(UNIFORM(1,9999,RANDOM()))::TEXT, 4, '0')"),
    ("PLAN_ID",            "'PLN' || LPAD(ABS(UNIFORM(1,9999,RANDOM()))::TEXT, 4, '0')"),
    ("FACILITY_ID",        "'FAC' || LPAD(ABS(UNIFORM(1,99999,RANDOM()))::TEXT, 5, '0')"),
    ("SOURCE_SYSTEM_ID",   "'SYS' || LPAD(ABS(UNIFORM(1,9999,RANDOM()))::TEXT, 4, '0')"),
    ("DX_CODE",            "CASE MOD(ABS(UNIFORM(0,9,RANDOM())),9) WHEN 0 THEN 'E11.9' WHEN 1 THEN 'I10' WHEN 2 THEN 'Z00.00' WHEN 3 THEN 'J18.9' WHEN 4 THEN 'N18.3' WHEN 5 THEN 'F32.9' WHEN 6 THEN 'M54.5' WHEN 7 THEN 'Z23' ELSE 'K21.0' END"),
    ("DX_CODE_TYPE",       "CASE MOD(ABS(UNIFORM(0,2,RANDOM())),2) WHEN 0 THEN 'ICD10' ELSE 'ICD9' END"),
    ("ICD_VERSION",        "CASE MOD(ABS(UNIFORM(0,2,RANDOM())),2) WHEN 0 THEN '10' ELSE '9' END"),
    ("PROCEDURE_CODE",     "CASE MOD(ABS(UNIFORM(0,5,RANDOM())),5) WHEN 0 THEN '99213' WHEN 1 THEN '93000' WHEN 2 THEN '80053' WHEN 3 THEN '71046' ELSE '99232' END"),
    ("PROCEDURE_CODE_TYPE","CASE MOD(ABS(UNIFORM(0,2,RANDOM())),2) WHEN 0 THEN 'CPT' ELSE 'HCPCS' END"),
    ("HCPCS_CODE",         "CASE MOD(ABS(UNIFORM(0,4,RANDOM())),4) WHEN 0 THEN 'G0438' WHEN 1 THEN 'G0439' WHEN 2 THEN 'Q9950' ELSE 'S9110' END"),
    ("NDC_CODE",           "LPAD(ABS(UNIFORM(10000000000,99999999999,RANDOM()))::TEXT, 11, '0')"),
    ("DRUG_CLASS",         "CASE MOD(ABS(UNIFORM(0,5,RANDOM())),5) WHEN 0 THEN 'ANTIDIAB' WHEN 1 THEN 'ANTIHY' WHEN 2 THEN 'STATIN' WHEN 3 THEN 'ANTICOAG' ELSE 'ANALGES' END"),
    ("FORMULARY_TIER",     "CASE MOD(ABS(UNIFORM(0,4,RANDOM())),4) WHEN 0 THEN 'T1' WHEN 1 THEN 'T2' WHEN 2 THEN 'T3' ELSE 'T4' END"),
    ("CLAIM_STATUS",       "CASE MOD(ABS(UNIFORM(0,4,RANDOM())),4) WHEN 0 THEN 'PAID' WHEN 1 THEN 'DENIED' WHEN 2 THEN 'PENDING' ELSE 'ADJUSTED' END"),
    ("CLAIM_TYPE",         "CASE MOD(ABS(UNIFORM(0,3,RANDOM())),3) WHEN 0 THEN 'MEDICAL' WHEN 1 THEN 'PHARMACY' ELSE 'DENTAL' END"),
    ("AUTH_STATUS",        "CASE MOD(ABS(UNIFORM(0,3,RANDOM())),3) WHEN 0 THEN 'APPROVED' WHEN 1 THEN 'DENIED' ELSE 'PENDING' END"),
    ("DENIAL_REASON_CODE", "CASE MOD(ABS(UNIFORM(0,4,RANDOM())),4) WHEN 0 THEN 'NOTCV' WHEN 1 THEN 'PRAUT' WHEN 2 THEN 'DUPL' ELSE 'BUND' END"),
    ("ADJ_REASON_CODE",    "CASE MOD(ABS(UNIFORM(0,4,RANDOM())),4) WHEN 0 THEN 'CO45' WHEN 1 THEN 'CO97' WHEN 2 THEN 'PR1' ELSE 'OA23' END"),
    ("ELIGIBILITY_STATUS", "CASE MOD(ABS(UNIFORM(0,3,RANDOM())),3) WHEN 0 THEN 'ACTIVE' WHEN 1 THEN 'TERMINATED' ELSE 'PENDING' END"),
    ("RECORD_STATUS",      "CASE MOD(ABS(UNIFORM(0,3,RANDOM())),3) WHEN 0 THEN 'ACTIVE' WHEN 1 THEN 'INACTIVE' ELSE 'ARCHIVED' END"),
    ("NETWORK_TIER_CODE",  "CASE MOD(ABS(UNIFORM(0,3,RANDOM())),3) WHEN 0 THEN 'INN' WHEN 1 THEN 'OON' ELSE 'PFD' END"),
    ("BENEFIT_CODE",       "'BEN' || LPAD(ABS(UNIFORM(1,9999,RANDOM()))::TEXT, 4, '0')"),
    ("SPECIALTY_CODE",     "CASE MOD(ABS(UNIFORM(0,5,RANDOM())),5) WHEN 0 THEN 'CARDIOL' WHEN 1 THEN 'ONCOL' WHEN 2 THEN 'INT_MED' WHEN 3 THEN 'PEDS' ELSE 'FAM_MED' END"),
    ("POS_CODE",           "LPAD(ABS(UNIFORM(11,99,RANDOM()))::TEXT, 2, '0')"),
    ("REVENUE_CODE",       "LPAD(ABS(UNIFORM(100,999,RANDOM()))::TEXT, 4, '0')"),
    ("TYPE_OF_BILL",       "LPAD(ABS(UNIFORM(110,999,RANDOM()))::TEXT, 3, '0')"),
    ("RISK_SCORE_MODEL",   "CASE MOD(ABS(UNIFORM(0,3,RANDOM())),3) WHEN 0 THEN 'HCC' WHEN 1 THEN 'RXHCC' ELSE 'CRG' END"),
    ("SOURCE_SYSTEM",      "CASE MOD(ABS(UNIFORM(0,3,RANDOM())),3) WHEN 0 THEN 'FACETS' WHEN 1 THEN 'QNXT' ELSE 'AMISYS' END"),
    ("GENDER_CODE",        "CASE MOD(ABS(UNIFORM(0,2,RANDOM())),2) WHEN 0 THEN 'M' ELSE 'F' END"),
    ("RACE_CODE",          "CASE MOD(ABS(UNIFORM(0,5,RANDOM())),5) WHEN 0 THEN 'WHITE' WHEN 1 THEN 'BLACK' WHEN 2 THEN 'HISPA' WHEN 3 THEN 'ASIAN' ELSE 'OTHER' END"),
    ("ETHNICITY_CODE",     "CASE MOD(ABS(UNIFORM(0,2,RANDOM())),2) WHEN 0 THEN 'HISPA' ELSE 'NHISP' END"),
    ("LANGUAGE_CODE",      "CASE MOD(ABS(UNIFORM(0,3,RANDOM())),3) WHEN 0 THEN 'EN' WHEN 1 THEN 'ES' ELSE 'ZH' END"),
    ("COUNTRY_CODE",       "CASE MOD(ABS(UNIFORM(0,2,RANDOM())),2) WHEN 0 THEN 'US' ELSE 'CA' END"),
    ("STATE_CODE",         "CASE MOD(ABS(UNIFORM(0,5,RANDOM())),5) WHEN 0 THEN 'CA' WHEN 1 THEN 'TX' WHEN 2 THEN 'NY' WHEN 3 THEN 'FL' ELSE 'IL' END"),
    ("ZIP_CODE",           "LPAD(ABS(UNIFORM(10000,99999,RANDOM()))::TEXT, 5, '0')"),
    ("CITY",               "CASE MOD(ABS(UNIFORM(0,5,RANDOM())),5) WHEN 0 THEN 'Los Angeles' WHEN 1 THEN 'Houston' WHEN 2 THEN 'New York' WHEN 3 THEN 'Miami' ELSE 'Chicago' END"),
    ("ADDRESS_LINE1",      "ABS(UNIFORM(100,9999,RANDOM()))::TEXT || ' ' || CASE MOD(ABS(UNIFORM(0,4,RANDOM())),4) WHEN 0 THEN 'Oak St' WHEN 1 THEN 'Maple Ave' WHEN 2 THEN 'Main Blvd' ELSE 'Park Dr' END"),
    ("PHONE_NUMBER",       "'(' || ABS(UNIFORM(200,999,RANDOM()))::TEXT || ') ' || ABS(UNIFORM(200,999,RANDOM()))::TEXT || '-' || LPAD(ABS(UNIFORM(0,9999,RANDOM()))::TEXT,4,'0')"),
    ("EMAIL_ADDRESS",      "'user' || ABS(UNIFORM(1,99999,RANDOM()))::TEXT || '@healthmail.com'"),
    ("FIRST_NAME",         "CASE MOD(ABS(UNIFORM(0,5,RANDOM())),5) WHEN 0 THEN 'James' WHEN 1 THEN 'Maria' WHEN 2 THEN 'David' WHEN 3 THEN 'Linda' ELSE 'Robert' END"),
    ("LAST_NAME",          "CASE MOD(ABS(UNIFORM(0,5,RANDOM())),5) WHEN 0 THEN 'Smith' WHEN 1 THEN 'Johnson' WHEN 2 THEN 'Garcia' WHEN 3 THEN 'Lee' ELSE 'Williams' END"),
    ("FULL_NAME",          "CASE MOD(ABS(UNIFORM(0,3,RANDOM())),3) WHEN 0 THEN 'James Smith' WHEN 1 THEN 'Maria Garcia' ELSE 'David Lee' END"),
    ("FACILITY_NAME",      "CASE MOD(ABS(UNIFORM(0,4,RANDOM())),4) WHEN 0 THEN 'CVS MinuteClinic' WHEN 1 THEN 'Aetna Health Center' WHEN 2 THEN 'Oak Street Clinic' ELSE 'Caremark Specialty' END"),
    ("GROUP_NAME",         "'GROUP_' || LPAD(ABS(UNIFORM(1,999,RANDOM()))::TEXT, 3, '0')"),
    ("PLAN_NAME",          "CASE MOD(ABS(UNIFORM(0,3,RANDOM())),3) WHEN 0 THEN 'Aetna Gold PPO' WHEN 1 THEN 'Aetna Silver HMO' ELSE 'CVS Advantage' END"),
    ("NOTES",              "CASE MOD(ABS(UNIFORM(0,3,RANDOM())),3) WHEN 0 THEN 'Routine claim' WHEN 1 THEN 'Requires review' ELSE 'Auto-adjudicated' END"),
]

NUMBER_EXPRS: dict[str, str] = {
    "RECORD_ID":         "ROW_NUMBER() OVER (ORDER BY SEQ8())",
    "AGE_AT_SERVICE":    "ABS(UNIFORM(18,89,RANDOM()))",
    "DAYS_SUPPLY":       "ABS(UNIFORM(1,90,RANDOM()))",
    "LENGTH_OF_STAY":    "ABS(UNIFORM(1,30,RANDOM()))",
    "QUANTITY":          "ABS(UNIFORM(1,200,RANDOM()))",
    "UNITS_ADMINISTERED":"ABS(UNIFORM(1,100,RANDOM()))",
    "CLAIM_LINE_NBR":    "ABS(UNIFORM(1,20,RANDOM()))",
    "SEQUENCE_NBR":      "ABS(UNIFORM(1,9999,RANDOM()))",
    "VERSION_NBR":       "ABS(UNIFORM(1,10,RANDOM()))",
    "STARS_RATING":      "ABS(UNIFORM(1,5,RANDOM()))",
    "RISK_SCORE":        "ROUND(UNIFORM(0.5,4.5,RANDOM()),4)",
    "QUALITY_SCORE":     "ROUND(UNIFORM(0,100,RANDOM()),2)",
    "BILLED_AMOUNT":     "ROUND(UNIFORM(50,50000,RANDOM()),2)",
    "PAID_AMOUNT":       "ROUND(UNIFORM(10,40000,RANDOM()),2)",
    "ALLOWED_AMOUNT":    "ROUND(UNIFORM(10,45000,RANDOM()),2)",
    "COPAY_AMOUNT":      "ROUND(UNIFORM(5,150,RANDOM()),2)",
    "COINSURANCE_AMOUNT":"ROUND(UNIFORM(0,5000,RANDOM()),2)",
    "DEDUCTIBLE_AMOUNT": "ROUND(UNIFORM(0,8000,RANDOM()),2)",
    "PREMIUM_AMOUNT":    "ROUND(UNIFORM(100,2000,RANDOM()),2)",
    "OOP_AMOUNT":        "ROUND(UNIFORM(0,7500,RANDOM()),2)",
    "CAPITATION_AMOUNT": "ROUND(UNIFORM(10,500,RANDOM()),2)",
    "INCENTIVE_AMOUNT":  "ROUND(UNIFORM(0,1000,RANDOM()),2)",
    "PENALTY_AMOUNT":    "ROUND(UNIFORM(0,500,RANDOM()),2)",
    "WITHHOLD_AMOUNT":   "ROUND(UNIFORM(0,200,RANDOM()),2)",
}

DATE_EXPRS: dict[str, str] = {
    "SERVICE_DATE":      "DATEADD('day', -ABS(UNIFORM(0,730,RANDOM())), CURRENT_DATE())",
    "ADMISSION_DATE":    "DATEADD('day', -ABS(UNIFORM(0,730,RANDOM())), CURRENT_DATE())",
    "DISCHARGE_DATE":    "DATEADD('day', -ABS(UNIFORM(0,720,RANDOM())), CURRENT_DATE())",
    "PAID_DATE":         "DATEADD('day', -ABS(UNIFORM(0,700,RANDOM())), CURRENT_DATE())",
    "PROCESSED_DATE":    "DATEADD('day', -ABS(UNIFORM(0,700,RANDOM())), CURRENT_DATE())",
    "EXTRACT_DATE":      "DATEADD('day', -ABS(UNIFORM(0,365,RANDOM())), CURRENT_DATE())",
    "LOAD_DATE":         "DATEADD('day', -ABS(UNIFORM(0,180,RANDOM())), CURRENT_DATE())",
    "SNAPSHOT_DATE":     "DATEADD('day', -ABS(UNIFORM(0,365,RANDOM())), CURRENT_DATE())",
    "REPORTING_PERIOD":  "DATEADD('month', -ABS(UNIFORM(0,24,RANDOM())), CURRENT_DATE())",
    "ENROLLMENT_DATE":   "DATEADD('day', -ABS(UNIFORM(0,3650,RANDOM())), CURRENT_DATE())",
    "EFFECTIVE_DATE":    "DATEADD('day', -ABS(UNIFORM(0,3650,RANDOM())), CURRENT_DATE())",
    "TERMINATION_DATE":  "DATEADD('day', ABS(UNIFORM(0,3650,RANDOM())), CURRENT_DATE())",
    "BIRTH_DATE":        "DATEADD('day', -ABS(UNIFORM(6570,29200,RANDOM())), CURRENT_DATE())",
}

TIMESTAMP_EXPR = "DATEADD('second', -ABS(UNIFORM(0,31536000,RANDOM())), CURRENT_TIMESTAMP())"
BOOLEAN_EXPR   = "ABS(UNIFORM(0,1,RANDOM()))::BOOLEAN"

TEXT_FALLBACK  = "'TXT' || LPAD(ABS(UNIFORM(1,99999,RANDOM()))::TEXT, 5, '0')"


def col_expr(col_name: str, data_type: str, max_len: int | None = None) -> str:
    col_upper = col_name.upper()
    if data_type in ("NUMBER", "FLOAT", "FIXED"):
        return NUMBER_EXPRS.get(col_upper, "ROUND(UNIFORM(0,99999,RANDOM()),2)")
    if data_type in ("DATE",):
        return DATE_EXPRS.get(col_upper, "DATEADD('day', -ABS(UNIFORM(0,730,RANDOM())), CURRENT_DATE())")
    if "TIMESTAMP" in data_type:
        return TIMESTAMP_EXPR
    if data_type in ("BOOLEAN",):
        return BOOLEAN_EXPR
    # TEXT / VARCHAR — find the best expression then clamp to column length
    expr = TEXT_FALLBACK
    for pattern, e in TEXT_EXPRS:
        if pattern in col_upper:
            expr = e
            break
    if max_len and max_len > 0:
        expr = f"LEFT({expr}, {max_len})"
    return expr


def build_insert(schema: str, table: str, columns: list[tuple[str, str, int | None]]) -> str:
    col_list  = ", ".join(f'"{c}"' for c, _, _ in columns)
    expr_list = ", ".join(col_expr(c, d, m) for c, d, m in columns)
    return (
        f"INSERT INTO {DB}.{schema}.\"{table}\" ({col_list})\n"
        f"SELECT {expr_list}\n"
        f"FROM TABLE(GENERATOR(ROWCOUNT => {ROWS}))"
    )


def make_conn() -> snowflake.connector.SnowflakeConnection:
    return snowflake.connector.connect(
        account=os.getenv("SNOWFLAKE_ACCOUNT"),
        user=os.getenv("SNOWFLAKE_USER"),
        password=os.getenv("SNOWFLAKE_PASSWORD"),
        warehouse=WAREHOUSE,
        role=ROLE,
        database=DB,
        session_parameters={"QUERY_TAG": "scale_test_loader"},
    )


def collect_tasks(conn: snowflake.connector.SnowflakeConnection) -> list[tuple[str, str, list]]:
    log.info("Collecting table schemas (bulk query)...")
    schemas_csv = ", ".join(f"'{s}'" for s in SCHEMAS)
    cur = conn.cursor()
    cur.execute(
        f"SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH "
        f"FROM {DB}.INFORMATION_SCHEMA.COLUMNS "
        f"WHERE TABLE_SCHEMA IN ({schemas_csv}) "
        f"ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
    )
    schema_table_cols: dict[tuple, list] = defaultdict(list)
    for schema, table, col, dtype, max_len in cur.fetchall():
        schema_table_cols[(schema, table)].append((col, dtype, max_len))

    tasks = sorted((s, t, cols) for (s, t), cols in schema_table_cols.items())
    counts: dict[str, int] = {}
    for s, t, _ in tasks:
        counts[s] = counts.get(s, 0) + 1
    for schema in SCHEMAS:
        log.info(f"  {schema}: {counts.get(schema, 0)} tables")
    log.info(f"Total tasks: {len(tasks)}")
    return tasks


def main():
    start = time.time()
    log.info("Opening single Snowflake connection...")
    conn = make_conn()
    cur  = conn.cursor()

    tasks   = collect_tasks(conn)
    total   = len(tasks)
    loaded  = 0
    skipped = 0
    errors  = []

    log.info(f"Loading {ROWS} rows into {total} tables (single connection, sequential)...")

    for i, (schema, table, columns) in enumerate(tasks, 1):
        try:
            cur.execute(f'SELECT COUNT(*) FROM {DB}.{schema}."{table}"')
            if cur.fetchone()[0] >= ROWS:
                skipped += 1
            else:
                cur.execute(build_insert(schema, table, columns))
                loaded += 1
        except Exception as e:
            errors.append(f"{schema}.{table}: {e}")
            log.warning(f"FAIL {schema}.{table}: {e}")

        if i % 100 == 0 or i == total:
            elapsed = time.time() - start
            rate    = i / elapsed if elapsed else 0
            eta     = (total - i) / rate if rate else 0
            log.info(f"Progress: {i}/{total} | {rate:.1f} tbl/s | ETA {eta:.0f}s | loaded {loaded} skipped {skipped} errors {len(errors)}")

    conn.close()
    elapsed = time.time() - start
    log.info("=" * 60)
    log.info(f"Done in {elapsed:.0f}s — loaded {loaded}, skipped {skipped}, errors {len(errors)}")
    if errors:
        for e in errors[:10]:
            log.info(f"  {e}")


if __name__ == "__main__":
    main()
