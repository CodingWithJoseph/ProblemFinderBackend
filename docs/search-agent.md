# Bounded Search agent

`POST /clusters/search/agent` is the agentic successor to the single-call
`/clusters/search/interpret` endpoint. The original endpoint remains available
for compatibility.

The agent receives one user message and an optional current draft. It may make
at most three sequential function calls:

1. `get_search_filter_options` reads the bounded canonical filter values already
   present in the database.
2. `prepare_search_draft` returns the complete structured interpretation and
   ends the run.

The public response includes only fixed action names and outcomes. Model
reasoning, prompts, provider responses, credentials, and the internal user ID
are neither returned nor logged.

## Stop conditions

- `confirmation_required`: a validated draft is ready for user approval.
- `clarification_required`: one material ambiguity must be answered by the user.
- `fallback`: the provider or filter-options read was unavailable.
- `step_limit`: the model did not reach a valid terminal tool within three calls.

## Deliberately unavailable in this PR

- database query execution
- automatic execution without confirmation
- AI-generated result summaries
- arbitrary tools, SQL, columns, or operators

Those capabilities can be added later as separately reviewed tools without
weakening the current confirmation boundary.

## Optional external evidence search

`POST /clusters/search/web` is a separate, authenticated post-confirmation
operation. It is not available to the draft-building agent, so interpreting a
message cannot silently trigger an external request. The request must contain
`confirmed: true` and the deployment must explicitly set
`SEARCH_WEB_ENABLED=true`.

The adapter permits only Groq Compound or Compound Mini and enables only the
provider's `web_search` tool. It discards the provider's generated answer and
returns at most five normalized citation records. Insecure URLs, local/private
addresses, credential-bearing URLs, fragments, tracking parameters, and
duplicates are removed.

Recommended rollout:

1. Keep `SEARCH_WEB_ENABLED=false` in local development and production.
2. Enable it in staging with `SEARCH_WEB_MODEL=groq/compound-mini`.
3. Verify Groq Zero Data Retention, source quality, latency, and spend.
4. Enable it in production only after that review.

Groq's built-in web search is not a HIPAA Covered Cloud Service. Do not send
protected health information through this feature.

References:

- https://console.groq.com/docs/tool-use/built-in-tools/web-search
- https://console.groq.com/docs/compound/built-in-tools
- https://console.groq.com/docs/your-data

## Optional grounded summaries

`POST /clusters/search/summary` is hidden unless
`SEARCH_SUMMARIES_ENABLED=true`. It requires `confirmed: true`, an owned Search
run ID, and one to five cluster IDs. The selected clusters must be a subset of
the IDs persisted for that confirmed run. The server then reloads those
clusters and their bounded top evidence items from the database; the client
cannot send text for the model to summarize.

An optional `web_query` runs through the separately gated external evidence
adapter. The summary model receives only the server-built database evidence and
normalized web snippets. The overview and every finding must cite exact
evidence IDs, and the server rejects the complete response if any citation is
unknown or if no database citation is present. Provider failure returns an
unavailable response; the API does not fabricate a fallback summary.

Keep summaries disabled in production until the staging rollout has verified
grounding quality, latency, provider privacy controls, and cost.
