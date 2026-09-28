"""The pure-function parts of scripts/db/retrieval.py: no database, no Qdrant, no model call."""

from scripts.db.retrieval import (
    _content_words,
    _reciprocal_rank_fusion,
    _singular,
    expand_with_joins,
)


def test_singular_handles_the_ordinary_plural_shapes():
    assert _singular("customers") == "customer"
    assert _singular("invoices") == "invoice"
    assert _singular("genres") == "genre"
    assert _singular("categories") == "category"
    assert _singular("addresses") == "address"
    assert _singular("track") == "track"  # already singular; left alone
    assert _singular("glass") == "glass"  # ends in "ss", not a plural


def test_content_words_drops_stopwords_and_singularises():
    words = _content_words("How many customers are there in the database")
    assert "customer" in words
    assert "database" in words
    assert "how" not in words
    assert "are" not in words
    assert "there" not in words
    assert "the" not in words


def test_reciprocal_rank_fusion_favours_items_that_rank_well_in_both_lists():
    dense = ["customer", "invoice", "track"]
    lexical = ["invoice", "customer"]
    fused = _reciprocal_rank_fusion(dense, lexical)
    # customer ranks #1 in dense and #2 in lexical; invoice ranks #2 and #1. Both outrank
    # track, which only appears in one list.
    assert fused[0] in {"customer", "invoice"}
    assert fused.index("track") == len(fused) - 1


def test_reciprocal_rank_fusion_handles_an_empty_list():
    assert _reciprocal_rank_fusion([], ["a", "b"])[0] == "a"
    assert _reciprocal_rank_fusion([]) == []


def _chain_graph():
    # artist -> album -> track -> invoice_line, a straight chain, the way Chinook's own
    # foreign keys actually run from an artist through to a sale.
    return {
        "artist": {"album"},
        "album": {"artist", "track"},
        "track": {"album", "invoice_line", "genre"},
        "invoice_line": {"track", "invoice"},
        "invoice": {"invoice_line", "customer"},
        "customer": {"invoice"},
        "genre": {"track"},
    }


def test_expand_with_joins_fills_in_the_path_between_two_retrieved_tables():
    expanded = expand_with_joins({"artist", "invoice_line"}, _chain_graph())
    assert expanded == {"artist", "album", "track", "invoice_line"}


def test_expand_with_joins_adds_nothing_for_a_single_table():
    expanded = expand_with_joins({"genre"}, _chain_graph())
    assert expanded == {"genre"}


def test_expand_with_joins_adds_nothing_for_tables_already_directly_joined():
    expanded = expand_with_joins({"track", "genre"}, _chain_graph())
    assert expanded == {"track", "genre"}


def test_expand_with_joins_gives_up_on_a_path_longer_than_the_hop_cap():
    # artist to customer is five hops on this graph (album, track, invoice_line, invoice in
    # between), past the cap. Two tables that are only related by a long, incidental chain
    # should not pull in most of the schema just to connect them.
    expanded = expand_with_joins({"artist", "customer"}, _chain_graph())
    assert expanded == {"artist", "customer"}
