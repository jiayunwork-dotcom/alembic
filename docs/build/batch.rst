.. _batch_migrations:

Running "Batch" Migrations for SQLite and Other Databases
=========================================================

The SQLite database presents a challenge to migration tools
in that it has almost no support for the ALTER statement which
relational schema migrations rely upon.  The rationale for this stems from
philosophical and architectural concerns within SQLite, and they are unlikely
to be changed.

Migration tools are instead expected to produce copies of SQLite tables that
correspond to the new structure, transfer the data from the existing
table to the new one, then drop the old table.  For our purposes here
we'll call this **"move and copy"** workflow, and in order to accommodate it
in a way that is reasonably predictable, while also remaining compatible
with other databases, Alembic provides the **batch** operations context.

Within this context, a relational table is named, and then a series of
mutation operations to that table alone are specified within
the block.  When the context is complete, a process begins whereby the
"move and copy" procedure begins; the existing table structure is reflected
from the database, a new version of this table is created with the given
changes, data is copied from the
old table to the new table using "INSERT from SELECT", and finally the old
table is dropped and the new one renamed to the original name.

The :meth:`.Operations.batch_alter_table` method provides the gateway to this
process::

    with op.batch_alter_table("some_table") as batch_op:
        batch_op.add_column(Column('foo', Integer))
        batch_op.drop_column('bar')

When the above directives are invoked within a migration script, on a
SQLite backend we would see SQL like:

.. sourcecode:: sql

    CREATE TABLE _alembic_batch_temp (
      id INTEGER NOT NULL,
      foo INTEGER,
      PRIMARY KEY (id)
    );
    INSERT INTO _alembic_batch_temp (id) SELECT some_table.id FROM some_table;
    DROP TABLE some_table;
    ALTER TABLE _alembic_batch_temp RENAME TO some_table;

On other backends, we'd see the usual ``ALTER`` statements done as though
there were no batch directive - the batch context by default only does
the "move and copy" process if SQLite is in use, and if there are
migration directives other than :meth:`.Operations.add_column` present,
which is the one kind of column-level ALTER statement that SQLite supports.
:meth:`.Operations.batch_alter_table` can be configured
to run "move and copy" unconditionally in all cases, including on databases
other than SQLite; more on this is below.

.. _batch_controlling_table_reflection:

Controlling Table Reflection
----------------------------

The :class:`~sqlalchemy.schema.Table` object that is reflected when
"move and copy" proceeds is performed using the standard ``autoload=True``
approach.  This call can be affected using the
:paramref:`~.Operations.batch_alter_table.reflect_args` and
:paramref:`~.Operations.batch_alter_table.reflect_kwargs` arguments.
For example, to override a :class:`~sqlalchemy.schema.Column` within
the reflection process such that a :class:`~sqlalchemy.types.Boolean`
object is reflected with the ``create_constraint`` flag set to ``False``::

    with self.op.batch_alter_table(
        "bar",
        reflect_args=[Column('flag', Boolean(create_constraint=False))]
    ) as batch_op:
        batch_op.alter_column(
            'flag', new_column_name='bflag', existing_type=Boolean)

Another use case, add a listener to the :class:`~sqlalchemy.schema.Table`
as it is reflected so that special logic can be applied to columns or
types, using the :meth:`~sqlalchemy.events.DDLEvents.column_reflect` event::

    def listen_for_reflect(inspector, table, column_info):
        "correct an ENUM type"
        if column_info['name'] == 'my_enum':
            column_info['type'] = Enum('a', 'b', 'c')

    with self.op.batch_alter_table(
        "bar",
        reflect_kwargs=dict(
            listeners=[
                ('column_reflect', listen_for_reflect)
            ]
        )
    ) as batch_op:
        batch_op.alter_column(
            'flag', new_column_name='bflag', existing_type=Boolean)

The reflection process may also be bypassed entirely by sending a
pre-fabricated :class:`~sqlalchemy.schema.Table` object; see
:ref:`batch_offline_mode` for an example.

.. _sqlite_batch_constraints:

Dealing with Constraints
------------------------

There are a variety of issues when using "batch" mode with constraints,
such as FOREIGN KEY, CHECK and UNIQUE constraints.  This section
will attempt to detail many of these scenarios.

.. _dropping_sqlite_foreign_keys:

Dropping Unnamed or Named Foreign Key Constraints
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

SQLite, unlike any other database, allows constraints to exist in the
database that have no identifying name.  On all other backends, the
target database will always generate some kind of name, if one is not
given.

The challenge this represents is that an unnamed constraint can't
by itself be targeted by the :meth:`.BatchOperations.drop_constraint` method.
An unnamed FOREIGN KEY constraint is implicit whenever the
:class:`~sqlalchemy.schema.ForeignKey`
or :class:`~sqlalchemy.schema.ForeignKeyConstraint` objects are used without
passing them a name.  Only on SQLite will these constraints remain entirely
unnamed when they are created on the target database; an automatically generated
name will be assigned in the case of all other database backends.

Within the scope of batch mode, this presents the issue that the
:meth:`.BatchOperations.drop_constraint` method requires a constraint name
in order to target the correct constraint.

In order to overcome this, the :meth:`.Operations.batch_alter_table` method supports a
:paramref:`~.Operations.batch_alter_table.naming_convention` argument, so that
all reflected constraints, including foreign keys that are unnamed may be
given a name, as described in :ref:`autogen_naming_conventions`. 
Usage is as follows::

    naming_convention = {
        "fk":
        "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    }
    with self.op.batch_alter_table(
            "bar", naming_convention=naming_convention) as batch_op:
        batch_op.drop_constraint(
            "fk_bar_foo_id_foo", type_="foreignkey")

Note that the naming convention feature requires at least
**SQLAlchemy 0.9.4** for support.

.. _batch_schematype_constraints:

Changing the Type of Boolean, Enum and other implicit CHECK datatypes
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The SQLAlchemy types :class:`~sqlalchemy.types.Boolean` and
:class:`~sqlalchemy.types.Enum` are part of a category of types known as
"schema" types; this style of type creates other structures along with the
type itself, most commonly (but not always) a CHECK constraint.

Alembic handles dropping and creating the CHECK constraints here automatically,
including in the case of batch mode.  When changing the type of an existing
column, what's necessary is that the existing type be specified fully::

  with self.op.batch_alter_table("some_table") as batch_op:
      batch_op.alter_column(
          'q', type_=Integer,
          existing_type=Boolean(create_constraint=True, constraint_name="ck1"))

When dropping a column that includes a named CHECK constraint, as of Alembic
1.7 this named constraint must also be provided using a similar form, as there
is no ability for Alembic to otherwise link this reflected CHECK constraint as
belonging to a particular column::

    with self.op.batch_alter_table("some_table") as batch_op:
        batch_op.drop_column(
            'q',
            existing_type=Boolean(create_constraint=True, constraint_name="ck1"))
        )

.. versionchanged:: 1.7  The :meth:`.BatchOperations.drop_column` operation can
   accept an ``existing_type`` directive where a "schema type" such as
   :class:`~sqlalchemy.types.Boolean` and :class:`~sqlalchemy.types.Enum` may
   be specified such that an associated named constraint can be removed.

.. _batch_check_constraints:

Including CHECK constraints
^^^^^^^^^^^^^^^^^^^^^^^^^^^

As of Alembic 1.7, **named** CHECK constraints are automatically included
in batch mode, as modern SQLAlchemy versions are capable of reflecting these
constraints like any other constraint.

Note that when dropping or renaming a column that is mentioned in a named
CHECK constraint, this CHECK constraint must be explicitly dropped first,
as Alembic has no means of linking a reflected CHECK constraint to that
column.  Supposing column ``q`` of ``some_table`` were mentioned in a CHECK
constraint named ``ck1``.  In order to drop this column, we have to drop
the check constraint also::

    with self.op.batch_alter_table("some_table") as batch_op:
        batch_op.drop_constraint("ck1", type_="check")
        batch_op.drop_column('q')

.. versionchanged:: 1.7  Named CHECK constraints participate in batch mode
   in the same way as any other kind of constraint. This requires that column
   drops or renames now include explicit directives to drop an existing named
   constraint which refers to this column, as it will otherwise not be
   automatically detected as being associated with that particular column.

   Unnamed CHECK constraints continue to be silently omitted from the table
   recreate operation.

For **unnamed** CHECK constraints, these are still not automatically included
as part of the batch process.  Note that this limitation **includes** the CHECK
constraints generated by the
:class:`~sqlalchemy.types.Boolean` or :class:`~sqlalchemy.types.Enum`
datatypes, which up through SQLAlchemy 1.3 would generate CHECK constraints
automatically and cannot be tracked to the reflected table, assuming they are
generated in an unnamed way.

Unnamed constraints can be stated explicitly if they are to be included in the
recreated table::

    with op.batch_alter_table("some_table", table_args=[
          CheckConstraint('x > 5')
      ]) as batch_op:
        batch_op.add_column(Column('foo', Integer))
        batch_op.drop_column('bar')

The above step needs only be taken for CHECK constraints that are explicitly stated
as part of the table definition.

For CHECK constraints that are generated by datatypes such as
:class:`~sqlalchemy.types.Boolean` or :class:`~sqlalchemy.types.Enum`, the type
objects themselves **must be named** in order for their CHECK constraints to be
included in the batch process.   Boolean and Enum datatypes that do not
have the ``.name`` attribute set will **not** have CHECK constraints
regenerated.  This name can be set by specifying the ``.name`` parameter
or by using a named Python ``Enum`` object as the source of enumeration.

Dealing with Referencing Foreign Keys
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

It is important to note that batch table operations **do not work** with
foreign keys that enforce referential integrity.  This because the
target table is dropped; if foreign keys refer to it, this will raise
an error.   On SQLite, whether or not foreign keys actually enforce is
controlled by the ``PRAGMA FOREIGN KEYS`` pragma; this pragma, if in use,
must be disabled when the workflow mode proceeds.   When the operation is
complete, the batch-migrated table will have the same name
that it started with, so those referring foreign keys will again
refer to this table.

A special case is dealing with self-referring foreign keys.  Here,
Alembic takes a special step of recreating the self-referring foreign key
as referring to the original table name, rather than at the "temp" table,
so that like in the case of other foreign key constraints, when the table
is renamed to its original name, the foreign key
again references the correct table.   This operation only works when
referential integrity is disabled, consistent with the same requirement
for referring foreign keys from other tables.

When SQLite's ``PRAGMA FOREIGN KEYS`` mode is turned on, it does provide
the service that foreign key constraints, including self-referential, will
automatically be modified to point to their table across table renames,
however this mode prevents the target table from being dropped as is required
by a batch migration.  Therefore it may be necessary to manipulate the
``PRAGMA FOREIGN KEYS`` setting if a migration seeks to rename a table vs.
batch migrate it.

.. _batch_offline_mode:

Working in Offline Mode
-----------------------

In the preceding sections, we've seen how much of an emphasis the
"move and copy" process has on using reflection in order to know the
structure of the table that is to be copied.  This means that in the typical
case, "online" mode, where a live database connection is present so that
:meth:`.Operations.batch_alter_table` can reflect the table from the
database, is required; the ``--sql`` flag **cannot** be used without extra
steps.

To support offline mode, the system must work without table reflection
present, which means the full table as it intends to be created must be
passed to :meth:`.Operations.batch_alter_table` using
:paramref:`~.Operations.batch_alter_table.copy_from`::

    meta = MetaData()
    some_table = Table(
        'some_table', meta,
        Column('id', Integer, primary_key=True),
        Column('bar', String(50))
    )

    with op.batch_alter_table("some_table", copy_from=some_table) as batch_op:
        batch_op.add_column(Column('foo', Integer))
        batch_op.drop_column('bar')

The above use pattern is pretty tedious and quite far off from Alembic's
preferred style of working; however, if one needs to do SQLite-compatible
"move and copy" migrations and need them to generate flat SQL files in
"offline" mode, there's not much alternative.


Batch mode with Autogenerate
----------------------------

The syntax of batch mode is essentially that :meth:`.Operations.batch_alter_table`
is used to enter a batch block, and the returned :class:`.BatchOperations` context
works just like the regular :class:`.Operations` context, except that
the "table name" and "schema name" arguments are omitted.

To support rendering of migration commands in batch mode for autogenerate,
configure the :paramref:`.EnvironmentContext.configure.render_as_batch`
flag in ``env.py``::

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True
    )

Autogenerate will now generate along the lines of::

    def upgrade():
        ### commands auto generated by Alembic - please adjust! ###
        with op.batch_alter_table('address', schema=None) as batch_op:
            batch_op.add_column(sa.Column('street', sa.String(length=50), nullable=True))

This mode is safe to use in all cases, as the :meth:`.Operations.batch_alter_table`
directive by default only takes place for SQLite; other backends will
behave just as they normally do in the absence of the batch directives.

Note that autogenerate support does not include "offline" mode, where
the :paramref:`.Operations.batch_alter_table.copy_from` parameter is used.
The table definition here would need to be entered into migration files
manually if this is needed.

Batch mode with databases other than SQLite
--------------------------------------------

There's an odd use case some shops have, where the "move and copy" style
of migration is useful in some cases for databases that do already support
ALTER.   There's some cases where an ALTER operation may block access to the
table for a long time, which might not be acceptable.  "move and copy" can
be made to work on other backends, though with a few extra caveats.

The batch mode directive will run the "recreate" system regardless of
backend if the flag ``recreate='always'`` is passed::

    with op.batch_alter_table("some_table", recreate='always') as batch_op:
        batch_op.add_column(Column('foo', Integer))

The issues that arise in this mode are mostly to do with constraints.
Databases such as Postgresql and MySQL with InnoDB will enforce referential
integrity (e.g. via foreign keys) in all cases.   Unlike SQLite, it's not
as simple to turn off referential integrity across the board (nor would it
be desirable).    Since a new table is replacing the old one, existing
foreign key constraints which refer to the target table will need to be
unconditionally dropped before the batch operation, and re-created to refer
to the new table afterwards.  Batch mode currently does not provide any
automation for this.

The Postgresql database and possibly others also have the behavior such
that when the new table is created, a naming conflict occurs with the
named constraints of the new table, in that they match those of the old
table, and on Postgresql, these names need to be unique across all tables.
The Postgresql dialect will therefore emit a "DROP CONSTRAINT" directive
for all constraints on the old table before the new one is created; this is
"safe" in case of a failed operation because Postgresql also supports
transactional DDL.

Note that also as is the case with SQLite, CHECK constraints need to be
moved over between old and new table manually using the
:paramref:`.Operations.batch_alter_table.table_args` parameter.


.. _sqlite_grouped_batch:

SQLite grouped batch migrations (multi-table)
---------------------------------------------

Ordinary :meth:`.Operations.batch_alter_table` blocks rebuild **one table at
a time**.  On SQLite this means the application has to switch
``PRAGMA foreign_keys`` off in ``env.py`` while the migration runs, because
the intermediate, partially-migrated schema would otherwise violate foreign
keys.  This manual switch is awkward for tables that reference each other,
including self-referential foreign keys and multi-table foreign key cycles,
and a per-table rebuild offers no single transaction across the whole
migration.

The :meth:`.Operations.sqlite_batch_group` context manager collects several
``batch_alter_table()`` blocks and applies them as **one atomic grouped
migration**, taking ownership of the ``foreign_keys`` pragma and the
transaction so that ``env.py`` no longer has to::

    def upgrade() -> None:
        with op.sqlite_batch_group():
            with op.batch_alter_table("orders") as batch_op:
                batch_op.alter_column(
                    "note", existing_type=sa.String(20), nullable=False
                )
            with op.batch_alter_table("order_items") as batch_op:
                batch_op.alter_column(
                    "label", existing_type=sa.String(20), nullable=False
                )

Inside the group the following batch directives are supported:
``alter_column``, ``add_column``, ``drop_column``, ``create_foreign_key``,
``drop_constraint``, ``create_index`` and ``drop_index``.  Tables whose
changes can be applied with a native ``ALTER TABLE`` (for example a plain
``add_column``) are altered directly and never use a copy table, while the
others use the same copy-and-rename machinery as regular batch mode.

Configuration
~~~~~~~~~~~~~

Grouped migrations are online, SQLite-only, and require
:paramref:`.EnvironmentContext.configure.transaction_per_migration` to be
enabled, because the group transaction must also cover the
``alembic_version`` update::

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        transaction_per_migration=True,
    )

How it works, and the trade-offs
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When the group starts, Alembic:

1. validates every collected plan against the live database **before any
   change is made**;
2. records the current value of ``PRAGMA foreign_keys`` and disables foreign
   key enforcement for the duration of the rebuild;
3. opens a single explicit SQLite transaction (``BEGIN``) so that DDL is
   transactional - pysqlite would otherwise auto-commit DDL statements;
4. rebuilds the tables that need it, using group-unique temporary table
   names, and applies native ``ALTER TABLE`` to the others;
5. runs ``PRAGMA foreign_key_check`` and only then commits, restoring the
   previous ``foreign_keys`` value.

Because every table of the group is rebuilt inside the same transaction
while foreign keys are temporarily disabled, foreign key cycles and
self-references need no special ordering, and tables *outside* the group
(including tables that reference group tables with ``ON DELETE CASCADE``)
are never touched.

* **Rollback reliability.** If any step fails - copying data, creating an
  index, or writing the version row - the whole transaction is rolled back
  and the database is left exactly as it was before the revision; the
  original error is re-raised unchanged, and no data is modified or deleted
  to make constraints pass.
* **Temporary disk usage.** Each rebuilt table briefly occupies a second
  copy of its data, so peak extra disk space is roughly the sum of the
  rebuilt tables' sizes.  Temporary tables, indexes and references are
  always removed (or rolled back) and never remain in ``sqlite_master``.
* **Lock hold time.** The write lock is held for the whole rebuild, which
  is longer than an independent per-table migration.  This is the cost of
  making the change atomic; other connections cannot observe a partially
  migrated schema and simply block until commit or rollback.
* **Scope.** Only ordinary tables in the main database may be rebuilt.
  Virtual tables, tables with triggers, and views (or other schema objects)
  that depend on a rebuilt table are rejected up front; drop and recreate
  such objects around the group yourself.  Offline ``--sql`` mode,
  pre-existing caller transactions, nested groups and more than one group
  per revision are likewise rejected before anything changes.

Autogenerate
~~~~~~~~~~~~

Set :paramref:`.EnvironmentContext.configure.render_as_batch` to the string
``"grouped"`` to have autogenerate wrap the per-table batch blocks of a
revision in a single ``op.sqlite_batch_group()`` block::

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch="grouped",
    )

The existing ``False`` (plain directives) and ``True`` (per-table batch
blocks, no group) behaviors are unchanged.  Because a group can only alter
existing tables, a revision that also creates or drops whole tables cannot
be rendered as one group and autogenerate reports the unsupported
differences instead of emitting a script that would fail at runtime.

.. _sqlite_batch_autoincrement:

Preserving ``AUTOINCREMENT`` numbering across a rebuild
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

SQLite keeps the highest number ever committed for an ``AUTOINCREMENT``
table in its internal ``sqlite_sequence`` table.  That number is *not* part
of the ``CREATE TABLE`` statement, and SQLAlchemy's reflection does not
report the attribute, so a naive copy-and-rename rebuild drops both the
``AUTOINCREMENT`` keyword and the committed history.  After such a rebuild
SQLite hands out numbers starting from the highest remaining row, which
means numbers belonging to deleted rows can be issued again::

    CREATE TABLE fact(id INTEGER PRIMARY KEY AUTOINCREMENT,
                      note VARCHAR(20));
    INSERT INTO fact(id) VALUES (1), (1000);
    DELETE FROM fact WHERE id = 1000;
    -- after a plain batch rebuild of "fact":
    INSERT INTO fact(note) VALUES ('new');  -- id 2, expected 1001

Alembic now carries the attribute and the history through the rebuild:

* If the live table uses ``AUTOINCREMENT``, an ordinary batch rebuild keeps
  it and restores the committed ``sqlite_sequence`` mark, even when the
  table has been emptied and even when the row holding the highest number
  was deleted.  The maximum *remaining* primary key is never used as a
  substitute for the committed history.
* The same applies when the attribute is requested explicitly with
  ``table_kwargs={"sqlite_autoincrement": True}``, on
  :meth:`.Operations.batch_alter_table` and inside
  :meth:`.Operations.sqlite_batch_group`.
* A table created as a plain ``INTEGER PRIMARY KEY`` keeps SQLite's ordinary
  allocation--the first presumably suitable number is used and deleted
  numbers may be reused.
* Each table's history is independent; tables outside the group and tables
  that took the native ``ALTER TABLE`` path are untouched.
* Numbers are restored inside the same transaction as the rebuild, so a
  failed or rolled-back migration (including the ``alembic_version``
  update) restores the previous history.  Numbers committed by a
  transaction that later rolled back were never part of the history.
* If the committed mark has reached SQLite's signed 64-bit ceiling, the
  next insert after the migration still raises an error rather than reusing
  a number, exactly as it would without the migration.

To change the attribute itself, use
:meth:`.Operations.set_sqlite_autoincrement` inside a batch block::

    with op.batch_alter_table("fact") as batch_op:
        # enable AUTOINCREMENT, keeping the committed history
        batch_op.set_sqlite_autoincrement(enabled=True, existing_enabled=False)

    with op.sqlite_batch_group():
        with op.batch_alter_table("fact") as batch_op:
            # disable it: ordinary allocation applies from now on and the
            # old non-reuse guarantee is intentionally dropped
            batch_op.set_sqlite_autoincrement(enabled=False, existing_enabled=True)

Enabling the attribute starts a fresh history from the data present at
that moment; enabling it again after it was disabled starts a new history
from the data present then.  A migration that changes something else while
the attribute remains in place continues the original history.

In grouped autogenerate mode (``render_as_batch="grouped"``) a difference
in the ``sqlite_autoincrement`` table option is detected and rendered as a
``set_sqlite_autoincrement()`` directive with a reversing downgrade.  This
comparison is only performed in grouped mode; ``render_as_batch=False`` and
``render_as_batch=True`` keep their previous behavior.

Implementation notes and trade-offs
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Two ways of moving the numbering history across a rebuild were considered.

*A dialect hook inside the existing rebuild* (the one that is implemented)
has the SQLite dialect read the committed mark from ``sqlite_sequence``
just before the original table is dropped, and write it back after the
replacement table has been renamed, using the ``begin_batch_table_rebuild``
/ ``finish_batch_table_rebuild`` hooks on
:class:`.DefaultImpl`.  Only a single integer per rebuilt table is carried
in Python; no business row is kept alive to "reserve" a number and no
auxiliary table is created.  The read and the write are ordinary statements
of the same transaction that already owns the rebuild, so they cost no extra
temporary database objects, add only a negligible amount to the lock hold
time, and are rolled back together with the DDL, the copied data and the
``alembic_version`` stamp.  The dialect dependency is limited to
``SQLiteImpl``; other backends inherit no-op hooks.

*A post-group fixup or a side table* would record the history outside the
rebuild (for example a Python dictionary finalized after the group commits,
or a journal table that a later step replays).  It has to either give up
atomicity - a failure between the rebuild and the fixup would leave the
history lost - or introduce an additional schema object and a second write
phase at the end of the migration, extending the lock hold time and moving
part of the work outside the group's rollback scope.  It would also have to
be wired into both grouped and per-table batch runs to keep ordinary batch
mode consistent.  The in-transaction hook was therefore chosen.

