# HappyRobot Demo — Full Build Guide

Complete documentation of how this system was built and why each decision was made. Detailed enough to rebuild it from scratch.

---

## What this system does

A voice AI agent ("Alex") that:
1. Answers inbound calls from freight carriers
2. Looks up a load in Google Sheets
3. Negotiates the rate (accept / counter / decline) over multiple rounds
4. Logs the outcome back to Google Sheets

**The call flow in one sentence:** Vapi (voice AI) → n8n (orchestration) → Python Flask service (logic) → Google Sheets (data).

---

## Architecture

```
Phone call
    │
    ▼
 VAPI
 "Alex - Apex Freight"
 (voice agent, tool calls)
    │
    │  HTTP POST (tool call webhook)
    ▼
 N8N (localhost:5678, exposed via ngrok)
 Two workflows:
   - get_load          → fetch load details from Sheets
   - evaluate_offer    → negotiate rate + log outcome
    │
    │  HTTP POST
    ▼
 PYTHON-SERVICE (localhost:5001)
 Flask app, four endpoints:
   /parse-input        → normalise Vapi envelope
   /evaluate           → negotiation logic
   /format-get-load    → format response for Vapi
   /format-evaluate    → format response for Vapi
    │
    │  Google Sheets API (OAuth)
    ▼
 GOOGLE SHEETS
 Sheet: "Loads"
 Columns: load_id, origin, destination, target_rate, floor_rate,
          status, agreed_rate, carrier_name
```

Both n8n and python-service run as Docker containers on the same Docker network (`happyrobot`). This means n8n can reach the Python service using the hostname `python-service` instead of an IP address.

---

## Part 1 — Docker Setup

### Why Docker?

Docker lets you run n8n and the Python service as isolated, reproducible environments. Nothing is installed on the host machine directly — n8n and the Python service each live in their own container with their own dependencies.

### The Docker network

```bash
docker network create happyrobot
```

This creates a private network that containers can join. Once both containers are on `happyrobot`, they can talk to each other using their container names as hostnames. So n8n calls `http://python-service:5001/...` and Docker resolves that automatically.

### The Python service container

**File: `python-service/Dockerfile`**
```dockerfile
FROM python:3.12-alpine       # start from a minimal Python image
WORKDIR /app                  # all commands run from /app inside the container
COPY requirements.txt .       # copy dependency list first (Docker caching trick)
RUN pip install --no-cache-dir -r requirements.txt   # install Flask
COPY app.py .                 # copy the application code
EXPOSE 5001                   # document which port the app uses
CMD ["python", "app.py"]      # command that starts the service
```

**Why copy requirements.txt before app.py?** Docker builds in layers. If app.py changes but requirements.txt doesn't, Docker reuses the cached pip install layer and only rebuilds the last step. This makes rebuilds much faster.

**File: `python-service/requirements.txt`**
```
flask==3.1.1
```

Only one dependency: Flask, the web framework that lets Python receive HTTP requests.

**Starting the container:**
```bash
docker build -t python-service ./python-service
docker run -d --name python-service --network happyrobot -p 5001:5001 python-service
```

- `-d` — run in the background (detached)
- `--name python-service` — gives the container this hostname on the Docker network
- `--network happyrobot` — joins the shared network
- `-p 5001:5001` — maps port 5001 on the host machine to port 5001 inside the container

### The n8n container

n8n's official Docker image has its package manager (`apk`) removed for security. Adding Python into it is not straightforward. So all Python logic lives in the separate `python-service` container, and n8n calls it over HTTP.

**Starting n8n:**
```bash
docker run -d \
  --name n8n \
  --network happyrobot \
  -p 5678:5678 \
  -e N8N_EDITOR_BASE_URL=http://localhost:5678 \
  -e WEBHOOK_URL=https://<your-ngrok-domain> \
  -e N8N_API_DISABLED=false \
  -v n8n_data:/home/node/.n8n \
  n8nio/n8n:2.27.4
```

Key environment variables:
- `N8N_EDITOR_BASE_URL` — the URL n8n uses for its own UI and OAuth callbacks. Must be localhost so Google OAuth doesn't get routed through ngrok.
- `WEBHOOK_URL` — the public URL n8n uses for inbound webhooks (what Vapi calls). Must be the ngrok tunnel so Vapi can reach it from the internet.
- `N8N_API_DISABLED=false` — enables n8n's REST API so workflows can be configured programmatically.
- `-v n8n_data:/home/node/.n8n` — persists n8n data (workflows, credentials) in a Docker volume so it survives container restarts.

**Why two different URLs?** This took a while to figure out. If you set `WEBHOOK_URL` to the ngrok address, n8n also uses that address for Google OAuth callbacks. ngrok's free tier shows an interstitial "are you sure you want to visit this page?" HTML page before forwarding traffic — this breaks the OAuth handshake completely. Separating the two variables fixes it: OAuth stays on localhost, webhooks use ngrok.

---

## Part 2 — ngrok Tunnel

Vapi runs in the cloud; n8n runs locally. ngrok creates a tunnel so Vapi can reach the local n8n instance via a public URL.

```bash
ngrok http --domain=<your-ngrok-domain> 5678
```

- `--domain=...` — uses the static domain assigned to the free account (so the URL doesn't change every restart)
- `5678` — forwards traffic to port 5678 on the host machine, where n8n is listening

**Critical gotcha:** The ngrok dashboard shows a "credential ID" (`cr_...`) on the token detail page. This is NOT the auth token. The actual token is only shown once, when you first create it. If you navigate away, you must delete and recreate it.

**Auth token setup:**
```bash
ngrok config add-authtoken <your-actual-token>
```

---

## Part 3 — Google Sheets

The sheet acts as the load database (TMS), with these columns:

| load_id | origin | destination | target_rate | floor_rate | status | agreed_rate | carrier_name |
|---|---|---|---|---|---|---|---|
| MUC-HH-001 | Munich | Hamburg | 1150 | 1300 | open | | |

- `target_rate` — the rate the broker wants to pay (1150 EUR). Alex counters to this rate if the carrier asks for more.
- `floor_rate` — the absolute maximum the broker will pay (1300 EUR). Above this, the agent declines.
- `status` — starts as `open`, becomes `negotiating`, `booked`, or `declined`.
- `agreed_rate` and `carrier_name` — filled in when the call concludes.

### Connecting Google Sheets to n8n

1. Create a Google Cloud project
2. Enable the **Google Sheets API** and **Google Drive API**
3. Create OAuth 2.0 credentials (type: Web application)
4. Add `http://localhost:5678/rest/oauth2-credential/callback` as an authorized redirect URI
5. In n8n, add a Google Sheets credential using OAuth2 and authenticate with your Google account

Why Drive API? n8n uses the Drive API to list spreadsheets when you're setting up a node. Sheets API alone isn't enough.

---

## Part 4 — How n8n Works

n8n is a visual workflow tool. A **workflow** is a chain of **nodes**, where each node does one thing (receive a webhook, call an HTTP endpoint, read a spreadsheet, etc.). Data flows from left to right — each node receives the output of the previous node as its input.

### The node types used

**Webhook node** — the entry point of a workflow. It creates a URL that, when called via HTTP POST, starts the workflow. The incoming request body becomes the first item of data in the flow.

**HTTP Request node** — sends an HTTP request to any URL and passes the response forward. This is how n8n calls the Python service. Key settings:
- `Method: POST`
- `URL: http://python-service:5001/endpoint-name`
- `Content Type: JSON` — sends the body as `application/json`
- `Specify Body: Using JSON` — lets you write a JSON body with n8n expressions in it

**Google Sheets node** — reads from or writes to a Google Sheet. Two operations used here:
- `Read Rows` — fetches all rows matching a filter (filtered by `load_id`)
- `Update Row` — updates specific columns in a row matching a key (matched on `load_id`)

### n8n expressions

Inside any node, you can use `={{ ... }}` to write JavaScript expressions that reference data from other nodes. The main pattern:

```
{{ $json.field_name }}                          — current node's input data
{{ $("Node Name").first().json.field_name }}    — data from any named node
```

`$("Node Name")` returns all items that came out of that node. `.first()` gets the first item. `.json` gets the data object. Then you access a field by name.

This is essential because data from earlier nodes gets replaced as you move through the workflow. If the "Evaluate Offer" node runs and then a "Google Sheets Update" node runs, `$json` is now the Sheets row data — not the evaluation result. To get the evaluation result, you use `$("Evaluate Offer").first().json.decision`.

### Workflow data format

n8n passes data between nodes as an array of **items**. Each item looks like:
```json
{ "json": { "field": "value", ... } }
```

When an HTTP Request node gets a response, the response body becomes `$json` for the next node. When a Google Sheets "Read" node finds rows, each row becomes one item.

---

## Part 5 — The `get_load` Workflow

**Purpose:** When Vapi calls this webhook, fetch the load details from Google Sheets and return them to Vapi.

**Trigger:** Vapi calls `POST https://<your-ngrok-domain>/webhook/get-load` when the carrier asks about a load.

### Nodes (left to right)

#### 1. Webhook
- **Type:** Webhook
- **Path:** `/get-load`
- **Method:** POST
- **Response Mode:** "Using Respond to Webhook Node" — means n8n waits until the last node runs and then sends its output as the HTTP response

Vapi sends a payload like:
```json
{
  "message": {
    "toolCallList": [{
      "id": "call_abc123",
      "function": {
        "name": "get_load",
        "arguments": { "load_id": "MUC-HH-001", "carrier_name": "Fast Freight GmbH" }
      }
    }]
  }
}
```

#### 2. Parse Input (HTTP Request → python-service)
- **URL:** `http://python-service:5001/parse-input`
- **Body:** `={{ JSON.stringify($json) }}`

Why: The Vapi envelope is complex (nested `message.toolCallList[0].function.arguments`). This Python endpoint normalises it into a flat, clean object:
```json
{
  "toolCallId": "call_abc123",
  "load_id": "MUC-HH-001",
  "carrier_name": "Fast Freight GmbH"
}
```

#### 3. Get Load Details (Google Sheets — Read Rows)
- **Operation:** Read Rows
- **Sheet:** Loads
- **Filter:** `load_id` equals `={{ $json.load_id }}`

Fetches the matching row from Google Sheets. Output looks like:
```json
{
  "load_id": "MUC-HH-001",
  "origin": "Munich",
  "destination": "Hamburg",
  "target_rate": 1150,
  "floor_rate": 1300,
  "status": "open"
}
```

Note: `$json.load_id` references the output of the previous node (Parse Input).

#### 4. Format Response (HTTP Request → python-service)
- **URL:** `http://python-service:5001/format-get-load`
- **Body:**
```
={{ JSON.stringify(Object.assign({}, $json, { toolCallId: $("Parse Input").first().json.toolCallId })) }}
```

Why: The current `$json` is the Sheets row, but `toolCallId` from the Parse Input step is also needed. `Object.assign({}, $json, {...})` merges two objects together.

The Python endpoint wraps everything in the Vapi response format:
```json
{
  "results": [{
    "toolCallId": "call_abc123",
    "result": "{\"load_id\": \"MUC-HH-001\", \"origin\": \"Munich\", ...}"
  }]
}
```

Vapi requires this exact format — `results` array where each item has a `toolCallId` matching the original call and a `result` string.

#### 5. Respond to Webhook
- Sends the Format Response output back to Vapi as the HTTP response

---

## Part 6 — The `evaluate_offer` Workflow

**Purpose:** When the carrier names a price, evaluate it against target/floor rates, decide accept/counter/decline, log the outcome to Sheets, and return the decision to Vapi.

**Trigger:** Vapi calls `POST https://<your-ngrok-domain>/webhook/evaluate-offer`

Vapi sends:
```json
{
  "message": {
    "toolCallList": [{
      "id": "call_xyz789",
      "function": {
        "name": "evaluate_offer",
        "arguments": {
          "load_id": "MUC-HH-001",
          "offered_rate": 1400,
          "carrier_name": "Fast Freight GmbH",
          "round": 1
        }
      }
    }]
  }
}
```

### Nodes (left to right)

#### 1. Webhook
- Same as get_load — entry point for Vapi calls

#### 2. Parse Input (HTTP Request → python-service)
- **URL:** `http://python-service:5001/parse-input`
- **Body:** `={{ JSON.stringify($json) }}`

Output:
```json
{
  "toolCallId": "call_xyz789",
  "load_id": "MUC-HH-001",
  "offered_rate": 1400,
  "carrier_name": "Fast Freight GmbH",
  "round": 1
}
```

#### 3. Get Load Details (Google Sheets — Read Rows)
- **Filter:** `load_id` equals `={{ $json.load_id }}`

Fetches `target_rate`, `floor_rate`, and `row_number` (the row index, needed to update the right row later).

#### 4. Evaluate Offer (HTTP Request → python-service)
- **URL:** `http://python-service:5001/evaluate`
- **Body:**
```
={{ JSON.stringify(Object.assign({}, $json, {
  offered_rate: $("Parse Input").first().json.offered_rate,
  carrier_name: $("Parse Input").first().json.carrier_name,
  round:        $("Parse Input").first().json.round,
  toolCallId:   $("Parse Input").first().json.toolCallId
})) }}
```

Why the merge: `$json` here is the Sheets row (has `target_rate`, `floor_rate`). The fields from Parse Input (`offered_rate`, `round`, `toolCallId`) get merged in. The Python endpoint needs all of them together to make a decision.

Output:
```json
{
  "decision": "counter",
  "message": "I understand, but the best I can do is 1150 EUR. Can you work with that?",
  "agreed_rate": "",
  "status": "negotiating",
  "load_id": "MUC-HH-001",
  "carrier_name": "Fast Freight GmbH",
  "toolCallId": "call_xyz789"
}
```

#### 5. Update Sheet (Google Sheets — Update Row)
- **Operation:** Update Row
- **Matching Columns:** `load_id` — this tells n8n which row to find
- **Columns to update:** `status`, `agreed_rate`, `carrier_name`
- Values come from `$json` (the Evaluate Offer output)

This logs the current state of the negotiation to the sheet.

**Critical gotcha:** After this node runs, `$json` becomes the Sheets row data again — not the evaluation result. The `decision` and `message` fields are gone. This is why the next node must reference the Evaluate Offer node directly.

#### 6. Format Response (HTTP Request → python-service)
- **URL:** `http://python-service:5001/format-evaluate`
- **Body:**
```
={{ JSON.stringify({
  decision:    $("Evaluate Offer").first().json.decision,
  message:     $("Evaluate Offer").first().json.message,
  agreed_rate: $("Evaluate Offer").first().json.agreed_rate,
  toolCallId:  $("Evaluate Offer").first().json.toolCallId
}) }}
```

References "Evaluate Offer" node directly to recover the fields that were overwritten.

Output (Vapi format):
```json
{
  "results": [{
    "toolCallId": "call_xyz789",
    "result": "{\"decision\": \"counter\", \"message\": \"I understand...\", \"agreed_rate\": \"\"}"
  }]
}
```

#### 7. Respond to Webhook
- Sends the formatted response back to Vapi

---

## Part 7 — The Python Flask Service

Flask is a Python web framework. It lets you define HTTP endpoints with simple decorator syntax. The service runs on port 5001 and has four endpoints.

### Why a separate service?

n8n's built-in Code node does support Python, but it's marked as "internal mode — for debugging only" and requires an external virtualenv to be pre-installed at a specific hardcoded path. In the standard n8n Docker image, this doesn't exist. Rather than fight this, all logic was extracted into a separate Flask service. This is also arguably better architecture: the logic is testable with plain curl, independent of n8n, and written in clean Python.

### Endpoint 1: `/parse-input`

**Purpose:** Normalize the incoming request. Vapi wraps tool call arguments in a complex envelope. Direct curl tests send flat JSON. This endpoint handles both.

```python
@app.route("/parse-input", methods=["POST"])
def parse_input():
    data = request.get_json()
    body = data.get("body", data)   # n8n wraps in {body: ...}; curl sends flat

    msg = body.get("message", {})
    tool_calls = msg.get("toolCallList", [])

    if tool_calls:
        tool_call    = tool_calls[0]
        tool_call_id = tool_call["id"]
        raw_args     = tool_call["function"]["arguments"]
        # Critical: Vapi sends arguments as a dict, not a JSON string
        args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args)
    else:
        tool_call_id = None
        args = body

    return jsonify({
        "toolCallId":   tool_call_id,
        "load_id":      args.get("load_id"),
        "offered_rate": float(args["offered_rate"]) if "offered_rate" in args else None,
        "carrier_name": args.get("carrier_name", "Unknown Carrier"),
        "round":        int(args.get("round", 1)),
    })
```

**The `isinstance` check:** Vapi's documentation suggests `arguments` would be a JSON string. In practice, Vapi sends it as an already-parsed Python dict. Calling `json.loads()` on a dict crashes with `TypeError`. The fix: check the type first.

### Endpoint 2: `/evaluate`

**Purpose:** Pure negotiation logic. Takes `offered_rate`, `target_rate`, `floor_rate`, and `round` — returns a decision.

```python
def _negotiate(offered_rate, target_rate, floor_rate, round_number):
    if offered_rate <= target_rate:
        # Great deal — accept immediately
        return {"decision": "accept", "agreed_rate": offered_rate, "status": "booked",
                "message": f"Perfect, we have a deal at {int(offered_rate)} EUR. I will send confirmation shortly."}

    if offered_rate <= floor_rate:
        if round_number >= 2:
            # Within budget and already countered once — close the deal
            return {"decision": "accept", "agreed_rate": offered_rate, "status": "booked",
                    "message": f"Alright, we will do {int(offered_rate)} EUR. I will get the paperwork to you now."}
        else:
            # First offer, within budget but above target — push back to target
            return {"decision": "counter", "agreed_rate": None, "status": "negotiating",
                    "message": f"I understand, but the best I can do on this lane is {int(target_rate)} EUR. Can you work with that?"}

    # Carrier is above the absolute ceiling
    if round_number < 2:
        return {"decision": "counter", "agreed_rate": None, "status": "negotiating",
                "message": f"I hear you, but our rate for this load is {int(target_rate)} EUR. That is the best we can offer on this lane."}
    else:
        # Two rounds, still too expensive — decline
        return {"decision": "decline", "agreed_rate": None, "status": "declined",
                "message": "I am sorry, we simply cannot go higher on this load. I hope we can work together on a future shipment. Have a good day."}
```

**Three zones:**
1. `offered <= target` → accept (great deal)
2. `target < offered <= floor` → counter round 1, accept round 2 (within budget)
3. `offered > floor` → counter round 1, decline round 2 (above budget)

The `round` parameter tracks the negotiation rounds. Alex passes `round=1` on the first offer, `round=2` when the carrier responds to the counter.

### Endpoint 3: `/format-get-load`

**Purpose:** Wrap load data in Vapi's required response format.

Vapi requires tool call responses in this exact shape:
```json
{
  "results": [
    {
      "toolCallId": "call_abc123",
      "result": "<JSON string of the actual data>"
    }
  ]
}
```

Note that `result` must be a **string** (a JSON-encoded string of the data), not an object. Hence the `json.dumps()` on the data.

```python
@app.route("/format-get-load", methods=["POST"])
def format_get_load():
    data = request.get_json()
    tool_call_id = data.get("toolCallId")
    load_data = {k: v for k, v in data.items() if k not in ("toolCallId", "row_number")}

    if tool_call_id:
        return jsonify({"results": [{"toolCallId": tool_call_id, "result": json.dumps(load_data)}]})
    return jsonify(load_data)   # direct curl test — return flat
```

### Endpoint 4: `/format-evaluate`

**Purpose:** Same pattern, wraps the negotiation result for Vapi.

```python
@app.route("/format-evaluate", methods=["POST"])
def format_evaluate():
    data = request.get_json()
    tool_call_id = data.get("toolCallId")
    result = {
        "decision":    data["decision"],
        "message":     data["message"],
        "agreed_rate": data["agreed_rate"],
    }
    if tool_call_id:
        return jsonify({"results": [{"toolCallId": tool_call_id, "result": json.dumps(result)}]})
    return jsonify(result)
```

---

## Part 8 — Vapi Assistant

Vapi is the voice AI platform. The assistant "Alex" is configured with:

### System prompt (key parts)

```
You are Alex, a freight broker at Apex Freight (a fictional brokerage). You speak to carriers who call in about loads.

When a carrier calls:
1. Greet them and ask which load they're calling about and their company name.
2. Call get_load with the load_id and carrier_name to fetch load details.
3. Share the load details (origin, destination) but DO NOT reveal the rate yet.
4. Ask what rate the carrier is looking for.
5. When the carrier names a rate, call evaluate_offer with:
   - load_id
   - offered_rate (the number the carrier said)
   - carrier_name
   - round=1
6. Speak the message from the response. Do not add to it or summarize it.
7. When the carrier responds to your counter — whether they name a new rate OR accept your counter — call evaluate_offer again with round=2.
   If they accepted your counter, use your counter rate as offered_rate.
8. If decision=accept, confirm booking and end the call.
9. If decision=decline, wish them well and end the call.
```

**Why rule 7 is important:** This rule exists because of a bug. Without it, when the carrier said "ok, 1150 works," Alex verbally confirmed the booking but never called `evaluate_offer` with round=2. The sheet stayed at "negotiating." The explicit instruction to "use your counter rate as offered_rate if they accepted" fixed this.

### Tools

Two tools configured in Vapi:

**`get_load`**
- Webhook URL: `https://<your-ngrok-domain>/webhook/get-load`
- Parameters: `load_id` (string), `carrier_name` (string)

**`evaluate_offer`**
- Webhook URL: `https://<your-ngrok-domain>/webhook/evaluate-offer`
- Parameters: `load_id` (string), `offered_rate` (number), `carrier_name` (string), `round` (number)

---

## Part 9 — Startup Sequence

To run the demo from scratch:

**Terminal 1 — ngrok:**
```bash
ngrok http --domain=<your-ngrok-domain> 5678
```
Leave this running.

**Terminal 2 — Docker containers:**
```bash
# Start python-service (if not already running)
docker start python-service

# Start n8n (if not already running)
docker start n8n
```

If containers don't exist yet (first time):
```bash
docker network create happyrobot

docker build -t python-service ./python-service
docker run -d --name python-service --network happyrobot -p 5001:5001 python-service

docker run -d \
  --name n8n \
  --network happyrobot \
  -p 5678:5678 \
  -e N8N_EDITOR_BASE_URL=http://localhost:5678 \
  -e WEBHOOK_URL=https://<your-ngrok-domain> \
  -e N8N_API_DISABLED=false \
  -v n8n_data:/home/node/.n8n \
  n8nio/n8n:2.27.4
```

**Verify:**
```bash
# Check python-service is running
curl -X POST http://localhost:5001/parse-input \
  -H "Content-Type: application/json" \
  -d '{"load_id": "MUC-HH-001", "offered_rate": 1200, "carrier_name": "Test"}'

# n8n UI
open http://localhost:5678
```

---

## Part 10 — Hard Edges (What Broke and Why)

These are the non-obvious things that cost the most time during the build. Worth knowing before building something similar.

**1. ngrok token vs credential ID**
The ngrok dashboard shows a "credential ID" (`cr_...`) on the token detail page. That is NOT the auth token. The actual token is only shown once at creation time. If you leave the page, delete the token and create a new one.

**2. n8n OAuth callback URL broken by free ngrok tier**
Setting `WEBHOOK_URL` to the ngrok address also routes Google OAuth callbacks through ngrok's free-tier interstitial page, which breaks the handshake. Fix: separate `N8N_EDITOR_BASE_URL` (keep on localhost) from `WEBHOOK_URL` (ngrok). Google OAuth stays on localhost; Vapi webhooks use ngrok.

**3. Google Sheets AND Google Drive APIs must both be enabled**
Creating OAuth credentials in Google Cloud is not enough. You must explicitly enable both the Google Sheets API and the Google Drive API in your Cloud project. n8n uses Drive to list spreadsheets in its UI.

**4. n8n's hardened Docker image has no package manager**
`apk` is removed from `n8nio/n8n`. You cannot do `RUN apk add python3`. You can copy Python from another image (multi-stage Dockerfile), but n8n's Python Code node runner still won't work because it needs a pre-built virtualenv at an internal hardcoded path.

**5. n8n Python Code nodes are debug-only**
n8n's built-in Python support (enabled via `N8N_RUNNERS_ENABLED=true`) requires an external task runner process and a specific virtualenv. It is not production-ready in the standard Docker image. Solution: separate Python Flask service, called via HTTP Request nodes.

**6. Vapi sends tool `arguments` as a dict, not a JSON string**
API documentation implies `arguments` is a JSON-encoded string. In practice, Vapi sends it as an already-parsed dictionary. Calling `json.loads()` on it crashes with `TypeError`. Fix: `args = raw if isinstance(raw, dict) else json.loads(raw)`.

**7. After a Google Sheets update node, previous data is gone**
When the Google Sheets Update node runs, `$json` becomes the row data from Sheets. Any fields from earlier nodes (like `decision`, `message` from the evaluate step) are no longer in `$json`. Fix: reference earlier nodes explicitly with `$("Evaluate Offer").first().json.decision`.

**8. Agent verbal confirmation ≠ tool call**
When the carrier said "ok, 1150 works," the Vapi agent verbally confirmed booking but did not call `evaluate_offer` with round=2. The system prompt only said "call evaluate_offer when the carrier names a rate." Accepting a counter doesn't match "names a rate." Fix: explicitly instruct the agent to call evaluate_offer when the carrier accepts a counter, using the counter rate as the offered_rate.

---

## How to Replicate This Pattern

This architecture — webhook trigger → parse input → fetch data → run logic → log result → return formatted response — applies to almost any "AI agent calls a backend" scenario. The pattern in steps:

1. **Webhook node** — entry point, receives the tool call from the AI platform
2. **Parse node** — normalise the AI platform's envelope into your own clean format
3. **Data fetch** — get whatever context you need (from a DB, sheet, API)
4. **Logic node** — run your business rules (in Python, via HTTP Request to a Flask service)
5. **Log result** — write the outcome back to your data store
6. **Format node** — wrap the result in whatever format the AI platform expects
7. **Respond to Webhook** — send the formatted response back

The split between n8n (orchestration, I/O) and Python (logic) keeps each part simple and testable independently.
