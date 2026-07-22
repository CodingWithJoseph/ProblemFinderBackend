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
- external or web search
- AI-generated result summaries
- arbitrary tools, SQL, columns, or operators

Those capabilities can be added later as separately reviewed tools without
weakening the current confirmation boundary.
