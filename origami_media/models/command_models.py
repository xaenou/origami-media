from __future__ import annotations

from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from maubot.matrix import MaubotMessageEvent
    from mautrix.types import EventID


class Route(Enum):
    URL = auto()
    QUERY = auto()
    PRINT = auto()


class Command:
    def __init__(
        self,
        name: str,
        route: Route,
        description: str,
        modifier: Optional[str] = None,
        api_provider: Optional[str] = None,
    ):
        self.name = name
        self.route = route
        self.description = description
        self.modifier = modifier
        self.api_provider = api_provider

    def __repr__(self):
        return f"<Command name={self.name} route={self.route}>"


BASE_COMMANDS = {
    "help": Command(
        name="help",
        route=Route.PRINT,
        description="Show this help message.",
    ),
    "get": Command(
        name="get",
        route=Route.URL,
        description="Download media from a url.",
    ),
    "audio": Command(
        name="audio",
        route=Route.URL,
        description="Download audio only for a url.",
        modifier="force_audio_only",
    ),
    "tenor": Command(
        name="tenor",
        route=Route.QUERY,
        description="Download gif by querying tenor.",
        api_provider="tenor",
    ),
    "unsplash": Command(
        name="unsplash",
        route=Route.QUERY,
        description="Download image by querying unsplash.",
        api_provider="unsplash",
    ),
    "lexica": Command(
        name="lexica",
        route=Route.QUERY,
        description="Download an image by querying Lexica.",
        api_provider="lexica",
    ),
    "waifu": Command(
        name="waifu",
        route=Route.QUERY,
        description="Roll for a random Waifu.",
        api_provider="waifu",
    ),
}

ALIASES = {
    "gif": "tenor",
    "img": "unsplash",
    "lex": "lexica",
    "girl": "waifu",
    "g": "waifu",
    "mp3": "audio",
}


class CommandPacket:
    def __init__(
        self,
        command: Command,
        event: "MaubotMessageEvent",
        args: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ):
        self.command = command
        self.route = command.route
        self.event = event
        self.args = args or {}
        self.data = data or {}
        self.reaction_id: Optional["EventID"] = None

    def __repr__(self):
        return f"<CommandPacket command={self.command.name} route={self.route}>"
