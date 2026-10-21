"""CareLoop MCP server: Streamable HTTP (MCP 2025-11-25 handshake and 2026-07-28 stateless), plus stdio.

Environment:
  CARELOOP_DATA   path of the household JSON file (default ./data/careloop.json)
  CARELOOP_HOST / CARELOOP_PORT   bind address (default 127.0.0.1:8765)
  CARELOOP_TOKEN  if set, every /mcp request needs "Authorization: Bearer <token>"
  CARELOOP_ALLOWED_HOSTS  extra Host headers to accept when deployed behind a domain (comma separated)
  CARELOOP_NOW    freeze the clock for demos, e.g. 2026-10-23T08:05
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
from pathlib import Path
import sys
from typing import Annotated, Any, Literal

from mcp.server.apps import Apps
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .cards import VIEWS, card_html, uri
from .clock import Clock
from .core import CareError, CareLoop
from .store import Store

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
ADD = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
UPDATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
BUY = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)

Speaker = Annotated[str, Field(description="Who is talking to the assistant: a household member's name or id "
                                            "(on Alexa+, the recognized speaker). Example: 'Sara'")]
Medication = Annotated[str, Field(description="Medication name or what it is for, e.g. 'Lisinopril' or 'blood pressure pill'")]

INSTRUCTIONS = """CareLoop coordinates a family caring for someone at home: medication schedule and dose log,
refills (two-step purchase), appointments, check-ins, alerts and handoff notes between caregivers.
Voice rules: every result has a `speech` field. Say it as written; it is already short and safe.
Always pass the recognized speaker. Never give medical advice or change doses; CareLoop records what the
family scheduled. For purchases, call refill_quote first, read the price and code aloud, and only call
refill_confirm after the user says the code back. If a check-in mentions an emergency, the reply tells the
user to call emergency services: repeat that first."""


def build_server(care: CareLoop) -> MCPServer:
    apps = Apps()

    def reply(result: dict[str, Any]) -> CallToolResult:
        result.setdefault("timezone", care.clock.tz.key)  # lets screen cards show household-local times
        return CallToolResult(
            content=[TextContent(type="text", text=result["speech"]),
                     TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))],
            structuredContent=result)

    def run(fn, *args, **kwargs) -> CallToolResult:
        try:
            return reply(fn(*args, **kwargs))
        except CareError as e:
            raise ToolError(str(e)) from e

    # -------------------------------------------------------------- UI-bound tools (render on screens)
    @apps.tool(resource_uri=uri("briefing"), name="care_briefing", title="Care briefing", annotations=UPDATE)
    def care_briefing(speaker: Speaker) -> CallToolResult:
        """The one-call update for a caregiver: alerts, missed and upcoming doses, today's and tomorrow's
        appointments, low supplies and new notes from the family since this person last checked.
        Use for 'how is Mom doing?', 'what do I need to know?', 'good morning'. Marks updates as seen."""
        return run(care.briefing, speaker)

    @apps.tool(resource_uri=uri("doses"), name="meds_due", title="Medications due", annotations=READ)
    def meds_due(window_hours: Annotated[float, Field(ge=0.5, le=24, description="How far ahead to look, in hours")] = 3) -> CallToolResult:
        """What medications are due now or soon, and which earlier doses today are not logged yet.
        Returns the full day as a carousel for screens."""
        return run(care.meds_due, window_hours)

    @apps.tool(resource_uri=uri("adherence"), name="adherence_report", title="Adherence report", annotations=READ)
    def adherence_report(days: Annotated[int, Field(ge=1, le=31, description="Number of days to look back")] = 7) -> CallToolResult:
        """How reliably doses were taken over the last N days, per day and per medication, with the most-missed one."""
        return run(care.adherence, days)

    @apps.tool(resource_uri=uri("order"), name="refill_quote", title="Refill quote", annotations=ADD)
    def refill_quote(speaker: Speaker, medication: Medication,
                     days_supply: Annotated[int, Field(ge=7, le=90, description="Days of supply to order")] = 30) -> CallToolResult:
        """Step 1 of a purchase: price a refill and get a 4-character confirmation code. Places NO order.
        Read the total and the code aloud. Checks the household's monthly spending limit."""
        return run(care.refill_quote, speaker, medication, days_supply)

    @apps.tool(resource_uri=uri("order"), name="refill_confirm", title="Confirm refill order", annotations=BUY)
    def refill_confirm(speaker: Speaker,
                       code: Annotated[str, Field(description="The 4-character code from refill_quote, as the user said it back")]) -> CallToolResult:
        """Step 2 of a purchase: place the refill order for a quote. Only call after the user repeats the code.
        Only members allowed to buy can confirm; expired or replaced quotes are refused; confirming the same
        code twice returns the same order (never double-orders)."""
        return run(care.refill_confirm, speaker, code)

    @apps.tool(resource_uri=uri("alerts"), name="alerts_list", title="List alerts", annotations=READ)
    def alerts_list(include_acknowledged: Annotated[bool, Field(description="Also show alerts already handled")] = False) -> CallToolResult:
        """Open alerts (red-flag check-ins, extra doses, low mood), most urgent first, with who was notified."""
        return run(care.alerts, include_acknowledged)

    @apps.tool(resource_uri=uri("report"), name="care_report", title="Weekly care report", annotations=READ)
    def care_report(days: Annotated[int, Field(ge=1, le=31, description="Number of days to look back")] = 7) -> CallToolResult:
        """The family's measurable week, computed only from logged events: adherence, double doses prevented,
        alerts and median minutes to acknowledge, refills ordered before running out, notes shared, and who is
        carrying the dose-logging load (with a gentle suggestion when one person does most of it)."""
        return run(care.care_report, days)

    for view in VIEWS:
        apps.add_html_resource(uri(view), card_html(view), title=f"CareLoop {view} card",
                               description=f"Screen card for the {view} view (Echo Show and other MCP Apps hosts)")

    mcp = MCPServer("careloop", title="CareLoop", version=__version__, instructions=INSTRUCTIONS,
                    description="Family care coordination for Alexa+: meds, refills, appointments, check-ins.",
                    extensions=[apps])

    # -------------------------------------------------------------- plain tools
    @mcp.tool(title="Household overview", annotations=READ)
    def household_overview() -> CallToolResult:
        """Who is in the household, who is cared for, caregivers, and the medication list."""
        return run(care.household)

    @mcp.tool(title="Log a dose", annotations=ADD)
    def dose_log(speaker: Speaker, medication: Medication,
                 status: Annotated[Literal["taken", "skipped"], Field(description="Whether the dose was taken or skipped")] = "taken",
                 at: Annotated[str | None, Field(description="When it was taken, 'HH:MM' today or ISO time. Default: now")] = None,
                 confirm_extra_dose: Annotated[bool, Field(description="Only true if the user explicitly confirmed a second dose really was taken after a double-dose warning")] = False) -> CallToolResult:
        """Record that a medication was taken or skipped. Has a double-dose guard: if the same medication was
        logged within its minimum interval, nothing is logged and the user is warned. Also warns when supply runs low."""
        return run(care.dose_log, speaker, medication, status, at, confirm_extra_dose)

    @mcp.tool(title="Add medication", annotations=ADD)
    def med_add(speaker: Speaker, name: Annotated[str, Field(min_length=1, description="Medication name, e.g. 'Lisinopril'")],
                times: Annotated[list[str], Field(description="Times of day in 24h format, e.g. ['08:00', '20:00']")],
                strength: Annotated[str, Field(description="e.g. '10 mg'")] = "",
                instructions: Annotated[str, Field(description="As written by the prescriber, e.g. 'with food'")] = "",
                aliases: Annotated[list[str] | None, Field(description="Everyday names, e.g. ['blood pressure pill']")] = None,
                pills_on_hand: Annotated[int, Field(ge=0, description="Tablets currently in the bottle")] = 30,
                pills_per_dose: Annotated[int, Field(ge=1, le=10, description="Tablets per dose")] = 1,
                min_interval_hours: Annotated[float, Field(ge=0, le=48, description="Minimum hours between doses, for the double-dose guard")] = 4) -> CallToolResult:
        """Add a medication to the schedule exactly as prescribed. CareLoop never suggests doses."""
        return run(care.med_add, speaker, name, times, strength, instructions, aliases, pills_on_hand, pills_per_dose, min_interval_hours)

    @mcp.tool(title="Refill status", annotations=READ)
    def refill_status() -> CallToolResult:
        """Days of supply left for each medication, the run-out date, and which need a refill."""
        return run(care.refill_status)

    @mcp.tool(title="Leave a handoff note", annotations=ADD)
    def note_add(speaker: Speaker, text: Annotated[str, Field(min_length=1, description="The note, in the speaker's words")]) -> CallToolResult:
        """Leave a note for the other caregivers ('Tell Omar Mom skipped lunch'). They hear it in their next update."""
        return run(care.note_add, speaker, text)

    @mcp.tool(title="What's new", annotations=UPDATE)
    def whats_new(speaker: Speaker) -> CallToolResult:
        """Everything other people did since this speaker last checked: notes, doses logged, orders, alerts.
        Remembers per person across sessions."""
        return run(care.whats_new, speaker)

    @mcp.tool(title="Check-in", annotations=ADD)
    def checkin(speaker: Speaker, text: Annotated[str, Field(description="How the person is feeling, in their words")],
                mood: Annotated[int | None, Field(ge=1, le=5, description="Optional mood, 1 very low to 5 great")] = None) -> CallToolResult:
        """Record how the cared-for person is doing. Red-flag words (a fall, chest pain, dizziness...) alert all
        caregivers immediately and the reply points to emergency services."""
        return run(care.checkin, speaker, text, mood)

    @mcp.tool(title="Acknowledge alert", annotations=UPDATE)
    def alert_ack(speaker: Speaker, alert_id: Annotated[str, Field(description="Alert id from alerts_list or care_briefing")]) -> CallToolResult:
        """Mark an alert as handled so the other caregivers know someone is on it."""
        return run(care.alert_ack, speaker, alert_id)

    @mcp.tool(title="Add appointment", annotations=ADD)
    def appointment_add(speaker: Speaker, title: Annotated[str, Field(min_length=1, description="e.g. 'Cardiology check-up'")],
                        when: Annotated[str, Field(description="ISO date-time in the household time zone, e.g. 2026-10-24T10:30")],
                        where: Annotated[str, Field(description="Place")] = "",
                        driver: Annotated[str | None, Field(description="Household member who will drive")] = None,
                        notes: Annotated[str, Field(description="Anything to bring or remember")] = "") -> CallToolResult:
        """Add an appointment for the cared-for person, optionally with who is driving."""
        return run(care.appointment_add, speaker, title, when, where, driver, notes)

    @mcp.tool(title="Agenda", annotations=READ)
    def agenda(day: Annotated[str, Field(description="'today', 'tomorrow', 'week' or a date like 2026-10-24")] = "today") -> CallToolResult:
        """Appointments for a day or the next 7 days, with who is driving."""
        return run(care.agenda, day)

    # -------------------------------------------------------------- resources & prompts
    @mcp.resource("careloop://household", mime_type="application/json", title="Household")
    def household_resource() -> str:
        """Members, roles and medication list."""
        return json.dumps(care.household(), default=str)

    @mcp.resource("careloop://today", mime_type="application/json", title="Today's schedule")
    def today_resource() -> str:
        """Today's dose schedule with live states (taken, due, missed, upcoming)."""
        return json.dumps(care.meds_due(), default=str)

    @mcp.prompt(title="Morning briefing")
    def morning_briefing(speaker: str) -> str:
        """Start a caregiver's day."""
        return (f"{speaker} just said good morning. Call care_briefing with speaker='{speaker}', say its speech, "
                "then offer the single most useful next step (log a dose, get a refill quote, or acknowledge an alert).")

    @mcp.prompt(title="End-of-day handoff")
    def end_of_day_handoff(speaker: str) -> str:
        """Hand over to the next caregiver."""
        return (f"Help {speaker} hand over for the night. Call meds_due to find anything not logged, ask about it, "
                f"then ask what the next person should know and save it with note_add (speaker='{speaker}').")

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "name": "careloop", "version": __version__})

    return mcp


def load(path: str | None = None, clock: Clock | None = None) -> CareLoop:
    store = Store(path or os.environ.get("CARELOOP_DATA", "data/careloop.json"))
    tz = store.data["household"].get("timezone") or "UTC"
    return CareLoop(store, clock or Clock(tz))


def demo_household(path: str | None = None) -> CareLoop:
    import json
    import tempfile
    from datetime import datetime

    from . import seed

    clock = Clock(seed.TZ, frozen=os.environ.get("CARELOOP_NOW") or "")
    start = clock.now() if clock._frozen else datetime.now(clock.tz).replace(hour=8, minute=5, second=0, microsecond=0)
    target = Path(path) if path else Path(tempfile.mkdtemp(prefix="careloop-demo-")) / "careloop.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(seed.build(start), indent=2), encoding="utf-8")
    if not clock._frozen:
        clock.start_at(start)
    return CareLoop(Store(target), clock)


class BearerGuard:
    """ASGI middleware: require a static bearer token on the MCP endpoint (health check stays open)."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            auth = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(auth, b"Bearer " + self.token):
                await JSONResponse({"error": "unauthorized"}, status_code=401,
                                   headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
                return
        await self.app(scope, receive, send)


def http_app(care: CareLoop | None = None, host: str = "127.0.0.1"):
    from mcp.server.transport_security import TransportSecuritySettings

    mcp = build_server(care or load())
    extra = [h.strip() for h in os.environ.get("CARELOOP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    security = None
    if extra:
        security = TransportSecuritySettings(allowed_hosts=["127.0.0.1:*", "localhost:*", *extra],
                                             allowed_origins=["http://127.0.0.1:*", "http://localhost:*",
                                                              *[f"https://{h}" for h in extra]])
    app = mcp.streamable_http_app(transport_security=security, host=host)
    token = os.environ.get("CARELOOP_TOKEN")
    return BearerGuard(app, token) if token else app


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="careloop", description="CareLoop MCP server")
    p.add_argument("--stdio", action="store_true", help="serve over stdio instead of Streamable HTTP")
    p.add_argument("--host", default=os.environ.get("CARELOOP_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CARELOOP_PORT", "8765")))
    p.add_argument("--data", default=None, help="household JSON file (default $CARELOOP_DATA or data/careloop.json)")
    p.add_argument("--demo", action="store_true",
                   help="start a fresh demo household (the Haddad family) at 8:05 AM today, in a temp file")
    a = p.parse_args(argv)
    if a.demo:
        care = demo_household(a.data)
        print(f"Demo household: {care.store.path} (clock starts at {care.now():%H:%M} {care.clock.tz})", file=sys.stderr)
    else:
        care = load(a.data)
    if a.stdio:
        build_server(care).run("stdio")
        return
    import uvicorn

    print(f"CareLoop MCP server on http://{a.host}:{a.port}/mcp", file=sys.stderr)
    uvicorn.run(http_app(care, a.host), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
