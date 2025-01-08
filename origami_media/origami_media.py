import asyncio
from enum import Enum, auto
from typing import Any, Dict, Optional, Type, cast

from maubot.handlers import event
from maubot.matrix import MaubotMessageEvent
from maubot.plugin_base import Plugin
from mautrix.types import EventID, EventType
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from .handlers.dependency_handler import DependencyHandler
from .handlers.display_handler import DisplayHandler
from .handlers.media_handler import MediaHandler
from .handlers.query_handler import QueryHandler
from .handlers.url_handler import UrlHandler


class Branch(Enum):
    URL = auto()
    QUERY = auto()
    PRINT = auto()


class QueueItem:
    def __init__(
        self,
        branch: Branch,
        event: MaubotMessageEvent,
        args: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ):
        self.branch = branch
        self.event = event
        self.args = args or {}
        self.data = data or {}
        self.reaction_id: Optional[EventID] = None


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
        self.query_handler = QueryHandler(
            config=self.config, log=self.log, http=self.http
        )

        self.preprocess_worker_limit = self.config.queue.get(
            "preprocess_worker_limit", 10
        )
        self.initial_reaction_tasks = set()
        self.initial_reaction_lock = asyncio.Lock()

        self.event_queue = asyncio.Queue(
            self.config.queue.get("event_queue_capacity", 10)
        )

        self.BRANCH_EXECUTION_TIMEOUT = 180
        self.process_workers = [
            asyncio.create_task(self._process_worker(), name=f"worker_{i}")
            for i in range(self.config.queue.get("process_worker_count", 1))
        ]

        self.command_prefix = self.config.command.get("command_prefix", "!")

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    BASE_COMMANDS = {
        "help": {
            "type": "print",
            "content": "_help_message",
            "description": "Show this help message.",
        },
        "get": {
            "type": "url",
            "modifier": None,
            "description": "Download media from a url.",
        },
        "audio": {
            "type": "url",
            "modifier": "force_audio_only",
            "description": "Download audio only for a url.",
        },
        "tenor": {
            "type": "query",
            "api_provider": "tenor",
            "description": "Download gif by querying tenor.",
        },
        "unsplash": {
            "type": "query",
            "api_provider": "unsplash",
            "description": "Download image by querying unsplash.",
        },
        "lexica": {
            "type": "query",
            "api_provider": "lexica",
            "description": "Download an image by querying Lexica.",
        },
        "waifu": {
            "type": "query",
            "api_provider": "waifu",
            "description": "Roll for a random Waifu.",
        },
    }

    ALIASES = {
        "gif": "tenor",
        "img": "unsplash",
        "lex": "lexica",
        "girl": "waifu",
        "g": "waifu",
        "mp3": "audio",
    }

    @cast(Any, event.on)(EventType.ROOM_MESSAGE)
    async def main(self, event: MaubotMessageEvent) -> None:
        if not event.content.msgtype.is_text or event.sender == self.client.mxid:
            return

        item = self._handle_active(event)
        if item:
            asyncio.create_task(self._preprocess_worker(item))
            return

        item = self._handle_passive(event)
        if item:
            asyncio.create_task(self._preprocess_worker(item))
            return

    def _handle_passive(self, event: MaubotMessageEvent) -> Optional[QueueItem]:
        if not self.config.meta.get("enable_passive_url_detection", False):
            return

        if "http" not in event.content.body:
            return

        url_tuple = self.url_handler.process(event)
        if not url_tuple:
            return

        item = QueueItem(branch=Branch.URL, event=event, data={}, args={})
        item.data["url_tuple"] = url_tuple
        item.args["media_modifier"] = None

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

        command_map = {
            f"{self.command_prefix}{cmd}": details
            for cmd, details in self.BASE_COMMANDS.items()
        }
        command_map.update(
            {
                f"{self.command_prefix}{alias}": command_map[
                    f"{self.command_prefix}{target}"
                ]
                for alias, target in self.ALIASES.items()
            }
        )

        try:
            if command in command_map:
                command_info = command_map[command]

                if command_info["type"] == "url":
                    url_tuple = self.url_handler.process(event)
                    if not url_tuple:
                        return

                    item = QueueItem(branch=Branch.URL, event=event, data={}, args={})
                    item.data["url_tuple"] = url_tuple
                    item.args["media_modifier"] = command_info["modifier"]
                    return item

                elif command_info["type"] == "query":
                    item = QueueItem(branch=Branch.QUERY, event=event, data={}, args={})
                    item.args["query"] = argument
                    item.args["api_provider"] = command_info["api_provider"]
                    return item

                elif command_info["type"] == "print":
                    content_function = getattr(self, command_info["content"], None)
                    if callable(content_function):
                        content = content_function()
                    else:
                        content = str(command_info["content"])

                    item = QueueItem(branch=Branch.PRINT, event=event, data={}, args={})
                    item.data["content"] = content
                    return item

        except asyncio.QueueFull:
            self.log.warning("Message queue is full. Dropping incoming message.")

    async def _preprocess_worker(self, item: QueueItem) -> None:
        if item.branch == Branch.PRINT:
            await item.event.respond(item.data["content"])
            return

        async with self.initial_reaction_lock:
            if len(self.initial_reaction_tasks) >= self.config.queue.get(
                "preprocess_worker_limit", 10
            ):
                self.log.warning(
                    f"Skipping reaction for event {item.event.event_id}: "
                    f"Active reactions limit reached ({len(self.initial_reaction_tasks)}/"
                    f"{self.config.queue.get('preprocess_worker_limit')}."
                )
                return

            try:
                item.reaction_id = await item.event.react("⏳")
                self.initial_reaction_tasks.add(item.reaction_id)
                self.event_queue.put_nowait(item)
            except asyncio.QueueFull:
                self.log.warning("Message queue is full. Dropping incoming message.")
            except Exception as e:
                self.log.error(f"Failed to add reaction: {e}")

    async def _process_worker(self) -> None:
        while True:
            try:
                item: QueueItem = await self.event_queue.get()

                if item.reaction_id:
                    async with self.initial_reaction_lock:
                        self.initial_reaction_tasks.discard(item.reaction_id)

                    await self.client.redact(
                        room_id=item.event.room_id, event_id=item.reaction_id
                    )
                item.reaction_id = await item.event.react("🔄")

                if item.branch == Branch.URL:
                    try:
                        await asyncio.wait_for(
                            self._execute_url_branch(item),
                            timeout=self.BRANCH_EXECUTION_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        self.log.warning("Timeout while executing default branch.")
                    except Exception as e:
                        self.log.error(f"Error during default branch execution: {e}")

                elif item.branch == Branch.QUERY:
                    try:
                        await asyncio.wait_for(
                            self._execute_query_branch(item),
                            timeout=self.BRANCH_EXECUTION_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        self.log.warning("Timeout while executing query branch.")
                    except Exception as e:
                        self.log.error(f"Error during query branch execution: {e}")

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
                if item.reaction_id:
                    await self.client.redact(
                        room_id=item.event.room_id, event_id=item.reaction_id
                    )
                self.event_queue.task_done()

    async def _execute_url_branch(self, item: QueueItem):
        url_tuple = item.data["url_tuple"]
        valid_urls, sanitized_message, should_censor = url_tuple

        if should_censor:
            await self.url_handler.censor(
                sanitized_message=sanitized_message, event=item.event
            )

        processed_media = await self.media_handler.process(
            urls=valid_urls, modifier=item.args["media_modifier"]
        )

        if item.reaction_id:
            await self.client.redact(
                room_id=item.event.room_id, event_id=item.reaction_id
            )
            item.reaction_id = None

        await self.display_handler.render(media=processed_media, event=item.event)

    async def _execute_query_branch(self, item: QueueItem):
        url = await self.query_handler.query_image_controller(
            query=item.args["query"],
            provider=item.args["api_provider"],
        )

        valid_urls = self.url_handler.process_string(message=url)

        processed_media = await self.media_handler.process(
            urls=valid_urls,
        )

        if item.reaction_id:
            await self.client.redact(
                room_id=item.event.room_id, event_id=item.reaction_id
            )
            item.reaction_id = None

        await self.display_handler.render(
            media=processed_media, event=item.event, reply=False
        )

    async def stop(self) -> None:
        self.log.info("Stopping OrigamiMedia workers...")

        for task in self.process_workers:
            task.cancel()

        await asyncio.gather(*self.process_workers, return_exceptions=True)

        async with self.initial_reaction_lock:
            self.initial_reaction_tasks.clear()

        self.log.info("All workers stopped cleanly.")
        await super().stop()

    def _help_message(self) -> str:
        help_message = "**Available Commands:**\n"
        for cmd, details in self.BASE_COMMANDS.items():
            description = details.get("description", f"{cmd.capitalize()} command.")
            aliases = [alias for alias, target in self.ALIASES.items() if target == cmd]
            alias_text = f" (Aliases: {', '.join(aliases)})" if aliases else ""
            cmd_type = details.get("type")

            if cmd == "waifu":
                arg_text = ""
            elif cmd_type == "query":
                arg_text = "[query]"
            elif cmd_type == "url":
                arg_text = "[url]"
            else:
                arg_text = ""

            help_message += f"- `{self.command_prefix}{cmd} {arg_text}`: {description}{alias_text}\n"

        return help_message
