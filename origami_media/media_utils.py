from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import shlex
import subprocess
import unicodedata
import uuid
from io import BytesIO
from typing import TYPE_CHECKING, Any, AsyncIterable, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

from aiohttp import ClientError, ClientSession, ClientTimeout
from mautrix.types import ReactionEvent, RelationType
from mautrix.util.ffmpeg import probe_bytes

from .media_models import Media, MediaFile, MediaInfo

if TYPE_CHECKING:
    from main import Config
    from maubot.matrix import MaubotMatrixClient, MaubotMessageEvent
    from mautrix.types import EventID, RoomID
    from mautrix.util.logging.trace import TraceLogger


class MediaProcessor:
    def __init__(self, config: "Config", log: "TraceLogger"):
        self.config = config
        self.log = log

    def _is_image_magic_number(self, data: bytes) -> bool:
        """
        Check the first few bytes of a file for common image format signatures.
        """
        if data.startswith(b"\xFF\xD8\xFF"):  # JPEG
            return True
        if data.startswith(b"\x89PNG\r\n\x1a\n"):  # PNG
            return True
        if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):  # GIF
            return True
        if data.startswith(b"\x49\x49\x2A\x00") or data.startswith(
            b"\x4D\x4D\x00\x2A"
        ):  # TIFF
            return True
        if data.startswith(b"\x42\x4D"):  # BMP
            return True
        if data.startswith(b"\x00\x00\x01\x00") or data.startswith(
            b"\x00\x00\x02\x00"
        ):  # ICO
            return True
        if data.startswith(b"RIFF") and b"WEBP" in data:  # WEBP
            return True
        if data.startswith(b"\x1A\x45\xDF\xA3"):  # WebM (EBML Header)
            return True

        return False

    async def _is_image(self, url: str) -> bool:
        try:
            timeout = ClientTimeout(total=10)
            async with ClientSession(timeout=timeout) as session:
                async with session.head(url, allow_redirects=True) as response:
                    ctype = response.headers.get("Content-Type", "").lower()
                    self.log.debug(
                        f"MediaProcessor._is_image: HEAD Content-Type for {url}: {ctype}"
                    )
                    if ctype.startswith("image/"):
                        return True

                async with session.get(url, allow_redirects=True) as response:
                    ctype = response.headers.get("Content-Type", "").lower()
                    if ctype.startswith("image/"):
                        return True

                    first_bytes = await response.content.read(12)
                    if self._is_image_magic_number(first_bytes):
                        return True

        except Exception as e:
            self.log.warning(
                f"MediaProcessor._is_image: Failed to determine if URL is an image ({url}): {e}"
            )
            return False

        return False

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
                self.log.warning(
                    f"MediaProcessor._ytdlp_execute_query: Skipping empty command entry: {name}"
                )
                continue

            try:
                self.log.info(
                    f"MediaProcessor._ytdlp_execute_query: Running yt-dlp command: {name} → {command}"
                )
                process = await asyncio.create_subprocess_shell(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=100
                )

                if process.returncode != 0:
                    error_message = stderr.decode().strip()
                    self.log.warning(
                        f"MediaProcessor._ytdlp_execute_query: {name} failed: {error_message}"
                    )

                    if "403" in error_message or "404" in error_message:
                        self.log.error(
                            f"MediaProcessor._ytdlp_execute_query: {name}: Non-retryable error detected. Stopping retries."
                        )
                        break

                    continue

                output = stdout.decode().strip()
                if not output:
                    self.log.warning(
                        f"MediaProcessor._ytdlp_execute_query: {name} produced empty output."
                    )
                    continue

                return json.loads(output)

            except json.JSONDecodeError as e:
                self.log.error(
                    f"MediaProcessor._ytdlp_execute_query: {name} failed to parse JSON output: {e}"
                )
            except asyncio.TimeoutError:
                self.log.error(
                    f"MediaProcessor._ytdlp_execute_query: {name}: Command timed out. Killing process."
                )
                process.kill()
                await process.wait()
            except ClientError as e:
                self.log.warning(
                    f"MediaProcessor._ytdlp_execute_query: {name} encountered a network error: {e}"
                )
            except Exception as e:
                self.log.exception(
                    f"MediaProcessor._ytdlp_execute_query: {name} encountered an error: {e}"
                )

        self.log.error(
            "MediaProcessor._ytdlp_execute_query: All yt-dlp commands (including fallbacks) failed."
        )
        return None

    async def _ytdlp_execute_download(
        self, commands: List[dict]
    ) -> Optional[Union[BytesIO, str]]:
        for command_entry in commands:
            name = command_entry.get("name", "Unnamed Command")
            command = command_entry.get("command")

            if not command:
                self.log.warning(
                    f"MediaProcessor._ytdlp_execute_download: Skipping empty download command: {name}"
                )
                continue

            try:
                self.log.info(
                    f"MediaProcessor._ytdlp_execute_download: Executing yt-dlp download command: {name} → {command}"
                )

                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                if process.stdout is None:
                    self.log.error(
                        f"MediaProcessor._ytdlp_execute_download: {name}: Process stdout is None, cannot proceed with download."
                    )
                    await process.wait()
                    return None

                video_data = BytesIO()
                total_size = 0
                output_path = self.config.file.get("output_path", "-")
                in_memory = output_path == "-"

                max_file_size_key = (
                    "max_in_memory_file_size" if in_memory else "max_file_size"
                )
                max_file_size = self.config.file.get(max_file_size_key, 0)

                while True:
                    chunk = await asyncio.wait_for(
                        process.stdout.read(1024 * 64), timeout=30
                    )
                    if not chunk:
                        break

                    chunk_size = len(chunk)
                    total_size += chunk_size

                    if max_file_size > 0 and total_size > max_file_size:
                        self.log.error(
                            f"MediaProcessor._ytdlp_execute_download: {name}: File size limit exceeded ({total_size} > {max_file_size} bytes)."
                        )
                        process.kill()
                        await process.wait()
                        return None

                    video_data.write(chunk)

                _, stderr = await process.communicate()

                if process.returncode != 0:
                    error_message = (
                        stderr.decode().strip()
                        if stderr
                        else "No error message captured."
                    )
                    self.log.warning(
                        f"MediaProcessor._ytdlp_execute_download: {name} download failed: {error_message}"
                    )

                    if "403" in error_message or "404" in error_message:
                        self.log.error(
                            f"MediaProcessor._ytdlp_execute_download: {name}: Non-retryable error detected. Stopping retries."
                        )
                        break
                    continue

                if total_size == 0:
                    self.log.warning(
                        f"MediaProcessor._ytdlp_execute_download: {name}: Downloaded data is empty."
                    )
                    return None

                video_data.seek(0)

                if in_memory:
                    self.log.info(
                        "MediaProcessor._ytdlp_execute_download: Saving output to memory (BytesIO)"
                    )
                    self.log.info(
                        f"MediaProcessor._ytdlp_execute_download: Final BytesIO size: {video_data.getbuffer().nbytes} bytes"
                    )
                    return video_data

                if output_path and output_path != "-":
                    resolved_path = os.path.abspath(output_path)
                    if os.path.exists(resolved_path):
                        self.log.info(
                            f"MediaProcessor._ytdlp_execute_download: {name}: Video downloaded successfully to '{resolved_path}'."
                        )
                        return resolved_path
                    else:
                        self.log.warning(
                            f"MediaProcessor._ytdlp_execute_download: {name}: Expected output file '{resolved_path}' does not exist."
                        )
                        continue

            except asyncio.TimeoutError:
                self.log.warning(
                    f"MediaProcessor._ytdlp_execute_download: {name}: Download timed out."
                )
            except Exception as e:
                self.log.exception(
                    f"MediaProcessor._ytdlp_execute_download: {name}: An unexpected error occurred: {e}"
                )
            finally:
                if process.returncode is None:
                    self.log.warning(
                        f"MediaProcessor._ytdlp_execute_download: {name}: Process still running. Forcing termination."
                    )
                    process.kill()
                    await process.wait()

        self.log.error(
            "MediaProcessor._ytdlp_execute_download: All yt-dlp download commands (including fallbacks) failed."
        )
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

        in_memory = output_path == "-"
        max_file_size_key = "max_in_memory_file_size" if in_memory else "max_file_size"
        max_file_size = self.config.file.get(max_file_size_key, 0)

        if in_memory:
            if not command_parts or command_parts[-1] != "pipe:1":
                command_parts.append("pipe:1")

            self.log.info(
                f"MediaProcessor._ffmpeg_livestream_capture: Running FFmpeg (in-memory) with args:\n{' '.join(command_parts)}"
            )

            process = await asyncio.create_subprocess_shell(
                " ".join(command_parts),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            if process.stdout is None:
                self.log.error(
                    "MediaProcessor._ffmpeg_livestream_capture: FFmpeg stdout is None, cannot proceed with in-memory capture."
                )
                await process.wait()
                return None

            video_data = BytesIO()
            total_size = 0

            try:
                while True:
                    chunk = await asyncio.wait_for(
                        process.stdout.read(1024 * 64), timeout=30
                    )
                    if not chunk:
                        break

                    chunk_size = len(chunk)
                    total_size += chunk_size

                    if max_file_size > 0 and total_size > max_file_size:
                        self.log.error(
                            f"MediaProcessor._ffmpeg_livestream_capture: Stream size exceeded limit ({total_size} > {max_file_size} bytes). Terminating FFmpeg."
                        )
                        process.kill()
                        await process.wait()
                        return None

                    video_data.write(chunk)

                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=300
                    )
                return_code = process.returncode

                if return_code != 0:
                    self.log.error(
                        f"MediaProcessor._ffmpeg_livestream_capture: FFmpeg exited with code {return_code}."
                    )
                    if stderr:
                        self.log.error(
                            f"MediaProcessor._ffmpeg_livestream_capture: [FFmpeg stderr]\n{stderr.decode(errors='ignore')}"
                        )
                    return None

                if total_size == 0:
                    self.log.error(
                        "MediaProcessor._ffmpeg_livestream_capture: FFmpeg returned empty data."
                    )
                    return None

                video_data.seek(0)
                self.log.info(
                    f"MediaProcessor._ffmpeg_livestream_capture: Captured {total_size} bytes from the stream as a fragmented MP4 (in-memory)."
                )
                return video_data

            except asyncio.TimeoutError:
                self.log.error(
                    "MediaProcessor._ffmpeg_livestream_capture: FFmpeg stream timed out."
                )
                await process.wait()
                return None
            except Exception as e:
                self.log.exception(
                    f"MediaProcessor._ffmpeg_livestream_capture: Unexpected error during FFmpeg capture: {e}"
                )
                await process.wait()
                return None
            finally:
                if process and process.returncode is None:
                    self.log.warning(
                        "MediaProcessor._ffmpeg_livestream_capture: FFmpeg process still running. Forcing termination."
                    )
                    process.kill()
                    await process.wait()

        else:
            if command_parts and command_parts[-1] == "pipe:1":
                command_parts.pop()

            resolved_path = os.path.abspath(output_path)
            command_parts.append("-y")
            command_parts.append(shlex.quote(resolved_path))

            self.log.info(
                f"MediaProcessor._ffmpeg_livestream_capture: Running FFmpeg (file-output) with args:\n{' '.join(command_parts)}"
            )

            process = await asyncio.create_subprocess_shell(
                " ".join(command_parts),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
            return_code = process.returncode

            if return_code != 0:
                self.log.error(
                    f"MediaProcessor._ffmpeg_livestream_capture: FFmpeg exited with code {return_code}."
                )
                if stderr:
                    self.log.error(
                        f"MediaProcessor._ffmpeg_livestream_capture: [FFmpeg stderr]\n{stderr.decode(errors='ignore')}"
                    )
                return None

            if not os.path.exists(resolved_path):
                self.log.error(
                    f"MediaProcessor._ffmpeg_livestream_capture: FFmpeg did not produce the file: {resolved_path}"
                )
                return None

            file_size = os.path.getsize(resolved_path)
            if file_size == 0:
                self.log.error(
                    f"MediaProcessor._ffmpeg_livestream_capture: FFmpeg output file is empty: {resolved_path}"
                )
                return None

            if max_file_size > 0 and file_size > max_file_size:
                self.log.error(
                    f"MediaProcessor._ffmpeg_livestream_capture: File size limit exceeded ({file_size} > {max_file_size} bytes)."
                )
                return None

            self.log.info(
                f"MediaProcessor._ffmpeg_livestream_capture: Captured ~{file_size} bytes from the stream as a fragmented MP4 file:\n{resolved_path}"
            )
            return resolved_path

    async def _download_image(self, stream_url: str) -> Optional[Union[BytesIO, str]]:
        output_path = self.config.file.get("output_path", "-")
        max_retries = self.config.file.get("max_retries", 3)
        timeout = ClientTimeout(total=30)
        in_memory = output_path == "-"

        max_file_size_key = "max_in_memory_file_size" if in_memory else "max_file_size"
        max_file_size = self.config.file.get(max_file_size_key, 0)

        self.log.info(
            f"MediaProcessor._download_image: Starting stream from '{stream_url}' to {'memory' if in_memory else output_path}. "
            f"MediaProcessor._download_image: Max size limit: {max_file_size} bytes"
        )

        for attempt in range(1, max_retries + 1):
            try:
                async with ClientSession(timeout=timeout) as session:
                    async with session.get(stream_url) as response:
                        if response.status != 200:
                            self.log.warning(
                                f"MediaProcessor._download_image: Attempt {attempt}: Failed to fetch stream, status: {response.status}"
                            )
                            continue

                        total_bytes = 0

                        if in_memory:
                            output = BytesIO()
                        else:
                            resolved_path = os.path.abspath(output_path)
                            with open(resolved_path, "wb") as output:
                                async for chunk in response.content.iter_chunked(8192):
                                    chunk_size = len(chunk)
                                    total_bytes += chunk_size

                                    if (
                                        max_file_size > 0
                                        and total_bytes > max_file_size
                                    ):
                                        self.log.error(
                                            f"MediaProcessor._download_image: Stream size exceeded limit ({total_bytes} > {max_file_size} bytes). Aborting."
                                        )
                                        return None

                                    output.write(chunk)

                                self.log.info(
                                    f"MediaProcessor._download_image: Streamed {total_bytes} bytes into file: {resolved_path}"
                                )
                                return resolved_path

                        async for chunk in response.content.iter_chunked(8192):
                            chunk_size = len(chunk)
                            total_bytes += chunk_size

                            if max_file_size > 0 and total_bytes > max_file_size:
                                self.log.error(
                                    f"MediaProcessor._download_image: Stream size exceeded limit ({total_bytes} > {max_file_size} bytes). Aborting."
                                )
                                return None

                            output.write(chunk)

                        output.seek(0)
                        self.log.info(
                            f"MediaProcessor._download_image: Streamed {total_bytes} bytes into memory."
                        )
                        return output

            except ClientError as e:
                self.log.warning(
                    f"MediaProcessor._download_image: Attempt {attempt}: Network error: {e}"
                )
            except asyncio.TimeoutError:
                self.log.warning(
                    f"MediaProcessor._download_image: Attempt {attempt}: Timeout while streaming data."
                )
            except Exception as e:
                self.log.warning(
                    f"MediaProcessor._download_image: Attempt {attempt}: Error streaming data: {e}"
                )

            await asyncio.sleep(1)

        self.log.error(
            f"MediaProcessor._download_image: Failed to stream data after {max_retries} attempts."
        )
        return None

    async def _get_stream_metadata(self, data: bytes) -> Optional[Dict[str, Any]]:
        try:
            max_file_size = self.config.file.get("max_in_memory_file_size", 0)
            if len(data) > max_file_size:
                self.log.warning(
                    f"MediaProcessor._get_stream_metadata: "
                    f"File size exceeds max_in_memory_file_size ({max_file_size} bytes)."
                )
                return None

            self.log.info(
                f"MediaProcessor._get_stream_metadata: Data size: {len(data)} bytes"
            )

            metadata = await probe_bytes(data)
            self.log.info(
                f"MediaProcessor._get_stream_metadata: Raw metadata from probe_bytes: {metadata}"
            )

            format_info = metadata.get("format", {})
            format_name = format_info.get("format_name", "").lower()

            image_formats = (
                "image2",
                "png_pipe",
                "mjpeg",
                "gif",
                "png",
                "jpeg",
                "bmp",
                "tiff",
            )

            if any(img_keyword in format_name for img_keyword in image_formats):
                self.log.debug(
                    f"MediaProcessor._get_stream_metadata: Detected image format: {format_name}"
                )

                width = format_info.get("width")
                height = format_info.get("height")

                if not width or not height:
                    first_stream = metadata.get("streams", [{}])[0]
                    width = first_stream.get("width")
                    height = first_stream.get("height")

                if isinstance(width, str):
                    try:
                        width = int(width)
                    except ValueError:
                        width = None
                if isinstance(height, str):
                    try:
                        height = int(height)
                    except ValueError:
                        height = None

                return {
                    "width": width,
                    "height": height,
                    "duration": 0.0,
                    "size": len(data),
                    "has_video": False,
                    "has_audio": False,
                    "is_image": True,
                }

            if "streams" not in metadata or not metadata["streams"]:
                self.log.warning(
                    "MediaProcessor._get_stream_metadata: No streams found and not recognized as image."
                )
                return None

            video_stream = next(
                (s for s in metadata["streams"] if s.get("codec_type") == "video"), None
            )
            audio_stream = next(
                (s for s in metadata["streams"] if s.get("codec_type") == "audio"), None
            )

            if not video_stream and not audio_stream:
                self.log.warning(
                    "MediaProcessor._get_stream_metadata: No video or audio stream found."
                )
                return None

            has_video = bool(video_stream)
            has_audio = bool(audio_stream)

            width = video_stream.get("width") if has_video else None
            height = video_stream.get("height") if has_video else None

            if isinstance(width, str):
                try:
                    width = int(width)
                except ValueError:
                    width = None
            if isinstance(height, str):
                try:
                    height = int(height)
                except ValueError:
                    height = None

            active_stream = video_stream if has_video else audio_stream
            assert (
                active_stream is not None
            ), "MediaProcessor._get_stream_metadata: Active stream must not be None at this point."
            duration_str = active_stream.get("duration")
            if not duration_str or duration_str in ("N/A", ""):
                duration = 0.0
            else:
                try:
                    duration = float(duration_str)
                except ValueError:
                    self.log.info(
                        f"MediaProcessor._get_stream_metadata: Non-numeric duration '{duration_str}' detected. Defaulting to 0.0"
                    )
                    duration = 0.0

            self.log.info(
                f"MediaProcessor._get_stream_metadata: Stream duration: {duration}"
            )

            if duration > self.config.file.get("max_duration", 0):
                self.log.warning(
                    f"MediaProcessor._get_stream_metadata: Duration exceeds max_duration "
                    f"({self.config.file.get('max_duration')}s)."
                )
                return None

            return {
                "width": width,
                "height": height,
                "duration": duration,
                "size": len(data),
                "has_video": has_video,
                "has_audio": has_audio,
                "is_image": False,
            }
        except Exception as e:
            self.log.exception(f"MediaProcessor._get_stream_metadata: {e}")
            return None

    def _generate_filename(
        self, url, ytdlp_metadata: Optional[Dict[str, str]] = None
    ) -> str:
        if ytdlp_metadata:
            filename = "{title}-{uploader}-{extractor}-{id}.{ext}".format(
                title=ytdlp_metadata.get("title", "unknown_title"),
                uploader=ytdlp_metadata.get("uploader", "unknown_uploader"),
                extractor=ytdlp_metadata.get("extractor", "unknown_platform"),
                id=ytdlp_metadata.get("id", "unknown_id"),
                ext=ytdlp_metadata.get("ext", "unknown_extension"),
            )
        else:
            filename = url.rsplit("/", 1)[-1] if "/" in url else url

        # Normalize Unicode
        filename = (
            unicodedata.normalize("NFKD", filename)
            .encode("ASCII", "ignore")
            .decode("ASCII")
        )
        # Replace invalid characters
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1F\'’“”]', "_", filename)
        # Replace spaces with underscores
        filename = re.sub(r"\s+", "_", filename)
        # Replace multiple underscores with a single one
        filename = re.sub(r"__+", "_", filename)
        # Trim leading and trailing dots/underscores and enforce max length
        filename = filename.strip("_.")[:255]
        # Final cleanup of redundant underscores
        filename = re.sub(r"_+", "_", filename)

        return filename

    async def _handle_image(self, url: str) -> Optional[MediaFile]:
        image_data = await self._download_image(url)
        if not image_data:
            self.log.warning(
                f"MediaHandler._handle_image: Failed to fetch image from {url}"
            )
            return None

        if isinstance(image_data, BytesIO):
            image_metadata = (
                await self._get_stream_metadata(image_data.getvalue()) or {}
            )
        else:
            self.log.warning(
                "MediaHandler._handle_image: Downloading outside of stdout is not yet supported."
            )
            return None

        filename = self._generate_filename(url)
        extension = filename.rsplit(".", 1)[-1] if "." in filename else "jpg"

        parsed_url = urlparse(url)
        domain = parsed_url.netloc.split(":")[0]

        media_object = MediaFile(
            filename=filename,
            stream=image_data,
            metadata=MediaInfo(
                url=url,
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, url)),
                title=url,
                uploader="Unknown Uploader",
                ext=extension,
                extractor=domain,
                duration=image_metadata.get("duration", 0),
                width=image_metadata.get("width", 0),
                height=image_metadata.get("height", 0),
                size=image_metadata.get("size", 0),
                has_video=image_metadata.get("has_video", False),
                has_audio=image_metadata.get("has_audio", False),
                is_image=image_metadata.get("is_image", True),
            ),
        )

        return media_object

    async def _handle_non_image(self, url):
        commands = self._create_ytdlp_commands(url)
        if not commands:
            self.log.warning(
                "MediaHandler._handle_non_image: No valid yt-dlp commands found in configuration."
            )
            return None

        query_commands, download_commands = commands[0], commands[1]

        ytdlp_metadata = await self._ytdlp_execute_query(commands=query_commands)
        if not ytdlp_metadata:
            self.log.warning(
                "MediaHandler._handle_non_image: Failed to retrieve metadata from yt-dlp."
            )
            return None

        is_live = ytdlp_metadata.get("is_live", False)
        media_stream_data: Optional[Union[BytesIO, str]] = None

        if is_live and not self.config.ffmpeg.get("enable_livestream_previews", False):
            self.log.warning(
                "MediaHandler._handle_non_image: Live media detected, but livestream previews are disabled."
            )
            return None

        elif is_live:
            media_stream_data = await self._ffmpeg_livestream_capture(
                stream_url=ytdlp_metadata["url"]
            )
            if not media_stream_data:
                self.log.warning(
                    "MediaHandler._handle_non_image: Failed to download live stream."
                )
                return None

        else:
            duration = ytdlp_metadata.get("duration")
            if duration is not None:
                if duration > self.config.file.get("max_duration", 0):
                    self.log.warning(
                        "MediaHandler._handle_non_image: Media length exceeds the configured duration limit."
                    )
                    return None
            else:
                self.log.warning(
                    "MediaHandler._handle_non_image: Media duration is missing from metadata. "
                    "Attempting to download the stream anyway."
                )

            media_stream_data = await self._ytdlp_execute_download(
                commands=download_commands
            )

        if not media_stream_data:
            self.log.warning(
                "MediaHandler._handle_non_image: Failed to download media stream."
            )
            return None

        if isinstance(media_stream_data, BytesIO):
            media_stream_metadata = (
                await self._get_stream_metadata(media_stream_data.getvalue()) or {}
            )
        else:
            self.log.warning(
                "MediaHandler._handle_non_image: Downloading outside of stdout is not yet supported."
            )
            return None

        filename = self._generate_filename(url, ytdlp_metadata=ytdlp_metadata)

        media_object = MediaFile(
            filename=filename,
            stream=media_stream_data,
            metadata=MediaInfo(
                url=ytdlp_metadata["url"],
                id=ytdlp_metadata.get("id"),
                thumbnail_url=ytdlp_metadata.get("thumbnail", None),
                title=ytdlp_metadata.get("title", url),
                uploader=ytdlp_metadata.get("uploader", "unknown_uploader"),
                ext=ytdlp_metadata.get("ext", "mp4"),
                extractor=ytdlp_metadata.get("extractor"),
                duration=media_stream_metadata.get("duration", 0),
                width=media_stream_metadata.get("width", 0),
                height=media_stream_metadata.get("height", 0),
                size=media_stream_metadata.get("size", 0),
                has_video=media_stream_metadata.get("has_video", False),
                has_audio=media_stream_metadata.get("has_audio", False),
                is_image=media_stream_metadata.get("is_image", False),
            ),
        )

        return media_object

    async def process_url(self, url: str) -> Optional[Media]:
        parsed_url = urlparse(url)
        domain = parsed_url.netloc.split(":")[0]

        primary_file_object = None
        thumbnail_file_object = None

        try:
            if self._is_image(url):
                self.log.info(
                    "MediaProcessor.process_url: Detected image URL. Processing as image."
                )
                primary_file_object = await self._handle_image(url)
            else:
                self.log.info(
                    "MediaProcessor.process_url: Detected non-image URL. Processing as non-image."
                )
                primary_file_object = await self._handle_non_image(url)

            if primary_file_object:
                primary_file_object.metadata.extractor = domain

            if primary_file_object and primary_file_object.metadata.thumbnail_url:
                thumbnail_file_object = await self._handle_image(
                    primary_file_object.metadata.thumbnail_url
                )
                if thumbnail_file_object:
                    ext = (
                        thumbnail_file_object.metadata.ext
                        if thumbnail_file_object.metadata.ext
                        else "jpg"
                    )
                    thumbnail_file_object.filename = (
                        f"{primary_file_object.filename}.{ext}"
                    )
                    thumbnail_file_object.metadata.uploader = (
                        primary_file_object.metadata.uploader
                    )
                    thumbnail_file_object.metadata.id = primary_file_object.metadata.id
                else:
                    self.log.warning(
                        "MediaProcessor.process_url: Failed to fetch thumbnail metadata."
                    )

        except Exception as e:
            self.log.warning(
                f"MediaProcessor.process_url: Failed to process URL: {url}. Error: {e}"
            )
            return None

        if not primary_file_object:
            self.log.warning("MediaProcessor.process_url: Failed to download file.")
            return None

        if primary_file_object.metadata.has_video:
            media_type = "Video"
        elif primary_file_object.metadata.has_audio:
            media_type = "Audio"
        elif primary_file_object.metadata.is_image:
            media_type = "Image"
        else:
            media_type = "Unknown"

        self.log.info(
            f"Media Found:\n- Filename: {primary_file_object.filename}\n"
            f"- Size: {primary_file_object.metadata.size} bytes\n"
            f"- Media type: {media_type}\n"
            f"- Website: {domain}"
        )

        return Media(content=primary_file_object, thumbnail=thumbnail_file_object)


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

    async def _handle_async_upload(self, data: BytesIO, filename: str, size: int):
        upload_data = self._bytes_io_to_async_iter(data)
        task = asyncio.create_task(
            self.client.upload_media(
                data=upload_data,
                filename=filename,
                size=size,
                async_upload=True,
            )
        )
        self.log.info(
            f"SynapseProcessor._handle_async_upload: Async upload initiated for {filename} (size: {size} bytes)"
        )
        return task

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
        async_upload_enabled = self.config.file.get("async_upload", False)
        async_upload_required = size > 10 * 1024 * 1024

        if async_upload_enabled and async_upload_required:
            return await self._handle_async_upload(data, filename, size)

        return await self._handle_sync_upload(data, filename, size)
