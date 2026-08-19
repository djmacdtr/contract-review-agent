"""Create/migrate an isolated PostgreSQL database, then run the test suite."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess

import asyncpg

TEST_DATABASE = "contract_review_test"


async def prepare_database() -> None:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", TEST_DATABASE):
        raise RuntimeError("Unsafe test database name")

    admin_url = os.environ["TEST_ADMIN_DATABASE_URL"]
    connection = await asyncpg.connect(admin_url)
    try:
        exists = await connection.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", TEST_DATABASE
        )
        if not exists:
            await connection.execute(f'CREATE DATABASE "{TEST_DATABASE}"')
    finally:
        await connection.close()


def main() -> None:
    asyncio.run(prepare_database())
    subprocess.run(["alembic", "upgrade", "head"], check=True)
    subprocess.run(
        ["pytest", "-q", "-p", "no:cacheprovider"],
        check=True,
    )


if __name__ == "__main__":
    main()
