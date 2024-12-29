import asyncio
from typing import Any, Dict, Type, cast

from maubot.handlers import command
from maubot.matrix import MaubotMessageEvent
from maubot.plugin_base import Plugin
from mautrix.types.event import message
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from .dependency_handler import DependencyHandler
from .media_pipeline import MediaPipeline
from .url_handler import UrlHandler


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("whitelist")
        helper.copy("ytdlp")
        helper.copy("queue")
        helper.copy("meta")
        helper.copy("other")

    @property
    def ytdlp(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("ytdlp", {}))

    @property
    def whitelist(self) -> list[str]:
        return cast(list[str], self.get("whitelist", []))

    @property
    def meta(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("meta", {}))

    @property
    def queue(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("queue", {}))

    @property
    def other(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("other", {}))


class OrigamiVideo(Plugin):
    async def start(self) -> None:
        self.log.info(f"Starting Origami Video")
        await super().start()
        self.config.load_and_update()  # type: ignore

        self.dependency_handler = DependencyHandler(log=self.log)
        self.url_handler = UrlHandler(log=self.log, config=self.config)
        self.media_pipeline = MediaPipeline(
            log=self.log, client=self.client, config=self.config
        )
        self.valid_urls = []
        self.url_lock = asyncio.Lock()
        self.event_queue = asyncio.Queue(
            maxsize=self.config.queue.get("max_size", 100)  # type: ignore
        )

        self.workers = [
            asyncio.create_task(self._message_worker()),
            asyncio.create_task(self._pipeline_worker()),
        ]

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    @command.passive(r"https?:\/\/[^\s.,!?]+", case_insensitive=True, multiple=True)
    async def main(self, event: MaubotMessageEvent, val):
        self.log.info(event.content.body, val)
        if not self.config.meta.get("enable_passive", False):  # type: ignore
            self.log.info("Passive command is currently disabled. Ignoring message.")
            return
        try:
            self.event_queue.put_nowait(event)
            self.log.info("Event added to the processing queue.")
        except asyncio.QueueFull:
            self.log.warning("Message queue is full. Dropping incoming message.")

    async def _message_worker(self):
        while True:
            try:
                event = await self.event_queue.get()
                valid_urls, event = await self.url_handler.process(event)
                if valid_urls:
                    async with self.url_lock:
                        self.valid_urls.extend(
                            [{"url": url, "event": event} for url in valid_urls]
                        )
                    self.log.info(f"Extracted and stored valid URLs: {valid_urls}")
                else:
                    self.log.info("No valid URLs found in the queued message.")
            except asyncio.CancelledError:
                self.log.info("Message worker shutting down gracefully.")
                break
            except Exception as e:
                self.log.error(f"Error in message processing: {e}")
            finally:
                self.event_queue.task_done()

    async def _pipeline_worker(self):
        while True:
            await asyncio.sleep(0.1)
            async with self.url_lock:
                if self.valid_urls:
                    item = self.valid_urls.pop(0)
                    url = item["url"]
                    event = item["event"]
                else:
                    continue
            try:
                self.log.info(f"Sending URL to MediaPipeline: {url}")
                await self.media_pipeline.process(event=event, url=url)
            except asyncio.CancelledError:
                self.log.info("Message worker shutting down gracefully.")
                break
            except Exception as e:
                self.log.error(f"Failed to process URL {url} in MediaPipeline: {e}")

    async def stop(self) -> None:
        self.log.info("Shutting down workers...")
        for task in self.workers:
            task.cancel()
        try:
            await asyncio.gather(*self.workers, return_exceptions=True)
        except asyncio.CancelledError:
            self.log.info("Worker tasks were cancelled cleanly.")
        self.log.info("All workers stopped cleanly.")
        await super().stop()

    @command.new(name="ov", help="Help command.")
    async def ov(self, event: MaubotMessageEvent):
        if not self.config.meta.get("enable_active", False):  # type: ignore
            await event.respond("Active commands are currently disabled.")
            self.log.info("Active commands are disabled. Ignoring `dl` command.")
            return
        help_text = (
            "**Origami Video Commands**\n\n"
            "**Available commands:**\n"
            "• `!ov dl <url>` — Download and post a video from a URL\n"
            "   Example: `!ov dl https://example.com/video`\n\n"
            "• `!ov check` — Check if all required dependencies are installed\n"
            "   Example: `!ov check` "
        )
        content = message.TextMessageEventContent(
            msgtype=message.MessageType.NOTICE,
            format=message.Format.HTML,
            formatted_body=help_text,
            body=help_text,
        )

        await event.respond(content)
        self.log.info("OrigamiVideo.main: Help message sent successfully.")
        return

    @ov.subcommand(name="dl", help="Downloads and posts a video")
    @command.argument(name="url", pass_raw=True)
    async def dl(self, event: MaubotMessageEvent, url: str):
        if not self.config.meta.get("enable_active", False):  # type: ignore
            await event.respond("Active commands are currently disabled.")
            self.log.info("Active commands are disabled. Ignoring `dl` command.")
            return

        await self.media_pipeline.process(event=event, url=url)

    @ov.subcommand(name="check", help="Checks for dependencies.")
    async def check(self, event: MaubotMessageEvent):
        if not self.config.meta.enable_active.get("enable_active", False):  # type: ignore
            await event.respond("Active commands are currently disabled.")
            self.log.info("Active commands are disabled. Ignoring `check` command.")
            return

        await self.dependency_handler.run_all_checks(event=event)
