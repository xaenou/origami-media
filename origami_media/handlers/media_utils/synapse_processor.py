from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, AsyncIterable

if TYPE_CHECKING:
    from maubot.matrix import MaubotMatrixClient
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.origami_media import Config


class SynapseProcessor:
    def __init__(
        self, log: "TraceLogger", client: "MaubotMatrixClient", config: "Config"
    ):
        self.log = log
        self.client = client
        self.config = config

    async def _bytes_io_to_async_iter(
        self, stream: BytesIO, chunk_size: int = 4096
    ) -> AsyncIterable[bytes]:
        while chunk := stream.read(chunk_size):
            yield chunk

    async def _handle_sync_upload(self, data: BytesIO, filename: str, size: int):
        upload_data = data.read()
        response = await self.client.upload_media(
            data=upload_data,
            filename=filename,
            size=size,
            async_upload=False,
        )
        return response

    async def upload_to_content_repository(
        self, data: BytesIO, filename: str, size: int
    ):
        return await self._handle_sync_upload(data, filename, size)
