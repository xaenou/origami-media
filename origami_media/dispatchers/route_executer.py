from __future__ import annotations

from typing import TYPE_CHECKING

from origami_media.dispatchers.event_processor import CommandPacket

if TYPE_CHECKING:
    from maubot.matrix import MaubotMatrixClient
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.handlers.display_handler import DisplayHandler
    from origami_media.handlers.media_handler import MediaHandler
    from origami_media.handlers.query_handler import QueryHandler
    from origami_media.handlers.url_handler import UrlHandler
    from origami_media.main import Config


class RouteExecutor:
    def __init__(
        self,
        log: "TraceLogger",
        config: "Config",
        client: "MaubotMatrixClient",
        display_handler: "DisplayHandler",
        media_handler: "MediaHandler",
        query_handler: "QueryHandler",
        url_handler: "UrlHandler",
    ):
        self.log = log
        self.config = config
        self.client = client
        self.display_handler = display_handler
        self.media_handler = media_handler
        self.query_handler = query_handler
        self.url_handler = url_handler

    async def execute_url_route(self, packet: CommandPacket) -> None:
        url_tuple = packet.data["url_tuple"]
        valid_urls, sanitized_message, should_censor = url_tuple

        if should_censor:
            await self.url_handler.censor(
                sanitized_message=sanitized_message, event=packet.event
            )

        processed_media = await self.media_handler.process(
            urls=valid_urls, modifier=packet.args["media_modifier"]
        )

        if packet.reaction_id:
            await self.client.redact(
                room_id=packet.event.room_id, event_id=packet.reaction_id
            )
            packet.reaction_id = None

        await self.display_handler.render(media=processed_media, event=packet.event)

    async def execute_query_route(self, packet: CommandPacket) -> None:
        url = await self.query_handler.query_image_controller(
            query=packet.args["query"],
            provider=packet.args["api_provider"],
        )

        valid_urls = self.url_handler.process_string(message=url)

        processed_media = await self.media_handler.process(
            urls=valid_urls,
        )

        if packet.reaction_id:
            await self.client.redact(
                room_id=packet.event.room_id, event_id=packet.reaction_id
            )
            packet.reaction_id = None

        await self.display_handler.render(
            media=processed_media, event=packet.event, reply=False
        )

    async def execute_debug_route(self, packet: CommandPacket) -> None:
        return
