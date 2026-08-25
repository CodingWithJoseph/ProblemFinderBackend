# AI gateway

Immensity uses one configured AI provider and model for Search, Analyze, Analyze conversations, and Build. Each operation keeps its own prompt and strict JSON contract, but provider selection, credentials, timeouts, structured-output validation, and error handling are centralized.

## Local development with Ollama

Install Ollama, then run:

```powershell
ollama pull gpt-oss:20b
ollama serve
```

Configure the backend `.env`:

```env
ENVIRONMENT=development
AI_PROVIDER=ollama
AI_BASE_URL=http://127.0.0.1:11434/v1
AI_MODEL=gpt-oss:20b
AI_API_KEY=
AI_TIMEOUT_SECONDS=120
AI_REASONING_EFFORT=medium
```

If your installed model has a different Ollama name, set `AI_MODEL` to the exact value shown by `ollama list`.

## Production with OpenAI

Configure the production environment:

```env
ENVIRONMENT=production
AI_PROVIDER=openai
AI_BASE_URL=https://api.openai.com/v1
AI_MODEL=gpt-5.6-terra
AI_API_KEY=your-secret-key
AI_TIMEOUT_SECONDS=120
AI_REASONING_EFFORT=medium
```

Production rejects Ollama and custom OpenAI hosts. The removed Search, Analyze, Build, Groq, and legacy OpenAI variables are no longer read.

## Reliability behavior

- Every operation validates model JSON against its own contract and makes one repair attempt when JSON or schema validation fails.
- Analyze and Build workers only persist a version after validation succeeds, so a failed generation does not replace the last valid result.
- Search can use its existing deterministic parsing fallback when AI interpretation is unavailable. This is not a second model provider.
