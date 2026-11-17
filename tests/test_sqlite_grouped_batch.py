"""Regression tests for SQLite grouped batch migrations
(``op.sqlite_batch_group()``).

The tests run entirely against temporary SQLite files with synthetic data;
no external database service is required.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy import Column
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy import ForeignKeyConstraint
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import PrimaryKeyConstraint
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import text
from sqlalchemy import UniqueConstraint

from alembic import command
from alembic.migration import MigrationContext
from alembic.migration import RevisionStep
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from alembic.script.revision import RevisionMap
from alembic.testing import assert_raises_message
from alembic.testing import eq_
from alembic.testing import TestBase
from alembic.testing.env import clear_staging_env
from alembic.testing.env import staging_env
from alembic.testing.env import _sqlite_testing_config
from alembic.util import CommandError


def _fk_on_connect():
    """Return an event listener that turns foreign keys on for every new
    pooled DBAPI connection."""

    def _listen(dbapi_connection, rec):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return _listen


@contextmanager
def _sqlite_engine(tmp_path, *, fk_default_on=True, suffix=".db"):
    path = tmp_path / ("grouped" + suffix)
    engine = create_engine(f"sqlite:///{path}")
    if fk_default_on:
        event.listen(engine, "connect", _fk_on_connect())
    try:
        yield engine
    finally:
        engine.dispose()


def _pragma(conn, name):
    return conn.exec_driver_sql(f"PRAGMA {name}").scalar()


def _pragma_rows(conn, name):
    return conn.exec_driver_sql(f"PRAGMA {name}").fetchall()


def _table_names(conn):
    return [
        row[0]
        for row in conn.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type IN ('table','index','view','trigger') "
                "ORDER BY name"
            )
        )
    ]


def _no_temp_artifacts(conn):
    rows = conn.execute(
        text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE name LIKE '%_alembic%tmp%' "
            "OR name LIKE '%_alembic_group_tmp%'"
        )
    ).fetchall()
    assert rows == [], rows
    # no schema object may reference a temporary table name
    refs = conn.execute(
        text(
            "SELECT name FROM sqlite_master "
            "WHERE sql LIKE '%_alembic_group_tmp%'"
        )
    ).fetchall()
    assert refs == [], refs


class _ManualScript:
    def __init__(
        self,
        fn,
        revision="rev1",
        down=(),
        *,
        upgrade_fn=None,
        downgrade_fn=None,
    ):
        self.module = SimpleNamespace(
            upgrade=upgrade_fn if upgrade_fn is not None else fn,
            downgrade=(
                downgrade_fn if downgrade_fn is not None else lambda **kw: None
            ),
        )
        self.revision = revision
        self.doc = None
        self._normalized_down_revisions = tuple(down)
        self._versioned_down_revisions = tuple(down)


def _step(fn, **kw):
    return RevisionStep(RevisionMap(()), _ManualScript(fn, **kw), True)


def _run_revision(engine, revision_fn, *, opts=None):
    with engine.connect() as conn:
        final_opts = {"transaction_per_migration": True}
        if opts:
            final_opts.update(opts)
        final_opts["fn"] = lambda heads, ctx: [
            _step(revision_fn(Operations(ctx)))
        ]
        context = MigrationContext.configure(conn, opts=final_opts)
        context.run_migrations()
        return context


class GroupedBatchTest(TestBase):
    def _cyclic_schema(self, engine):
        with engine.begin() as conn:
            conn.execute(
                text(
                    """CREATE TABLE a (
                id INTEGER NOT NULL PRIMARY KEY,
                b_id INTEGER,
                note VARCHAR(20),
                CONSTRAINT fk_a_b FOREIGN KEY(b_id) REFERENCES b(id))"""
                )
            )
            conn.execute(
                text(
                    """CREATE TABLE b (
                id INTEGER NOT NULL PRIMARY KEY,
                a_id INTEGER NOT NULL,
                label VARCHAR(20),
                CONSTRAINT fk_b_a FOREIGN KEY(a_id) REFERENCES a(id))"""
                )
            )
        with engine.connect() as conn:
            ac = conn.execution_options(isolation_level="AUTOCOMMIT")
            ac.exec_driver_sql("PRAGMA foreign_keys=OFF")
            conn.execute(
                text("INSERT INTO a VALUES (1,1,'n1'),(2,1,'n2')")
            )
            conn.execute(
                text("INSERT INTO b VALUES (1,1,'l1'),(2,2,'l2')")
            )
            conn.commit()
            ac.exec_driver_sql("PRAGMA foreign_keys=ON")

    def test_cyclic_tables_tighten_not_null(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            self._cyclic_schema(engine)

            def fn(op):
                def upgrade(**kw):
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("a") as batch_op:
                            batch_op.alter_column(
                                "note",
                                existing_type=String(20),
                                nullable=False,
                            )
                        with op.batch_alter_table("b") as batch_op:
                            batch_op.alter_column(
                                "label",
                                existing_type=String(20),
                                nullable=False,
                            )

                return upgrade

            _run_revision(engine, fn)

            with engine.connect() as conn:
                eq_(_pragma(conn, "foreign_keys"), 1)
                eq_(_pragma_rows(conn, "foreign_key_check"), [])
                _no_temp_artifacts(conn)
                a_cols = {r[1]: r[3] for r in conn.exec_driver_sql(
                    "PRAGMA table_info(a)"
                )}
                b_cols = {r[1]: r[3] for r in conn.exec_driver_sql(
                    "PRAGMA table_info(b)"
                )}
                eq_(a_cols["note"], 1)
                eq_(b_cols["label"], 1)
                # FKs preserved
                eq_(len(conn.exec_driver_sql(
                    "PRAGMA foreign_key_list(a)"
                ).fetchall()), 1)
                eq_(len(conn.exec_driver_sql(
                    "PRAGMA foreign_key_list(b)"
                ).fetchall()), 1)
                # data preserved
                eq_(
                    conn.execute(text("SELECT id, note FROM a ORDER BY id")).fetchall(),
                    [(1, "n1"), (2, "n2")],
                )
                # version stamped in the same commit
                eq_(
                    conn.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).fetchall(),
                    [("rev1",)],
                )

    def test_downgrade_rolls_back_together(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            self._cyclic_schema(engine)

            def fn(op):
                def upgrade(**kw):
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("a") as batch_op:
                            batch_op.alter_column(
                                "note",
                                existing_type=String(20),
                                nullable=False,
                            )

                return upgrade

            _run_revision(engine, fn)
            with engine.connect() as conn:
                eq_(
                    conn.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar(),
                    "rev1",
                )

            # downgrade re-opens the column and removes the stamp
            def downgrade_fn(op):
                def downgrade(**kw):
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("a") as batch_op:
                            batch_op.alter_column(
                                "note",
                                existing_type=String(20),
                                nullable=True,
                            )

                return downgrade

            with engine.connect() as conn:
                opts = {"transaction_per_migration": True}
                opts["fn"] = lambda heads, ctx: [
                    RevisionStep(
                        RevisionMap(()),
                        _ManualScript(
                            None,
                            downgrade_fn=downgrade_fn(Operations(ctx)),
                        ),
                        False,
                    )
                ]
                context = MigrationContext.configure(conn, opts=opts)
                context.run_migrations()

            with engine.connect() as conn:
                a_cols = {r[1]: r[3] for r in conn.exec_driver_sql(
                    "PRAGMA table_info(a)"
                )}
                eq_(a_cols["note"], 0)
                eq_(
                    conn.execute(
                        text("SELECT COUNT(*) FROM alembic_version")
                    ).scalar(),
                    0,
                )
                eq_(_pragma(conn, "foreign_keys"), 1)

    def test_bad_data_fails_and_preserves_old_state(self, tmp_path):
        from sqlalchemy import event as _ev

        with _sqlite_engine(tmp_path) as engine:
            @_ev.listens_for(engine, "connect")
            def _tr(c, r):
                c.set_trace_callback(
                    lambda st: print("TRACE:", str(st)[:50])
                )

            self._cyclic_schema(engine)
            # make b.label contain a NULL, incompatible with NOT NULL
            with engine.begin() as conn:
                conn.execute(text("UPDATE b SET label=NULL WHERE id=2"))

            def fn(op):
                def upgrade(**kw):
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("b") as batch_op:
                            batch_op.alter_column(
                                "label",
                                existing_type=String(20),
                                nullable=False,
                            )

                return upgrade

            import sqlalchemy.exc

            with pytest.raises(sqlalchemy.exc.IntegrityError):
                _run_revision(engine, fn)

            with engine.connect() as conn:
                # original schema and rows intact
                b_cols = {r[1]: r[3] for r in conn.exec_driver_sql(
                    "PRAGMA table_info(b)"
                )}
                eq_(b_cols["label"], 0)
                eq_(
                    conn.execute(text("SELECT COUNT(*) FROM b")).scalar(),
                    2,
                )
                eq_(_pragma(conn, "foreign_keys"), 1)
                _no_temp_artifacts(conn)
                # no version row was stamped (the version table itself may
                # have been created by normal bookkeeping before the
                # failing group, but it must be empty)
                if conn.execute(
                    text(
                        "SELECT COUNT(*) FROM sqlite_master "
                        "WHERE name='alembic_version'"
                    )
                ).scalar():
                    eq_(
                        conn.execute(
                            text(
                                "SELECT COUNT(*) FROM alembic_version"
                            )
                        ).scalar(),
                        0,
                    )
                # connection still usable
                eq_(_pragma_rows(conn, "foreign_key_check"), [])

    def test_fk_violation_after_rebuild_aborts(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE TABLE p(id INTEGER PRIMARY KEY, "
                        "name VARCHAR(10) NOT NULL)"
                    )
                )
                conn.execute(
                    text(
                        "CREATE TABLE c(id INTEGER PRIMARY KEY, "
                        "p_id INTEGER, "
                        "CONSTRAINT fk_c_p FOREIGN KEY(p_id) "
                        "REFERENCES p(id))"
                    )
                )
            with engine.connect() as conn:
                ac = conn.execution_options(isolation_level="AUTOCOMMIT")
                ac.exec_driver_sql("PRAGMA foreign_keys=OFF")
                conn.execute(text("INSERT INTO p VALUES (1,'x')"))
                # orphan child row
                conn.execute(text("INSERT INTO c VALUES (1, 99)"))
                conn.commit()
                ac.exec_driver_sql("PRAGMA foreign_keys=ON")

            def fn(op):
                def upgrade(**kw):
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("p") as batch_op:
                            batch_op.alter_column(
                                "name",
                                existing_type=String(10),
                                nullable=False,
                            )

                return upgrade

            # foreign_key_check must detect the orphan row
            with pytest.raises(CommandError, match="foreign key"):
                _run_revision(engine, fn)

            with engine.connect() as conn:
                eq_(
                    conn.execute(text("SELECT COUNT(*) FROM c")).scalar(),
                    1,
                )
                _no_temp_artifacts(conn)

    def test_self_referential_table(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE TABLE node(id INTEGER PRIMARY KEY, "
                        "parent_id INTEGER, title VARCHAR(20), "
                        "CONSTRAINT fk_self FOREIGN KEY(parent_id) "
                        "REFERENCES node(id))"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO node VALUES (1,NULL,'r'),(2,1,'c')"
                    )
                )

            def fn(op):
                def upgrade(**kw):
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("node") as batch_op:
                            batch_op.alter_column(
                                "title",
                                existing_type=String(20),
                                nullable=False,
                            )

                return upgrade

            _run_revision(engine, fn)
            with engine.connect() as conn:
                eq_(_pragma_rows(conn, "foreign_key_check"), [])
                fks = conn.exec_driver_sql(
                    "PRAGMA foreign_key_list(node)"
                ).fetchall()
                eq_(len(fks), 1)
                eq_(fks[0][2], "node")
                eq_(
                    conn.execute(
                        text("SELECT id, title FROM node ORDER BY id")
                    ).fetchall(),
                    [(1, "r"), (2, "c")],
                )

    def test_composite_primary_key_and_named_constraints(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            metadata = MetaData()
            Table(
                "line",
                metadata,
                Column("order_id", Integer, nullable=False),
                Column("line_no", Integer, nullable=False),
                Column("qty", Integer),
                PrimaryKeyConstraint("order_id", "line_no", name="pk_line"),
                UniqueConstraint("qty", name="uq_line_qty"),
                CheckConstraint("qty >= 0", name="ck_line_qty"),
            )
            metadata.create_all(engine)
            with engine.begin() as conn:
                conn.execute(
                    text("INSERT INTO line VALUES (1,1,3),(1,2,4)")
                )

            def fn(op):
                def upgrade(**kw):
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("line") as batch_op:
                            batch_op.alter_column(
                                "qty",
                                existing_type=Integer(),
                                nullable=False,
                            )

                return upgrade

            _run_revision(engine, fn)
            with engine.connect() as conn:
                cols = {r[1]: r[3] for r in conn.exec_driver_sql(
                    "PRAGMA table_info(line)"
                )}
                eq_(cols["qty"], 1)
                # composite PK columns still marked primary key
                pk_cols = [r[1] for r in conn.exec_driver_sql(
                    "PRAGMA table_info(line)"
                ) if r[5]]
                eq_(sorted(pk_cols), ["line_no", "order_id"])
                # named constraints are preserved in the rebuilt CREATE
                # statement (SQLite stores PK/UNIQUE as auto-indexes and
                # CHECK inline).
                create_sql = conn.execute(
                    text(
                        "SELECT sql FROM sqlite_master WHERE name='line'"
                    )
                ).scalar()
                assert "pk_line" in create_sql
                assert "uq_line_qty" in create_sql
                assert "ck_line_qty" in create_sql
                # the UNIQUE constraint still behaves as unique
                unique_indexes = {
                    r[1]: r[2]
                    for r in conn.exec_driver_sql(
                        "PRAGMA index_list(line)"
                    )
                }
                assert any(
                    name.startswith("sqlite_autoindex") and unique
                    for name, unique in unique_indexes.items()
                )
                eq_(
                    conn.execute(
                        text("SELECT COUNT(*) FROM line")
                    ).scalar(),
                    2,
                )

    def test_outside_cascade_child_untouched(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE TABLE parent(id INTEGER PRIMARY KEY, "
                        "name VARCHAR(20))"
                    )
                )
                conn.execute(
                    text(
                        "CREATE TABLE child(id INTEGER PRIMARY KEY, "
                        "parent_id INTEGER, "
                        "CONSTRAINT fk_child_parent "
                        "FOREIGN KEY(parent_id) REFERENCES parent(id) "
                        "ON DELETE CASCADE)"
                    )
                )
                conn.execute(
                    text("INSERT INTO parent VALUES (1,'a'),(2,'b')")
                )
                conn.execute(
                    text("INSERT INTO child VALUES (1,1),(2,1),(3,2)")
                )

            def fn(op):
                def upgrade(**kw):
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("parent") as batch_op:
                            batch_op.alter_column(
                                "name",
                                existing_type=String(20),
                                nullable=False,
                            )

                return upgrade

            _run_revision(engine, fn)
            with engine.connect() as conn:
                # child table fully preserved, including its cascade FK
                eq_(
                    conn.execute(
                        text("SELECT COUNT(*) FROM child")
                    ).scalar(),
                    3,
                )
                fk = conn.exec_driver_sql(
                    "PRAGMA foreign_key_list(child)"
                ).fetchall()
                eq_(len(fk), 1)
                eq_(fk[0][6], "CASCADE")
                eq_(_pragma_rows(conn, "foreign_key_check"), [])

    def test_native_alter_and_rebuilt_tables_mix(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE TABLE t1(id INTEGER PRIMARY KEY, "
                        "v VARCHAR(10))"
                    )
                )
                conn.execute(
                    text(
                        "CREATE TABLE t2(id INTEGER PRIMARY KEY, "
                        "w VARCHAR(10))"
                    )
                )
                conn.execute(text("INSERT INTO t1 VALUES (1,'x')"))
                conn.execute(text("INSERT INTO t2 VALUES (1,'y')"))

            def fn(op):
                def upgrade(**kw):
                    with op.sqlite_batch_group():
                        # add_column only -> native ALTER, no copy
                        with op.batch_alter_table("t1") as batch_op:
                            batch_op.add_column(
                                Column("extra", Integer)
                            )
                        # alter nullable -> rebuild
                        with op.batch_alter_table("t2") as batch_op:
                            batch_op.alter_column(
                                "w",
                                existing_type=String(10),
                                nullable=False,
                            )

                return upgrade

            _run_revision(engine, fn)
            with engine.connect() as conn:
                t1_cols = [r[1] for r in conn.exec_driver_sql(
                    "PRAGMA table_info(t1)"
                )]
                t2_cols = {r[1]: r[3] for r in conn.exec_driver_sql(
                    "PRAGMA table_info(t2)"
                )}
                assert "extra" in t1_cols
                eq_(t2_cols["w"], 1)
                eq_(
                    conn.execute(text("SELECT * FROM t1")).fetchall(),
                    [(1, "x", None)],
                )
                eq_(
                    conn.execute(text("SELECT * FROM t2")).fetchall(),
                    [(1, "y")],
                )
                _no_temp_artifacts(conn)

    def test_indexes_preserved_and_created(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            metadata = MetaData()
            Table(
                "thing",
                metadata,
                Column("id", Integer, primary_key=True),
                Column("code", String(10)),
                Index("ix_thing_code", "code"),
            )
            metadata.create_all(engine)
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO thing(code) VALUES ('a')"))

            def fn(op):
                def upgrade(**kw):
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("thing") as batch_op:
                            batch_op.alter_column(
                                "code",
                                existing_type=String(10),
                                nullable=False,
                            )
                            batch_op.create_index(
                                "ix_thing_code2", ["code"]
                            )
                            batch_op.drop_index("ix_thing_code")

                return upgrade

            _run_revision(engine, fn)
            with engine.connect() as conn:
                indexes = {
                    r[1]
                    for r in conn.exec_driver_sql(
                        "PRAGMA index_list(thing)"
                    )
                }
                assert "ix_thing_code2" in indexes
                assert "ix_thing_code" not in indexes
                _no_temp_artifacts(conn)

    def test_quoted_identifiers(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        'CREATE TABLE "weird table" ('
                        '"id col" INTEGER PRIMARY KEY, '
                        '"q ""quoted""" VARCHAR(20))'
                    )
                )
                conn.execute(
                    text(
                        'INSERT INTO "weird table" VALUES '
                        "(1, 'v1')"
                    )
                )

            def fn(op):
                def upgrade(**kw):
                    with op.sqlite_batch_group():
                        with op.batch_alter_table(
                            'weird table'
                        ) as batch_op:
                            batch_op.alter_column(
                                'q "quoted"',
                                existing_type=String(20),
                                nullable=False,
                            )

                return upgrade

            _run_revision(engine, fn)
            with engine.connect() as conn:
                cols = {r[1]: r[3] for r in conn.exec_driver_sql(
                    'PRAGMA table_info(\'weird table\')'
                )}
                eq_(cols['q "quoted"'], 1)
                eq_(
                    conn.execute(
                        text('SELECT * FROM "weird table"')
                    ).fetchall(),
                    [(1, "v1")],
                )
                _no_temp_artifacts(conn)


class GroupedBatchGuardTest(TestBase):
    def test_rejects_offline_mode(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.connect() as conn:
                context = MigrationContext.configure(
                    conn,
                    opts={"as_sql": True, "transaction_per_migration": True},
                )
                op = Operations(context)
                assert_raises_message(
                    CommandError,
                    "offline",
                    op.sqlite_batch_group().__enter__,
                )

    def test_rejects_without_transaction_per_migration(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.connect() as conn:
                context = MigrationContext.configure(conn, opts={})
                op = Operations(context)
                assert_raises_message(
                    CommandError,
                    "transaction_per_migration",
                    op.sqlite_batch_group().__enter__,
                )

    def test_rejects_nested_group(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.begin() as conn:
                conn.execute(text("CREATE TABLE t(id INTEGER PRIMARY KEY)"))
            with engine.connect() as conn:
                context = MigrationContext.configure(
                    conn, opts={"transaction_per_migration": True}
                )
                op = Operations(context)
                with op.sqlite_batch_group():
                    assert_raises_message(
                        CommandError,
                        "nested",
                        op.sqlite_batch_group().__enter__,
                    )

    def test_rejects_unsupported_operation(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.begin() as conn:
                conn.execute(text("CREATE TABLE t(id INTEGER PRIMARY KEY)"))
            with engine.connect() as conn:
                context = MigrationContext.configure(
                    conn, opts={"transaction_per_migration": True}
                )
                op = Operations(context)

                def fn():
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("t") as batch_op:
                            batch_op.create_table_comment("a table")

                assert_raises_message(
                    CommandError, "not supported", fn
                )

    def test_rejects_unsupported_table_comment_directive(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.begin() as conn:
                conn.execute(text("CREATE TABLE t(id INTEGER PRIMARY KEY)"))
            with engine.connect() as conn:
                context = MigrationContext.configure(
                    conn, opts={"transaction_per_migration": True}
                )
                op = Operations(context)

                def fn():
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("t") as batch_op:
                            batch_op.drop_table_comment()

                assert_raises_message(
                    CommandError, "not supported", fn
                )
                # nothing was changed
                with engine.connect() as check:
                    eq_(
                        check.execute(
                            text(
                                "SELECT name FROM sqlite_master "
                                "WHERE name='t'"
                            )
                        ).scalar(),
                        "t",
                    )

    def test_rejects_virtual_table(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.begin() as conn:
                conn.execute(
                    text("CREATE VIRTUAL TABLE ft USING fts5(content)")
                )
            with engine.connect() as conn:
                context = MigrationContext.configure(
                    conn, opts={"transaction_per_migration": True}
                )
                op = Operations(context)

                def fn():
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("ft") as batch_op:
                            batch_op.add_column(Column("x", Integer))

                assert_raises_message(
                    CommandError, "virtual table", fn
                )

    def test_rejects_trigger_dependency(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.begin() as conn:
                conn.execute(text("CREATE TABLE t(id INTEGER PRIMARY KEY)"))
                conn.execute(
                    text(
                        "CREATE TRIGGER tr AFTER INSERT ON t "
                        "BEGIN SELECT 1; END"
                    )
                )
            with engine.connect() as conn:
                context = MigrationContext.configure(
                    conn, opts={"transaction_per_migration": True}
                )
                op = Operations(context)

                def fn():
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("t") as batch_op:
                            batch_op.add_column(Column("x", Integer))

                assert_raises_message(
                    CommandError, "trigger", fn
                )

    def test_rejects_view_dependency(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.begin() as conn:
                conn.execute(text("CREATE TABLE t(id INTEGER PRIMARY KEY)"))
                conn.execute(text("CREATE VIEW v AS SELECT id FROM t"))
            with engine.connect() as conn:
                context = MigrationContext.configure(
                    conn, opts={"transaction_per_migration": True}
                )
                op = Operations(context)

                def fn():
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("t") as batch_op:
                            batch_op.add_column(Column("x", Integer))

                assert_raises_message(CommandError, "View", fn)

    def test_rejects_external_transaction(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.begin() as conn:
                conn.execute(text("CREATE TABLE t(id INTEGER PRIMARY KEY)"))
            with engine.connect() as conn:
                trans = conn.begin()
                try:
                    context = MigrationContext.configure(
                        conn,
                        opts={"transaction_per_migration": True},
                    )
                    op = Operations(context)
                    assert_raises_message(
                        CommandError,
                        "no transaction be active",
                        op.sqlite_batch_group().__enter__,
                    )
                finally:
                    trans.rollback()

    def test_rejects_copy_from(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            metadata = MetaData()
            t = Table(
                "t",
                metadata,
                Column("id", Integer, primary_key=True),
            )
            metadata.create_all(engine)
            with engine.connect() as conn:
                context = MigrationContext.configure(
                    conn, opts={"transaction_per_migration": True}
                )
                op = Operations(context)

                def fn():
                    with op.sqlite_batch_group():
                        with op.batch_alter_table(
                            "t", copy_from=t
                        ) as batch_op:
                            batch_op.add_column(Column("x", Integer))

                assert_raises_message(
                    CommandError, "copy_from", fn
                )

    def test_rejects_two_groups_one_revision(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.begin() as conn:
                conn.execute(text("CREATE TABLE t(id INTEGER PRIMARY KEY)"))

            def fn(op):
                def upgrade(**kw):
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("t") as batch_op:
                            batch_op.add_column(Column("x", Integer))
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("t") as batch_op:
                            batch_op.add_column(Column("y", Integer))

                return upgrade

            assert_raises_message(
                CommandError, "one group", _run_revision, engine, fn
            )
            # neither column was added
            with engine.connect() as conn:
                cols = [r[1] for r in conn.exec_driver_sql(
                    "PRAGMA table_info(t)"
                )]
                assert "x" not in cols and "y" not in cols


class GroupedBatchFailureInjectionTest(TestBase):
    def _engine_with_failure(self, tmp_path, fail_at):
        engine = create_engine(
            f"sqlite:///{tmp_path}/fail.db"
        )
        event.listen(engine, "connect", _fk_on_connect())
        # failures are only armed once the test setup has finished, so the
        # injected error hits the grouped migration rather than the schema
        # fixture.
        state = {"fail_at": fail_at, "copy_done": False, "armed": False}

        @event.listens_for(engine, "before_cursor_execute")
        def _maybe_fail(
            conn, cursor, statement, params, context_, executemany
        ):
            if not state["armed"]:
                return
            upper = statement.lstrip().upper()
            if fail_at == "copy" and upper.startswith("INSERT INTO ") and (
                "_alembic_group_tmp" in statement
            ):
                state["copy_done"] = True
                raise RuntimeError("injected copy failure")
            if (
                fail_at == "index"
                and upper.startswith("CREATE INDEX")
                and "ix_t_new" in statement
            ):
                raise RuntimeError("injected index failure")
            if fail_at == "version" and "alembic_version" in statement and (
                upper.startswith("INSERT") or upper.startswith("UPDATE")
            ):
                raise RuntimeError("injected version failure")

        return engine, state

    def _setup(self, engine):
        metadata = MetaData()
        Table(
            "t",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("v", String(20)),
            CheckConstraint("v IS NOT NULL OR 1=1", name="ck"),
            Index("ix_t_v", "v"),
        )
        metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO t VALUES (1,'a'),(2,'b')"))

    def _assert_pristine(self, engine):
        with engine.connect() as reader:
            cols = {r[1]: r[3] for r in reader.exec_driver_sql(
                "PRAGMA table_info(t)"
            )}
            # still nullable (migration did not commit)
            eq_(cols["v"], 0)
            eq_(
                reader.execute(text("SELECT COUNT(*) FROM t")).scalar(),
                2,
            )
            indexes = {
                r[1]
                for r in reader.exec_driver_sql("PRAGMA index_list(t)")
            }
            assert "ix_t_v" in indexes
            _no_temp_artifacts(reader)
            eq_(_pragma(reader, "foreign_keys"), 1)
            # the version table may have been created by normal
            # bookkeeping before the failing group, but no version row may
            # have been stamped.
            if reader.execute(
                text(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE name='alembic_version'"
                )
            ).scalar():
                eq_(
                    reader.execute(
                        text("SELECT COUNT(*) FROM alembic_version")
                    ).scalar(),
                    0,
                )

    @pytest.mark.parametrize("fail_at", ["copy", "index", "version"])
    def test_failure_rolls_back_from_new_connection(
        self, tmp_path, fail_at
    ):
        engine, state = self._engine_with_failure(tmp_path, fail_at)
        self._setup(engine)

        def fn(op):
            def upgrade(**kw):
                with op.sqlite_batch_group():
                    with op.batch_alter_table("t") as batch_op:
                        batch_op.alter_column(
                            "v",
                            existing_type=String(20),
                            nullable=False,
                        )
                        if fail_at == "index":
                            batch_op.create_index(
                                "ix_t_new", ["v"]
                            )

            return upgrade

        state["armed"] = True
        with pytest.raises(RuntimeError, match="injected"):
            _run_revision(engine, fn)

        # independent connection sees the pre-revision state
        self._assert_pristine(engine)
        engine.dispose()

    def test_reader_never_sees_half_migration(self, tmp_path):
        # dedicated engine whose single failure hook observes from an
        # independent connection *before* raising, so the observation is
        # guaranteed to happen while the group transaction is open.
        engine = create_engine(f"sqlite:///{tmp_path}/fail.db")
        event.listen(engine, "connect", _fk_on_connect())
        self._setup(engine)
        seen = []
        observer_engine = create_engine(
            f"sqlite:///{tmp_path}/fail.db"
        )

        @event.listens_for(engine, "before_cursor_execute")
        def _fail_and_observe(conn, cursor, statement, *a):
            normalized = statement.lstrip().upper()
            if (
                normalized.startswith("INSERT INTO ")
                and "_alembic_group_tmp" in statement
            ):
                with observer_engine.connect() as reader:
                    names = [
                        r[0]
                        for r in reader.execute(
                            text(
                                "SELECT name FROM sqlite_master "
                                "WHERE name LIKE '_alembic_group_tmp%'"
                            )
                        )
                    ]
                seen.append(names)
                raise RuntimeError("injected copy failure")

        def fn(op):
            def upgrade(**kw):
                with op.sqlite_batch_group():
                    with op.batch_alter_table("t") as batch_op:
                        batch_op.alter_column(
                            "v",
                            existing_type=String(20),
                            nullable=False,
                        )

            return upgrade

        with pytest.raises(RuntimeError, match="injected"):
            _run_revision(engine, fn)
        # every mid-flight observation saw no temp table from outside
        assert seen and all(names == [] for names in seen), seen
        engine.dispose()
        observer_engine.dispose()


class GroupedAutogenTest(TestBase):
    def setUp(self):
        self.env = staging_env()
        self.cfg = _sqlite_testing_config()

    def tearDown(self):
        clear_staging_env()

    def _env_py(self, target_metadata, render_mode):
        script = ScriptDirectory.from_config(self.cfg)
        env_location = script.env_py_location
        env_source = (
            _ENV_PY_TEMPLATE
            .replace(
                "__DB_URL_DIR__",
                os.path.dirname(
                    self.cfg.get_main_option("sqlalchemy.url").replace(
                        "sqlite:///", ""
                    )
                ),
            )
            .replace(
                "__DB_URL__",
                self.cfg.get_main_option("sqlalchemy.url"),
            )
            .replace("__RENDER_MODE__", repr(render_mode))
        )
        with open(env_location, "w") as f:
            f.write(env_source)
        # make the target metadata importable
        import sys
        import types

        module = types.ModuleType("grouped_targets")
        module.metadata = target_metadata
        sys.modules["grouped_targets"] = module
        return module

    def test_autogenerate_upgrade_downgrade_roundtrip(self, tmp_path):
        # current database schema
        engine = create_engine(
            self.cfg.get_main_option("sqlalchemy.url")
        )
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE orders(id INTEGER PRIMARY KEY, "
                    "note VARCHAR(20))"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE items(id INTEGER PRIMARY KEY, "
                    "order_id INTEGER, label VARCHAR(20), "
                    "CONSTRAINT fk_items_orders FOREIGN KEY(order_id) "
                    "REFERENCES orders(id))"
                )
            )
            conn.execute(text("INSERT INTO orders VALUES (1,'o1')"))
            conn.execute(text("INSERT INTO items VALUES (1,1,'i1')"))

        # desired metadata tightens nullability
        target = MetaData()
        Table(
            "orders",
            target,
            Column("id", Integer, primary_key=True),
            Column("note", String(20), nullable=False),
        )
        Table(
            "items",
            target,
            Column("id", Integer, primary_key=True),
            Column("order_id", Integer),
            Column("label", String(20), nullable=False),
            ForeignKeyConstraint(
                ["order_id"], ["orders.id"], name="fk_items_orders"
            ),
        )

        self._env_py(target, "grouped")
        command.revision(self.cfg, message="tighten", autogenerate=True)

        script = ScriptDirectory.from_config(self.cfg)
        revs = list(script.walk_revisions())
        eq_(len(revs), 1)
        generated = open(revs[0].path).read()
        assert "sqlite_batch_group" in generated
        assert "batch_alter_table" in generated

        # upgrade executes against the real reflected schema
        command.upgrade(self.cfg, "head")
        with engine.connect() as conn:
            eq_(_pragma_rows(conn, "foreign_key_check"), [])
            orders_cols = {r[1]: r[3] for r in conn.exec_driver_sql(
                "PRAGMA table_info(orders)"
            )}
            items_cols = {r[1]: r[3] for r in conn.exec_driver_sql(
                "PRAGMA table_info(items)"
            )}
            eq_(orders_cols["note"], 1)
            eq_(items_cols["label"], 1)
            _no_temp_artifacts(conn)

        # a second comparison against the target metadata yields no diffs
        from alembic.autogenerate import compare_metadata

        with engine.connect() as conn:
            context = MigrationContext.configure(conn)
            diffs = compare_metadata(context, target)
            eq_(diffs, [])

        # downgrade reverses the change and is also executable
        command.downgrade(self.cfg, "base")
        with engine.connect() as conn:
            orders_cols = {r[1]: r[3] for r in conn.exec_driver_sql(
                "PRAGMA table_info(orders)"
            )}
            eq_(orders_cols["note"], 0)
            eq_(
                conn.execute(
                    text("SELECT COUNT(*) FROM items")
                ).scalar(),
                1,
            )
        engine.dispose()

    def test_render_mode_rejects_create_table_diff(self):
        from alembic.autogenerate import api
        from alembic.operations import ops as _ops

        container = _ops.UpgradeOps(
            [
                _ops.ModifyTableOps(
                    "t",
                    [
                        _ops.AlterColumnOp(
                            "t",
                            "v",
                            modify_nullable=False,
                            existing_type=String(20),
                            existing_nullable=True,
                        )
                    ],
                ),
                _ops.DropTableOp("gone"),
            ]
        )
        assert_raises_message(
            CommandError,
            "unsupported operations",
            api.render_python_code,
            container,
            render_as_batch="grouped",
        )

    def test_render_modes_false_and_true_unchanged(self):
        from alembic.autogenerate import api
        from alembic.operations import ops as _ops

        container = _ops.UpgradeOps(
            [
                _ops.ModifyTableOps(
                    "t",
                    [
                        _ops.AlterColumnOp(
                            "t",
                            "v",
                            modify_nullable=False,
                            existing_type=String(20),
                            existing_nullable=True,
                        )
                    ],
                )
            ]
        )
        plain = api.render_python_code(container, render_as_batch=False)
        batched = api.render_python_code(container, render_as_batch=True)
        grouped = api.render_python_code(
            container, render_as_batch="grouped"
        )
        assert "sqlite_batch_group" not in plain
        assert "batch_alter_table" in batched
        assert "sqlite_batch_group" not in batched
        assert "sqlite_batch_group" in grouped
        assert "batch_alter_table" in grouped

    def test_invalid_render_mode_rejected(self):
        from alembic.autogenerate import api
        from alembic.operations import ops as _ops

        container = _ops.UpgradeOps([])
        assert_raises_message(
            ValueError,
            "render_as_batch",
            api.render_python_code,
            container,
            render_as_batch="bogus",
        )


_ENV_PY_TEMPLATE = """
from sqlalchemy import engine_from_config, pool
from logging.config import fileConfig
from alembic import context
import sys
sys.path.insert(0, "__DB_URL_DIR__")
from grouped_targets import metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata

def run_migrations_offline():
    context.configure(
        url="__DB_URL__",
        target_metadata=target_metadata,
        render_as_batch=__RENDER_MODE__,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=__RENDER_MODE__,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
"""
