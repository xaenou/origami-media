from __future__ import annotations

from typing import TYPE_CHECKING, Optional, cast

from maubot.matrix import MaubotMessageEvent

from origami_media.models.command_models import (
    ALIASES,
    BASE_COMMANDS,
    Command,
    CommandPacket,
    Route,
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

        url_tuple = self.url_handler.process(event)
        if not url_tuple:
            return

        command = BASE_COMMANDS.get("get")
        if not command:
            return None

        return CommandPacket(
            command=command,
            event=event,
            data={"url_tuple": url_tuple},
            args={"media_modifier": None},
        )

    def handle_active(self, event: MaubotMessageEvent) -> Optional[CommandPacket]:
        return self._build_command_packet(event=event)

    def _build_command_packet(
        self, event: "MaubotMessageEvent"
    ) -> Optional[CommandPacket]:
        if not self.config.meta.get("enable_commands", False):
            return None

        body = cast(str, event.content.body)
        parts = body.split(" ", 1)
        command_name = parts[0]
        argument = parts[1] if len(parts) > 1 else ""

        command = self._resolve_command(command_name)
        if not command:
            return None

        if command.route == Route.URL:
            url_tuple = self.url_handler.process(event)
            if not url_tuple:
                return None

            return CommandPacket(
                command=command,
                event=event,
                data={"url_tuple": url_tuple},
                args={"media_modifier": command.modifier},
            )

        elif command.route == Route.QUERY:
            return CommandPacket(
                command=command,
                event=event,
                args={
                    "query": argument,
                    "api_provider": command.api_provider,
                },
            )

        elif command.route == Route.PRINT:
            content_function = getattr(self, command.name, None)
            content = content_function() if callable(content_function) else command.name

            return CommandPacket(
                command=command,
                event=event,
                data={"content": content},
            )

        return None

    def _resolve_command(self, command_name: str) -> Optional[Command]:
        command_name = command_name[len(self.command_prefix) :]

        if command_name in ALIASES:
            command_name = ALIASES[command_name]

        return BASE_COMMANDS.get(command_name)

    def help(self) -> str:
        help_message = "**Available Commands:**\n"
        for command, details in BASE_COMMANDS.items():
            description = details.description
            aliases = [alias for alias, target in ALIASES.items() if target == command]
            alias_text = f" (Aliases: {', '.join(aliases)})" if aliases else ""

            if command == "waifu":
                arg_text = ""
            elif details.route == Route.QUERY:
                arg_text = "[query]"
            elif details.route == Route.URL:
                arg_text = "[url]"
            else:
                arg_text = ""

            help_message += f"- `{self.command_prefix}{command} {arg_text}`: {description}{alias_text}\n"

        return help_message
