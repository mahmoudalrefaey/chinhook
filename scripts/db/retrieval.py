"""Schema retrieval: the table definitions a question is answered from."""

from config import QDRANT_COLLECTION
from scripts.db.clients import embed, qdrant

SMALL_SCHEMA_THRESHOLD = 25


def get_relevant_schema(question: str, top_k=5) -> str:
    """Table definitions relevant to a question.

    Retrieval only helps once a schema is too big to show the model in full. Below that, it
    is pure downside: similarity search can rank a table low for reasons that have nothing to
    do with whether it is actually needed, the way a literal word in the question ("tracks")
    pulled unrelated track-named tables above the one table that actually holds sales data.
    Below the threshold, every table is returned and nothing gets silently left out.
    """
    total = qdrant.count(QDRANT_COLLECTION).count

    if total <= SMALL_SCHEMA_THRESHOLD:
        points = qdrant.scroll(
            collection_name=QDRANT_COLLECTION,
            limit=total,
            with_payload=True,
            with_vectors=False,
        )[0]
        return "\n".join(p.payload["table_def"] for p in points)

    hits = qdrant.query_points(
        collection_name=QDRANT_COLLECTION,
        query=embed(question),
        limit=top_k,
    ).points
    return "\n".join(h.payload["table_def"] for h in hits)
