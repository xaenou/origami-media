from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Set

from origami_media.models.command_models import CommandPacket

if TYPE_CHECKING:
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.handlers.command_handler import CommandHandler
    from origami_media.main import Config


class PreprocessWorker:
    def __init__(
        self,
        log: "TraceLogger",
        config: "Config",
        preprocess_lock: asyncio.Lock,
        preprocess_tasks: Set,
        event_queue: asyncio.Queue,
        command_handler: "CommandHandler",
    ):
        self.log = log
        self.config = config
        self.preprocess_lock = preprocess_lock
        self.preprocess_tasks = preprocess_tasks
        self.event_queue = event_queue
        self.command_handler = command_handler

    async def preprocess(self, packet: CommandPacket) -> None:
        self.preprocess_tasks.add(packet.event.event_id)
        allowed = await self._is_allowed()
        if not allowed:
            self.log.warning(
                f"Skipping preprocess task for {packet.event.event_id}: "
                f"Active preprocess task limit reached ({len(self.preprocess_tasks)}/"
                f"{self.config.queue.get('preprocess_worker_limit')}."
            )
            return
        try:
            preprocessed_packet = await self.command_handler.handle_preprocess(packet)
            if preprocessed_packet:
                self.event_queue.put_nowait(packet)
        except asyncio.QueueFull:
            self.log.warning("Message queue is full. Dropping incoming message.")
        except Exception as e:
            self.log.error(f"Unexpected error: {e}")
        finally:
            self.preprocess_tasks.discard(packet.event.event_id)

    async def _is_allowed(self) -> bool:
        async with self.preprocess_lock:
            if len(self.preprocess_tasks) > self.config.queue.get(
                "preprocess_worker_limit", 10
            ):
                return False
            else:
                return True
