# Security, privacy and safety

CareLoop handles a family's medication schedule, so the defaults are conservative. This file describes what the code does today.

## Data and privacy
- **Self-hosted, single household.** All state is one JSON file (`CARELOOP_DATA`) that the family controls. Writes are atomic (temp file plus rename).
- **No outbound calls.** No analytics, no telemetry, no third-party APIs. The pharmacy is a local simulator behind the `Pharmacy` interface, and every order it returns carries `simulated: true`.
- **Data minimisation.** CareLoop stores names, medication schedules, dose logs, notes, check-ins, alerts and appointments. It does not store diagnoses, insurance details or payment cards.
- **Audit log.** Every write records who did it and when, along with blocked double doses and purchase steps. The log keeps the last 500 entries.

## Transport and access
- **Streamable HTTP** binds to `127.0.0.1` by default.
- **DNS-rebinding protection:** Host and Origin headers are validated. Use `CARELOOP_ALLOWED_HOSTS` to add a public hostname.
- **Bearer token:** set `CARELOOP_TOKEN` to require `Authorization: Bearer …` on `/mcp`. Tokens are compared in constant time. `/healthz` stays open.
- When exposing CareLoop beyond the home network, put it behind TLS (a reverse proxy) and set a token.

## Safety rules (enforced in code, covered by tests)
| Risk | Control |
|---|---|
| Giving a dose twice | Per-medication minimum interval. A second log is refused, says who logged the dose and when, and needs an explicit `confirm_extra_dose`. A confirmed extra dose alerts all caregivers. |
| Accidental or unauthorised purchase | Two-step quote and spoken code. Only members with `can_purchase` can confirm. Quotes expire after 10 minutes, a medication has only one live quote, a monthly spending cap applies, and confirming twice returns the same order. Purchases cannot be made by touch. |
| Emergency mentioned in a check-in | Red-flag words raise an urgent alert to every caregiver, and the reply points to the local emergency number. |
| Medical advice | CareLoop never suggests doses or changes. It only records what the prescriber scheduled. |
| A model ignoring instructions | All of the above live in `core.py`, not in prompts. Tools declare all four MCP behaviour hints so hosts can gate writes and purchases. |

## Not in scope
CareLoop is not a medical device and is not an emergency dispatch service.

## Reporting a vulnerability
Please open a GitHub security advisory on this repository (Security → Report a vulnerability) rather than a public issue.
