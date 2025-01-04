from __future__ import annotations

import asyncio
from io import BytesIO
from typing import TYPE_CHECKING, List, Optional, Tuple, TypeAlias

from .media_utils.media_processor import MediaProcessor
from .media_utils.models import ProcessedMedia
from .media_utils.synapse_processor import SynapseProcessor

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from maubot.matrix import MaubotMatrixClient, MaubotMessageEvent
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.origami_media import Config

    from .media_utils.models import Media


processed_media_event: TypeAlias = Tuple[
    Optional[List[ProcessedMedia]], "MaubotMessageEvent"
]


class MediaHandler:
    def __init__(
        self,
        log: "TraceLogger",
        client: "MaubotMatrixClient",
        config: "Config",
        http: "ClientSession",
    ):
        self.log = log
        self.client = client
        self.config = config
        self.http = http
        self.media_processor = MediaProcessor(
            log=self.log, config=self.config, http=self.http
        )
        self.synapse_processor = SynapseProcessor(
            log=self.log, client=self.client, config=self.config
        )

    async def _upload_media(
        self, media_object: "Media"
    ) -> Tuple[Optional[str], Optional[str]]:

        content_upload_result = None
        thumbnail_upload_result = None

        try:
            for media_part, result_variable in [
                (media_object.content, "content_upload_result"),
                (media_object.thumbnail, "thumbnail_upload_result"),
            ]:
                if not media_part:
                    self.log.warning(
                        f"Skipping {result_variable} as media_part is None or empty."
                    )
                    continue

                stream = media_part.stream
                filename = media_part.filename
                size = media_part.metadata.size or 0

                self.log.info(
                    f"Uploading {result_variable}: filename={filename}, size={size}"
                )

                if isinstance(stream, BytesIO):
                    stream.seek(0)

                upload_result = (
                    await self.synapse_processor.upload_to_content_repository(
                        data=stream,
                        filename=filename,
                        size=size,
                    )
                )

                if isinstance(upload_result, asyncio.Task):
                    await upload_result
                    upload_result = upload_result.result()

                if not upload_result:
                    self.log.warning(f"Failed to upload file: {filename}")
                else:
                    self.log.info(
                        f"{result_variable} uploaded successfully: URI={upload_result}"
                    )

                if result_variable == "content_upload_result":
                    content_upload_result = upload_result
                else:
                    thumbnail_upload_result = upload_result

            self.log.info(
                f"Upload Results: content={content_upload_result}, thumbnail={thumbnail_upload_result}"
            )
            return content_upload_result, thumbnail_upload_result

        finally:
            for media_part in [media_object.content, media_object.thumbnail]:
                if media_part and isinstance(media_part.stream, BytesIO):
                    media_part.stream.close()
                    self.log.warning(f"Closed stream for file: {media_part.filename}")

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
                self.log.warning(f"MediaHandler.process: Failed to process URL: {url}")
                continue

            media_uri, thumbnail_uri = await self._upload_media(media_object)
            if not media_uri:
                self.log.warning(
                    f"MediaHandler.process: Failed to upload content for URL: {url}"
                )
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
