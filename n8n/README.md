# n8n Workflows

Two workflows orchestrate the agent's tool calls. Full node-by-node documentation is in `docs/build-guide.md` (repo root), Parts 5 and 6.

## get_load

Webhook (`/get-load`) → Parse Input (Flask `/parse-input`) → Google Sheets Read Rows (filter by `load_id`) → Format Response (Flask `/format-get-load`) → Respond to Webhook

## evaluate_offer

Webhook (`/evaluate-offer`) → Parse Input → Google Sheets Read Rows → Evaluate Offer (Flask `/evaluate`) → Google Sheets Update Row (status, agreed_rate, carrier_name) → Format Response (Flask `/format-evaluate`) → Respond to Webhook

## Workflow exports

`get_load.json` and `evaluate_offer.json` are exports from the n8n editor (workflow menu → Download). Credentials are not included in n8n exports; you connect your own Google Sheets credential after importing. `YOUR_GOOGLE_SHEET_ID` and `YOUR_CREDENTIAL_ID` are placeholders — point them at your own sheet and credential after import.
