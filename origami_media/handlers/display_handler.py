from __future__ import annotations

from typing import TYPE_CHECKING

from maubot.matrix import parse_formatted
from mautrix.types import (
    AudioInfo,
    ContentURI,
    FileInfo,
    Format,
    ImageInfo,
    ThumbnailInfo,
    VideoInfo,
)
from mautrix.types.event import message
from mautrix.types.event.type import EventType

if TYPE_CHECKING:
    from maubot.matrix import MaubotMatrixClient, MaubotMessageEvent
    from mautrix.types import EventID
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.main import Config

    from .media_handler import ProcessedMedia


class DisplayHandler:
    def __init__(
        self, log: "TraceLogger", client: "MaubotMatrixClient", config: "Config"
    ):
        self.log = log
        self.client = client
        self.config = config

    def _convert_extractor(self, key: str) -> str:
        SERVICES = {
            "youtube": "YouTube",
            "youtu": "YouTube",
            "twitter": "Twitter",
            "x": "X",
            "rumble": "Rumble",
            "odysee": "Odysee",
            "bitchute": "BitChute",
            "4cdn": "4chan",
            "tenor": "Tenor",
            "unsplash": "Unsplash",
            "waifu": "Waifu.im",
        }
        return SERVICES.get(key, key)

    async def _build_message_content(self, processed_media: "ProcessedMedia"):
        filename = processed_media.filename
        content_info = processed_media.content_info
        uri = processed_media.content_uri

        thumbnail_info = None
        thumbnail_uri = None
        if processed_media.thumbnail_info and processed_media.thumbnail_uri:
            t_meta = processed_media.thumbnail_info
            t_size = t_meta.size or 0
            thumbnail_uri = processed_media.thumbnail_uri
            self.log.info("Content being rendered with thumbnail")
            thumbnail_info = ThumbnailInfo(
                height=int(t_meta.height or 0),
                width=int(t_meta.width or 0),
                mimetype=t_meta.mimetype,
                size=int(t_size),
            )
        body = None

        if content_info.media_type == "video":
            self.log.info("Content being rendered as video")
            if content_info.origin == "advanced":
                if content_info.size:
                    size_in_MB = content_info.size / (1024 * 1024)
                    size_str = f"{size_in_MB:.2f}MB"
                if content_info.duration:
                    total_seconds = int(content_info.duration)
                    hours = total_seconds // 3600
                    minutes = (total_seconds % 3600) // 60
                    seconds = total_seconds % 60
                    if hours > 0:
                        duration_str = f"{hours}:{minutes:02}:{seconds:02}"
                    elif minutes > 0:
                        duration_str = f"{minutes}:{seconds:02}"
                    else:
                        duration_str = f"{seconds} seconds"
                body = f"**Title:** {content_info.title}\n\n**Duration:** {duration_str}\n\n**Size:** {size_str}"
            msgtype = message.MessageType.VIDEO
            media_info = VideoInfo(
                mimetype=content_info.mimetype,
                duration=int(content_info.duration or 0),
                height=int(content_info.height or 0),
                width=int(content_info.width or 0),
                size=int(content_info.size or 0),
                thumbnail_info=thumbnail_info if thumbnail_info else None,
                thumbnail_url=ContentURI(thumbnail_uri) if thumbnail_uri else None,
            )

        elif content_info.media_type == "audio":
            self.log.info("Content being rendered as audio")
            msgtype = message.MessageType.AUDIO
            media_info = AudioInfo(
                mimetype=content_info.mimetype,
                duration=int(content_info.duration or 0),
                size=int(content_info.size or 0),
            )
        elif content_info.media_type == "image":
            self.log.info("Content being rendered as image")
            if content_info.origin == "advanced-thumbnail-fallback":
                if content_info.meta_duration:
                    total_seconds = content_info.meta_duration
                    hours = total_seconds // 3600
                    minutes = (total_seconds % 3600) // 60
                    seconds = total_seconds % 60
                    if hours > 0:
                        duration_str = f"{hours}:{minutes:02}:{seconds:02}"
                    elif minutes > 0:
                        duration_str = f"{minutes}:{seconds:02}"
                    else:
                        duration_str = f"{seconds} seconds"
                body = f"**Title:** {content_info.title}\n\n**Duration:** {duration_str}\n\n**Platform:** {self._convert_extractor(content_info.extractor or "")}"
            msgtype = message.MessageType.IMAGE
            media_info = ImageInfo(
                mimetype=content_info.mimetype,
                height=int(content_info.height or 0),
                width=int(content_info.width or 0),
                size=int(content_info.size or 0),
            )
        else:
            self.log.info("Content being rendered as file")
            msgtype = message.MessageType.FILE
            media_info = FileInfo(
                mimetype=content_info.mimetype,
                size=int(content_info.size or 0),
                thumbnail_info=thumbnail_info if thumbnail_info else None,
                thumbnail_url=ContentURI(thumbnail_uri) if thumbnail_uri else None,
            )

        content = message.MediaMessageEventContent(
            msgtype=msgtype,
            url=ContentURI(uri),
            filename=filename,
            info=media_info,
            body=body or "",
        )

        content.format = Format.HTML
        content.body, content.formatted_body = await parse_formatted(
            content.body, render_markdown=True, allow_html=True
        )
        return content

    async def render(
        self,
        media: list["ProcessedMedia"],
        event: "MaubotMessageEvent",
        reply: bool = True,
    ) -> None:
        for media_object in media:
            try:
                content = await self._build_message_content(
                    processed_media=media_object
                )
                if reply:
                    content.set_reply(event, disable_fallback=True)

                await self.client.send_message_event(
                    room_id=event.room_id,
                    event_type=EventType.ROOM_MESSAGE,
                    content=content,
                )
            except Exception as e:
                self.log.error(
                    f"MediaHandler.process: Unexpected error when trying to render {event.event_id}: {e}"
                )
                continue

    async def censor(
        self, sanitized_message: str, event: "MaubotMessageEvent"
    ) -> "EventID":
        if " " in sanitized_message:
            cleaned_content = f'Tracking parameter(s) removed:\n\n"{sanitized_message}"'
        else:
            cleaned_content = "Link tracking parameter(s) removed: " + sanitized_message
        content = message.TextMessageEventContent(
            msgtype=message.MessageType.TEXT, body=cleaned_content
        )
        content.set_reply(event, disable_fallback=True)
        await event.redact(reason="Redacted for tracking URL(s).")
        new_message_event_id = await self.client.send_message_event(
            room_id=event.room_id, event_type=EventType.ROOM_MESSAGE, content=content
        )
        return new_message_event_id
