# mypy: allow-untyped-defs, allow-incomplete-defs, allow-untyped-calls
# mypy: no-warn-return-any, allow-any-generics

"""MigrateOperation classes for SQLite FTS5 full-text indexes."""

from __future__ import annotations

from typing import Sequence
from typing import Tuple
from typing import TYPE_CHECKING

from . import fts
from .base import Operations
from .ops import MigrateOperation
from .. import util

if TYPE_CHECKING:
    from .fts import FTSIndexSpec


@Operations.register_operation("create_fts_index")
class CreateFTSIndexOp(MigrateOperation):
    """Represent the creation of a managed SQLite FTS5 index.

    .. versionadded:: 1.18.0
    """

    def __init__(
        self,
        name: str,
        table_name: str,
        columns: Sequence[str],
        *,
        tokenize: str = fts.DEFAULT_TOKENIZER,
    ) -> None:
        self.spec: FTSIndexSpec = fts.FTSIndexSpec(
            name=name,
            table_name=table_name,
            columns=tuple(columns),
            tokenize=tokenize,
        )

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def table_name(self) -> str:
        return self.spec.table_name

    @property
    def columns(self) -> Tuple[str, ...]:
        return self.spec.columns

    @property
    def tokenize(self) -> str:
        return self.spec.tokenize

    def reverse(self) -> "DropFTSIndexOp":
        return DropFTSIndexOp(self.spec.name, _reverse_spec=self.spec)

    def to_diff_tuple(self):
        return ("add_fts_index", self.spec.to_decl())

    @classmethod
    def from_spec(cls, spec: "FTSIndexSpec") -> "CreateFTSIndexOp":
        return cls(
            spec.name,
            spec.table_name,
            list(spec.columns),
            tokenize=spec.tokenize,
        )

    @classmethod
    def create_fts_index(
        cls,
        operations: Operations,
        name: str,
        table_name: str,
        columns: Sequence[str],
        *,
        tokenize: str = fts.DEFAULT_TOKENIZER,
    ) -> None:
        r"""Create a managed SQLite FTS5 full-text index.

        Creates a standalone FTS5 virtual table named *name* over the
        given TEXT *columns* of the ordinary row table *table_name*,
        backfills it with every existing row and installs three
        synchronization triggers so that subsequent ``INSERT``,
        ``UPDATE`` and ``DELETE`` statements - including statements
        executed through independent connections - keep the index in
        sync.  A registry entry records the ownership and the exact
        declared definition.

        e.g.::

            op.create_fts_index(
                "article_search",
                "articles",
                ["title", "body"],
                tokenize="unicode61",
            )

        The index is queried directly with SQLite::

            SELECT rowid FROM "article_search"
            WHERE "article_search" MATCH :q

        :param name: public FTS5 virtual table name.  Must be unique
         database-wide and must not collide with an existing ordinary
         object.
        :param table_name: name of the content table, which must have
         exactly one ``INTEGER PRIMARY KEY`` column used as the FTS
         rowid.
        :param columns: ordered list of TEXT column names to index.
        :param tokenize: FTS5 tokenizer; ``"unicode61"`` (default) or
         ``"porter unicode61"``.

        Only supported against an online SQLite main database whose
        runtime library includes FTS5.  Offline ``--sql`` mode and other
        dialects are rejected.
        """
        op = cls(name, table_name, columns, tokenize=tokenize)
        return operations.invoke(op)


@Operations.register_operation("drop_fts_index")
class DropFTSIndexOp(MigrateOperation):
    """Represent the removal of a managed SQLite FTS5 index.

    .. versionadded:: 1.18.0
    """

    def __init__(
        self,
        name: str,
        *,
        _reverse_spec: "FTSIndexSpec | None" = None,
    ) -> None:
        if not isinstance(name, str) or not name:
            raise util.CommandError(
                "drop_fts_index() requires a non-empty index name."
            )
        self.name = name
        # the full spec is attached when the op is produced from a
        # known declaration (autogenerate / reverse()), so that a
        # rendered downgrade script stays self-describing.  At runtime
        # the registry is authoritative anyway.
        self._reverse_spec = _reverse_spec

    def reverse(self) -> CreateFTSIndexOp:
        if self._reverse_spec is None:
            raise util.CommandError(
                "Cannot reverse drop_fts_index(%r) without its original "
                "definition; reverse the corresponding "
                "CreateFTSIndexOp instead." % self.name
            )
        return CreateFTSIndexOp.from_spec(self._reverse_spec)

    def to_diff_tuple(self):
        if self._reverse_spec is not None:
            return ("remove_fts_index", self._reverse_spec.to_decl())
        return ("remove_fts_index", {"name": self.name})

    @classmethod
    def from_spec(cls, spec: "FTSIndexSpec") -> "DropFTSIndexOp":
        return cls(spec.name, _reverse_spec=spec)

    @classmethod
    def drop_fts_index(cls, operations: Operations, name: str) -> None:
        """Drop a managed FTS5 index previously created with
        :meth:`.Operations.create_fts_index`.

        The synchronization triggers, the FTS5 virtual table and its
        shadow tables, plus the registry entry are removed.  Objects are
        only removed when the registry proves Alembic owns them and they
        still match the recorded definition; otherwise an error naming
        the affected index is raised.
        """
        op = cls(name)
        return operations.invoke(op)
