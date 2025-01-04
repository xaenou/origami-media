import asyncio
from typing import Any, Dict, Type, cast

from maubot.handlers import command, event
from maubot.matrix import MaubotMessageEvent
from maubot.plugin_base import Plugin
from mautrix.types import EventType
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from .handlers.command_handler import CommandHandler
from .handlers.dependency_handler import DependencyHandler
from .handlers.display_handler import DisplayHandler
from .handlers.media_handler import MediaHandler
from .handlers.url_handler import UrlHandler


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
        self.event_queue = asyncio.Queue(maxsize=self.config.queue.get("max_size", 100))
        self.url_event_queue = asyncio.Queue()
        self.media_event_queue = asyncio.Queue()

        self.workers = [
            asyncio.create_task(self._url_worker()),
            asyncio.create_task(self._media_worker()),
            asyncio.create_task(self._display_worker()),
        ]

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    @cast(Any, event.on)(EventType.ROOM_MESSAGE)
    async def main(self, event: MaubotMessageEvent) -> None:
        if not self.config.meta.get("enable_passive_url_detection", False):
            return
        if (
            not event.content.msgtype.is_text
            or event.sender == self.client.mxid
            or cast(str, event.content.body).startswith("!")
        ):
            return
        if "http" not in event.content.body and "www" not in event.content.body:
            return
        try:
            self.event_queue.put_nowait(event)
            self.log.info("Event added to the processing queue.")
        except asyncio.QueueFull:
            self.log.warning("Message queue is full. Dropping incoming message.")

    async def _url_worker(self) -> None:
        while True:
            try:
                event = await self.event_queue.get()
                self.log.info(
                    f"[url Worker] Sending event to url_handler: {event.event_id}"
                )
                valid_urls, event = await self.url_handler.process(event)
                if valid_urls:
                    processed_url_event = (valid_urls, event)
                    self.log.info(
                        f"[url Worker] Extracted valid urls: {valid_urls} for {event.event_id}"
                    )
                    await self.url_event_queue.put(processed_url_event)
                else:
                    self.log.info(
                        f"[url Worker] No valid urls were found for {event.event_id}"
                    )
            except asyncio.CancelledError:
                self.log.info("[url Worker] Shutting down gracefully.")
                break
            except Exception as e:
                self.log.error(
                    f"[url Worker] Failed to extract urls for {event.event_id} in url_handler: {e}"
                )
            finally:
                self.event_queue.task_done()

    async def _media_worker(self) -> None:
        while True:
            try:
                processed_url_event = await self.url_event_queue.get()
                valid_urls, event = processed_url_event
                self.log.info(
                    f"[Media Worker] Sending valid urls to media_handler: {valid_urls} for {event.event_id}"
                )
                processed_media_list, event = await self.media_handler.process(
                    urls=valid_urls, event=event
                )
                if processed_media_list:
                    processed_media_event = (processed_media_list, event)
                    self.log.info(
                        f"[Media Worker] Uploaded media: {processed_media_list} for {event.event_id}"
                    )
                    await self.media_event_queue.put(processed_media_event)
                else:
                    self.log.info(
                        f"[url Worker] No media was processed for {event.event_id}"
                    )
            except asyncio.CancelledError:
                self.log.info("[Media Worker] Shutting down gracefully.")
                break
            except Exception as e:
                self.log.error(
                    f"[Media Worker] Failed to process media for {event.event_id} in media_handler: {e}"
                )
            finally:
                self.url_event_queue.task_done()

    async def _display_worker(self) -> None:
        while True:
            try:
                processed_media_event = await self.media_event_queue.get()
                processed_media_list, event = processed_media_event
                self.log.info(
                    f"[Display Worker] Sending processed_media_list to display_handler: {processed_media_list} for {event.event_id}"
                )
                await self.display_handler.render(
                    media=processed_media_list, event=event
                )
            except asyncio.CancelledError:
                self.log.info("[Display Worker] Shutting down gracefully.")
                break
            except Exception as e:
                self.log.error(
                    f"[Display Worker] Failed to render display for {event.event_id} in display_handler: {e}"
                )
            finally:
                self.media_event_queue.task_done()

    async def stop(self) -> None:
        self.log.info("Shutting down workers...")
        for task in self.workers:
            task.cancel()

        results = await asyncio.gather(*self.workers, return_exceptions=True)
        for task, result in zip(self.workers, results):
            if isinstance(result, Exception) and not isinstance(
                result, asyncio.CancelledError
            ):
                self.log.error(
                    f"Task {task.get_name()} failed during shutdown: {result}"
                )

        self.log.info("All workers stopped cleanly.")
        await super().stop()

    @command.new(
        name="om",
        require_subcommand=False,
        help="Pass in a url and it will return the media.",
    )
    @command.argument("url", pass_raw=True, required=False)
    async def om(self, event: MaubotMessageEvent, url: str) -> None:
        if not self.config.meta.get("enable_commands", False):
            return

        if not url:
            return

        urls, _ = self.url_handler.process_string(message=url, event=event)
        if not urls:
            return

        processed_media, _ = await self.media_handler.process(urls=urls, event=event)
        if not processed_media:
            return

        await self.display_handler.render(media=processed_media, event=event)

    @command.new(
        name="tenor",
        aliases=["gif", "g"],
        require_subcommand=False,
        help="Pass in a query and it will return a random gif. Aliases: 'gif', 'g'.",
    )
    @command.argument("query", pass_raw=True, required=False)
    async def tenor(self, event: MaubotMessageEvent, query: str) -> None:
        if not self.config.meta.get("enable_commands", False):
            return

        provider = "tenor"

        await self.command_handler.query_image_controller(
            event=event,
            query=query,
            provider=provider,
            media_handler=self.media_handler,
            display_handler=self.display_handler,
        )

    @command.new(
        name="unsplash",
        aliases=["img", "us"],
        require_subcommand=False,
        help="Pass in a query and it will return a random image.",
    )
    @command.argument("query", pass_raw=True, required=False)
    async def unsplash(self, event: MaubotMessageEvent, query: str) -> None:
        if not self.config.meta.get("enable_commands", False):
            return

        provider = "unsplash"

        await self.command_handler.query_image_controller(
            event=event,
            query=query,
            provider=provider,
            media_handler=self.media_handler,
            display_handler=self.display_handler,
        )

    @command.new(name="debug")
    @command.argument(name="url", pass_raw=True, required=False)
    async def debug(self, event: MaubotMessageEvent, url: str) -> None:
        if self.config.meta.get("debug", False) and self.config.meta.get(
            "enable_commands", False
        ):
            if not url:
                return
