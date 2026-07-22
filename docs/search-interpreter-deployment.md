# Conversational search interpreter deployment

The search interpreter is optional. If its configured model server, credentials,
or database-backed filter options are unavailable, the API remains online and
returns a confirmable keyword-search draft.

## Local development on the RTX 3090 Ti

Run `llama-server` (or another compatible local runtime) with an OpenAI-compatible
Chat Completions endpoint and schema-constrained output enabled. The default
configuration expects the server at `http://127.0.0.1:8080/v1` and a local model
alias of `gpt-oss-20b`:

```env
SEARCH_INTERPRETER_PROVIDER=local
SEARCH_INTERPRETER_BASE_URL=http://127.0.0.1:8080/v1
SEARCH_INTERPRETER_MODEL=gpt-oss-20b
SEARCH_INTERPRETER_TIMEOUT_SECONDS=30
```

Schema-constrained generation can take roughly 20-30 seconds on the local
20B model, so keep the local timeout at 30 seconds unless the deployed model is
known to respond faster.

When the backend runs in Docker and the model server runs on the Windows host,
use `http://host.docker.internal:8080/v1` instead. Local mode does not require an
API key. The runtime's model alias remains configurable because it may differ
from the model's display name.

## Railway production with Groq

Configure these Railway variables:

```env
SEARCH_INTERPRETER_PROVIDER=groq
SEARCH_INTERPRETER_BASE_URL=https://api.groq.com/openai/v1
SEARCH_INTERPRETER_MODEL=openai/gpt-oss-20b
SEARCH_INTERPRETER_TIMEOUT_SECONDS=8
GROQ_API_KEY=<Railway secret>
```

The Groq adapter only permits the official Groq API base URL so a Groq credential
cannot be forwarded to another host. Missing credentials do not fail application
startup; interpretation falls back to keyword mode.

### Required privacy control

Before enabling Groq in production, a Groq organization administrator must enable
**Zero Data Retention** in **Groq Console → Data Controls**. Groq Chat Completions
does not currently support the `store` request parameter, so omitting persistence
features is not a substitute for enabling Zero Data Retention.

The application does not send its internal user ID to either provider and does
not log prompts, user messages, model responses, or credentials. Operational logs
contain only the provider name and a fixed failure category.

References:

- https://console.groq.com/docs/structured-outputs
- https://console.groq.com/docs/your-data
- https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
