from typing import Optional

from mautrix.types import ContentURI, ThumbnailInfo, VideoInfo
from mautrix.types.event import message

from .media_models import ThumbnailData, ThumbnailMetadata, VideoData, VideoMetadata
from .media_pipeline_utils import MediaHandler, SynapseHandler


class MediaPipeline:
    def __init__(self, log, client, config):
        self.log = log
        self.client = client
        self.config = config
        self.video_processor = MediaHandler(log=self.log, config=self.config)
        self.synapse_handler = SynapseHandler(log=self.log, client=self.client)

    async def process(self, url, event):
        try:
            reaction_event_ID = await event.react(key="🔄")

            result = await self.video_processor.process_url(url)
            video: Optional[VideoData] = result[0]
            thumbnail: Optional[ThumbnailData] = result[1]

            if video is None:
                raise Exception

            video_metadata: Optional[VideoMetadata] = (
                video.info if video and video.info else None
            )
            video_ext = (
                video_metadata.ext if video_metadata and video_metadata.ext else "mp4"
            )
            video_filename = (
                f"{video_metadata.id}.{video_ext}" if video_metadata else "video.mp4"
            )
            video_size = video.size if video else 0

            video_uri = await self.synapse_handler.upload_to_content_repository(
                data=video.stream, filename=video_filename, size=video_size
            )
            if not video_uri:
                self.log.warning(
                    "OrigamiVideo.dl: Failed to interact with content repository for video upload."
                )
                raise Exception

            self.log.info(f"Video uploaded successfully: {video_uri}")

            thumbnail_uri = None
            thumbnail_info = None

            if thumbnail and thumbnail.info:
                thumbnail_metadata: ThumbnailMetadata = thumbnail.info
                thumbnail_ext = (
                    thumbnail_metadata.ext if thumbnail_metadata.ext else "jpg"
                )
                thumbnail_filename = (
                    f"{video_metadata.id}_thumbnail.{thumbnail_ext}"
                    if video_metadata
                    else "thumbnail.jpg"
                )
                thumbnail_size = thumbnail.size if thumbnail else 0

                thumbnail_uri = await self.synapse_handler.upload_to_content_repository(
                    data=thumbnail.stream,
                    filename=thumbnail_filename,
                    size=thumbnail_size,
                )
                if thumbnail_uri:
                    self.log.info(f"Thumbnail uploaded successfully: {thumbnail_uri}")
                    thumbnail_info = ThumbnailInfo(
                        height=int(thumbnail_metadata.height or 0),
                        width=int(thumbnail_metadata.width or 0),
                        mimetype=f"image/{thumbnail_ext}",
                        size=int(thumbnail_size),
                    )
                else:
                    self.log.warning(
                        "OrigamiVideo.dl: Failed to upload thumbnail to Synapse."
                    )

            video_info = VideoInfo(
                duration=int(video_metadata.duration or 0) if video_metadata else 0,
                height=int(video_metadata.height or 0) if video_metadata else 0,
                width=int(video_metadata.width or 0) if video_metadata else 0,
                mimetype=f"video/{video_ext}",
                size=int(video_size),
                thumbnail_info=thumbnail_info,
                thumbnail_url=ContentURI(thumbnail_uri) if thumbnail_uri else None,
            )

            # Construct and send the video event
            content = message.MediaMessageEventContent(
                msgtype=message.MessageType.VIDEO,
                url=ContentURI(video_uri),
                filename=video_filename,
                info=video_info,
            )

            await event.respond(content=content, reply=True)
            self.log.info("OrigamiVideo.dl: Video message sent successfully.")
            reaction_event = await self.client.get_event(
                event_id=reaction_event_ID, room_id=event.room_id
            )
            await reaction_event.redact()

        except Exception as e:
            self.log.exception(f"OrigamiVideo.dl: {e}")
            reaction_event = await self.client.get_event(
                event_id=reaction_event_ID, room_id=event.room_id
            )
            await reaction_event.redact()
