from __future__ import annotations

from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from maubot.matrix import MaubotMessageEvent
    from mautrix.types import EventID


class CommandType(Enum):
    URL = auto()
    QUERY = auto()
    PRINT = auto()
    DEBUG = auto()


class Command:
    def __init__(
        self,
        name: str,
        type: CommandType,
        description: str,
        modifier: Optional[str] = None,
    ):
        self.name = name
        self.type = type
        self.description = description
        self.modifier = modifier

    def __repr__(self):
        return f"<Command name={self.name} type={self.type}>"


BASE_COMMANDS = {
    "help": Command(
        name="help",
        type=CommandType.PRINT,
        description="Show this help message.",
    ),
    "get": Command(
        name="get",
        type=CommandType.URL,
        description="Download media from a url.",
    ),
    "audio": Command(
        name="audio",
        type=CommandType.URL,
        description="Download audio only for a url.",
        modifier="force_audio_only",
    ),
    "tenor": Command(
        name="tenor",
        type=CommandType.QUERY,
        description="Download gif by querying tenor.",
        modifier="tenor",
    ),
    "unsplash": Command(
        name="unsplash",
        type=CommandType.QUERY,
        description="Download image by querying unsplash.",
        modifier="unsplash",
    ),
    "lexica": Command(
        name="lexica",
        type=CommandType.QUERY,
        description="Download an image by querying Lexica.",
        modifier="lexica",
    ),
    "waifu": Command(
        name="waifu",
        type=CommandType.QUERY,
        description="Roll for a random Waifu.",
        modifier="waifu",
    ),
    "debug": Command(
        name="debug",
        type=CommandType.DEBUG,
        description="N/A",
    ),
    "cookies": Command(
        name="cookies",
        type=CommandType.DEBUG,
        description="Set cookies from config.",
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
        user_args: str,
        data: Optional[Dict[str, Any]] = None,
    ):
        self.command = command
        self.event = event
        self.user_args = user_args
        self.data = data or {}
        self.reaction_id: Optional["EventID"] = None

    def __repr__(self):
        return f"< CommandPacket command={self.command.name} command type={self.command.type} >"
