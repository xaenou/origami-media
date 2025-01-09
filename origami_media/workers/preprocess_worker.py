from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Set

from origami_media.dispatchers.event_processor import CommandPacket, Route

if TYPE_CHECKING:
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.main import Config


class PreprocessWorker:
    def __init__(
        self,
        log: "TraceLogger",
        config: "Config",
        initial_reaction_lock: asyncio.Lock,
        initial_reaction_tasks: Set,
        event_queue: asyncio.Queue,
    ):
        self.log = log
        self.config = config
        self.initial_reaction_lock = initial_reaction_lock
        self.initial_reaction_tasks = initial_reaction_tasks
        self.event_queue = event_queue

    async def preprocess(self, packet: CommandPacket) -> None:
        self.log.info("Preprocess running.")
        if packet.route == Route.PRINT:
            self.log.info("Print route initiated")
            self.log.info(packet.event.event_id)
            await packet.event.respond(packet.data["content"])
            self.log.info("Print response sent")
            return

        async with self.initial_reaction_lock:
            if len(self.initial_reaction_tasks) >= self.config.queue.get(
                "preprocess_worker_limit", 10
            ):
                self.log.warning(
                    f"Skipping reaction for event {packet.event.event_id}: "
                    f"Active reactions limit reached ({len(self.initial_reaction_tasks)}/"
                    f"{self.config.queue.get('preprocess_worker_limit')}."
                )
                return

            try:
                packet.reaction_id = await packet.event.react("⏳")
                self.initial_reaction_tasks.add(packet.reaction_id)
                self.event_queue.put_nowait(packet)
            except asyncio.QueueFull:
                self.log.warning("Message queue is full. Dropping incoming message.")
            except Exception as e:
                self.log.error(f"Failed to add reaction: {e}")
