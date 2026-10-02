"""Schema retrieval, wrapped in a chat-scoped cache.

chinhook.db owns the actual search: the hybrid dense-and-lexical retrieval over
indexed tables, the foreign-key expansion, and the value index. What this module adds is
that a task's retrieval can be reused: a follow-up that names entities already searched
reuses those cached definitions and skips the embedding round trip; a question about
anything else does a fresh retrieval, and a fresh retrieval adds to the cache rather than
replacing it. It also holds the cheap, non-embedding introspection (table and column names,
with no model call) that node_understand uses to ground an entity or tell a genuinely
unsupported question apart from one whose table just was not retrieved this time.
"""

from __future__ import annotations

import re
import time
from typing import Optional

from chinhook import runtime
from chinhook.agent.state import SchemaCache, split_schema

_CATALOG_RETRY_COOLDOWN_SECONDS = 5.0
_CATALOG_TTL_SECONDS = 300.0
# Keyed by the connected database (runtime.Runtime.tenant_id): every session connected to the
# same database shares one copy, and no session ever reads another database's.
_catalogs: dict[str, tuple[float, dict[str, list[tuple[str, str]]]]] = {}
_catalog_failures: dict[str, float] = {}
_word_indexes: dict[str, dict[str, str]] = {}


def catalog() -> dict[str, list[tuple[str, str]]]:
    """Table name -> [(column, type)], introspected and cached for a few minutes at a time.

    The query understanding node is given this so it can ground an entity against real
    columns instead of guessing at them. Cached with a TTL rather than for the process's
    whole life, so a table added, dropped or renamed outside this process (a migration, a
    restore) is seen again soon rather than only after a restart. A failed introspection
    returns the last good catalog (or an empty one) and remembers the failure only for a
    short cooldown: a blip in one read should not turn away every question afterwards.
    """
    tenant = runtime.current().tenant_id
    now = time.monotonic()
    cached = _catalogs.get(tenant)
    if cached is not None and (now - cached[0]) < _CATALOG_TTL_SECONDS:
        return cached[1]
    failed_at = _catalog_failures.get(tenant)
    if failed_at and (now - failed_at) < _CATALOG_RETRY_COOLDOWN_SECONDS:
        return cached[1] if cached else {}

    from chinhook.db.introspection import get_catalog
    from chinhook.db.clients import get_connection

    try:
        with get_connection() as conn:
            tables = get_catalog(conn)
    except Exception as exc:  # noqa: BLE001
        _catalog_failures[tenant] = time.monotonic()
        print(f"Schema catalog unavailable for this request: {type(exc).__name__}: {exc}")
        return cached[1] if cached else {}
    _catalogs[tenant] = (time.monotonic(), tables)
    _word_indexes.pop(tenant, None)
    _forget_idle(now)
    return tables


def _forget_idle(now: float) -> None:
    """Drop catalogs nobody has refreshed in an hour, so they do not pile up forever."""
    for tenant, (stamp, _tables) in list(_catalogs.items()):
        if now - stamp > 3600:
            _catalogs.pop(tenant, None)
            _word_indexes.pop(tenant, None)
            _catalog_failures.pop(tenant, None)


def _singular(word: str) -> str:
    """The singular of an English word, well enough to match a table name to a question.

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


def schema_index() -> dict[str, str]:
    """Word -> table, built from the schema of the database that is actually connected.

    Nothing here is written for a particular database. The index is the tables' own names,
    their parts, their columns and ordinary plural forms of all of them, so a question is
    matched to a table by the words the question uses against the words the schema has.
    Rebuilding it when the catalog is rebuilt means it cannot fall out of step with what is
    connected.
    """
    tenant = runtime.current().tenant_id
    cached = _word_indexes.get(tenant)
    if cached is not None:
        return cached

    index: dict[str, str] = {}
    tables = catalog()
    for table, columns in tables.items():
        parts = [p for p in re.split(r"[^A-Za-z0-9]+", table) if p]
        for part in parts:
            index.setdefault(part.lower(), table)
        index.setdefault(table.lower(), table)
        index.setdefault(_singular(table.lower()), table)
        for column, _dtype in columns:
            words = [p for p in re.split(r"[^A-Za-z0-9]+", column) if p]
            for word in words:
                # A column names its table too, which is how a question about a field gets
                # to the table the field lives in.
                index.setdefault(word.lower(), table)
                index.setdefault(_singular(word.lower()), table)
    # An empty index built from a database that genuinely has no tables would be unusual but
    # real, and caching it would be correct. An empty index built because catalog() just hit
    # its failure cooldown is not the same thing and must not be locked in: only a catalog
    # read that actually returned tables gets cached here.
    if tables:
        _word_indexes[tenant] = index
    return index


def mentions_table_word(text: str) -> list[str]:
    """Tables a message names, in the order it names them."""
    index = schema_index()
    found: list[str] = []
    for word in re.findall(r"[a-z0-9]+", (text or "").lower()):
        table = index.get(word) or index.get(_singular(word))
        if table and table not in found:
            found.append(table)
    return found


def catalog_text() -> str:
    """The same information as one compact block of text for a prompt."""
    return "\n".join(
        f'"{name}"({", ".join(column for column, _ in columns)})'
        for name, columns in catalog().items()
    )


def table_names_text() -> str:
    """Every table name the database has, with no column detail.

    Deliberately smaller than catalog_text(): understand needs to know whether a concept
    exists anywhere in the database at all, to tell a genuinely unsupported question apart
    from one whose table just did not make it into this message's retrieved slice, and a list
    of names answers that on a database of any size. The detailed definition of any table it
    actually needs still comes from retrieval, not from this list.
    """
    return ", ".join(sorted(catalog()))


def hinted_tables(task) -> list[str]:
    """Which tables a task is about, matched from the words of the task against the schema.

    Used only to decide whether the cached schema can answer this task, so an incomplete
    guess is safe: the worst case is a fresh retrieval.
    """
    haystack = " ".join(
        filter(None, [
            task.question,
            " ".join(task.entities or []),
            " ".join(str(f.get("column_hint") or "") for f in (task.filters or [])),
            " ".join(str(f.get("table_hint") or "") for f in (task.filters or [])),
        ])
    )
    return mentions_table_word(haystack)


def retrieve_for_task(task, cache: SchemaCache, top_k: Optional[int] = None) -> tuple[str, list[str], bool]:
    """Schema context for one task.

    Returns (schema_text, table_names, came_from_cache). A task whose tables are all
    already in this chat's cache reuses them; anything else retrieves fresh. top_k widens the
    search past its ordinary default, for a repair retry after a query named a table this
    task's first, narrower retrieval never surfaced.
    """
    hints = hinted_tables(task)
    if hints and top_k is None:
        cached = cache.lookup(hints)
        if cached is not None:
            return cached, hints, True

    from chinhook.db.retrieval import get_relevant_schema

    kwargs = {"top_k": top_k} if top_k is not None else {}
    schema_text = get_relevant_schema(task.question or task.raw, **kwargs)
    names = list(split_schema(schema_text))
    cache.add(schema_text)
    return schema_text, names, False


def retrieve_for_message(question: str) -> tuple[str, list[str]]:
    """A coarse schema slice for reading the whole message, before it is split into tasks.

    Deliberately not cached and not scoped to one task: this runs once per message, against
    whatever the user actually typed, so that node_understand can tell what the message is
    about without being handed the entire catalog. Each task gets its own, more precisely
    scoped retrieval afterward, in its own branch of the graph.
    """
    from chinhook.db.retrieval import retrieve_tables

    tables = retrieve_tables(question)
    schema_text = "\n".join(t["table_def"] for t in tables)
    names = [t["table"] for t in tables]
    return schema_text, names


def search_values_for(question: str, top_k: int = 10) -> list[dict]:
    """Real, indexed values that match the question (chinhook.db.retrieval.search_values)."""
    from chinhook.db.retrieval import search_values

    return search_values(question, top_k=top_k)


def all_schema_text() -> str:
    """Every table definition in the index, used when a task needs the whole picture.

    Genuinely every table, not a search result: routing this through get_relevant_schema
    instead used to return only the top few hits for whatever the question happened to be,
    which on a schema with more tables than that top-k defeats the entire point of asking for
    the whole picture.
    """
    from chinhook.db.retrieval import all_schema_text as _all_schema_text

    return _all_schema_text()
