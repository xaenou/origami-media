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

                description = details.description
                aliases = [
                    alias for alias, target in ALIASES.items() if target == command
                ]
                alias_text = f" (Aliases: {', '.join(aliases)})" if aliases else ""

                if command == "waifu":
                    arg_text = ""
                elif details.type == CommandType.QUERY:
                    arg_text = "[query]"
                elif details.type == CommandType.URL:
                    arg_text = "[url]"
                else:
                    arg_text = ""

                help_message += f"- `{self.config.command.get("command_prefix")}{command} {arg_text}`: {description}{alias_text}\n"

            await packet.event.respond(help_message)

    async def _preprocess_debug(self, packet: CommandPacket) -> None:
        if not self.config.meta.get("debug"):
            return

        if packet.command.name == "cookies":
            cookies = self.config.ytdlp.get("cookies_file")
            result = self.native_controller.write_to_directory(
                content=cookies, directory="/tmp", file_name="cookies.txt"
            )
            if not result:
                await packet.event.respond("Cookies failed to write.")
            else:
                await packet.event.respond("Cookies written successfully.")

    async def _process_url(self, packet: CommandPacket) -> None:
        packet.reaction_id = await packet.event.react("🔄")

        valid_urls = packet.data["valid_urls"]

        processed_media = await self.media_handler.process(
            urls=valid_urls, modifier=packet.command.modifier
        )

        if packet.reaction_id:
            await self.client.redact(
                room_id=packet.event.room_id, event_id=packet.reaction_id
            )
            packet.reaction_id = None

        await self.display_handler.render(media=processed_media, event=packet.event)

    async def _process_query(self, packet: CommandPacket) -> None:
        packet.reaction_id = await packet.event.react("🔄")

        api_provider = packet.command.modifier or ""
        url = await self.query_handler.query_image_controller(
            query=packet.user_args,
            provider=api_provider,
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
