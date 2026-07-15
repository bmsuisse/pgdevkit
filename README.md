# pgdevkit

A helper for developing with Postgres.

## `pgdb compare`

Compare a directory of SQL scripts (see the `database-in-source` layout
convention) against a live database and report differences:

```bash
pgdb compare --url postgresql://user:pass@host:port/db path/to/database/
```

## `pgdb testdb`

Manages a single shared, Podman-backed Postgres container for local tests
across all your projects — no more one-container-per-project-per-worktree.
Isolation between projects and worktrees is per-database, inside one
container.

Add to `pyproject.toml`:

```toml
[tool.pgdevkit]
name = "myproject"        # optional; defaults to the repo directory name
database_dir = "database" # optional; defaults to "database"
```

Add to `conftest.py`:

```python
import os
import pytest
from pgdevkit.testdb import ensure_testdb

@pytest.fixture(scope="session", autouse=True)
def ensure_test_postgres():
    for k, v in ensure_testdb().items():
        os.environ[k] = v
```

CLI: `pgdb testdb up|reset|run-sql|status|shell|clean`.
