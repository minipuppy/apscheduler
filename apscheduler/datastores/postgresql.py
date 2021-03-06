import asyncio
import logging
from asyncio import current_task
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Set
from uuid import UUID

import sniffio
from anyio import create_task_group, move_on_after, sleep
from anyio.abc import TaskGroup
from asyncpg import UniqueViolationError
from asyncpg.pool import Pool

from ..abc import DataStore, Job, Schedule, Serializer
from ..events import (
    EventHub, JobAdded, ScheduleAdded, ScheduleEvent, ScheduleRemoved, ScheduleUpdated)
from ..exceptions import ConflictingIdError, SerializationError
from ..policies import ConflictPolicy
from ..serializers.pickle import PickleSerializer

logger = logging.getLogger(__name__)


class PostgresqlDataStore(DataStore, EventHub):
    _task_group: TaskGroup
    _schedules_event: Optional[asyncio.Event] = None
    _jobs_event: Optional[asyncio.Event] = None

    def __init__(self, pool: Pool, *, schema: str = 'public',
                 notify_channel: Optional[str] = 'apscheduler',
                 serializer: Optional[Serializer] = None,
                 lock_expiration_delay: float = 30, max_poll_time: Optional[float] = 1,
                 max_idle_time: float = 60, start_from_scratch: bool = False):
        super().__init__()
        self.pool = pool
        self.schema = schema
        self.notify_channel = notify_channel
        self.serializer = serializer or PickleSerializer()
        self.lock_expiration_delay = lock_expiration_delay
        self.max_poll_time = max_poll_time
        self.max_idle_time = max_idle_time
        self.start_from_scratch = start_from_scratch
        self._logger = logging.getLogger(__name__)
        self._loans = 0

    async def __aenter__(self):
        asynclib = sniffio.current_async_library() or '(unknown)'
        if asynclib != 'asyncio':
            raise RuntimeError(f'This data store requires asyncio; currently running: {asynclib}')

        if self._loans == 0:
            self._schedules_event = asyncio.Event()
            self._jobs_event = asyncio.Event()
            await self._setup()

        self._loans += 1
        if self._loans == 1 and self.notify_channel:
            print('entering postgresql data store in task', id(current_task()))
            self._task_group = create_task_group()
            await self._task_group.__aenter__()
            await self._task_group.spawn(self._listen_notifications)

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        assert self._loans
        self._loans -= 1
        if self._loans == 0 and self.notify_channel:
            print('exiting postgresql data store in task', id(current_task()))
            await self._task_group.cancel_scope.cancel()
            await self._task_group.__aexit__(exc_type, exc_val, exc_tb)
            del self._schedules_event
            del self._jobs_event

    async def _listen_notifications(self) -> None:
        def callback(connection, pid, channel: str, payload: str) -> None:
            self._logger.debug('Received notification on channel %s: %s', channel, payload)
            if payload == 'schedule':
                self._schedules_event.set()
            elif payload == 'job':
                self._jobs_event.set()

        while True:
            async with self.pool.acquire() as conn:
                await conn.add_listener(self.notify_channel, callback)
                try:
                    while True:
                        await sleep(self.max_idle_time)
                        await conn.execute('SELECT 1')
                finally:
                    await conn.remove_listener(self.notify_channel, callback)

    async def _setup(self) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            if self.start_from_scratch:
                await conn.execute(f"DROP TABLE IF EXISTS {self.schema}.schedules")
                await conn.execute(f"DROP TABLE IF EXISTS {self.schema}.jobs")
                await conn.execute(f"DROP TABLE IF EXISTS {self.schema}.metadata")

            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.schema}.metadata (
                    schema_version INTEGER NOT NULL
                )
            """)
            version = await conn.fetchval(f"SELECT schema_version FROM {self.schema}.metadata")
            if version is None:
                await conn.execute(f"INSERT INTO {self.schema}.metadata VALUES (1)")
                await conn.execute(f"""
                    CREATE TABLE {self.schema}.schedules (
                        id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL,
                        serialized_data BYTEA NOT NULL,
                        next_fire_time TIMESTAMP WITH TIME ZONE,
                        acquired_by TEXT,
                        acquired_until TIMESTAMP WITH TIME ZONE
                    ) WITH (fillfactor = 80)
                """)
                await conn.execute(f"CREATE INDEX ON {self.schema}.schedules (next_fire_time)")
                await conn.execute(f"""
                    CREATE TABLE {self.schema}.jobs (
                        id UUID PRIMARY KEY,
                        serialized_data BYTEA NOT NULL,
                        task_id TEXT NOT NULL,
                        tags TEXT[] NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
                        acquired_by TEXT,
                        acquired_until TIMESTAMP WITH TIME ZONE
                    ) WITH (fillfactor = 80)
                """)
                await conn.execute(f"CREATE INDEX ON {self.schema}.jobs (task_id)")
                await conn.execute(f"CREATE INDEX ON {self.schema}.jobs (tags)")
            elif version > 1:
                raise RuntimeError(f'Unexpected schema version ({version}); '
                                   f'only version 1 is supported by this version of APScheduler')

    async def clear(self) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(f"TRUNCATE TABLE {self.schema}.schedules")
            await conn.execute(f"TRUNCATE TABLE {self.schema}.jobs")

    async def add_schedule(self, schedule: Schedule, conflict_policy: ConflictPolicy) -> None:
        event: Optional[ScheduleEvent] = None
        serialized_data = self.serializer.serialize(schedule)
        query = (f"INSERT INTO {self.schema}.schedules (id, serialized_data, task_id, "
                 f"next_fire_time) VALUES ($1, $2, $3, $4)")
        async with self.pool.acquire() as conn:
            try:
                async with conn.transaction():
                    await conn.execute(query, schedule.id, serialized_data, schedule.task_id,
                                       schedule.next_fire_time)
            except UniqueViolationError:
                if conflict_policy is ConflictPolicy.exception:
                    raise ConflictingIdError(schedule.id) from None
                elif conflict_policy is ConflictPolicy.replace:
                    query = (f"UPDATE {self.schema}.schedules SET serialized_data = $2, "
                             f"task_id = $3, next_fire_time = $4 WHERE id = $1")
                    async with conn.transaction():
                        await conn.execute(query, schedule.id, serialized_data, schedule.task_id,
                                           schedule.next_fire_time)
                    event = ScheduleUpdated(datetime.now(timezone.utc), schedule.id,
                                            schedule.next_fire_time)
            else:
                event = ScheduleAdded(datetime.now(timezone.utc), schedule.id,
                                      schedule.next_fire_time)

        if event:
            await self.publish(event)

        if self.notify_channel:
            await self.pool.execute(f"NOTIFY {self.notify_channel}, 'schedule'")

    async def remove_schedules(self, ids: Iterable[str]) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            now = datetime.now(timezone.utc)
            query = (f"DELETE FROM {self.schema}.schedules "
                     f"WHERE id = any($1::text[]) "
                     f"  AND (acquired_until IS NULL OR acquired_until < $2) "
                     f"RETURNING id")
            removed_ids = [row[0] for row in await conn.fetch(query, list(ids), now)]

        for schedule_id in removed_ids:
            await self.publish(ScheduleRemoved(now, schedule_id))

    async def get_schedules(self, ids: Optional[Set[str]] = None) -> List[Schedule]:
        query = f"SELECT serialized_data FROM {self.schema}.schedules"
        args = ()
        if ids:
            query += " WHERE id = any($1::text[])"
            args = (ids,)

        query += " ORDER BY id"
        records = await self.pool.fetch(query, *args)
        return [self.serializer.deserialize(r[0]) for r in records]

    async def acquire_schedules(self, scheduler_id: str, limit: int) -> List[Schedule]:
        while True:
            schedules: List[Schedule] = []
            async with self.pool.acquire() as conn, conn.transaction():
                acquired_until = datetime.fromtimestamp(
                    datetime.now(timezone.utc).timestamp() + self.lock_expiration_delay,
                    timezone.utc)
                records = await conn.fetch(f"""
                    WITH schedule_ids AS (
                        SELECT id FROM {self.schema}.schedules
                        WHERE next_fire_time IS NOT NULL AND next_fire_time <= $1
                            AND (acquired_until IS NULL OR $1 > acquired_until)
                        ORDER BY next_fire_time
                        FOR NO KEY UPDATE SKIP LOCKED
                        FETCH FIRST $2 ROWS ONLY
                    )
                    UPDATE {self.schema}.schedules SET acquired_by = $3, acquired_until = $4
                    WHERE id IN (SELECT id FROM schedule_ids)
                    RETURNING serialized_data
                    """, datetime.now(timezone.utc), limit, scheduler_id, acquired_until)

            for record in records:
                schedule = self.serializer.deserialize(record['serialized_data'])
                schedules.append(schedule)

            if schedules:
                return schedules

            async with move_on_after(self.max_poll_time):
                await self._schedules_event.wait()

    async def release_schedules(self, scheduler_id: str, schedules: List[Schedule]) -> None:
        update_events: List[ScheduleUpdated] = []
        finished_schedule_ids: List[str] = []
        async with self.pool.acquire() as conn, conn.transaction():
            update_args = []
            now = datetime.now(timezone.utc)
            for schedule in schedules:
                if schedule.next_fire_time is not None:
                    try:
                        serialized_data = self.serializer.serialize(schedule)
                    except SerializationError:
                        self._logger.exception('Error serializing schedule %r – '
                                               'removing from data store', schedule.id)
                        finished_schedule_ids.append(schedule.id)
                        continue

                    update_args.append((serialized_data, schedule.next_fire_time, schedule.id))
                    update_events.append(
                        ScheduleUpdated(now, schedule.id, schedule.next_fire_time))
                else:
                    finished_schedule_ids.append(schedule.id)

            # Update schedules that have a next fire time
            if update_args:
                await conn.executemany(
                    f"UPDATE {self.schema}.schedules SET serialized_data = $1, "
                    f"next_fire_time = $2, acquired_by = NULL, acquired_until = NULL "
                    f"WHERE id = $3 AND acquired_by = {scheduler_id!r}", update_args)

            # Remove schedules that have no next fire time or failed to serialize
            if finished_schedule_ids:
                await conn.execute(
                    f"DELETE FROM {self.schema}.schedules "
                    f"WHERE id = any($1::text[]) AND acquired_by = $2",
                    list(finished_schedule_ids), scheduler_id)

        for event in update_events:
            await self.publish(event)

        if update_events and self.notify_channel:
            await self.pool.execute(f"NOTIFY {self.notify_channel}, 'schedule'")

        for schedule_id in finished_schedule_ids:
            event = ScheduleRemoved(datetime.now(timezone.utc), schedule_id)
            await self.publish(event)

    async def add_job(self, job: Job) -> None:
        now = datetime.now(timezone.utc)
        query = (f"INSERT INTO {self.schema}.jobs (id, task_id, created_at, serialized_data, "
                 f"tags) VALUES($1, $2, $3, $4, $5)")
        await self.pool.execute(query, job.id, job.task_id, now, self.serializer.serialize(job),
                                job.tags)
        await self.publish(JobAdded(now, job.id, job.task_id, job.schedule_id))

        if self.notify_channel:
            await self.pool.execute(f"NOTIFY {self.notify_channel}, 'job'")

    async def get_jobs(self, ids: Optional[Iterable[UUID]] = None) -> List[Job]:
        query = f"SELECT serialized_data FROM {self.schema}.jobs"
        args = ()
        if ids:
            query += " WHERE id = any($1::uuid[])"
            args = (ids,)

        query += " ORDER BY id"
        records = await self.pool.fetch(query, *args)
        return [self.serializer.deserialize(r[0]) for r in records]

    async def acquire_jobs(self, worker_id: str, limit: Optional[int] = None) -> List[Job]:
        while True:
            print('acquiring jobs')
            jobs: List[Job] = []
            async with self.pool.acquire() as conn, conn.transaction():
                now = datetime.now(timezone.utc)
                acquired_until = datetime.fromtimestamp(
                    now.timestamp() + self.lock_expiration_delay, timezone.utc)
                records = await conn.fetch(f"""
                    WITH job_ids AS (
                        SELECT id FROM {self.schema}.jobs
                        WHERE acquired_until IS NULL OR acquired_until < $1
                        ORDER BY created_at
                        FOR NO KEY UPDATE SKIP LOCKED
                        FETCH FIRST $2 ROWS ONLY
                    )
                    UPDATE {self.schema}.jobs SET acquired_by = $3, acquired_until = $4
                    WHERE id IN (SELECT id FROM job_ids)
                    RETURNING serialized_data
                    """, now, limit, worker_id, acquired_until)

            for record in records:
                job = self.serializer.deserialize(record['serialized_data'])
                jobs.append(job)

            if jobs:
                return jobs

            async with move_on_after(self.max_poll_time):
                await self._jobs_event.wait()
                self._jobs_event.clear()

    async def release_jobs(self, worker_id: str, jobs: List[Job]) -> None:
        job_ids = {j.id for j in jobs}
        await self.pool.execute(
            f"DELETE FROM {self.schema}.jobs WHERE acquired_by = $1 AND id = any($2::uuid[])",
            worker_id, job_ids)
