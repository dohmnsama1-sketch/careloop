# Changelog

## 1.1.0 (2026-10-22)
- `care_report`: a measurable week computed from logged events. It covers adherence, double doses prevented, median minutes to acknowledge an alert, refills ordered before running out, notes shared and who carries the dose-logging load, with a gentle suggestion to share it. It has its own MCP Apps card.
- Touch actions on Echo Show cards through MCP Apps `tools/call`: "Mark given" on the dose carousel and "I'm on it" on alerts. Purchases stay voice-only.
- Accessibility: `aria-live` cards, labelled 44 px touch targets, `prefers-reduced-motion` and `prefers-contrast` support.
- `careloop --demo`: a fresh demo household whose clock runs in real time from 8:05 AM today.
- Handoff notes addressed to the listener are read naturally ("Sara left you a note: …").
- Tests went from 35 to 42, with source coverage at 93%. Tested on Python 3.11, 3.12 and 3.13.
- SECURITY.md and FRICTION_LOG.md.

## 1.0.0 (2026-10-22)
- First release: 16 tools, 5 MCP Apps cards, an Agent Skill, a double-dose guard, two-step voice purchasing, check-ins with escalation, per-person handoff memory, and support for both MCP 2025-11-25 and 2026-07-28.
