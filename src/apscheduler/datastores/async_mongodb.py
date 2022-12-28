from __future__ import annotations

import logging
import operator
from collections import defaultdict
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, ClassVar, Iterable, Type, Tuple
from uuid import UUID

import attrs
from attrs.validators import instance_of
from bson import CodecOptions, UuidRepresentation
from bson.codec_options import TypeEncoder, TypeRegistry
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase, AsyncIOMotorCollection
from pymongo import ASCENDING, DeleteOne, UpdateOne
from pymongo.errors import ConnectionFailure, DuplicateKeyError

from .base import BaseExternalDataStore
from .._enums import CoalescePolicy, ConflictPolicy, JobOutcome
from .._events import (
    DataStoreEvent,
    JobAcquired,
    JobAdded,
    ScheduleAdded,
    ScheduleRemoved,
    ScheduleUpdated,
    TaskAdded,
    TaskRemoved,
    TaskUpdated,
)
from .._exceptions import (
    ConflictingIdError,
    DeserializationError,
    SerializationError,
    TaskLookupError,
)
from .._structures import Job, JobResult, Schedule, Task
from ..abc import EventBroker


class CustomEncoder(TypeEncoder):
    def __init__(self, python_type: type, encoder: Callable):
        self._python_type = python_type
        self._encoder = encoder

    @property
    def python_type(self) -> type:
        return self._python_type

    def transform_python(self, value: Any) -> Any:
        return self._encoder(value)


@attrs.define(eq=False)
class AsyncMongoDBDataStore(BaseExternalDataStore):
    """
    Uses a MongoDB server to store data.

    When started, this data store creates the appropriate indexes on the given database
    if they're not already present.

    Operations are retried (in accordance to ``retry_settings``) when an operation
    raises :exc:`pymongo.errors.ConnectionFailure`.

    :param client: a Motor client
    :param database: name of the database to use
    """

    client: AsyncIOMotorClient = attrs.field(validator=instance_of(AsyncIOMotorClient))
    database: str = attrs.field(default="apscheduler", kw_only=True)

    _task_attrs: ClassVar[list[str]] = [field.name for field in attrs.fields(Task)]  # noqa
    _schedule_attrs: ClassVar[list[str]] = [
        field.name for field in attrs.fields(Schedule)  # noqa
    ]
    _job_attrs: ClassVar[list[str]] = [field.name for field in attrs.fields(Job)]  # noqa

    @property
    def _temporary_failure_exceptions(self) -> Tuple[Type[Exception], ...]:
        return ConnectionFailure,

    def __attrs_post_init__(self) -> None:
        type_registry = TypeRegistry(
            [
                CustomEncoder(timedelta, timedelta.total_seconds),
                CustomEncoder(ConflictPolicy, operator.attrgetter("name")),
                CustomEncoder(CoalescePolicy, operator.attrgetter("name")),
                CustomEncoder(JobOutcome, operator.attrgetter("name")),
            ]
        )
        codec_options = CodecOptions(
            tz_aware=True,
            type_registry=type_registry,
            uuid_representation=UuidRepresentation.STANDARD,
        )
        database: AsyncIOMotorDatabase = self.client.get_database(self.database, codec_options=codec_options)
        self._tasks: AsyncIOMotorCollection = database["tasks"]
        self._schedules: AsyncIOMotorCollection = database["schedules"]
        self._jobs: AsyncIOMotorCollection = database["jobs"]
        self._jobs_results: AsyncIOMotorCollection = database["job_results"]

    @classmethod
    def from_url(cls, uri: str, **options) -> AsyncMongoDBDataStore:
        client = AsyncIOMotorClient(uri)
        return cls(client, **options)  # noqa

    async def _initialize(self) -> None:
        async with await self.client.start_session() as session:
            if self.start_from_scratch:
                await self._tasks.delete_many({}, session=session)
                await self._schedules.delete_many({}, session=session)
                await self._jobs.delete_many({}, session=session)
                await self._jobs_results.delete_many({}, session=session)

            await self._schedules.create_index("next_fire_time", session=session)
            await self._jobs.create_index("task_id", session=session)
            await self._jobs.create_index("created_at", session=session)
            await self._jobs.create_index("tags", session=session)
            await self._jobs_results.create_index("finished_at", session=session)
            await self._jobs_results.create_index("expires_at", session=session)

    async def start(
            self, exit_stack: AsyncExitStack, event_broker: EventBroker
    ) -> None:
        await super().start(exit_stack, event_broker)
        server_info = await self.client.server_info()
        if server_info["versionArray"] < [4, 0]:
            raise RuntimeError(
                f"MongoDB server must be at least v4.0; current version = "
                f"{server_info['version']}"
            )

        async for attempt in self._retry():
            with attempt:
                await self._initialize()

    async def add_task(self, task: Task) -> None:
        async for attempt in self._retry():
            with attempt:
                previous = await self._tasks.find_one_and_update(
                    {"_id": task.id},
                    {
                        "$set": task.marshal(self.serializer),
                        "$setOnInsert": {"running_jobs": 0},
                    },
                    upsert=True,
                )

        if previous:
            await self._event_broker.publish(TaskUpdated(task_id=task.id))
        else:
            await self._event_broker.publish(TaskAdded(task_id=task.id))

    async def remove_task(self, task_id: str) -> None:
        async for attempt in self._retry():
            with attempt:
                if not await self._tasks.find_one_and_delete({"_id": task_id}):
                    raise TaskLookupError(task_id)

        await self._event_broker.publish(TaskRemoved(task_id=task_id))

    async def get_task(self, task_id: str) -> Task:
        async for attempt in self._retry():
            with attempt:
                document: dict = await self._tasks.find_one(
                    {"_id": task_id},
                    projection=self._task_attrs
                )

        if not document:
            raise TaskLookupError(task_id)

        document["id"] = document.pop("_id")
        task = Task.unmarshal(self.serializer, document)
        return task

    async def get_tasks(self) -> list[Task]:
        async for attempt in self._retry():
            with attempt:
                tasks: list[Task] = []
                async for document in self._tasks.find(
                        projection=self._task_attrs, sort=[("_id", ASCENDING)]
                ):
                    document["id"] = document.pop("_id")
                    tasks.append(Task.unmarshal(self.serializer, document))

        return tasks

    async def get_schedules(self, ids: set[str] | None = None) -> list[Schedule]:
        filters = {"_id": {"$in": list(ids)}} if ids is not None else {}
        async for attempt in self._retry():
            with attempt:
                schedules: list[Schedule] = []
                async for document in self._schedules.find(filters).sort("_id"):
                    document["id"] = document.pop("_id")
                    try:
                        schedule = Schedule.unmarshal(self.serializer, document)
                    except DeserializationError:
                        self._logger.warning(
                            "Failed to deserialize schedule %r", document["_id"]
                        )
                        continue

                    schedules.append(schedule)

        return schedules

    async def add_schedule(
            self, schedule: Schedule, conflict_policy: ConflictPolicy
    ) -> None:
        event: DataStoreEvent
        document = schedule.marshal(self.serializer)
        document["_id"] = document.pop("id")
        try:
            async for attempt in self._retry():
                with attempt:
                    await self._schedules.insert_one(document)
        except DuplicateKeyError:
            if conflict_policy is ConflictPolicy.exception:
                raise ConflictingIdError(schedule.id) from None
            if conflict_policy is ConflictPolicy.replace:
                async for attempt in self._retry():
                    with attempt:
                        await self._schedules.replace_one(
                            {"_id": schedule.id}, document, True
                        )

                event = ScheduleUpdated(
                    schedule_id=schedule.id, next_fire_time=schedule.next_fire_time
                )
                await self._event_broker.publish(event)
        else:
            event = ScheduleAdded(
                schedule_id=schedule.id, next_fire_time=schedule.next_fire_time
            )
            await self._event_broker.publish(event)

    async def remove_schedules(self, ids: Iterable[str]) -> None:
        filters = {"_id": {"$in": list(ids)}} if ids is not None else {}
        async for attempt in self._retry():
            with attempt:
                async with await self.client.start_session() as session:
                    ids = []
                    async for doc in self._schedules.find(filters, projection=["_id"], session=session):
                        ids.append(doc["_id"])
                    if ids:
                        await self._schedules.delete_many(filters, session=session)

        for schedule_id in ids:
            await self._event_broker.publish(ScheduleRemoved(schedule_id=schedule_id))

    async def acquire_schedules(self, scheduler_id: str, limit: int) -> list[Schedule]:
        async for attempt in self._retry():
            with attempt:
                async with await self.client.start_session() as session:
                    schedules: list[Schedule] = []
                    cursor = (
                        self._schedules.find(
                            {
                                "$and": [
                                    {"next_fire_time": {"$ne": None}},
                                    {"next_fire_time": {"$lt": datetime.now(timezone.utc)}}
                                ],
                                "$or": [
                                    {"acquired_until": {"$exists": False}},
                                    {"acquired_until": {"$lt": datetime.now(timezone.utc)}},
                                ],
                            },
                            session=session,
                        )
                        .sort("next_fire_time")
                        .limit(limit)
                    )
                    async for document in cursor:
                        document["id"] = document.pop("_id")
                        schedule = Schedule.unmarshal(self.serializer, document)
                        schedules.append(schedule)

                    if schedules:
                        now = datetime.now(timezone.utc)
                        acquired_until = datetime.fromtimestamp(
                            now.timestamp() + self.lock_expiration_delay, now.tzinfo
                        )
                        filters = {"_id": {"$in": [schedule.id for schedule in schedules]}}
                        update = {
                            "$set": {
                                "acquired_by": scheduler_id,
                                "acquired_until": acquired_until,
                            }
                        }
                        await self._schedules.update_many(filters, update, session=session)

        return schedules

    async def release_schedules(
            self, scheduler_id: str, schedules: list[Schedule]
    ) -> None:
        updated_schedules: list[tuple[str, datetime]] = []
        finished_schedule_ids: list[str] = []

        # Update schedules that have a next fire time
        requests = []
        for schedule in schedules:
            filters = {"_id": schedule.id, "acquired_by": scheduler_id}
            if schedule.next_fire_time is not None:
                try:
                    serialized_trigger = self.serializer.serialize(schedule.trigger)
                except SerializationError:
                    self._logger.exception(
                        "Error serializing schedule %r – removing from data store",
                        schedule.id,
                    )
                    requests.append(DeleteOne(filters))
                    finished_schedule_ids.append(schedule.id)
                    continue

                update = {
                    "$unset": {
                        "acquired_by": True,
                        "acquired_until": True,
                    },
                    "$set": {
                        "trigger": serialized_trigger,
                        "next_fire_time": schedule.next_fire_time,
                    },
                }
                requests.append(UpdateOne(filters, update))
                updated_schedules.append((schedule.id, schedule.next_fire_time))
            else:
                requests.append(DeleteOne(filters))
                finished_schedule_ids.append(schedule.id)

            if requests:
                async for attempt in self._retry():
                    with attempt:
                        async with await self.client.start_session() as session:
                            await self._schedules.bulk_write(
                                requests, ordered=False, session=session
                            )

        for schedule_id, next_fire_time in updated_schedules:
            event = ScheduleUpdated(
                schedule_id=schedule_id, next_fire_time=next_fire_time
            )
            await self._event_broker.publish(event)

        for schedule_id in finished_schedule_ids:
            await self._event_broker.publish(ScheduleRemoved(schedule_id=schedule_id))

    async def get_next_schedule_run_time(self) -> datetime | None:
        async for attempt in self._retry():
            with attempt:
                document = await self._schedules.find_one(
                    {"next_fire_time": {"$ne": None}},
                    projection=["next_fire_time"],
                    sort=[("next_fire_time", ASCENDING)],
                )

        if document:
            return document["next_fire_time"]
        else:
            return None

    async def add_job(self, job: Job) -> None:
        document = job.marshal(self.serializer)
        document["_id"] = document.pop("id")
        async for attempt in self._retry():
            with attempt:
                await self._jobs.insert_one(document)

        event = JobAdded(
            job_id=job.id,
            task_id=job.task_id,
            schedule_id=job.schedule_id,
            tags=job.tags,
        )
        await self._event_broker.publish(event)

    async def get_jobs(self, ids: Iterable[UUID] | None = None) -> list[Job]:
        filters = {"_id": {"$in": list(ids)}} if ids is not None else {}
        async for attempt in self._retry():
            with attempt:
                jobs: list[Job] = []
                async for document in self._jobs.find(filters).sort("_id"):
                    document["id"] = document.pop("_id")
                    try:
                        job = Job.unmarshal(self.serializer, document)
                    except DeserializationError:
                        self._logger.warning(
                            "Failed to deserialize job %r", document["id"]
                        )
                        continue

                    jobs.append(job)

        return jobs

    async def acquire_jobs(self,
                           worker_id: str,
                           limit: int | None = None,
                           ignored_tasks: list | None = None) -> list[Job]:
        async for attempt in self._retry():
            with attempt:
                async with await self.client.start_session() as session:
                    query = {
                        "$or": [
                            {"acquired_until": {"$exists": False}},
                            {"acquired_until": {"$lt": datetime.now(timezone.utc)}},
                        ]
                    }
                    if ignored_tasks is not None:
                        query = {"$and": [query, {"task_id": {"$nin": ignored_tasks}}]}
                    documents = []
                    async for doc in self._jobs.find(query,
                                                     sort=[("created_at", ASCENDING)],
                                                     limit=limit,
                                                     session=session):
                        documents.append(doc)

                    # Retrieve the limits
                    task_ids: set[str] = {document["task_id"] for document in documents}
                    task_limits = self._tasks.find(
                        {"_id": {"$in": list(task_ids)}, "max_running_jobs": {"$ne": None}},
                        projection=["max_running_jobs", "running_jobs"],
                        session=session,
                    )
                    job_slots_left = {}
                    async for doc in task_limits:
                        job_slots_left[doc["_id"]] = doc["max_running_jobs"] - doc["running_jobs"]

                    # Filter out jobs that don't have free slots
                    acquired_jobs: list[Job] = []
                    increments: dict[str, int] = defaultdict(lambda: 0)
                    for document in documents:
                        document["id"] = document.pop("_id")
                        job = Job.unmarshal(self.serializer, document)

                        # Don't acquire the job if there are no free slots left
                        slots_left = job_slots_left.get(job.task_id)
                        if slots_left == 0:
                            continue
                        if slots_left is not None:
                            job_slots_left[job.task_id] -= 1

                        acquired_jobs.append(job)
                        increments[job.task_id] += 1

                    if acquired_jobs:
                        now = datetime.now(timezone.utc)
                        for job in acquired_jobs:
                            lock_expiration_delay = job.lock_expiration_delay or self.lock_expiration_delay
                            acquired_until = datetime.fromtimestamp(
                                now.timestamp() + lock_expiration_delay, timezone.utc
                            )
                            started_at = datetime.fromtimestamp(now.timestamp(), timezone.utc)
                            await self._jobs.find_one_and_update(
                                {"_id": job.id},
                                {"$set": {"acquired_by": worker_id,
                                          "acquired_until": acquired_until,
                                          "started_at": started_at}},
                                session=session)

                        # Increment the running job counters on each task
                        for task_id, increment in increments.items():
                            await self._tasks.find_one_and_update(
                                {"_id": task_id},
                                {"$inc": {"running_jobs": increment}},
                                session=session,
                            )

        # Publish the appropriate events
        for job in acquired_jobs:
            await self._event_broker.publish(
                JobAcquired(job_id=job.id, worker_id=worker_id)
            )

        return acquired_jobs

    async def release_job(
            self, worker_id: str, task_id: str, result: JobResult
    ) -> None:
        async for attempt in self._retry():
            with attempt:
                async with await self.client.start_session() as session:
                    # Record the job result
                    if result.expires_at > result.finished_at:
                        document = result.marshal(self.serializer)
                        document["_id"] = document.pop("job_id")
                        try:
                            await self._jobs_results.insert_one(document, session=session)
                        except DuplicateKeyError as exc:
                            logging.exception("Could not insert document %s into jobs_result collection "
                                              "as a document with the same id was already present",
                                              document, exc_info=exc)

                    # Delete the job
                    delete_result = await self._jobs.delete_one({"_id": result.job_id}, session=session)
                    if delete_result.deleted_count > 0:
                        # Decrement the running jobs counter if job could be deleted
                        await self._tasks.find_one_and_update(
                            {"_id": task_id},
                            {"$inc": {"running_jobs": -delete_result.deleted_count}},
                            session=session
                        )
                    else:
                        logging.error("Could not delete job with id %s, as it was not "
                                      "found in jobs collection", result.job_id)

    async def get_job_result(self, job_id: UUID) -> JobResult | None:
        async for attempt in self._retry():
            with attempt:
                document: dict = await self._jobs_results.find_one_and_delete({"_id": job_id})

        if document:
            document["job_id"] = document.pop("_id")
            return JobResult.unmarshal(self.serializer, document)
        return None