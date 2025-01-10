from __future__ import annotations

from typing import TYPE_CHECKING, Optional, cast

from maubot.matrix import MaubotMessageEvent

from origami_media.models.command_models import (
    ALIASES,
    BASE_COMMANDS,
    Command,
    CommandPacket,
)

if TYPE_CHECKING:
    from origami_media.handlers.url_handler import UrlHandler
    from origami_media.main import Config

    """
    Processes incoming events and instantiates command packets.
    
    """


class EventProcessor:
    def __init__(self, url_handler: "UrlHandler", config: "Config"):
        self.url_handler = url_handler
        self.config = config
        self.command_prefix = self.config.command.get("command_prefix", "!")

    def handle_passive(self, event: MaubotMessageEvent) -> Optional[CommandPacket]:
        if not self.config.meta.get("enable_passive_url_detection", False):
            return

        if "http" not in event.content.body:
            return

        command = BASE_COMMANDS.get("get")
        if not command:
            return

        return CommandPacket(command=command, event=event, user_args="")

    def handle_active(self, event: MaubotMessageEvent) -> Optional[CommandPacket]:
        if not self.config.meta.get("enable_commands", False):
            return None

        body = cast(str, event.content.body)
        if not body.strip():
            return None

        parts = body.split(" ", 1)
        command_name = parts[0]
        user_args = parts[1].strip() if len(parts) > 1 else ""

        command = self._resolve_command(command_name)
        if not command:
            return None

        return CommandPacket(command=command, event=event, user_args=user_args)

    def _resolve_command(self, command_name: str) -> Optional[Command]:
        command_name = command_name[len(self.command_prefix) :]

        if command_name in ALIASES:
            command_name = ALIASES[command_name]

        return BASE_COMMANDS.get(command_name)
