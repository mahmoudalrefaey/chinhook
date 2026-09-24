import sys
import os

# Add parent directory to path for config import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.db_module import (
    check_and_index,
    full_reindex,
    incremental_reindex,
    get_db_fingerprint,
    get_qdrant_fingerprint,
    compare_fingerprints,
)


def run_index_check():
    """Run index check and re-index if needed. Returns number of tables indexed."""
    return check_and_index()


def run_full_reindex():
    """Force full re-index."""
    return full_reindex()


def run_incremental_reindex(changed_tables: list[str]):
    """Run incremental re-index for specific tables."""
    return incremental_reindex(changed_tables)


def get_index_status() -> dict:
    """Get current index status without modifying anything."""
    from scripts.db_module import conn
    db_fp = get_db_fingerprint(conn)
    qdrant_fp = get_qdrant_fingerprint()
    needs_reindex, changed = compare_fingerprints(db_fp, qdrant_fp)

    return {
        "db_tables": len(db_fp),
        "qdrant_tables": len(qdrant_fp),
        "needs_reindex": needs_reindex,
        "changed_tables": changed,
        "db_fingerprint": db_fp,
        "qdrant_fingerprint": qdrant_fp,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Chinhook Indexer")
    parser.add_argument("--check", action="store_true", help="Check and index if needed")
    parser.add_argument("--full", action="store_true", help="Force full re-index")
    parser.add_argument("--status", action="store_true", help="Show index status")

    args = parser.parse_args()

    if args.status:
        status = get_index_status()
        print(f"DB Tables: {status['db_tables']}")
        print(f"Qdrant Tables: {status['qdrant_tables']}")
        print(f"Needs Re-index: {status['needs_reindex']}")
        if status['changed_tables']:
            print(f"Changed Tables: {status['changed_tables']}")
    elif args.full:
        count = run_full_reindex()
        print(f"Full re-index complete: {count} tables")
    else:
        count = run_index_check()
        print(f"Index check complete: {count} tables indexed")