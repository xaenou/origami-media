import asyncio
import json
import re
import subprocess
from io import BytesIO
from typing import Optional, Tuple
import unicodedata

from aiohttp import ClientSession
from mautrix.util.ffmpeg import probe_bytes
from mautrix.types import RelationType

from .media_models import Media, MediaMetadata


class MediaHandler:
    def __init__(self, config, log):
        self.config = config
        self.log = log

    def _create_ytdlp_command(self, url: str) -> Optional[str]:
        commands = self.config.ytdlp.get("commands", [])
        if not commands or "command" not in commands[0]:
            self.log.error(
                "MediaProcessor._create_ytdlp_command: Invalid yt-dlp command configuration."
            )
            return None
        command_template = commands[0]["command"]
        return f"{command_template} {url}"

    async def _run_ytdlp_command(self, command: str) -> Optional[dict]:
        try:
            process = await asyncio.create_subprocess_shell(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                self.log.error(f"yt-dlp failed: {stderr.decode().strip()}")
                return None

            output = stdout.decode().strip()
            if not output:
                self.log.error(
                    "MediaHandler._run_ytdlp_command: yt-dlp output is empty."
                )
                return None

            return json.loads(output)

        except Exception as e:
            self.log.exception(f"MediaHandler._run_ytdlp_command: {e}")
            return None

    async def _stream_to_memory(self, stream_url: str) -> BytesIO | None:
        video_data = BytesIO()
        max_retries = self.config.other.get("max_retries", 0)

        for attempt in range(1, max_retries + 1):
            try:
                async with ClientSession() as session:
                    async with session.get(stream_url, timeout=30) as response:
                        if response.status != 200:
                            self.log.warning(
                                f"Attempt {attempt}: Failed to fetch stream, status: {response.status}"
                            )
                            continue

                        total_bytes = 0
                        async for chunk in response.content.iter_chunked(8192):
                            chunk_size = len(chunk)
                            total_bytes += chunk_size
                            video_data.write(chunk)

                            if total_bytes > self.config.other.get(
                                "max_stream_size", 10485760
                            ):
                                self.log.warning(
                                    f"Stream exceeded max size limit of {self.config.other.get('max_stream_size', 10485760)} bytes. Aborting."
                                )
                                return None

                video_data.seek(0)
                return video_data

            except Exception as e:
                self.log.warning(f"Attempt {attempt}: Error streaming video: {e}")
                await asyncio.sleep(1)

        self.log.error(
            f"MediaProcessor._stream_to_memory: Failed to stream video after {max_retries} attempts."
        )
        return None

    async def _get_stream_metadata(self, data: bytes) -> Optional[dict]:
        try:
            metadata = await probe_bytes(data)
            stream = next(
                (
                    stream
                    for stream in metadata.get("streams", [])
                    if stream.get("codec_type") == "video"
                ),
                None,
            )
            if not stream:
                self.log.warning(
                    f"MediaProcessor._get_stream_metadata: No stream found."
                )
                return None

            duration = float(stream.get("duration", 0))
            if duration > self.config.other.get("max_duration", 0):
                self.log.warning(
                    f"MediaProcessor._get_stream_metadata: Duration exceeds max_duration."
                )
                return None

            return {
                "width": stream.get("width"),
                "height": stream.get("height"),
                "duration": duration,
                "size": len(data),
            }
        except Exception as e:
            self.log.exception(f"MediaProcessor._get_stream_metadata: {e}")
            return None

    def _get_extension_from_url(self, url: str) -> str:
        filename = url.rsplit("/", 1)[-1]
        return filename.rsplit('.', 1)[-1] if "." in filename else "jpg"

    async def process_url(self, url: str) -> Tuple[Optional[Media], Optional[Media]]:

        command = self._create_ytdlp_command(url)
        if not command:
            self.log.warning("MediaHandler.process_url: Invalid command, check config.")
            raise Exception

        video_ytdlp_metadata = await self._run_ytdlp_command(command)
        if not video_ytdlp_metadata:
            self.log.warning(
                "MediaHandler.process_url: Failed to find video with yt_dlp"
            )
            raise Exception
        
        is_live = video_ytdlp_metadata.get("is_live")
        if is_live is None:
            self.log.warning(
                "MediaHandler.process_url: 'is_live' key is missing from metadata. Defaulting to non-live."
            )
            is_live = False

        if is_live and not self.config.other.get("enable_livestreams", False):
            self.log.warning(
                "MediaHandler.process_url: Live video detected, and livestreams are disabled. Stopping processing."
            )
            raise Exception("Livestreams are disabled in the configuration.")

        if not is_live:
            duration = video_ytdlp_metadata.get("duration")
            if duration is not None:
                if duration > self.config.other.get("max_duration", 0):
                    self.log.warning(
                        "MediaHandler.process_url: Video length is over the duration limit. Stopping processing."
                    )
                    raise Exception
            else:
                self.log.warning(
                    "MediaHandler.process_url: Video duration is missing from metadata. Will extract it from stream data."
                )

        video_stream = await self._stream_to_memory(video_ytdlp_metadata["url"])
        if not video_stream:
            self.log.warning("MediaHandler.process_url: Failed to download video.")
            raise Exception

        video_stream_metadata = (
            await self._get_stream_metadata(video_stream.getvalue()) or {}
        )

        filename = "{title}-{uploader}-{extractor}-{id}.{ext}".format(
            title=video_ytdlp_metadata.get("title", "unknown_title"),
            uploader=video_ytdlp_metadata.get("uploader", "unknown_uploader"),
            extractor=video_ytdlp_metadata.get("extractor", "unknown_platform"),
            id=video_ytdlp_metadata["id"],
            ext=video_ytdlp_metadata.get("ext", "unknown_extension"),
        )
        # Normalize to ASCII to remove non-ASCII characters (e.g., curly quotes)
        filename = unicodedata.normalize('NFKD', filename).encode('ASCII', 'ignore').decode('ASCII')
        # Add single quote (') and curly quotes if not normalized
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1F\'’“”]', "_", filename)
        # Replace spaces with underscores
        filename = re.sub(r'\s+', "_", filename)
        # Consolidate multiple underscores into a single underscore
        filename = re.sub(r'__+', "_", filename)
        # Remove leading and trailing underscores and dots, and limit length
        filename = filename.strip("_.")[:255]
        # Ensure no consecutive underscores remain
        filename = re.sub(r'_+', "_", filename)


        video = Media(
            filename=filename,
            stream=video_stream,
            metadata=MediaMetadata(
                url=video_ytdlp_metadata["url"],
                id=video_ytdlp_metadata["id"],
                title=video_ytdlp_metadata.get("title"),
                uploader=video_ytdlp_metadata.get("uploader"),
                ext=video_ytdlp_metadata.get("ext"),
                extractor=video_ytdlp_metadata.get("extractor"),
                duration=video_stream_metadata.get("duration"),
                width=video_stream_metadata.get("width"),
                height=video_stream_metadata.get("height"),
                size=video_stream_metadata.get("size"),
            ),
        )

        thumbnail: Optional[Media] = None
        thumbnail_url = video_ytdlp_metadata.get("thumbnail")
        if thumbnail_url:
            thumbnail_stream = await self._stream_to_memory(thumbnail_url)
            if thumbnail_stream:
                thumbnail_stream_metadata = await self._get_stream_metadata(
                    thumbnail_stream.getvalue()
                )
                if thumbnail_stream_metadata:
                    thumbnail_ext = self._get_extension_from_url(thumbnail_url)
                    thumbnail = Media(
                        filename=video_ytdlp_metadata["id"]
                        + "_thumbnail"
                        + thumbnail_ext,
                        stream=thumbnail_stream,
                        metadata=MediaMetadata(
                            url=thumbnail_url,
                            id=video_ytdlp_metadata["id"],
                            uploader=video_ytdlp_metadata.get("uploader"),
                            ext=thumbnail_ext,
                            width=thumbnail_stream_metadata.get("width"),
                            height=thumbnail_stream_metadata.get("height"),
                            size=thumbnail_stream_metadata.get("size"),
                        ),
                    )
                else:
                    self.log.warning(
                        "MediaHandler.process_url: Failed to fetch thumbnail metadata."
                    )
            else:
                self.log.warning(
                    "MediaHandler.process_url: Failed to download thumbnail."
                )

        self.log.info(
            f"Video Found:\n- Title: {video.metadata.title}\n- Uploader: {video.metadata.uploader}\n"
            f"- Resolution: {video.metadata.width}x{video.metadata.height}\n"
            f"- Duration: {video.metadata.duration}s"
        )

        if thumbnail:
            self.log.info(
                f"Thumbnail Found:\n- Resolution: {thumbnail.metadata.width}x{thumbnail.metadata.height}\n"
                f"- Size: {thumbnail.metadata.size} bytes"
            )

        return (video, thumbnail)


class SynapseHandler:
    def __init__(self, log, client):
        self.log = log
        self.client = client

    async def _is_reacted(
        self, room_id, event_id, reaction: str
    ) -> Tuple[bool, Optional[str]]:
        try:
            response = await self.client.get_event_context(
                room_id=room_id, event_id=event_id, limit=4
            )
            for event in response.events_after:
                content = getattr(event, "content", None)
                if content:
                    relates_to = getattr(content, "relates_to", None)
                    if relates_to:
                        if (
                            relates_to.rel_type == RelationType.ANNOTATION
                            and relates_to.event_id == event_id
                            and relates_to.key == reaction
                            and event.sender == self.client.mxid
                        ):
                            return True, event.event_id
            return False, None
        except Exception as e:
            self.log.error(f"Failed to fetch reaction event: {e}")
            return False, None

    async def reaction_handler(self, event) -> None:
        is_reacted, reaction_id = await self._is_reacted(
            room_id=event.room_id, event_id=event.event_id, reaction="🔄"
        )
        if not is_reacted:
            await event.react(key="🔄")
        if is_reacted:
            await self.client.redact(room_id=event.room_id, event_id=reaction_id)

    async def upload_to_content_repository(self, data, filename, size) -> str | None:
        response = await self.client.upload_media(
            data=data,
            filename=filename,
            size=size,
        )
        uri = response
        if not uri:
            self.log.error(
                f"SynapseHandler.upload_to_content_repository: uri not obtained"
            )
            raise Exception

        return uri
