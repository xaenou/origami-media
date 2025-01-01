import asyncio
import json
import os
import re
import shlex
import subprocess
import unicodedata
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple, Union

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
        self, commands: List[dict]
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

                if process.stdout is None:
                    self.log.error(
                        f"{name}: Process stdout is None, cannot proceed with download."
                    )
                    await process.wait()
                    return None

                video_data = BytesIO()
                total_size = 0
                output_path = self.config.file.get("output_path", "-")
                in_memory = output_path == "-"

                if in_memory:
                    max_file_size = self.config.file.get("max_in_memory_file_size", 0)
                else:
                    max_file_size = self.config.file.get("max_file_size", 0)

                while True:
                    chunk = await process.stdout.read(1024 * 64)
                    if not chunk:
                        break

                    chunk_size = len(chunk)
                    total_size += chunk_size

                    if max_file_size > 0 and total_size > max_file_size:
                        self.log.error(
                            f"{name}: File size limit exceeded ({total_size} > {max_file_size} bytes)."
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
                    self.log.warning(f"{name} download failed: {error_message}")

                    if "403" in error_message or "404" in error_message:
                        self.log.error(
                            f"{name}: Non-retryable error detected. Stopping retries."
                        )
                        break
                    continue

                if total_size == 0:
                    self.log.warning(f"{name}: Downloaded data is empty.")
                    return None

                video_data.seek(0)

                if in_memory:
                    self.log.info("[DEBUG] Saving output to memory (BytesIO)")
                    self.log.info(
                        f"[DEBUG] Final BytesIO size: {video_data.getbuffer().nbytes} bytes"
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

        in_memory = output_path == "-"
        if in_memory:
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

            if process.stdout is None:
                self.log.error(
                    "[ERROR] FFmpeg stdout is None, cannot proceed with in-memory capture."
                )
                await process.wait()
                return None

            video_data = BytesIO()
            total_size = 0
            max_file_size = self.config.file.get("max_in_memory_file_size", 10485760)

            try:
                while True:
                    chunk = await process.stdout.read(1024 * 64)
                    if not chunk:
                        break

                    chunk_size = len(chunk)
                    total_size += chunk_size

                    if max_file_size > 0 and total_size > max_file_size:
                        self.log.error(
                            f"[ERROR] Stream size exceeded limit ({total_size} > {max_file_size} bytes). Terminating FFmpeg."
                        )
                        process.kill()
                        await process.wait()
                        return None

                    video_data.write(chunk)

                _, stderr = await process.communicate()
                return_code = process.returncode

                if return_code != 0:
                    self.log.error(f"FFmpeg exited with code {return_code}.")
                    if stderr:
                        self.log.error(
                            f"[FFmpeg stderr]\n{stderr.decode(errors='ignore')}"
                        )
                    return None

                if total_size == 0:
                    self.log.error("[ERROR] FFmpeg returned empty data.")
                    return None

                video_data.seek(0)
                self.log.info(
                    f"[INFO] Captured {total_size} bytes from the stream as a fragmented MP4 (in-memory)."
                )
                return video_data

            except asyncio.TimeoutError:
                self.log.error("[ERROR] FFmpeg stream timed out.")
                await process.wait()
                return None
            except Exception as e:
                self.log.exception(
                    f"[ERROR] Unexpected error during FFmpeg capture: {e}"
                )
                await process.wait()
                return None

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

            max_file_size = self.config.file.get(
                "max_file_size", 104857600
            )  # Default 100MB
            if max_file_size > 0 and file_size > max_file_size:
                self.log.error(
                    f"[ERROR] File size limit exceeded ({file_size} > {max_file_size} bytes)."
                )
                return None

            self.log.info(
                f"[INFO] Captured ~{file_size} bytes from the stream as a fragmented MP4 file:\n{resolved_path}"
            )
            return resolved_path

    async def _download_image(self, stream_url: str) -> Optional[Union[BytesIO, str]]:
        output_path = self.config.file.get("output_path", "-")
        max_retries = self.config.file.get("max_retries", 3)
        timeout = ClientTimeout(total=30)
        in_memory = output_path == "-"

        max_file_size = (
            self.config.file.get("max_in_memory_file_size", 10485760)
            if in_memory
            else self.config.file.get("max_file_size", 104857600)
        )

        self.log.info(
            f"[INFO] Starting stream from '{stream_url}' to {'memory' if in_memory else output_path}. "
            f"Max size limit: {max_file_size} bytes"
        )

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
                                            f"[ERROR] Stream size exceeded limit ({total_bytes} > {max_file_size} bytes). Aborting."
                                        )
                                        return None

                                    output.write(chunk)

                                self.log.info(
                                    f"[INFO] Streamed {total_bytes} bytes into file: {resolved_path}"
                                )
                                return resolved_path

                        async for chunk in response.content.iter_chunked(8192):
                            chunk_size = len(chunk)
                            total_bytes += chunk_size

                            if max_file_size > 0 and total_bytes > max_file_size:
                                self.log.error(
                                    f"[ERROR] Stream size exceeded limit ({total_bytes} > {max_file_size} bytes). Aborting."
                                )
                                return None

                            output.write(chunk)

                        output.seek(0)
                        self.log.info(
                            f"[INFO] Streamed {total_bytes} bytes into memory."
                        )
                        return output

            except ClientError as e:
                self.log.warning(f"Attempt {attempt}: Network error: {e}")
            except asyncio.TimeoutError:
                self.log.warning(f"Attempt {attempt}: Timeout while streaming data.")
            except Exception as e:
                self.log.warning(f"Attempt {attempt}: Error streaming data: {e}")

            await asyncio.sleep(1)

        self.log.error(f"[ERROR] Failed to stream data after {max_retries} attempts.")
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

            self.log.info(f"[DEBUG] Data size: {len(data)} bytes")

            metadata = await probe_bytes(data)
            self.log.info(f"[DEBUG] Raw metadata from probe_bytes: {metadata}")

            format_info = metadata.get("format", {})
            format_name = format_info.get("format_name", "").lower()

            # Define known image formats
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
                self.log.debug(f"Detected image format: {format_name}")

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
                    "[DEBUG] No streams found and not recognized as image."
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
            ), "Active stream must not be None at this point."
            duration_str = active_stream.get("duration")
            if not duration_str or duration_str in ("N/A", ""):
                duration = 0.0
            else:
                try:
                    duration = float(duration_str)
                except ValueError:
                    self.log.info(
                        f"Non-numeric duration '{duration_str}' detected. Defaulting to 0.0"
                    )
                    duration = 0.0

            self.log.info(f"[DEBUG] Stream duration: {duration}")

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

    def _get_extension_from_url(self, url: str) -> str:
        filename = url.rsplit("/", 1)[-1]
        return filename.rsplit(".", 1)[-1] if "." in filename else "jpg"

    async def process_url(self, url: str) -> Tuple[Optional[Media], Optional[Media]]:
        is_image = False
        try:
            timeout = ClientTimeout(total=10)
            async with ClientSession(timeout=timeout) as session:
                async with session.head(url, allow_redirects=True) as response:
                    ctype = response.headers.get("Content-Type", "").lower()
                    self.log.debug(f"HEAD Content-Type for {url}: {ctype}")
                    if ctype.startswith("image/"):
                        is_image = True
        except Exception as e:
            self.log.warning(f"Failed HEAD request on {url}, ignoring: {e}")

        if is_image:
            self.log.info(
                f"Detected image via Content-Type. Using _stream_to_memory for: {url}"
            )
            image_data = await self._download_image(url)
            if not image_data:
                raise Exception(f"Failed to fetch image from {url}")

            if isinstance(image_data, BytesIO):
                image_metadata = (
                    await self._get_stream_metadata(image_data.getvalue()) or {}
                )
            else:
                raise Exception("Downloading outside of stdout is not yet supported.")

            filename = url.rsplit("/", 1)[-1] if "/" in url else url
            filename = (
                unicodedata.normalize("NFKD", filename)
                .encode("ASCII", "ignore")
                .decode("ASCII")
            )
            filename = re.sub(r'[<>:"/\\|?*\x00-\x1F\'’“”]', "_", filename)
            filename = re.sub(r"\s+", "_", filename)
            filename = re.sub(r"__+", "_", filename)
            filename = filename.strip("_.")[:255]
            filename = re.sub(r"_+", "_", filename)

            image_obj = Media(
                filename=filename or "unnamed_image.jpg",
                stream=image_data,
                metadata=MediaMetadata(
                    url=url,
                    id="unknown_id",
                    title=filename,
                    uploader=None,
                    ext=filename.rsplit(".", 1)[-1] if "." in filename else "jpg",
                    extractor="direct_image",
                    duration=image_metadata.get("duration"),
                    width=image_metadata.get("width"),
                    height=image_metadata.get("height"),
                    size=image_metadata.get("size"),
                    has_video=image_metadata.get("has_video", False),
                    has_audio=image_metadata.get("has_audio", False),
                    is_image=image_metadata.get("is_image", False),
                ),
            )

            self.log.info(
                f"Image Found:\n- Filename: {image_obj.filename}\n"
                f"- Size: {image_obj.metadata.size} bytes"
            )
            self.log.info(f"Media Object: {image_obj}")
            return (image_obj, None)

        commands = self._create_ytdlp_commands(url)
        if not commands:
            self.log.warning("MediaHandler.process_url: Invalid command, check config.")
            raise Exception("No valid yt-dlp commands found in configuration.")

        query_commands, download_commands = commands[0], commands[1]

        media_ytdlp_metadata = await self._ytdlp_execute_query(commands=query_commands)
        if not media_ytdlp_metadata:
            self.log.warning(
                "MediaHandler.process_url: Failed to find media with yt_dlp"
            )
            raise Exception("Failed to retrieve metadata from yt-dlp.")

        is_live = media_ytdlp_metadata.get("is_live", False)
        media_stream_data: Optional[Union[BytesIO, str]] = None

        if is_live and not self.config.ffmpeg.get("enable_livestream_previews", False):
            self.log.warning(
                "MediaHandler.process_url: Live media detected, but livestream previews are disabled."
            )
            raise Exception("Livestream processing is disabled in configuration.")
        elif is_live:
            media_stream_data = await self._ffmpeg_livestream_capture(
                stream_url=media_ytdlp_metadata["url"]
            )
            if not media_stream_data:
                self.log.warning(
                    "MediaHandler.process_url: Failed to download live stream."
                )
                raise Exception("Failed to download live stream.")

        else:
            duration = media_ytdlp_metadata.get("duration")
            if duration is not None:
                if duration > self.config.file.get("max_duration", 0):
                    self.log.warning(
                        "MediaHandler.process_url: Media length exceeds the configured duration limit."
                    )
                    raise Exception("Media duration exceeds the configured maximum.")
            else:
                self.log.warning(
                    "MediaHandler.process_url: Media duration is missing from metadata. "
                    "Attempting to download the stream anyway."
                )

            media_stream_data = await self._ytdlp_execute_download(
                commands=download_commands
            )

        if not media_stream_data:
            self.log.warning(
                "MediaHandler.process_url: Failed to download media stream."
            )
            raise Exception("Failed to download media stream.")

        if isinstance(media_stream_data, BytesIO):
            media_stream_metadata = (
                await self._get_stream_metadata(media_stream_data.getvalue()) or {}
            )
        else:
            raise Exception("Downloading outside of stdout is not yet supported.")

        filename = "{title}-{uploader}-{extractor}-{id}.{ext}".format(
            title=media_ytdlp_metadata.get("title", "unknown_title"),
            uploader=media_ytdlp_metadata.get("uploader", "unknown_uploader"),
            extractor=media_ytdlp_metadata.get("extractor", "unknown_platform"),
            id=media_ytdlp_metadata["id"],
            ext=media_ytdlp_metadata.get("ext", "unknown_extension"),
        )
        filename = (
            unicodedata.normalize("NFKD", filename)
            .encode("ASCII", "ignore")
            .decode("ASCII")
        )
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1F\'’“”]', "_", filename)
        filename = re.sub(r"\s+", "_", filename)
        filename = re.sub(r"__+", "_", filename)
        filename = filename.strip("_.")[:255]
        filename = re.sub(r"_+", "_", filename)

        media_obj = Media(
            filename=filename,
            stream=media_stream_data,
            metadata=MediaMetadata(
                url=media_ytdlp_metadata["url"],
                id=media_ytdlp_metadata["id"],
                title=media_ytdlp_metadata.get("title"),
                uploader=media_ytdlp_metadata.get("uploader"),
                ext=media_ytdlp_metadata.get("ext"),
                extractor=media_ytdlp_metadata.get("extractor"),
                duration=media_stream_metadata.get("duration"),
                width=media_stream_metadata.get("width"),
                height=media_stream_metadata.get("height"),
                size=media_stream_metadata.get("size"),
                has_video=media_stream_metadata.get("has_video", False),
                has_audio=media_stream_metadata.get("has_audio", False),
                is_image=media_stream_metadata.get("is_image", False),
            ),
        )

        thumbnail: Optional[Media] = None
        thumbnail_url = media_ytdlp_metadata.get("thumbnail")
        if thumbnail_url:
            thumb_data = await self._download_image(thumbnail_url)
            if isinstance(thumb_data, BytesIO):
                image_metadata = (
                    await self._get_stream_metadata(thumb_data.getvalue()) or {}
                )
            else:
                raise Exception("Downloading outside of stdout is not yet supported.")
            if thumb_data:
                thumb_metadata = await self._get_stream_metadata(thumb_data.getvalue())
                if thumb_metadata:
                    thumb_ext = self._get_extension_from_url(thumbnail_url)
                    thumbnail = Media(
                        filename=media_ytdlp_metadata["id"] + "_thumbnail" + thumb_ext,
                        stream=thumb_data,
                        metadata=MediaMetadata(
                            url=thumbnail_url,
                            id=media_ytdlp_metadata["id"],
                            uploader=media_ytdlp_metadata.get("uploader"),
                            ext=thumb_ext,
                            width=thumb_metadata.get("width"),
                            height=thumb_metadata.get("height"),
                            size=thumb_metadata.get("size"),
                            has_video=thumb_metadata.get("has_video", False),
                            has_audio=thumb_metadata.get("has_audio", False),
                            is_image=thumb_metadata.get("is_image", False),
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

        # Log final info
        has_video = media_stream_metadata.get("has_video", False)
        has_audio = media_stream_metadata.get("has_audio", False)
        is_image = media_stream_metadata.get("is_image", False)

        if is_image:
            self.log.info(
                f"Image Found:\n- Filename: {media_obj.filename}\n"
                f"- Size: {media_obj.metadata.size} bytes"
            )
        elif has_video:
            self.log.info(
                f"Video Found:\n- Title: {media_obj.metadata.title}\n"
                f"- Uploader: {media_obj.metadata.uploader}\n"
                f"- Resolution: {media_obj.metadata.width}x{media_obj.metadata.height}\n"
                f"- Duration: {media_obj.metadata.duration}s\n"
                f"- Size: {media_obj.metadata.size} bytes"
            )
        elif has_audio:
            self.log.info(
                f"Audio Found:\n- Title: {media_obj.metadata.title}\n"
                f"- Uploader: {media_obj.metadata.uploader}\n"
                f"- Duration: {media_obj.metadata.duration}s\n"
                f"- Size: {media_obj.metadata.size} bytes"
            )
        else:
            self.log.info(
                f"Unknown media type:\n- Title: {media_obj.metadata.title}\n"
                f"- Size: {media_obj.metadata.size} bytes"
            )

        if thumbnail:
            self.log.info(
                f"Thumbnail Found:\n- Resolution: {thumbnail.metadata.width}x{thumbnail.metadata.height}\n"
                f"- Size: {thumbnail.metadata.size} bytes"
            )

        return (media_obj, thumbnail)


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
