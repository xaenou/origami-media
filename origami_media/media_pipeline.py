from mautrix.types import ContentURI, ThumbnailInfo, VideoInfo
from mautrix.types.event import message

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
            await self.synapse_handler.reaction_handler(event)
            result = await self.video_processor.process_url(url)
            video = result[0]
            thumbnail = result[1]

            if video is None:
                raise Exception

            video_metadata = video.metadata if video and video.metadata else None

            video_ext = (
                video_metadata.ext if video_metadata and video_metadata.ext else "mp4"
            )

            video_filename = video.filename if video_metadata else "video.mp4"

            video_size = (
                video_metadata.size if video_metadata and video_metadata.size else 0
            )

            video_uri = await self.synapse_handler.upload_to_content_repository(
                data=video.stream, filename=video_filename, size=video_size
            )
            if not video_uri:
                self.log.warning(
                    "OrigamiMedia.dl: Failed to interact with content repository for video upload."
                )
                raise Exception

            thumbnail_uri = None
            thumbnail_info = None

            if thumbnail and thumbnail.metadata:
                thumbnail_metadata = thumbnail.metadata

                thumbnail_ext = (
                    thumbnail_metadata.ext if thumbnail_metadata.ext else "jpg"
                )

                thumbnail_filename = (
                    thumbnail.filename if thumbnail.filename else "thumbnail"
                )

                thumbnail_size = (
                    thumbnail_metadata.size
                    if thumbnail_metadata and thumbnail_metadata.size
                    else 0
                )

                thumbnail_uri = await self.synapse_handler.upload_to_content_repository(
                    data=thumbnail.stream,
                    filename=thumbnail_filename,
                    size=thumbnail_size,
                )
                if thumbnail_uri:
                    thumbnail_info = ThumbnailInfo(
                        height=int(thumbnail_metadata.height or 0),
                        width=int(thumbnail_metadata.width or 0),
                        mimetype=f"image/{thumbnail_ext}",
                        size=int(thumbnail_size),
                    )
                else:
                    self.log.warning(
                        "OrigamiMedia.dl: Failed to upload thumbnail to Synapse."
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

            content = message.MediaMessageEventContent(
                msgtype=message.MessageType.VIDEO,
                url=ContentURI(video_uri),
                filename=video_filename,
                info=video_info,
            )

            await event.respond(content=content, reply=True)
            await self.synapse_handler.reaction_handler(event)

        except Exception as e:
            self.log.exception(f"OrigamiMedia.dl: {e}")
            await self.synapse_handler.reaction_handler(event)
