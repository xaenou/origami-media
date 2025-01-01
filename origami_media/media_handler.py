from mautrix.types import (
    AudioInfo,
    ContentURI,
    FileInfo,
    ImageInfo,
    ThumbnailInfo,
    VideoInfo,
)
from mautrix.types.event import message

from .media_utils import MediaProcessor, SynapseProcessor


class MediaHandler:
    def __init__(self, log, client, config):
        self.log = log
        self.client = client
        self.config = config
        self.media_processor = MediaProcessor(log=self.log, config=self.config)
        self.synapse_processor = SynapseProcessor(log=self.log, client=self.client)

    async def process(self, url: str, event):
        try:
            await self.synapse_processor.reaction_handler(event)

            result = await self.media_processor.process_url(url)
            if not result:
                raise Exception("No result returned from process_url.")

            media_obj, thumbnail_obj = result

            if media_obj is None or media_obj.metadata is None:
                raise Exception("Failed to create media_obj or metadata is missing.")

            media_meta = media_obj.metadata
            media_ext = media_meta.ext or "mp4"
            media_filename = media_obj.filename or "media_file"
            media_size = media_meta.size or 0

            media_uri = await self.synapse_processor.upload_to_content_repository(
                data=media_obj.stream, filename=media_filename, size=media_size
            )
            if not media_uri:
                self.log.warning("Could not upload main media to content repository.")
                raise Exception("Failed to upload main media.")

            thumbnail_uri, thumbnail_info = None, None
            if thumbnail_obj and thumbnail_obj.metadata:
                t_meta = thumbnail_obj.metadata
                t_ext = t_meta.ext or "jpg"
                t_filename = thumbnail_obj.filename or "thumbnail"
                t_size = t_meta.size or 0

                tmp_uri = await self.synapse_processor.upload_to_content_repository(
                    data=thumbnail_obj.stream, filename=t_filename, size=t_size
                )
                if tmp_uri:
                    thumbnail_uri = tmp_uri
                    thumbnail_info = ThumbnailInfo(
                        height=int(t_meta.height or 0),
                        width=int(t_meta.width or 0),
                        mimetype=f"image/{t_ext}",
                        size=int(t_size),
                    )
                else:
                    self.log.warning("Failed to upload thumbnail to Synapse.")

            has_video = media_meta.has_video
            has_audio = media_meta.has_audio
            is_image = media_meta.is_image

            if is_image:
                msgtype = message.MessageType.IMAGE
                mimetype = f"image/{media_ext}"
                image_info = ImageInfo(
                    height=int(media_meta.height or 0),
                    width=int(media_meta.width or 0),
                    mimetype=mimetype,
                    size=int(media_size),
                )
                content = message.MediaMessageEventContent(
                    msgtype=msgtype,
                    url=ContentURI(media_uri),
                    filename=media_filename,
                    info=image_info,
                )

            elif has_video:
                msgtype = message.MessageType.VIDEO
                mimetype = f"video/{media_ext}"
                video_info = VideoInfo(
                    duration=int(media_meta.duration or 0),
                    height=int(media_meta.height or 0),
                    width=int(media_meta.width or 0),
                    mimetype=mimetype,
                    size=int(media_size),
                    thumbnail_info=thumbnail_info,
                    thumbnail_url=ContentURI(thumbnail_uri) if thumbnail_uri else None,
                )
                content = message.MediaMessageEventContent(
                    msgtype=msgtype,
                    url=ContentURI(media_uri),
                    filename=media_filename,
                    info=video_info,
                )

            elif has_audio:
                msgtype = message.MessageType.AUDIO
                mimetype = f"audio/{media_ext}"
                audio_info = AudioInfo(
                    duration=int(media_meta.duration or 0),
                    mimetype=mimetype,
                    size=int(media_size),
                )
                content = message.MediaMessageEventContent(
                    msgtype=msgtype,
                    url=ContentURI(media_uri),
                    filename=media_filename,
                    info=audio_info,
                )

            else:
                msgtype = message.MessageType.FILE
                mimetype = "application/octet-stream"
                file_info = FileInfo(
                    mimetype=mimetype,
                    size=int(media_size),
                )
                content = message.MediaMessageEventContent(
                    msgtype=msgtype,
                    url=ContentURI(media_uri),
                    filename=media_filename,
                    info=file_info,
                )

            await event.respond(content=content, reply=True)
            await self.synapse_processor.reaction_handler(event)

        except Exception as e:
            self.log.exception(f"OrigamiMedia: {e}")
            await self.synapse_processor.reaction_handler(event)
