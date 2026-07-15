# Voice Agent for Inbound Carrier Sales

A working voice AI agent that answers inbound calls from freight carriers, presents load details, negotiates the rate across multiple rounds, and logs the outcome. Built in about 4 hours to understand where the hard edges are when deploying voice agents against real business logic.

Inspired by the inbound carrier sales use case pioneered by [HappyRobot](https://www.happyrobot.ai). This is an independent learning rebuild, not affiliated with any company.

## What it does

A carrier calls in about a posted load. The agent ("Alex") greets them, looks up the load, shares origin and destination, and asks what rate they're looking for. Then it negotiates:

- Offer at or below the target rate → accept immediately
- Offer between target and floor → counter once, accept on round 2
- Offer above the floor → counter once, decline politely on round 2

The agent never reveals the floor rate and never exceeds it. Every outcome (booked, declined, negotiating) is written back to the load board with the agreed rate and carrier name.

## Architecture

```
Phone call
    ↓
Vapi ─ voice agent, persona + tool calls
    ↓  HTTP webhook (via ngrok tunnel)
n8n ─ orchestration, two workflows:
    │    get_load        → fetch load details
    │    evaluate_offer  → negotiate + log outcome
    ↓  HTTP
Python Flask service ─ negotiation logic, 4 endpoints
    ↓
Google Sheets ─ plays the load board / TMS
```

The split matters: n8n handles orchestration and I/O, the Flask service holds the business logic. That keeps the logic curl-testable and independent of any workflow tool.

## Repo contents

| Path | What it is |
|---|---|
| `python-service/` | Flask negotiation service (Dockerized) |
| `vapi/system-prompt.md` | The voice agent's persona and negotiation rules |
| `n8n/` | Workflow structure and export notes |
| `docs/build-guide.md` | Full build walkthrough, every decision explained |

## What broke (the interesting part)

Eight non-obvious failures cost most of the build time. The full list is in [docs/build-guide.md](docs/build-guide.md), Part 10. Highlights:

1. **Verbal confirmation ≠ tool call.** The agent verbally confirmed a booking but never fired the round-2 tool call, so the backend stayed at "negotiating". Fixed with an explicit system prompt rule: accepting a counter counts as naming a rate.
2. **Vapi sends tool arguments as a dict, not a JSON string** — despite what the docs imply. `json.loads()` on a dict crashes.
3. **n8n's hardened Docker image can't run Python code nodes**, which forced the logic into a separate Flask container. Better architecture anyway.
4. **ngrok's free-tier interstitial page silently breaks Google OAuth** if you route n8n's editor URL through the tunnel. Editor URL and webhook URL must be separated.

## Running it

See [docs/build-guide.md](docs/build-guide.md) for the full setup (Docker network, ngrok tunnel, Google Sheets OAuth, Vapi tool registration). Quick check that the logic works, no voice stack needed:

```bash
docker build -t python-service ./python-service
docker run -d --name python-service -p 5001:5001 python-service

curl -X POST http://localhost:5001/evaluate \
  -H "Content-Type: application/json" \
  -d '{"offered_rate": 1400, "target_rate": 1150, "floor_rate": 1300, "round": 1}'
# → {"decision": "counter", "message": "I hear you, but our rate for this load is 1150 EUR..."}
```

## Stack

Vapi · n8n · Python (Flask) · Docker · Google Sheets · ngrok — built with Claude Code.
