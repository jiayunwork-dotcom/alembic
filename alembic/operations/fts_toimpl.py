# mypy: allow-untyped-defs, allow-incomplete-defs, allow-untyped-calls
# mypy: no-warn-return-any, allow-any-generics

"""Runtime implementations of the FTS5 migration operations.

Transaction model
-----------------

pysqlite opens implicit transactions only around DML, so a plain
``CREATE VIRTUAL TABLE`` / ``CREATE TRIGGER`` issued through SQLAlchemy
would autocommit immediately and could not be rolled back together with
the data backfill and the ``alembic_version`` update.  To get
all-or-nothing semantics for FTS lifecycle changes, every online FTS
operation executes inside an explicit SQLite transaction opened with a
raw ``BEGIN`` through an AUTOCOMMIT wrapper connection (the same approach
as :mod:`alembic.operations.grouped`):

* when the revision already runs inside an
  :class:`.SQLiteBatchGroup` transaction, FTS DDL participates in that
  very transaction and no extra boundary is created;
* otherwise the per-revision SQLAlchemy transaction (present when
  ``transaction_per_migration=True``) is suspended, the explicit
  transaction is opened and left open, and the revision proxy commits or
  rolls it back together with the ``alembic_version`` stamp via hooks
  stored on the :class:`.MigrationContext`;
* standalone use of the operation outside ``run_migrations()`` (no
  revision proxy) commits or rolls back the explicit transaction itself.

On failure the explicit transaction is rolled back and the connection's
previous isolation level and ``PRAGMA foreign_keys`` value are restored.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from typing import cast

from . import fts
from .base import Operations
from .fts_ops import CreateFTSIndexOp
from .fts_ops import DropFTSIndexOp
from .. import util

if TYPE_CHECKING:
    from typing import Iterator

    from sqlalchemy.engine import Connection


@contextmanager
def _explicit_fts_transaction(
    operations: Operations,
) -> "Iterator[fts.FTSIndexManager]":
    context = operations.migration_context
    fts.require_sqlite_online(context, "FTS index operation")
    base_connection = context.bind
    assert base_connection is not None

    # inside an sqlite_batch_group() the group already owns an explicit
    # BEGIN on its AUTOCOMMIT wrapper; simply join it.
    group = getattr(context, "sqlite_batch_group", None)
    if group is not None:
        manager = fts.FTSIndexManager(group._autocommit_connection)
        if not fts.fts5_available(group._autocommit_connection):
            raise util.CommandError(
                "The connected SQLite runtime does not support the FTS5 "
                "extension; cannot manage FTS indexes."
            )
        yield manager
        return

    if not fts.fts5_available(base_connection):
        raise util.CommandError(
            "The connected SQLite runtime does not support the FTS5 "
            "extension; cannot manage FTS indexes."
        )

    # another FTS lifecycle operation earlier in this revision already
    # opened the explicit transaction and registered the revision-level
    # commit/rollback hooks; join it instead of nesting.
    if getattr(context, "_sqlite_fts_tx_hooks", None) is not None:
        joined_connection: "Connection" = (
            operations.impl.connection  # type: ignore[assignment, union-attr]
        )
        yield fts.FTSIndexManager(joined_connection)
        return

    saved_isolation = base_connection.get_isolation_level()
    saved_foreign_keys = int(
        base_connection.exec_driver_sql("PRAGMA foreign_keys").scalar() or 0
    )

    # Suspend any autobegun SQLAlchemy transaction (read-only
    # bookkeeping) so the raw BEGIN below is the only active boundary.
    revision_proxy = context._transaction
    context._transaction = None
    if revision_proxy is not None:
        revision_proxy.commit()
    if base_connection.in_transaction():
        base_connection.commit()

    autocommit = base_connection.execution_options(
        isolation_level="AUTOCOMMIT"
    )
    previous_impl_connection = cast(
        "Connection",
        operations.impl.connection,  # type: ignore[union-attr]
    )
    previous_context_connection = context.connection
    operations.impl.connection = autocommit  # type: ignore[union-attr]
    context.connection = autocommit
    manager = fts.FTSIndexManager(autocommit)

    def restore():
        if getattr(context, "_sqlite_fts_finalized", False):
            return
        context._sqlite_fts_finalized = True
        operations.impl.connection = (  # type: ignore[union-attr]
            previous_impl_connection
        )
        context.connection = previous_context_connection
        context._sqlite_fts_tx_hooks = None
        try:
            autocommit.exec_driver_sql(
                "PRAGMA foreign_keys=%d" % saved_foreign_keys
            )
            autocommit.commit()
            base_connection.execution_options(isolation_level=saved_isolation)
        except Exception:
            try:
                base_connection.invalidate()
            except Exception:
                pass

    def commit_hook():
        try:
            autocommit.exec_driver_sql("COMMIT")
            try:
                autocommit.commit()
            except Exception:
                pass
        finally:
            restore()

    def rollback_hook():
        try:
            try:
                autocommit.exec_driver_sql("ROLLBACK")
                autocommit.rollback()
            except Exception:
                pass
        finally:
            restore()

    try:
        autocommit.exec_driver_sql("BEGIN")
        if revision_proxy is not None:
            # the revision proxy exit drives commit/rollback of the raw
            # transaction; hand the (now empty) proxy back.
            context._transaction = revision_proxy
            context._sqlite_fts_finalized = False
            context._sqlite_fts_tx_hooks = {
                "commit": commit_hook,
                "rollback": rollback_hook,
            }
        yield manager
    except BaseException:
        if revision_proxy is not None and getattr(
            context, "_sqlite_fts_tx_hooks", None
        ):
            # let the revision-level exception path run the rollback
            # hook through the proxy __exit__; nothing else to do here.
            raise
        try:
            rollback_hook()
        finally:
            context._transaction = revision_proxy
        raise
    else:
        if revision_proxy is None:
            # standalone use: no revision owner will close the raw
            # transaction for us.
            commit_hook()
        # else: stay inside the explicit transaction; the revision
        # proxy commits it (with the alembic_version stamp) on exit.


@Operations.implementation_for(CreateFTSIndexOp)
def create_fts_index(
    operations: Operations, operation: CreateFTSIndexOp
) -> None:
    with _explicit_fts_transaction(operations) as manager:
        manager.create_index(operation.spec)


@Operations.implementation_for(DropFTSIndexOp)
def drop_fts_index(operations: Operations, operation: DropFTSIndexOp) -> None:
    with _explicit_fts_transaction(operations) as manager:
        manager.drop_index(operation.name)
