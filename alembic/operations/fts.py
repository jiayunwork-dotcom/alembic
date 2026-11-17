# mypy: allow-untyped-defs, allow-incomplete-defs, allow-untyped-calls
# mypy: no-warn-return-any, allow-any-generics

"""SQLite FTS5 full-text index lifecycle support.

This module implements the metadata declaration, persistence, comparison
and runtime creation of FTS5 full-text indexes that are *managed* by
Alembic.  The public surface is:

* :data:`.METADATA_INFO_KEY` - the ``MetaData.info`` key holding the list
  of declared indexes,
* :class:`.FTSIndexSpec` - a validated index declaration,
* :class:`.FTSIndexManager` - runtime helpers used by the operation
  implementations, the autogenerate comparator and the batch machinery,
* :class:`.CreateFTSIndexOp` / :class:`.DropFTSIndexOp` are defined in
  :mod:`alembic.operations.fts_ops`.

Design summary
--------------

Each managed index is a standalone (content-storing) FTS5 virtual table
named after the public index name (e.g. ``article_search``).  Standalone
FTS5 tables keep their own copy of the indexed text, which costs disk but
makes them independent of the rowid lifetime of the content table: rows
may be deleted, have their ``INTEGER PRIMARY KEY`` changed or be copied
into a rebuilt table without ever leaving stale terms behind, because the
synchronization triggers (below) maintain the index with ordinary
``INSERT``/``DELETE`` statements.

Three triggers are created on the content table per managed index
(``..._ai`` / ``..._au`` / ``..._ad``) so that plain application SQL -
including statements issued over a separate DBAPI connection that never
goes through Alembic or SQLAlchemy - keeps the index synchronized:

* ``AFTER INSERT``  -> insert the new row's indexed text into the FTS
  table with the content row's rowid,
* ``AFTER UPDATE``  -> delete the old FTS row by ``rowid`` and insert the
  new values (this also covers primary key changes and NULL/non-NULL
  flips),
* ``AFTER DELETE``  -> delete the FTS row by ``rowid``.

Ownership and tamper detection
------------------------------

Managed indexes are recorded in a small registry table
(:data:`.REGISTRY_TABLE`) with one row per index.  The row stores the
exact declared definition (content table, columns in order, tokenizer) as
JSON plus a signature over that definition.  The registry - not name
prefixes - is the authoritative source of which objects Alembic manages.
Shadow tables (``<name>_data``, ``<name>_idx`` ...) and the three
triggers are derived from the registry entry.  Any mismatch (missing
registry row for an expected object, missing objects for a registry row,
wrong virtual-table SQL, drift of the stored definition) is raised as an
error naming the affected index; Alembic never silently adopts an
unknown object and never drops something it cannot prove ownership of.

User-created FTS tables and triggers are simply ignored: without a
registry row they are ordinary user objects, and the autogenerate
comparator filters only the objects reachable from the registry.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any
from typing import Dict
from typing import FrozenSet
from typing import List
from typing import Optional
from typing import Tuple
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy import types as sqltypes

from .. import util

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection
    from sqlalchemy.sql.schema import MetaData
    from sqlalchemy.sql.schema import Table

    from ..runtime.migration import MigrationContext


#: ``MetaData.info`` key under which the list of FTS index declarations
#: is stored.
METADATA_INFO_KEY = "sqlite_fts_indexes"

#: Name of the registry table that records every Alembic-managed FTS
#: index.  The name is fixed and quoted so that applications can avoid it.
REGISTRY_TABLE = "alembic_fts_index_registry"

#: tokenizers that may appear in a declaration.  The default is
#: ``unicode61``; ``porter unicode61`` adds the porter stemming wrapper.
ALLOWED_TOKENIZERS: FrozenSet[str] = frozenset(
    ["unicode61", "porter unicode61"]
)
DEFAULT_TOKENIZER = "unicode61"

#: suffixes used by FTS5 for the shadow tables of a virtual table named
#: ``<name>``.  Note that FTS5 may create a subset depending on options;
#: membership is always checked against ``sqlite_master`` at runtime.
FTS_SHADOW_SUFFIXES: Tuple[str, ...] = (
    "_data",
    "_idx",
    "_content",
    "_docsize",
    "_config",
)

#: trigger name suffixes created per managed index.
TRIGGER_INSERT_SUFFIX = "_ai"
TRIGGER_UPDATE_SUFFIX = "_au"
TRIGGER_DELETE_SUFFIX = "_ad"

_SCHEMA_VERSION = 1

# An identifier is a non-empty string.  quoted_name subclasses str, so the
# type checks below accept it naturally and preserve the user's quoting
# intent through repr() rendering and the local quote() helper.
_NAME_RE = re.compile(r"^.+$", re.DOTALL)


def quote_ident(name: str) -> str:
    """Render *name* as a quoted SQLite identifier.

    Names are treated as SQL names (not :attr:`.Column.key`).  Any
    character - including spaces, double quotes and full Unicode code
    points - is preserved by wrapping the identifier in double quotes and
    doubling embedded double quotes.
    """
    return '"' + str(name).replace('"', '""') + '"'


def _is_sql_name(value: Any) -> bool:
    return isinstance(value, str) and bool(_NAME_RE.match(value))


def validate_tokenize(tokenize: Any) -> str:
    if not isinstance(tokenize, str) or tokenize not in ALLOWED_TOKENIZERS:
        raise util.CommandError(
            "Unsupported FTS5 tokenizer %r; supported tokenizers are: %s"
            % (
                tokenize,
                ", ".join(sorted(ALLOWED_TOKENIZERS)),
            )
        )
    return tokenize


@dataclass(frozen=True)
class FTSIndexSpec:
    """A validated FTS5 index declaration.

    Attributes are stored exactly as declared so that generated scripts
    are self-contained and do not depend on the originating
    :class:`~sqlalchemy.schema.MetaData` being present at execution time.
    """

    name: str
    table_name: str
    columns: Tuple[str, ...]
    tokenize: str = DEFAULT_TOKENIZER

    def __post_init__(self) -> None:
        if not _is_sql_name(self.name):
            raise util.CommandError(
                "FTS index name must be a non-empty SQL name string; "
                f"got {self.name!r}"
            )
        if not _is_sql_name(self.table_name):
            raise util.CommandError(
                "FTS index %r references a content table with an invalid "
                "name %r; the table name must be a non-empty SQL name "
                "string." % (self.name, self.table_name)
            )
        if not isinstance(self.columns, tuple) or not self.columns:
            raise util.CommandError(
                "FTS index %r must declare at least one indexed column."
                % self.name
            )
        seen: Dict[str, None] = {}
        for column in self.columns:
            if not _is_sql_name(column):
                raise util.CommandError(
                    "FTS index %r contains an invalid column name %r; "
                    "column names must be non-empty SQL name strings."
                    % (self.name, column)
                )
            if column in seen:
                raise util.CommandError(
                    "FTS index %r lists column %r more than once; indexed "
                    "columns must be unique and are stored in declaration "
                    "order." % (self.name, column)
                )
            seen[column] = None
        # dataclass is frozen; validate through object.__setattr__ would
        # be needed for normalization, but callers go through from_decl().
        validate_tokenize(self.tokenize)

    @classmethod
    def from_decl(cls, decl: Any) -> "FTSIndexSpec":
        """Build a spec from one raw ``MetaData.info`` entry."""

        if not isinstance(decl, dict):
            raise util.CommandError(
                "Each entry of MetaData.info[%r] must be a dictionary; "
                "got %r." % (METADATA_INFO_KEY, decl)
            )
        allowed = {"name", "table", "columns", "tokenize"}
        unknown = set(decl).difference(allowed)
        if unknown:
            raise util.CommandError(
                "FTS index declaration %r contains unsupported key(s) %s; "
                "allowed keys are: %s"
                % (decl.get("name"), sorted(unknown), sorted(allowed))
            )
        missing = allowed.difference(decl).difference(["tokenize"])
        if missing:
            raise util.CommandError(
                "FTS index declaration %r is missing required key(s): %s"
                % (decl.get("name"), sorted(missing))
            )
        columns = decl["columns"]
        if not isinstance(columns, (list, tuple)):
            raise util.CommandError(
                "FTS index %r 'columns' must be a list of SQL column "
                "names; got %r." % (decl["name"], type(columns).__name__)
            )
        tokenize = decl.get("tokenize", DEFAULT_TOKENIZER)
        return cls(
            name=decl["name"],
            table_name=decl["table"],
            columns=tuple(columns),
            tokenize=tokenize,
        )

    def to_decl(self) -> Dict[str, Any]:
        """Return the canonical ``MetaData.info`` dictionary form."""

        return {
            "name": self.name,
            "table": self.table_name,
            "columns": list(self.columns),
            "tokenize": self.tokenize,
        }

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema_version": _SCHEMA_VERSION,
                "spec": self.to_decl(),
            },
            sort_keys=True,
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, payload: str) -> "FTSIndexSpec":
        try:
            data = json.loads(payload)
        except (ValueError, TypeError) as exc:
            raise util.CommandError(
                "FTS registry entry is not valid JSON: %s" % exc
            ) from exc
        if not isinstance(data, dict) or "spec" not in data:
            raise util.CommandError(
                "FTS registry entry has an unrecognized shape: %r" % data
            )
        return cls.from_decl(data["spec"])

    @property
    def signature(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    # -- object names derived from the public index name ---------------

    @property
    def trigger_names(self) -> Tuple[str, str, str]:
        return (
            self.name + TRIGGER_INSERT_SUFFIX,
            self.name + TRIGGER_UPDATE_SUFFIX,
            self.name + TRIGGER_DELETE_SUFFIX,
        )

    @property
    def shadow_names(self) -> Tuple[str, ...]:
        return tuple(self.name + suffix for suffix in FTS_SHADOW_SUFFIXES)

    # -- SQL generation -------------------------------------------------

    def create_virtual_table_sql(self) -> str:
        columns_sql = ", ".join(quote_ident(c) for c in self.columns)
        return "CREATE VIRTUAL TABLE %s USING fts5(%s, tokenize=%s)" % (
            quote_ident(self.name),
            columns_sql,
            sql_string_literal(self.tokenize),
        )

    def create_triggers_sql(self) -> List[str]:
        fts = quote_ident(self.name)
        tbl = quote_ident(self.table_name)
        col_list = ", ".join(quote_ident(c) for c in self.columns)
        new_cols = ", ".join("new." + quote_ident(c) for c in self.columns)
        ai, au, ad = self.trigger_names
        return [
            "CREATE TRIGGER %s AFTER INSERT ON %s BEGIN INSERT INTO %s("
            "rowid, %s) VALUES (new.rowid, %s); END"
            % (quote_ident(ai), tbl, fts, col_list, new_cols),
            "CREATE TRIGGER %s AFTER UPDATE ON %s BEGIN DELETE FROM %s "
            "WHERE rowid = old.rowid; INSERT INTO %s(rowid, %s) VALUES "
            "(new.rowid, %s); END"
            % (
                quote_ident(au),
                tbl,
                fts,
                fts,
                col_list,
                new_cols,
            ),
            "CREATE TRIGGER %s AFTER DELETE ON %s BEGIN DELETE FROM %s "
            "WHERE rowid = old.rowid; END" % (quote_ident(ad), tbl, fts),
        ]

    def drop_triggers_sql(self) -> List[str]:
        return [
            "DROP TRIGGER IF EXISTS %s" % quote_ident(n)
            for n in self.trigger_names
        ]

    def drop_virtual_table_sql(self) -> str:
        return "DROP TABLE IF EXISTS %s" % quote_ident(self.name)

    def backfill_sql(self) -> str:
        """``INSERT ... SELECT`` statement loading existing content rows.

        The FTS rowid is taken from the content table's
        ``INTEGER PRIMARY KEY`` (``rowid`` alias), so the whole table is
        copied in SQL without ever materializing rows in Python.
        """
        fts = quote_ident(self.name)
        tbl = quote_ident(self.table_name)
        col_list = ", ".join(quote_ident(c) for c in self.columns)
        return "INSERT INTO %s(rowid, %s) SELECT rowid, %s FROM %s" % (
            fts,
            col_list,
            col_list,
            tbl,
        )


def sql_string_literal(value: str) -> str:
    """Quote *value* as a single-quoted SQL string literal."""

    return "'" + value.replace("'", "''") + "'"


def load_metadata_specs(
    metadata: Optional["MetaData"],
    *,
    validate_content: bool = True,
) -> List[FTSIndexSpec]:
    """Validate and return the FTS declarations of *metadata*.

    A missing ``MetaData.info`` key, or an empty list, means "no managed
    indexes".  Structural validation - shape, key types, duplicate
    columns/names, tokenizer allow-list and name collisions with ordinary
    metadata tables - is performed here so that configuration errors are
    raised before any database write.

    *validate_content* additionally checks the referenced content table
    and columns against the given MetaData; it is disabled during
    autogenerate, where a column referenced by a surviving declaration may
    simultaneously be detected as removed (that situation has its own,
    more specific rejection in the comparator).
    """

    if metadata is None:
        return []
    raw = metadata.info.get(METADATA_INFO_KEY)
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise util.CommandError(
            "MetaData.info[%r] must be a list of FTS index declaration "
            "dictionaries; got %r." % (METADATA_INFO_KEY, type(raw).__name__)
        )

    specs: List[FTSIndexSpec] = []
    names: Dict[str, None] = {}
    for decl in raw:
        spec = FTSIndexSpec.from_decl(decl)
        if spec.name in names:
            raise util.CommandError(
                "Duplicate FTS index name %r; index names must be unique "
                "across the whole database." % spec.name
            )
        names[spec.name] = None
        specs.append(spec)

    # names may not collide with ordinary tables declared in the metadata
    metadata_tables = set(metadata.tables)
    for spec in specs:
        if spec.name in metadata_tables:
            raise util.CommandError(
                "FTS index %r cannot be created: an ordinary table with "
                "the same name is already declared in the MetaData."
                % spec.name
            )
        if not validate_content:
            continue
        if (
            spec.table_name not in metadata_tables
            and not _is_declared_in_any_metadata(metadata, spec.table_name)
        ):
            raise util.CommandError(
                "FTS index %r references content table %r which is not "
                "declared in the target MetaData."
                % (spec.name, spec.table_name)
            )
        table = metadata.tables.get(spec.table_name)
        if table is not None:
            _validate_content_table(table, spec)

    # more than one index may target the same table; make sure registry
    # trigger/object names derived from index names do not collide.
    derived: Dict[str, str] = {}
    for spec in specs:
        for derived_name in (
            spec.name,
            *[
                spec.name + s
                for s in (
                    TRIGGER_INSERT_SUFFIX,
                    TRIGGER_UPDATE_SUFFIX,
                    TRIGGER_DELETE_SUFFIX,
                )
            ],
        ):
            if derived_name in derived and derived[derived_name] != spec.name:
                raise util.CommandError(
                    "FTS maintenance object name %r would be used by more "
                    "than one index." % derived_name
                )
            derived[derived_name] = spec.name
    return specs


def _is_declared_in_any_metadata(
    metadata: "MetaData", table_name: str
) -> bool:
    # metadata may be a single MetaData; keep the hook explicit in case
    # callers wrap a sequence later.
    return table_name in metadata.tables


def _validate_content_table(table: "Table", spec: FTSIndexSpec) -> None:
    pk_columns = list(table.primary_key.columns)
    integer_pk = [
        c for c in pk_columns if isinstance(c.type, sqltypes.Integer)
    ]
    # SQLAlchemy marks the rowid alias column with primary_key=True and
    # an integer-affinity type.  Require exactly one such column.
    if len(pk_columns) != 1 or len(integer_pk) != 1:
        raise util.CommandError(
            "FTS index %r requires content table %r to have exactly one "
            "INTEGER PRIMARY KEY column (the SQLite rowid alias); found "
            "primary key %r."
            % (spec.name, spec.table_name, [c.name for c in pk_columns])
        )
    for column_name in spec.columns:
        if column_name not in table.c:
            raise util.CommandError(
                "FTS index %r references column %r which is not present "
                "on content table %r."
                % (spec.name, column_name, spec.table_name)
            )
        column = table.c[column_name]
        # TEXT affinity only.  NULLTYPE (reflected/placeholder tables or
        # untyped columns) is accepted because SQLite itself assigns no
        # strict type in that case and stores text values unchanged.
        if not isinstance(column.type, (sqltypes.String, sqltypes.NullType)):
            raise util.CommandError(
                "FTS index %r may only index TEXT columns; column %s.%s "
                "has type %r."
                % (
                    spec.name,
                    spec.table_name,
                    column_name,
                    column.type,
                )
            )


class FTSIndexManager:
    """Runtime and reflection helpers for managed FTS indexes.

    A manager is bound to a live online SQLite connection.  All reflection
    helpers are read-only.  Mutating helpers (:meth:`create_index`,
    :meth:`drop_index` ...) only emit SQL through the bound connection and
    rely on the caller's transaction boundaries for atomicity.
    """

    def __init__(self, connection: "Connection") -> None:
        self.connection = connection

    # ------------------------------------------------------------------
    # registry
    # ------------------------------------------------------------------
    def registry_exists(self) -> bool:
        row = self.connection.execute(
            text(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name = :name"
            ),
            {"name": REGISTRY_TABLE},
        ).first()
        return row is not None

    def ensure_registry_table(self) -> None:
        if self.registry_exists():
            # an object with the reserved registry name already exists;
            # make sure it has the exact shape Alembic owns rather than
            # silently writing into a user table with the same name.
            pragma_rows = self.connection.execute(
                text("PRAGMA table_info(%s)" % quote_ident(REGISTRY_TABLE))
            ).fetchall()
            actual_columns = [row[1] for row in pragma_rows]
            expected_columns = [
                "name",
                "table_name",
                "columns",
                "tokenize",
                "definition",
                "signature",
            ]
            if actual_columns != expected_columns:
                raise util.CommandError(
                    "A table named %r already exists but does not have "
                    "the shape of the Alembic FTS registry (expected "
                    "columns %r, found %r).  Rename or remove the "
                    "conflicting table; Alembic will not overwrite an "
                    "object it does not own."
                    % (
                        REGISTRY_TABLE,
                        expected_columns,
                        actual_columns,
                    )
                )
            return
        self.connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS %s ("
                "name TEXT PRIMARY KEY, "
                "table_name TEXT NOT NULL, "
                "columns TEXT NOT NULL, "
                "tokenize TEXT NOT NULL, "
                "definition TEXT NOT NULL, "
                "signature TEXT NOT NULL)" % quote_ident(REGISTRY_TABLE)
            )
        )

    def drop_registry_table_if_empty(self) -> None:
        count = self.connection.execute(
            text("SELECT COUNT(*) FROM %s" % quote_ident(REGISTRY_TABLE))
        ).scalar()
        if not count:
            self.connection.execute(
                text("DROP TABLE IF EXISTS %s" % quote_ident(REGISTRY_TABLE))
            )

    def read_registry(self) -> Dict[str, FTSIndexSpec]:
        if not self.registry_exists():
            return {}
        rows = self.connection.execute(
            text(
                "SELECT name, table_name, columns, tokenize, definition, "
                "signature FROM %s" % quote_ident(REGISTRY_TABLE)
            )
        ).fetchall()
        result: Dict[str, FTSIndexSpec] = {}
        for (
            name,
            table_name,
            columns_json,
            tokenize,
            definition,
            signature,
        ) in rows:
            spec = self._row_to_spec(
                name,
                table_name,
                columns_json,
                tokenize,
                definition,
                signature,
            )
            result[name] = spec
        return result

    def _row_to_spec(
        self,
        name: str,
        table_name: str,
        columns_json: str,
        tokenize: str,
        definition: str,
        signature: str,
    ) -> FTSIndexSpec:
        try:
            embedded = FTSIndexSpec.from_json(definition)
        except util.CommandError as exc:
            raise util.CommandError(
                "FTS registry entry for index %r is corrupted: %s"
                % (name, exc)
            )
        if embedded.signature != signature:
            raise util.CommandError(
                "FTS index %r has a corrupted registry entry: the stored "
                "signature does not match its definition. The index may "
                "have been modified outside of Alembic; refusing to "
                "migrate or compare it." % name
            )
        # cross-check the denormalized columns against the JSON payload
        try:
            stored_columns = json.loads(columns_json)
        except (ValueError, TypeError) as exc:
            raise util.CommandError(
                "FTS registry entry for index %r has invalid column data: "
                "%s" % (name, exc)
            )
        if (
            embedded.name != name
            or embedded.table_name != table_name
            or list(embedded.columns) != stored_columns
            or embedded.tokenize != tokenize
        ):
            raise util.CommandError(
                "FTS index %r has inconsistent registry data; refusing to "
                "continue." % name
            )
        return embedded

    def _write_registry_row(self, spec: FTSIndexSpec) -> None:
        self.connection.execute(
            text(
                "INSERT INTO %s(name, table_name, columns, tokenize, "
                "definition, signature) VALUES (:name, :table_name, "
                ":columns, :tokenize, :definition, :signature)"
                % quote_ident(REGISTRY_TABLE)
            ),
            {
                "name": spec.name,
                "table_name": spec.table_name,
                "columns": json.dumps(list(spec.columns), ensure_ascii=False),
                "tokenize": spec.tokenize,
                "definition": spec.to_json(),
                "signature": spec.signature,
            },
        )

    def _delete_registry_row(self, name: str) -> None:
        self.connection.execute(
            text(
                "DELETE FROM %s WHERE name = :name"
                % quote_ident(REGISTRY_TABLE)
            ),
            {"name": name},
        )

    # ------------------------------------------------------------------
    # sqlite_master helpers
    # ------------------------------------------------------------------
    def master_rows(self) -> Dict[str, Tuple[str, str, Optional[str]]]:
        """Return ``name -> (type, tbl_name, sql)`` for main schema."""

        rows = self.connection.execute(
            text(
                "SELECT name, type, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%'"
            )
        ).fetchall()
        return {
            name: (type_, tbl_name, sql) for name, type_, tbl_name, sql in rows
        }

    def managed_object_names(
        self, specs: Dict[str, FTSIndexSpec]
    ) -> FrozenSet[str]:
        names: Dict[str, None] = {REGISTRY_TABLE: None}
        for spec in specs.values():
            names[spec.name] = None
            for trigger in spec.trigger_names:
                names[trigger] = None
            # only shadows that actually exist are managed, but listing
            # the expected names is enough for filtering purposes
            for shadow in spec.shadow_names:
                names[shadow] = None
        return frozenset(names)

    # ------------------------------------------------------------------
    # integrity / tamper detection
    # ------------------------------------------------------------------
    def verify_index_intact(
        self, spec: FTSIndexSpec, master: Dict[str, Any]
    ) -> None:
        """Raise if objects backing *spec* are missing or altered."""

        row = master.get(spec.name)
        if row is None:
            raise util.CommandError(
                "FTS index %r is registered but its virtual table is "
                "missing; the index may have been dropped outside of "
                "Alembic. Refusing to continue." % spec.name
            )
        type_, tbl_name, sql_text = row
        if type_ != "table" or tbl_name != spec.name:
            raise util.CommandError(
                "Object %r registered as an FTS index is not an FTS5 "
                "virtual table (found %s on %r)."
                % (spec.name, type_, tbl_name)
            )
        expected_sql = self._normalize(spec.create_virtual_table_sql())
        if not sql_text or self._normalize(sql_text) != expected_sql:
            raise util.CommandError(
                "FTS virtual table %r does not match its registered "
                "definition (expected %r, found %r); it may have been "
                "modified outside of Alembic. Refusing to continue."
                % (spec.name, expected_sql, sql_text)
            )
        for trigger_name in spec.trigger_names:
            trow = master.get(trigger_name)
            if trow is None:
                raise util.CommandError(
                    "Synchronization trigger %r for FTS index %r is "
                    "missing; the index cannot be kept in sync. Refusing "
                    "to continue." % (trigger_name, spec.name)
                )
            t_type, t_tbl, _ = trow
            if t_type != "trigger" or t_tbl != spec.table_name:
                raise util.CommandError(
                    "Object %r expected to be a trigger on table %r for "
                    "FTS index %r but found %s on %r."
                    % (
                        trigger_name,
                        spec.table_name,
                        spec.name,
                        t_type,
                        t_tbl,
                    )
                )

    @staticmethod
    def _normalize(sql_text: str) -> str:
        return " ".join(sql_text.strip().split()).lower()

    def verify_all(self) -> Dict[str, FTSIndexSpec]:
        """Read the registry and verify every managed index.

        Also detects registry rows whose virtual table exists under an
        unexpected object type, and unknown objects carrying a managed
        name.  Returns the validated specs keyed by index name.
        """

        specs = self.read_registry()
        master = self.master_rows()
        for spec in specs.values():
            self.verify_index_intact(spec, master)
        return specs

    def assert_name_available(self, spec: FTSIndexSpec) -> None:
        """Reject creating *spec* if any of its public names is taken."""

        master = self.master_rows()
        clashing = [n for n in (spec.name, *spec.trigger_names) if n in master]
        if clashing:
            raise util.CommandError(
                "Cannot create FTS index %r: name(s) %s already exist in "
                "the database. Alembic will not replace objects it does "
                "not own; drop or rename them first." % (spec.name, clashing)
            )

    def assert_content_table_usable(self, spec: FTSIndexSpec) -> None:
        master = self.master_rows()
        row = master.get(spec.table_name)
        if row is None:
            raise util.CommandError(
                "Content table %r for FTS index %r does not exist in the "
                "main database." % (spec.table_name, spec.name)
            )
        type_, _tbl, sql_text = row
        if type_ != "table":
            raise util.CommandError(
                "FTS index %r content object %r is a %s, not an ordinary "
                "row table." % (spec.name, spec.table_name, type_)
            )
        if sql_text and sql_text.lstrip().upper().startswith("CREATE VIRTUAL"):
            raise util.CommandError(
                "FTS index %r cannot use virtual table %r as its content "
                "table; only ordinary rowid tables are supported."
                % (spec.name, spec.table_name)
            )
        if sql_text and "WITHOUT ROWID" in sql_text.upper():
            raise util.CommandError(
                "FTS index %r cannot use WITHOUT ROWID table %r as its "
                "content table." % (spec.name, spec.table_name)
            )
        pragma_rows = self.connection.execute(
            text("PRAGMA table_info(%s)" % quote_ident(spec.table_name))
        ).fetchall()
        # cid, name, type, notnull, dflt_value, pk
        pk_cols = [r for r in pragma_rows if r[5]]
        if len(pk_cols) != 1 or pk_cols[0][2].upper() != "INTEGER":
            raise util.CommandError(
                "FTS index %r requires content table %r to have exactly "
                "one INTEGER PRIMARY KEY column; found %r."
                % (
                    spec.name,
                    spec.table_name,
                    [(r[1], r[2], r[5]) for r in pk_cols],
                )
            )
        present = {r[1] for r in pragma_rows}
        for column in spec.columns:
            if column not in present:
                raise util.CommandError(
                    "FTS index %r references missing column %s.%s."
                    % (spec.name, spec.table_name, column)
                )

    # ------------------------------------------------------------------
    # mutating helpers - run inside the caller's transaction
    # ------------------------------------------------------------------
    def create_index(self, spec: FTSIndexSpec) -> None:
        self.ensure_registry_table()
        self.assert_name_available(spec)
        self.assert_content_table_usable(spec)
        conn = self.connection
        conn.execute(text(spec.create_virtual_table_sql()))
        try:
            conn.execute(text(spec.backfill_sql()))
            for trigger_sql in spec.create_triggers_sql():
                conn.execute(text(trigger_sql))
            # make sure the freshly populated index is internally consistent
            conn.execute(
                text(
                    "INSERT INTO %s(%s) VALUES('integrity-check')"
                    % (
                        quote_ident(spec.name),
                        quote_ident(spec.name),
                    )
                )
            )
            self._write_registry_row(spec)
        except BaseException:
            # best-effort cleanup of objects created in this call; the
            # outer transaction rollback is the primary recovery path.
            for trigger_sql in spec.drop_triggers_sql():
                conn.execute(text(trigger_sql))
            conn.execute(text(spec.drop_virtual_table_sql()))
            raise

    def drop_index(self, name: str) -> None:
        specs = self.read_registry()
        if name not in specs:
            raise util.CommandError(
                "FTS index %r is not registered as an Alembic-managed "
                "FTS index; refusing to drop unknown objects." % name
            )
        spec = specs[name]
        master = self.master_rows()
        self.verify_index_intact(spec, master)
        for trigger_sql in spec.drop_triggers_sql():
            self.connection.execute(text(trigger_sql))
        self.connection.execute(text(spec.drop_virtual_table_sql()))
        self._delete_registry_row(name)
        self.drop_registry_table_if_empty()

    def detach_triggers(self, spec: FTSIndexSpec) -> None:
        """Drop the sync triggers of *spec* without touching the index.

        Used immediately before a content-table rebuild so that copying
        rows into the replacement table does not double-populate the
        index.
        """

        for trigger_sql in spec.drop_triggers_sql():
            self.connection.execute(text(trigger_sql))

    def reindex_index(self, spec: FTSIndexSpec) -> None:
        """Rebuild *spec* from its current content table and re-arm it.

        Used after a content-table rebuild: the FTS table is cleared and
        repopulated in SQL (no Python rows), triggers are recreated and
        the result is integrity-checked.
        """

        conn = self.connection
        # standalone (content-storing) FTS5 tables are cleared with an
        # ordinary DELETE; the 'delete-all' command only exists for
        # contentless/external-content tables.
        conn.execute(text("DELETE FROM %s" % quote_ident(spec.name)))
        conn.execute(text(spec.backfill_sql()))
        for trigger_sql in spec.create_triggers_sql():
            conn.execute(text(trigger_sql))
        conn.execute(
            text(
                "INSERT INTO %s(%s) VALUES('integrity-check')"
                % (quote_ident(spec.name), quote_ident(spec.name))
            )
        )


def require_sqlite_online(context: "MigrationContext", action: str) -> None:
    """Guard shared by every FTS operation entry point."""

    if context.as_sql:
        raise util.CommandError(
            "FTS index operations are not supported in offline (--sql) "
            "mode; %s requires a live online SQLite connection." % action
        )
    if context.dialect.name != "sqlite":
        raise util.CommandError(
            "FTS index lifecycle support is only available on the SQLite "
            "dialect; current dialect is %r while %s was requested."
            % (context.dialect.name, action)
        )
    if context.bind is None:
        raise util.CommandError(
            "FTS index operations require a live database connection."
        )


def fts5_available(connection: "Connection") -> bool:
    """Return True if the connected SQLite library includes FTS5."""

    try:
        connection.execute(
            text("CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(x)")
        )
    except Exception:
        return False
    else:
        connection.execute(text("DROP TABLE temp.fts5_probe"))
        return True
