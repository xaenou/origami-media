from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from origami_media.models.command_models import CommandPacket

if TYPE_CHECKING:
    from maubot.matrix import MaubotMatrixClient
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.handlers.command_handler import CommandHandler
    from origami_media.main import Config


class ProcessWorker:
    def __init__(
        self,
        log: "TraceLogger",
        config: "Config",
        client: "MaubotMatrixClient",
        event_queue: asyncio.Queue,
        ROUTE_EXECUTION_TIMEOUT,
        command_handler: "CommandHandler",
    ):
        self.log = log
        self.config = config
        self.client = client
        self.event_queue = event_queue
        self.ROUTE_EXECUTION_TIMEOUT = ROUTE_EXECUTION_TIMEOUT
        self.command_handler = command_handler

    async def process(self) -> None:
        while True:
            try:
                packet: CommandPacket = await self.event_queue.get()
                try:
                    await asyncio.wait_for(
                        self.command_handler.handle_process(packet),
                        timeout=self.ROUTE_EXECUTION_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    self.log.warning("Timeout while executing command execution.")
                except Exception as e:
                    self.log.error(f"Error during command execution: {e}")

            except asyncio.TimeoutError:
                self.log.warning(
                    "Worker timed out waiting for new event. Continuing..."
                )

            except asyncio.CancelledError:
                self.log.info("[Worker] Shutting down gracefully.")
                raise

            except Exception as e:
                self.log.error(f"[Worker] Unexpected error: {e}")

            finally:
                self.event_queue.task_done()
