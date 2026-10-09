# Plan amendments after `/bms-plan-review` (verdict: go, with changes)

1. `source` = `AsyncConnection | Callable[[], AsyncContextManager[AsyncConnection]]` only (`pool.connection` / `get_pg_connection` are callables) — no duck-typed pool branch.
2. Caller-owned connection must outlive the response (`Depends` with `yield`, FastAPI >= 0.118); `async with get_pg_connection() as c: return Resp(c, ...)` closes it before streaming. Document + test; recommend the callable form.
3. `query: LiteralString | Composable` (not `str`); single `# bdt-lint: ignore sql-unverified-call` at the one `SQL(cast(LiteralString, q))`. Run `bdt lint` + `ty check`.
4. `expose_errors` = class attribute overridden via subclassing only (no runtime assignment, no per-call kwarg).
5. Chunked `stream(size=N)` needs libpq >= 17 — fall back to `size=1` when `psycopg.pq.version() < 170000`.
6. Own-watcher cancellation (client gone) is swallowed; outer-scope cancellation re-raised.
7. Disconnect tests call the ASGI app directly with a controllable `receive` (ASGITransport cannot abort mid-request); Granian e2e also asserts a mid-stream error aborts the response rather than ending it cleanly. Playwright e2e n/a (no UI).
8. Nits: drop `cache_seconds` (use `headers=`); no `from __future__ import annotations`; docs note data-modifying CTEs need `query_produces_json=True`, and that the response does no authorization / params must be bound.
