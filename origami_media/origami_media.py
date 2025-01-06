import asyncio
from enum import Enum, auto
from typing import Any, Dict, Optional, Type, cast

from maubot.handlers import event
from maubot.matrix import MaubotMessageEvent
from maubot.plugin_base import Plugin
from mautrix.types import EventType
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from .handlers.command_handler import CommandHandler
from .handlers.dependency_handler import DependencyHandler
from .handlers.display_handler import DisplayHandler
from .handlers.media_handler import MediaHandler
from .handlers.url_handler import UrlHandler


class Intent(Enum):
    DEFAULT = auto()
    QUERY = auto()
    AUDIO = auto()


class QueueItem:
    def __init__(
        self,
        intent: Intent,
        event: MaubotMessageEvent,
        data: Optional[Dict[str, Any]] = None,
    ):
        self.intent = intent
        self.event = event
        self.data = data or {}

    def update(self, intent: Intent, **data_updates):
        self.intent = intent
        self.data.update(data_updates)


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper):
        helper.copy("meta")
        helper.copy("whitelist")
        helper.copy("ytdlp")
        helper.copy("ffmpeg")
        helper.copy("file")
        helper.copy("queue")
        helper.copy("command")

    @property
    def meta(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("meta", {}))

    @property
    def whitelist(self) -> list[str]:
        return cast(list[str], self.get("whitelist", []))

    @property
    def ytdlp(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("ytdlp", {}))

    @property
    def ffmpeg(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("ffmpeg", {}))

    @property
    def file(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("file", {}))

    @property
    def queue(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("queue", {}))

    @property
    def command(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("command", {}))


class OrigamiMedia(Plugin):
    config: Config

    async def start(self):
        self.log.info(f"Starting Origami Media")
        await super().start()

        if not self.config:
            raise Exception("Config is not initialized")

        self.config.load_and_update()

        self.dependency_handler = DependencyHandler(log=self.log)
        self.url_handler = UrlHandler(log=self.log, config=self.config)
        self.media_handler = MediaHandler(
            log=self.log, config=self.config, client=self.client, http=self.http
        )
        self.display_handler = DisplayHandler(
            log=self.log, config=self.config, client=self.client
        )
        self.command_handler = CommandHandler(
            config=self.config, log=self.log, http=self.http
        )

        self.preprocessor_worker_limit = self.config.queue.get(
            "preprocessor_worker_limit", 10
        )
        self.initial_reaction_tasks = set()
        self.initial_reaction_lock = asyncio.Lock()

        self.event_queue = asyncio.Queue(
            self.config.queue.get("event_queue_capacity", 10)
        )

        self.process_workers = [
            asyncio.create_task(self._event_worker(), name=f"worker_{i}")
            for i in range(self.config.queue.get("processor_worker_count", 1))
        ]

        self.command_prefix = self.config.command.get("command_prefix", "!")

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    @cast(Any, event.on)(EventType.ROOM_MESSAGE)
    async def main(self, event: MaubotMessageEvent) -> None:
        if not event.content.msgtype.is_text or event.sender == self.client.mxid:
            return

        item = self._handle_active(event)
        if item:
            asyncio.create_task(self._enqueue_item(item))
            return

        item = self._handle_passive(event)
        if item:
            asyncio.create_task(self._enqueue_item(item))
            return

    def _handle_passive(self, event: MaubotMessageEvent) -> Optional[QueueItem]:
        if not self.config.meta.get("enable_passive_url_detection", False):
            return

        if "http" not in event.content.body:
            return

        result = self.url_handler.process(event)
        if not result:
            return

        item = QueueItem(intent=Intent.DEFAULT, event=event, data={"result": result})
        return item

    def _handle_active(self, event: MaubotMessageEvent) -> Optional[QueueItem]:
        if not cast(str, event.content.body).startswith(self.command_prefix):
            return

        item = self._command_controller(event=event)
        if not item:
            return

        return item

    def _command_controller(self, event: MaubotMessageEvent) -> Optional[QueueItem]:
        if not self.config.meta.get("enable_commands", False):
            return

        body = cast(str, event.content.body)
        parts = body.split(" ", 1)
        command = parts[0]
        argument = parts[1] if len(parts) > 1 else ""

        query_commands = {
            f"{self.command_prefix}tenor": "tenor",
            f"{self.command_prefix}gif": "tenor",
            f"{self.command_prefix}tr": "tenor",
            f"{self.command_prefix}unsplash": "unsplash",
            f"{self.command_prefix}img": "unsplash",
            f"{self.command_prefix}uh": "unsplash",
            f"{self.command_prefix}lexica": "lexica",
            f"{self.command_prefix}lex": "lexica",
            f"{self.command_prefix}la": "lexica",
            f"{self.command_prefix}girl": "waifu",
            f"{self.command_prefix}g": "waifu",
        }
        try:
            if command == f"{self.command_prefix}dl":
                item = QueueItem(intent=Intent.DEFAULT, event=event, data={})
                return item

            elif command in query_commands:
                provider = query_commands[command]
                item = QueueItem(intent=Intent.QUERY, event=event, data={})
                item.data["query"] = argument
                item.data["provider"] = provider
                return item

            elif command == f"{self.command_prefix}audio":
                return
        except asyncio.QueueFull:
            self.log.warning("Message queue is full. Dropping incoming message.")

    async def _enqueue_item(self, item: QueueItem) -> None:
        async with self.initial_reaction_lock:
            if len(self.initial_reaction_tasks) >= self.config.queue.get(
                "preprocessor_worker_limit", 10
            ):
                self.log.warning(
                    f"Skipping reaction for event {item.event.event_id}: "
                    f"Active reactions limit reached ({len(self.initial_reaction_tasks)}/"
                    f"{self.config.queue.get('preprocessor_worker_limit')}."
                )
                return

            try:
                hourglass_reaction_event_id = await item.event.react("⏳")
                item.data["active_reaction_id"] = hourglass_reaction_event_id
                self.initial_reaction_tasks.add(hourglass_reaction_event_id)
                self.event_queue.put_nowait(item)
            except asyncio.QueueFull:
                self.log.warning("Message queue is full. Dropping incoming message.")
            except Exception as e:
                self.log.error(f"Failed to add reaction: {e}")

    async def _event_worker(self) -> None:
        while True:
            try:
                item: QueueItem = await self.event_queue.get()
                reaction_id = item.data.get("active_reaction_id")

                if reaction_id:
                    async with self.initial_reaction_lock:
                        self.initial_reaction_tasks.discard(reaction_id)

                    try:
                        await self.client.redact(
                            room_id=item.event.room_id,
                            event_id=reaction_id,
                        )
                    except Exception as redact_error:
                        self.log.warning(
                            f"Failed to redact reaction {reaction_id}: {redact_error}"
                        )

                try:
                    loading_reaction_event_id = await item.event.react("🔄")
                    item.data["active_reaction_id"] = loading_reaction_event_id
                except Exception as reaction_error:
                    self.log.warning(
                        f"Failed to add '🔄' reaction for event {getattr(item.event, 'event_id', 'N/A')}: {reaction_error}"
                    )

                if item.intent == Intent.DEFAULT:
                    result = item.data["result"]
                    valid_urls, sanitized_message, should_censor = result
                    if should_censor:
                        await self.url_handler.censor(
                            sanitized_message=sanitized_message, event=item.event
                        )
                    processed_media = await self.media_handler.process(
                        urls=valid_urls, event=item.event
                    )
                    await self.display_handler.render(
                        media=processed_media, event=item.event
                    )

                elif item.intent == Intent.QUERY:
                    url = await self.command_handler.query_image_controller(
                        query=item.data["query"],
                        provider=item.data["provider"],
                    )
                    valid_urls = self.url_handler.process_string(message=url)
                    processed_media = await self.media_handler.process(
                        urls=valid_urls, event=item.event
                    )
                    await self.display_handler.render(
                        media=processed_media, event=item.event, reply=False
                    )

            except asyncio.CancelledError:
                self.log.info("[Worker] Shutting down gracefully.")
                break

            except Exception as e:
                self.log.error(
                    f"[Worker] Failed to process event (Room: {getattr(item.event, 'room_id', 'N/A')}, "
                    f"Event: {getattr(item.event, 'event_id', 'N/A')}) - {e}"
                )
            finally:
                reaction_id = item.data.get("active_reaction_id")
                if reaction_id:
                    await self.client.redact(
                        room_id=item.event.room_id, event_id=reaction_id
                    )
                self.event_queue.task_done()

    async def stop(self) -> None:
        self.log.info("Stopping OrigamiMedia workers...")

        for task in self.process_workers:
            task.cancel()

        await asyncio.gather(*self.process_workers, return_exceptions=True)

        async with self.initial_reaction_lock:
            self.initial_reaction_tasks.clear()

        self.log.info("All workers stopped cleanly.")
        await super().stop()
