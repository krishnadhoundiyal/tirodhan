"""One event loop per generated history; every example resets disposable application data."""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tirodhan.db.base import Base


class DatabaseHarness:
    def __init__(self, url):
        self.loop = asyncio.new_event_loop()
        self.engine = create_async_engine(url, poolclass=NullPool)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.run(self.reset())

    def run(self, coroutine):
        return self.loop.run_until_complete(coroutine)

    async def reset(self):
        # ORM metadata contains application tables only, never PostGIS extension tables.
        names = ", ".join('"' + t.name + '"' for t in Base.metadata.sorted_tables)
        async with self.engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE TABLE {names} CASCADE"))

    def close(self):
        try:
            self.run(self.reset())
        finally:
            self.run(self.engine.dispose())
            self.loop.close()
