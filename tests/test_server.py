import json

import pytest
from mcp import Client

from careloop.server import build_server

pytestmark = pytest.mark.anyio

UI_TOOLS = {"care_briefing": "briefing", "meds_due": "doses", "adherence_report": "adherence",
            "refill_quote": "order", "refill_confirm": "order", "alerts_list": "alerts"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def server(care):
    return build_server(care)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_handshake_and_protocol_versions(server, mode):
    async with Client(server, mode=mode) as c:
        assert c.protocol_version == ("2026-07-28" if mode == "auto" else "2025-11-25")
        names = {t.name for t in (await c.list_tools()).tools}
        assert len(names) == 17
        assert set(UI_TOOLS) <= names


async def test_tool_contract_quality(server):
    async with Client(server) as c:
        for t in (await c.list_tools()).tools:
            a = t.annotations
            assert a is not None, t.name
            for hint in (a.read_only_hint, a.destructive_hint, a.idempotent_hint, a.open_world_hint):
                assert hint is not None, f"{t.name} misses a behaviour hint"
            assert t.description and len(t.description) > 40, t.name
            for pname, prop in t.input_schema.get("properties", {}).items():
                assert prop.get("description"), f"{t.name}.{pname} has no description"
            assert len(t.name) <= 64 and t.name.replace("_", "").isalnum()


async def test_mcp_apps_cards_are_linked_and_served(server):
    async with Client(server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        for name, view in UI_TOOLS.items():
            assert tools[name].meta["ui"]["resourceUri"] == f"ui://careloop/{view}.html"
        res = {str(r.uri): r for r in (await c.list_resources()).resources}
        for view in set(UI_TOOLS.values()):
            uri = f"ui://careloop/{view}.html"
            assert res[uri].mime_type == "text/html;profile=mcp-app"
            body = (await c.read_resource(uri)).contents[0].text
            assert f'const VIEW = "{view}"' in body and "ui/notifications/tool-result" in body


async def test_voice_first_results(server):
    async with Client(server) as c:
        r = await c.call_tool("care_briefing", {"speaker": "Sara"})
        assert not r.is_error
        assert r.content[0].text.startswith("Good morning, Sara.")
        assert r.structured_content["recipient"] == "Nora"
        assert json.loads(r.content[1].text)["speech"] == r.content[0].text


async def test_full_purchase_flow_over_mcp(server):
    async with Client(server) as c:
        q = await c.call_tool("refill_quote", {"speaker": "sara", "medication": "blood pressure pill"})
        code = q.structured_content["quote"]["code"]
        assert "confirm order" in q.content[0].text
        o = await c.call_tool("refill_confirm", {"speaker": "sara", "code": code})
        assert o.structured_content["order"]["status"] == "placed"
        again = await c.call_tool("refill_confirm", {"speaker": "sara", "code": code})
        assert again.structured_content["duplicate"] is True


async def test_errors_are_spoken_tool_errors(server):
    async with Client(server) as c:
        r = await c.call_tool("dose_log", {"speaker": "Bob", "medication": "lisinopril"})
        assert r.is_error and "I don't know Bob" in r.content[0].text
        r = await c.call_tool("refill_confirm", {"speaker": "leila", "code": "AAAA"})
        assert r.is_error and "quote" in r.content[0].text


async def test_double_dose_guard_over_mcp(server):
    async with Client(server) as c:
        await c.call_tool("dose_log", {"speaker": "sara", "medication": "lisinopril"})
        r = await c.call_tool("dose_log", {"speaker": "omar", "medication": "lisinopril"})
        assert r.structured_content["logged"] is False and "Hold on" in r.content[0].text


async def test_resources_and_prompts(server):
    async with Client(server) as c:
        h = json.loads((await c.read_resource("careloop://household")).contents[0].text)
        assert h["recipient"] == "Nora"
        today = json.loads((await c.read_resource("careloop://today")).contents[0].text)
        assert len(today["today"]) == 5
        prompts = {p.name for p in (await c.list_prompts()).prompts}
        assert prompts == {"morning_briefing", "end_of_day_handoff"}
        p = await c.get_prompt("morning_briefing", {"speaker": "Omar"})
        assert "care_briefing" in p.messages[0].content.text


async def test_every_tool_end_to_end_over_mcp(server):
    """Drive all 17 tools through the MCP client, the way an assistant would."""
    async with Client(server) as c:
        async def call(tool, **args):
            r = await c.call_tool(tool, args)
            assert not r.is_error, (tool, r.content)
            assert r.structured_content["speech"], tool
            return r.structured_content
        assert (await call("household_overview"))["recipient"]
        await call("med_add", speaker="sara", name="Amlodipine", times=["09:00"], strength="5 mg",
                   aliases=["new pill"], pills_on_hand=20)
        await call("dose_log", speaker="sara", medication="new pill", status="skipped")
        await call("refill_status")
        await call("note_add", speaker="omar", text="Pharmacy closes at 6 today")
        assert "Pharmacy" in (await call("whats_new", speaker="sara"))["speech"]
        await call("checkin", speaker="nora", text="feeling fine", mood=4)
        await call("appointment_add", speaker="sara", title="Eye exam", when="2026-10-24T15:00", driver="leila")
        assert "Eye exam" in (await call("agenda", day="tomorrow"))["speech"]
        await call("meds_due", window_hours=12)
        await call("adherence_report", days=7)
        rep = await call("care_report", days=7)
        assert rep["metrics"]["doses_scheduled"] > 0
        await call("checkin", speaker="nora", text="I have chest pain")
        alerts = await call("alerts_list")
        await call("alert_ack", speaker="omar", alert_id=alerts["alerts"][0]["id"])
        bad = await c.call_tool("refill_confirm", {"speaker": "leila", "code": "ZZZZ"})
        assert bad.is_error


async def test_all_resources_and_prompts(server):
    async with Client(server) as c:
        uris = {str(r.uri) for r in (await c.list_resources()).resources}
        assert {"careloop://household", "careloop://today", "ui://careloop/report.html"} <= uris
        hh = (await c.read_resource("careloop://household")).contents[0].text
        assert "Nora" in hh
        assert "Lisinopril" in (await c.read_resource("careloop://today")).contents[0].text
        for p in (await c.list_prompts()).prompts:
            got = await c.get_prompt(p.name, {"speaker": "Sara"})
            assert "Sara" in got.messages[0].content.text


def test_bearer_guard_and_health(care, monkeypatch):
    from starlette.testclient import TestClient
    from careloop.server import http_app
    monkeypatch.setenv("CARELOOP_TOKEN", "s3cret")
    monkeypatch.setenv("CARELOOP_ALLOWED_HOSTS", "care.example.com")
    with TestClient(http_app(care), base_url="http://127.0.0.1:8765") as web:
        assert web.get("/healthz").status_code == 200
        r = web.post("/mcp", json={})
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        ok = web.post("/mcp", headers={"Authorization": "Bearer s3cret", "Accept": "application/json, text/event-stream"},
                      json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert ok.status_code != 401
