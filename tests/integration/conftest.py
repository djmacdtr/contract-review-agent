import pytest_asyncio
from sqlalchemy import text

from app.db.session import engine


@pytest_asyncio.fixture(autouse=True)
async def clean_database():
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE task_event, task_result, task_file, check_task CASCADE"))
    yield
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE task_event, task_result, task_file, check_task CASCADE"))

