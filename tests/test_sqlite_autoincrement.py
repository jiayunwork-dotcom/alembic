"""Regression tests for preserving SQLite ``AUTOINCREMENT`` history across
grouped batch rebuilds, and for the autogenerate/render support of the
``sqlite_autoincrement`` table attribute.

All tests run against temporary SQLite files and use an independent reader
connection to inspect the resulting state; no external database service is
required.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import Column
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from alembic import command
from alembic.autogenerate import api
from alembic.autogenerate import compare_metadata
from alembic.ddl._sqlite_autoincrement import ddl_uses_autoincrement
from alembic.migration import MigrationContext
from alembic.migration import RevisionStep
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from alembic.script.revision import RevisionMap
from alembic.testing import eq_
from alembic.testing import TestBase
from alembic.testing.env import _sqlite_testing_config
from alembic.testing.env import clear_staging_env
from alembic.testing.env import staging_env
from alembic.util import CommandError

INT64_MAX = 2**63 - 1

_probe_counter = [0]


def _fk_on_connect():
    def _listen(dbapi_connection, rec):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return _listen


@contextmanager
def _sqlite_engine(tmp_path, *, fk_default_on=True, suffix=".db"):
    path = tmp_path / ("autoincrement" + suffix)
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


def _create_sql(conn, table):
    return conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).scalar()


def _sequence(conn, table):
    if not conn.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'"
    ).first():
        return None
    row = conn.exec_driver_sql(
        "SELECT seq FROM sqlite_sequence WHERE name=?", (table,)
    ).first()
    return None if row is None else int(row[0])


def _next_id(conn, table, note=None):
    """Insert a row without an id and return the allocated value.

    The insert is committed so the observation is durable and the next
    allocation starts after it; the caller may be a throw-away reader
    connection.  A unique marker is used so repeated probes never read
    back an earlier row.
    """

    if note is None:
        _probe_counter[0] += 1
        note = "probe-%d" % _probe_counter[0]
    conn.exec_driver_sql(f'INSERT INTO "{table}" (note) VALUES (?)', (note,))
    value = conn.exec_driver_sql(
        f'SELECT id FROM "{table}" WHERE note=?', (note,)
    ).scalar()
    conn.commit()
    return value


def _no_temp_artifacts(conn):
    rows = conn.execute(
        text(
            "SELECT name FROM sqlite_master "
            "WHERE name LIKE '%_alembic%tmp%'"
        )
    ).fetchall()
    assert rows == [], rows


class _ManualScript:
    def __init__(self, upgrade_fn, revision="rev1", down=("rev0",)):
        self.module = SimpleNamespace(
            upgrade=upgrade_fn, downgrade=lambda **kw: None
        )
        self.revision = revision
        self.doc = None
        self._normalized_down_revisions = tuple(down)
        self._versioned_down_revisions = tuple(down)


def _run_revision(engine, revision_fn, *, opts=None, revision="rev1"):
    with engine.connect() as conn:
        final_opts = {"transaction_per_migration": True}
        if opts:
            final_opts.update(opts)
        final_opts["fn"] = lambda heads, ctx: [
            RevisionStep(
                RevisionMap(()),
                _ManualScript(revision_fn(Operations(ctx)), revision=revision),
                True,
            )
        ]
        context = MigrationContext.configure(conn, opts=final_opts)
        context.run_migrations()


def _autoincrement_table_sql(
    table="fact", *, autoincrement=True, not_null_note=False
) -> str:
    increment = " AUTOINCREMENT" if autoincrement else ""
    note = " NOT NULL" if not_null_note else ""
    return (
        f'CREATE TABLE "{table}" '
        f"(id INTEGER NOT NULL PRIMARY KEY{increment}, "
        f"note VARCHAR(20){note})"
    )


class DDLDetectionTest(TestBase):
    @pytest.mark.parametrize(
        "sql,expected",
        [
            (
                "CREATE TABLE t(id INTEGER PRIMARY KEY AUTOINCREMENT)",
                True,
            ),
            (
                "create table t(id integer primary key autoincrement)",
                True,
            ),
            (
                "CREATE TABLE t(id INTEGER, PRIMARY KEY(id AUTOINCREMENT))",
                True,
            ),
            ("CREATE TABLE t(id INTEGER PRIMARY KEY)", False),
            (
                'CREATE TABLE "AUTOINCREMENT"(id INTEGER PRIMARY KEY, '
                "note TEXT DEFAULT 'AUTOINCREMENT')",
                False,
            ),
            (
                'CREATE TABLE t(id INTEGER PRIMARY KEY, "AUTOINCREMENT" TEXT)',
                False,
            ),
            (
                "CREATE TABLE t(id INTEGER PRIMARY KEY) " "-- AUTOINCREMENT",
                False,
            ),
            (
                "CREATE TABLE t(id INTEGER PRIMARY KEY) "
                "/* AUTOINCREMENT */",
                False,
            ),
            (
                "CREATE TABLE t(id INTEGER PRIMARY KEY, "
                "note TEXT DEFAULT 'AUTOINCREMENT')",
                False,
            ),
            (
                'CREATE TABLE "a b""c"(id INTEGER PRIMARY KEY, '
                '"x AUTOINCREMENT" TEXT)',
                False,
            ),
            (None, False),
            ("", False),
        ],
    )
    def test_ddl_detection(self, sql, expected):
        eq_(ddl_uses_autoincrement(sql), expected)


class PreserveHistoryTest(TestBase):
    def _seed(self, engine, create_sql=None):
        with engine.begin() as conn:
            conn.exec_driver_sql(create_sql or _autoincrement_table_sql())
            conn.exec_driver_sql(
                "INSERT INTO fact(id, note) VALUES (1, 'a'), (1000, 'b')"
            )
            conn.exec_driver_sql("DELETE FROM fact WHERE id = 1000")

    def _rebuild(self, op, *, table_kwargs=None, alter_id=False):
        with op.sqlite_batch_group():
            with op.batch_alter_table(
                "fact", table_kwargs=table_kwargs or {}
            ) as batch_op:
                if alter_id:
                    batch_op.alter_column(
                        "id",
                        existing_type=Integer,
                        nullable=False,
                    )
                else:
                    batch_op.alter_column(
                        "note",
                        existing_type=String(20),
                        nullable=False,
                    )

    def test_minimal_reproduction(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            self._seed(engine)

            def fn(op):
                return lambda **kw: self._rebuild(op)

            _run_revision(engine, fn)
            with engine.connect() as reader:
                assert "AUTOINCREMENT" in _create_sql(reader, "fact")
                eq_(_sequence(reader, "fact"), 1000)
                eq_(_next_id(reader, "fact"), 1001)
                _no_temp_artifacts(reader)

    def test_explicit_table_kwargs_true_keeps_history(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            self._seed(engine)

            def fn(op):
                return lambda **kw: self._rebuild(
                    op, table_kwargs={"sqlite_autoincrement": True}
                )

            _run_revision(engine, fn)
            with engine.connect() as reader:
                eq_(_next_id(reader, "fact"), 1001)

    def test_emptied_table_does_not_reuse_history(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            self._seed(engine)
            with engine.begin() as conn:
                conn.exec_driver_sql("DELETE FROM fact")

            def fn(op):
                return lambda **kw: self._rebuild(op)

            _run_revision(engine, fn)
            with engine.connect() as reader:
                eq_(_sequence(reader, "fact"), 1000)
                eq_(_next_id(reader, "fact"), 1001)

    def test_deleted_high_row_not_replaced_by_current_max(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            self._seed(engine)
            # the live maximum is 1; the committed mark must still be 1000
            with engine.connect() as reader:
                eq_(
                    reader.exec_driver_sql(
                        "SELECT max(id) FROM fact"
                    ).scalar(),
                    1,
                )

            def fn(op):
                return lambda **kw: self._rebuild(op)

            _run_revision(engine, fn)
            with engine.connect() as reader:
                eq_(_next_id(reader, "fact"), 1001)

    def test_plain_table_keeps_ordinary_allocation(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            self._seed(
                engine,
                create_sql=_autoincrement_table_sql(autoincrement=False),
            )

            def fn(op):
                return lambda **kw: self._rebuild(op)

            _run_revision(engine, fn)
            with engine.connect() as reader:
                assert "AUTOINCREMENT" not in _create_sql(reader, "fact")
                eq_(_next_id(reader, "fact"), 2)

    def test_explicit_disable_drops_guarantee(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            self._seed(engine)

            def fn(op):
                return lambda **kw: self._rebuild(
                    op, table_kwargs={"sqlite_autoincrement": False}
                )

            _run_revision(engine, fn)
            with engine.connect() as reader:
                assert "AUTOINCREMENT" not in _create_sql(reader, "fact")
                eq_(_next_id(reader, "fact"), 2)

    def test_multi_table_histories_are_independent(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.begin() as conn:
                for name, mark in (("a", 500), ("b", 7000), ("c", 9000)):
                    conn.exec_driver_sql(_autoincrement_table_sql(table=name))
                    conn.exec_driver_sql(
                        f"INSERT INTO {name}(id, note) "
                        "VALUES (1, 'x'), (:mark, 'y')".replace(
                            ":mark", str(mark)
                        )
                    )
                    conn.exec_driver_sql(f"DELETE FROM {name} WHERE id > 1")
                conn.exec_driver_sql(_autoincrement_table_sql(table="out"))
                conn.exec_driver_sql(
                    "INSERT INTO out(id, note) VALUES (1, 'x'), (123456, 'y')"
                )
                conn.exec_driver_sql("DELETE FROM out WHERE id > 1")

            def fn(op):
                def upgrade(**kw):
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("a") as batch_op:
                            batch_op.alter_column(
                                "note",
                                existing_type=String(20),
                                nullable=False,
                            )
                        # native ALTER path, not a rebuild
                        with op.batch_alter_table("b") as batch_op:
                            batch_op.add_column(Column("extra", Integer))
                        with op.batch_alter_table("c") as batch_op:
                            batch_op.alter_column(
                                "note",
                                existing_type=String(20),
                                nullable=False,
                            )

                return upgrade

            _run_revision(engine, fn)
            with engine.connect() as reader:
                eq_(_sequence(reader, "a"), 500)
                eq_(_sequence(reader, "b"), 7000)
                eq_(_sequence(reader, "c"), 9000)
                eq_(_sequence(reader, "out"), 123456)
                eq_(_next_id(reader, "a"), 501)
                # b took the native ALTER path: its history was never
                # touched by the rebuild
                eq_(_next_id(reader, "b"), 7001)
                eq_(_next_id(reader, "c"), 9001)
                eq_(_next_id(reader, "out"), 123457)

    def test_attribute_and_column_change_together(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            self._seed(engine)

            def fn(op):
                def upgrade(**kw):
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("fact") as batch_op:
                            batch_op.alter_column(
                                "id",
                                existing_type=Integer,
                                nullable=False,
                            )
                            batch_op.alter_column(
                                "note",
                                existing_type=String(20),
                                nullable=False,
                            )

                return upgrade

            _run_revision(engine, fn)
            with engine.connect() as reader:
                assert "AUTOINCREMENT" in _create_sql(reader, "fact")
                cols = {
                    r[1]: r[3]
                    for r in reader.exec_driver_sql("PRAGMA table_info(fact)")
                }
                eq_(cols["id"], 1)
                eq_(cols["note"], 1)
                eq_(_next_id(reader, "fact"), 1001)

    def test_quoted_identifiers(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    'CREATE TABLE "weird table" '
                    '("id col" INTEGER PRIMARY KEY AUTOINCREMENT, '
                    '"q ""x""" VARCHAR(20))'
                )
                conn.exec_driver_sql(
                    "INSERT INTO \"weird table\" VALUES (1, 'a'), (900, 'b')"
                )
                conn.exec_driver_sql(
                    'DELETE FROM "weird table" WHERE "id col" = 900'
                )

            def fn(op):
                def upgrade(**kw):
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("weird table") as batch_op:
                            batch_op.alter_column(
                                'q "x"',
                                existing_type=String(20),
                                nullable=False,
                            )

                return upgrade

            _run_revision(engine, fn)
            with engine.connect() as reader:
                assert "AUTOINCREMENT" in _create_sql(reader, "weird table")
                eq_(_sequence(reader, "weird table"), 900)
                reader.exec_driver_sql(
                    'INSERT INTO "weird table" ("q ""x""") VALUES (\'c\')'
                )
                eq_(
                    reader.exec_driver_sql(
                        'SELECT "id col" FROM "weird table" '
                        'WHERE "q ""x""" = \'c\''
                    ).scalar(),
                    901,
                )

    def test_keyword_false_positive(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    'CREATE TABLE "AUTOINCREMENT"('
                    "id INTEGER PRIMARY KEY, "
                    "note TEXT DEFAULT 'AUTOINCREMENT')"
                )
                conn.exec_driver_sql(
                    "INSERT INTO \"AUTOINCREMENT\" VALUES (1, 'a'), (50, 'b')"
                )
                conn.exec_driver_sql(
                    'DELETE FROM "AUTOINCREMENT" WHERE id = 50'
                )
                conn.exec_driver_sql(
                    "CREATE TABLE t2(id INTEGER PRIMARY KEY, note TEXT) "
                    "-- AUTOINCREMENT"
                )
                conn.exec_driver_sql(
                    "INSERT INTO t2 VALUES (1, 'a'), (50, 'b')"
                )
                conn.exec_driver_sql("DELETE FROM t2 WHERE id = 50")

            def fn(op):
                def upgrade(**kw):
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("AUTOINCREMENT") as batch_op:
                            batch_op.alter_column(
                                "note",
                                existing_type=String(20),
                                nullable=False,
                            )
                        with op.batch_alter_table("t2") as batch_op:
                            batch_op.alter_column(
                                "note",
                                existing_type=String(20),
                                nullable=False,
                            )

                return upgrade

            _run_revision(engine, fn)
            with engine.connect() as reader:
                # neither table has the real attribute, so neither CREATE
                # may acquire it and ordinary allocation stays in effect
                assert "PRIMARY KEY AUTOINCREMENT" not in _create_sql(
                    reader, "AUTOINCREMENT"
                )
                assert "PRIMARY KEY AUTOINCREMENT" not in _create_sql(
                    reader, "t2"
                )
                eq_(_sequence(reader, "AUTOINCREMENT"), None)
                eq_(_sequence(reader, "t2"), None)
                reader.exec_driver_sql(
                    "INSERT INTO \"AUTOINCREMENT\" (note) VALUES ('c')"
                )
                eq_(
                    reader.exec_driver_sql(
                        'SELECT id FROM "AUTOINCREMENT" ' "WHERE note = 'c'"
                    ).scalar(),
                    2,
                )

    def test_explicit_autoincrement_option_enables_attribute(self, tmp_path):
        # the attribute is explicitly requested through table_kwargs; the
        # rebuild must apply it even when the live database is plain.
        with _sqlite_engine(tmp_path) as engine:
            self._seed(
                engine,
                create_sql=_autoincrement_table_sql(autoincrement=False),
            )

            def fn(op):
                return lambda **kw: self._rebuild(
                    op, table_kwargs={"sqlite_autoincrement": True}
                )

            _run_revision(engine, fn)
            with engine.connect() as reader:
                assert "AUTOINCREMENT" in _create_sql(reader, "fact")
                # the plain table had no committed history, so the fresh
                # history starts from the data present at that time
                eq_(_next_id(reader, "fact"), 2)

    def test_int64_max_still_fails_after_migration(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.begin() as conn:
                conn.exec_driver_sql(_autoincrement_table_sql())
                conn.exec_driver_sql(
                    "INSERT INTO fact(id, note) VALUES (1, 'a')"
                )
                conn.exec_driver_sql(
                    "UPDATE sqlite_sequence SET seq=? WHERE name='fact'",
                    (INT64_MAX,),
                )

            def fn(op):
                return lambda **kw: self._rebuild(op)

            _run_revision(engine, fn)
            with engine.connect() as reader:
                eq_(_sequence(reader, "fact"), INT64_MAX)
                # AUTOINCREMENT never reuses a number; at the signed 64-bit
                # ceiling the insert fails rather than handing out a value
                with pytest.raises(OperationalError):
                    reader.exec_driver_sql(
                        "INSERT INTO fact(note) VALUES ('overflow')"
                    )

    def test_rolled_back_insert_is_not_history(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            self._seed(engine)
            with engine.connect() as conn:
                trans = conn.begin()
                conn.exec_driver_sql(
                    "INSERT INTO fact(note) VALUES ('rolled-back')"
                )
                trans.rollback()
            with engine.connect() as reader:
                eq_(_sequence(reader, "fact"), 1000)

            def fn(op):
                return lambda **kw: self._rebuild(op)

            _run_revision(engine, fn)
            with engine.connect() as reader:
                eq_(_sequence(reader, "fact"), 1000)
                eq_(_next_id(reader, "fact"), 1001)


class ForeignKeyTest(TestBase):
    def test_fk_relationships_preserved(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "CREATE TABLE parent("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "name VARCHAR(20), "
                    "parent_id INTEGER, "
                    "CONSTRAINT fk_self FOREIGN KEY(parent_id) "
                    "REFERENCES parent(id))"
                )
                conn.exec_driver_sql(
                    "CREATE TABLE child("
                    "id INTEGER PRIMARY KEY, "
                    "parent_id INTEGER, "
                    "CONSTRAINT fk_c_p FOREIGN KEY(parent_id) "
                    "REFERENCES parent(id) ON DELETE CASCADE)"
                )
                conn.exec_driver_sql(
                    "INSERT INTO parent(id, name) VALUES (1, 'a'), (1000, 'b')"
                )
                conn.exec_driver_sql("INSERT INTO child VALUES (1, 1), (2, 1)")
                conn.exec_driver_sql("DELETE FROM parent WHERE id = 1000")

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
            with engine.connect() as reader:
                eq_(_pragma_rows(reader, "foreign_key_check"), [])
                fks = reader.exec_driver_sql(
                    "PRAGMA foreign_key_list(child)"
                ).fetchall()
                eq_(len(fks), 1)
                eq_(fks[0][6], "CASCADE")
                eq_(
                    reader.execute(
                        text("SELECT COUNT(*) FROM child")
                    ).scalar(),
                    2,
                )
                reader.exec_driver_sql("INSERT INTO parent(name) VALUES ('c')")
                eq_(
                    reader.exec_driver_sql(
                        "SELECT id FROM parent WHERE name = 'c'"
                    ).scalar(),
                    1001,
                )

    def test_intra_group_and_outside_references(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.begin() as conn:
                # two AUTOINCREMENT tables referencing each other, plus an
                # outside table referencing one of them
                conn.exec_driver_sql(
                    "CREATE TABLE a(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "b_id INTEGER, note VARCHAR(20), "
                    "CONSTRAINT fk_a_b FOREIGN KEY(b_id) REFERENCES b(id))"
                )
                conn.exec_driver_sql(
                    "CREATE TABLE b(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "a_id INTEGER, note VARCHAR(20), "
                    "CONSTRAINT fk_b_a FOREIGN KEY(a_id) REFERENCES a(id))"
                )
                conn.exec_driver_sql(
                    "CREATE TABLE out(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "a_id INTEGER, "
                    "CONSTRAINT fk_out_a FOREIGN KEY(a_id) "
                    "REFERENCES a(id) ON DELETE CASCADE)"
                )
                conn.exec_driver_sql(
                    "INSERT INTO a VALUES (1, NULL, 'a'), (500, NULL, 'a')"
                )
                conn.exec_driver_sql(
                    "INSERT INTO b VALUES (1, 1, 'b'), (7000, NULL, 'b')"
                )
                conn.exec_driver_sql("INSERT INTO out VALUES (1, 1)")
                conn.exec_driver_sql("DELETE FROM a WHERE id > 1")
                conn.exec_driver_sql("DELETE FROM b WHERE id > 1")

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
                                "note",
                                existing_type=String(20),
                                nullable=False,
                            )

                return upgrade

            _run_revision(engine, fn)
            with engine.connect() as reader:
                eq_(_pragma_rows(reader, "foreign_key_check"), [])
                eq_(_sequence(reader, "a"), 500)
                eq_(_sequence(reader, "b"), 7000)
                eq_(_sequence(reader, "out"), 1)
                reader.exec_driver_sql("INSERT INTO a(note) VALUES ('n')")
                reader.exec_driver_sql("INSERT INTO b(note) VALUES ('n')")
                reader.exec_driver_sql("INSERT INTO out(a_id) VALUES (1)")
                reader.commit()
                eq_(
                    reader.exec_driver_sql(
                        "SELECT id FROM a WHERE note='n'"
                    ).scalar(),
                    501,
                )
                eq_(
                    reader.exec_driver_sql(
                        "SELECT id FROM b WHERE note='n'"
                    ).scalar(),
                    7001,
                )
                eq_(
                    reader.exec_driver_sql(
                        "SELECT id FROM out ORDER BY id DESC"
                    ).scalar(),
                    2,
                )


class DirectCallTest(TestBase):
    def test_set_sqlite_autoincrement_requires_batch(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.begin() as conn:
                conn.exec_driver_sql("CREATE TABLE t(id INTEGER PRIMARY KEY)")
            with engine.connect() as conn:
                op = Operations(MigrationContext.configure(conn))
                with pytest.raises(CommandError, match="batch_alter_table"):
                    op.set_sqlite_autoincrement("t", enabled=True)


class AttrToggleTest(TestBase):
    def test_enable_disable_reenable_roundtrip(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    _autoincrement_table_sql(autoincrement=False)
                )
                conn.exec_driver_sql(
                    "INSERT INTO fact(id, note) VALUES (1, 'a'), (1000, 'b')"
                )
                conn.exec_driver_sql("DELETE FROM fact WHERE id = 1000")

            def fn(op):
                def upgrade(**kw):
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("fact") as batch_op:
                            batch_op.set_sqlite_autoincrement(
                                enabled=True, existing_enabled=False
                            )

                return upgrade

            _run_revision(engine, fn)
            with engine.connect() as reader:
                assert "AUTOINCREMENT" in _create_sql(reader, "fact")
                # enabling starts a fresh history from the data present
                eq_(_next_id(reader, "fact"), 2)
                reader.exec_driver_sql("INSERT INTO fact(note) VALUES ('b')")
                eq_(
                    reader.exec_driver_sql(
                        "SELECT max(id) FROM fact"
                    ).scalar(),
                    3,
                )
                reader.exec_driver_sql("DELETE FROM fact WHERE id = 3")
                reader.commit()

            # downgrade disables the attribute again
            def down(op):
                def downgrade(**kw):
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("fact") as batch_op:
                            batch_op.set_sqlite_autoincrement(
                                enabled=False, existing_enabled=True
                            )

                return downgrade

            _run_revision(engine, down, revision="rev2")
            with engine.connect() as reader:
                assert "AUTOINCREMENT" not in _create_sql(reader, "fact")
                # ordinary allocation: id 1 and 2 are occupied, so the
                # first free number is handed out
                eq_(_next_id(reader, "fact"), 3)

            # re-enabling starts a fresh history from the data present at
            # that time: the highest existing id is 3, so even after the
            # table is emptied the next number is 4, not 1.
            _run_revision(engine, fn, revision="rev3")
            with engine.connect() as reader:
                assert "AUTOINCREMENT" in _create_sql(reader, "fact")
                eq_(_sequence(reader, "fact"), 3)
                reader.exec_driver_sql("DELETE FROM fact")
                reader.commit()
                eq_(_next_id(reader, "fact"), 4)

    def test_grouped_attr_change_with_column_change(self, tmp_path):
        with _sqlite_engine(tmp_path) as engine:
            with engine.begin() as conn:
                conn.exec_driver_sql(_autoincrement_table_sql())
                conn.exec_driver_sql(
                    "INSERT INTO fact(id, note) VALUES (1, 'a'), (1000, 'b')"
                )
                conn.exec_driver_sql("DELETE FROM fact WHERE id = 1000")

            def fn(op):
                def upgrade(**kw):
                    with op.sqlite_batch_group():
                        with op.batch_alter_table("fact") as batch_op:
                            batch_op.set_sqlite_autoincrement(
                                enabled=True, existing_enabled=True
                            )
                            batch_op.alter_column(
                                "note",
                                existing_type=String(20),
                                nullable=False,
                            )

                return upgrade

            _run_revision(engine, fn)
            with engine.connect() as reader:
                assert "AUTOINCREMENT" in _create_sql(reader, "fact")
                eq_(_next_id(reader, "fact"), 1001)


class TransactionalTest(TestBase):
    def _seed(self, engine):
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE alembic_version "
                "(version_num VARCHAR(32) NOT NULL)"
            )
            conn.exec_driver_sql("INSERT INTO alembic_version VALUES ('rev0')")
            conn.exec_driver_sql(_autoincrement_table_sql())
            conn.exec_driver_sql(
                "INSERT INTO fact(id, note) VALUES (1, 'a'), (1000, 'b'), "
                "(5000, 'c')"
            )
            conn.exec_driver_sql("DELETE FROM fact WHERE id > 1")

    @pytest.mark.parametrize("fail_at", ["copy", "sequence", "version"])
    def test_failure_leaves_original_state(self, tmp_path, fail_at):
        engine = create_engine(f"sqlite:///{tmp_path}/fail.db")
        event.listen(engine, "connect", _fk_on_connect())
        self._seed(engine)
        state = {"armed": False}

        @event.listens_for(engine, "before_cursor_execute")
        def _maybe_fail(conn, cursor, statement, params, ctx, many):
            if not state["armed"]:
                return
            upper = statement.lstrip().upper()
            if (
                fail_at == "copy"
                and upper.startswith("INSERT INTO ")
                and "_alembic_group_tmp" in statement
            ):
                raise RuntimeError("injected copy failure")
            if (
                fail_at == "sequence"
                and "sqlite_sequence" in statement
                and upper.startswith(("INSERT", "UPDATE"))
            ):
                raise RuntimeError("injected sequence failure")
            if (
                fail_at == "version"
                and "alembic_version" in statement
                and upper.startswith(("INSERT", "UPDATE"))
            ):
                raise RuntimeError("injected version failure")

        def fn(op):
            def upgrade(**kw):
                with op.sqlite_batch_group():
                    with op.batch_alter_table("fact") as batch_op:
                        batch_op.alter_column(
                            "note",
                            existing_type=String(20),
                            nullable=False,
                        )

            return upgrade

        state["armed"] = True
        with pytest.raises(RuntimeError, match="injected"):
            _run_revision(engine, fn)
        state["armed"] = False

        with engine.connect() as reader:
            # structure, data, sequence, version and temp objects all match
            # the pre-migration state, observed from a new connection
            assert "note VARCHAR(20) NOT NULL" not in _create_sql(
                reader, "fact"
            )
            assert "AUTOINCREMENT" in _create_sql(reader, "fact")
            eq_(
                reader.execute(text("SELECT * FROM fact")).fetchall(),
                [(1, "a")],
            )
            eq_(_sequence(reader, "fact"), 5000)
            eq_(
                reader.execute(
                    text("SELECT version_num FROM alembic_version")
                ).fetchall(),
                [("rev0",)],
            )
            _no_temp_artifacts(reader)
            eq_(_pragma(reader, "foreign_keys"), 1)
            # the connection remains usable for a subsequent legal
            # migration
            eq_(reader.execute(text("SELECT 1")).scalar(), 1)

        # the same, now legal, migration still works after the failure
        state["armed"] = False
        _run_revision(engine, fn)
        with engine.connect() as reader:
            eq_(_next_id(reader, "fact"), 5001)
            eq_(
                reader.execute(
                    text("SELECT version_num FROM alembic_version")
                ).fetchall(),
                [("rev1",)],
            )
        engine.dispose()


class AttributeAutogenTest(TestBase):
    def setUp(self):
        self.env = staging_env()
        self.cfg = _sqlite_testing_config()

    def tearDown(self):
        clear_staging_env()

    def _write_env(self, target_metadata, render_mode="grouped"):
        script = ScriptDirectory.from_config(self.cfg)
        env_location = script.env_py_location
        env_source = (
            _ENV_PY_TEMPLATE.replace(
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
        import sys
        import types

        module = types.ModuleType("grouped_targets")
        module.metadata = target_metadata
        sys.modules["grouped_targets"] = module

    def _engine(self):
        return create_engine(self.cfg.get_main_option("sqlalchemy.url"))

    def _target(self, *, enabled, nullable_note=False):
        md = MetaData()
        Table(
            "fact",
            md,
            Column("id", Integer, primary_key=True),
            Column("note", String(20), nullable=nullable_note),
            sqlite_autoincrement=enabled,
        )
        return md

    @pytest.mark.parametrize("target_enabled", [True, False])
    def test_attribute_only_diff_renders_executable_roundtrip(
        self, tmp_path, target_enabled
    ):
        engine = self._engine()
        with engine.begin() as conn:
            conn.exec_driver_sql(
                _autoincrement_table_sql(autoincrement=not target_enabled)
            )
            conn.exec_driver_sql(
                "INSERT INTO fact(id, note) VALUES (1, 'a'), (1000, 'b')"
            )
            conn.exec_driver_sql("DELETE FROM fact WHERE id = 1000")

        target = self._target(enabled=target_enabled)
        self._write_env(target, "grouped")
        command.revision(self.cfg, message="toggle", autogenerate=True)

        script = ScriptDirectory.from_config(self.cfg)
        revs = list(script.walk_revisions())
        generated = open(revs[0].path).read()
        assert "sqlite_batch_group" in generated
        assert "set_sqlite_autoincrement" in generated

        command.upgrade(self.cfg, "head")
        with engine.connect() as reader:
            assert (
                "AUTOINCREMENT" in _create_sql(reader, "fact")
            ) == target_enabled
            eq_(
                compare_metadata(
                    MigrationContext.configure(
                        reader, opts={"render_as_batch": "grouped"}
                    ),
                    target,
                ),
                [],
            )
            eq_(
                reader.execute(
                    text("SELECT version_num FROM alembic_version")
                ).fetchall(),
                [(revs[0].revision,)],
            )
            if target_enabled:
                # turning it on starts from the data present: max id is 1
                eq_(_next_id(reader, "fact"), 2)
            else:
                # ordinary allocation reuses the first free number
                eq_(_next_id(reader, "fact"), 2)

        command.downgrade(self.cfg, "base")
        with engine.connect() as reader:
            assert (
                "AUTOINCREMENT" in _create_sql(reader, "fact")
            ) != target_enabled
            eq_(
                reader.execute(
                    text("SELECT COUNT(*) FROM alembic_version")
                ).scalar(),
                0,
            )
            # the downgrade direction allocates according to the restored
            # attribute, checked after it has run
            if target_enabled:
                # downgrade disabled it: ordinary allocation, ids 1 and 2
                # are occupied so the first free number is 3
                eq_(_next_id(reader, "fact"), 3)
            else:
                # downgrade re-enabled it: fresh history from data (max 2)
                eq_(_next_id(reader, "fact"), 3)
        engine.dispose()

    def test_enable_after_deleted_high_row_keeps_fresh_history(self, tmp_path):
        # enabling on a plain table on a database whose rows above the new
        # history were deleted: the fresh history starts from the data
        # present, which is how SQLite's ordinary allocation works.
        engine = self._engine()
        with engine.begin() as conn:
            conn.exec_driver_sql(_autoincrement_table_sql(autoincrement=False))
            conn.exec_driver_sql(
                "INSERT INTO fact(id, note) VALUES (1, 'a'), (1000, 'b')"
            )
            conn.exec_driver_sql("DELETE FROM fact WHERE id = 1000")

        target = self._target(enabled=True)
        self._write_env(target, "grouped")
        command.revision(self.cfg, message="enable", autogenerate=True)
        command.upgrade(self.cfg, "head")
        with engine.connect() as reader:
            assert "AUTOINCREMENT" in _create_sql(reader, "fact")
            eq_(
                compare_metadata(
                    MigrationContext.configure(
                        reader, opts={"render_as_batch": "grouped"}
                    ),
                    target,
                ),
                [],
            )
        engine.dispose()

    def test_unrelated_diff_preserves_existing_attribute(self, tmp_path):
        engine = self._engine()
        with engine.begin() as conn:
            conn.exec_driver_sql(_autoincrement_table_sql(not_null_note=False))
            conn.exec_driver_sql(
                "INSERT INTO fact(id, note) VALUES (1, 'a'), (1000, 'b')"
            )
            conn.exec_driver_sql("DELETE FROM fact WHERE id = 1000")

        # metadata does not state the option, and only nullability differs
        md = MetaData()
        Table(
            "fact",
            md,
            Column("id", Integer, primary_key=True, nullable=False),
            Column("note", String(20), nullable=False),
        )
        self._write_env(md, "grouped")
        command.revision(self.cfg, message="notnull", autogenerate=True)
        command.upgrade(self.cfg, "head")
        with engine.connect() as reader:
            assert "AUTOINCREMENT" in _create_sql(reader, "fact")
            eq_(_next_id(reader, "fact"), 1001)
            eq_(compare_metadata(MigrationContext.configure(reader), md), [])
        engine.dispose()

    def test_diff_only_emitted_in_grouped_mode(self, tmp_path):
        engine = self._engine()
        with engine.begin() as conn:
            conn.exec_driver_sql(
                _autoincrement_table_sql(
                    autoincrement=False, not_null_note=False
                )
            )
        target = self._target(enabled=True, nullable_note=True)
        with engine.connect() as conn:
            for mode in (False, True):
                ctx = MigrationContext.configure(
                    conn, opts={"render_as_batch": mode}
                )
                eq_(compare_metadata(ctx, target), [])
            ctx = MigrationContext.configure(
                conn, opts={"render_as_batch": "grouped"}
            )
            diffs = compare_metadata(ctx, target)
            eq_(len(diffs), 1)
            eq_(diffs[0][0], "modify_table_autoincrement")
        engine.dispose()

    def test_plain_and_batch_renderings_unchanged(self, tmp_path):
        from alembic.operations import ops as _ops

        container = _ops.UpgradeOps(
            [
                _ops.ModifyTableOps(
                    "t",
                    [
                        _ops.SetTableAutoincrementOp(
                            "t", enabled=True, existing_enabled=False
                        )
                    ],
                )
            ]
        )
        grouped = api.render_python_code(container, render_as_batch="grouped")
        assert "sqlite_batch_group" in grouped
        assert "set_sqlite_autoincrement" in grouped

    def test_hand_written_group_migration(self, tmp_path):
        engine = self._engine()
        with engine.begin() as conn:
            conn.exec_driver_sql(_autoincrement_table_sql())
            conn.exec_driver_sql(
                "INSERT INTO fact(id, note) VALUES (1, 'a'), (1000, 'b')"
            )
            conn.exec_driver_sql("DELETE FROM fact WHERE id = 1000")

        target = self._target(enabled=True, nullable_note=True)
        self._write_env(target, "grouped")
        command.revision(self.cfg, message="empty")
        script = ScriptDirectory.from_config(self.cfg)
        rev = list(script.walk_revisions())[0]
        with open(rev.path, "w") as f:
            f.write(
                '"""hand written"""\n'
                "revision = %r\n"
                "down_revision = None\n"
                "branch_labels = None\n"
                "depends_on = None\n"
                "from alembic import op\n"
                "import sqlalchemy as sa\n"
                "\n"
                "def upgrade():\n"
                "    with op.sqlite_batch_group():\n"
                "        with op.batch_alter_table('fact') as batch_op:\n"
                "            batch_op.set_sqlite_autoincrement(\n"
                "                enabled=True, existing_enabled=True)\n"
                "\n"
                "def downgrade():\n"
                "    with op.sqlite_batch_group():\n"
                "        with op.batch_alter_table('fact') as batch_op:\n"
                "            batch_op.set_sqlite_autoincrement(\n"
                "                enabled=False, existing_enabled=True)\n"
                % (rev.revision,)
            )
        command.upgrade(self.cfg, "head")
        with engine.connect() as reader:
            eq_(_next_id(reader, "fact"), 1001)
            eq_(
                compare_metadata(
                    MigrationContext.configure(
                        reader, opts={"render_as_batch": "grouped"}
                    ),
                    target,
                ),
                [],
            )
        command.downgrade(self.cfg, "base")
        with engine.connect() as reader:
            assert "AUTOINCREMENT" not in _create_sql(reader, "fact")
        engine.dispose()


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
