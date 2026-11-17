# mypy: allow-untyped-defs, allow-incomplete-defs, allow-untyped-calls
# mypy: no-warn-return-any, allow-any-generics

"""Autogenerate comparison for managed SQLite FTS5 indexes.

The comparator runs after the ordinary table/column/index comparators and
produces :class:`.CreateFTSIndexOp` / :class:`.DropFTSIndexOp` top-level
operations.  A declaration whose columns or tokenizer changed produces a
drop+create pair that rebuilds the index from the current content table.

Managed objects (the registry table, FTS virtual tables and their shadow
tables, and the synchronization triggers reachable from registry entries)
are filtered out of the ordinary table comparison so that autogenerate
never emits ``create_table``/``drop_table`` for them.  FTS tables and
triggers created directly by the user - i.e. without a registry entry -
are never claimed or removed by Alembic.
"""

from __future__ import annotations

from typing import Any
from typing import Dict
from typing import List
from typing import Set
from typing import Tuple
from typing import TYPE_CHECKING


from .. import util
from ..operations import fts
from ..operations.fts_ops import CreateFTSIndexOp
from ..operations.fts_ops import DropFTSIndexOp
from ..operations.ops import UpgradeOps

if TYPE_CHECKING:
    from sqlalchemy.sql.schema import MetaData

    from ..autogenerate.api import AutogenContext
    from ..operations.fts import FTSIndexSpec


def _created_table_names(upgrade_ops: UpgradeOps) -> Set[str]:
    from ..operations.ops import CreateTableOp

    return {
        op.table_name
        for op in upgrade_ops.ops
        if isinstance(op, CreateTableOp)
    }


def _is_sqlite(autogen_context: AutogenContext) -> bool:
    ctx = autogen_context.migration_context
    return (
        not ctx.as_sql
        and autogen_context.dialect is not None
        and autogen_context.dialect.name == "sqlite"
    )


def metadata_for_fts(
    metadata: Any,
) -> "MetaData":
    """Return a single MetaData carrying the FTS declarations.

    The ordinary autogenerate flow accepts either one MetaData or a
    sequence of them (multi-database setups).  For FTS purposes the
    declarations of every MetaData are collected into a synthetic
    MetaData that also references the originating tables, so validation
    and comparison keep working without mutating the caller's objects.
    """

    from sqlalchemy.schema import MetaData as _MetaData

    if metadata is None:
        return _MetaData()
    if not isinstance(metadata, (list, tuple)):
        return metadata

    merged = _MetaData()
    declarations: List[Any] = []
    for md in metadata:
        for key, table in md.tables.items():
            if key not in merged.tables:
                table.to_metadata(merged)
        declarations.extend(md.info.get(fts.METADATA_INFO_KEY, []) or [])
    merged.info[fts.METADATA_INFO_KEY] = declarations
    return merged


def get_managed_object_names(
    autogen_context: AutogenContext,
) -> Set[str]:
    """Return names of all FTS objects Alembic currently owns.

    Read-only: queries the registry and ``sqlite_master`` but never
    mutates the database or the metadata.
    """

    if not _is_sqlite(autogen_context):
        return set()
    connection = autogen_context.connection
    assert connection is not None
    manager = fts.FTSIndexManager(connection)
    if not manager.registry_exists():
        return set()
    specs = manager.verify_all()
    return set(manager.managed_object_names(specs))


def compare_fts_indexes(
    autogen_context: AutogenContext,
    upgrade_ops: UpgradeOps,
    metadata: MetaData,
) -> None:
    """Compare declared FTS indexes with the live database and append
    lifecycle operations to *upgrade_ops*."""

    if not _is_sqlite(autogen_context):
        # declarations on non-sqlite targets are configuration errors;
        # surface them instead of silently ignoring.
        declared = fts.load_metadata_specs(metadata)
        if declared:
            raise util.CommandError(
                "MetaData.info[%r] FTS index declarations are only "
                "supported on the SQLite dialect; current dialect is "
                "%r."
                % (
                    fts.METADATA_INFO_KEY,
                    (
                        autogen_context.dialect.name
                        if autogen_context.dialect
                        else None
                    ),
                )
            )
        return

    # validate declarations up front (shape, keys, names).  Content
    # table/column shape is checked separately because a column referenced
    # by a surviving declaration may at the same time be detected as
    # removed, which has its own explicit rejection below.
    declared_specs = fts.load_metadata_specs(metadata, validate_content=False)
    declared_by_name: Dict[str, FTSIndexSpec] = {
        s.name: s for s in declared_specs
    }

    connection = autogen_context.connection
    assert connection is not None
    manager = fts.FTSIndexManager(connection)
    existing = manager.verify_all() if manager.registry_exists() else {}

    # A content column or content table that is still referenced by a
    # target declaration may not be removed in the same revision.  When
    # the declaration itself was removed, the generated drop_fts_index
    # runs first and the ordinary table/column change then follows the
    # rules of the selected render mode.
    _reject_dropped_referenced_objects(
        autogen_context,
        upgrade_ops,
        declared_by_name,
        existing,
        metadata,
    )

    # full content-table validation now that removal conflicts are out
    # of the way.
    fts.load_metadata_specs(metadata)

    # validate structural preconditions against the live database for
    # declarations that will be created or rebuilt.  A content table
    # created in this same revision does not exist in the database yet,
    # so those declarations are validated solely against the metadata.
    created_tables = _created_table_names(upgrade_ops)
    for spec in declared_specs:
        if spec.name not in existing or existing[spec.name] != spec:
            if spec.table_name not in created_tables:
                manager.assert_content_table_usable(spec)

    ops_for_fts: List[Any] = []

    def _filter(spec: Any, reflected: bool, compare_to: Any = None) -> bool:
        # "fts_index" is an Alembic-specific object class; the filter API
        # is typed for the built-in SQLAlchemy object kinds.
        return autogen_context.run_object_filters(
            None,  # type: ignore[arg-type]
            spec.name if hasattr(spec, "name") else spec,
            "fts_index",  # type: ignore[arg-type]
            reflected,
            compare_to,
        )

    # removals first (also covers changed definitions: drop the old one)
    for name in sorted(set(existing).difference(declared_by_name)):
        spec = existing[name]
        if _filter(spec, True):
            ops_for_fts.append(DropFTSIndexOp.from_spec(spec))
            log_line(autogen_context, "Detected removed FTS index %r", name)

    for name in sorted(declared_by_name):
        new_spec = declared_by_name[name]
        if name not in existing:
            if _filter(new_spec, False):
                ops_for_fts.append(CreateFTSIndexOp.from_spec(new_spec))
                log_line(
                    autogen_context,
                    "Detected added FTS index %r on table %r",
                    name,
                    new_spec.table_name,
                )
        elif existing[name] != new_spec:
            old_spec = existing[name]
            if _filter(new_spec, False, old_spec):
                # rebuild: drop old definition, create the new one from
                # the current content table.
                ops_for_fts.append(DropFTSIndexOp.from_spec(old_spec))
                ops_for_fts.append(CreateFTSIndexOp.from_spec(new_spec))
                log_line(
                    autogen_context,
                    "Detected changed FTS index %r (columns %r/%r or "
                    "tokenizer %r -> %r); rebuilding",
                    name,
                    list(old_spec.columns),
                    list(new_spec.columns),
                    old_spec.tokenize,
                    new_spec.tokenize,
                )

    _order_fts_ops(upgrade_ops, ops_for_fts, declared_by_name, existing)


def log_line(
    autogen_context: AutogenContext, template: str, *args: Any
) -> None:
    import logging

    logging.getLogger("alembic.autogenerate.compare").info(template, *args)


def _reject_dropped_referenced_objects(
    autogen_context: AutogenContext,
    upgrade_ops: UpgradeOps,
    declared_by_name: Dict[str, "FTSIndexSpec"],
    existing: Dict[str, "FTSIndexSpec"],
    metadata: "MetaData",
) -> None:
    """Reject revisions that would drop a table/column still used by a
    target or still-registered FTS declaration.

    Dropping a content table is always rejected while any index on it
    remains declared or registered (the index must be removed first in
    its own revision).  Dropping a column is rejected when a *surviving*
    index still references it: if every index referencing the column is
    itself removed in this same revision, the FTS drops run first and the
    ordinary column drop proceeds within the render mode's existing
    scope.
    """

    from ..operations.ops import DropColumnOp
    from ..operations.ops import DropTableOp
    from ..operations.ops import ModifyTableOps

    # table name -> set of columns referenced by indexes that survive
    # this revision (still declared at the target state)
    surviving_by_table: Dict[str, Set[str]] = {}
    for spec in declared_by_name.values():
        surviving_by_table.setdefault(spec.table_name, set()).update(
            spec.columns
        )

    # columns actually removed per table by the ordinary diffs
    dropped_by_table: Dict[str, Set[str]] = {}
    for op in upgrade_ops.ops:
        if isinstance(op, ModifyTableOps):
            dropped_by_table[op.table_name] = {
                inner.column_name
                for inner in op.ops
                if isinstance(inner, DropColumnOp)
            }

    for spec in declared_by_name.values():
        table = metadata.tables.get(spec.table_name)
        if table is None:
            raise util.CommandError(
                "FTS index %r references content table %r which is not "
                "declared in the target MetaData."
                % (spec.name, spec.table_name)
            )
        missing = [c for c in spec.columns if c not in table.c]
        for column in missing:
            if column in dropped_by_table.get(spec.table_name, set()):
                raise util.CommandError(
                    "Cannot drop indexed column %r from table %r while "
                    "FTS index %r still references it; update or remove "
                    "the FTS declaration first."
                    % (column, spec.table_name, spec.name)
                )
            raise util.CommandError(
                "FTS index %r references column %r which is not present "
                "on content table %r." % (spec.name, column, spec.table_name)
            )

    for op in upgrade_ops.ops:
        if isinstance(op, DropTableOp):
            # a table whose surviving declarations still reference it
            # may not be dropped; indexes that are themselves removed in
            # this revision are handled by the FTS drop operation which
            # is ordered ahead of the table drop.
            if op.table_name in surviving_by_table:
                raise util.CommandError(
                    "Cannot drop content table %r in the same revision "
                    "where FTS index(es) still declare it; remove the "
                    "FTS declaration(s) first so the index(es) can be "
                    "dropped before the table." % op.table_name
                )
        elif isinstance(op, ModifyTableOps):
            dropped_columns = dropped_by_table.get(op.table_name, set())
            if not dropped_columns:
                continue
            clash = dropped_columns.intersection(
                surviving_by_table.get(op.table_name, set())
            )
            if clash:
                raise util.CommandError(
                    "Cannot drop indexed column(s) %r from table %r "
                    "while an FTS index still references them; update or "
                    "remove the FTS declaration first."
                    % (sorted(clash), op.table_name)
                )


def _order_fts_ops(
    upgrade_ops: UpgradeOps,
    fts_ops_list: List[Any],
    declared_by_name: Dict[str, "FTSIndexSpec"],
    existing: Dict[str, "FTSIndexSpec"],
) -> None:
    """Insert FTS operations into the overall upgrade stream respecting
    content-table dependencies.

    * creates of a content table must precede creates of its FTS index;
    * drops of an FTS index must precede drops of its content table;
    * FTS changes tied to an existing table that is rebuilt in the same
      revision are placed after the table alteration so the rebuilt
      table's rows are what gets backfilled.
    """

    if not fts_ops_list:
        return

    from ..operations.ops import CreateTableOp
    from ..operations.ops import DropTableOp
    from ..operations.ops import ModifyTableOps

    def table_position(table_name: str) -> Tuple[int, int]:
        """Return (create_after_idx, drop_before_idx) anchors."""

        create_after = -1
        drop_before = len(upgrade_ops.ops)
        for idx, op in enumerate(upgrade_ops.ops):
            if isinstance(op, CreateTableOp) and op.table_name == table_name:
                create_after = idx
            elif isinstance(op, DropTableOp) and op.table_name == table_name:
                drop_before = idx
            elif (
                isinstance(op, ModifyTableOps) and op.table_name == table_name
            ):
                create_after = idx
        return create_after, drop_before

    deferred_creates: List[Any] = []
    leading_drops: List[Any] = []
    middle: List[Any] = []

    for op in fts_ops_list:
        if isinstance(op, CreateFTSIndexOp):
            deferred_creates.append(op)
        else:
            table_name = (
                op._reverse_spec.table_name
                if op._reverse_spec is not None
                else existing[op.name].table_name
            )
            _create_after, drop_before = table_position(table_name)
            if drop_before < len(upgrade_ops.ops):
                leading_drops.append((drop_before, op))
            else:
                middle.append(op)

    # drops that must run before a content-table drop, in stable order
    leading_drops.sort(key=lambda item: item[0])
    for offset, (_anchor, op) in enumerate(leading_drops):
        upgrade_ops.ops.insert(offset, op)

    # ordinary drops/rebuild-pairs of indexes on retained tables go
    # after the table alterations
    upgrade_ops.ops.extend(middle)
    upgrade_ops.ops.extend(deferred_creates)
