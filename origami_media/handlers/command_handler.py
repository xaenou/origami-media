from __future__ import annotations

from functools import wraps
from typing import TYPE_CHECKING, Optional

from origami_media.models.command_models import (
    ALIASES,
    BASE_COMMANDS,
    CommandPacket,
    CommandType,
)
from origami_media.services.native import Native

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from maubot.matrix import MaubotMatrixClient
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.handlers.display_handler import DisplayHandler
    from origami_media.handlers.media_handler import MediaHandler
    from origami_media.handlers.query_handler import QueryHandler
    from origami_media.handlers.url_handler import UrlHandler
    from origami_media.main import Config


# Command methods are defined here.


class CommandHandler:
    def __init__(
        self,
        log: "TraceLogger",
        config: "Config",
        client: "MaubotMatrixClient",
        http: "ClientSession",
        display_handler: "DisplayHandler",
        media_handler: "MediaHandler",
        query_handler: "QueryHandler",
        url_handler: "UrlHandler",
    ):
        self.log = log
        self.config = config
        self.client = client
        self.http = http
        self.display_handler = display_handler
        self.media_handler = media_handler
        self.query_handler = query_handler
        self.url_handler = url_handler
        self.native_controller = Native(self.config, self.log, self.http)

    @staticmethod
    def ensure_reaction_cleanup(method):
        @wraps(method)
        async def wrapper(self, packet: CommandPacket, *args, **kwargs):
            try:
                if packet.reaction_id:
                    await self.client.redact(
                        room_id=packet.event.room_id, event_id=packet.reaction_id
                    )
                return await method(self, packet, *args, **kwargs)
            finally:
                if packet.reaction_id:
                    await self.client.redact(
                        room_id=packet.event.room_id, event_id=packet.reaction_id
                    )
                    packet.reaction_id = None

        return wrapper

    @ensure_reaction_cleanup
    async def handle_process(self, packet: CommandPacket) -> None:
        if packet.command.type == CommandType.URL:
            await self._process_url(packet)

        if packet.command.type == CommandType.QUERY:
            await self._process_query(packet)

    async def handle_preprocess(self, packet: CommandPacket) -> Optional[CommandPacket]:
        if packet.command.type == CommandType.URL:
            preprocessed_packet = await self._preprocess_url(packet)

        if packet.command.type == CommandType.QUERY:
            preprocessed_packet = await self._preprocess_query(packet)

        if packet.command.type == CommandType.PRINT:
            preprocessed_packet = await self._preprocess_print(packet)

        if packet.command.type == CommandType.DEBUG:
            preprocessed_packet = await self._preprocess_debug(packet)

        return preprocessed_packet

    async def _preprocess_url(self, packet: CommandPacket) -> Optional[CommandPacket]:
        result = self.url_handler.process(packet.event)
        if not result:
            return None

        valid_urls, sanitized_message, should_censor, exceeds_url_limit = result

        if should_censor:
            new_message_event_id = await self.display_handler.censor(
                sanitized_message=sanitized_message, event=packet.event
            )
            packet.event.event_id = new_message_event_id

        if exceeds_url_limit:
            return None

        packet.data["valid_urls"] = valid_urls
        packet.reaction_id = await packet.event.react("⏳")
        return packet

    async def _preprocess_query(self, packet: CommandPacket) -> CommandPacket:
        packet.reaction_id = await packet.event.react("⏳")
        return packet

    async def _preprocess_print(self, packet: CommandPacket) -> None:
        if packet.command.name == "help":
            help_message = "**Available Commands:**\n"
            for command, details in BASE_COMMANDS.items():
                if details.type == CommandType.DEBUG and not self.config.meta.get(
                    "debug"
                ):
                    continue

                if command == "get" and self.config.meta.get(
                    "enable_passive_url_detection"
                ):
                    continue

                description = details.description
                aliases = [
                    alias for alias, target in ALIASES.items() if target == command
                ]
                if aliases:
                    command_prefix = self.config.command.get("command_prefix")
                    alias_text = f" (Aliases: {', '.join(f'`{command_prefix}{alias}`' for alias in aliases)})"
                else:
                    alias_text = ""

                if command == "waifu":
                    arg_text = ""
                elif details.type == CommandType.QUERY:
                    arg_text = "[query]"
                elif details.type == CommandType.URL:
                    arg_text = "[url]"
                elif details.type == CommandType.DEBUG:
                    arg_text = "[DEBUG]"
                else:
                    arg_text = ""

                help_message += f"- `{self.config.command.get('command_prefix')}{command} {arg_text}`: {description}{alias_text}\n"

            _ = await self.display_handler.render_text(
                message_=help_message, event=packet.event
            )

    async def _preprocess_debug(self, packet: CommandPacket) -> None:
        if not self.config.meta.get("debug"):
            return

    async def _process_url(self, packet: CommandPacket) -> None:
        packet.reaction_id = await packet.event.react("🔄")

        valid_urls = packet.data["valid_urls"]

        media_requests = await self.media_handler.preprocess(
            valid_urls, modifier=packet.command.modifier
        )
        if not media_requests:
            return None

        processed_media = await self.media_handler.process(requests=media_requests)

        if packet.reaction_id:
            await self.client.redact(
                room_id=packet.event.room_id, event_id=packet.reaction_id
            )
            packet.reaction_id = None

        await self.display_handler.render_media(
            media=processed_media, event=packet.event
        )

    async def _process_query(self, packet: CommandPacket) -> None:
        packet.reaction_id = await packet.event.react("🔄")

        api_provider = packet.command.modifier or ""
        url = await self.query_handler.query_image_controller(
            query=packet.user_args,
            provider=api_provider,
        )

        valid_urls = self.url_handler.process_query_url_string(message=url)
        media_requests = await self.media_handler.preprocess(
            valid_urls, query_derived=True
        )

        processed_media = await self.media_handler.process(requests=media_requests)

        if packet.reaction_id:
            await self.client.redact(
                room_id=packet.event.room_id, event_id=packet.reaction_id
            )
            packet.reaction_id = None

        await self.display_handler.render_media(
            media=processed_media, event=packet.event, reply=False
        )
