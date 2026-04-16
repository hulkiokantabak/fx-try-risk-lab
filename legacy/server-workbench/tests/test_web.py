from __future__ import annotations

import importlib
import json
import re
import sqlite3
import sys
import uuid
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _fresh_client(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_env: dict[str, str] | None = None,
) -> TestClient:
    data_dir = workspace / "data"
    monkeypatch.setenv("FX_APP_NAME", "FX TRY Risk Lab Test")
    monkeypatch.setenv("FX_DATABASE_URL", f"sqlite:///{(workspace / 'test.db').as_posix()}")
    monkeypatch.setenv("FX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("FX_REPORTS_DIR", str(data_dir / "reports"))
    monkeypatch.setenv("FX_EXPORTS_DIR", str(data_dir / "exports"))
    monkeypatch.setenv("FX_EVIDENCE_DIR", str(data_dir / "evidence_packs"))
    monkeypatch.setenv("FX_RAW_DATA_DIR", str(data_dir / "raw"))
    monkeypatch.setenv("FX_NORMALIZED_DATA_DIR", str(data_dir / "normalized"))
    monkeypatch.setenv("FX_LOG_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("FX_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")
    monkeypatch.setenv("FX_SESSION_SECRET", "test-session-secret")
    if extra_env:
        for key, value in extra_env.items():
            monkeypatch.setenv(key, value)

    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            sys.modules.pop(module_name, None)

    app_main = importlib.import_module("app.main")
    app = app_main.create_app()
    return TestClient(app)


@pytest.fixture
def workspace() -> Path:
    base = Path(".tmp") / "pytest-workspaces"
    base.mkdir(parents=True, exist_ok=True)
    workspace = base / str(uuid.uuid4())
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


@pytest.fixture
def client(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    with _fresh_client(workspace, monkeypatch) as client:
        yield client


def _extract_csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _report_ids_for_cycle(workspace: Path, cycle_id: int) -> dict[str, int]:
    with sqlite3.connect(workspace / "test.db") as connection:
        rows = connection.execute(
            """
            select report_type, id
            from reports
            where cycle_id = ?
            order by id asc
            """,
            (cycle_id,),
        ).fetchall()
    return {report_type: report_id for report_type, report_id in rows}


def _sample_feed(title_prefix: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{title_prefix}</title>
    <item>
      <title>{title_prefix} Item One</title>
      <link>https://example.com/{title_prefix.lower().replace(" ", "-")}-1</link>
      <description>First item summary.</description>
      <pubDate>Wed, 15 Apr 2026 10:00:00 GMT</pubDate>
      <author>Desk One</author>
    </item>
    <item>
      <title>{title_prefix} Item Two</title>
      <link>https://example.com/{title_prefix.lower().replace(" ", "-")}-2</link>
      <description>Second item summary.</description>
      <pubDate>Wed, 15 Apr 2026 11:00:00 GMT</pubDate>
      <author>Desk Two</author>
    </item>
  </channel>
</rss>
"""


def _sample_imf_payload() -> str:
    return json.dumps(
        {
            "values": {
                "PCPIPCH": {
                    "TUR": {
                        "2024": 58.51,
                        "2025": 35.4,
                    }
                },
                "BCA_NGDPD": {
                    "TUR": {
                        "2024": -2.1,
                        "2025": -1.8,
                    }
                },
                "NGDP_RPCH": {
                    "TUR": {
                        "2024": 3.2,
                        "2025": 2.9,
                    }
                },
            }
        }
    )


def _sample_evds_datagroups_payload() -> str:
    return json.dumps(
        {
            "items": [
                {
                    "DATAGROUP_CODE": "bie_uypucay",
                    "DATAGROUP_NAME": (
                        "CBRT Net Funding (Business, Million TRY) and Weighted Average "
                        "Funding Cost of the CBRT Funding (Percentage)"
                    ),
                },
                {
                    "DATAGROUP_CODE": "bie_mbr",
                    "DATAGROUP_NAME": "Central Bank Reserves (Million US Dollar)",
                },
            ]
        }
    )


def _sample_evds_series_catalog_payload() -> str:
    return json.dumps(
        {
            "items": [
                {
                    "SERIE_CODE": "TP.RK.USD.A",
                    "SERIE_NAME": "Central Bank Reserves",
                },
                {
                    "SERIE_CODE": "TP.DK.TEST.A",
                    "SERIE_NAME": "Weighted Average Funding Cost of the CBRT Funding",
                },
            ]
        }
    )


def _sample_evds_observations_payload(
    series_code: str,
    latest_value: float,
    previous_value: float,
) -> str:
    return json.dumps(
        {
            "items": [
                {
                    "Tarih": "14-04-2026",
                    series_code: previous_value,
                },
                {
                    "Tarih": "15-04-2026",
                    series_code: latest_value,
                },
            ]
        }
    )


def _sample_ecb_csv(latest_value: float, previous_value: float) -> str:
    return (
        "TIME_PERIOD,OBS_VALUE\n"
        f"2026-04-14,{previous_value}\n"
        f"2026-04-15,{latest_value}\n"
    )


def _sample_cbrt_policy_rate_html() -> str:
    return """
    <html>
      <body>
        <h1>1 Week Repo</h1>
        <div>DATE Borrowing Lending</div>
        <div>18.04.2025 - 46.00</div>
        <div>23.01.2026 - 37.00</div>
      </body>
    </html>
    """


def _sample_cbrt_irfcl_html() -> str:
    return """
    <html>
      <body>
        <h1>International Reserves and Foreign Currency Liquidity</h1>
        <a href="https://www.tcmb.gov.tr/files/irfcl_latest.zip">ZIP Link</a>
      </body>
    </html>
    """


def _sample_cbrt_irfcl_zip_bytes() -> bytes:
    shared_strings = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="5" uniqueCount="5">
  <si><t>INTERNATIONAL RESERVES AND FOREIGN CURRENCY LIQUIDITY (*)</t></si>
  <si><t>(Million US Dollars)</t></si>
  <si><t>March 2026</t></si>
  <si><t>I.A Official reserve assets</t></si>
  <si><t>I.A.1 Foreign currency reserves (in convertible foreign currencies)</t></si>
</sst>
"""
    sheet_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="4"><c r="B4" t="s"><v>0</v></c></row>
    <row r="9">
      <c r="B9" t="s"><v>1</v></c>
      <c r="C9"><v>46108</v></c>
      <c r="D9" t="s"><v>2</v></c>
      <c r="E9"><v>46115</v></c>
    </row>
    <row r="10">
      <c r="B10" t="s"><v>3</v></c>
      <c r="C10"><v>155339</v></c>
      <c r="D10"><v>150774</v></c>
      <c r="E10"><v>161645</v></c>
    </row>
    <row r="11">
      <c r="B11" t="s"><v>4</v></c>
      <c r="C11"><v>47591</v></c>
      <c r="D11"><v>41588</v></c>
      <c r="E11"><v>50729</v></c>
    </row>
  </sheetData>
</worksheet>
"""
    workbook_bytes = BytesIO()
    with zipfile.ZipFile(workbook_bytes, "w", compression=zipfile.ZIP_DEFLATED) as workbook_zip:
        workbook_zip.writestr("xl/sharedStrings.xml", shared_strings)
        workbook_zip.writestr("xl/worksheets/sheet1.xml", sheet_xml)

    outer_bytes = BytesIO()
    with zipfile.ZipFile(outer_bytes, "w", compression=zipfile.ZIP_DEFLATED) as outer_zip:
        outer_zip.writestr("URDL_TEST_ING.xlsx", workbook_bytes.getvalue())
    return outer_bytes.getvalue()


def _sample_cboe_csv(latest_value: float, previous_value: float) -> str:
    previous_high = previous_value + 0.5
    previous_low = previous_value - 0.3
    latest_open = latest_value - 0.4
    latest_high = latest_value + 0.3
    latest_low = latest_value - 0.6
    return (
        "DATE,OPEN,HIGH,LOW,CLOSE\n"
        f"2026-04-14,{previous_value:.2f},{previous_high:.2f},{previous_low:.2f},"
        f"{previous_value:.2f}\n"
        f"2026-04-15,{latest_open:.2f},{latest_high:.2f},{latest_low:.2f},"
        f"{latest_value:.2f}\n"
    )


def _sample_cboe_value_csv(symbol: str, latest_value: float, previous_value: float) -> str:
    return (
        f"DATE,{symbol}\n"
        f"2026-04-14,{previous_value:.6f}\n"
        f"2026-04-15,{latest_value:.6f}\n"
    )


def _sample_fred_csv(series_code: str, latest_value: float, previous_value: float) -> str:
    return (
        f"observation_date,{series_code}\n"
        f"2026-04-14,{previous_value:.2f}\n"
        f"2026-04-15,{latest_value:.2f}\n"
    )


def test_healthcheck_and_security_headers(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert "default-src 'self'" in response.headers["content-security-policy"]

    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert ready.json()["database_ok"] is True
    assert ready.json()["storage_ok"] is True


def test_homepage_is_publicly_reachable(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "FX TRY Risk Lab Test" in response.text
    assert "Dashboard" in response.text


def test_new_assessment_issues_csrf_token(client: TestClient) -> None:
    response = client.get("/assessments/new")
    assert response.status_code == 200
    assert 'name="csrf_token"' in response.text
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=lax" in response.headers["set-cookie"].lower()


def test_production_settings_require_explicit_session_secret(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FX_ENVIRONMENT", "production")
    monkeypatch.setenv("FX_DATABASE_URL", f"sqlite:///{(workspace / 'prod.db').as_posix()}")
    monkeypatch.setenv("FX_DATA_DIR", str(workspace / "data"))
    monkeypatch.delenv("FX_SESSION_SECRET", raising=False)

    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            sys.modules.pop(module_name, None)

    with pytest.raises(ValueError, match="FX_SESSION_SECRET"):
        app_main = importlib.import_module("app.main")
        app_main.create_app()


def test_local_production_http_override_keeps_app_public(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _fresh_client(
        workspace,
        monkeypatch,
        extra_env={
            "FX_ENVIRONMENT": "production",
            "FX_SECURE_COOKIES": "false",
        },
    ) as auth_client:
        home_page = auth_client.get("/")
        assert home_page.status_code == 200
        assert "Dashboard" in home_page.text


def test_create_assessment_requires_valid_csrf(client: TestClient) -> None:
    missing = client.post(
        "/assessments",
        data={
            "primary_horizon": "1m",
            "custom_context": "missing token",
        },
        follow_redirects=False,
    )
    assert missing.status_code == 422

    page = client.get("/assessments/new")
    csrf_token = _extract_csrf_token(page.text)
    invalid = client.post(
        "/assessments",
        data={
            "primary_horizon": "1m",
            "custom_context": "bad token",
            "csrf_token": f"{csrf_token}-tampered",
        },
        follow_redirects=False,
    )
    assert invalid.status_code == 403


def test_create_assessment_validates_horizon_and_creates_cycle(
    client: TestClient,
    workspace: Path,
) -> None:
    page = client.get("/assessments/new")
    csrf_token = _extract_csrf_token(page.text)

    invalid_horizon = client.post(
        "/assessments",
        data={
            "primary_horizon": "2y",
            "custom_context": "invalid horizon",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert invalid_horizon.status_code == 422

    page = client.get("/assessments/new")
    csrf_token = _extract_csrf_token(page.text)
    prompt = "Check Fed repricing, reserves stress, and sanctions risk around the CBRT."
    created = client.post(
        "/assessments",
        data={
            "primary_horizon": "3m",
            "custom_context": prompt,
            "refresh_official_data": "on",
            "include_news_chatter": "on",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert created.headers["location"].startswith("/assessments/")

    detail = client.get(created.headers["location"])
    assert detail.status_code == 200
    assert prompt in detail.text
    assert "Ankara" in detail.text
    assert "Ledger" in detail.text
    assert "Meridian" in detail.text
    assert "Turkey policy/reserve layer is incomplete" in detail.text
    assert "Expert Readiness" in detail.text
    assert "Next Setup Actions" in detail.text
    assert "Run queued source refreshes" in detail.text
    assert "Can I trust this cycle?" in detail.text
    assert "How this cycle fits the chain" in detail.text
    assert "Analyst Briefing Mode" in detail.text
    assert "Quick Read" in detail.text
    assert "Full Workup" in detail.text
    assert "Need-To-Know Terms" in detail.text

    evidence_pack = workspace / "data" / "evidence_packs" / "cycle-00001.json"
    assert evidence_pack.exists()
    evidence_payload = json.loads(evidence_pack.read_text(encoding="utf-8"))
    assert evidence_payload["primary_horizon"] == "3m"
    assert evidence_payload["schema_version"] == 4
    assert "assessment_timestamp" in evidence_payload
    assert evidence_payload["request_flags"]["refresh_official_data"] is True
    assert evidence_payload["request_flags"]["include_news_chatter"] is True
    assert "agents" in evidence_payload["expert_readiness"]
    assert evidence_payload["action_queue"][0]["title"] == "Run queued source refreshes"
    assert any(
        item["title"] == "Complete Turkey macro coverage"
        for item in evidence_payload["action_queue"]
    )
    assert {item["specialist_name"] for item in evidence_payload["activated_specialists"]} >= {
        "Ankara",
        "Ledger",
        "Meridian",
    }
    assert evidence_payload["refresh_plan"]["queued_count"] == 9
    assert evidence_payload["macro_summary"]["configured_series"] >= 9
    assert evidence_payload["macro_summary"]["series_with_observations"] == 0
    assert evidence_payload["price_summary"]["configured_series"] >= 12
    assert evidence_payload["price_summary"]["series_with_observations"] == 0
    assert evidence_payload["price_summary"]["market_regime"]["ready"] is False

    sources = client.get("/sources")
    assert sources.status_code == 200
    assert "queued" in sources.text


def test_follow_up_cycle_clones_setup_and_preserves_original(
    client: TestClient,
    workspace: Path,
) -> None:
    page = client.get("/assessments/new")
    csrf_token = _extract_csrf_token(page.text)
    prompt = "Carry the same TRY setup into a fresh follow-up snapshot."
    created = client.post(
        "/assessments",
        data={
            "primary_horizon": "6m",
            "custom_context": prompt,
            "refresh_official_data": "on",
            "include_news_chatter": "on",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert created.status_code == 303

    original_detail = client.get(created.headers["location"])
    assert "Create follow-up cycle" in original_detail.text
    follow_up_token = _extract_csrf_token(original_detail.text)
    follow_up = client.post(
        f"{created.headers['location']}/follow-up",
        data={"csrf_token": follow_up_token},
        follow_redirects=False,
    )
    assert follow_up.status_code == 303
    assert follow_up.headers["location"] == "/assessments/2"

    follow_up_detail = client.get(follow_up.headers["location"])
    assert follow_up_detail.status_code == 200
    assert "Created follow-up cycle 2 from cycle 1" in follow_up_detail.text
    assert "Follow-up to #1" in follow_up_detail.text
    assert prompt in follow_up_detail.text
    assert "Parent Cycle" in follow_up_detail.text
    assert "Can I trust this cycle?" in follow_up_detail.text
    assert "What changed since last cycle" in follow_up_detail.text

    with sqlite3.connect(workspace / "test.db") as connection:
        rows = connection.execute(
            """
            select
                id,
                label,
                primary_horizon,
                parent_cycle_id,
                user_prompt,
                summary,
                status,
                evidence_pack_path
            from assessment_cycles
            order by id asc
            """
        ).fetchall()

    assert len(rows) == 2
    first_cycle = rows[0]
    second_cycle = rows[1]
    assert first_cycle[0] == 1
    assert first_cycle[2] == "6m"
    assert first_cycle[3] is None
    assert first_cycle[4] == prompt
    assert "Follow-up to" not in first_cycle[1]
    assert second_cycle[0] == 2
    assert second_cycle[2] == "6m"
    assert second_cycle[3] == 1
    assert second_cycle[4] == prompt
    assert "Follow-up to #1" in second_cycle[1]
    assert "Follow-up to cycle 1." in second_cycle[5]
    assert second_cycle[6] == "draft"
    assert Path(first_cycle[7]).exists()
    assert Path(second_cycle[7]).exists()

    follow_up_pack = workspace / "data" / "evidence_packs" / "cycle-00002.json"
    follow_up_payload = json.loads(follow_up_pack.read_text(encoding="utf-8"))
    assert follow_up_payload["primary_horizon"] == "6m"
    assert follow_up_payload["request_flags"]["refresh_official_data"] is True
    assert follow_up_payload["request_flags"]["include_news_chatter"] is True
    assert "Follow-up to #1" in follow_up_payload["cycle_label"]

    history_page = client.get("/assessments")
    assert "Follow-up to #1" in history_page.text
    assert "1 follow-up" in history_page.text


def test_follow_up_delta_summary_flows_into_cycle_and_report(
    client: TestClient,
    workspace: Path,
) -> None:
    page = client.get("/assessments/new")
    csrf_token = _extract_csrf_token(page.text)
    created = client.post(
        "/assessments",
        data={
            "primary_horizon": "6m",
            "custom_context": "Compare this follow-up against the earlier TRY read.",
            "refresh_official_data": "on",
            "include_news_chatter": "on",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert created.status_code == 303

    original_detail = client.get(created.headers["location"])
    follow_up_token = _extract_csrf_token(original_detail.text)
    follow_up = client.post(
        f"{created.headers['location']}/follow-up",
        data={"csrf_token": follow_up_token},
        follow_redirects=False,
    )
    assert follow_up.status_code == 303

    first_pack = workspace / "data" / "evidence_packs" / "cycle-00001.json"
    second_pack = workspace / "data" / "evidence_packs" / "cycle-00002.json"
    first_payload = json.loads(first_pack.read_text(encoding="utf-8"))
    second_payload = json.loads(second_pack.read_text(encoding="utf-8"))
    first_payload.setdefault("price_summary", {}).setdefault("market_regime", {})[
        "regime_label"
    ] = "Broad EM/CEE relief"
    second_payload.setdefault("price_summary", {}).setdefault("market_regime", {})[
        "regime_label"
    ] = "Broad EM/CEE pressure"
    first_payload.setdefault("macro_summary", {}).setdefault("turkey_policy_reserves", {})[
        "regime_label"
    ] = "Domestic fragility stabilizing"
    second_payload.setdefault("macro_summary", {}).setdefault("turkey_policy_reserves", {})[
        "regime_label"
    ] = "Domestic fragility rising"
    first_pack.write_text(json.dumps(first_payload), encoding="utf-8")
    second_pack.write_text(json.dumps(second_payload), encoding="utf-8")

    timestamp = "2026-04-16 12:00:00"
    with sqlite3.connect(workspace / "test.db") as connection:
        connection.execute(
            """
            insert into house_views (
                cycle_id,
                primary_horizon,
                house_primary_score,
                house_confidence,
                disagreement_range,
                stress_flag,
                minority_risk_note,
                risk_curve,
                created_at,
                updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "6m",
                44.0,
                "medium",
                9.0,
                0,
                None,
                json.dumps({"1w": 24.0, "1m": 31.0, "3m": 38.0, "6m": 44.0, "1y": 48.0}),
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            insert into house_views (
                cycle_id,
                primary_horizon,
                house_primary_score,
                house_confidence,
                disagreement_range,
                stress_flag,
                minority_risk_note,
                risk_curve,
                created_at,
                updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                2,
                "6m",
                58.0,
                "high",
                11.0,
                0,
                None,
                json.dumps({"1w": 26.0, "1m": 37.0, "3m": 49.0, "6m": 58.0, "1y": 63.0}),
                timestamp,
                timestamp,
            ),
        )
        connection.executemany(
            """
            insert into cycle_specialist_activations (
                cycle_id,
                specialist_name,
                trigger_topic,
                materiality_reason,
                activated_at
            ) values (?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    "Ledger",
                    "reserves",
                    "Reserve pressure sat at the center of the prior cycle.",
                    timestamp,
                ),
                (2, "Ledger", "reserves", "Reserve pressure is still active.", timestamp),
                (2, "Ankara", "policy risk", "New domestic policy friction is in play.", timestamp),
            ],
        )
        connection.executemany(
            """
            insert into agent_round_outputs (
                cycle_id,
                agent_name,
                agent_role,
                round_name,
                stance,
                primary_horizon,
                primary_risk_score,
                confidence,
                risk_curve,
                top_drivers,
                counterevidence,
                watch_triggers,
                content,
                created_at,
                updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    "Atlas",
                    "Global Macro Economist",
                    "round4",
                    "bearish TRY",
                    "6m",
                    44.0,
                    "medium",
                    json.dumps({"6m": 44.0}),
                    json.dumps(["global rates pressure"]),
                    "Some external pressure was easing.",
                    json.dumps(["Fed repricing fade"]),
                    "Parent cycle final view.",
                    timestamp,
                    timestamp,
                ),
                (
                    2,
                    "Atlas",
                    "Global Macro Economist",
                    "round4",
                    "bearish TRY",
                    "6m",
                    58.0,
                    "high",
                    json.dumps({"6m": 58.0}),
                    json.dumps(["reserve fragility", "policy credibility"]),
                    "Some market relief is still possible.",
                    json.dumps(["CBRT reserve drawdown", "FX intervention footprint"]),
                    "Follow-up cycle final view.",
                    timestamp,
                    timestamp,
                ),
            ],
        )
        connection.commit()

    follow_up_detail = client.get("/assessments/2")
    assert follow_up_detail.status_code == 200
    assert "What changed since last cycle" in follow_up_detail.text
    assert "House view rose from 44.0 to 58.0 (+14.0)." in follow_up_detail.text
    assert (
        "Market regime moved from Broad EM/CEE relief to Broad EM/CEE pressure."
        in follow_up_detail.text
    )
    assert (
        "Turkey policy/reserve layer moved from Domestic fragility stabilizing "
        "to Domestic fragility rising." in follow_up_detail.text
    )
    assert "Specialist mix changed: added Ankara." in follow_up_detail.text
    assert (
        "Watch triggers shifted: new: CBRT reserve drawdown, FX intervention footprint; "
        "cleared: Fed repricing fade." in follow_up_detail.text
    )

    report_token = _extract_csrf_token(follow_up_detail.text)
    report_redirect = client.post(
        "/assessments/2/generate-report",
        data={"csrf_token": report_token},
        follow_redirects=False,
    )
    assert report_redirect.status_code == 303
    assert report_redirect.headers["location"] == "/assessments/2"

    report_ids = _report_ids_for_cycle(workspace, 2)
    html_report = client.get(f"/reports/{report_ids['html_assessment']}")
    assert html_report.status_code == 200
    assert "What changed since last cycle" in html_report.text
    assert "Against cycle #1." in html_report.text
    assert "House view rose from 44.0 to 58.0 (+14.0)." in html_report.text
    assert "Current watch triggers" in html_report.text
    assert "CBRT reserve drawdown" in html_report.text


def test_assessment_detail_can_refresh_queued_sources_from_cycle_screen(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = client.get("/assessments/new")
    csrf_token = _extract_csrf_token(page.text)
    created = client.post(
        "/assessments",
        data={
            "primary_horizon": "1m",
            "custom_context": "Refresh from cycle screen.",
            "refresh_official_data": "on",
            "include_news_chatter": "on",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert created.status_code == 303

    web_routes = importlib.import_module("app.routes.web")

    def fake_execute(_db, _settings):
        return {
            "processed_runs": 0,
            "status_counts": {},
            "message": "Processed queued refreshes from the cycle screen.",
        }

    monkeypatch.setattr(web_routes, "execute_queued_refreshes", fake_execute)

    detail_page = client.get(created.headers["location"])
    refresh_token = _extract_csrf_token(detail_page.text)
    refreshed = client.post(
        f"{created.headers['location']}/refresh-sources",
        data={"csrf_token": refresh_token},
        follow_redirects=False,
    )
    assert refreshed.status_code == 303
    assert refreshed.headers["location"] == created.headers["location"]

    refreshed_detail = client.get(refreshed.headers["location"])
    assert "Processed queued refreshes from the cycle screen." in refreshed_detail.text
    assert "Refresh queued sources" in refreshed_detail.text


def test_report_route_rejects_paths_outside_reports_directory(
    client: TestClient,
    workspace: Path,
) -> None:
    page = client.get("/assessments/new")
    csrf_token = _extract_csrf_token(page.text)
    created = client.post(
        "/assessments",
        data={
            "primary_horizon": "1m",
            "custom_context": "Security check for report loading.",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert created.status_code == 303

    outside_report = workspace / "outside-report.html"
    outside_report.write_text("<html><body>outside</body></html>", encoding="utf-8")

    with sqlite3.connect(workspace / "test.db") as connection:
        connection.execute(
            """
            insert into reports (
                cycle_id,
                report_type,
                title,
                file_path,
                generated_at,
                created_at,
                updated_at
            ) values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "html_assessment",
                "Outside report",
                str(outside_report),
                "2026-04-16 12:00:00",
                "2026-04-16 12:00:00",
                "2026-04-16 12:00:00",
            ),
        )
        report_id = connection.execute(
            "select id from reports where title = ?",
            ("Outside report",),
        ).fetchone()[0]
        connection.commit()

    blocked = client.get(f"/reports/{report_id}")
    assert blocked.status_code == 404
    assert "outside the reports directory" in blocked.text


def test_sources_page_shows_seeded_sources(client: TestClient) -> None:
    response = client.get("/sources")
    assert response.status_code == 200
    assert "CBRT Policy Rates" in response.text
    assert "CBRT Reserve Liquidity" in response.text
    assert "never-run" in response.text
    assert "Market Chatter" in response.text
    assert "Series:" in response.text


def test_macro_series_page_lists_seeded_catalog_and_accepts_new_series(
    client: TestClient,
) -> None:
    page = client.get("/macro-series")
    assert page.status_code == 200
    assert "Effective Federal Funds Rate" in page.text
    assert "US 2-Year Treasury Yield" in page.text
    assert "Broad Trade-Weighted US Dollar Index" in page.text
    assert "Turkey CPI Inflation" in page.text
    assert "domestic_rates" in page.text
    assert "CBRT One-Week Repo Policy Rate" in page.text
    assert "CBRT Official Reserve Assets" in page.text
    assert "CBRT Foreign Currency Reserves" in page.text
    assert "CBRT 1 Week Repo page" in page.text

    csrf_token = _extract_csrf_token(page.text)
    created = client.post(
        "/macro-series",
        data={
            "source_id": "2",
            "code": "DXY",
            "name": "US Dollar Index",
            "category": "global_dollar",
            "frequency": "daily",
            "unit": "index",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert created.status_code == 303

    refreshed = client.get("/macro-series")
    assert "US Dollar Index" in refreshed.text
    assert "DXY" in refreshed.text

    invalid_page = client.get("/macro-series")
    invalid_token = _extract_csrf_token(invalid_page.text)
    invalid = client.post(
        "/macro-series",
        data={
            "source_id": "2",
            "code": "../bad",
            "name": "Bad<Series>",
            "category": "Global Rates",
            "frequency": "Month ly",
            "unit": "index",
            "csrf_token": invalid_token,
        },
        follow_redirects=False,
    )
    assert invalid.status_code == 400


def test_evds_template_series_resolves_with_metadata_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = importlib.import_module("app.config")
    entities = importlib.import_module("app.models.entities")
    source_refresh = importlib.import_module("app.services.source_refresh")

    monkeypatch.setenv("FX_EVDS_API_KEY", "test-evds-key")
    settings = config.Settings()
    series = entities.MacroSeries(
        code="evds-search:Central Bank Reserves (Million US Dollar)|Central Bank Reserves",
        name="CBRT Central Bank Reserves (EVDS Template)",
        category="reserves",
        unit="usd_million",
        frequency="weekly",
    )

    def fake_fetch(
        url: str,
        timeout_seconds: int = 15,
        max_bytes: int = 2_000_000,
        extra_headers: dict[str, str] | None = None,
    ) -> str:
        assert extra_headers is not None
        assert extra_headers.get("key") == "test-evds-key"
        if "getCategorywithDatagroups" in url:
            return _sample_evds_datagroups_payload()
        if "serieList" in url:
            return _sample_evds_series_catalog_payload()
        if "/service/evds/series=" in url:
            assert "TP.RK.USD.A" in url
            return _sample_evds_observations_payload("TP.RK.USD.A", 136.2, 140.0)
        raise AssertionError(f"Unexpected EVDS URL fetched in test: {url}")

    monkeypatch.setattr(source_refresh, "_fetch_text", fake_fetch)

    result = source_refresh._fetch_evds_series(settings, series)

    assert isinstance(result, source_refresh.MacroSeriesFetchResult)
    assert len(result.observations) == 2
    assert result.observations[-1].value == 136.2
    resolved_payload = json.loads(result.raw_payload)
    assert resolved_payload["template_resolution"]["datagroup_code"] == "bie_mbr"
    assert resolved_payload["template_resolution"]["series_code"] == "TP.RK.USD.A"
    assert resolved_payload["template_resolution"]["series_name"] == "Central Bank Reserves"


def test_price_series_page_lists_seeded_catalog(
    client: TestClient,
    workspace: Path,
) -> None:
    page = client.get("/price-series")
    assert page.status_code == 200
    assert "EUR/TRY ECB Reference Rate" in page.text
    assert "Cboe VIX Index" in page.text
    assert "Cboe OVX Oil Volatility Index" in page.text
    assert "Cboe GVZ Gold Volatility Index" in page.text
    assert 'name="csrf_token"' in page.text

    csrf_token = _extract_csrf_token(page.text)
    with sqlite3.connect(workspace / "test.db") as connection:
        ecb_source_id = connection.execute(
            "select id from sources where slug = ?",
            ("ecb-exr",),
        ).fetchone()[0]
    created = client.post(
        "/price-series",
        data={
            "source_id": str(ecb_source_id),
            "symbol": "D.GBP.EUR.SP00.A",
            "name": "EUR/GBP ECB Reference Rate",
            "base_currency": "EUR",
            "quote_currency": "GBP",
            "frequency": "daily",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert created.status_code == 303

    refreshed = client.get("/price-series")
    assert "EUR/GBP ECB Reference Rate" in refreshed.text
    assert "D.GBP.EUR.SP00.A" in refreshed.text

    invalid_page = client.get("/price-series")
    invalid_token = _extract_csrf_token(invalid_page.text)
    invalid = client.post(
        "/price-series",
        data={
            "source_id": "4",
            "symbol": "../bad",
            "name": "Bad<Series>",
            "base_currency": "eur",
            "quote_currency": "",
            "frequency": "Week ly",
            "csrf_token": invalid_token,
        },
        follow_redirects=False,
    )
    assert invalid.status_code == 400


def test_source_fetch_rejects_unsafe_urls() -> None:
    source_refresh = importlib.import_module("app.services.source_refresh")

    with pytest.raises(ValueError, match="HTTPS"):
        source_refresh._fetch_text("http://127.0.0.1/test")

    with pytest.raises(ValueError, match="Local or loopback"):
        source_refresh._fetch_text("https://127.0.0.1/test")


def test_rates_backdrop_summary_flows_into_evidence_rounds_and_report(
    client: TestClient,
    workspace: Path,
) -> None:
    page = client.get("/assessments/new")
    csrf_token = _extract_csrf_token(page.text)
    created = client.post(
        "/assessments",
        data={
            "primary_horizon": "1m",
            "custom_context": "Track the carry backdrop for TRY.",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert created.status_code == 303

    db_path = workspace / "test.db"
    with sqlite3.connect(db_path) as connection:
        series_ids = dict(
            connection.execute(
                """
                select code, id
                from macro_series
                where code in (?, ?, ?, ?, ?)
                """,
                ("DTWEXBGS", "DGS2", "DGS10", "FEDFUNDS", "PCPIPCH/TUR"),
            ).fetchall()
        )
        connection.executemany(
            """
            insert into macro_observations (
                series_id,
                observation_date,
                release_date,
                value,
                notes,
                fetched_at
            ) values (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    series_ids["DTWEXBGS"],
                    "2026-04-14 00:00:00",
                    "2026-04-14 00:00:00",
                    122.1,
                    None,
                    "2026-04-15 12:00:00",
                ),
                (
                    series_ids["DTWEXBGS"],
                    "2026-04-15 00:00:00",
                    "2026-04-15 00:00:00",
                    123.6,
                    None,
                    "2026-04-15 12:00:00",
                ),
                (
                    series_ids["DGS2"],
                    "2026-04-14 00:00:00",
                    "2026-04-14 00:00:00",
                    4.30,
                    None,
                    "2026-04-15 12:00:00",
                ),
                (
                    series_ids["DGS2"],
                    "2026-04-15 00:00:00",
                    "2026-04-15 00:00:00",
                    4.85,
                    None,
                    "2026-04-15 12:00:00",
                ),
                (
                    series_ids["DGS10"],
                    "2026-04-14 00:00:00",
                    "2026-04-14 00:00:00",
                    4.15,
                    None,
                    "2026-04-15 12:00:00",
                ),
                (
                    series_ids["DGS10"],
                    "2026-04-15 00:00:00",
                    "2026-04-15 00:00:00",
                    4.55,
                    None,
                    "2026-04-15 12:00:00",
                ),
                (
                    series_ids["FEDFUNDS"],
                    "2026-03-01 00:00:00",
                    "2026-03-01 00:00:00",
                    4.25,
                    None,
                    "2026-04-15 12:00:00",
                ),
                (
                    series_ids["FEDFUNDS"],
                    "2026-04-01 00:00:00",
                    "2026-04-01 00:00:00",
                    4.50,
                    None,
                    "2026-04-15 12:00:00",
                ),
                (
                    series_ids["PCPIPCH/TUR"],
                    "2025-01-01 00:00:00",
                    "2025-01-01 00:00:00",
                    58.51,
                    None,
                    "2026-04-15 12:00:00",
                ),
                (
                    series_ids["PCPIPCH/TUR"],
                    "2026-01-01 00:00:00",
                    "2026-01-01 00:00:00",
                    35.40,
                    None,
                    "2026-04-15 12:00:00",
                ),
            ],
        )
        connection.commit()

    detail_page = client.get(created.headers["location"])
    rebuild_token = _extract_csrf_token(detail_page.text)
    rebuilt = client.post(
        f"{created.headers['location']}/rebuild",
        data={"csrf_token": rebuild_token},
        follow_redirects=False,
    )
    assert rebuilt.status_code == 303

    rebuilt_detail = client.get(rebuilt.headers["location"])
    assert "Rates Backdrop" in rebuilt_detail.text
    assert "Dollar and rates headwind" in rebuilt_detail.text
    assert "external carry backdrop is tightening" in rebuilt_detail.text
    assert "domestic disinflation is improving carry optics" in rebuilt_detail.text

    evidence_pack = workspace / "data" / "evidence_packs" / "cycle-00001.json"
    evidence_payload = json.loads(evidence_pack.read_text(encoding="utf-8"))
    rates_regime = evidence_payload["macro_summary"]["rates_regime"]
    assert rates_regime["ready"] is True
    assert rates_regime["regime_label"] == "Dollar and rates headwind"
    assert rates_regime["carry_signal"] == "domestic disinflation is improving carry optics"
    assert rates_regime["broad_dollar_value"] == 123.6
    assert rates_regime["us2y_value"] == 4.85
    assert rates_regime["us10y_value"] == 4.55

    rounds_token = _extract_csrf_token(rebuilt_detail.text)
    rounds = client.post(
        f"{created.headers['location']}/run-rounds",
        data={"csrf_token": rounds_token},
        follow_redirects=False,
    )
    assert rounds.status_code == 303

    rounds_detail = client.get(rounds.headers["location"])
    assert "broad dollar regime" in rounds_detail.text
    assert "global rates pressure" in rounds_detail.text

    report_token = _extract_csrf_token(rounds_detail.text)
    report_redirect = client.post(
        f"{created.headers['location']}/generate-report",
        data={"csrf_token": report_token},
        follow_redirects=False,
    )
    assert report_redirect.status_code == 303
    assert report_redirect.headers["location"] == created.headers["location"]

    report_detail = client.get(created.headers["location"])
    assert "Open PDF report" in report_detail.text
    assert "Open HTML twin" in report_detail.text

    cycle_id = int(created.headers["location"].rsplit("/", 1)[-1])
    report_ids = _report_ids_for_cycle(workspace, cycle_id)
    pdf_report = client.get(f"/reports/{report_ids['pdf_assessment']}")
    assert pdf_report.status_code == 200
    assert pdf_report.headers["content-type"].startswith("application/pdf")
    assert pdf_report.content.startswith(b"%PDF-1.4")

    html_report = client.get(f"/reports/{report_ids['html_assessment']}")
    assert "Dollar and rates headwind" in html_report.text
    assert "Broad Trade-Weighted US Dollar Index" in html_report.text


def test_turkey_policy_reserve_summary_flows_into_evidence_rounds_and_report(
    client: TestClient,
    workspace: Path,
) -> None:
    page = client.get("/assessments/new")
    csrf_token = _extract_csrf_token(page.text)
    created = client.post(
        "/assessments",
        data={
            "primary_horizon": "1m",
            "custom_context": "Check Turkey policy credibility and reserve strain.",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert created.status_code == 303

    db_path = workspace / "test.db"
    with sqlite3.connect(db_path) as connection:
        cbrt_rate_source_id = connection.execute(
            "select id from sources where slug = ?",
            ("cbrt-policy-rates",),
        ).fetchone()[0]
        cbrt_reserve_source_id = connection.execute(
            "select id from sources where slug = ?",
            ("cbrt-reserves",),
        ).fetchone()[0]
        connection.executemany(
            """
            insert into macro_series (
                source_id,
                code,
                name,
                category,
                unit,
                frequency,
                created_at,
                updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    cbrt_rate_source_id,
                    "CBRT.WAFC.TEST",
                    "CBRT Weighted Average Funding Cost",
                    "domestic_rates",
                    "percent",
                    "daily",
                    "2026-04-15 12:00:00",
                    "2026-04-15 12:00:00",
                ),
                (
                    cbrt_reserve_source_id,
                    "CBRT.RESERVES.TEST",
                    "CBRT Gross FX Reserves",
                    "reserves",
                    "usd_billion",
                    "weekly",
                    "2026-04-15 12:00:00",
                    "2026-04-15 12:00:00",
                ),
            ],
        )
        series_ids = dict(
            connection.execute(
                """
                select code, id
                from macro_series
                where code in (?, ?)
                """,
                ("CBRT.WAFC.TEST", "CBRT.RESERVES.TEST"),
            ).fetchall()
        )
        connection.executemany(
            """
            insert into macro_observations (
                series_id,
                observation_date,
                release_date,
                value,
                notes,
                fetched_at
            ) values (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    series_ids["CBRT.WAFC.TEST"],
                    "2026-04-14 00:00:00",
                    "2026-04-14 00:00:00",
                    49.00,
                    None,
                    "2026-04-15 12:00:00",
                ),
                (
                    series_ids["CBRT.WAFC.TEST"],
                    "2026-04-15 00:00:00",
                    "2026-04-15 00:00:00",
                    46.50,
                    None,
                    "2026-04-15 12:00:00",
                ),
                (
                    series_ids["CBRT.RESERVES.TEST"],
                    "2026-04-11 00:00:00",
                    "2026-04-11 00:00:00",
                    140.00,
                    None,
                    "2026-04-15 12:00:00",
                ),
                (
                    series_ids["CBRT.RESERVES.TEST"],
                    "2026-04-15 00:00:00",
                    "2026-04-15 00:00:00",
                    136.20,
                    None,
                    "2026-04-15 12:00:00",
                ),
            ],
        )
        connection.commit()

    detail_page = client.get(created.headers["location"])
    rebuild_token = _extract_csrf_token(detail_page.text)
    rebuilt = client.post(
        f"{created.headers['location']}/rebuild",
        data={"csrf_token": rebuild_token},
        follow_redirects=False,
    )
    assert rebuilt.status_code == 303

    rebuilt_detail = client.get(rebuilt.headers["location"])
    assert "Turkey Policy / Reserves" in rebuilt_detail.text
    assert "Domestic fragility rising" in rebuilt_detail.text
    assert "domestic policy stance is easing" in rebuilt_detail.text
    assert "reserve buffer is thinning" in rebuilt_detail.text

    evidence_pack = workspace / "data" / "evidence_packs" / "cycle-00001.json"
    evidence_payload = json.loads(evidence_pack.read_text(encoding="utf-8"))
    turkey_policy = evidence_payload["macro_summary"]["turkey_policy_reserves"]
    assert turkey_policy["ready"] is True
    assert turkey_policy["regime_label"] == "Domestic fragility rising"
    assert turkey_policy["policy_signal"] == "domestic policy stance is easing"
    assert turkey_policy["reserve_signal"] == "reserve buffer is thinning"
    assert turkey_policy["primary_domestic_rate_value"] == 46.5
    assert turkey_policy["primary_reserve_value"] == 136.2

    rounds_token = _extract_csrf_token(rebuilt_detail.text)
    rounds = client.post(
        f"{created.headers['location']}/run-rounds",
        data={"csrf_token": rounds_token},
        follow_redirects=False,
    )
    assert rounds.status_code == 303

    rounds_detail = client.get(rounds.headers["location"])
    assert "domestic policy-rate posture" in rounds_detail.text
    assert "reserve adequacy" in rounds_detail.text

    report_token = _extract_csrf_token(rounds_detail.text)
    report_redirect = client.post(
        f"{created.headers['location']}/generate-report",
        data={"csrf_token": report_token},
        follow_redirects=False,
    )
    assert report_redirect.status_code == 303
    assert report_redirect.headers["location"] == created.headers["location"]

    cycle_id = int(created.headers["location"].rsplit("/", 1)[-1])
    report_ids = _report_ids_for_cycle(workspace, cycle_id)
    html_report = client.get(f"/reports/{report_ids['html_assessment']}")
    assert "Domestic fragility rising" in html_report.text
    assert "CBRT Gross FX Reserves" in html_report.text


def test_evidence_pack_is_anchored_to_cycle_timestamp(
    client: TestClient,
    workspace: Path,
) -> None:
    page = client.get("/assessments/new")
    csrf_token = _extract_csrf_token(page.text)
    created = client.post(
        "/assessments",
        data={
            "primary_horizon": "1m",
            "custom_context": "Anchor this cycle to the original evidence timestamp.",
            "include_news_chatter": "on",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert created.status_code == 303

    db_path = workspace / "test.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "update assessment_cycles set assessment_timestamp = ? where id = 1",
            ("2026-04-15 12:00:00",),
        )
        source_ids = dict(
            connection.execute(
                "select slug, id from sources where slug in (?, ?, ?)",
                ("imf-data", "financial-news-rss", "market-chatter"),
            ).fetchall()
        )
        series_ids = dict(
            connection.execute(
                """
                select code, id
                from macro_series
                where code in (?)
                """,
                ("PCPIPCH/TUR",),
            ).fetchall()
        )
        price_ids = dict(
            connection.execute(
                """
                select symbol, id
                from price_series
                where symbol in (?, ?)
                """,
                ("D.TRY.EUR.SP00.A", "D.USD.EUR.SP00.A"),
            ).fetchall()
        )
        connection.executemany(
            """
            insert into macro_observations (
                series_id,
                observation_date,
                release_date,
                value,
                notes,
                fetched_at
            ) values (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    series_ids["PCPIPCH/TUR"],
                    "2025-01-01 00:00:00",
                    None,
                    44.0,
                    "Older IMF CPI read",
                    "2026-04-14 10:00:00",
                ),
                (
                    series_ids["PCPIPCH/TUR"],
                    "2026-01-01 00:00:00",
                    None,
                    29.0,
                    "Later IMF CPI read",
                    "2026-04-16 09:00:00",
                ),
            ],
        )
        connection.executemany(
            """
            insert into price_observations (
                series_id,
                observed_at,
                open_value,
                high_value,
                low_value,
                close_value,
                volume,
                fetched_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    price_ids["D.TRY.EUR.SP00.A"],
                    "2026-04-14 00:00:00",
                    40.5,
                    40.5,
                    40.5,
                    40.5,
                    None,
                    "2026-04-14 08:00:00",
                ),
                (
                    price_ids["D.TRY.EUR.SP00.A"],
                    "2026-04-15 00:00:00",
                    41.0,
                    41.0,
                    41.0,
                    41.0,
                    None,
                    "2026-04-15 08:00:00",
                ),
                (
                    price_ids["D.TRY.EUR.SP00.A"],
                    "2026-04-16 00:00:00",
                    43.0,
                    43.0,
                    43.0,
                    43.0,
                    None,
                    "2026-04-16 08:00:00",
                ),
                (
                    price_ids["D.USD.EUR.SP00.A"],
                    "2026-04-14 00:00:00",
                    1.09,
                    1.09,
                    1.09,
                    1.09,
                    None,
                    "2026-04-14 08:00:00",
                ),
                (
                    price_ids["D.USD.EUR.SP00.A"],
                    "2026-04-15 00:00:00",
                    1.10,
                    1.10,
                    1.10,
                    1.10,
                    None,
                    "2026-04-15 08:00:00",
                ),
                (
                    price_ids["D.USD.EUR.SP00.A"],
                    "2026-04-16 00:00:00",
                    1.09,
                    1.09,
                    1.09,
                    1.09,
                    None,
                    "2026-04-16 08:00:00",
                ),
            ],
        )
        connection.executemany(
            """
            insert into headlines (
                source_id,
                published_at,
                title,
                url,
                summary,
                sentiment_hint,
                tags,
                created_at,
                updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    source_ids["financial-news-rss"],
                    "2026-04-14 09:00:00",
                    "Before anchor headline",
                    "https://example.com/before-headline",
                    None,
                    None,
                    None,
                    "2026-04-14 09:00:00",
                    "2026-04-14 09:00:00",
                ),
                (
                    source_ids["financial-news-rss"],
                    "2026-04-16 09:00:00",
                    "After anchor headline",
                    "https://example.com/after-headline",
                    None,
                    None,
                    None,
                    "2026-04-16 09:00:00",
                    "2026-04-16 09:00:00",
                ),
            ],
        )
        connection.executemany(
            """
            insert into chatter_items (
                source_id,
                posted_at,
                author,
                content,
                url,
                trust_score,
                tags,
                created_at,
                updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    source_ids["market-chatter"],
                    "2026-04-14 10:00:00",
                    "macro_user",
                    "Before anchor chatter",
                    "https://example.com/before-chatter",
                    0.4,
                    None,
                    "2026-04-14 10:00:00",
                    "2026-04-14 10:00:00",
                ),
                (
                    source_ids["market-chatter"],
                    "2026-04-16 10:00:00",
                    "macro_user",
                    "After anchor chatter",
                    "https://example.com/after-chatter",
                    0.4,
                    None,
                    "2026-04-16 10:00:00",
                    "2026-04-16 10:00:00",
                ),
            ],
        )
        connection.commit()

    assessment_engine = importlib.import_module("app.services.assessment_engine")
    app_db = importlib.import_module("app.db")
    entities = importlib.import_module("app.models.entities")
    with app_db.SessionLocal() as session:
        cycle = session.get(entities.AssessmentCycle, 1)
        assert cycle is not None
        evidence_payload = assessment_engine.build_evidence_pack(
            session,
            cycle=cycle,
            refresh_official_data=False,
            include_news_chatter=True,
            activations=[],
        )

    assert evidence_payload["assessment_timestamp"] == "2026-04-15 12:00:00"
    assert evidence_payload["macro_summary"]["as_of"] == "2026-04-15 12:00:00"
    assert evidence_payload["price_summary"]["as_of"] == "2026-04-15 12:00:00"
    assert evidence_payload["news_summary"]["as_of"] == "2026-04-15 12:00:00"

    inflation_row = next(
        item
        for item in evidence_payload["macro_summary"]["key_observations"]
        if item["code"] == "PCPIPCH/TUR"
    )
    assert inflation_row["observation_date"] == "2025-01-01"
    assert inflation_row["value"] == 44.0

    eur_try_row = next(
        item
        for item in evidence_payload["price_summary"]["series"]
        if item["symbol"] == "D.TRY.EUR.SP00.A"
    )
    assert eur_try_row["observed_at"] == "2026-04-15"
    assert eur_try_row["close_value"] == 41.0

    usd_try_row = next(
        item
        for item in evidence_payload["price_summary"]["derived_pairs"]
        if item["symbol"] == "USDTRY_DERIVED"
    )
    assert usd_try_row["observed_at"] == "2026-04-15"

    assert evidence_payload["news_summary"]["headline_count_14d"] == 1
    assert evidence_payload["news_summary"]["chatter_count_14d"] == 1
    assert (
        evidence_payload["news_summary"]["recent_headlines"][0]["title"]
        == "Before anchor headline"
    )
    assert (
        evidence_payload["news_summary"]["recent_chatter"][0]["content"]
        == "Before anchor chatter"
    )


def test_refresh_sources_executes_queued_runs(
    client: TestClient,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = client.get("/assessments/new")
    create_token = _extract_csrf_token(page.text)
    created = client.post(
        "/assessments",
        data={
            "primary_horizon": "1m",
            "custom_context": "Fed and reserves focus.",
            "refresh_official_data": "on",
            "include_news_chatter": "on",
            "csrf_token": create_token,
        },
        follow_redirects=False,
    )
    assert created.status_code == 303

    source_refresh = importlib.import_module("app.services.source_refresh")

    def fake_fetch(
        url: str,
        timeout_seconds: int = 15,
        max_bytes: int = 2_000_000,
        extra_headers: dict[str, str] | None = None,
    ) -> str:
        if "Central%2BBank%2BInterest%2BRates/1%2BWeek%2BRepo" in url:
            return _sample_cbrt_policy_rate_html()
        if "International%2BReserves%2Band%2BForeign%2BCurrency%2BLiquidity" in url:
            return _sample_cbrt_irfcl_html()
        if "news.google.com" in url:
            return _sample_feed("News Feed")
        if "reddit.com" in url:
            return _sample_feed("Chatter Feed")
        if "imf.org/external/datamapper/api/v1/" in url:
            return _sample_imf_payload()
        if "fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10" in url:
            return _sample_fred_csv("DGS10", 4.35, 4.20)
        if "fred.stlouisfed.org/graph/fredgraph.csv?id=DGS2" in url:
            return _sample_fred_csv("DGS2", 3.82, 3.75)
        if "fred.stlouisfed.org/graph/fredgraph.csv?id=FEDFUNDS" in url:
            return _sample_fred_csv("FEDFUNDS", 4.33, 4.33)
        if "fred.stlouisfed.org/graph/fredgraph.csv?id=DTWEXBGS" in url:
            return _sample_fred_csv("DTWEXBGS", 117.20, 116.85)
        if "data-api.ecb.europa.eu" in url and "D.TRY.EUR.SP00.A" in url:
            return _sample_ecb_csv(41.5, 41.1)
        if "data-api.ecb.europa.eu" in url and "D.USD.EUR.SP00.A" in url:
            return _sample_ecb_csv(1.12, 1.10)
        if "data-api.ecb.europa.eu" in url and "D.ZAR.EUR.SP00.A" in url:
            return _sample_ecb_csv(20.6, 20.2)
        if "data-api.ecb.europa.eu" in url and "D.BRL.EUR.SP00.A" in url:
            return _sample_ecb_csv(6.3, 6.2)
        if "data-api.ecb.europa.eu" in url and "D.HUF.EUR.SP00.A" in url:
            return _sample_ecb_csv(402.0, 398.0)
        if "data-api.ecb.europa.eu" in url and "D.PLN.EUR.SP00.A" in url:
            return _sample_ecb_csv(4.31, 4.29)
        if url.endswith("/VIX9D_History.csv"):
            return _sample_cboe_csv(20.40, 18.40)
        if url.endswith("/VVIX_History.csv"):
            return _sample_cboe_csv(103.00, 96.00)
        if url.endswith("/VXEEM_History.csv"):
            return _sample_cboe_csv(42.40, 40.50)
        if url.endswith("/OVX_History.csv"):
            return _sample_cboe_value_csv("OVX", 75.35, 80.55)
        if url.endswith("/GVZ_History.csv"):
            return _sample_cboe_value_csv("GVZ", 31.04, 30.00)
        if url.endswith("/VIX_History.csv"):
            return _sample_cboe_csv(18.95, 18.20)
        raise AssertionError(f"Unexpected URL fetched in test: {url}")

    def fake_fetch_bytes(
        url: str,
        timeout_seconds: int = 15,
        max_bytes: int = 2_000_000,
        extra_headers: dict[str, str] | None = None,
    ) -> bytes:
        if url.endswith("irfcl_latest.zip"):
            return _sample_cbrt_irfcl_zip_bytes()
        raise AssertionError(f"Unexpected binary URL fetched in test: {url}")

    monkeypatch.setattr(source_refresh, "_fetch_text", fake_fetch)
    monkeypatch.setattr(source_refresh, "_fetch_bytes", fake_fetch_bytes)

    sources_page = client.get("/sources")
    refresh_token = _extract_csrf_token(sources_page.text)
    refreshed = client.post(
        "/sources/refresh",
        data={"csrf_token": refresh_token},
        follow_redirects=False,
    )
    assert refreshed.status_code == 303
    assert refreshed.headers["location"] == "/sources"

    refreshed_page = client.get("/sources")
    assert "Processed 9 queued refreshes." in refreshed_page.text
    assert "success" in refreshed_page.text
    assert "config-required" in refreshed_page.text

    detail_page = client.get(created.headers["location"])
    rebuild_token = _extract_csrf_token(detail_page.text)
    rebuilt = client.post(
        f"{created.headers['location']}/rebuild",
        data={"csrf_token": rebuild_token},
        follow_redirects=False,
    )
    assert rebuilt.status_code == 303
    rebuilt_detail = client.get(rebuilt.headers["location"])
    assert "Rebuilt Round 0 evidence pack" in rebuilt_detail.text
    assert "Turkey CPI Inflation" in rebuilt_detail.text
    assert "CBRT One-Week Repo Policy Rate" in rebuilt_detail.text
    assert "CBRT Official Reserve Assets" in rebuilt_detail.text
    assert "USD/TRY Derived From ECB EUR Crosses" in rebuilt_detail.text
    assert rebuilt_detail.text.count("USD/TRY Derived From ECB EUR Crosses") == 1
    assert "USD/ZAR Derived From ECB EUR Crosses" in rebuilt_detail.text
    assert "Market Regime" in rebuilt_detail.text
    assert "TRY vs Peers Gap" in rebuilt_detail.text
    assert "Cboe VIX Index" in rebuilt_detail.text
    assert "Cboe VIX9D Index" in rebuilt_detail.text
    assert "Cboe VVIX Index" in rebuilt_detail.text
    assert "Cboe VXEEM Index" in rebuilt_detail.text
    assert "OVX" in rebuilt_detail.text
    assert "GVZ" in rebuilt_detail.text
    assert "Volatility Regime" in rebuilt_detail.text
    assert "Trend:" in rebuilt_detail.text

    rounds_token = _extract_csrf_token(rebuilt_detail.text)
    rounds = client.post(
        f"{created.headers['location']}/run-rounds",
        data={"csrf_token": rounds_token},
        follow_redirects=False,
    )
    assert rounds.status_code == 303

    rounds_detail = client.get(rounds.headers["location"])
    assert "FX Experts completed rounds 1-4" in rounds_detail.text
    assert "Round 1 - Topic Framing" in rounds_detail.text
    assert "Round 4 - Final Verdict" in rounds_detail.text
    assert "House View" in rounds_detail.text
    assert "Atlas" in rounds_detail.text
    assert "Bosphorus" in rounds_detail.text

    report_token = _extract_csrf_token(rounds_detail.text)
    report_redirect = client.post(
        f"{created.headers['location']}/generate-report",
        data={"csrf_token": report_token},
        follow_redirects=False,
    )
    assert report_redirect.status_code == 303
    assert report_redirect.headers["location"] == created.headers["location"]

    cycle_id = int(created.headers["location"].rsplit("/", 1)[-1])
    report_ids = _report_ids_for_cycle(workspace, cycle_id)

    refreshed_cycle_page = client.get(created.headers["location"])
    assert "Open PDF report" in refreshed_cycle_page.text
    assert "Open HTML twin" in refreshed_cycle_page.text

    pdf_report_page = client.get(f"/reports/{report_ids['pdf_assessment']}")
    assert pdf_report_page.status_code == 200
    assert pdf_report_page.headers["content-type"].startswith("application/pdf")
    assert pdf_report_page.content.startswith(b"%PDF-1.4")

    html_report_page = client.get(f"/reports/{report_ids['html_assessment']}")
    assert html_report_page.status_code == 200
    assert "<title>TRY Risk Cycle" in html_report_page.text
    assert "Final core-agent verdicts" in html_report_page.text
    assert "House Probability" in html_report_page.text
    assert "Expert coverage before debate" in html_report_page.text
    assert "Next setup actions" in html_report_page.text
    assert "CBRT One-Week Repo Policy Rate" in html_report_page.text
    assert "Market snapshot" in html_report_page.text
    assert "USD/TRY Derived From ECB EUR Crosses" in html_report_page.text
    assert "Cboe VIX9D Index" in html_report_page.text
    assert "Cboe VXEEM Index" in html_report_page.text
    assert "Cboe OVX Oil Volatility Index" in html_report_page.text
    assert "Cboe GVZ Gold Volatility Index" in html_report_page.text
    assert "Broad EM/CEE relief" in html_report_page.text
    assert "cross-asset volatility stress rising" in html_report_page.text
    assert "oil volatility pressure is easing" in html_report_page.text

    evidence_page = client.get("/evidence")
    assert evidence_page.status_code == 200
    assert "Turkey CPI Inflation" in evidence_page.text
    assert "Cboe VIX Index" in evidence_page.text
    assert "Cboe VIX9D Index" in evidence_page.text
    assert "Cboe VXEEM Index" in evidence_page.text
    assert "Cboe OVX Oil Volatility Index" in evidence_page.text
    assert "Cboe GVZ Gold Volatility Index" in evidence_page.text
    assert "News Feed Item One" in evidence_page.text
    assert "Chatter Feed Item One" in evidence_page.text

    with sqlite3.connect(workspace / "test.db") as connection:
        connection.execute(
            "update assessment_cycles set assessment_timestamp = ? where id = 1",
            ("2026-04-08 12:00:00",),
        )
        eur_try_series_id = connection.execute(
            "select id from price_series where symbol = ?",
            ("D.TRY.EUR.SP00.A",),
        ).fetchone()[0]
        eur_usd_series_id = connection.execute(
            "select id from price_series where symbol = ?",
            ("D.USD.EUR.SP00.A",),
        ).fetchone()[0]
        connection.executemany(
            """
            insert into price_observations (
                series_id,
                observed_at,
                open_value,
                high_value,
                low_value,
                close_value,
                volume,
                fetched_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    eur_try_series_id,
                    "2026-04-08 00:00:00",
                    39.2,
                    39.2,
                    39.2,
                    39.2,
                    None,
                    "2026-04-15 12:00:00",
                ),
                (
                    eur_usd_series_id,
                    "2026-04-08 00:00:00",
                    1.12,
                    1.12,
                    1.12,
                    1.12,
                    None,
                    "2026-04-15 12:00:00",
                ),
                (
                    eur_try_series_id,
                    "2026-05-15 00:00:00",
                    44.8,
                    44.8,
                    44.8,
                    44.8,
                    None,
                    "2026-05-15 12:00:00",
                ),
                (
                    eur_usd_series_id,
                    "2026-05-15 00:00:00",
                    1.12,
                    1.12,
                    1.12,
                    1.12,
                    None,
                    "2026-05-15 12:00:00",
                ),
            ],
        )
        connection.commit()

    backtesting_page = client.get("/backtesting")
    assert backtesting_page.status_code == 200
    assert 'name="csrf_token"' in backtesting_page.text
    backtesting_token = _extract_csrf_token(backtesting_page.text)
    refreshed_backtesting = client.post(
        "/backtesting/refresh",
        data={"csrf_token": backtesting_token},
        follow_redirects=False,
    )
    assert refreshed_backtesting.status_code == 303

    refreshed_backtesting_page = client.get("/backtesting")
    assert "Resolved Primary Horizons" in refreshed_backtesting_page.text
    assert "Triggered" in refreshed_backtesting_page.text
    assert "Mean Abs Error" in refreshed_backtesting_page.text
    assert "Trigger Rate" in refreshed_backtesting_page.text
    assert "Calibration Bias" in refreshed_backtesting_page.text
    assert "Calibration By Horizon" in refreshed_backtesting_page.text
    assert "1m" in refreshed_backtesting_page.text

    outcome_detail = client.get(created.headers["location"])
    assert "Realized Outcomes" in outcome_detail.text
    assert "Calibration gap" in outcome_detail.text
    assert "Triggered" in outcome_detail.text

    regenerate_page = client.get(created.headers["location"])
    regenerate_token = _extract_csrf_token(regenerate_page.text)
    regenerated = client.post(
        f"{created.headers['location']}/generate-report",
        data={"csrf_token": regenerate_token},
        follow_redirects=False,
    )
    assert regenerated.status_code == 303
    assert regenerated.headers["location"] == created.headers["location"]

    regenerated_report_ids = _report_ids_for_cycle(workspace, cycle_id)
    regenerated_html_report_page = client.get(
        f"/reports/{regenerated_report_ids['html_assessment']}"
    )
    assert "Realized outcomes to date" in regenerated_html_report_page.text
    assert "triggered" in regenerated_html_report_page.text

    db_path = workspace / "test.db"
    with sqlite3.connect(db_path) as connection:
        headline_count = connection.execute("select count(*) from headlines").fetchone()[0]
        chatter_count = connection.execute("select count(*) from chatter_items").fetchone()[0]
        macro_count = connection.execute("select count(*) from macro_observations").fetchone()[0]
        price_count = connection.execute("select count(*) from price_observations").fetchone()[0]
        realized_outcome_count = connection.execute(
            "select count(*) from realized_outcomes"
        ).fetchone()[0]
        resolved_outcome_count = connection.execute(
            "select count(*) from realized_outcomes where event_occurred is not null"
        ).fetchone()[0]
        round_outputs = connection.execute(
            "select count(*) from agent_round_outputs"
        ).fetchone()[0]
        house_views = connection.execute("select count(*) from house_views").fetchone()[0]
        reports = connection.execute("select count(*) from reports").fetchone()[0]
        report_file = connection.execute(
            "select file_path from reports order by id desc limit 1"
        ).fetchone()[0]
        cycle_status = connection.execute(
            "select status from assessment_cycles where id = 1"
        ).fetchone()[0]
        statuses = dict(
            connection.execute(
                "select status, count(*) from source_fetch_runs group by status"
            ).fetchall()
        )

    assert headline_count == 2
    assert chatter_count == 2
    assert macro_count == 20
    assert price_count == 28
    assert realized_outcome_count == 5
    assert resolved_outcome_count == 2
    assert round_outputs >= 18
    assert house_views == 1
    assert reports == 2
    assert Path(report_file).exists()
    assert Path(report_file).suffix == ".pdf"
    assert cycle_status == "assessed"
    assert statuses["success"] == 8
    assert statuses["config-required"] == 1

    evidence_pack = workspace / "data" / "evidence_packs" / "cycle-00001.json"
    evidence_payload = json.loads(evidence_pack.read_text(encoding="utf-8"))
    assert evidence_payload["refresh_plan"]["queued_count"] == 0
    assert evidence_payload["macro_summary"]["series_with_observations"] == 10
    assert evidence_payload["price_summary"]["series_with_observations"] == 12
    turkey_policy = evidence_payload["macro_summary"]["turkey_policy_reserves"]
    assert turkey_policy["primary_domestic_rate_delta"] == -9.0
    assert turkey_policy["primary_reserve_delta"] == 3138.0
    assert turkey_policy["reserve_signal"] == "reserve buffer is rebuilding"
    assert len(evidence_payload["price_summary"]["derived_pairs"]) == 5
    assert evidence_payload["price_summary"]["market_regime"]["ready"] is True
    assert evidence_payload["price_summary"]["market_regime"]["peer_count"] == 4
    assert evidence_payload["price_summary"]["market_regime"]["volatility_regime_label"] == (
        "Cross-asset volatility stress"
    )
    assert evidence_payload["price_summary"]["market_regime"]["commodity_vol_signal"] == (
        "commodity volatility is rising in at least one key complex"
    )
    assert "growth" in evidence_payload["expert_readiness"]["available_categories"]
    assert not any(
        item["title"] == "Strengthen external macro coverage"
        for item in evidence_payload["action_queue"]
    )
    assert evidence_payload["news_summary"]["headline_count_14d"] == 2

    refreshed_rounds_detail = client.get(created.headers["location"])
    invalidate_token = _extract_csrf_token(refreshed_rounds_detail.text)
    invalidated = client.post(
        f"{created.headers['location']}/rebuild",
        data={"csrf_token": invalidate_token},
        follow_redirects=False,
    )
    assert invalidated.status_code == 303
    invalidated_detail = client.get(invalidated.headers["location"])
    assert "Expert debate outputs are not stored yet" in invalidated_detail.text
    assert "House View" not in invalidated_detail.text

    with sqlite3.connect(db_path) as connection:
        round_outputs_after_rebuild = connection.execute(
            "select count(*) from agent_round_outputs"
        ).fetchone()[0]
        house_views_after_rebuild = connection.execute(
            "select count(*) from house_views"
        ).fetchone()[0]
        reports_after_rebuild = connection.execute(
            "select count(*) from reports"
        ).fetchone()[0]
        cycle_status_after_rebuild = connection.execute(
            "select status from assessment_cycles where id = 1"
        ).fetchone()[0]

    assert round_outputs_after_rebuild == 0
    assert house_views_after_rebuild == 0
    assert reports_after_rebuild == 0
    assert not Path(report_file).exists()
    assert cycle_status_after_rebuild == "draft"
