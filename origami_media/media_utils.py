import asyncio
import json
import re
import shlex
import subprocess
import unicodedata
from io import BytesIO
from typing import List, Optional, Tuple

from aiohttp import ClientError, ClientSession, ClientTimeout
from mautrix.types import RelationType
from mautrix.util.ffmpeg import probe_bytes

from .media_models import Media, MediaMetadata


class MediaProcessor:
    def __init__(self, config, log):
        self.config = config
        self.log = log

    def _create_ytdlp_commands(self, url: str) -> List[dict]:
        commands = self.config.ytdlp.get("commands", [])
        if not commands:
            self.log.error(
                "MediaProcessor._create_ytdlp_commands: No yt-dlp commands configured."
            )
            return []

        command_list = []
        escaped_url = shlex.quote(url)

        for command_entry in commands:
            base_command = command_entry.get("command")
            if not base_command:
                self.log.warning(
                    f"MediaProcessor._create_ytdlp_commands: Command missing in {command_entry}"
                )
                continue

            command_list.append(
                {
                    "name": command_entry["name"],
                    "command": f"{base_command} {escaped_url}",
                }
            )

            for fallback in command_entry.get("fallback_commands", []):
                fallback_command = fallback.get("command")
                if fallback_command:
                    command_list.append(
                        {
                            "name": fallback["name"],
                            "command": f"{fallback_command} {escaped_url}",
                        }
                    )

        return command_list

    async def _run_ytdlp_commands(self, commands: List[dict]) -> Optional[dict]:
        for command_entry in commands:
            name = command_entry.get("name", "Unnamed Command")
            command = command_entry.get("command")

            if not command:
                self.log.warning(f"Skipping empty command entry: {name}")
                continue

            try:
                self.log.info(f"Running yt-dlp command: {name} → {command}")
                process = await asyncio.create_subprocess_shell(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                stdout, stderr = await process.communicate()

                if process.returncode != 0:
                    error_message = stderr.decode().strip()
                    self.log.warning(f"{name} failed: {error_message}")

                    if "403" in error_message or "404" in error_message:
                        self.log.error(
                            f"{name}: Non-retryable error detected. Stopping retries."
                        )
                        break

                    continue

                output = stdout.decode().strip()
                if not output:
                    self.log.warning(f"{name} produced empty output.")
                    continue

                return json.loads(output)

            except json.JSONDecodeError as e:
                self.log.error(f"{name} failed to parse JSON output: {e}")
            except ClientError as e:
                self.log.warning(f"{name} encountered a network error: {e}")
            except Exception as e:
                self.log.exception(f"{name} encountered an error: {e}")

        self.log.error("All yt-dlp commands (including fallbacks) failed.")
        return None

    async def _stream_to_memory(self, stream_url: str) -> BytesIO | None:
        video_data = BytesIO()
        max_retries = self.config.other.get("max_retries", 0)
        timeout = ClientTimeout(total=30)

        for attempt in range(1, max_retries + 1):
            try:
                async with ClientSession(timeout=timeout) as session:
                    async with session.get(stream_url) as response:
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

            except ClientError as e:
                self.log.warning(f"Attempt {attempt}: Network error: {e}")
            except asyncio.TimeoutError:
                self.log.warning(f"Attempt {attempt}: Timeout while streaming data.")
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
        return filename.rsplit(".", 1)[-1] if "." in filename else "jpg"

    async def process_url(self, url: str) -> Tuple[Optional[Media], Optional[Media]]:

        command = self._create_ytdlp_commands(url)
        if not command:
            self.log.warning("MediaHandler.process_url: Invalid command, check config.")
            raise Exception("No valid yt-dlp commands found in configuration.")

        video_ytdlp_metadata = await self._run_ytdlp_commands(command)
        if not video_ytdlp_metadata:
            self.log.warning(
                "MediaHandler.process_url: Failed to find video with yt_dlp"
            )
            raise Exception("Failed to retrieve metadata from yt-dlp.")

        is_live = video_ytdlp_metadata.get("is_live", False)
        if is_live and not self.config.other.get("enable_livestreams", False):
            self.log.warning(
                "MediaHandler.process_url: Live video detected, and livestreams are disabled. Stopping processing."
            )
            raise Exception("Livestream processing is disabled in configuration.")

        video_stream = None

        if not is_live:
            duration = video_ytdlp_metadata.get("duration")
            if duration is not None:
                if duration > self.config.other.get("max_duration", 0):
                    self.log.warning(
                        "MediaHandler.process_url: Video length exceeds the configured duration limit. Stopping processing."
                    )
                    raise Exception("Video duration exceeds the configured maximum.")
            else:
                self.log.warning(
                    "MediaHandler.process_url: Video duration is missing from metadata. Attempting to download the stream."
                )

            video_stream = await self._stream_to_memory(video_ytdlp_metadata["url"])
            if not video_stream:
                self.log.warning(
                    "MediaHandler.process_url: Failed to download video stream."
                )
                raise Exception("Failed to download video stream.")

        if not video_stream:
            raise Exception("Video stream is not available after processing.")

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
        filename = (
            unicodedata.normalize("NFKD", filename)
            .encode("ASCII", "ignore")
            .decode("ASCII")
        )
        # Add single quote (') and curly quotes if not normalized
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1F\'’“”]', "_", filename)
        # Replace spaces with underscores
        filename = re.sub(r"\s+", "_", filename)
        # Consolidate multiple underscores into a single underscore
        filename = re.sub(r"__+", "_", filename)
        # Remove leading and trailing underscores and dots, and limit length
        filename = filename.strip("_.")[:255]
        # Ensure no consecutive underscores remain
        filename = re.sub(r"_+", "_", filename)

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


class SynapseProcessor:
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
