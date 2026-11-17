"""Integration tests for SQLite FTS5 full-text index lifecycle support.

The tests use temporary SQLite files and synthetic article rows; they
assert on search *results* (rowid sets) rather than only on object
existence, and every search expectation is stated before the check.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import Column
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import Text
from sqlalchemy import text

from alembic import command
from alembic.autogenerate import api
from alembic.autogenerate import compare_metadata
from alembic.autogenerate.api import render_python_code
from alembic.migration import MigrationContext
from alembic.migration import RevisionStep
from alembic.operations import Operations
from alembic.operations.fts import FTSIndexSpec
from alembic.operations.fts import METADATA_INFO_KEY
from alembic.operations.fts import REGISTRY_TABLE
from alembic.operations.fts_ops import CreateFTSIndexOp
from alembic.operations.fts_ops import DropFTSIndexOp
from alembic.script import ScriptDirectory
from alembic.script.revision import RevisionMap
from alembic.testing import assert_raises_message
from alembic.testing import eq_
from alembic.testing import TestBase
from alembic.testing.env import clear_staging_env
from alembic.testing.env import staging_env
from alembic.testing.env import _sqlite_testing_config
from alembic.util import CommandError

from .fts_helpers import all_managed_names
from .fts_helpers import fts_decl
from .fts_helpers import fts_hits
from .fts_helpers import master_names
from .fts_helpers import q


# ----------------------------------------------------------------------
# fixtures / model helpers
# ----------------------------------------------------------------------


def articles_model(metadata=None, *, pk="id"):
    metadata = metadata or MetaData()
    Table(
        "articles",
        metadata,
        Column(pk, Integer, primary_key=True, nullable=False),
        Column("title", Text),
        Column("body", Text),
        Column("note", String(40)),
    )
    return metadata


def fts_model(declarations, metadata=None):
    metadata = articles_model(metadata)
    if declarations:
        metadata.info[METADATA_INFO_KEY] = list(declarations)
    return metadata


@contextmanager
def _engine(tmp_path, name="fts.db", fk_on=False):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    if fk_on:

        @event.listens_for(engine, "connect")
        def _fk(dbapi, rec):
            cur = dbapi.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    try:
        yield engine
    finally:
        engine.dispose()


def _seed_articles(engine):
    articles_model().create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO articles(id, title, body, note) VALUES "
                "(1, 'hello world', 'foo bar', 'n1'),"
                "(2, NULL, 'baz qux', 'n2'),"
                "(3, 'running dogs', 'the running fox', 'n3')"
            )
        )


def _ops(engine, **opts):
    final = {"transaction_per_migration": True}
    final.update(opts)
    conn = engine.connect()
    context = MigrationContext.configure(conn, opts=final)
    return Operations(context), conn


class _ManualScript:
    revision = "rev1"
    doc = None
    _normalized_down_revisions = ()
    _versioned_down_revisions = ()

    def __init__(self, upgrade, downgrade=None):
        self.module = SimpleNamespace(
            upgrade=upgrade,
            downgrade=downgrade or (lambda **kw: None),
        )


def _run(engine, fn):
    with engine.connect() as conn:
        context = MigrationContext.configure(
            conn,
            opts={
                "transaction_per_migration": True,
                "fn": lambda heads, ctx: [
                    RevisionStep(RevisionMap(()), _ManualScript(fn), True)
                ],
            },
        )
        context.run_migrations()


# ----------------------------------------------------------------------
# declaration validation
# ----------------------------------------------------------------------


class FTSDeclarationTest(TestBase):
    def _expect(self, declarations, fragment, model=None):
        metadata = model or articles_model()
        metadata.info[METADATA_INFO_KEY] = declarations
        assert_raises_message(
            CommandError,
            fragment,
            compare_metadata,
            MigrationContext.configure(create_engine("sqlite://").connect()),
            metadata,
        )

    def test_missing_key_treated_as_empty(self):
        metadata = articles_model()
        with _engine_pytest_tmp() as engine:
            articles_model().create_all(engine)
            with engine.connect() as conn:
                eq_(
                    compare_metadata(
                        MigrationContext.configure(conn), metadata
                    ),
                    [],
                )

    def test_shape_errors(self):
        base = fts_decl("a", "articles", ["title"])
        self._expect(["nope"], "must be a dictionary")
        self._expect([{**base, "bogus": 1}], "unsupported key")
        self._expect(
            [{"name": "a", "table": "articles"}],
            "missing required",
        )
        self._expect(
            [{"name": "a", "table": "articles", "columns": "title"}],
            "'columns' must be a list",
        )

    def test_name_column_and_tokenizer_errors(self):
        self._expect(
            [{"name": 5, "table": "articles", "columns": ["title"]}],
            "SQL name",
        )
        self._expect(
            [fts_decl("a", "articles", ["title", "title"])],
            "more than once",
        )
        self._expect(
            [fts_decl("a", "articles", ["missing"])],
            "not present",
        )
        self._expect(
            [{**fts_decl("a", "articles", ["title"]), "tokenize": "x"}],
            "Unsupported FTS5 tokenizer",
        )

    def test_duplicate_index_and_table_collision(self):
        self._expect(
            [
                fts_decl("a", "articles", ["title"]),
                fts_decl("a", "articles", ["body"]),
            ],
            "Duplicate FTS index name",
        )
        metadata = articles_model()
        Table("collide", metadata, Column("id", Integer, primary_key=True))
        metadata.info[METADATA_INFO_KEY] = [
            fts_decl("collide", "articles", ["title"])
        ]
        assert_raises_message(
            CommandError,
            "ordinary table",
            compare_metadata,
            MigrationContext.configure(create_engine("sqlite://").connect()),
            metadata,
        )

    def test_content_table_shape(self):
        metadata = MetaData()
        Table(
            "comp",
            metadata,
            Column("a", Integer, primary_key=True),
            Column("b", Integer, primary_key=True),
            Column("t", Text),
        )
        metadata.info[METADATA_INFO_KEY] = [fts_decl("x", "comp", ["t"])]
        assert_raises_message(
            CommandError,
            "exactly one INTEGER PRIMARY KEY",
            compare_metadata,
            MigrationContext.configure(create_engine("sqlite://").connect()),
            metadata,
        )

        metadata2 = articles_model()
        metadata2.info[METADATA_INFO_KEY] = [fts_decl("y", "articles", ["id"])]
        assert_raises_message(
            CommandError,
            "TEXT columns",
            compare_metadata,
            MigrationContext.configure(create_engine("sqlite://").connect()),
            metadata2,
        )


@contextmanager
def _engine_pytest_tmp():
    # tiny helper used by the MetaData-only tests above
    engine = create_engine("sqlite:///:memory:")
    try:
        yield engine
    finally:
        engine.dispose()


# ----------------------------------------------------------------------
# runtime lifecycle and search behavior
# ----------------------------------------------------------------------


class FTSLifecycleTest(TestBase):
    def test_create_backfill_and_search(self, tmp_path):
        with _engine(tmp_path) as engine:
            _seed_articles(engine)
            with _engine_ctx(engine) as op:
                op.create_fts_index(
                    "article_search", "articles", ["title", "body"]
                )

            # expected rowid sets declared before checking
            expected = {
                "hello": [1],
                "baz": [2],
                '"hello world"': [1],
                "title:hello": [1],
                "body:hello": [],
                "dogs": [3],
            }
            with engine.connect() as conn:
                for query, want in expected.items():
                    eq_(fts_hits(conn, "article_search", query), want)
                # selected text columns are readable
                eq_(
                    conn.execute(
                        text(
                            'SELECT rowid, title, body FROM "article_search" '
                            "ORDER BY rowid"
                        )
                    ).fetchall(),
                    [
                        (1, "hello world", "foo bar"),
                        (2, None, "baz qux"),
                        (3, "running dogs", "the running fox"),
                    ],
                )
                # managed objects exist and the registry is populated
                names = set(master_names(conn))
                for managed in all_managed_names("article_search"):
                    assert managed in names, managed
                eq_(
                    conn.execute(
                        text(
                            "SELECT table_name, tokenize FROM %s"
                            % q(REGISTRY_TABLE)
                        )
                    ).fetchall(),
                    [("articles", "unicode61")],
                )

    def test_two_indexes_share_content_table(self, tmp_path):
        with _engine(tmp_path) as engine:
            _seed_articles(engine)
            with _engine_ctx(engine) as op:
                op.create_fts_index(
                    "article_search", "articles", ["title", "body"]
                )
                op.create_fts_index(
                    "article_porter",
                    "articles",
                    ["body"],
                    tokenize="porter unicode61",
                )
            with engine.connect() as conn:
                # porter stems "running" to "run"
                eq_(fts_hits(conn, "article_porter", "run"), [3])
                eq_(
                    fts_hits(conn, "article_search", "running"),
                    [3],
                )
            # dropping one index leaves the other fully functional
            with _engine_ctx(engine) as op:
                op.drop_fts_index("article_porter")
            with engine.connect() as conn:
                names = set(master_names(conn))
                assert "article_porter" not in names
                assert "article_search" in names
                eq_(
                    fts_hits(conn, "article_search", "hello"),
                    [1],
                )

    def test_direct_sql_sync_including_pk_change(self, tmp_path):
        with _engine(tmp_path) as engine:
            _seed_articles(engine)
            with _engine_ctx(engine) as op:
                op.create_fts_index(
                    "article_search", "articles", ["title", "body"]
                )

            # writes issued through a completely independent connection
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO articles(id, title, body) "
                        "VALUES (9, 'fresh news', 'lorem ipsum')"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE articles SET id=100, title='goodbye' "
                        "WHERE id=1"
                    )
                )
                conn.execute(text("UPDATE articles SET title=NULL WHERE id=2"))
                conn.execute(text("DELETE FROM articles WHERE id=3"))

            with engine.connect() as conn:
                eq_(
                    fts_hits(conn, "article_search", "fresh"),
                    [9],
                )
                eq_(
                    fts_hits(conn, "article_search", "goodbye"),
                    [100],
                )
                eq_(
                    fts_hits(conn, "article_search", "hello"),
                    [],
                )
                eq_(
                    fts_hits(conn, "article_search", "running"),
                    [],
                )
                eq_(
                    conn.execute(
                        text(
                            'SELECT rowid FROM "article_search" ORDER BY rowid'
                        )
                    ).fetchall(),
                    [(2,), (9,), (100,)],
                )

    def test_null_flips(self, tmp_path):
        with _engine(tmp_path) as engine:
            _seed_articles(engine)
            with _engine_ctx(engine) as op:
                op.create_fts_index(
                    "article_search", "articles", ["title", "body"]
                )
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE articles SET title='filled title' WHERE id=2")
                )
            with engine.connect() as conn:
                eq_(
                    fts_hits(conn, "article_search", "filled"),
                    [2],
                )
            with engine.begin() as conn:
                conn.execute(text("UPDATE articles SET title=NULL WHERE id=2"))
            with engine.connect() as conn:
                eq_(
                    fts_hits(conn, "article_search", "filled"),
                    [],
                )
                eq_(
                    conn.execute(
                        text(
                            'SELECT title FROM "article_search" WHERE rowid=2'
                        )
                    ).scalar(),
                    None,
                )

    def test_transaction_rollback_keeps_index_consistent(self, tmp_path):
        with _engine(tmp_path) as engine:
            _seed_articles(engine)
            with _engine_ctx(engine) as op:
                op.create_fts_index(
                    "article_search", "articles", ["title", "body"]
                )
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "INSERT INTO articles(id, title, body) "
                            "VALUES (7, 'temp', 'x')"
                        )
                    )
                    conn.execute(
                        text("UPDATE articles SET title='changed' WHERE id=1")
                    )
                    conn.execute(text("DELETE FROM articles WHERE id=2"))
                    raise RuntimeError("rollback please")
            except RuntimeError:
                pass
            with engine.connect() as conn:
                eq_(fts_hits(conn, "article_search", "temp"), [])
                eq_(fts_hits(conn, "article_search", "changed"), [])
                eq_(fts_hits(conn, "article_search", "hello"), [1])
                eq_(fts_hits(conn, "article_search", "baz"), [2])

    def test_drop_removes_everything(self, tmp_path):
        with _engine(tmp_path) as engine:
            _seed_articles(engine)
            with _engine_ctx(engine) as op:
                op.create_fts_index(
                    "article_search", "articles", ["title", "body"]
                )
                op.drop_fts_index("article_search")
            with engine.connect() as conn:
                names = set(master_names(conn))
                for managed in all_managed_names("article_search"):
                    assert managed not in names, managed
                assert REGISTRY_TABLE not in names

    def test_quoted_identifiers_with_spaces_quotes_unicode(self, tmp_path):
        with _engine(tmp_path, name="quoted.db") as engine:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        'CREATE TABLE "my articles" ('
                        '"art id" INTEGER PRIMARY KEY, '
                        '"ti tle" TEXT, "bo""dy" TEXT)'
                    )
                )
                conn.execute(
                    text(
                        'INSERT INTO "my articles" VALUES '
                        "(1, 'Hello world', 'foo bar'), (2, NULL, 'baz')"
                    )
                )
            index_name = 'art "search" 検索'
            with _engine_ctx(engine) as op:
                op.create_fts_index(
                    index_name,
                    "my articles",
                    ["ti tle", 'bo"dy'],
                )
            with engine.connect() as conn:
                eq_(fts_hits(conn, index_name, "hello"), [1])
                eq_(
                    fts_hits(conn, index_name, '"hello world"'),
                    [1],
                )
                eq_(
                    fts_hits(conn, index_name, '"ti tle":hello'),
                    [1],
                )
            with _engine_ctx(engine) as op:
                op.drop_fts_index(index_name)
            with engine.connect() as conn:
                assert index_name not in master_names(conn)


@contextmanager
def _engine_ctx(engine, **opts):
    operations, conn = _ops(engine, **opts)
    try:
        yield operations
    finally:
        conn.close()


# ----------------------------------------------------------------------
# autogenerate comparison and script rendering
# ----------------------------------------------------------------------


class FTSAutogenerateTest(TestBase):
    def test_diffs_for_add_change_remove(self, tmp_path):
        with _engine(tmp_path) as engine:
            _seed_articles(engine)
            target = fts_model(
                [fts_decl("article_search", "articles", ["title", "body"])]
            )
            with engine.connect() as conn:
                diffs = _flatten(
                    compare_metadata(MigrationContext.configure(conn), target)
                )
            eq_(
                [d[0] for d in diffs],
                ["add_fts_index"],
            )

            # apply
            script = api.produce_migrations(
                MigrationContext.configure(engine.connect()), target
            )
            with _engine_ctx(engine) as op:
                for operation in script.upgrade_ops.ops:
                    op.invoke(operation)

            # no diffs afterwards
            with engine.connect() as conn:
                eq_(
                    compare_metadata(MigrationContext.configure(conn), target),
                    [],
                )

            # change column order + tokenizer -> drop+create pair
            changed = fts_model(
                [
                    fts_decl(
                        "article_search",
                        "articles",
                        ["body", "title"],
                        tokenize="porter unicode61",
                    )
                ]
            )
            with engine.connect() as conn:
                diffs = _flatten(
                    compare_metadata(MigrationContext.configure(conn), changed)
                )
            eq_(
                [d[0] for d in diffs],
                ["remove_fts_index", "add_fts_index"],
            )

            script2 = api.produce_migrations(
                MigrationContext.configure(engine.connect()), changed
            )
            rendered = render_python_code(script2.upgrade_ops)
            assert "drop_fts_index('article_search')" in rendered
            assert "tokenize='porter unicode61'" in rendered
            assert "['body', 'title']" in rendered

            with _engine_ctx(engine) as op:
                for operation in script2.upgrade_ops.ops:
                    op.invoke(operation)
            with engine.connect() as conn:
                eq_(
                    compare_metadata(
                        MigrationContext.configure(conn), changed
                    ),
                    [],
                )
                eq_(
                    fts_hits(conn, "article_search", "hello"),
                    [1],
                )

            # downgrade restores the old definition from content rows
            with _engine_ctx(engine) as op:
                for operation in script2.downgrade_ops.ops:
                    op.invoke(operation)
            with engine.connect() as conn:
                eq_(
                    compare_metadata(MigrationContext.configure(conn), target),
                    [],
                )
                eq_(
                    fts_hits(conn, "article_search", "hello"),
                    [1],
                )

    def test_managed_objects_never_diff_as_business_tables(self, tmp_path):
        with _engine(tmp_path) as engine:
            _seed_articles(engine)
            target = fts_model(
                [fts_decl("article_search", "articles", ["title"])]
            )
            script = api.produce_migrations(
                MigrationContext.configure(engine.connect()), target
            )
            with _engine_ctx(engine) as op:
                for operation in script.upgrade_ops.ops:
                    op.invoke(operation)
            with engine.connect() as conn:
                diffs = _flatten(
                    compare_metadata(MigrationContext.configure(conn), target)
                )
                eq_(diffs, [])

    def test_user_fts_table_and_trigger_are_not_adopted(self, tmp_path):
        with _engine(tmp_path) as engine:
            _seed_articles(engine)
            with engine.begin() as conn:
                conn.execute(
                    text("CREATE VIRTUAL TABLE user_own USING fts5(x)")
                )
                conn.execute(
                    text(
                        "CREATE TRIGGER user_tr AFTER INSERT ON articles "
                        "BEGIN SELECT 1; END"
                    )
                )
            target = fts_model(
                [fts_decl("article_search", "articles", ["title"])]
            )
            with engine.connect() as conn:
                diffs = _flatten(
                    compare_metadata(MigrationContext.configure(conn), target)
                )
            fts_diffs = [
                d
                for d in diffs
                if d[0] in ("add_fts_index", "remove_fts_index")
            ]
            eq_([d[1]["name"] for d in fts_diffs], ["article_search"])
            # no FTS lifecycle op references the user's objects
            assert all(
                d[1].get("name") not in ("user_own", "user_tr")
                for d in fts_diffs
            )

    def test_dropping_referenced_column_or_table_rejected(self, tmp_path):
        with _engine(tmp_path) as engine:
            _seed_articles(engine)
            target = fts_model(
                [
                    fts_decl("article_search", "articles", ["title"]),
                    fts_decl("body_search", "articles", ["body"]),
                ]
            )
            script = api.produce_migrations(
                MigrationContext.configure(engine.connect()), target
            )
            with _engine_ctx(engine) as op:
                for operation in script.upgrade_ops.ops:
                    op.invoke(operation)

            # metadata drops a column ('note') that a surviving FTS
            # declaration references.  The declaration still names the
            # column against the live database (the column exists there),
            # so validation reaches the comparator's drop guard rather
            # than failing at declaration shape.
            broken = MetaData()
            Table(
                "articles",
                broken,
                Column("id", Integer, primary_key=True, nullable=False),
                Column("title", Text),
                Column("body", Text),
            )
            broken.info[METADATA_INFO_KEY] = [
                # note is being dropped from the table but the surviving
                # index still declares it
                fts_decl(
                    "note_search",
                    "articles",
                    ["note", "title"],
                ),
            ]
            with engine.connect() as conn:
                assert_raises_message(
                    CommandError,
                    "Cannot drop indexed column",
                    compare_metadata,
                    MigrationContext.configure(conn),
                    broken,
                )

            # when the declaration referencing the dropped column is also
            # removed, generation succeeds and the FTS drop is emitted
            # before the ordinary column drop.
            consistent = MetaData()
            Table(
                "articles",
                consistent,
                Column("id", Integer, primary_key=True, nullable=False),
                Column("body", Text),
                Column("note", String(40)),
            )
            consistent.info[METADATA_INFO_KEY] = [
                fts_decl("body_search", "articles", ["body"]),
            ]
            script = api.produce_migrations(
                MigrationContext.configure(engine.connect()), consistent
            )
            rendered = render_python_code(script.upgrade_ops)
            assert "drop_fts_index('article_search')" in rendered
            assert "drop_column" in rendered

    def test_table_and_index_can_be_removed_together(self, tmp_path):
        with _engine(tmp_path) as engine:
            target = fts_model(
                [fts_decl("article_search", "articles", ["title"])]
            )
            target.create_all(engine)
            script = api.produce_migrations(
                MigrationContext.configure(engine.connect()), target
            )
            with _engine_ctx(engine) as op:
                for operation in script.upgrade_ops.ops:
                    op.invoke(operation)

            # removing both the table and its declaration orders the
            # FTS drop ahead of the table drop and succeeds
            empty = MetaData()
            removal = api.produce_migrations(
                MigrationContext.configure(engine.connect()), empty
            )
            kinds = [
                type(operation).__name__
                for operation in removal.upgrade_ops.ops
            ]
            assert kinds.index("DropFTSIndexOp") < kinds.index("DropTableOp")
            with _engine_ctx(engine) as op:
                for operation in removal.upgrade_ops.ops:
                    op.invoke(operation)
            with engine.connect() as conn:
                eq_(
                    conn.execute(
                        text(
                            "SELECT COUNT(*) FROM sqlite_master "
                            "WHERE name NOT LIKE 'sqlite_%'"
                        )
                    ).scalar(),
                    0,
                )

            # keeping the declaration while dropping the table is still
            # rejected.  Rebuild the scenario first.
            target2 = fts_model(
                [fts_decl("article_search", "articles", ["title"])]
            )
            target2.create_all(engine)
            script2 = api.produce_migrations(
                MigrationContext.configure(engine.connect()), target2
            )
            with _engine_ctx(engine) as op:
                for operation in script2.upgrade_ops.ops:
                    op.invoke(operation)
            dangling = MetaData()
            dangling.info[METADATA_INFO_KEY] = [
                fts_decl("article_search", "articles", ["title"])
            ]
            with engine.connect() as conn:
                assert_raises_message(
                    CommandError,
                    "content table",
                    api.produce_migrations,
                    MigrationContext.configure(conn),
                    dangling,
                )

    def test_same_revision_table_and_fts_creation_order(self, tmp_path):
        from alembic.autogenerate import api as _api

        with _engine(tmp_path) as engine:
            target = fts_model(
                [fts_decl("article_search", "articles", ["title"])]
            )
            with engine.connect() as conn:
                diffs = _flatten(
                    compare_metadata(MigrationContext.configure(conn), target)
                )
            eq_(
                [d[0] for d in diffs],
                ["add_table", "add_fts_index"],
            )

            # executing the generated stream works without the table
            # pre-existing
            script = _api.produce_migrations(
                MigrationContext.configure(engine.connect()), target
            )
            with _engine_ctx(engine) as op:
                for operation in script.upgrade_ops.ops:
                    op.invoke(operation)
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO articles(id, title, body, note) "
                        "VALUES (1, 'hello', 'b', 'n')"
                    )
                )
            with engine.connect() as conn:
                eq_(
                    fts_hits(conn, "article_search", "hello"),
                    [1],
                )

    def test_include_object_filter_applies_to_fts_indexes(self, tmp_path):
        with _engine(tmp_path) as engine:
            _seed_articles(engine)
            target = fts_model(
                [
                    fts_decl("article_search", "articles", ["title"]),
                    fts_decl("other_search", "articles", ["body"]),
                ]
            )
            with engine.connect() as conn:
                context = MigrationContext.configure(
                    conn,
                    opts={
                        "include_object": (
                            lambda obj, name, type_, reflected, cmp: (
                                name != "other_search"
                                if type_ == "fts_index"
                                else True
                            )
                        )
                    },
                )
                diffs = _flatten(compare_metadata(context, target))
            fts_diffs = [
                d
                for d in diffs
                if d[0] in ("add_fts_index", "remove_fts_index")
            ]
            eq_([d[1]["name"] for d in fts_diffs], ["article_search"])

    def test_render_modes(self, tmp_path):
        from alembic.operations.ops import AlterColumnOp
        from alembic.operations.ops import ModifyTableOps
        from alembic.operations.ops import UpgradeOps

        container = UpgradeOps(
            [
                CreateFTSIndexOp(
                    "article_search", "articles", ["title", "body"]
                ),
                ModifyTableOps(
                    "articles",
                    [
                        AlterColumnOp(
                            "articles",
                            "title",
                            existing_type=Text(),
                            modify_nullable=False,
                        )
                    ],
                ),
            ]
        )
        plain = render_python_code(container, render_as_batch=False)
        batched = render_python_code(container, render_as_batch=True)
        grouped = render_python_code(container, render_as_batch="grouped")
        assert "create_fts_index(" in plain
        assert "sqlite_batch_group" not in plain
        assert "batch_alter_table" in batched
        assert "sqlite_batch_group" in grouped
        # FTS op is rendered inside the group block
        group_lines = [
            line for line in grouped.splitlines() if "create_fts_index" in line
        ]
        assert group_lines and group_lines[0].startswith("    ")

    def test_grouped_mode_still_rejects_whole_table_drops(self, tmp_path):
        from alembic.operations.ops import DropTableOp
        from alembic.operations.ops import UpgradeOps

        container = UpgradeOps(
            [
                CreateFTSIndexOp("article_search", "articles", ["title"]),
                DropTableOp("articles"),
            ]
        )
        assert_raises_message(
            CommandError,
            "unsupported operations",
            render_python_code,
            container,
            render_as_batch="grouped",
        )


def _flatten(diffs):
    out = []
    for item in diffs:
        if isinstance(item, list):
            out.extend(item)
        else:
            out.append(item)
    return out


# ----------------------------------------------------------------------
# batch / grouped rebuild hand-off
# ----------------------------------------------------------------------


class FTSBatchRebuildTest(TestBase):
    def test_standalone_batch_rebuild_keeps_indexes(self, tmp_path):
        from sqlalchemy import Column as _Column

        with _engine(tmp_path) as engine:
            articles_model().create_all(engine)
            with engine.begin() as conn:
                # no NULL titles: this revision tightens title NOT NULL
                conn.execute(
                    text(
                        "INSERT INTO articles(id, title, body, note) VALUES "
                        "(1, 'hello world', 'foo bar', 'n1'),"
                        "(2, 'second', 'baz qux', 'n2'),"
                        "(3, 'running dogs', 'the running fox', 'n3')"
                    )
                )
            with _engine_ctx(engine) as op:
                op.create_fts_index(
                    "article_search", "articles", ["title", "body"]
                )
                op.create_fts_index(
                    "body_only",
                    "articles",
                    ["body"],
                    tokenize="porter unicode61",
                )
            with _engine_ctx(engine) as op:
                with op.batch_alter_table("articles") as batch_op:
                    batch_op.alter_column(
                        "title",
                        existing_type=Text(),
                        nullable=False,
                    )
                    batch_op.add_column(_Column("added", String(20)))
                    batch_op.drop_column("note")

            with engine.connect() as conn:
                table_cols = [
                    r[1]
                    for r in conn.execute(text("PRAGMA table_info(articles)"))
                ]
                eq_(
                    sorted(table_cols),
                    sorted(["id", "title", "body", "added"]),
                )
                eq_(
                    fts_hits(conn, "article_search", "hello"),
                    [1],
                )
                eq_(fts_hits(conn, "body_only", "baz"), [2])
                triggers = {
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='trigger'"
                        )
                    )
                }
                for index_name in ("article_search", "body_only"):
                    for suffix in ("_ai", "_au", "_ad"):
                        assert index_name + suffix in triggers
                eq_(
                    conn.execute(
                        text(
                            "SELECT COUNT(*) FROM sqlite_master "
                            "WHERE name LIKE '%_alembic_tmp%'"
                        )
                    ).scalar(),
                    0,
                )
            # sync still works through plain SQL afterwards
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO articles(id, title, body) "
                        "VALUES (9, 'new post', 'content')"
                    )
                )
            with engine.connect() as conn:
                eq_(
                    fts_hits(conn, "article_search", "new"),
                    [9],
                )
                eq_(fts_hits(conn, "body_only", "content"), [9])

    def test_grouped_rebuild_with_foreign_keys(self, tmp_path):
        with _engine(tmp_path, fk_on=True) as engine:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE TABLE authors(id INTEGER PRIMARY KEY, "
                        "name TEXT)"
                    )
                )
                conn.execute(
                    text(
                        "CREATE TABLE articles(id INTEGER PRIMARY KEY, "
                        "author_id INTEGER, title TEXT, body TEXT, "
                        "note TEXT, CONSTRAINT fk_a FOREIGN KEY(author_id) "
                        "REFERENCES authors(id))"
                    )
                )
                conn.execute(text("INSERT INTO authors VALUES (1, 'ada')"))
                conn.execute(
                    text(
                        "INSERT INTO articles VALUES "
                        "(1, 1, 'hello world', 'foo', 'n1'),"
                        "(2, 1, 'second', 'baz qux', NULL)"
                    )
                )
            with _engine_ctx(engine) as op:
                op.create_fts_index(
                    "article_search", "articles", ["title", "body"]
                )

            def revision(operations):
                def upgrade(**kw):
                    with operations.sqlite_batch_group():
                        with operations.batch_alter_table(
                            "articles"
                        ) as batch_op:
                            batch_op.alter_column(
                                "title",
                                existing_type=Text(),
                                nullable=False,
                            )
                            batch_op.add_column(Column("added", String(20)))
                            batch_op.drop_column("note")
                        with operations.batch_alter_table(
                            "authors"
                        ) as batch_op:
                            batch_op.alter_column(
                                "name",
                                existing_type=Text(),
                                nullable=False,
                            )

                return upgrade

            with engine.connect() as conn:
                context = MigrationContext.configure(
                    conn,
                    opts={
                        "transaction_per_migration": True,
                        "fn": lambda heads, ctx: [
                            RevisionStep(
                                RevisionMap(()),
                                _ManualScript(revision(Operations(ctx))),
                                True,
                            )
                        ],
                    },
                )
                context.run_migrations()

            with engine.connect() as conn:
                eq_(
                    conn.exec_driver_sql("PRAGMA foreign_keys").scalar(),
                    1,
                )
                eq_(
                    conn.exec_driver_sql(
                        "PRAGMA foreign_key_check"
                    ).fetchall(),
                    [],
                )
                eq_(
                    fts_hits(conn, "article_search", "hello"),
                    [1],
                )
                eq_(
                    fts_hits(conn, "article_search", "baz"),
                    [2],
                )
                eq_(
                    conn.execute(
                        text(
                            "SELECT COUNT(*) FROM sqlite_master "
                            "WHERE name LIKE '%_alembic_group_tmp%'"
                        )
                    ).scalar(),
                    0,
                )

            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO articles(id, author_id, title, body) "
                        "VALUES (9, 1, 'fresh', 'lorem')"
                    )
                )
            with engine.connect() as conn:
                eq_(
                    fts_hits(conn, "article_search", "fresh"),
                    [9],
                )

    def test_unrelated_group_table_is_not_rebuilt(self, tmp_path):
        # a group touching only table B must leave A's FTS triggers alone
        with _engine(tmp_path, fk_on=True) as engine:
            with engine.begin() as conn:
                conn.execute(
                    text("CREATE TABLE a(id INTEGER PRIMARY KEY, t TEXT)")
                )
                conn.execute(
                    text(
                        "CREATE TABLE b(id INTEGER PRIMARY KEY, label "
                        "VARCHAR(20))"
                    )
                )
                conn.execute(text("INSERT INTO a VALUES (1, 'hello')"))
            with _engine_ctx(engine) as op:
                op.create_fts_index("a_search", "a", ["t"])
            triggers_before = None
            with engine.connect() as conn:
                triggers_before = sorted(
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='trigger' ORDER BY name"
                        )
                    )
                )

            def revision(operations):
                def upgrade(**kw):
                    with operations.sqlite_batch_group():
                        with operations.batch_alter_table("b") as bo:
                            bo.alter_column(
                                "label",
                                existing_type=String(20),
                                nullable=False,
                            )

                return upgrade

            with engine.connect() as conn:
                context = MigrationContext.configure(
                    conn,
                    opts={
                        "transaction_per_migration": True,
                        "fn": lambda heads, ctx: [
                            RevisionStep(
                                RevisionMap(()),
                                _ManualScript(revision(Operations(ctx))),
                                True,
                            )
                        ],
                    },
                )
                context.run_migrations()
            with engine.connect() as conn:
                triggers_after = sorted(
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='trigger' ORDER BY name"
                        )
                    )
                )
                eq_(triggers_after, triggers_before)
                eq_(fts_hits(conn, "a_search", "hello"), [1])


# ----------------------------------------------------------------------
# failure injection and tamper detection
# ----------------------------------------------------------------------


class FTSFailureTest(TestBase):
    def test_registry_name_collision_is_rejected(self, tmp_path):
        with _engine(tmp_path) as engine:
            _seed_articles(engine)
            with engine.begin() as conn:
                conn.execute(
                    text("CREATE TABLE %s (x TEXT)" % q(REGISTRY_TABLE))
                )
            with _engine_ctx(engine) as op:
                assert_raises_message(
                    CommandError,
                    "does not have the shape of the Alembic FTS " "registry",
                    op.create_fts_index,
                    "article_search",
                    "articles",
                    ["title"],
                )
            with engine.connect() as conn:
                # the user's table is untouched and no FTS objects exist
                eq_(
                    [
                        row[1]
                        for row in conn.execute(
                            text("PRAGMA table_info(%s)" % q(REGISTRY_TABLE))
                        )
                    ],
                    ["x"],
                )
                eq_(
                    conn.execute(
                        text(
                            "SELECT COUNT(*) FROM sqlite_master "
                            "WHERE name LIKE 'article_search%'"
                        )
                    ).scalar(),
                    0,
                )

    def test_backfill_failure_rolls_back_everything(self, tmp_path):
        with _engine(tmp_path) as engine:
            _seed_articles(engine)

            @event.listens_for(engine, "before_cursor_execute")
            def boom(conn, cursor, statement, params, ctx, many):
                if statement.upper().startswith("INSERT INTO") and (
                    "article_search" in statement
                ):
                    raise RuntimeError("injected backfill failure")

            try:
                with _engine_ctx(engine) as op:
                    op.create_fts_index(
                        "article_search",
                        "articles",
                        ["title", "body"],
                    )
            except RuntimeError as e:
                assert "injected" in str(e)
            finally:
                event.remove(engine, "before_cursor_execute", boom)

            with engine.connect() as conn:
                names = set(master_names(conn))
                for managed in all_managed_names("article_search"):
                    assert managed not in names, managed
                assert REGISTRY_TABLE not in names
                eq_(
                    conn.execute(
                        text("SELECT COUNT(*) FROM articles")
                    ).scalar(),
                    3,
                )

            # the feature remains usable after the failed attempt
            with _engine_ctx(engine) as op:
                op.create_fts_index(
                    "article_search", "articles", ["title", "body"]
                )
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO articles(id, title, body) "
                        "VALUES (8, 'later', 'x')"
                    )
                )
            with engine.connect() as conn:
                eq_(fts_hits(conn, "article_search", "later"), [8])

    def test_tampered_objects_are_named_and_refused(self, tmp_path):
        with _engine(tmp_path) as engine:
            _seed_articles(engine)
            with _engine_ctx(engine) as op:
                op.create_fts_index(
                    "article_search", "articles", ["title", "body"]
                )

            # missing trigger
            with engine.begin() as conn:
                conn.execute(text("DROP TRIGGER article_search_ai"))
            with engine.connect() as conn:
                assert_raises_message(
                    CommandError,
                    "article_search",
                    compare_metadata,
                    MigrationContext.configure(conn),
                    fts_model([]),
                )
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE TRIGGER article_search_ai AFTER INSERT "
                        "ON articles BEGIN INSERT INTO article_search"
                        "(rowid, title, body) VALUES (new.id, new.title, "
                        "new.body); END"
                    )
                )

            # drop unknown index is refused without touching anything
            with _engine_ctx(engine) as op:
                assert_raises_message(
                    CommandError,
                    "not registered",
                    op.drop_fts_index,
                    "mystery",
                )

            # corrupted signature
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE %s SET signature='bad' WHERE "
                        "name='article_search'" % q(REGISTRY_TABLE)
                    )
                )
            with engine.connect() as conn:
                assert_raises_message(
                    CommandError,
                    "corrupted registry entry",
                    compare_metadata,
                    MigrationContext.configure(conn),
                    fts_model([]),
                )

    def test_foreign_keys_pragma_is_restored(self, tmp_path):
        for entering in (0, 1):
            with _engine(
                tmp_path, name=f"fk{entering}.db", fk_on=bool(entering)
            ) as engine:
                _seed_articles(engine)
                with engine.connect() as conn:
                    eq_(
                        conn.exec_driver_sql("PRAGMA foreign_keys").scalar(),
                        entering,
                    )
                with _engine_ctx(engine) as op:
                    op.create_fts_index(
                        "article_search",
                        "articles",
                        ["title"],
                    )
                    op.drop_fts_index("article_search")
                with engine.connect() as conn:
                    eq_(
                        conn.exec_driver_sql("PRAGMA foreign_keys").scalar(),
                        entering,
                    )

    def test_batch_copy_failure_rearms_triggers(self, tmp_path):
        from sqlalchemy import Text as _Text

        with _engine(tmp_path) as engine:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE TABLE articles(id INTEGER PRIMARY KEY, "
                        "title TEXT, body TEXT)"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO articles VALUES "
                        "(1, 'hello', 'foo'), (2, NULL, 'baz')"
                    )
                )
            with _engine_ctx(engine) as op:
                op.create_fts_index(
                    "article_search", "articles", ["title", "body"]
                )

            armed = {"on": True}

            @event.listens_for(engine, "before_cursor_execute")
            def fail_copy(conn, cursor, statement, params, ctx, many):
                if (
                    armed["on"]
                    and statement.upper().startswith("INSERT INTO")
                    and "_alembic_tmp_articles" in statement
                ):
                    raise RuntimeError("injected copy failure")

            with _engine_ctx(engine) as op:
                try:
                    with op.batch_alter_table("articles") as batch_op:
                        batch_op.alter_column(
                            "title",
                            existing_type=_Text(),
                            nullable=False,
                        )
                except RuntimeError as e:
                    assert "injected copy failure" in str(e)
            event.remove(engine, "before_cursor_execute", fail_copy)

            with engine.connect() as conn:
                triggers = sorted(
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='trigger'"
                        )
                    )
                )
                eq_(
                    triggers,
                    [
                        "article_search_ad",
                        "article_search_ai",
                        "article_search_au",
                    ],
                )
                eq_(
                    conn.execute(
                        text("SELECT COUNT(*) FROM articles")
                    ).scalar(),
                    2,
                )
            # synchronization continues to work via plain SQL
            with engine.begin() as conn:
                conn.execute(
                    text("INSERT INTO articles VALUES (9, 'later', 'x')")
                )
            with engine.connect() as conn:
                eq_(
                    fts_hits(conn, "article_search", "later"),
                    [9],
                )

    def test_grouped_rebuild_failure_keeps_full_old_state(self, tmp_path):
        from sqlalchemy import Text as _Text

        with _engine(tmp_path, fk_on=True) as engine:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE TABLE articles(id INTEGER PRIMARY KEY, "
                        "title TEXT, body TEXT)"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO articles VALUES "
                        "(1, 'hello', 'foo'), (2, NULL, 'baz')"
                    )
                )
            with _engine_ctx(engine) as op:
                op.create_fts_index(
                    "article_search", "articles", ["title", "body"]
                )

            def revision(operations):
                def upgrade(**kw):
                    with operations.sqlite_batch_group():
                        with operations.batch_alter_table(
                            "articles"
                        ) as batch_op:
                            # existing NULL title violates the new
                            # constraint, so the copy fails
                            batch_op.alter_column(
                                "title",
                                existing_type=_Text(),
                                nullable=False,
                            )

                return upgrade

            with engine.connect() as conn:
                context = MigrationContext.configure(
                    conn,
                    opts={
                        "transaction_per_migration": True,
                        "fn": lambda heads, ctx: [
                            RevisionStep(
                                RevisionMap(()),
                                _ManualScript(revision(Operations(ctx))),
                                True,
                            )
                        ],
                    },
                )
                import sqlalchemy.exc

                with pytest.raises(sqlalchemy.exc.IntegrityError):
                    context.run_migrations()

            with engine.connect() as conn:
                eq_(
                    conn.exec_driver_sql("PRAGMA foreign_keys").scalar(),
                    1,
                )
                eq_(
                    conn.execute(
                        text(
                            "SELECT COUNT(*) FROM sqlite_master "
                            "WHERE name LIKE '%_alembic_group_tmp%'"
                        )
                    ).scalar(),
                    0,
                )
                eq_(
                    sorted(
                        r[0]
                        for r in conn.execute(
                            text(
                                "SELECT name FROM sqlite_master "
                                "WHERE type='trigger'"
                            )
                        )
                    ),
                    [
                        "article_search_ad",
                        "article_search_ai",
                        "article_search_au",
                    ],
                )
                eq_(
                    fts_hits(conn, "article_search", "hello"),
                    [1],
                )
                eq_(
                    conn.execute(
                        text("SELECT COUNT(*) FROM articles")
                    ).scalar(),
                    2,
                )

    def test_replacement_failure_restores_old_definition(self, tmp_path):
        with _engine(tmp_path) as engine:
            _seed_articles(engine)
            with _engine_ctx(engine) as op:
                op.create_fts_index("article_search", "articles", ["title"])

            @event.listens_for(engine, "before_cursor_execute")
            def fail_backfill(conn, cursor, statement, params, ctx, many):
                if (
                    statement.upper().startswith("INSERT INTO")
                    and "article_search" in statement
                ):
                    raise RuntimeError("injected replacement backfill failure")

            old_spec = FTSIndexSpec(
                "article_search",
                "articles",
                ("title",),
                "unicode61",
            )
            with engine.connect() as conn:
                context = MigrationContext.configure(
                    conn,
                    opts={"transaction_per_migration": True},
                )
                operations = Operations(context)
                try:
                    with context.begin_transaction(_per_migration=True):
                        operations.invoke(DropFTSIndexOp.from_spec(old_spec))
                        operations.invoke(
                            CreateFTSIndexOp(
                                "article_search",
                                "articles",
                                ["title", "body"],
                            )
                        )
                except RuntimeError as e:
                    assert "injected" in str(e)
            event.remove(engine, "before_cursor_execute", fail_backfill)

            with engine.connect() as conn:
                eq_(
                    conn.execute(
                        text(
                            "SELECT tokenize FROM %s WHERE name="
                            "'article_search'" % q(REGISTRY_TABLE)
                        )
                    ).scalar(),
                    "unicode61",
                )
                eq_(
                    fts_hits(conn, "article_search", "hello"),
                    [1],
                )
                eq_(
                    sorted(
                        r[0]
                        for r in conn.execute(
                            text(
                                "SELECT name FROM sqlite_master "
                                "WHERE type='trigger'"
                            )
                        )
                    ),
                    [
                        "article_search_ad",
                        "article_search_ai",
                        "article_search_au",
                    ],
                )


# ----------------------------------------------------------------------
# end-to-end command.revision(autogenerate=True) round trip
# ----------------------------------------------------------------------


_ENV_TEMPLATE = """
from sqlalchemy import engine_from_config, pool
from logging.config import fileConfig
from alembic import context
import sys, types
sys.path.insert(0, "%(dir)s")
mod = types.ModuleType("fts_targets")
mod.metadata = TARGET
sys.modules["fts_targets"] = mod
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = mod.metadata

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
            render_as_batch=%(mode)r,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()

run_migrations_online()
"""


class FTSCommandRoundTripTest(TestBase):
    def setUp(self):
        self.cfg = _sqlite_testing_config()
        staging_env()
        self.script = ScriptDirectory.from_config(self.cfg)

    def tearDown(self):
        clear_staging_env()

    def _write_env(self, target, mode):
        env_path = self.script.env_py_location
        import os

        source = _ENV_TEMPLATE % {
            "dir": os.path.dirname(
                self.cfg.get_main_option("sqlalchemy.url").replace(
                    "sqlite:///", ""
                )
            ),
            "mode": mode,
        }
        # embed the metadata construction as a serialized pickle-free
        # literal by building it against real SA classes in env.py text
        decls = target.info.get(METADATA_INFO_KEY, [])
        builder = (
            "from sqlalchemy import MetaData, Table, Column, "
            "Integer, Text, String\n"
            "from alembic.operations.fts import METADATA_INFO_KEY\n"
            "TARGET = MetaData()\n"
            "Table('articles', TARGET, "
            "Column('id', Integer, primary_key=True, nullable=False), "
            "Column('title', Text), Column('body', Text), "
            "Column('note', String(40)))\n"
            "TARGET.info[METADATA_INFO_KEY] = %r\n" % decls
        )
        with open(env_path, "w") as f:
            f.write(builder + source)

    @pytest.mark.parametrize("mode", [False, True, "grouped"])
    def test_revision_upgrade_downgrade_roundtrip(self, tmp_path, mode):
        engine = create_engine(self.cfg.get_main_option("sqlalchemy.url"))
        try:
            _seed_articles(engine)
            target = fts_model(
                [
                    fts_decl(
                        "article_search",
                        "articles",
                        ["title", "body"],
                    ),
                    fts_decl(
                        "article_porter",
                        "articles",
                        ["body"],
                        tokenize="porter unicode61",
                    ),
                ]
            )
            self._write_env(target, mode)
            command.revision(self.cfg, message="add fts", autogenerate=True)
            revisions = list(
                ScriptDirectory.from_config(self.cfg).walk_revisions()
            )
            eq_(len(revisions), 1)
            generated = open(revisions[0].path).read()
            assert "create_fts_index(" in generated
            assert "drop_fts_index(" in generated

            command.upgrade(self.cfg, "head")
            with engine.connect() as conn:
                eq_(
                    fts_hits(conn, "article_search", "hello"),
                    [1],
                )
                eq_(fts_hits(conn, "article_porter", "run"), [3])
                eq_(
                    compare_metadata(MigrationContext.configure(conn), target),
                    [],
                )

            command.downgrade(self.cfg, "base")
            with engine.connect() as conn:
                names = set(master_names(conn))
                assert "article_search" not in names
                assert "article_porter" not in names
                assert REGISTRY_TABLE not in names
                # data untouched
                eq_(
                    conn.execute(
                        text("SELECT COUNT(*) FROM articles")
                    ).scalar(),
                    3,
                )
        finally:
            engine.dispose()
