from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from origami_media.workers.preprocess_worker import PreprocessWorker
from origami_media.workers.process_worker import ProcessWorker

if TYPE_CHECKING:
    from maubot.matrix import MaubotMatrixClient
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.dispatchers.route_executer import RouteExecutor
    from origami_media.handlers.display_handler import DisplayHandler
    from origami_media.handlers.media_handler import MediaHandler
    from origami_media.handlers.query_handler import QueryHandler
    from origami_media.handlers.url_handler import UrlHandler
    from origami_media.main import Config
    from origami_media.models.command_models import CommandPacket


class Manager:
    def __init__(
        self,
        log: "TraceLogger",
        config: "Config",
        client: "MaubotMatrixClient",
        display_handler: "DisplayHandler",
        media_handler: "MediaHandler",
        query_handler: "QueryHandler",
        url_handler: "UrlHandler",
        route_executer: "RouteExecutor",
    ):
        self.log = log
        self.config = config
        self.client = client
        self.display_handler = display_handler
        self.media_handler = media_handler
        self.query_handler = query_handler
        self.url_handler = url_handler
        self.route_executer = route_executer

        self.ROUTE_EXECUTION_TIMEOUT = 180
        self.initial_reaction_tasks = set()
        self.initial_reaction_lock = asyncio.Lock()

        self.event_queue = asyncio.Queue(
            self.config.queue.get("event_queue_capacity", 10)
        )

        self.preprocess_worker = PreprocessWorker(
            log=self.log,
            config=self.config,
            initial_reaction_lock=self.initial_reaction_lock,
            initial_reaction_tasks=self.initial_reaction_tasks,
            event_queue=self.event_queue,
        )

        self._process_worker = ProcessWorker(
            log=self.log,
            config=self.config,
            client=self.client,
            initial_reaction_lock=self.initial_reaction_lock,
            initial_reaction_tasks=self.initial_reaction_tasks,
            event_queue=self.event_queue,
            ROUTE_EXECUTION_TIMEOUT=self.ROUTE_EXECUTION_TIMEOUT,
            route_executer=self.route_executer,
        )

    async def spawn_process_workers(self) -> None:
        self.process_workers = [
            asyncio.create_task(self._process_worker.process(), name=f"worker_{i}")
            for i in range(self.config.queue.get("process_worker_count", 1))
        ]

    def spawn_preprocess_worker(self, packet: CommandPacket) -> None:
        asyncio.create_task(self.preprocess_worker.preprocess(packet))

    async def stop(self) -> None:
        for task in self.process_workers:
            task.cancel()

        await asyncio.gather(*self.process_workers, return_exceptions=True)

        async with self.initial_reaction_lock:
            self.initial_reaction_tasks.clear()
