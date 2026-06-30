"""Pytest unit tests for governance_engine_mcp tools.

All IDMC API responses are mocked via unittest.mock.patch — no network
calls, no .env reads, no session minting against real IDMC.

Run from project root:
    pytest tests/test_governance_engine.py -v
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import governance_engine_mcp as gem


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
class FakeResponse:
    """Minimal stand-in for httpx.Response — only what our tools touch."""

    def __init__(self, status_code: int, body=None, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body) if body is not None else text

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


@pytest.fixture
def session_stub(monkeypatch):
    """Stand-in v2 session + JWT — bypasses .env reads and real logins."""
    monkeypatch.setattr(gem, "_current_session", lambda: "fake_sid")
    monkeypatch.setattr(gem, "_current_jwt", lambda: "fake_jwt")
    monkeypatch.setattr(gem, "_read_env", lambda: {
        "IDMC_USER": "u", "IDMC_PASS": "p",
        "IDMC_SESSION_ID": "fake_sid",
        "IDMC_SERVER_URL": "https://srv.example.com/saas",
        "IDMC_LOGIN_HOST": "dmp-us.informaticacloud.com",
    })
    return {"sid": "fake_sid", "jwt": "fake_jwt"}


# ---------------------------------------------------------------------------
# create_dq_rules
# ---------------------------------------------------------------------------
def test_create_dq_rules_success(session_stub):
    """Happy path: POST shell → PATCH body → GET verify, returns id + ui_url."""
    new_id = "0AbcDef1234567890ABC"
    responses = [
        FakeResponse(201, {"id": new_id}),                  # POST /Documents
        FakeResponse(204, text=""),                         # PATCH /Documents
        FakeResponse(200, {"documentState": "VALID"}),      # GET /Documents
    ]
    with patch("httpx.request", side_effect=responses) as req:
        out = gem.create_dq_rules(
            rule_name="TEST_NULL_CHECK",
            description="unit test",
            field_name="customer_name",
            dimension="COMPLETENESS",
        )

    assert out["id"] == new_id
    assert out["name"] == "TEST_NULL_CHECK"
    assert out["dimension"] == "COMPLETENESS"
    assert out["documentState"] == "VALID"
    assert new_id in out["ui_url"]
    assert req.call_count == 3

    # Every FRS call uses IDS-SESSION-ID auth
    for call in req.call_args_list:
        assert call.kwargs["headers"]["IDS-SESSION-ID"] == "fake_sid"

    # POST body carries the right type + dimension attribute
    post_body = req.call_args_list[0].kwargs["json"]
    assert post_body["documentType"] == "RULE_SPECIFICATION"
    assert post_body["name"] == "TEST_NULL_CHECK"
    assert any(
        a["name"] == "DIMENSION" and a["value"] == "COMPLETENESS"
        for a in post_body["customAttributes"]["stringAttrs"]
    )

    # PATCH body carries the ruleModel under nativeData.documentBlob
    patch_body = req.call_args_list[1].kwargs["json"]
    assert patch_body["id"] == new_id
    assert patch_body["documentState"] == "VALID"
    blob = json.loads(patch_body["nativeData"]["documentBlob"])
    rule_model = json.loads(blob["ruleModel"])
    assert rule_model["$$id"] == new_id
    assert rule_model["fields"][0]["name"] == "customer_name"


def test_create_dq_rules_401_retry(session_stub, monkeypatch):
    """Expired session: POST 401 → _login_v2 refreshes → retry uses new sid."""
    login_calls: list[tuple[str, str]] = []

    def fake_login():
        login_calls.append(("refreshed_sid", "https://srv.example.com/saas"))
        return login_calls[-1]

    monkeypatch.setattr(gem, "_login_v2", fake_login)

    new_id = "0XyZ9876543210AbCdEf"
    responses = iter([
        FakeResponse(401, text='{"error":"session expired"}'),  # POST initial
        FakeResponse(201, {"id": new_id}),                       # POST retry
        FakeResponse(204, text=""),                              # PATCH
        FakeResponse(200, {"documentState": "VALID"}),           # GET verify
    ])
    # _request mutates its headers dict in place, so call_args_list would
    # show the final value for every call. Snapshot the sid at call time.
    sid_at_call: list[str] = []

    def capture(method, url, **kw):
        sid_at_call.append((kw.get("headers") or {}).get("IDS-SESSION-ID", ""))
        return next(responses)

    with patch("httpx.request", side_effect=capture):
        out = gem.create_dq_rules(rule_name="RETRY_TEST")

    assert out["id"] == new_id
    assert len(login_calls) == 1, "expected exactly one _login_v2 call on 401"

    # The first POST used the stale sid; the retry used the refreshed sid.
    assert sid_at_call[0] == "fake_sid"
    assert sid_at_call[1] == "refreshed_sid"


def test_create_dq_rules_500_error_raises(session_stub):
    """IDMC 500 on the POST surfaces a RuntimeError naming the step."""
    with patch("httpx.request", side_effect=[FakeResponse(500, text='{"error":"internal"}')]):
        with pytest.raises(RuntimeError, match="POST Documents failed.*500"):
            gem.create_dq_rules(rule_name="WILL_FAIL")


def test_create_dq_rules_patch_failure_attempts_cleanup(session_stub):
    """PATCH failure: tool tries DELETE on the orphan shell before raising."""
    new_id = "0OrphanCleanupId01AB"
    responses = [
        FakeResponse(201, {"id": new_id}),         # POST OK
        FakeResponse(400, text='{"error":"bad"}'), # PATCH fails
        FakeResponse(204, text=""),                # DELETE cleanup
    ]
    with patch("httpx.request", side_effect=responses) as req:
        with pytest.raises(RuntimeError, match="PATCH failed.*400"):
            gem.create_dq_rules(rule_name="CLEANUP_TEST")

    methods = [c.args[0] for c in req.call_args_list]
    assert methods == ["POST", "PATCH", "DELETE"]


# ---------------------------------------------------------------------------
# generate_dq_mapping_task
# ---------------------------------------------------------------------------
def test_generate_dq_mapping_task_parameter_binding(session_stub, monkeypatch):
    """Verify M_DQ_Generic $Source$ / $Target$ / $Input_Field_Map$ bindings."""
    # Skip the v3→v2 translation; pass a v2-shaped id directly.
    monkeypatch.setattr(gem, "_resolve_v2_mapping_id", lambda x: x)

    created = {
        "id": "010YK20B00MAPTASK001",
        "name": "mt_dq_test",
        "mappingId": "010YK20B0000000MAP01",
        "runtimeEnvironmentId": "010YK2250000000000DY",
        "containerId": None,
    }

    with patch.object(gem, "_request_v2", return_value=FakeResponse(201, created)) as req:
        out = gem.generate_dq_mapping_task(
            source_connection_id="010YK20B000000000044",
            source_table="CUSTOMER_POSITIONS",
            target_connection_id="010YK20B000000000044",
            target_table="CUSTOMER_POSITIONS_BAD_RECORDS",
            input_field_mapping="customer_name=Input",
            runtime_environment_id="010YK2250000000000DY",
            template_mapping_id="010YK20B0000000MAP01",  # M_DQ_Generic
            rule_spec_id="0RuleSpec1234567890A",
        )

    req.assert_called_once()
    args, kwargs = req.call_args
    assert args[0] == "POST"
    assert args[1] == "/api/v2/mttask/"

    body = kwargs["json"]
    assert body["@type"] == "mtTask"
    assert body["mappingId"] == "010YK20B0000000MAP01"
    assert body["runtimeEnvironmentId"] == "010YK2250000000000DY"

    params = {p["name"]: p for p in body["parameters"]}
    assert set(params) == {"$Source$", "$Target$", "$Input_Field_Map$"}

    src = params["$Source$"]
    assert src["type"] == "EXTENDED_SOURCE"
    assert src["sourceConnectionId"] == "010YK20B000000000044"
    assert src["sourceObject"] == "CUSTOMER_POSITIONS"

    tgt = params["$Target$"]
    assert tgt["type"] == "TARGET"
    assert tgt["targetConnectionId"] == "010YK20B000000000044"
    assert tgt["targetObject"] == "CUSTOMER_POSITIONS_BAD_RECORDS"
    assert tgt["operationType"] == "Insert", "Insert is required or runtime writes nothing"

    fm = params["$Input_Field_Map$"]
    assert fm["type"] == "STRING"
    assert fm["text"] == "customer_name=Input"

    assert out["id"] == "010YK20B00MAPTASK001"
    assert out["parameter_names"] == ["$Source$", "$Target$", "$Input_Field_Map$"]


def test_generate_dq_mapping_task_empty_field_map_rejected(session_stub):
    """Empty input_field_mapping raises ValueError (v2 returns APP_13506)."""
    with pytest.raises(ValueError, match="input_field_mapping is required"):
        gem.generate_dq_mapping_task(
            source_connection_id="010YK20B000000000044",
            source_table="CUSTOMER_POSITIONS",
            target_connection_id="010YK20B000000000044",
            target_table="CUSTOMER_POSITIONS_BAD_RECORDS",
            input_field_mapping="",
            runtime_environment_id="010YK2250000000000DY",
        )


def test_generate_dq_mapping_task_500_error_raises(session_stub, monkeypatch):
    monkeypatch.setattr(gem, "_resolve_v2_mapping_id", lambda x: x)
    err = FakeResponse(500, text='{"error":"boom"}')
    with patch.object(gem, "_request_v2", return_value=err):
        with pytest.raises(RuntimeError, match="generate_dq_mapping_task HTTP 500"):
            gem.generate_dq_mapping_task(
                source_connection_id="010YK20B000000000044",
                source_table="X",
                target_connection_id="010YK20B000000000044",
                target_table="X_BAD",
                input_field_mapping="a=Input",
                runtime_environment_id="010YK2250000000000DY",
                template_mapping_id="010YK20B0000000MAP01",
            )


# ---------------------------------------------------------------------------
# register_in_cdgc
# ---------------------------------------------------------------------------
def test_register_in_cdgc_two_item_batch_payload(session_stub):
    """Publish body must be a 2-item batch: RuleInstance OBJECT + RELATIONSHIP."""
    column_id = (
        "snowflake-origin://INCEPT_GOV_DEV/DQ_TEST/CUSTOMER_POSITIONS/customer_name"
        "~com.infa.odin.models.relational.Column"
    )
    rule_spec_id = "0FRSdocId1234567890Z"

    publish_response = {
        "items": [
            {
                "elementType": "OBJECT",
                "type": "com.infa.ccgf.models.governance.RuleInstance",
                "identity": "DQO-99",
                "internalIdentity": "abc-1234-uuid",
                "statusCode": 201,
                "messageCode": "CONTENT_SUCCESS",
            },
            {
                "elementType": "RELATIONSHIP",
                "statusCode": 201,
                "messageCode": "CONTENT_SUCCESS",
            },
        ],
    }

    with patch.object(gem, "_request_cdgc", return_value=FakeResponse(207, publish_response)) as req:
        out = gem.register_in_cdgc(
            rule_spec_id=rule_spec_id,
            column_id=column_id,
            occurrence_name="DEMO_OCC",
            dimension="COMPLETENESS",
            criticality="High",
            target=95.0,
            threshold=80.0,
        )

    req.assert_called_once()
    args, kwargs = req.call_args
    assert args[0] == "POST"
    assert "/ccgf-contentv2/api/v1/publish" in args[1]

    body = kwargs["json"]
    assert isinstance(body["items"], list) and len(body["items"]) == 2, \
        "publish must be a 2-item batch (RuleInstance OBJECT + RELATIONSHIP)"

    # Item 1: RuleInstance carrying TechnicalRuleReference
    obj = body["items"][0]
    assert obj["elementType"] == "OBJECT"
    assert obj["operation"] == "INSERT"
    assert obj["type"] == "com.infa.ccgf.models.governance.RuleInstance"
    assert obj["identityType"] == "PROVISIONAL"
    attrs = obj["attributes"]
    assert attrs["com.infa.ccgf.models.governance.TechnicalRuleReference"] == rule_spec_id
    # COMPLETENESS (CDQ-side) maps to "Completeness" (CDGC-side)
    assert attrs["com.infa.ccgf.models.governance.RuleType"] == "Completeness"
    assert attrs["core.name"] == "DEMO_OCC"
    assert attrs["com.infa.ccgf.models.governance.Criticality"] == "High"
    assert attrs["com.infa.ccgf.models.governance.Target"] == 95.0
    assert attrs["com.infa.ccgf.models.governance.Threshold"] == 80.0
    # catalog_origin derived from the column_id prefix
    assert attrs["core.origin"] == "snowflake-origin"

    # Item 2: RELATIONSHIP column → provisional rule occurrence
    rel = body["items"][1]
    assert rel["elementType"] == "RELATIONSHIP"
    assert rel["operation"] == "INSERT"
    assert rel["type"] == "com.infa.ccgf.models.governance.asscParentDataElementRuleInstance"
    assert rel["fromIdentity"] == column_id
    assert rel["toIdentity"] == obj["identity"], \
        "relationship must reference the OBJECT's provisional identity"
    assert rel["sourceIdentityType"] == "EXTERNAL"
    assert rel["targetIdentityType"] == "PROVISIONAL"

    assert out["occurrence_id"] == "DQO-99"
    assert out["internal_id"] == "abc-1234-uuid"
    assert out["http_status"] == 207


def test_register_in_cdgc_internal_identity_requires_origin(session_stub):
    """INTERNAL column_identity_type without catalog_origin raises before HTTP."""
    with pytest.raises(RuntimeError, match="catalog_origin is required"):
        gem.register_in_cdgc(
            rule_spec_id="X",
            column_id="raw-internal-uuid",
            occurrence_name="N",
            column_identity_type="INTERNAL",
        )


def test_register_in_cdgc_500_error_raises(session_stub):
    err = FakeResponse(500, text='{"error":"publish failed"}')
    with patch.object(gem, "_request_cdgc", return_value=err):
        with pytest.raises(RuntimeError, match="register_in_cdgc HTTP 500"):
            gem.register_in_cdgc(
                rule_spec_id="X",
                column_id="o://a/b/c~com.infa.odin.models.relational.Column",
                occurrence_name="N",
            )


def test_register_in_cdgc_partial_failure_in_207_raises(session_stub):
    """A 207 with a per-item failure surfaces a partial-failure error."""
    body = {
        "items": [
            {"elementType": "OBJECT", "type": "com.infa.ccgf.models.governance.RuleInstance",
             "statusCode": 400, "messageCode": "VALIDATION_FAILED",
             "identity": "DQO-X", "internalIdentity": None},
            {"elementType": "RELATIONSHIP", "statusCode": 201, "messageCode": "CONTENT_SUCCESS"},
        ],
    }
    with patch.object(gem, "_request_cdgc", return_value=FakeResponse(207, body)):
        with pytest.raises(RuntimeError, match="partial failure"):
            gem.register_in_cdgc(
                rule_spec_id="X",
                column_id="o://a/b/c~com.infa.odin.models.relational.Column",
                occurrence_name="N",
            )


# ---------------------------------------------------------------------------
# upload_dq_scores
# ---------------------------------------------------------------------------
def test_upload_dq_scores_uses_bearer_jwt(session_stub):
    """publishScore must hit CDGC with Authorization: Bearer <jwt> + X-INFA-ORG-ID."""
    captured: dict = {}

    def fake_request(method, url, **kw):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = dict(kw.get("headers") or {})
        captured["json"] = kw.get("json")
        return FakeResponse(204, text="")

    with patch("httpx.request", side_effect=fake_request):
        out = gem.upload_dq_scores(
            asset_id="ruleInstance-uuid-xyz",
            value=87.5,
            total_count=100,
            exception=12,
        )

    assert captured["method"] == "PATCH"
    assert captured["headers"].get("Authorization") == "Bearer fake_jwt", \
        "upload_dq_scores must send Bearer JWT, not raw IDS-SESSION-ID"
    assert captured["headers"].get("X-INFA-ORG-ID"), "tenant scoping header required"
    assert "/ccgf-ruleautomation/api/v1/dataQuality/publishScore" in captured["url"]
    assert "refBy=INTERNAL" in captured["url"]

    score = captured["json"]["scores"][0]
    assert score["assetId"] == "ruleInstance-uuid-xyz"
    facts = score["dqscore"]["facts"]
    assert facts["com.infa.ccgf.models.governance.value"] == 87.5
    assert facts["com.infa.ccgf.models.governance.totalCount"] == 100
    assert facts["com.infa.ccgf.models.governance.exception"] == 12
    assert facts["com.infa.ccgf.models.governance.scannedTime"], "scannedTime must default to now"

    assert out["http_status"] == 204
    assert out["asset_id"] == "ruleInstance-uuid-xyz"
    assert out["value"] == 87.5


def test_upload_dq_scores_500_error_raises(session_stub):
    with patch("httpx.request", return_value=FakeResponse(500, text="server error")):
        with pytest.raises(RuntimeError, match="upload_dq_scores HTTP 500"):
            gem.upload_dq_scores(
                asset_id="X", value=1.0, total_count=1, exception=0,
            )


# ---------------------------------------------------------------------------
# recommend_dq_rules (skipped — awaiting CC3's implementation)
#
# Interface spec under test:
#   recommend_dq_rules(profile_results: dict) -> dict
#     profile_results : per-column stats (null_count, null_pct, distinct_count,
#                       min, max, total_rows, ...)
#     returns         : {"recommendations": [{column_name, dimension,
#                        rule_template, suggested_rule_name, rationale,
#                        severity}, ...]}
# ---------------------------------------------------------------------------
def test_recommend_dq_rules_null_stats_to_completeness():
    """High null_pct on a string column → a COMPLETENESS recommendation."""
    profile = {
        "total_rows": 100,
        "columns": {
            "customer_name": {"data_type": "string", "null_count": 30, "null_pct": 0.30},
        },
    }
    out = gem.recommend_dq_rules(profile)
    assert any(
        r["column_name"] == "customer_name"
        and r["dimension"] == "COMPLETENESS"
        and "null" in r["rule_template"].lower()
        for r in out["recommendations"]
    )


def test_recommend_dq_rules_range_stats_to_accuracy():
    """Out-of-range numeric column → an ACCURACY recommendation."""
    profile = {
        "total_rows": 100,
        "columns": {
            "exposure": {
                "data_type": "decimal", "null_count": 0,
                "min": -50, "max": 200,
                "expected_min": 0, "expected_max": 100,
                "out_of_range_count": 8,
            },
        },
    }
    out = gem.recommend_dq_rules(profile)
    assert any(
        r["column_name"] == "exposure"
        and r["dimension"] == "ACCURACY"
        and "range" in r["rule_template"].lower()
        for r in out["recommendations"]
    )


def test_recommend_dq_rules_severity_bands():
    """Affected-row ratio maps to HIGH (>=10%) / MEDIUM (>=1%) / LOW."""
    profile = {
        "total_rows": 100,
        "columns": {
            "col_high":   {"data_type": "string", "null_count": 15},
            "col_medium": {"data_type": "string", "null_count": 2},
            "col_clean":  {"data_type": "string", "null_count": 0},
        },
    }
    out = gem.recommend_dq_rules(profile)
    by_col = {r["column_name"]: r for r in out["recommendations"]}
    assert by_col["col_high"]["severity"] == "HIGH"
    assert by_col["col_medium"]["severity"] == "MEDIUM"
    assert "col_clean" not in by_col, "zero-violation columns should not be recommended"


def test_recommend_dq_rules_empty_profile_returns_no_recommendations():
    out = gem.recommend_dq_rules({"total_rows": 100, "columns": {}})
    assert out.get("recommendations") == []


def test_recommend_dq_rules_each_rec_has_documented_keys():
    """Every recommendation must carry the keys named in the interface spec."""
    profile = {
        "total_rows": 10,
        "columns": {"customer_name": {"data_type": "string", "null_count": 3}},
    }
    out = gem.recommend_dq_rules(profile)
    required = {"column_name", "dimension", "rule_template",
                "suggested_rule_name", "rationale", "severity"}
    for r in out["recommendations"]:
        missing = required - r.keys()
        assert not missing, f"recommendation missing keys: {missing}"


def test_recommend_dq_rules_missing_total_rows_handled_gracefully():
    """A profile without total_rows should not crash — either skips severity
    computation or assumes a sane default. Should not raise."""
    profile = {"columns": {"customer_name": {"data_type": "string", "null_count": 3}}}
    gem.recommend_dq_rules(profile)  # must not raise
