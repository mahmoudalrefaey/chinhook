# Setting up the read-only database role

The application code checks that a query is a plain SELECT before running it, but a check
written in Python is one layer, and one layer with a gap in it is not a guarantee. The real
guarantee comes from the database itself refusing to let the connection write anything at
all, regardless of what the application layer does or does not catch.

This is a one-time setup step against your Postgres database. Run it once per database, as a
superuser or any role with `CREATEROLE`.

```sql
CREATE ROLE chinhook_ro WITH LOGIN PASSWORD 'choose-a-real-password' NOSUPERUSER NOCREATEDB NOCREATEROLE;

GRANT CONNECT ON DATABASE chinook TO chinhook_ro;
GRANT USAGE ON SCHEMA public TO chinhook_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO chinhook_ro;

-- So a table created after this role exists is still readable without repeating the grant.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO chinhook_ro;

-- The two settings that actually matter. This is what makes a write fail even if a parser
-- somewhere upstream is fooled into allowing it through.
ALTER ROLE chinhook_ro SET default_transaction_read_only = on;
ALTER ROLE chinhook_ro SET statement_timeout = '3s';
```

Replace `chinook` and `public` with the real database and schema name if they differ.

Then set `DATABASE_URL_RO` in the environment to a connection string using this role:

```
DATABASE_URL_RO=postgresql://chinhook_ro:choose-a-real-password@host:port/database?sslmode=require
```

`DATABASE_URL` stays pointed at whatever role already has the access needed for indexing and
for the conversation checkpointer, both of which do write, just never to the application's
own tables. `DATABASE_URL_RO` is what the query path and the value-grounding probes use.

If `DATABASE_URL_RO` is left unset, the app falls back to `DATABASE_URL` for queries too, so it
keeps running without this step. It just runs without the one guarantee that holds even if
every check in the Python code has a hole in it, which is the whole reason this role exists.

## Checking it actually took

```sql
SET ROLE chinhook_ro;
DELETE FROM artist WHERE artist_id = -1;
-- should fail with: cannot execute DELETE in a read-only transaction
RESET ROLE;
```
