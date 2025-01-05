from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING, Any, Dict, Optional, Union

from mautrix.util.ffmpeg import convert_bytes, probe_bytes

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

    async def extract_thumbnail(
        self, video_data: bytes, format: str = "mp4"
    ) -> BytesIO:
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

        return BytesIO(thumbnail_data)

    async def capture_livestream(self, stream_url: str) -> BytesIO:
        input_args = [
            "-i",
            stream_url,
            "-t",
            "90",
        ]

        output_args = [
            "-c",
            "copy",
            "-c:a",
            "aac",
            "-bsf:a",
            "aac_adtstoasc",
        ]

        stream_data = await convert_bytes(
            data=b"",
            output_extension="mp4",
            input_args=input_args,
            output_args=output_args,
        )

        self.log.info("Livestream preview succesfully extracted.")
        if not self._validate_file_size(stream_data):
            raise

        return BytesIO(stream_data)

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
