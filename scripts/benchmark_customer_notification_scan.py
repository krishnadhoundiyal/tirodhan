"""Synthetic isolated-schema PostgreSQL plans; no application rows are modified.

Run with TIRODHAN_TEST_DATABASE_URL and --output <JSON path>. Copies the deployed
outbox/inbox/work definitions and indexes, then compares progressively larger histories.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

QUERY = """SELECT outbox_event.* FROM outbox_event
WHERE event_type IN ('CollectionRequestAccepted','CustomerFinancialStatusChanged')
AND available_at <= now()
AND NOT EXISTS (SELECT 1 FROM inbox_message
 WHERE consumer_name='customer-notification-history'
 AND message_id=outbox_event.outbox_event_id::varchar)
ORDER BY created_at,outbox_event_id LIMIT 100 FOR UPDATE SKIP LOCKED"""

INCREMENTAL_QUERY = """SELECT e.* FROM customer_notification_pending p
JOIN outbox_event e ON e.outbox_event_id=p.source_event_id
WHERE p.available_at<=now() ORDER BY p.available_at,p.source_event_id
LIMIT 100 FOR UPDATE OF p SKIP LOCKED"""


async def run() -> dict:
    engine = create_async_engine(os.environ["TIRODHAN_TEST_DATABASE_URL"])
    results = []
    try:
        async with engine.begin() as connection:
            version = await connection.scalar(text("SELECT version()"))
            # Regular tables use shared buffers like production. Only this exact synthetic
            # schema is created/dropped, within one transactional connection.
            schema = "customer_scan_benchmark_" + uuid4().hex
            await connection.execute(text(f"CREATE SCHEMA {schema}"))
            await connection.execute(text(f"SET LOCAL search_path TO {schema},public"))
            for table in ("outbox_event", "inbox_message", "customer_notification_pending"):
                await connection.execute(
                    text(f"CREATE TABLE {table} (LIKE public.{table} INCLUDING ALL)")
                )
            for size in (1000, 10000, 100000, 500000):
                await connection.execute(
                    text("TRUNCATE outbox_event,inbox_message,customer_notification_pending")
                )
                await connection.execute(
                    text("""INSERT INTO outbox_event
                    (outbox_event_id,event_key,aggregate_type,aggregate_id,event_type,payload,
                     status,created_at,available_at,publish_attempt_count)
                    SELECT md5(n::text)::uuid, 'synthetic:'||n, 'collection_request',
                    md5('request')::uuid, 'CollectionRequestAccepted', '{}'::jsonb, 'PENDING',
                    now()-interval '30 days'+n*interval '1 second', now()-interval '1 day',0
                    FROM generate_series(1,:size+10) n"""),
                    {"size": size},
                )
                await connection.execute(
                    text("""INSERT INTO inbox_message
                    (consumer_name,message_id,message_type,status,first_received_at,processed_at)
                    SELECT 'customer-notification-history', md5(n::text)::uuid::text,
                    'CollectionRequestAccepted','PROCESSED',now(),now()
                    FROM generate_series(1,:size) n"""),
                    {"size": size},
                )
                await connection.execute(text("ANALYZE outbox_event"))
                await connection.execute(text("ANALYZE inbox_message"))
                await connection.execute(
                    text("""INSERT INTO customer_notification_pending
                    SELECT outbox_event_id,available_at FROM outbox_event
                    WHERE event_key::text IN
                    (SELECT 'synthetic:'||n FROM generate_series(:size+1,:size+10) n)"""),
                    {"size": size},
                )
                await connection.execute(text("ANALYZE customer_notification_pending"))
                plans = {}
                for strategy, query in (("anti_join", QUERY), ("incremental", INCREMENTAL_QUERY)):
                    plans[strategy] = [
                        await connection.scalar(
                            text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query)
                        )
                        for _ in range(3)
                    ]
                results.append({"processed_history": size, "new_events": 10, "plans": plans})
            await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        return {"postgresql": version, "runs": results}
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    output = parser.parse_args().output
    result = asyncio.run(run())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2))
