import asyncio
import json
import os
import re
import shlex
import subprocess
import unicodedata
from io import BytesIO
from typing import List, Optional, Tuple, Union

from aiohttp import ClientError, ClientSession, ClientTimeout
from mautrix.types import RelationType
from mautrix.util.ffmpeg import probe_bytes

from .media_models import Media, MediaMetadata


class MediaProcessor:
    def __init__(self, config, log):
        self.config = config
        self.log = log

    def _create_ytdlp_commands(self, url: str) -> Tuple[List[dict], List[dict]]:
        commands = self.config.ytdlp.get("presets", [])
        if not commands:
            self.log.error(
                "MediaProcessor._create_ytdlp_commands: No yt-dlp commands configured."
            )
            return [], []

        download_commands = []
        query_commands = []
        escaped_url = shlex.quote(url)
        query_flags = "-s -j"
        output_arg = self.config.file.get("output_path", "-")

        for command_entry in commands:
            name = command_entry.get("name", "Unnamed Command")
            base_format = command_entry.get("format")

            if not base_format:
                self.log.warning(
                    f"MediaProcessor._create_ytdlp_commands: Format missing in command {name}"
                )
                continue

            query_commands.append(
                {
                    "name": name,
                    "command": f"yt-dlp -q --no-warnings {query_flags} -f '{base_format}' {escaped_url}",
                }
            )

            download_commands.append(
                {
                    "name": name,
                    "command": f"yt-dlp -q --no-warnings -f '{base_format}' -o '{output_arg}' {escaped_url}",
                }
            )

            for idx, fallback_format in enumerate(
                command_entry.get("fallback_formats", [])
            ):
                fallback_name = f"{name} (Fallback {idx + 1})"
                query_commands.append(
                    {
                        "name": fallback_name,
                        "command": f"yt-dlp -q --no-warnings {query_flags} -f '{fallback_format}' {escaped_url}",
                    }
                )
                download_commands.append(
                    {
                        "name": fallback_name,
                        "command": f"yt-dlp -q --no-warnings -f '{fallback_format}' -o '{output_arg}' {escaped_url}",
                    }
                )

        return query_commands, download_commands

    async def _ytdlp_execute_query(self, commands: List[dict]) -> Optional[dict]:
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

    async def _ytdlp_execute_download(
        self,
        commands: List[dict],
    ) -> Optional[Union[BytesIO, str]]:
        for command_entry in commands:
            name = command_entry.get("name", "Unnamed Command")
            command = command_entry.get("command")

            if not command:
                self.log.warning(f"Skipping empty download command: {name}")
                continue

            try:
                self.log.info(f"Running yt-dlp download: {name} → {command}")
                self.log.info(f"[DEBUG] Executing yt-dlp command: {command}")
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                stdout, stderr = await process.communicate()

                if process.returncode != 0:
                    error_message = (
                        stderr.decode().strip()
                        if stderr
                        else "No error message captured."
                    )
                    self.log.warning(f"{name} download failed: {error_message}")

                    if "403" in error_message or "404" in error_message:
                        self.log.error(
                            f"{name}: Non-retryable error detected. Stopping retries."
                        )
                        break
                    continue

                output_path = self.config.file.get("output_path", "-")

                if output_path == "-":
                    self.log.info("[DEBUG] Saving output to memory (BytesIO)")
                    self.log.info(
                        f"[DEBUG] Raw stdout type: {type(stdout)}, size: {len(stdout)} bytes"
                    )
                    video_data = BytesIO(stdout)
                    video_data.seek(0)

                    if video_data.getbuffer().nbytes == 0:
                        self.log.warning(f"{name}: Downloaded data is empty.")
                        continue

                    self.log.info(f"{name}: Video downloaded successfully into memory.")
                    self.log.info(
                        f"[DEBUG] BytesIO size: {video_data.getbuffer().nbytes} bytes"
                    )
                    return video_data

                if output_path and output_path != "-":
                    resolved_path = os.path.abspath(output_path)
                    if os.path.exists(resolved_path):
                        self.log.info(
                            f"{name}: Video downloaded successfully to '{resolved_path}'."
                        )
                        return resolved_path
                    else:
                        self.log.warning(
                            f"{name}: Expected output file '{resolved_path}' does not exist."
                        )
                        continue

            except asyncio.TimeoutError:
                self.log.warning(f"{name}: Download timed out.")
            except Exception as e:
                self.log.exception(f"{name}: An unexpected error occurred: {e}")

        self.log.error("All yt-dlp download commands (including fallbacks) failed.")
        return None

    async def _ffmpeg_livestream_capture(
        self, stream_url: str
    ) -> Optional[Union[BytesIO, str]]:
        output_path = self.config.file.get("output_path", "-")
        ffmpeg_args = self.config.ffmpeg.get("livestream_args", [])

        command_parts = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            shlex.quote(stream_url),
        ]
        command_parts.extend(ffmpeg_args)

        if output_path == "-":
            if not command_parts or command_parts[-1] != "pipe:1":
                command_parts.append("pipe:1")

            self.log.info(
                f"[INFO] Running FFmpeg (in-memory) with args:\n{' '.join(command_parts)}"
            )

            process = await asyncio.create_subprocess_shell(
                " ".join(command_parts),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            return_code = process.returncode

            if return_code != 0:
                self.log.error(f"FFmpeg exited with code {return_code}.")
                if stderr:
                    self.log.error(f"[FFmpeg stderr]\n{stderr.decode(errors='ignore')}")
                return None

            if not stdout:
                self.log.error("[ERROR] FFmpeg returned empty data.")
                return None

            video_data = BytesIO(stdout)
            video_data.seek(0)
            self.log.info(
                f"[INFO] Captured {len(stdout)} bytes from the stream as a fragmented MP4 (in-memory)."
            )
            return video_data

        else:
            if command_parts and command_parts[-1] == "pipe:1":
                command_parts.pop()

            resolved_path = os.path.abspath(output_path)
            command_parts.append("-y")
            command_parts.append(shlex.quote(resolved_path))

            self.log.info(
                f"[INFO] Running FFmpeg (file-output) with args:\n{' '.join(command_parts)}"
            )

            process = await asyncio.create_subprocess_shell(
                " ".join(command_parts),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            return_code = process.returncode

            if return_code != 0:
                self.log.error(f"FFmpeg exited with code {return_code}.")
                if stderr:
                    self.log.error(f"[FFmpeg stderr]\n{stderr.decode(errors='ignore')}")
                return None

            if not os.path.exists(resolved_path):
                self.log.error(
                    f"[ERROR] FFmpeg did not produce the file: {resolved_path}"
                )
                return None

            file_size = os.path.getsize(resolved_path)
            if file_size == 0:
                self.log.error(f"[ERROR] FFmpeg output file is empty: {resolved_path}")
                return None

            self.log.info(
                f"[INFO] Captured ~{file_size} bytes from the stream as a fragmented MP4 file:\n{resolved_path}"
            )
            return resolved_path

    async def _stream_to_memory(self, stream_url: str) -> BytesIO | None:
        video_data = BytesIO()
        max_retries = self.config.file.get("max_retries", 0)
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

                            if total_bytes > self.config.file.get(
                                "max_stream_size", 10485760
                            ):
                                self.log.warning(
                                    f"Stream exceeded max size limit of {self.config.file.get('max_stream_size', 10485760)} bytes. Aborting."
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
            max_file_size = self.config.file.get("max_in_memory_file_size", 0)
            if len(data) > max_file_size:
                self.log.warning(
                    f"MediaProcessor._get_stream_metadata: File size exceeds max_file_size ({max_file_size} bytes)."
                )
                return None

            self.log.info(f"[DEBUG] Data size: {len(data)} bytes")

            metadata = await probe_bytes(data)
            self.log.info(f"[DEBUG] Raw metadata from probe_bytes: {metadata}")

            if "streams" not in metadata or not metadata["streams"]:
                self.log.warning(f"[DEBUG] No streams found in metadata: {metadata}")
                return None

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
                    f"MediaProcessor._get_stream_metadata: No video stream found in metadata."
                )
                return None

            duration = float(stream.get("duration", 0) or 0)
            self.log.info(f"[DEBUG] Stream duration: {duration}")

            if duration > self.config.file.get("max_duration", 0):
                self.log.warning(
                    f"MediaProcessor._get_stream_metadata: Duration exceeds max_duration ({self.config.file.get('max_duration')}s)."
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

        commands = self._create_ytdlp_commands(url)
        if not commands:
            self.log.warning("MediaHandler.process_url: Invalid command, check config.")
            raise Exception("No valid yt-dlp commands found in configuration.")

        query_commands = commands[0]
        download_commands = commands[1]

        video_ytdlp_metadata = await self._ytdlp_execute_query(commands=query_commands)
        if not video_ytdlp_metadata:
            self.log.warning(
                "MediaHandler.process_url: Failed to find video with yt_dlp"
            )
            raise Exception("Failed to retrieve metadata from yt-dlp.")

        is_live = video_ytdlp_metadata.get("is_live", False)
        video_stream = None
        if is_live and not self.config.ffmpeg.get("enable_livestream_previews", False):
            self.log.warning(
                "MediaHandler.process_url: Live video detected, and livestreams are disabled. Stopping processing."
            )
            raise Exception("Livestream processing is disabled in configuration.")
        elif is_live:
            video_stream = await self._ffmpeg_livestream_capture(
                stream_url=video_ytdlp_metadata["url"]
            )
            if not video_stream:
                self.log.warning(
                    "MediaHandler.process_url: Failed to download video stream."
                )
                raise Exception("Failed to download live stream.")
        elif not is_live:
            duration = video_ytdlp_metadata.get("duration")
            if duration is not None:
                if duration > self.config.file.get("max_duration", 0):
                    self.log.warning(
                        "MediaHandler.process_url: Video length exceeds the configured duration limit. Stopping processing."
                    )
                    raise Exception("Video duration exceeds the configured maximum.")
            else:
                self.log.warning(
                    "MediaHandler.process_url: Video duration is missing from metadata. Attempting to download the stream."
                )

            video_stream = await self._ytdlp_execute_download(
                commands=download_commands
            )

        if not video_stream:
            self.log.warning(
                "MediaHandler.process_url: Failed to download video stream."
            )
            raise Exception("Failed to download video stream.")

        if isinstance(video_stream, BytesIO):
            video_stream_metadata = (
                await self._get_stream_metadata(video_stream.getvalue()) or {}
            )
        else:
            raise Exception("Downloading outside of stdout is not yet supported.")

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
            f"- Size {video.metadata.size}"
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
