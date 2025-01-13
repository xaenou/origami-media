from __future__ import annotations

import asyncio
from typing import Any, Dict, Type, cast

from maubot.handlers import event
from maubot.matrix import MaubotMessageEvent
from maubot.plugin_base import Plugin
from mautrix.types import EventType
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from origami_media.dispatchers import EventProcessor, Manager
from origami_media.handlers import (
    CommandHandler,
    DependencyHandler,
    DisplayHandler,
    MediaHandler,
    QueryHandler,
    UrlHandler,
)


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper):
        helper.copy("meta")
        helper.copy("file")
        helper.copy("queue")
        helper.copy("command")
        helper.copy("ytdlp")
        helper.copy("ffmpeg")
        helper.copy("platforms")
        helper.copy("platform_configs")

    @property
    def meta(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("meta", {}))

    @property
    def file(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("file", {}))

    @property
    def queue(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("queue", {}))

    @property
    def command(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("command", {}))

    @property
    def ytdlp(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("ytdlp", {}))

    @property
    def ffmpeg(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("ffmpeg", {}))

    @property
    def platforms(self) -> list:
        return cast(list, self.get("platforms", []))

    @property
    def platform_configs(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("platform_configs", {}))


class OrigamiMedia(Plugin):
    config: Config

    async def start(self):
        self.log.info(f"Starting Origami Media Bot")
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
        self.event_processor = EventProcessor(
            config=self.config, url_handler=self.url_handler
        )

        self.command_handler = CommandHandler(
            config=self.config,
            log=self.log,
            client=self.client,
            http=self.http,
            display_handler=self.display_handler,
            media_handler=self.media_handler,
            query_handler=self.query_handler,
            url_handler=self.url_handler,
        )

        self.worker_manager = Manager(
            config=self.config,
            log=self.log,
            client=self.client,
            command_handler=self.command_handler,
        )

        await self.worker_manager.spawn_process_workers()

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    @cast(Any, event.on)(EventType.ROOM_MESSAGE)
    async def main(self, event: MaubotMessageEvent) -> None:
        try:
            if not event.content.msgtype.is_text or event.sender == self.client.mxid:
                return

            packet = self.event_processor.handle_active(event)
            if packet:
                self.worker_manager.spawn_preprocess_worker(packet)
                return

            packet = self.event_processor.handle_passive(event)
            if packet:
                self.worker_manager.spawn_preprocess_worker(packet)
                return

        except Exception as e:
            self.log.error(f"Error occurred in main: {e}")

    async def stop(self) -> None:
        self.log.info("Stopping OrigamiMedia workers...")
        await self.worker_manager.stop()
        self.log.info("All workers stopped cleanly.")
        await super().stop()
