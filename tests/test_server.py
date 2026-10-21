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
        assert len(names) == 16
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
