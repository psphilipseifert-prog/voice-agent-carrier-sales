"""
HappyRobot demo — Python negotiation service
Runs on port 5001. Called by n8n via HTTP Request nodes.

Four endpoints:
  /parse-input          extract params from Vapi envelope OR direct curl call
  /evaluate             negotiation logic — accept / counter / decline
  /format-get-load      wrap load data in Vapi result format
  /format-evaluate      wrap decision in Vapi result format
"""

import json
from flask import Flask, request, jsonify

app = Flask(__name__)


# ── 1. PARSE INPUT ────────────────────────────────────────────────────────────
# Vapi wraps every tool call in a message envelope.
# Direct curl tests just send the params flat.
# This endpoint normalises both into one shape.

@app.route("/parse-input", methods=["POST"])
def parse_input():
    data = request.get_json()
    body = data.get("body", data)   # n8n sends {body: ...}; curl sends flat

    msg = body.get("message", {})
    tool_calls = msg.get("toolCallList", [])

    if tool_calls:
        # Vapi format — arguments may be a dict or a JSON string depending on the model
        tool_call    = tool_calls[0]
        tool_call_id = tool_call["id"]
        raw_args     = tool_call["function"]["arguments"]
        args         = raw_args if isinstance(raw_args, dict) else json.loads(raw_args)
    else:
        # Direct curl format — params are at the top level
        tool_call_id = None
        args         = body

    return jsonify({
        "toolCallId":   tool_call_id,
        "load_id":      args.get("load_id"),
        "offered_rate": float(args["offered_rate"]) if "offered_rate" in args else None,
        "carrier_name": args.get("carrier_name", "Unknown Carrier"),
        "round":        int(args.get("round", 1)),
    })


# ── 2. EVALUATE OFFER ─────────────────────────────────────────────────────────
# Pure negotiation logic. No I/O — just maths and rules.
# n8n calls this after it has fetched target_rate and floor_rate from Google Sheets.

def _negotiate(offered_rate, target_rate, floor_rate, round_number):
    """
    Three-zone decision:
      offered <= target              → accept immediately (great deal)
      target < offered <= floor      → counter round 1, accept round 2
      offered > floor                → counter round 1, decline round 2
    """
    if offered_rate <= target_rate:
        return {
            "decision":    "accept",
            "agreed_rate": offered_rate,
            "status":      "booked",
            "message":     f"Perfect, we have a deal at {int(offered_rate)} EUR. "
                           f"I will send confirmation shortly.",
        }

    if offered_rate <= floor_rate:
        if round_number >= 2:
            # Carrier came back within budget after our counter — close the deal
            return {
                "decision":    "accept",
                "agreed_rate": offered_rate,
                "status":      "booked",
                "message":     f"Alright, we will do {int(offered_rate)} EUR. "
                               f"I will get the paperwork to you now.",
            }
        else:
            # First offer above target but within budget — push back to target
            return {
                "decision":    "counter",
                "agreed_rate": None,
                "status":      "negotiating",
                "message":     f"I understand, but the best I can do on this lane "
                               f"is {int(target_rate)} EUR. Can you work with that?",
            }

    # Carrier is above our absolute ceiling
    if round_number < 2:
        # Hold firm — do not reveal the floor rate
        return {
            "decision":    "counter",
            "agreed_rate": None,
            "status":      "negotiating",
            "message":     f"I hear you, but our rate for this load is {int(target_rate)} EUR. "
                           f"That is the best we can offer on this lane.",
        }
    else:
        # Two rounds exhausted — decline politely
        return {
            "decision":    "decline",
            "agreed_rate": None,
            "status":      "declined",
            "message":     "I am sorry, we simply cannot go higher on this load. "
                           "I hope we can work together on a future shipment. "
                           "Have a good day.",
        }


@app.route("/evaluate", methods=["POST"])
def evaluate():
    data = request.get_json()

    offered_rate = float(data["offered_rate"])
    target_rate  = float(data["target_rate"])
    floor_rate   = float(data["floor_rate"])
    round_number = int(data.get("round", 1))

    result = _negotiate(offered_rate, target_rate, floor_rate, round_number)

    return jsonify({
        "decision":     result["decision"],
        "message":      result["message"],
        "agreed_rate":  result["agreed_rate"] if result["agreed_rate"] is not None else "",
        "status":       result["status"],
        # Pass these through so the next n8n nodes can use them
        "load_id":      data.get("load_id"),
        "row_number":   data.get("row_number"),
        "carrier_name": data.get("carrier_name"),
        "toolCallId":   data.get("toolCallId"),
    })


# ── 3. FORMAT GET-LOAD RESPONSE ───────────────────────────────────────────────
# Vapi expects: {"results": [{"toolCallId": "...", "result": "<JSON string>"}]}
# Direct curl: just return the load data as-is

@app.route("/format-get-load", methods=["POST"])
def format_get_load():
    data         = request.get_json()
    tool_call_id = data.get("toolCallId")

    # Strip internal fields Vapi doesn't need
    load_data = {k: v for k, v in data.items()
                 if k not in ("toolCallId", "row_number")}

    if tool_call_id:
        return jsonify({
            "results": [{"toolCallId": tool_call_id, "result": json.dumps(load_data)}]
        })
    else:
        return jsonify(load_data)


# ── 4. FORMAT EVALUATE RESPONSE ───────────────────────────────────────────────

@app.route("/format-evaluate", methods=["POST"])
def format_evaluate():
    data         = request.get_json()
    tool_call_id = data.get("toolCallId")

    result = {
        "decision":    data["decision"],
        "message":     data["message"],
        "agreed_rate": data["agreed_rate"],
    }

    if tool_call_id:
        return jsonify({
            "results": [{"toolCallId": tool_call_id, "result": json.dumps(result)}]
        })
    else:
        return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
