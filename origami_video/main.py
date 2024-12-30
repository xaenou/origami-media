import asyncio
from typing import Any, Dict, Type, cast

from maubot.handlers import command, event
from maubot.matrix import MaubotMessageEvent
from maubot.plugin_base import Plugin
from mautrix.types.event import message, EventType
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from .dependency_handler import DependencyHandler
from .media_pipeline import MediaPipeline
from .url_handler import UrlHandler


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper):
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
    async def start(self):
        self.log.info(f"Starting Origami Video")
        await super().start()
        self.config.load_and_update()

        self.dependency_handler = DependencyHandler(log=self.log)
        self.url_handler = UrlHandler(log=self.log, config=self.config)
        self.media_pipeline = MediaPipeline(
            log=self.log, client=self.client, config=self.config
        )
        self.valid_urls = asyncio.Queue()
        self.event_queue = asyncio.Queue(
            maxsize=self.config.queue.get("max_size", 100)
        )

        self.workers = [
            asyncio.create_task(self._message_worker()),
            asyncio.create_task(self._pipeline_worker()),
        ]

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    @event.on(EventType.ROOM_MESSAGE)
    async def main(self, event: MaubotMessageEvent):
        if not self.config.meta.get("enable_passive", False):
            return
        if not event.content.msgtype.is_text or event.sender is self.client.mxid or event.content.body.startswith("!"):
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
                await self.media_pipeline.process(event=event, url=url)
            except asyncio.CancelledError:
                self.log.info("[Pipeline Worker] Shutting down gracefully.")
                break
            except Exception as e:
                self.log.error(f"[Pipeline Worker] Failed to process URL {url} in MediaPipeline: {e}")
            finally:
                self.valid_urls.task_done()

    async def stop(self) -> None:
        self.log.info("Shutting down workers...")
        for task in self.workers:
            task.cancel()
        
        results = await asyncio.gather(*self.workers, return_exceptions=True)
        for task, result in zip(self.workers, results):
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                self.log.error(f"Task {task.get_name()} failed during shutdown: {result}")
        
        self.log.info("All workers stopped cleanly.")
        await super().stop()

    @command.new(name="ov")
    async def ov(self, event: MaubotMessageEvent) -> None:
        if not self.config.meta.get("enable_active", False):
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
        content = message.TextMaubotMessageEventContent(
            msgtype=message.MessageType.NOTICE,
            format=message.Format.HTML,
            formatted_body=help_text,
            body=help_text,
        )

        await event.respond(content)
        return

    @ov.subcommand(name="dl", help="Downloads and posts a video")
    @command.argument(name="url", pass_raw=True)
    async def dl(self, event: MaubotMessageEvent, url: str) -> None:
        if not self.config.meta.get("enable_active", False):
            await event.respond("Active commands are currently disabled.")
            self.log.info("Active commands are disabled. Ignoring `dl` command.")
            return

        await self.media_pipeline.process(event=event, url=url)

    @ov.subcommand(name="check", help="Checks for dependencies.")
    async def check(self, event: MaubotMessageEvent) -> None:
        if not self.config.meta.get("enable_active", False):
            await event.respond("Active commands are currently disabled.")
            self.log.info("Active commands are disabled. Ignoring `check` command.")
            return
        
        await self.dependency_handler.run_all_checks(event=event)

    @ov.subcommand(name="debug")
    async def debug(self, event: MaubotMessageEvent):
        if self.config.meta.get("debug", False):
            try:
                room_id = event.room_id
                self.log.info(f"[DEBUG] Room ID: {room_id}")
                
                initial_event_id = event.event_id
                self.log.info(f"[DEBUG] Initial Event ID: {initial_event_id}")
                
                reaction_id = await event.react(key="🐛")
                self.log.info(f"[DEBUG] Reaction Event ID: {reaction_id}")
                
                reaction_event = await self.client.get_event(event_id=reaction_id, room_id=room_id)
                self.log.info(f"[DEBUG] Reaction Event Details: {reaction_event}")
                
                await self.client.redact(room_id=room_id, event_id=reaction_id)
                updated_reaction_event = await self.client.get_event(event_id=reaction_id, room_id=room_id)
                self.log.info(f"[DEBUG] Updated Reaction Event Details: {updated_reaction_event}")

            except Exception as e:
                self.log.error(f"[ERROR] Exception occurred in debug: {e}")




