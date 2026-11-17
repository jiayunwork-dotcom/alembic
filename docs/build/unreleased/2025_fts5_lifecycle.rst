.. change::
    :tags: feature, sqlite, operations, autogenerate
    :tickets: fts5-lifecycle

    Added full lifecycle support for SQLite FTS5 full-text indexes.
    Indexes are declared in
    ``MetaData.info["sqlite_fts_indexes"]`` as dictionaries with
    ``name``, ``table``, ``columns`` and optional ``tokenize``
    (``"unicode61"`` or ``"porter unicode61"``) entries;
    ``compare_metadata`` and ``revision --autogenerate`` recognize
    additions, removals and definition changes and emit the new
    :meth:`.Operations.create_fts_index` and
    :meth:`.Operations.drop_fts_index` directives.  Each managed index
    is a standalone FTS5 virtual table backfilled with
    ``INSERT ... SELECT`` and maintained by three synchronization
    triggers, so plain application SQL over any connection (including
    primary-key updates and NULL/non-NULL flips) keeps the index
    consistent; transactions roll content and full-text state back
    together.  Ownership is recorded in an internal registry table,
    which also drives tamper detection and hides managed objects from
    ordinary table comparison; user-created FTS tables and triggers are
    never adopted.  Batch (``render_as_batch`` of ``False``, ``True``
    and ``"grouped"``) content-table rebuilds detach the triggers,
    rebuild the indexes from the surviving rows and re-arm them inside
    the same transaction.  Offline ``--sql`` mode, non-SQLite dialects,
    ``WITHOUT ROWID`` and virtual content tables are rejected.

    .. seealso::

        :ref:`sqlite_fts5`
