from typing import TYPE_CHECKING

from mautrix.types import (
    AudioInfo,
    ContentURI,
    FileInfo,
    ImageInfo,
    ThumbnailInfo,
    VideoInfo,
)
from mautrix.types.event import message
from mautrix.types.event.type import EventType

if TYPE_CHECKING:
    from maubot.matrix import MaubotMatrixClient, MaubotMessageEvent
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.origami_media import Config

    from .media_utils.models import ProcessedMedia


class DisplayHandler:
    def __init__(
        self, log: "TraceLogger", client: "MaubotMatrixClient", config: "Config"
    ):
        self.log = log
        self.client = client
        self.config = config

    async def _build_message_content(self, processed_media: "ProcessedMedia"):
        filename = processed_media.filename
        content_info = processed_media.content_info
        uri = processed_media.content_uri

        thumbnail_info = None
        thumbnail_uri = None
        if processed_media.thumbnail_info and processed_media.thumbnail_uri:
            t_meta = processed_media.thumbnail_info
            t_ext = t_meta.ext or "jpg"
            t_size = t_meta.size or 0
            thumbnail_uri = processed_media.thumbnail_uri
            self.log.info("Content being rendered with thumbnail")
            thumbnail_info = ThumbnailInfo(
                height=int(t_meta.height or 0),
                width=int(t_meta.width or 0),
                mimetype=f"image/{t_ext}",
                size=int(t_size),
            )

        if content_info.media_type == "video":
            self.log.info("Content being rendered as video")
            msgtype = message.MessageType.VIDEO
            media_info = VideoInfo(
                mimetype=f"video/{content_info.ext}",
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
                mimetype=f"audio/{content_info.ext}",
                duration=int(content_info.duration or 0),
                size=int(content_info.size or 0),
            )
        elif content_info.media_type == "image":
            self.log.info("Content being rendered as image")
            msgtype = message.MessageType.IMAGE
            media_info = ImageInfo(
                mimetype=f"image/{content_info.ext}",
                height=int(content_info.height or 0),
                width=int(content_info.width or 0),
                size=int(content_info.size or 0),
            )
        else:
            self.log.info("Content being rendered as file")
            msgtype = message.MessageType.FILE
            media_info = FileInfo(
                mimetype="application/octet-stream",
                size=int(content_info.size or 0),
                thumbnail_info=thumbnail_info if thumbnail_info else None,
                thumbnail_url=ContentURI(thumbnail_uri) if thumbnail_uri else None,
            )

        content = message.MediaMessageEventContent(
            msgtype=msgtype,
            url=ContentURI(uri),
            filename=filename,
            info=media_info,
            body=filename,
        )

        return content

    async def render(
        self,
        media: list["ProcessedMedia"],
        event: "MaubotMessageEvent",
        reply: bool = True,
    ) -> None:
        for media_object in media:

            content = await self._build_message_content(processed_media=media_object)

            room_id = event.room_id

            if reply:
                content.set_reply(event, disable_fallback=True)

            await self.client.send_message_event(
                room_id=room_id, event_type=EventType.ROOM_MESSAGE, content=content
            )
