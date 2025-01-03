from __future__ import annotations

import asyncio
from io import BytesIO
from typing import TYPE_CHECKING, List, Optional, Tuple, TypeAlias

from .media_models import ProcessedMedia
from .media_utils import MediaProcessor, SynapseProcessor

if TYPE_CHECKING:
    from main import Config
    from maubot.matrix import MaubotMatrixClient, MaubotMessageEvent
    from mautrix.util.logging.trace import TraceLogger

    from .media_utils import Media


processed_media_event: TypeAlias = Tuple[
    Optional[List[ProcessedMedia]], "MaubotMessageEvent"
]


class MediaHandler:
    def __init__(
        self, log: "TraceLogger", client: "MaubotMatrixClient", config: "Config"
    ):
        self.log = log
        self.client = client
        self.config = config
        self.media_processor = MediaProcessor(log=self.log, config=self.config)
        self.synapse_processor = SynapseProcessor(log=self.log, client=self.client)

    async def _upload_media(
        self, media_object: "Media"
    ) -> Tuple[Optional[str], Optional[str]]:
        content_stream_consumed = False
        thumbnail_stream_consumed = False

        try:
            if isinstance(media_object.content.stream, BytesIO):
                media_object.content.stream.seek(0)

            content_upload_result = (
                await self.synapse_processor.upload_to_content_repository(
                    data=media_object.content.stream,
                    filename=media_object.content.filename,
                    size=media_object.content.metadata.size or 0,
                )
            )
            content_stream_consumed = True

            if isinstance(content_upload_result, asyncio.Task):
                await content_upload_result
                content_upload_result = content_upload_result.result()

            if not content_upload_result:
                self.log.warning(
                    f"Failed to upload content for file: {media_object.content.filename}"
                )
                return None, None

            thumbnail_upload_result = None
            if media_object.thumbnail:
                if isinstance(media_object.thumbnail.stream, BytesIO):
                    media_object.thumbnail.stream.seek(0)

                thumbnail_upload_result = (
                    await self.synapse_processor.upload_to_content_repository(
                        data=media_object.thumbnail.stream,
                        filename=media_object.thumbnail.filename,
                        size=media_object.thumbnail.metadata.size or 0,
                    )
                )
                thumbnail_stream_consumed = True

                if isinstance(thumbnail_upload_result, asyncio.Task):
                    await thumbnail_upload_result
                    thumbnail_upload_result = thumbnail_upload_result.result()

                if not thumbnail_upload_result:
                    self.log.warning("Thumbnail failed to upload to homeserver.")

            return content_upload_result, thumbnail_upload_result

        finally:
            if not content_stream_consumed and isinstance(
                media_object.content.stream, BytesIO
            ):
                media_object.content.stream.close()
            if (
                media_object.thumbnail
                and not thumbnail_stream_consumed
                and isinstance(media_object.thumbnail.stream, BytesIO)
            ):
                media_object.thumbnail.stream.close()

    async def process(
        self, urls: list[str], event: "MaubotMessageEvent"
    ) -> processed_media_event:
        processed_media_array = []

        await self.synapse_processor.reaction_handler(event)

        for url in urls:
            media_object: Optional["Media"] = await self.media_processor.process_url(
                url
            )
            if not media_object:
                self.log.warning(f"Failed to process URL: {url}")
                continue

            media_uri, thumbnail_uri = await self._upload_media(media_object)
            if not media_uri:
                continue

            processed_media_array.append(
                ProcessedMedia(
                    filename=media_object.content.filename,
                    content_info=media_object.content.metadata,
                    content_uri=media_uri,
                    thumbnail_info=(
                        media_object.thumbnail.metadata
                        if media_object.thumbnail
                        else None
                    ),
                    thumbnail_uri=thumbnail_uri,
                )
            )

        await self.synapse_processor.reaction_handler(event)

        if not processed_media_array:
            self.log.warning(
                "MediaHandler.process: No media was sucessfully processed."
            )
            return (None, event)

        return (processed_media_array, event)

    # async def _upload_media(
    #     self, media_object: "Media"
    # ) -> Tuple[Optional[str], Optional[str]]:
    #     content_stream_consumed = False
    #     thumbnail_stream_consumed = False

    #     try:
    #         if isinstance(media_object.content.stream, BytesIO):
    #             media_object.content.stream.seek(0)

    #         media_uri = await self.synapse_processor.upload_to_content_repository(
    #             data=media_object.content.stream,
    #             filename=media_object.content.filename,
    #             size=media_object.content.metadata.size or 0,
    #         )
    #         content_stream_consumed = True

    #         if not media_uri:
    #             self.log.warning(
    #                 f"MediaHandler._upload_media: Failed to upload content for file: {media_object.content.filename}"
    #             )
    #             return None, None

    #         thumbnail_uri = None
    #         if media_object.thumbnail:
    #             if isinstance(media_object.thumbnail.stream, BytesIO):
    #                 media_object.thumbnail.stream.seek(0)

    #             thumbnail_uri = (
    #                 await self.synapse_processor.upload_to_content_repository(
    #                     data=media_object.thumbnail.stream,
    #                     filename=media_object.thumbnail.filename,
    #                     size=media_object.thumbnail.metadata.size or 0,
    #                 )
    #             )
    #             thumbnail_stream_consumed = True

    #             if not thumbnail_uri:
    #                 self.log.warning(
    #                     "MediaHandler._upload_media: Thumbnail failed to upload to homeserver."
    #                 )

    #         return media_uri, thumbnail_uri

    #     finally:
    #         if not content_stream_consumed and isinstance(
    #             media_object.content.stream, BytesIO
    #         ):
    #             media_object.content.stream.close()
    #         if (
    #             media_object.thumbnail
    #             and not thumbnail_stream_consumed
    #             and isinstance(media_object.thumbnail.stream, BytesIO)
    #         ):
    #             media_object.thumbnail.stream.close()
