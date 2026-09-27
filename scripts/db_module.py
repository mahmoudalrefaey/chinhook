"""Re-exports of the database tooling, which now lives in the scripts.db package.

Kept so that anything importing from scripts.db_module keeps working unchanged, and so that
the one place a caller looks for these is still the one it was.
"""

from scripts.db import *  # noqa: F401,F403
from scripts.db import __all__  # noqa: F401
from scripts.db.clients import get_connection  # noqa: F401


if __name__ == "__main__":
    from scripts.db.indexing import check_and_index
    from scripts.db.retrieval import get_relevant_schema

    check_and_index()
    question = input("Ask a question about the database: ")
    schema_context = get_relevant_schema(question)
    print(schema_context)
