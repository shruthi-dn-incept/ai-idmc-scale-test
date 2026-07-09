#!/usr/bin/env python
"""Load real column data_types from Snowflake INFORMATION_SCHEMA.COLUMNS.

One query returns every column in the database — authoritative metadata, no
per-column API calls. Returns {(SCHEMA, TABLE, COLUMN): DATA_TYPE} (upper-cased).
Key-pair auth via SNOWFLAKE_PRIVATE_KEY_B64.
"""
import base64
import os


def _private_key_der():
    b64 = os.getenv("SNOWFLAKE_PRIVATE_KEY_B64", "")
    if not b64:
        return None
    from cryptography.hazmat.primitives import serialization
    pem = base64.b64decode(b64)
    pk = serialization.load_pem_private_key(pem, password=None)
    return pk.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def make_conn():
    import snowflake.connector
    kwargs = dict(
        account=os.getenv("SNOWFLAKE_ACCOUNT"),
        user=os.getenv("SNOWFLAKE_USER"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE", "INCEPT_WH"),
        role=os.getenv("SNOWFLAKE_ROLE", "ACCOUNTADMIN"),
        database=os.getenv("SNOWFLAKE_GOVTEST_DB", "GOVERNANCE_SCALE_TEST"),
        session_parameters={"QUERY_TAG": "dqro_type_map"},
    )
    der = _private_key_der()
    if der:
        kwargs["private_key"] = der
    else:
        kwargs["password"] = os.getenv("SNOWFLAKE_PASSWORD")
    return snowflake.connector.connect(**kwargs)


def load_type_map(schemas=None) -> dict:
    """Return {(SCHEMA, TABLE, COLUMN): DATA_TYPE} for the whole DB (or given schemas)."""
    db = os.getenv("SNOWFLAKE_GOVTEST_DB", "GOVERNANCE_SCALE_TEST")
    sql = (f"SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE "
           f"FROM {db}.INFORMATION_SCHEMA.COLUMNS")
    if schemas:
        inlist = ",".join("'" + s.upper() + "'" for s in schemas)
        sql += f" WHERE TABLE_SCHEMA IN ({inlist})"
    conn = make_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql)
        m = {}
        for sch, tbl, col, dt in cur:
            m[(sch.upper(), tbl.upper(), col.upper())] = dt
        return m
    finally:
        conn.close()


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".env"))
    schemas = ["GOVTEST_CLINICAL", "GOVTEST_MEMBER", "GOVTEST_CLAIMS", "GOVTEST_PROVIDER"]
    m = load_type_map(schemas)
    print("total columns from Snowflake:", len(m))
    from collections import Counter
    c = Counter(m.values())
    print("data_type distribution:", dict(c))
    # sample
    for k in list(m)[:5]:
        print("  ", k, "->", m[k])
