from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional

from mautrix.util.ffmpeg import convert_bytes, probe_bytes

from origami_media.models.ffmpeg_models import FfmpegMetadata

if TYPE_CHECKING:
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.main import Config


class Ffmpeg:

    def __init__(self, config: "Config", log: "TraceLogger"):
        self.config = config
        self.log = log

    async def extract_thumbnail(self, video_data: bytes, format: str = "mp4") -> bytes:
        self.log.info(f"Thumbnail input video data size: {len(video_data)} bytes")

        thumbnail_data = await convert_bytes(
            data=video_data,
            output_extension="png",
            input_args=[
                "-nostdin",
                "-analyzeduration",
                "10M",
                "-probesize",
                "10M",
                "-f",
                format,
            ],
            output_args=["-frames:v", "1", "-f", "image2pipe", "-vcodec", "png"],
            input_mime=f"video/{format}",
            logger=self.log,
        )

        self.log.info("Thumbnail successfully extracted")
        if not self._validate_file_size(thumbnail_data):
            raise

        return thumbnail_data

    async def capture_livestream(self, stream_url: str) -> bytes:
        self.log.info("Downloading livestream preview...")
        length = self.config.ffmpeg.get("livestream_preview_length", 15)

        ffmpeg_cmd = [
            "ffmpeg",
            "-i",
            stream_url,
            "-t",
            str(length),
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            "-movflags",
            "+frag_keyframe+empty_moov",
            "-f",
            "mp4",
            "-blocksize",
            "1024",
            "pipe:1",
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                error_message = stderr.decode()
                raise RuntimeError(f"FFmpeg error: {error_message}")

            print("Livestream preview successfully extracted.")
            if not self._validate_file_size(stdout):
                raise RuntimeError("Repaired MP4 file size is too large")
            return stdout

        except Exception as e:
            raise RuntimeError(f"Failed to capture livestream: {e}")

    async def postprocess_video(self, video_data: bytes) -> bytes:
        self.log.info(f"Post-processing video, input size: {len(video_data)} bytes.")

        input_args = self.config.ffmpeg["video_input_args"]
        output_args = self.config.ffmpeg["video_output_args"]
        output_ext = self.config.ffmpeg["video_output_ext"]

        processed_data = await convert_bytes(
            data=video_data,
            output_extension=output_ext,
            input_args=input_args,
            output_args=output_args,
            logger=self.log,
        )

        self.log.info("Video successfully processed.")

        if not self._validate_file_size(processed_data):
            raise RuntimeError("Converted file size is too large")

        return processed_data

    async def prostprocess_audio(self, data: bytes) -> bytes:
        self.log.info(f"Post-processing audio, input size: {len(data)} bytes.")

        input_args = self.config.ffmpeg["audio_input_args"]
        output_args = self.config.ffmpeg["audio_output_args"]
        output_ext = self.config.ffmpeg["audio_output_ext"]

        processed_data = await convert_bytes(
            data=data,
            output_extension=output_ext,
            input_args=input_args,
            output_args=output_args,
            logger=self.log,
        )

        self.log.info("Audio succesfully processed.")

        if not self._validate_file_size(processed_data):
            raise RuntimeError("Converted file size is too large")

        return processed_data

    async def normalize_image(self, image_data: bytes) -> bytes:
        self.log.info(f"Converting image to PNG, input size: {len(image_data)} bytes.")

        processed_data = await convert_bytes(
            data=image_data,
            output_extension="png",
            input_args=["-nostdin"],
            output_args=[],
            logger=self.log,
        )

        self.log.info("Image successfully converted to PNG.")

        if not self._validate_file_size(processed_data):
            raise RuntimeError("Converted PNG file size is too large")

        return processed_data

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

    async def _probe_metadata(self, data: bytes) -> Optional[Dict[str, Any]]:
        metadata = await probe_bytes(data)
        return metadata

    def _validate_file_size(self, data: bytes) -> bool:
        max_file_size = self.config.file.get("max_in_memory_file_size", 0)
        if len(data) > max_file_size:
            self.log.warning(
                f"File size exceeds max_in_memory_file_size ({max_file_size} bytes)."
            )
            return False
        return True

    async def extract_metadata(self, data: bytes) -> FfmpegMetadata:
        if not self._validate_file_size(data):
            raise ValueError("File size validation failed.")

        metadata = await self._probe_metadata(data)
        if not metadata:
            raise ValueError("Failed to probe metadata from the file.")

        streams = metadata.get("streams", [])
        format_info = metadata.get("format", {})
        streams_info = streams[0] if streams else {}

        duration = (
            self._parse_duration(
                streams_info.get("duration") or format_info.get("duration")
            )
            or 0.0
        )

        # max_duration = self.config.get("file", {}).get("max_duration", 0)
        # if max_duration > 0 and duration > max_duration:
        #     raise ValueError(
        #         f"Duration exceeds maximum allowed duration ({max_duration}s)."
        #     )

        width = (
            self._parse_dimension(format_info.get("width"))
            or self._parse_dimension(streams_info.get("width"))
            or 0
        )
        height = (
            self._parse_dimension(format_info.get("height"))
            or self._parse_dimension(streams_info.get("height"))
            or 0
        )

        return FfmpegMetadata(
            width=width,
            height=height,
            duration=duration,
        )
