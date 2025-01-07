from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING, Optional, Tuple

from .media_utils.media_processor import MediaProcessor
from .media_utils.synapse_processor import SynapseProcessor

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from maubot.matrix import MaubotMatrixClient
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.origami_media import Config

    from .media_utils.media_processor import Media, MediaInfo


@dataclass
class ProcessedMedia:
    filename: str
    content_info: "MediaInfo"
    content_uri: str
    thumbnail_info: Optional["MediaInfo"]
    thumbnail_uri: Optional[str]

    def __str__(self):
        return (
            f"ProcessedMedia(content_uri='{self.content_uri}', "
            f"thumbnail_uri='{self.thumbnail_uri or 'None'}')"
        )


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

    async def _upload_media(self, media_object: "Media") -> Tuple[str, Optional[str]]:
        content_upload_result: Optional[str] = None
        thumbnail_upload_result: Optional[str] = None

        try:
            if not media_object.content:
                raise ValueError("Content is required for upload but is missing.")

            content_part = media_object.content
            content_part.stream.seek(0)
            content_upload_result = (
                await self.synapse_processor.upload_to_content_repository(
                    data=content_part.stream,
                    filename=content_part.filename,
                    size=content_part.metadata.size or 0,
                )
            )

            if not content_upload_result:
                raise RuntimeError(
                    f"Failed to upload content file: {content_part.filename}"
                )

            if media_object.thumbnail:
                thumbnail_part = media_object.thumbnail
                thumbnail_part.stream.seek(0)
                thumbnail_upload_result = (
                    await self.synapse_processor.upload_to_content_repository(
                        data=thumbnail_part.stream,
                        filename=thumbnail_part.filename,
                        size=thumbnail_part.metadata.size or 0,
                    )
                )

            return content_upload_result, thumbnail_upload_result

        finally:
            for media_part in [media_object.content, media_object.thumbnail]:
                if media_part and isinstance(media_part.stream, BytesIO):
                    media_part.stream.close()

    async def process(self, urls: list[str], modifier=None) -> list[ProcessedMedia]:
        processed_media_array: list[ProcessedMedia] = []

        for url in urls:
            try:
                media_object: Optional["Media"] = (
                    await self.media_processor.process_url(url=url, modifier=modifier)
                )
                if not media_object:
                    self.log.warning(
                        f"MediaHandler.process: Failed to process URL: {url}"
                    )
                    continue

                media_uri, thumbnail_uri = await self._upload_media(media_object)

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

            except Exception as e:
                self.log.error(
                    f"MediaHandler.process: Unexpected error for URL {url}: {e}"
                )
                continue

        if not processed_media_array:
            raise Exception(
                "MediaHandler.process: No media was successfully processed."
            )

        return processed_media_array
