from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING, Dict, Literal, Optional
from urllib.parse import urlparse

from .media_processor_utils.ffmpeg import Ffmpeg
from .media_processor_utils.native import Native
from .media_processor_utils.ytdlp import Ytdlp

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from mautrix.util.logging.trace import TraceLogger
    from media_processor_utils.ffmpeg import FfmpegMetadata

    from origami_media.origami_media import Config


@dataclass
class MediaInfo:
    url: str
    media_type: Literal["audio", "video", "image", "unknown"]
    origin: Literal["simple", "advanced", "thumbnail"]
    id: str
    thumbnail_url: Optional[str] = None
    title: Optional[str] = None
    uploader: Optional[str] = None
    extractor: Optional[str] = None
    ext: Optional[str] = None
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    size: Optional[int] = None


@dataclass
class MediaFile:
    filename: str
    metadata: MediaInfo
    stream: BytesIO = BytesIO()

    def __del__(self):
        if not self.stream.closed:
            self.stream.close()


@dataclass
class Media:
    content: MediaFile
    thumbnail: Optional[MediaFile] = None


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
        try:
            return await self.native_controller.client_download(url)
        except Exception as e:
            error_message = f"Failed to download media: {e}"
            self._handle_download_error(error_message)
            return None

    async def _query_advanced_media(self, url) -> Optional[Dict]:
        try:
            query_commands = self.ytdlp_controller.create_ytdlp_commands(
                url, command_type="query"
            )

            ytdlp_metadata = await self.ytdlp_controller.ytdlp_execute_query(
                commands=query_commands
            )
            return ytdlp_metadata

        except Exception as e:
            error_message = f"Failed to query media: {e}"
            self._handle_download_error(error_message)
            return None

    async def _download_advanced_media(self, ytdlp_metadata: dict) -> Optional[BytesIO]:
        try:
            if ytdlp_metadata.get("is_live", False):
                if not self.config.ffmpeg.get("enable_livestream_previews", False):
                    raise RuntimeError(
                        "Live media detected, but livestream previews are disabled."
                    )

                return await self.ffmpeg_controller.capture_livestream(
                    stream_url=ytdlp_metadata["url"]
                )

            if (duration := ytdlp_metadata.get("duration")) is not None:
                if duration > self.config.file.get("max_duration", 0):
                    raise RuntimeError(
                        "Media length exceeds the configured duration limit."
                    )

            return await self.ytdlp_controller.ytdlp_execute_download(
                commands=self.ytdlp_controller.create_ytdlp_commands(
                    ytdlp_metadata["url"], command_type="download"
                )
            )

        except Exception as e:
            error_message = f"Failed to download media: {e}"
            self._handle_download_error(error_message)
            return None

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
            "png_pipe": "png",
            "unknown_video": "png",
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
        mutated_ytdlp_metadata = {
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
            data, other_metadata=mutated_ytdlp_metadata, file_metadata=ffmpeg_metadata
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
            "x.com",
            "youtu",
        }
        should_skip = any(service in url.lower() for service in skip_simple)
        self.log.info(
            f"entering primary media controller, should skip simple download: {should_skip}"
        )

        if not should_skip:
            # try a simple flow without yt-dlp
            simple_data = await self._download_simple_media(url)
            if simple_data:
                simple_metadata = await self._analyze_file_metadata(simple_data)
                if simple_metadata:
                    return await self._process_simple_media(
                        simple_data, ffmpeg_metadata=simple_metadata, url=url
                    )

        # try an advanced flow with yt-dlp
        ytdlp_metadata = await self._query_advanced_media(url)
        if ytdlp_metadata:
            advanced_data = await self._download_advanced_media(
                ytdlp_metadata=ytdlp_metadata
            )
            if advanced_data:
                advanced_metadata = await self._analyze_file_metadata(advanced_data)
                if advanced_metadata:
                    return await self._process_advanced_media(
                        advanced_data,
                        ffmpeg_metadata=advanced_metadata,
                        ytdlp_metadata=ytdlp_metadata,
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

        elif (
            primary_media_object.metadata.media_type == "video"
            and self.config.ffmpeg["enable_thumbnail_generation"]
        ):
            primary_media_object.stream.seek(0)
            data = await self.ffmpeg_controller.extract_thumbnail(
                video_data=primary_media_object.stream.getvalue(),
                format=primary_media_object.metadata.ext or "mp4",
            )
            if data:
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
            self.log.warning("Thumbnail was not obtained.")

        self.log.info(
            f"Media Found:\n- Filename: {primary_file_object.filename}\n"
            f"- Size: {primary_file_object.metadata.size} bytes\n"
            f"- Media type: {primary_file_object.metadata.media_type}\n"
            f"- Website: {primary_file_object.metadata.extractor}"
        )

        return Media(content=primary_file_object, thumbnail=thumbnail_file_object)
