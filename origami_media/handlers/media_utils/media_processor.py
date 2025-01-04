from __future__ import annotations

import re
import unicodedata
import uuid
from io import BytesIO
from typing import TYPE_CHECKING, Dict, Literal, Optional
from urllib.parse import urlparse

from .media_processor_utils.ffmpeg import Ffmpeg
from .media_processor_utils.native import Native
from .media_processor_utils.ytdlp import Ytdlp
from .models import Media, MediaFile, MediaInfo

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from mautrix.util.logging.trace import TraceLogger
    from media_processor_utils.ffmpeg import FfmpegMetadata

    from origami_media.origami_media import Config


class MediaProcessor:
    def __init__(self, config: "Config", log: "TraceLogger", http: "ClientSession"):
        self.config = config
        self.log = log
        self.http = http

        self.ffmpeg_controller = Ffmpeg(log=self.log, config=self.config)
        self.ytdlp_controller = Ytdlp(log=self.log, config=self.config)
        self.native_controller = Native(
            log=self.log, config=self.config, http=self.http
        )

    async def _analyze_file_metadata(self, data: BytesIO) -> Optional[FfmpegMetadata]:
        try:
            return await self.ffmpeg_controller.extract_metadata(data.getvalue())
        except Exception as e:
            self.log.warning(f"Failed to extract metadata: {e}")
            return None

    async def _download_simple_media(self, url: str) -> Optional[BytesIO]:
        data = await self.native_controller.client_download(url)
        if not data:
            self._handle_download_error(f"Failed to fetch image from {url}")
            return None

        if not isinstance(data, BytesIO):
            self._handle_download_error(
                "Downloading outside of stdout is not yet supported."
            )
            return None

        return data

    async def _query_advanced_media(self, url) -> Optional[Dict]:
        query_commands = self.ytdlp_controller.create_ytdlp_commands(
            url, command_type="query"
        )

        if not query_commands:
            self._handle_download_error(
                "No valid yt-dlp commands found in configuration."
            )
            return None

        ytdlp_metadata = await self.ytdlp_controller.ytdlp_execute_query(
            commands=query_commands
        )
        if not ytdlp_metadata:
            return None

        return ytdlp_metadata

    async def _download_advanced_media(
        self, url: str, ytdlp_metadata: dict
    ) -> Optional[BytesIO]:
        download_commands = self.ytdlp_controller.create_ytdlp_commands(
            url, command_type="download"
        )

        if not ytdlp_metadata:
            self._handle_download_error("Failed to retrieve metadata from yt-dlp.")
            return None

        if ytdlp_metadata.get("is_live", False):
            if not self.config.ffmpeg.get("enable_livestream_previews", False):
                self._handle_download_error(
                    "Live media detected, but livestream previews are disabled."
                )
                return None

            data = await self.ffmpeg_controller.capture_livestream(
                stream_url=ytdlp_metadata["url"]
            )

            if not data or not isinstance(data, BytesIO):
                self._handle_download_error(
                    "Failed to download media stream or unsupported data type."
                )
                return None

            return data
        else:
            if (duration := ytdlp_metadata.get("duration")) is not None:
                if duration > self.config.file.get("max_duration", 0):
                    self._handle_download_error(
                        "Media length exceeds the configured duration limit."
                    )
                    return None

            data = await self.ytdlp_controller.ytdlp_execute_download(
                commands=download_commands
            )

            if not data or not isinstance(data, BytesIO):
                self._handle_download_error(
                    "Failed to download media stream or unsupported data type."
                )
                return None

            return data

    def _get_media_type(
        self, metadata: FfmpegMetadata
    ) -> Literal["video", "audio", "image", "unknown"]:
        if metadata.has_video:
            return "video"
        if metadata.has_audio:
            return "audio"
        if metadata.is_image:
            return "image"
        return "unknown"

    def _generate_filename(
        self,
        metadata: dict,
    ) -> str:
        filename = "{title}-{uploader}-{extractor}-{id}".format(
            title=metadata.get("title", "unknown_title"),
            uploader=metadata.get("uploader", "unknown_uploader"),
            extractor=metadata.get("extractor", "unknown_platform"),
            id=metadata.get("id", "unknown_id"),
        )

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

    def _sanitize_file_format(self, format: str):
        format_map = {
            "image2": "png",
            "video": "mp4",
            "audio": "mp3",
            "application": "bin",
            "vp9": "webm",
            "h264": "mp4",
        }
        new_format = format_map.get(format, format)
        if new_format != format:
            self.log.info(f"Format mapped from {format} to {new_format}")
        return new_format

    def _generate_media_filename(self, metadata: dict, extension: str) -> str:
        filename = self._generate_filename(metadata)
        return f"{filename}.{extension}"

    def _handle_download_error(self, message: str) -> None:
        self.log.warning(message)

    def _create_media_object(
        self, stream: BytesIO, other_metadata: dict, file_metadata: FfmpegMetadata
    ) -> MediaFile:
        media_type = self._get_media_type(file_metadata)
        return MediaFile(
            filename=self._generate_media_filename(
                other_metadata, other_metadata.get("ext", "mp4")
            ),
            stream=stream,
            metadata=MediaInfo(
                url=other_metadata["url"],
                id=other_metadata["id"],
                origin=other_metadata["origin"],
                title=other_metadata.get("title", "unknown_title"),
                uploader=other_metadata.get("uploader", "unknown_uploader"),
                ext=other_metadata.get("ext", "mp4"),
                extractor=other_metadata.get("extractor"),
                duration=file_metadata.duration,
                width=file_metadata.width,
                height=file_metadata.height,
                size=file_metadata.size,
                media_type=media_type,
                thumbnail_url=other_metadata.get("thumbnail"),
            ),
        )

    async def _process_simple_media(
        self, data: BytesIO, url: str, ffmpeg_metadata: FfmpegMetadata
    ) -> Optional[MediaFile]:
        url_uuid = uuid.uuid5(uuid.NAMESPACE_URL, url)
        metadata = {
            "id": str(url_uuid),
            "extractor": urlparse(url).netloc.split(":")[0],
            "uploader": "unknown_uploader",
            "title": "unknown_title",
            "url": url,
            "ext": self._sanitize_file_format(ffmpeg_metadata.format),
            "origin": "simple",
        }

        return self._create_media_object(data, metadata, ffmpeg_metadata)

    async def _process_advanced_media(
        self, data: BytesIO, ytdlp_metadata: Dict, ffmpeg_metadata: FfmpegMetadata
    ) -> Optional[MediaFile]:
        mutate_ytdlp_metadata = {
            "id": ytdlp_metadata.get("id"),
            "extractor": ytdlp_metadata.get("extractor"),
            "uploader": ytdlp_metadata.get("uploader", "unknown_uploader"),
            "title": ytdlp_metadata.get("title", "unknown_title"),
            "url": ytdlp_metadata["url"],
            "ext": ytdlp_metadata.get("ext", "mp4"),
            "origin": "advanced",
            "thumbnail": ytdlp_metadata.get("thumbnail"),
        }

        return self._create_media_object(
            data, other_metadata=mutate_ytdlp_metadata, file_metadata=ffmpeg_metadata
        )

    async def _process_thumbnail_media(
        self, data: BytesIO, url: str, ffmpeg_metadata: FfmpegMetadata
    ) -> Optional[MediaFile]:
        url_uuid = uuid.uuid5(uuid.NAMESPACE_URL, url)
        metadata = {
            "id": str(url_uuid),
            "extractor": urlparse(url).netloc.split(":")[0],
            "uploader": "unknown_uploader",
            "title": "unknown_title",
            "url": url,
            "ext": self._sanitize_file_format(ffmpeg_metadata.format),
            "origin": "thumbnail",
        }

        return self._create_media_object(data, metadata, ffmpeg_metadata)

    async def _primary_media_controller(self, url: str) -> Optional[MediaFile]:
        skip_simple = {
            "odysee",
            "youtube",
            "bitchute",
            "rumble",
            "twitter",
            "x",
            "youtu",
        }
        should_skip = any(service in url.lower() for service in skip_simple)

        if not should_skip:
            simple_data = await self._download_simple_media(url)
            if simple_data:
                simple_metadata = await self._analyze_file_metadata(simple_data)
                if simple_metadata:
                    return await self._process_simple_media(
                        simple_data, ffmpeg_metadata=simple_metadata, url=url
                    )

        ytdlp_metadata = await self._query_advanced_media(url)
        if ytdlp_metadata:
            data = await self._download_advanced_media(
                url, ytdlp_metadata=ytdlp_metadata
            )
            if data:
                metadata = await self._analyze_file_metadata(data)
                if metadata:
                    return await self._process_advanced_media(
                        data, ffmpeg_metadata=metadata, ytdlp_metadata=ytdlp_metadata
                    )

        return None

    async def _thumbnail_media_controller(
        self, primary_media_object: MediaFile
    ) -> Optional[MediaFile]:
        if (
            primary_media_object.metadata.origin == "advanced"
            and primary_media_object.metadata.thumbnail_url
        ):
            data = await self._download_simple_media(
                primary_media_object.metadata.thumbnail_url
            )
            if data:
                metadata = await self._analyze_file_metadata(data)
                if metadata:
                    return await self._process_thumbnail_media(
                        data,
                        ffmpeg_metadata=metadata,
                        url=primary_media_object.metadata.url,
                    )

        if primary_media_object.metadata.media_type == "video":
            data = await self.ffmpeg_controller.extract_thumbnail(
                video_data=primary_media_object.stream.getvalue()
            )
            if data:
                if not isinstance(data, BytesIO):
                    return None
                metadata = await self._analyze_file_metadata(data)
                if metadata:
                    return await self._process_thumbnail_media(
                        data,
                        ffmpeg_metadata=metadata,
                        url=primary_media_object.metadata.url,
                    )

        return None

    async def process_url(self, url: str) -> Optional[Media]:

        primary_file_object = await self._primary_media_controller(url)
        if not primary_file_object:
            self.log.warning("Failed to process primary media.")
            return None

        thumbnail_file_object = await self._thumbnail_media_controller(
            primary_file_object
        )
        if not thumbnail_file_object:
            self.log.info("Thumbnail was not obtained.")

        self.log.info(
            f"Media Found:\n- Filename: {primary_file_object.filename}\n"
            f"- Size: {primary_file_object.metadata.size} bytes\n"
            f"- Media type: {primary_file_object.metadata.media_type}\n"
            f"- Website: {primary_file_object.metadata.extractor}"
        )

        return Media(content=primary_file_object, thumbnail=thumbnail_file_object)
