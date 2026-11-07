"""Independent backend connections for background Jev observations."""


def observer_connection(store):
    """Capture connection configuration on the owner thread; open per observer."""
    from plangraph.keeper import db, db_pg
    if isinstance(store.conn, db_pg.PgConnection):
        # libpq's rendered DSN omits the password; preserve the connection's
        # actual parameters for observers using password-authenticated servers.
        from psycopg.conninfo import make_conninfo
        dsn = make_conninfo(**store.conn._raw.info.get_parameters(),
                            password=store.conn._raw.info.password)
        schema = store.conn.schema
        return lambda: db_pg.open_pg(None, dsn=dsn, schema=schema)
    database = store.conn.execute("PRAGMA database_list").fetchone()[2]
    return lambda: db.open_db(database, backend="sqlite")
