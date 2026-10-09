# Implementation plan: `pgdevkit[fastapi]` — streaming Postgres JSON response with query cancellation

## Goal
Move CCMT2's `backend/api/sql_response.py::PostgresJsonResponse` into pgdevkit as an optional extra so CCMT2, OneSales and MDMApp share one implementation, and make client-disconnect **actually cancel the query in Postgres**.

## Findings that drive the design
* **All three apps already expose the same shape**: `get_pg_connection() -> AsyncContextManager[AsyncConnection]` (CCMT2/OneSales: module-level pool; MDMApp: built on `pgdevkit.db.PgPool`). So the response takes a *connection source* instead of CCMT2's `"postgres"` sentinel + global import.
* **Use cases**: (1) CCMT2 — ~10 call sites, direct port; (2) OneSales / MDMApp — list/grid endpoints doing `fetchall()` + `dict_row` + FastAPI serialization (19 direct-return sites + many more via pydantic models); (3) any long query where the user navigates away / the frontend aborts the fetch.
* **Spike (real PG 18, psycopg 3.3.4)**: when the awaiting task is cancelled, the pool just discards the socket and **the backend keeps running the query** (`pg_sleep(30)` still `active`). A *shielded* `conn.cancel_safe()` stops it, for both `execute` and `stream`. Connection comes back `ACTIVE` → psycopg logs "error ignored in rollback" noise unless we close it ourselves.
* **Granian reports ASGI `spec_version 2.3`**, so today Starlette's `StreamingResponse` listens for disconnect, but on spec ≥ 2.4 it does not (it relies on `send()` raising). CCMT2's current code therefore depends on server spec version. We own disconnect detection instead.
* CCMT2's current implementation fetches via `fetchmany` after a client-side full `execute` (whole result buffered in libpq memory) and swallows mid-stream errors by ending the body with valid-looking truncation.

## Design (`pgdevkit/fastapi/`)
```python
from pgdevkit.fastapi import PostgresJsonResponse
return PostgresJsonResponse(get_pg_connection, "select ... where id = ANY(%(ids)s)", parameters={"ids": ids})
```
* **`source`**: `AsyncConnection` (caller-owned, never closed) · `PgPool` / `psycopg_pool` pool (anything with `.connection()`) · zero-arg callable returning an async CM of `AsyncConnection` (e.g. `get_pg_connection`). Pool connections are acquired **inside** the stream, not at construction, and released when the stream ends.
* **`query`**: `str | psycopg.sql.Composable`. Wrapped server-side as `select row_to_json(s)::text from (<query>) s` (trailing `;` stripped); `query_produces_json=True` skips wrapping (one text/json column per row). Params bound by psycopg (`%(name)s`) — no sqlglot dependency in the extra.
* **Output**: `[row,\nrow,…]`, identical to today's CCMT2 format. True streaming via `cursor.stream(size=batch_size)` (libpq chunked mode, default 1000) instead of buffering the full result. `cache_seconds` → `Cache-Control: max-age`, plus normal `headers`/`status_code`.
* **Errors before first byte**: HTTP 500 JSON `{"error": ...}`; detail text only if `expose_errors` (class attribute default `False`, overridable per call — apps set `IS_DEV or IS_TEST`). `HTTPException` raised by callers propagates unchanged.
* **Errors after headers are sent**: cancel + clean up, then **re-raise** so the server aborts the chunked response (client sees a network/JSON error rather than a silently truncated array) and APM/Sentry see it.
* **Not ported** (CCMT2-specific, stays in CCMT2): `debug_sql` (sqlglot/sqlfmt), `before_close_conn`, DuckDB branches.

## Query cancellation
Triggers: (a) `http.disconnect` while the query runs / before first byte — detected by our own `receive()` watcher in `__call__` (anyio task group), independent of spec version; (b) task cancelled by the server/ Starlette; (c) `send()` raising `OSError` mid-stream; (d) any other exception while the query is `ACTIVE`.
Mechanics, in a `finally` under `anyio.CancelScope(shield=True)` with a bounded timeout:
1. Only if `conn.info.transaction_status == ACTIVE` (query genuinely in flight) → `await conn.cancel_safe()`. This prevents the classic race of cancelling a *later* statement on a reused pooled connection.
2. If we acquired the connection from a pool: `await conn.close()` so it is never returned dirty (also silences psycopg's rollback warnings). The pool opens a fresh one.
3. Caller-owned connection: only the cancel is sent; the transaction is left in the aborted state — documented.
Works with PgBouncer/Azure null-pool (cancel is forwarded by key); psycopg ≥ 3.2 required (`cancel_safe`).

## Packaging
`pyproject.toml`: `fastapi = ["fastapi>=0.116.1"]` extra (anyio comes with Starlette; no pool/sqlglot additions); test group + `httpx`, `granian`; version bump `0.12.0 → 0.13.0` (repo convention: every feature bumps).

## Tests (real Postgres, no mocks; one isolated DB, ≤ 3 connections — the shared server is near `max_connections`)
Output shape (empty / < batch / > batch / multi-batch), params, `Composable`, `query_produces_json`, headers/cache, each source kind, pool connection returned & reusable, error before first byte (± `expose_errors`), `HTTPException` passthrough, mid-stream error re-raised, **cancel on disconnect before first byte (`pg_sleep`) / during streaming / after completion is a no-op**, backend verified gone via `pg_stat_activity`, no leaked pool connection. Plus **one end-to-end test under a real Granian server** (production server) with an aborted HTTP client.

## Docs
README section + `skills/pgdevkit/SKILL.md` (it ships to the apps) with the 3-line integration recipe for CCMT2 / OneSales / MDMApp and the "when not to use" note (bypasses pydantic `response_model` validation — meant for large/grid endpoints where SQL already shapes the output).

## Out of scope / follow-ups
* Migrating CCMT2 / OneSales / MDMApp call sites (separate PRs after pgdevkit 0.13.0 is released; CCMT2 becomes a ~10-line shim keeping `debug_sql`).
* `single=True` (one object / 404) mode and a `cancel_on_disconnect(request, conn)` context manager for non-streaming endpoints.
