from sqlalchemy import case, literal, or_, select

from app.config import PIPELINE_VERSION
from app.models import Cluster, ClusterItem
from app.search_normalization import meaningful_search_terms

SEARCH_MAX_TOKENS = 12

def _contains_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _item_text_match(pattern: str):
    exists_q = select(ClusterItem.id).where(
        ClusterItem.cluster_id == Cluster.id,
        ClusterItem.pipeline_version == PIPELINE_VERSION,
        or_(
            ClusterItem.title.ilike(pattern, escape="\\"),
            ClusterItem.body.ilike(pattern, escape="\\"),
            ClusterItem.problem_statement.ilike(pattern, escape="\\"),
            ClusterItem.platform.ilike(pattern, escape="\\"),
            ClusterItem.community.ilike(pattern, escape="\\"),
        ),
    )
    return exists_q.exists()


def search_text_expressions(query: str):
    conditions = []
    score_parts = []
    meaningful_terms = meaningful_search_terms(query)
    normalized_phrase = " ".join(meaningful_terms) or query
    terms: list[tuple[str, tuple[int, int, int]]] = [(normalized_phrase, (8, 5, 3))]
    seen = {normalized_phrase.casefold()}
    for token in meaningful_terms:
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        terms.append((token, (3, 2, 1)))
        if len(terms) >= SEARCH_MAX_TOKENS + 1:
            break

    for term, weights in terms:
        pattern = _contains_pattern(term)
        name_match = Cluster.name.ilike(pattern, escape="\\")
        summary_match = Cluster.summary.ilike(pattern, escape="\\")
        item_match = _item_text_match(pattern)
        conditions.extend([name_match, summary_match, item_match])
        score_parts.extend([
            case((name_match, weights[0]), else_=0),
            case((summary_match, weights[1]), else_=0),
            case((item_match, weights[2]), else_=0),
        ])

    relevance = sum(score_parts, literal(0))
    return conditions, relevance