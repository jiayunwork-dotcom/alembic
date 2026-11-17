.. _sqlite_fts5:

SQLite FTS5 Full-Text Index Lifecycle
=====================================

Alembic can manage SQLite `FTS5 <https://www.sqlite.org/fts5.html>`_
full-text indexes through the same upgrade/downgrade revision workflow as
ordinary tables.  The feature is enabled declaratively: indexes are
described in ``MetaData.info`` and ``revision --autogenerate`` produces
:meth:`.Operations.create_fts_index` /
:meth:`.Operations.drop_fts_index` directives that create, backfill,
synchronize and retire every object the index needs.  Searching is done
directly through SQLite - no separate search service is involved - and
the runtime SQLite library must be compiled with FTS5 enabled (most
distributions are).

Declaring an index
------------------

Declarations live in ``MetaData.info["sqlite_fts_indexes"]`` as a list of
dictionaries::

    from sqlalchemy import MetaData, Table, Column, Integer, Text

    metadata = MetaData()
    Table(
        "articles",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("title", Text),
        Column("body", Text),
    )

    metadata.info["sqlite_fts_indexes"] = [
        {
            "name": "article_search",
            "table": "articles",
            "columns": ["title", "body"],
            "tokenize": "unicode61",
        },
    ]

Each entry contains:

* ``name``: the public FTS5 virtual table name in the main database.
  Index names must be unique across the whole database and must not
  collide with an ordinary table or other object.
* ``table``: an ordinary row table in the same (main) database.  It must
  have exactly one ``INTEGER PRIMARY KEY`` column - its name does not have
  to be ``id`` - which is used as the FTS ``rowid``.
* ``columns``: the TEXT columns to index, in declaration order.  Columns
  may contain NULL.  Duplicate column names are rejected.
* ``tokenize``: optional; defaults to ``"unicode61"`` and additionally
  accepts ``"porter unicode61"``.  Matching, phrase and column-filter
  semantics are exactly those of FTS5 for the chosen tokenizer; no extra
  word splitting (including for Chinese text) is performed.

Names are SQL names rather than :attr:`.Column.key` values and may contain
spaces, double quotes or full Unicode characters; they are always emitted
as quoted identifiers.  Missing, mistyped or wrongly typed keys, duplicate
columns/names, unsupported tokenizers, non-TEXT columns, tables without a
single integer primary key, and collisions with ordinary objects are all
rejected with a specific error before any database write occurs.  When the
``sqlite_fts_indexes`` key is absent it is treated as an empty list and the
feature is fully inactive, leaving all other dialects and existing
migrations unchanged.

Operations
----------

The generated revision directives are also available directly::

    def upgrade():
        op.create_fts_index(
            "article_search",
            "articles",
            ["title", "body"],
            tokenize="unicode61",
        )

    def downgrade():
        op.drop_fts_index("article_search")

``create_fts_index`` creates a standalone FTS5 virtual table under the
public index name, backfills every existing content row with an
``INSERT ... SELECT`` (rows are never loaded into Python), installs three
synchronization triggers and records the definition in the Alembic FTS
registry table.  ``drop_fts_index`` removes the triggers, the virtual
table together with its FTS5 shadow tables, and the registry entry.

The operations are only available against an online SQLite main database.
Offline ``--sql`` mode, non-SQLite dialects, ``WITHOUT ROWID`` tables and
virtual content tables are explicitly rejected.

How content is stored and synchronized
--------------------------------------

Each managed index is a *standalone* (content-storing) FTS5 table: the
indexed text is duplicated inside FTS5's own shadow tables.  This trades
disk space for independence from the content table's rowid lifetime and
for simple, reliable synchronization:

* the ``AFTER INSERT`` trigger writes the new row's text with the content
  row's rowid;
* the ``AFTER UPDATE`` trigger deletes the old FTS row by rowid and
  inserts the new values, which uniformly covers indexed-column changes,
  NULL/non-NULL flips and primary key updates;
* the ``AFTER DELETE`` trigger deletes the FTS row by rowid.

Because the triggers live in the database, statements committed through
any independent DBAPI connection - not just writes going through
Alembic or SQLAlchemy - keep the index synchronized.  DML and trigger
effects share the application's transaction: a rollback removes both the
content change and its full-text effect, so stale terms can never outlive
a rolled-back write and new rows are never missing from the index.

Several indexes may target the same content table with different columns
or tokenizers; each owns an independent set of triggers and shadow tables
and dropping or rebuilding one never invalidates the others.

Querying
--------

Use the public index name with ordinary FTS5 MATCH queries; the indexed
text columns are also readable directly::

    SELECT rowid FROM "article_search"
    WHERE "article_search" MATCH :q

    SELECT rowid, title, body FROM "article_search"
    WHERE "article_search" MATCH 'title:hello OR body:world'

The returned ``rowid`` is the content table's ``INTEGER PRIMARY KEY``
value, which is how results are joined back to the article rows.

Ownership, the registry and tamper detection
--------------------------------------------

Alembic records every managed index in a registry table named
``alembic_fts_index_registry`` (one row per index) holding the content
table, the ordered columns, the tokenizer, the full JSON definition and a
signature of that definition.  The registry - not name prefixes - is the
authoritative proof of ownership:

* the registry table, the FTS virtual table and its shadow tables, and
  the three synchronization triggers are invisible to ordinary
  autogenerate comparison, so no spurious ``create_table`` /
  ``drop_table`` diffs are ever produced for them;
* FTS5 tables and triggers created directly by the user (without a
  registry row) are never adopted, modified or deleted by Alembic;
* if a registered index's virtual table, triggers or registry row is
  missing, altered or inconsistent, comparison, migration and deletion
  fail with an error naming the affected index.  Alembic never silently
  recreates a plausible-looking definition and never drops objects it
  cannot prove it owns.

Autogenerate, upgrades and downgrades
-------------------------------------

``compare_metadata`` and ``revision --autogenerate`` recognize managed
indexes and emit:

* a ``create_fts_index`` operation for a newly declared index;
* a ``drop_fts_index`` operation for a removed declaration;
* a drop followed by a create when ``columns`` (content or order) or
  ``tokenize`` changes, which rebuilds the index from the current content
  table.  The downgrade restores the previous definition and repopulates
  it from the same content rows.

The comparison process is strictly read-only and never mutates the
metadata; after applying the generated revision a second comparison
reports no differences.  The rendered script carries the complete index
definition, so it can upgrade and downgrade on a different machine without
the original metadata object.

Dropping a content table, or dropping a column that a surviving index
still references, is rejected while the declaration exists; remove the FTS
declaration first so the index is dropped in the same revision before the
ordinary object change.

Interaction with batch and grouped table rebuilds
--------------------------------------------------

When a content table is rebuilt (for example changing the nullability of
an indexed column, or adding/dropping an unrelated column) under
``render_as_batch`` of ``False``, ``True`` or ``"grouped"``:

1. the index's synchronization triggers are detached before the
   copy-and-rename so populating the replacement table cannot double-index
   rows;
2. the ordinary batch rebuild copies the surviving rows, preserving their
   rowid values - tables outside the rebuilt set are not touched;
3. each managed FTS table is cleared and repopulated from the rebuilt
   content table with an SQL-level backfill, its triggers are recreated
   and an FTS5 integrity check is run;
4. in grouped mode all of this happens inside the single group
   transaction together with the foreign-key handling and the
   ``alembic_version`` update.

The existing grouped-mode restrictions (whole-table create/drop, views,
user triggers beyond the managed FTS triggers, offline mode, ...) are
intentionally not widened.  If a rebuild fails, the explicit transaction
rolls the structure, content, full-text state and version stamp back
together; ``PRAGMA foreign_keys`` is restored to the value it had on
entry.
