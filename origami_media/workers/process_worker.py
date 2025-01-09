from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Set

from origami_media.dispatchers.event_processor import CommandPacket, Route

if TYPE_CHECKING:
    from maubot.matrix import MaubotMatrixClient
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.dispatchers.route_executer import RouteExecutor
    from origami_media.main import Config


class ProcessWorker:
    def __init__(
        self,
        log: "TraceLogger",
        config: "Config",
        client: "MaubotMatrixClient",
        initial_reaction_lock: asyncio.Lock,
        initial_reaction_tasks: Set,
        event_queue: asyncio.Queue,
        ROUTE_EXECUTION_TIMEOUT,
        route_executer: RouteExecutor,
    ):
        self.log = log
        self.config = config
        self.client = client
        self.initial_reaction_lock = initial_reaction_lock
        self.initial_reaction_tasks = initial_reaction_tasks
        self.event_queue = event_queue
        self.ROUTE_EXECUTION_TIMEOUT = ROUTE_EXECUTION_TIMEOUT
        self.route_executor = route_executer

    async def process(self) -> None:
        while True:
            try:
                packet: CommandPacket = await self.event_queue.get()

                if packet.reaction_id:
                    async with self.initial_reaction_lock:
                        self.initial_reaction_tasks.discard(packet.reaction_id)

                    await self.client.redact(
                        room_id=packet.event.room_id, event_id=packet.reaction_id
                    )
                packet.reaction_id = await packet.event.react("🔄")

                if packet.route == Route.URL:
                    try:
                        await asyncio.wait_for(
                            self.route_executor.execute_url_route(packet),
                            timeout=self.ROUTE_EXECUTION_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        self.log.warning("Timeout while executing url branch.")
                    except Exception as e:
                        self.log.error(f"Error during url branch execution: {e}")

                elif packet.route == Route.QUERY:
                    try:
                        await asyncio.wait_for(
                            self.route_executor.execute_query_route(packet),
                            timeout=self.ROUTE_EXECUTION_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        self.log.warning("Timeout while executing query branch.")
                    except Exception as e:
                        self.log.error(f"Error during query branch execution: {e}")

                elif packet.route == Route.DEBUG and self.config.meta.get(
                    "debug", False
                ):
                    try:
                        await asyncio.wait_for(
                            self.route_executor.execute_debug_route(packet),
                            timeout=self.ROUTE_EXECUTION_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        self.log.warning("Timeout while executing debug branch.")
                    except Exception as e:
                        self.log.error(f"Error during debug branch execution: {e}")

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
                if packet.reaction_id:
                    await self.client.redact(
                        room_id=packet.event.room_id, event_id=packet.reaction_id
                    )
                self.event_queue.task_done()
