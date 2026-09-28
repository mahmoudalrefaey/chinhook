"""Retrieval against the real index: real embeddings, real Qdrant, real foreign keys.

Needs the schema already indexed (scripts/indexer.py --full), and real Azure embedding
credentials to embed the question itself, so this is marked llm rather than plain
integration: it is exercising the value the embedding model actually adds, not just that the
database and Qdrant are reachable.
"""

import pytest

from scripts.db.retrieval import retrieve_tables, search_values

pytestmark = [pytest.mark.integration, pytest.mark.llm]


def test_retrieval_fills_in_the_join_path_for_an_artist_revenue_question():
    tables = {t["table"] for t in retrieve_tables("which artist made the most money from sales")}
    # Artist and InvoiceLine are the two ends of the question; Album and Track are the join
    # path between them that the question never names but the query cannot run without.
    assert {"artist", "album", "track", "invoice_line"} <= tables


def test_value_search_matches_a_demonym_to_the_stored_country():
    hits = search_values("Americans")
    top = hits[0]
    assert top["value"] == "USA"
    assert top["column"] in {"country", "billing_country"}


def test_value_search_matches_a_genre_word_to_its_real_casing():
    # This is the exact case a case-sensitive ILIKE probe used to get wrong: "rock" the word
    # against "Rock" the stored value.
    hits = search_values("rock")
    assert hits[0]["value"] == "Rock"
    assert hits[0]["column"] == "name"
