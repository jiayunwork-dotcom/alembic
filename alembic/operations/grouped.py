# mypy: allow-untyped-defs, allow-incomplete-defs, allow-untyped-calls
# mypy: no-warn-return-any, allow-any-generics

"""SQLite grouped ("multi-table") batch migration support.

This module implements the ``op.sqlite_batch_group()`` context manager.
Unlike per-table :meth:`.Operations.batch_alter_table` blocks, which rebuild
one table at a time and therefore require the application to disable
``foreign_keys`` manually, a grouped batch rebuilds every table of the
group together inside a single transaction while foreign key enforcement is
temporarily disabled.  This makes foreign-key cycles, self-referential
constraints and tables referenced from outside the group safe to migrate.

The design deliberately reuses the existing per-table rebuild machinery
(:class:`.BatchOperationsImpl` / :class:`.ApplyBatchImpl`); the group is
responsible for:

* collecting the per-table plans without flushing them,
* validating the whole group before anything is changed,
* owning the transaction and the ``PRAGMA foreign_keys`` switch,
* flushing the individual plans in a controlled order,
* running ``PRAGMA foreign_key_check`` before the transaction is committed.

Rollback reliability, temporary disk usage and lock-hold trade-offs are
documented in :doc:`/batch` and in the docstring of
:meth:`.Operations.sqlite_batch_group`.
"""

from __future__ import annotations

from contextlib import contextmanager
import re
from typing import Any
from typing import Dict
from typing import Iterator
from typing import List
from typing import Optional
from typing import TYPE_CHECKING

from sqlalchemy import event
from sqlalchemy import ForeignKeyConstraint
from sqlalchemy import inspect
from sqlalchemy import text

from .batch import ApplyBatchImpl
from .batch import BatchOperationsImpl
from .. import util

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

    from .base import Operations
    from ..ddl.impl import DefaultImpl
    from ..runtime.migration import MigrationContext


#: batch operations that may appear inside an ``sqlite_batch_group()`` block.
#: ``add_constraint`` is additionally restricted to foreign key constraints
#: (matching the public ``batch_op.create_foreign_key()`` directive).
_GROUPED_ALLOWED_OPS = frozenset(
    [
        "alter_column",
        "add_column",
        "drop_column",
        "add_constraint",
        "drop_constraint",
        "create_index",
        "drop_index",
        "set_table_autoincrement",
    ]
)

#: statements that mutate database state, used by the write-tracking guard
#: that prevents a migration from performing un-atomic writes around a group.
_WRITE_PREFIXES = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "CREATE",
    "DROP",
    "ALTER",
    "REPLACE",
    "PRAGMA",
    "REINDEX",
    "VACUUM",
    "ATTACH",
    "DETACH",
    "SAVEPOINT",
    "RELEASE",
)

_TMP_PREFIX = "_alembic_group_tmp_"


class SQLiteBatchGroup:
    """Coordinates the collected per-table batch plans of one group."""

    def __init__(
        self,
        operations: "Operations",
        temp_prefix: Optional[str] = None,
    ) -> None:
        self.operations = operations
        self.context: "MigrationContext" = operations.migration_context
        self.impl: "DefaultImpl" = self.context.impl
        self.connection: "Connection" = self.context.bind  # type: ignore
        self._plans: Dict[str, BatchOperationsImpl] = OrderedTablePlans()
        self._temp_prefix = temp_prefix or _TMP_PREFIX

        # the autocommit wrapper connection while the group is active;
        # ``None`` means the group has not started yet.
        self._autocommit_connection: Optional["Connection"] = None
        self._base_connection: "Connection" = self.connection
        self._saved_isolation_level: Optional[str] = None
        self._saved_foreign_keys: Optional[int] = None
        self._finalized = False
        self._restored = False
        self._rolled_back = False
        self._revision_proxy: Optional[Any] = None

    # ------------------------------------------------------------------
    # plan collection
    # ------------------------------------------------------------------
    def register(self, batch_impl: BatchOperationsImpl) -> None:
        """Register one per-table batch plan within this group.

        Static, database-independent validation happens immediately so
        that unsupported directives are rejected while the migration is
        still being collected and before anything is changed.
        """

        table_name = batch_impl.table_name
        if table_name in self._plans:
            raise util.CommandError(
                f"Table '{table_name}' is included more than once in the "
                "same sqlite_batch_group(); each table may only be rebuilt "
                "by one batch_alter_table() block per group."
            )
        if batch_impl.copy_from is not None:
            raise util.CommandError(
                "copy_from is not supported inside "
                "sqlite_batch_group(); grouped migrations always "
                f"reflect table '{table_name}' from the live database."
            )
        if batch_impl.recreate == "never":
            raise util.CommandError(
                "recreate='never' is not supported inside "
                "sqlite_batch_group()."
            )
        self._plans[table_name] = batch_impl

    def _validate_plan_operations(
        self, batch_impl: BatchOperationsImpl
    ) -> None:
        for opname, arg, kw in batch_impl.batch:
            if opname not in _GROUPED_ALLOWED_OPS:
                raise util.CommandError(
                    f"Operation '{opname}' on table "
                    f"'{batch_impl.table_name}' is not supported inside "
                    "sqlite_batch_group(); supported operations are: "
                    + ", ".join(sorted(_GROUPED_ALLOWED_OPS))
                )
            if opname == "add_constraint":
                const = arg[0]
                if not isinstance(const, ForeignKeyConstraint):
                    raise util.CommandError(
                        "Only foreign key constraints may be created inside "
                        "sqlite_batch_group() (use "
                        "batch_op.create_foreign_key()); got "
                        f"{const.__class__.__name__} on table "
                        f"'{batch_impl.table_name}'."
                    )

    # ------------------------------------------------------------------
    # pre-flight validation, all read-only
    # ------------------------------------------------------------------
    def _preflight(self) -> None:
        # static validation of the collected plans first; this runs before
        # any mutating statement because the group body only collected
        # plans.
        for batch_impl in self._plans.values():
            self._validate_plan_operations(batch_impl)

        inspector = inspect(self.connection)
        view_names = set(inspector.get_view_names())

        master_rows = self.connection.execute(
            text(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%'"
            )
        ).fetchall()

        triggers_by_table: Dict[str, List[str]] = {}
        virtual_tables = set()
        view_sql: Dict[str, str] = {}
        for type_, name, tbl_name, sql_text in master_rows:
            if type_ == "trigger":
                triggers_by_table.setdefault(tbl_name, []).append(name)
            elif type_ == "table" and sql_text is not None:
                if sql_text.lstrip().upper().startswith("CREATE VIRTUAL"):
                    virtual_tables.add(name)
            elif type_ == "view":
                view_sql[name] = sql_text or ""

        existing_table_names = {
            row[1] for row in master_rows if row[0] == "table"
        }

        for table_name, batch_impl in self._plans.items():
            if batch_impl.schema is not None:
                raise util.CommandError(
                    "sqlite_batch_group() only supports tables in the main "
                    f"database; table '{table_name}' declares schema "
                    f"'{batch_impl.schema}'."
                )
            if table_name not in existing_table_names:
                raise util.CommandError(
                    f"Table '{table_name}' referenced by "
                    "batch_alter_table() inside sqlite_batch_group() does "
                    "not exist in the main database."
                )
            if table_name in virtual_tables:
                raise util.CommandError(
                    f"Table '{table_name}' is a virtual table; virtual "
                    "tables cannot be rebuilt inside sqlite_batch_group()."
                )
            if table_name in triggers_by_table:
                raise util.CommandError(
                    f"Table '{table_name}' has SQLite trigger(s) "
                    f"{triggers_by_table[table_name]}; tables with "
                    "triggers cannot be rebuilt inside "
                    "sqlite_batch_group(). Drop and recreate the trigger "
                    "outside of the group instead."
                )
            self._check_view_dependencies(
                table_name, view_names, view_sql
            )

        # make sure every temporary name we are going to use is free and
        # unique across the group.
        used_temp_names = set()
        for table_name in self._plans:
            temp_name = self._temp_name(table_name)
            if temp_name in existing_table_names or temp_name in used_temp_names:
                raise util.CommandError(
                    f"Temporary table name '{temp_name}' required for the "
                    "grouped migration already exists; cannot proceed "
                    "safely."
                )
            used_temp_names.add(temp_name)

    def _check_view_dependencies(
        self,
        table_name: str,
        view_names: set,
        view_sql: Dict[str, str],
    ) -> None:
        # match the table name as an identifier: plain occurrence, or a
        # double-quoted / back-tick / square-bracket quoted occurrence.
        escaped = re.escape(table_name)
        pattern = re.compile(
            r"(?<![A-Za-z0-9_\"`\[])(?:"
            r"%s|\"%s\"|`%s`|\[%s\])"
            r"(?![A-Za-z0-9_\"`\]])"
            % (escaped, escaped, escaped, escaped),
            re.IGNORECASE,
        )
        for view_name in view_names:
            if pattern.search(view_sql.get(view_name, "")):
                raise util.CommandError(
                    f"View '{view_name}' depends on table '{table_name}'; "
                    "views and other schema objects that depend on a table "
                    "rebuilt inside sqlite_batch_group() must be dropped "
                    "before the group and recreated afterwards."
                )

    def _temp_name(self, table_name: str) -> str:
        return ("%s%s" % (self._temp_prefix, table_name))[0:50]

    # ------------------------------------------------------------------
    # transaction / foreign key ownership
    # ------------------------------------------------------------------
    def begin(self) -> None:
        """Validate the group, switch FKs off and open the group
        transaction.

        All validation that requires database access happens here, before
        the first mutating statement is emitted.
        """
        ctx = self.context

        if ctx.as_sql:
            raise util.CommandError(
                "sqlite_batch_group() is not supported in offline (--sql) "
                "mode; grouped batch migrations require a live database "
                "connection."
            )
        if ctx.dialect.name != "sqlite":
            raise util.CommandError(
                "sqlite_batch_group() is only supported on the SQLite "
                f"dialect; current dialect is '{ctx.dialect.name}'."
            )
        if not ctx._transaction_per_migration:
            raise util.CommandError(
                "sqlite_batch_group() requires "
                "transaction_per_migration=True to be configured on the "
                "MigrationContext so that the group transaction also "
                "covers the alembic_version update."
            )
        if ctx._in_external_transaction:
            raise util.CommandError(
                "sqlite_batch_group() requires that no transaction be "
                "active when run_migrations() is entered; the calling "
                "environment opened a transaction before running "
                "migrations. Remove the outer context.begin_transaction()/"
                "connection.begin() block."
            )
        if ctx.sqlite_batch_group is not None:
            raise util.CommandError(
                "sqlite_batch_group() cannot be nested, and only one group "
                "per migration revision is allowed."
            )
        if ctx._sqlite_group_finalized:
            raise util.CommandError(
                "Only one sqlite_batch_group() may be used per migration "
                "revision; a group has already been finalized in this "
                "revision."
            )
        if ctx._sqlite_user_writes:
            raise util.CommandError(
                "sqlite_batch_group() must be the first statement that "
                "changes the database in a revision; statements executed "
                "before the group cannot be rolled back together with it."
            )

        # Take over the revision transaction opened by run_migrations().
        # The proxy is restored (not committed) while the group runs, so
        # that its commit()/rollback() drive the group transaction instead
        # of a separate SQLAlchemy-level transaction.
        self._revision_proxy = ctx._transaction
        ctx._transaction = None
        if self._revision_proxy is not None:
            # close the autobegun SQLAlchemy transaction; its contents so
            # far are read-only bookkeeping statements.
            self._revision_proxy.commit()

        # Any transaction open here is the per-revision proxy's autobegin
        # caused by read-only bookkeeping (including the pragma reads
        # below).  It must be closed before ``PRAGMA foreign_keys`` can be
        # changed.
        self._commit_any_open_transaction()

        self._saved_foreign_keys = int(
            self.connection.exec_driver_sql(
                "PRAGMA foreign_keys"
            ).scalar()
        )
        # the pragma select itself autobegot a transaction; close it as
        # well.
        self._commit_any_open_transaction()

        # pysqlite (and SQLite) can only change the foreign_keys pragma
        # outside of a transaction.  Use an AUTOCOMMIT wrapper so that the
        # pragma statement does not start a SQLAlchemy-level transaction.
        self._saved_isolation_level = (
            self._base_connection.get_isolation_level()
        )
        autocommit_connection = self._base_connection.execution_options(
            isolation_level="AUTOCOMMIT"
        )
        if self._saved_foreign_keys != 0:
            autocommit_connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            # the pragma may have autobegot on some drivers; make sure no
            # transaction is pending before issuing the real BEGIN.
            autocommit_connection.commit()

        # The group transaction is a real SQLite transaction started with
        # an explicit BEGIN through the DBAPI.  pysqlite's implicit
        # transaction handling opens transactions only after DML, which
        # would silently auto-commit the DDL statements of a table rebuild;
        # the explicit BEGIN makes DDL transactional as well.
        autocommit_connection.exec_driver_sql("BEGIN")

        self._autocommit_connection = autocommit_connection
        ctx.sqlite_batch_group = self
        # every DDL/DML of the group now runs through this connection,
        # including alembic_version bookkeeping, until commit_group()/
        # rollback().
        self.impl.connection = autocommit_connection
        ctx.connection = autocommit_connection

    def _commit_any_open_transaction(self) -> None:
        if self.connection.in_transaction():
            self.connection.commit()

    def finalize(self) -> None:
        """Validate against the live database, flush all plans and run
        the foreign key check.

        The group transaction itself is committed later by the revision
        transaction owner (or rolled back on error), so that the
        ``alembic_version`` update shares the same transaction.
        """
        if self._finalized:
            return
        # database-dependent validation; still runs before any mutating
        # statement because the group body only collected plans.
        self._preflight()
        self._flush_plans()
        self._run_foreign_key_check()
        self._finalized = True

    def commit_group(self) -> None:
        """Commit the group transaction and restore the connection.

        Invoked by the revision-level transaction owner once the version
        stamp has been written.
        """
        if not self._finalized:
            # finalize() was not reached (should not happen in normal
            # flow); treat as a rollback.
            self._rollback_and_restore()
            return
        assert self._autocommit_connection is not None
        try:
            # explicit SQLite COMMIT (covers DDL), then synchronize the
            # SQLAlchemy RootTransaction state so the connection is clean.
            self._autocommit_connection.exec_driver_sql("COMMIT")
            self._sync_sa_transaction("commit")
        except BaseException:
            self._rollback_and_restore()
            raise
        else:
            self._restore_connection()

    def rollback(self) -> None:
        """Roll the group transaction back and restore the connection."""
        if self._restored:
            return
        self._rollback_and_restore()

    def _sync_sa_transaction(self, mode: str) -> None:
        """Bring the SQLAlchemy transaction bookkeeping in line with the
        explicit SQLite ``BEGIN``/``COMMIT``/``ROLLBACK`` we issued.

        pysqlite does not know about the raw transaction boundaries, so
        after an error SQLAlchemy may still believe a
        :class:`.RootTransaction` is active.  Calling the matching
        Connection method closes that bookkeeping without emitting another
        transaction control statement.
        """
        connection = self._autocommit_connection
        if connection is None:
            return
        try:
            if mode == "commit":
                connection.commit()
            else:
                connection.rollback()
        except Exception:
            # the database-level boundary already happened; never mask the
            # original failure with bookkeeping noise.
            pass

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _flush_plans(self) -> None:
        for table_name, batch_impl in self._plans.items():
            self._flush_one(table_name, batch_impl)

    def _flush_one(
        self, table_name: str, batch_impl: BatchOperationsImpl
    ) -> None:
        should_recreate = batch_impl._should_recreate()

        if not should_recreate:
            # native ALTER TABLE path (add/drop column variants and plain
            # index changes) - no copy table is created at all.
            for opname, arg, kw in batch_impl.batch:
                fn = getattr(self.impl, opname)
                fn(*arg, **kw)
            return

        if batch_impl.copy_from is not None:  # pragma: no cover - guarded
            existing_table = batch_impl.copy_from
            reflected = False
        else:
            from sqlalchemy import MetaData
            from sqlalchemy import Table

            if batch_impl.naming_convention:
                metadata = MetaData(
                    naming_convention=batch_impl.naming_convention
                )
            else:
                metadata = MetaData()
            existing_table = Table(
                table_name,
                metadata,
                schema=batch_impl.schema,
                autoload_with=self._autocommit_connection,
                *batch_impl.reflect_args,
                **batch_impl.reflect_kwargs,
            )
            reflected = True

        batch_apply = ApplyBatchImpl(
            self.impl,
            existing_table,
            batch_impl.table_args,
            batch_impl.table_kwargs,
            reflected,
            partial_reordering=batch_impl.partial_reordering,
        )
        # unique, group-scoped temporary name so that concurrent rebuilds
        # never collide, including tables whose names collide when
        # truncated to SQLite's identifier length.
        batch_apply.temp_table_name = self._temp_name(table_name)

        for opname, arg, kw in batch_impl.batch:
            fn = getattr(batch_apply, opname)
            fn(*arg, **kw)

        batch_apply._create(self.impl)

    def _run_foreign_key_check(self) -> None:
        assert self._autocommit_connection is not None
        rows = self._autocommit_connection.exec_driver_sql(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if rows:
            formatted = ", ".join(str(tuple(row)) for row in rows[:10])
            raise util.CommandError(
                "New foreign key constraints are violated by the existing "
                f"data (first offending rows: {formatted}); the grouped "
                "migration has been rolled back and no data was modified. "
                "Fix the offending rows before running the migration again."
            )

    def _rollback_and_restore(self) -> None:
        if self._restored:
            return
        autocommit_connection = self._autocommit_connection
        if autocommit_connection is not None:
            try:
                # explicit SQLite ROLLBACK (covers DDL), then synchronize
                # the SQLAlchemy RootTransaction state which pysqlite does
                # not update for raw transaction control statements.
                autocommit_connection.exec_driver_sql("ROLLBACK")
                self._sync_sa_transaction("rollback")
            except Exception:
                # best effort; the connection must remain usable
                pass
        self._rolled_back = True
        self._restore_connection()

    def _restore_connection(self) -> None:
        if self._restored:
            return
        self._restored = True
        ctx = self.context

        autocommit_connection = self._autocommit_connection
        if autocommit_connection is not None and (
            autocommit_connection.in_transaction()
        ):
            # COMMIT/ROLLBACK already ran; defensive cleanup only.
            autocommit_connection.rollback()

        # switch the MigrationContext back onto the original connection
        # before touching the pragma again.
        self.impl.connection = self._base_connection
        ctx.connection = self._base_connection
        ctx.sqlite_batch_group = None
        ctx._sqlite_group_finalized = True
        # hand the revision proxy back; its underlying SQLAlchemy
        # transaction was already committed/rolled back with the group, so
        # its __exit__ becomes a no-op.
        if self._revision_proxy is not None:
            ctx._transaction = None

        if autocommit_connection is not None:
            ctx._sqlite_restoring = True
            try:
                if self._saved_foreign_keys is not None:
                    # restore the pragma outside of any transaction
                    autocommit_connection.exec_driver_sql(
                        "PRAGMA foreign_keys=%d"
                        % (1 if self._saved_foreign_keys else 0,)
                    )
                    autocommit_connection.commit()
                # return the pooled connection to its previous isolation
                # level; the wrapper itself must not be closed because it
                # shares the DBAPI connection with the base Connection.
                if self._saved_isolation_level is not None:
                    self._base_connection.execution_options(
                        isolation_level=self._saved_isolation_level
                    )
            finally:
                ctx._sqlite_restoring = False
                self._autocommit_connection = None

        if self._rolled_back:
            # After a rollback the DBAPI connection that ran the explicit
            # transaction may retain stale schema/transaction bookkeeping
            # (pysqlite does not track the raw BEGIN/ROLLBACK).  Invalidate
            # it so the pool hands out a fresh connection; the engine and
            # the MigrationContext remain fully usable.
            try:
                self._base_connection.invalidate()
            except Exception:
                pass


class OrderedTablePlans(dict):
    """Plain dict with insertion-order iteration (dict is ordered on
    Python 3.7+; the subclass only documents intent)."""


def install_group_guards(context: "MigrationContext") -> None:
    """Install the per-revision write tracking guard on a migration
    context.

    The guard records whether any mutating SQL was emitted outside of an
    :class:`.SQLiteBatchGroup`, which is needed to reject migrations that
    cannot be made atomic (statements before the group, or extra writes
    after it).
    """

    if getattr(context, "_sqlite_guard_installed", False):
        return
    context._sqlite_guard_installed = True
    context._sqlite_user_writes = 0
    context._sqlite_group_finalized = False
    context._sqlite_restoring = False
    context.sqlite_batch_group = None

    connection = context.bind
    if connection is None:
        return

    def _track_write(
        conn, cursor, statement, parameters, context_, executemany
    ):
        ctx = context
        if getattr(ctx, "_sqlite_restoring", False):
            # pragma/cleanup statements emitted while the group is
            # restoring its connection state.
            return
        if ctx.sqlite_batch_group is not None:
            # statements inside the group (including the version stamp)
            # are part of the group transaction by definition.
            return
        if ctx._sqlite_version_write_allowed:
            return
        normalized = statement.lstrip().upper()
        if not normalized.startswith(_WRITE_PREFIXES):
            return
        if ctx._sqlite_group_finalized:
            raise util.CommandError(
                "Statements cannot be executed after "
                "sqlite_batch_group() within the same migration revision; "
                "the group transaction is still open and is committed "
                "together with the alembic_version update. Offending "
                f"statement: {statement}"
            )
        ctx._sqlite_user_writes += 1

    try:
        event.listen(
            connection, "before_cursor_execute", _track_write
        )
    except Exception:
        # mock / offline connections do not provide cursor events; the
        # write guard only applies to live connections.
        return

    context._sqlite_write_listener = _track_write
    context._sqlite_version_write_allowed = False


def remove_group_guards(context: "MigrationContext") -> None:
    listener = getattr(context, "_sqlite_write_listener", None)
    if listener is not None and context.bind is not None:
        event.remove(context.bind, "before_cursor_execute", listener)
        context._sqlite_write_listener = None
    context._sqlite_guard_installed = False


@contextmanager
def sqlite_batch_group(
    operations: "Operations",
    temp_prefix: Optional[str] = None,
) -> Iterator[SQLiteBatchGroup]:
    """Implementation backing :meth:`.Operations.sqlite_batch_group`."""

    context = operations.migration_context
    if getattr(context, "_sqlite_guard_installed", False) is False:
        # reached when the API is used outside of run_migrations();
        # install a one-off guard so the same rules apply.
        install_group_guards(context)
        standalone = True
    else:
        standalone = False

    group = SQLiteBatchGroup(operations, temp_prefix=temp_prefix)
    group.begin()
    try:
        yield group
    except BaseException:
        group.rollback()
        raise
    else:
        # flush + foreign_key_check only.  The group transaction is kept
        # open: any statements following the group within the same
        # revision, and in particular the alembic_version update, still
        # run inside it.  The transaction is committed by the revision
        # transaction owner in run_migrations(), or by the finally block
        # below when the API is used outside of run_migrations().
        group.finalize()
    finally:
        if standalone:
            if not group._restored:
                if group._finalized:
                    group.commit_group()
                else:
                    group.rollback()
            remove_group_guards(context)
