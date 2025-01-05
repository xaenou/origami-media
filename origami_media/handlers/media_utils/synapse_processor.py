from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, AsyncIterable, Optional, Tuple

from mautrix.types import ReactionEvent, RelationType

if TYPE_CHECKING:
    from maubot.matrix import MaubotMatrixClient, MaubotMessageEvent
    from mautrix.types import EventID, RoomID
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.origami_media import Config


class SynapseProcessor:
    def __init__(
        self, log: "TraceLogger", client: "MaubotMatrixClient", config: "Config"
    ):
        self.log = log
        self.client = client
        self.config = config

    async def _is_reacted(
        self, room_id: "RoomID", event_id: "EventID", reaction: str
    ) -> Tuple[bool, Optional["EventID"]]:
        try:
            response = await self.client.get_event_context(
                room_id=room_id, event_id=event_id, limit=10
            )
            if not response:
                return False, None

            reaction_event = next(
                (
                    event
                    for event in response.events_after
                    if isinstance(event, ReactionEvent)
                    and (relates_to := getattr(event.content, "relates_to", None))
                    and relates_to.rel_type == RelationType.ANNOTATION
                    and relates_to.event_id == event_id
                    and relates_to.key == reaction
                    and event.sender == self.client.mxid
                ),
                None,
            )

            return (True, reaction_event.event_id) if reaction_event else (False, None)

        except Exception as e:
            self.log.error(
                f"SynapseProcessor._is_reacted: Failed to fetch reaction event: {e}"
            )
            return False, None

    async def reaction_handler(self, event: "MaubotMessageEvent") -> None:
        is_reacted, reaction_id = await self._is_reacted(
            room_id=event.room_id, event_id=event.event_id, reaction="🔄"
        )
        try:
            if not is_reacted:
                await event.react(key="🔄")
            elif is_reacted and reaction_id:
                await self.client.redact(room_id=event.room_id, event_id=reaction_id)
        except Exception as e:
            self.log.error(
                f"SynapseProcessor.reaction_handler: Failed to handle reaction: {e}"
            )

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
