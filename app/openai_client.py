from openai import AsyncOpenAI
from app.config import get_settings
from app.services.ai_gateway import OLLAMA_BASE_URL, OPENAI_BASE_URL

# Shared async OpenAI client. Used for opportunity search embeddings, problem
# embeddings, and any future embedding/completion needs. Centralised here so the
# API key is read once and the client is reused across routes.
_settings = get_settings()
_provider = _settings.ai_provider.strip().lower()
openai_client = AsyncOpenAI(
    api_key=_settings.ai_api_key.strip() or "ollama-not-a-secret",
    base_url=(
        _settings.ai_base_url.strip()
        or (OPENAI_BASE_URL if _provider == "openai" else OLLAMA_BASE_URL)
    ),
)

# text-embedding-3-small -> 1536 dimensions. Matches problems.embedding and
# cluster_items.embedding column width.
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
