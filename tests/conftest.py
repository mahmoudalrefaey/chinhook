"""Shared fixtures.

Unit tests run with nothing else running: no database, no Qdrant, no Ollama, no Azure
credentials. Anything that needs one of those is an integration test, marked as such, and is
skipped automatically rather than failing when the stack it needs is not reachable, so the
unit suite stays something that can run anywhere in a few seconds.
"""

import socket

import pytest


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _stack_reachable() -> bool:
    import os

    db_url = os.getenv("DATABASE_URL", "")
    qdrant_url = os.getenv("QDRANT_URL", "http://localhost:6333")

    # A rough parse rather than a real URL library, since this only ever needs to pull a
    # host and a port out of a connection string already known to be well formed.
    def host_port(url: str, default_port: int) -> tuple[str, int]:
        rest = url.split("://", 1)[-1]
        rest = rest.split("@")[-1]
        rest = rest.split("/")[0]
        host, _, port = rest.partition(":")
        return host or "localhost", int(port) if port else default_port

    if not db_url:
        return False
    db_host, db_port = host_port(db_url, 5432)
    q_host, q_port = host_port(qdrant_url, 6333)
    return _port_open(db_host, db_port) and _port_open(q_host, q_port)


@pytest.fixture(scope="session")
def stack_reachable() -> bool:
    return _stack_reachable()


def pytest_collection_modifyitems(config, items):
    if _stack_reachable():
        return
    skip_integration = pytest.mark.skip(
        reason="Postgres/Qdrant not reachable; set DATABASE_URL and QDRANT_URL to run this"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
