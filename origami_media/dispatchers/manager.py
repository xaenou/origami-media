from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from origami_media.workers.preprocess_worker import PreprocessWorker
from origami_media.workers.process_worker import ProcessWorker

if TYPE_CHECKING:
    from maubot.matrix import MaubotMatrixClient
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.handlers.command_handler import CommandHandler
    from origami_media.main import Config
    from origami_media.models.command_models import CommandPacket


class Manager:
    def __init__(
        self,
        log: "TraceLogger",
        config: "Config",
        client: "MaubotMatrixClient",
        command_handler: "CommandHandler",
    ):
        self.log = log
        self.config = config
        self.client = client
        self.command_handler = command_handler

        self.ROUTE_EXECUTION_TIMEOUT = 180
        self.preprocess_tasks = set()
        self.preprocess_lock = asyncio.Lock()

        self.event_queue = asyncio.Queue(
            self.config.queue.get("event_queue_capacity", 10)
        )

        self.preprocess_worker = PreprocessWorker(
            log=self.log,
            config=self.config,
            preprocess_lock=self.preprocess_lock,
            preprocess_tasks=self.preprocess_tasks,
            command_handler=self.command_handler,
            event_queue=self.event_queue,
        )

        self._process_worker = ProcessWorker(
            log=self.log,
            config=self.config,
            client=self.client,
            event_queue=self.event_queue,
            ROUTE_EXECUTION_TIMEOUT=self.ROUTE_EXECUTION_TIMEOUT,
            command_handler=self.command_handler,
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

        async with self.preprocess_lock:
            self.preprocess_tasks.clear()
