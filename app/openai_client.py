from openai import AsyncOpenAI
from app.config import get_settings

# Shared async OpenAI client. Used for opportunity search embeddings, problem
# embeddings, and any future embedding/completion needs. Centralised here so the
# API key is read once and the client is reused across routes.
openai_client = AsyncOpenAI(api_key=get_settings().openai_api_key)

# text-embedding-3-small -> 1536 dimensions. Matches problems.embedding and
# opportunities.embedding column widths.
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
