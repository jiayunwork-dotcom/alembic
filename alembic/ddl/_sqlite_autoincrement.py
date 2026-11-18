# mypy: allow-untyped-defs, allow-incomplete-defs, allow-untyped-calls
# mypy: no-warn-return-any, allow-any-generics

"""SQLite ``AUTOINCREMENT`` attribute and sequence history helpers.

SQLite keeps the "highest number ever committed" for an ``AUTOINCREMENT``
table in the internal ``sqlite_sequence`` table.  That table is not part of
the table's ``CREATE`` statement, so both SQLAlchemy reflection and Alembic's
copy-and-rename batch rebuild lose it.  This module provides the small,
dependency-free primitives used by the batch rebuild and by autogenerate:

* :func:`ddl_uses_autoincrement` - detect the real ``AUTOINCREMENT``
  attribute from a ``CREATE TABLE`` statement, ignoring quoted identifiers,
  string literals and comments (so a table, column or default that merely
  contains the word does not produce a false positive).
* :func:`get_table_sql` - fetch the stored ``CREATE TABLE`` statement.
* :func:`read_sequence` / :func:`restore_sequence` - read and restore the
  committed high-water mark, without ever materializing a stand-in row.

All functions operate on a SQLAlchemy :class:`~sqlalchemy.engine.Connection`
and only use statements that are valid inside an explicit SQLite
transaction, so a grouped migration can roll the sequence history back
together with its DDL and data.
"""

from __future__ import annotations

import re
from typing import Any
from typing import Mapping
from typing import Optional
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection
    from sqlalchemy.sql.schema import Table

#: the ``AUTOINCREMENT`` keyword left after literals and identifiers have
#: been removed from a ``CREATE TABLE`` statement.
_AUTOINCREMENT_RE = re.compile(r"\bAUTOINCREMENT\b", re.IGNORECASE)


def _strip_sql_literals_and_comments(sql: str) -> str:
    """Return *sql* with string literals, quoted identifiers and comments
    removed.

    SQLite only accepts the ``AUTOINCREMENT`` keyword as the final token of
    an ``INTEGER PRIMARY KEY`` column definition; it is a reserved keyword
    and cannot be used as an unquoted identifier.  Removing every quoted
    section means that a table name, column name or default value containing
    the word cannot cause a false positive, while a real keyword always
    survives.
    """

    out = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if sql.startswith("--", index):
            end = sql.find("\n", index + 2)
            index = length if end == -1 else end
        elif sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            index = length if end == -1 else end + 2
        elif char == "'":
            # string literal; a doubled quote is an escaped quote
            index += 1
            while index < length:
                if sql[index] == "'":
                    if index + 1 < length and sql[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
        elif char == '"':
            # double-quoted identifier
            index += 1
            while index < length:
                if sql[index] == '"':
                    if index + 1 < length and sql[index + 1] == '"':
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
        elif char == "`":
            # back-tick quoted identifier (MySQL compatibility)
            index += 1
            while index < length:
                if sql[index] == "`":
                    if index + 1 < length and sql[index + 1] == "`":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
        elif char == "[":
            # square-bracket quoted identifier
            end = sql.find("]", index + 1)
            index = length if end == -1 else end + 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


def ddl_uses_autoincrement(sql: Optional[str]) -> bool:
    """Return True if a ``CREATE TABLE`` statement enables
    ``AUTOINCREMENT``."""

    if not sql:
        return False
    stripped = _strip_sql_literals_and_comments(sql)
    return bool(_AUTOINCREMENT_RE.search(stripped))


def _explicitly_set(mapping: Optional[Mapping[str, Any]], key: str) -> bool:
    """Return True if *key* was actually passed by the user.

    SQLAlchemy's ``_DialectArgView.__contains__`` reports every known
    dialect option (including dialect defaults), so membership must be
    tested against the view's iteration, which only yields explicitly
    supplied options.
    """

    if mapping is None:
        return False
    try:
        return key in set(mapping)
    except TypeError:  # pragma: no cover - defensive
        return key in mapping


def table_option_enabled(
    table_kwargs: Mapping[str, Any],
    dialect_kwargs: Optional[Mapping[str, Any]] = None,
) -> Optional[bool]:
    """Return the ``sqlite_autoincrement`` table option, if explicitly set.

    ``True`` means the option is enabled; ``False`` means it is explicitly
    disabled; ``None`` means it was not specified and the live database
    determines the behavior.
    """

    if _explicitly_set(table_kwargs, "sqlite_autoincrement"):
        return bool(table_kwargs["sqlite_autoincrement"])
    if _explicitly_set(dialect_kwargs, "sqlite_autoincrement"):
        assert dialect_kwargs is not None
        return bool(dialect_kwargs["sqlite_autoincrement"])
    return None


def resolve_table_autoincrement(
    table: "Table",
    table_kwargs: Mapping[str, Any],
    reflected_autoincrement: Optional[bool],
) -> Optional[bool]:
    """Combine explicit table options with the reflected attribute.

    An explicit ``sqlite_autoincrement`` (on the target metadata / batch
    ``table_kwargs``) wins.  Otherwise the attribute from the live database
    is used, so a rebuild does not silently drop it.
    """

    option = table_option_enabled(table_kwargs, table.dialect_kwargs)
    if option is not None:
        return option
    return reflected_autoincrement


def _quote_schema(schema: str) -> str:
    return '"%s"' % schema.replace('"', '""')


def _master_table(schema: Optional[str]) -> str:
    if schema in (None, "", "main"):
        return "sqlite_master"
    return "%s.sqlite_master" % _quote_schema(schema)


def _sequence_table(schema: Optional[str]) -> str:
    if schema in (None, "", "main"):
        return "sqlite_sequence"
    return "%s.sqlite_sequence" % _quote_schema(schema)


def get_table_sql(
    connection: "Connection",
    table_name: str,
    schema: Optional[str] = None,
) -> Optional[str]:
    """Return the stored ``CREATE TABLE`` statement for *table_name*."""

    row = connection.exec_driver_sql(
        "SELECT sql FROM %s WHERE type = 'table' AND name = ?"
        % _master_table(schema),
        (table_name,),
    ).first()
    return row[0] if row is not None else None


def table_uses_autoincrement(
    connection: "Connection",
    table_name: str,
    schema: Optional[str] = None,
) -> bool:
    """Return True if the live table has the ``AUTOINCREMENT`` attribute."""

    return ddl_uses_autoincrement(
        get_table_sql(connection, table_name, schema=schema)
    )


def _has_sequence_table(
    connection: "Connection", schema: Optional[str] = None
) -> bool:
    row = connection.exec_driver_sql(
        "SELECT 1 FROM %s WHERE type = 'table' AND name = 'sqlite_sequence'"
        % _master_table(schema)
    ).first()
    return row is not None


def read_sequence(
    connection: "Connection",
    table_name: str,
    schema: Optional[str] = None,
) -> Optional[int]:
    """Return the committed high-water mark for *table_name*, or ``None``.

    Only the value stored in ``sqlite_sequence`` is consulted; the maximum
    primary key currently present in the table is deliberately never used as
    a substitute, because deleted rows must not be handed out again.
    """

    if not _has_sequence_table(connection, schema=schema):
        return None
    row = connection.exec_driver_sql(
        "SELECT seq FROM %s WHERE name = ?" % _sequence_table(schema),
        (table_name,),
    ).first()
    return int(row[0]) if row is not None else None


def restore_sequence(
    connection: "Connection",
    table_name: str,
    value: int,
    schema: Optional[str] = None,
) -> None:
    """Ensure that *table_name* never allocates a number at or below
    *value* again.

    This only updates ``sqlite_sequence``; no business row is inserted or
    kept in order to reserve the number.  The caller must run inside the
    transaction that owns the table rebuild so that a rollback restores the
    previous history.
    """

    current = read_sequence(connection, table_name, schema=schema)
    if current is not None and current >= value:
        return
    if current is None:
        connection.exec_driver_sql(
            "INSERT INTO %s (name, seq) VALUES (?, ?)"
            % _sequence_table(schema),
            (table_name, value),
        )
    else:
        connection.exec_driver_sql(
            "UPDATE %s SET seq = ? WHERE name = ?" % _sequence_table(schema),
            (value, table_name),
        )
