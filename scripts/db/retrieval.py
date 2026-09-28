"""Schema retrieval: the table definitions and real values a question is answered from.

Built on the assumption that the schema itself can be too large to hand to a model whole,
the way its data already is. There is no "small enough, send everything" shortcut here: every
question goes through the same dense-plus-lexical search over the indexed tables, the same
foreign-key expansion to pull in the tables a join needs but the question never named, and
the same search over indexed values to match what the question actually said ("Americans",
"rock") onto what a column really holds.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Optional

from config import QDRANT_COLLECTION_TABLES, QDRANT_COLLECTION_VALUES
from scripts.db.clients import embed, qdrant

DEFAULT_TOP_K = 8
_MAX_JOIN_HOPS = 3
# A table only belongs in the core set if it is at least this much of the best-matching
# table's own score: dense similarity on a real schema drops off a cliff after the table a
# question is actually about, the way "how many customers" scored the customer table at
# 0.34 and everything else at 0.20 or below, so a fixed top-k took seven tables nothing
# needed along with the one that mattered. A ratio against the best match, rather than a
# fixed count, is what lets one obviously-dominant table stay alone and a genuinely
# multi-table question still keep every table that is actually close to what it named.
_CORE_SCORE_RATIO = 0.6


def _table_name_from_def(table_def: str) -> str:
    return table_def.split('"')[1].split('"')[0]


def _all_table_points() -> list:
    """Every indexed table point, with its payload. Cheap: one entry per table, not per row."""
    points: list = []
    offset = None
    while True:
        found, offset = qdrant.scroll(
            collection_name=QDRANT_COLLECTION_TABLES,
            limit=200,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        points.extend(found)
        if offset is None:
            break
    return points


def join_graph() -> dict[str, set[str]]:
    """The full foreign-key adjacency between every indexed table, read from the index itself.

    Reading it back from Qdrant rather than the live database means a repair-loop retry that
    needs the graph again is not another round trip to Postgres, and it is exactly the graph
    retrieval was built against: if the index is stale, expansion should be stale the same
    way retrieval is, not quietly more current than what a question is actually searched over.
    """
    graph: dict[str, set[str]] = {}
    for point in _all_table_points():
        name = point.payload.get("fingerprint", {}).get("table_name") or _table_name_from_def(
            point.payload.get("table_def", "")
        )
        graph[name] = set(point.payload.get("fk_neighbours") or [])
    return graph


def _shortest_path(graph: dict[str, set[str]], start: str, goal: str) -> Optional[list[str]]:
    if start == goal:
        return [start]
    visited = {start}
    queue: deque[list[str]] = deque([[start]])
    while queue:
        path = queue.popleft()
        if len(path) > _MAX_JOIN_HOPS + 1:
            continue
        for neighbour in graph.get(path[-1], ()):
            if neighbour == goal:
                return path + [neighbour]
            if neighbour not in visited:
                visited.add(neighbour)
                queue.append(path + [neighbour])
    return None


def expand_with_joins(table_names: set[str], graph: Optional[dict[str, set[str]]] = None) -> set[str]:
    """Every retrieved table, plus whatever sits on the join path between any two of them.

    A question about artists and revenue names neither Album, Track nor InvoiceLine, but
    without them there is no query that actually joins Artist to Invoice. The path is found
    on the graph as it stands independent of which tables happen to have ranked well for this
    particular question, and capped at a handful of hops so two tables that are only related
    by a long, incidental chain do not pull in most of the schema to connect them.

    Deliberately not "add every direct neighbour of every retrieved table" instead: on a
    schema where most tables sit one join away from several others, that pulls in whatever a
    core table happens to be connected to regardless of whether the question needs it, which
    is worse than the fixed top-k this replaced. A table that is genuinely needed only for a
    join, and never named or closely matched itself, is instead recovered by the repair loop:
    a query that could not reach it fails validation by naming a table this attempt was never
    given, and the retry that follows asks for a wider retrieval specifically because of that.
    """
    graph = graph if graph is not None else join_graph()
    names = list(table_names)
    expanded = set(table_names)
    for i, left in enumerate(names):
        for right in names[i + 1:]:
            if right in graph.get(left, ()):
                continue  # already directly joined; nothing to add
            path = _shortest_path(graph, left, right)
            if path:
                expanded.update(path)
    return expanded


_WORD = re.compile(r"[a-z0-9]+")

# Words that carry no meaning about which table is relevant. Left unfiltered, a question
# like "how many customers are there" matched "are" and "there" against a table's evidence
# sentence as readily as "customers" matched the customer table itself, which is what let an
# unrelated table like playlist_track outrank the one the question was actually about.
_STOPWORDS = {
    "how", "many", "much", "what", "which", "who", "whom", "whose", "where", "when", "why",
    "is", "are", "was", "were", "am", "be", "been", "being", "do", "does", "did", "can",
    "could", "would", "should", "will", "shall", "may", "might", "must", "have", "has", "had",
    "there", "here", "the", "a", "an", "of", "in", "on", "at", "to", "for", "from", "with",
    "and", "or", "but", "if", "then", "than", "that", "this", "these", "those", "it", "its",
    "me", "my", "i", "you", "your", "we", "our", "us", "they", "them", "each", "per", "all",
    "get", "got", "give", "show", "tell", "list", "find", "please", "about", "not", "no",
}


def _words(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def _singular(word: str) -> str:
    """The singular of an English word, well enough to match "customers" to "customer".

    A small, general piece of word handling rather than a list of table names: whatever the
    database is, a question says "invoices" where the schema says "Invoice".
    """
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("es") and word[-3] in "sxzh":
        return word[:-2]
    if len(word) > 2 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _content_words(text: str) -> set[str]:
    return {
        _singular(word)
        for word in _words(text)
        if len(word) > 2 and word not in _STOPWORDS
    }


def _named_tables(question: str, points: list) -> set[str]:
    """Tables whose own name is one of the question's words, not just somewhere in its text.

    Used for the core set rather than full table_def overlap: a table's def includes its
    generated evidence sentence, prose that can easily mention another table in passing (an
    employee's description naming the customers they support, say), and that is not the same
    thing as the question actually being about that other table.
    """
    asked = _content_words(question)
    named = set()
    for point in points:
        name = _table_name(point)
        name_words = {_singular(w) for w in re.split(r"[^a-z0-9]+", name.lower()) if w}
        if name_words & asked:
            named.add(name)
    return named


def _lexical_rank(question: str, points: list) -> list[str]:
    """Table point ids ranked by content-word overlap with the question, best first.

    The complement to dense search rather than a replacement for it: a table whose name or a
    column name is one of the question's own words belongs near the top even when nothing
    about its embedding happens to be close, the way a literal word in the question can rank
    an unrelated table above the one that actually answers it.
    """
    asked = _content_words(question)
    if not asked:
        return []
    scored = []
    for point in points:
        text_words = _content_words(point.payload.get("table_def", ""))
        overlap = len(asked & text_words)
        if overlap:
            scored.append((-overlap, point.id))
    scored.sort()
    return [pid for _score, pid in scored]


def _reciprocal_rank_fusion(*rankings: list[str], k: int = 60) -> list[str]:
    """Combine several ranked id lists into one, by how well each id ranks across all of them.

    Reciprocal rank rather than raw score: a dense cosine similarity and a lexical overlap
    count are not on the same scale and are not meaningfully comparable, but "how far down
    this list is it" is comparable across any ranking at all.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank + 1)
    return [item_id for item_id, _score in sorted(scores.items(), key=lambda kv: -kv[1])]


def _table_name(point) -> str:
    return point.payload.get("fingerprint", {}).get("table_name") or _table_name_from_def(
        point.payload.get("table_def", "")
    )


def retrieve_tables(question: str, top_k: Optional[int] = None) -> list[dict]:
    """Table definitions relevant to a question.

    Ordinarily adaptive: a table only makes the core set if its dense score is close to the
    best match's own (see _CORE_SCORE_RATIO), so one dominant table stays alone rather than
    dragging along whatever else happened to rank in some fixed top-k. A table the question
    names outright is added regardless of its score. The core set is then expanded with
    whatever sits on the join path between two core tables, since a join target does not
    have to describe itself in words to be needed for a query that connects two things the
    question did name.

    top_k switches to the older, wider behaviour instead: every one of the top top_k tables
    by fused dense-and-lexical rank, regardless of score gaps. Passed by a repair retry after
    a query named a table this task's first, adaptive pass did not surface, where the point is
    specifically to cast a net looser than "obviously close to the best match".

    Returns a list of {"table": name, "table_def": text, "fk_neighbours": [...]}.
    """
    points = _all_table_points()
    if not points:
        return []

    by_id = {point.id: point for point in points}
    pool_size = max((top_k or DEFAULT_TOP_K) * 3, 20)

    dense_hits = qdrant.query_points(
        collection_name=QDRANT_COLLECTION_TABLES,
        query=embed(question),
        limit=min(pool_size, len(points)),
    ).points
    lexical_ranking = _lexical_rank(question, points)

    if top_k is not None:
        dense_ranking = [hit.id for hit in dense_hits]
        fused = _reciprocal_rank_fusion(dense_ranking, lexical_ranking)[:top_k]
        core_ids = set(fused or dense_ranking[:top_k])
        core_names = {_table_name(by_id[pid]) for pid in core_ids if pid in by_id}
    else:
        top_score = dense_hits[0].score if dense_hits else 0
        close_enough = [
            hit for hit in dense_hits
            if top_score > 0 and hit.score >= top_score * _CORE_SCORE_RATIO
        ]
        if not close_enough and dense_hits:
            close_enough = dense_hits[:1]  # the best match always counts, even if score <= 0
        core_names = {_table_name(by_id[hit.id]) for hit in close_enough if hit.id in by_id}
        core_names |= _named_tables(question, points)

    graph = {_table_name(p): set(p.payload.get("fk_neighbours") or []) for p in points}
    expanded_names = expand_with_joins(core_names, graph)

    return [
        {
            "table": _table_name(point),
            "table_def": point.payload.get("table_def", ""),
            "fk_neighbours": sorted(point.payload.get("fk_neighbours") or []),
        }
        for point in points
        if _table_name(point) in expanded_names
    ]


def search_values(question: str, top_k: int = 10) -> list[dict]:
    """Real, indexed values that match the question, best first.

    Each hit is {"table", "column", "value", "score"}: a column and value the database really
    holds, not a guess at how the question's words map onto it. "Americans" matches
    Customer.Country = "USA" this way, from the value itself rather than from the word.
    """
    if not qdrant.collection_exists(QDRANT_COLLECTION_VALUES):
        return []
    hits = qdrant.query_points(
        collection_name=QDRANT_COLLECTION_VALUES,
        query=embed(question),
        limit=top_k,
    ).points
    return [
        {
            "table": hit.payload.get("table"),
            "column": hit.payload.get("column"),
            "value": hit.payload.get("value"),
            "score": hit.score,
        }
        for hit in hits
    ]


def get_relevant_schema(question: str, top_k: Optional[int] = None) -> str:
    """Table definitions relevant to a question, as one block of text.

    Kept as a plain string for the callers that only ever wanted the schema to put in a
    prompt; retrieve_tables above is the same search with the structure still on it, for
    anything that needs to reason about which tables it got rather than just read them.
    """
    tables = retrieve_tables(question, top_k=top_k)
    return "\n".join(t["table_def"] for t in tables)


def all_schema_text() -> str:
    """Every table definition currently indexed, used when a task needs the whole picture.

    A deliberately unbounded fallback, not the ordinary path: reached only when a task's own
    retrieval left it with the wrong entity and it is being given another attempt with
    nothing held back.
    """
    return "\n".join(point.payload.get("table_def", "") for point in _all_table_points())
