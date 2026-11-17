"""Shared helpers for the SQLite FTS5 lifecycle tests."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from typing import List
from typing import Sequence
from typing import Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine

from alembic.operations.fts import quote_ident


def q(name: str) -> str:
    return quote_ident(name)


def fts_hits(conn: Any, index_name: str, query: str) -> List[int]:
    rows = conn.execute(
        text(
            "SELECT rowid FROM %s WHERE %s MATCH :q ORDER BY rowid"
            % (q(index_name), q(index_name))
        ),
        {"q": query},
    ).fetchall()
    return [row[0] for row in rows]


def master_names(conn: Any) -> List[str]:
    return [
        row[0]
        for row in conn.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
    ]


@contextmanager
def assert_hits(engine: Engine, index_name: str):
    """Context manager yielding a checker bound to a fresh connection."""

    def check(query: str, expected: Sequence[int]) -> None:
        with engine.connect() as conn:
            got = fts_hits(conn, index_name, query)
            assert got == list(expected), (
                query,
                got,
                list(expected),
            )

    yield check


def fts_decl(
    name: str,
    table: str,
    columns: Sequence[str],
    tokenize: str = "unicode61",
) -> dict:
    return {
        "name": name,
        "table": table,
        "columns": list(columns),
        "tokenize": tokenize,
    }


def all_managed_names(
    index_name: str,
) -> Tuple[str, ...]:
    from alembic.operations.fts import (
        FTS_SHADOW_SUFFIXES,
        REGISTRY_TABLE,
        TRIGGER_DELETE_SUFFIX,
        TRIGGER_INSERT_SUFFIX,
        TRIGGER_UPDATE_SUFFIX,
    )

    names = [REGISTRY_TABLE, index_name]
    names.extend(index_name + suffix for suffix in FTS_SHADOW_SUFFIXES)
    names.extend(
        index_name + suffix
        for suffix in (
            TRIGGER_INSERT_SUFFIX,
            TRIGGER_UPDATE_SUFFIX,
            TRIGGER_DELETE_SUFFIX,
        )
    )
    return tuple(names)
