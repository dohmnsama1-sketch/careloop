"""End-to-end over real Streamable HTTP (uvicorn in a subprocess), both protocol eras, plus auth."""
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from mcp import Client

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(data_file, token=None):
    port = _free_port()
    env = {**os.environ, "CARELOOP_NOW": "2026-10-23T08:05"}
    env.pop("CARELOOP_TOKEN", None)
    if token:
        env["CARELOOP_TOKEN"] = token
    proc = subprocess.Popen([sys.executable, "-m", "careloop.server", "--port", str(port), "--data", str(data_file)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=0.5).status_code == 200:
                return proc, port
        except httpx.HTTPError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError(proc.stderr.read().decode())


@pytest.fixture
def http_server(data_file):
    proc, port = _start(data_file)
    yield f"http://127.0.0.1:{port}/mcp"
    proc.terminate()
    proc.wait(5)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_streamable_http_end_to_end(http_server, mode):
    async with Client(http_server, mode=mode) as c:
        assert c.protocol_version in ("2026-07-28", "2025-11-25")
        assert c.server_info.name == "careloop"
        b = await c.call_tool("care_briefing", {"speaker": "Sara"})
        assert "Lisinopril is running low" in b.content[0].text
        q = await c.call_tool("refill_quote", {"speaker": "Sara", "medication": "lisinopril"})
        o = await c.call_tool("refill_confirm", {"speaker": "Omar", "code": q.structured_content["quote"]["code"]})
        assert o.structured_content["order"]["confirmation"].startswith("SIM-")


async def test_state_persists_across_http_sessions(http_server):
    async with Client(http_server) as c:
        await c.call_tool("note_add", {"speaker": "Omar", "text": "Mom wants tea at 4"})
    async with Client(http_server, mode="legacy") as c:
        r = await c.call_tool("whats_new", {"speaker": "Sara"})
        assert "tea at 4" in r.content[0].text


def test_bearer_token_required_when_configured(data_file):
    proc, port = _start(data_file, token="s3cret-token")
    try:
        url = f"http://127.0.0.1:{port}/mcp"
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
        hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        assert httpx.post(url, json=body, headers=hdr).status_code == 401
        assert httpx.post(url, json=body, headers={**hdr, "Authorization": "Bearer wrong"}).status_code == 401
        ok = httpx.post(url, json=body, headers={**hdr, "Authorization": "Bearer s3cret-token"})
        assert ok.status_code == 200 and "careloop" in ok.text
        assert httpx.get(f"http://127.0.0.1:{port}/healthz").json()["ok"] is True
    finally:
        proc.terminate()
        proc.wait(5)


def test_dns_rebinding_protection(http_server):
    hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "Host": "evil.example"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    r = httpx.post(http_server, json=body, headers=hdr)
    assert r.status_code in (400, 403, 421)
