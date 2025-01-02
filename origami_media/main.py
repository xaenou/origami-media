import asyncio
from typing import Any, Dict, Type, cast

from maubot.handlers import command, event
from maubot.matrix import MaubotMessageEvent
from maubot.plugin_base import Plugin
from mautrix.types import EventType
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from .dependency_handler import DependencyHandler
from .media_handler import MediaHandler
from .url_handler import UrlHandler


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper):
        helper.copy("meta")
        helper.copy("whitelist")
        helper.copy("ytdlp")
        helper.copy("ffmpeg")
        helper.copy("file")
        helper.copy("queue")

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
            log=self.log, client=self.client, config=self.config
        )
        self.valid_urls = asyncio.Queue()
        self.event_queue = asyncio.Queue(maxsize=self.config.queue.get("max_size", 100))

        self.workers = [
            asyncio.create_task(self._message_worker()),
            asyncio.create_task(self._pipeline_worker()),
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

    async def _message_worker(self) -> None:
        while True:
            try:
                event = await self.event_queue.get()
                valid_urls, event = await self.url_handler.process(event)
                if valid_urls:
                    for url in valid_urls:
                        await self.valid_urls.put((url, event))
                    self.log.info(f"[Message Worker] Stored valid URLs: {valid_urls}")
                else:
                    self.log.info("[Message Worker] No valid URLs found.")
            except asyncio.CancelledError:
                self.log.info("[Message Worker] Shutting down gracefully.")
                break
            except Exception as e:
                self.log.error(f"[Message Worker] Error: {e}")
            finally:
                self.event_queue.task_done()

    async def _pipeline_worker(self) -> None:
        while True:
            try:
                item = await self.valid_urls.get()
                url, event = item
                self.log.info(f"[Pipeline Worker] Sending URL to MediaPipeline: {url}")
                await self.media_handler.process(event=event, url=url)
            except asyncio.CancelledError:
                self.log.info("[Pipeline Worker] Shutting down gracefully.")
                break
            except Exception as e:
                self.log.error(
                    f"[Pipeline Worker] Failed to process URL {url} in MediaPipeline: {e}"
                )
            finally:
                self.valid_urls.task_done()

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

    @command.new(name="om")
    async def om(self, event: MaubotMessageEvent) -> None:
        if self.config.meta.get("enable_commands", False):
            return

    @om.subcommand(name="debug")
    @command.argument(name="url", pass_raw=True)
    async def debug(self, event: MaubotMessageEvent, url: str) -> None:
        if self.config.meta.get("debug", False) and self.config.meta.get(
            "enable_commands", False
        ):
            return
