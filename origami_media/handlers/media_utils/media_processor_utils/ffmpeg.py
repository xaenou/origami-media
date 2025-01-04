import asyncio
import os
import shlex
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING, Any, Dict, Optional, Union

from mautrix.util.ffmpeg import probe_bytes

if TYPE_CHECKING:
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.origami_media import Config


@dataclass
class FfmpegMetadata:
    width: int
    height: int
    format: str
    size: int
    duration: float
    has_video: bool
    has_audio: bool
    is_image: bool


class Ffmpeg:

    def __init__(self, config: "Config", log: "TraceLogger"):
        self.config = config
        self.log = log

    async def _extract_thumbnail_stdout(
        self, command_parts: list, max_file_size: int, video_data: bytes
    ) -> Optional[BytesIO]:
        if not video_data:
            self.log.error("Empty video data provided")
            return None

        self.log.info(
            f"Extracting thumbnail in-memory with FFmpeg args: {' '.join(command_parts)}"
        )
        process = await asyncio.create_subprocess_exec(
            *command_parts,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        if not process.stdin or not process.stdout or not process.stderr:
            self.log.error(
                "FFmpeg stdin, stdout, or stderr is None, cannot extract thumbnail in-memory."
            )
            await process.wait()
            return None

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=video_data), timeout=30
            )

            if process.returncode != 0:
                self.log.error(
                    f"FFmpeg exited with return code {process.returncode}. "
                    f"Stderr: {stderr.decode().strip() if stderr else 'No stderr output'}"
                )
                return None

            thumbnail_data = BytesIO(stdout)
            if max_file_size and len(thumbnail_data.getvalue()) > max_file_size:
                self.log.error("Thumbnail exceeds max in-memory file size.")
                return None

            thumbnail_data.seek(0)
            return thumbnail_data

        except asyncio.TimeoutError:
            self.log.error("FFmpeg thumbnail extraction timed out.")
            return None
        except Exception as e:
            self.log.error(f"Unexpected error during thumbnail extraction: {e}")
            return None
        finally:
            if process.stdin:
                process.stdin.close()
                await process.stdin.wait_closed()
            if process.returncode is None:
                process.kill()
            await process.wait()

    async def extract_thumbnail(
        self, video_data: bytes, time: str = "00:00:05"
    ) -> Optional[Union[str, BytesIO]]:
        output_path = self.config.file.get("output_path", "-")
        self.log.info(f"Thumbnail input video data size: {len(video_data)} bytes")
        output_format = "png"

        command_parts = [
            "ffmpeg",
            "-i",
            "pipe:0",  # Read from stdin
            "-ss",
            time,  # Seek to specified time
            "-frames:v",
            "1",  # Extract only one frame
            "-f",
            "image2",  # Force image2 format
            "-c:v",
            output_format,  # Set codec to jpg/png
            "pipe:1",  # Output to stdout
        ]

        in_memory = output_path == "-"
        max_file_size_key = "max_in_memory_file_size" if in_memory else "max_file_size"
        max_file_size = self.config.file.get(max_file_size_key, 0)

        if in_memory:
            thumbnail_stream = await self._extract_thumbnail_stdout(
                command_parts=command_parts,
                max_file_size=max_file_size,
                video_data=video_data,
            )
            return thumbnail_stream

    async def _capture_livestream_filesystem(
        self, command_parts, max_file_size, output_path
    ) -> Optional[str]:
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

    async def _capture_livestream_stdout(
        self, command_parts, max_file_size
    ) -> Optional[BytesIO]:
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

    async def capture_livestream(
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
            file_stream = await self._capture_livestream_stdout(
                command_parts=command_parts,
                max_file_size=max_file_size,
            )
            return file_stream

        else:
            file_path = await self._capture_livestream_filesystem(
                command_parts=command_parts,
                max_file_size=max_file_size,
                output_path=output_path,
            )

            return file_path

    def _parse_dimension(self, value: Any) -> int:
        try:
            return int(value) if value is not None else 0
        except (ValueError, TypeError):
            return 0

    def _parse_duration(self, value: Any) -> float:
        if not value or value in ("N/A", ""):
            return 0.0
        try:
            return float(value)
        except ValueError:
            self.log.info(f"Non-numeric duration '{value}' detected. Defaulting to 0.0")
            return 0.0

    def _get_video_audio_metadata(
        self, metadata: Dict[str, Any], data_size: int
    ) -> Optional[FfmpegMetadata]:
        streams = metadata.get("streams", [])
        video_stream = next(
            (s for s in streams if s.get("codec_type") == "video"), None
        )
        audio_stream = next(
            (s for s in streams if s.get("codec_type") == "audio"), None
        )

        if not video_stream and not audio_stream:
            self.log.warning("No video or audio stream found.")
            return None

        has_video = bool(video_stream)
        has_audio = bool(audio_stream)

        active_stream = video_stream if has_video else audio_stream

        if not active_stream:
            self.log.warning("No active stream available for metadata extraction.")
            return None

        duration = self._parse_duration(active_stream.get("duration"))

        max_duration = self.config.file.get("max_duration", 0)

        if duration > max_duration:
            self.log.warning(
                f"Duration exceeds max_duration ({self.config.file.get('max_duration')}s)."
            )
            return None

        return FfmpegMetadata(
            width=self._parse_dimension(video_stream.get("width")) if has_video else 0,
            height=(
                self._parse_dimension(video_stream.get("height")) if has_video else 0
            ),
            format=active_stream.get("codec_name", "unknown"),
            size=data_size,
            duration=duration,
            has_video=has_video,
            has_audio=has_audio,
            is_image=False,
        )

    def _get_image_metadata(
        self, metadata: Dict[str, Any], data_size: int
    ) -> Optional[FfmpegMetadata]:
        format_info = metadata.get("format", {})
        width = self._parse_dimension(format_info.get("width"))
        height = self._parse_dimension(format_info.get("height"))

        if not width or not height:
            first_stream = metadata.get("streams", [{}])[0]
            width = self._parse_dimension(first_stream.get("width"))
            height = self._parse_dimension(first_stream.get("height"))

        format_name = format_info.get("format_name", "").lower()
        if not width or not height:
            self.log.warning("Invalid image dimensions detected.")
            return None

        max_size = self.config.file.get("max_image_size", 0)
        if max_size > 0 and data_size > max_size:
            self.log.warning(
                f"Image size exceeds maximum allowed size ({max_size} bytes)."
            )
            return None

        self.log.info(f"Detected image format: {format_name}")

        return FfmpegMetadata(
            width=width,
            height=height,
            format=format_name,
            size=data_size,
            duration=0.0,
            has_video=False,
            has_audio=False,
            is_image=True,
        )

    def _is_image_format(self, metadata: Dict[str, Any]) -> bool:
        format_name = metadata.get("format", {}).get("format_name", "").lower()
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
        return any(img_keyword in format_name for img_keyword in image_formats)

    async def _probe_metadata(self, data: bytes) -> Optional[Dict[str, Any]]:
        metadata = await probe_bytes(data)
        self.log.info(f"Raw metadata from probe_bytes: {metadata}")
        return metadata

    def _validate_file_size(self, data: bytes) -> bool:
        max_file_size = self.config.file.get("max_in_memory_file_size", 0)
        if len(data) > max_file_size:
            self.log.warning(
                f"File size exceeds max_in_memory_file_size ({max_file_size} bytes)."
            )
            return False
        self.log.info(f"Data size: {len(data)} bytes")
        return True

    async def extract_metadata(self, data: bytes) -> FfmpegMetadata:
        if not self._validate_file_size(data):
            raise Exception("File size validation failed.")

        metadata = await self._probe_metadata(data)
        if not metadata:
            raise Exception("Failed to probe metadata from the file.")

        if self._is_image_format(metadata):
            image_metadata = self._get_image_metadata(metadata, len(data))
            if not image_metadata:
                raise Exception("Failed to extract image metadata.")
            return image_metadata

        video_audio_metadata = self._get_video_audio_metadata(metadata, len(data))
        if not video_audio_metadata:
            raise Exception("Failed to extract video/audio metadata.")

        return video_audio_metadata
