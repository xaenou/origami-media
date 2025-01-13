from __future__ import annotations

import re
import unicodedata
import uuid
from io import BytesIO
from typing import TYPE_CHECKING, Dict, Optional, Tuple
from urllib.parse import urlparse

from mautrix.util.magic import mimetype

from origami_media.models.media_models import Media, MediaFile, MediaInfo
from origami_media.services.ffmpeg import Ffmpeg
from origami_media.services.native import Native
from origami_media.services.ytdlp import DownloadSizeExceededError, Ytdlp

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.main import Config
    from origami_media.services.ffmpeg import FfmpegMetadata


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

    async def _handle_cookies(self) -> None:
        if not self.config.ytdlp.get("enable_cookies"):
            self.log.info("Cookies are not enabled.")
            return

        current_cookie_str = self.config.ytdlp.get("cookies_file")
        cookie_file_path = "/tmp/cookies.txt"

        try:
            # Check if the file exists
            if self.native_controller.file_exists(
                directory="/tmp", file_name="cookies.txt"
            ):
                previous_cookie_str = self.native_controller.read_from_file(
                    directory="/tmp", file_name="cookies.txt"
                )

                # Update the file only if content differs
                if previous_cookie_str != current_cookie_str:
                    self.log.info("Updating cookies.txt as the content has changed.")
                    updated = self.native_controller.write_to_directory(
                        directory="/tmp",
                        file_name="cookies.txt",
                        content=current_cookie_str,
                    )
                    if not updated:
                        self.log.error("Failed to update cookies.txt.")
                        return
                    self.log.info("cookies.txt updated successfully.")
                    return
            else:
                # Write new cookies file if it doesn't exist
                self.log.info("cookies.txt not found. Writing new file.")
                created = self.native_controller.write_to_directory(
                    directory="/tmp",
                    file_name="cookies.txt",
                    content=current_cookie_str,
                )
                if not created:
                    self.log.error("Failed to write cookies.txt.")
                    return
                self.log.info("cookies.txt written successfully.")
                return

        except Exception as e:
            self.log.error(f"An error occurred while handling cookies: {e}")

    async def _attempt_thumbnail_fallback(
        self, ytdlp_metadata: dict
    ) -> Optional[bytes]:
        self.log.info("Attempting to fallback to thumbnail.")
        if not ytdlp_metadata.get("thumbnail"):
            self.log.warning("No thumbnail found.")
            return None

        data = await self._download_simple_media(ytdlp_metadata["thumbnail"])
        return data

    async def _post_process(
        self, data: bytes, modifier=None
    ) -> Optional[Tuple[bytes, FfmpegMetadata]]:
        try:
            _, _, type_ = self._get_mimetype(data)

            if type_ == "video":
                if modifier == "force_audio_only":
                    data = await self.ffmpeg_controller.normalize_audio(data)
                elif self.config.file.get("enable_normalize_videos_to_mp4"):
                    data = await self.ffmpeg_controller.normalize_video(data)
            elif type_ == "audio":
                if self.config.file.get("enable_normalize_audio_to_mp3"):
                    data = await self.ffmpeg_controller.normalize_audio(data)

            processed_data = data
            metadata = await self.ffmpeg_controller.extract_metadata(data)

            return processed_data, metadata
        except Exception as e:
            self.log.warning(f"Error: {e}")
            return None

    async def _download_simple_media(self, url: str) -> Optional[bytes]:
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

    async def _download_advanced_media(
        self, ytdlp_metadata: dict
    ) -> Tuple[Optional[bytes], bool]:
        # The bool is if the thumbnail fallback was used.
        try:
            # Handle live media
            if ytdlp_metadata.get("is_live"):
                if not self.config.ffmpeg.get("enable_livestream_previews"):
                    self.log.warning(
                        "Live media detected, but livestream previews are disabled."
                    )
                    return None, False
                data = await self.ffmpeg_controller.capture_livestream(
                    stream_url=ytdlp_metadata["url"]
                )
                is_thumbnail_fallback = False
                return data, is_thumbnail_fallback

            # Check duration constraints
            duration = ytdlp_metadata.get("duration")
            max_duration = self.config.file.get("max_duration", 0)
            if duration and duration > max_duration:
                self.log.warning("Media length exceeds the configured duration limit.")
                if not self.config.ytdlp.get(
                    "enable_thumbnail_fallback_if_duration_or_size_exceeds"
                ):
                    return None, False
                data = await self._attempt_thumbnail_fallback(ytdlp_metadata)
                is_thumbnail_fallback = True
                return data, is_thumbnail_fallback

            # Check size contraints
            size = ytdlp_metadata.get("filesize_approx")
            max_size = self.config.file.get("max_in_memory_file_size")
            if size and size > max_size:
                self.log.warning("Media size exceeds the configured size limit.")
                if not self.config.ytdlp.get(
                    "enable_thumbnail_fallback_if_duration_or_size_exceeds"
                ):
                    return None, False
                data = await self._attempt_thumbnail_fallback(ytdlp_metadata)
                is_thumbnail_fallback = True
                return data, is_thumbnail_fallback

            try:
                data = await self.ytdlp_controller.ytdlp_execute_download(
                    commands=self.ytdlp_controller.create_ytdlp_commands(
                        ytdlp_metadata["url"], command_type="download"
                    )
                )
                is_thumbnail_fallback = False
                return data, is_thumbnail_fallback

            except DownloadSizeExceededError:
                self.log.warning("Media size exceeds the configured file size limit.")
                if not self.config.ytdlp.get(
                    "enable_thumbnail_fallback_if_duration_or_size_exceeds"
                ):
                    return None, False
                data = await self._attempt_thumbnail_fallback(ytdlp_metadata)
                is_thumbnail_fallback = True
                return data, is_thumbnail_fallback

        except Exception as e:
            self._handle_download_error(f"Failed to handle media: {e}")
            return None, False

    def _generate_filename(self, metadata: dict) -> str:
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

    def _get_mimetype(self, bytes) -> tuple[str, str, str]:
        mime = mimetype(bytes)
        type_, subtype = mime.split("/", 1)
        return mime, subtype, type_

    def _generate_media_filename(self, metadata: dict, extension: str) -> str:
        filename = self._generate_filename(metadata)
        return f"{filename}.{extension}"

    def _handle_download_error(self, message: str) -> None:
        self.log.warning(message)

    def _create_media_object(
        self, stream: bytes, other_metadata: dict, file_metadata: FfmpegMetadata
    ) -> MediaFile:
        mimetype, ext, type_ = self._get_mimetype(stream)

        if other_metadata["origin"] == "thumbnail":
            mimetype = "image/jpeg"
            ext = "jpeg"
            type_ = "image"

        if type_ == "audio":
            mimetype = "audio/mp3"
            ext = "mp3"

        return MediaFile(
            filename=self._generate_media_filename(other_metadata, extension=ext),
            stream=BytesIO(stream),
            metadata=MediaInfo(
                url=other_metadata["url"],
                id=other_metadata["id"],
                origin=other_metadata["origin"],
                title=other_metadata.get("title", "unknown_title"),
                uploader=other_metadata.get("uploader", "unknown_uploader"),
                extractor=other_metadata.get("extractor"),
                ext=ext,
                mimetype=mimetype,
                duration=file_metadata.duration,
                width=file_metadata.width,
                height=file_metadata.height,
                size=len(stream),
                media_type=type_,
                thumbnail_url=other_metadata.get("thumbnail"),
                meta_size=other_metadata.get("meta_size"),
                meta_duration=other_metadata.get("meta_duration"),
            ),
        )

    async def _process_simple_media(
        self, data: bytes, url: str, ffmpeg_metadata: FfmpegMetadata
    ) -> Optional[MediaFile]:
        url_uuid = uuid.uuid5(uuid.NAMESPACE_URL, url)

        metadata = {
            "id": str(url_uuid),
            "extractor": urlparse(url).netloc.split(":")[0],
            "uploader": "unknown_uploader",
            "title": "unknown_title",
            "url": url,
            "origin": "simple",
        }

        return self._create_media_object(data, metadata, ffmpeg_metadata)

    async def _process_advanced_media(
        self,
        data: bytes,
        ytdlp_metadata: Dict,
        ffmpeg_metadata: FfmpegMetadata,
        _is_thumbnail_fallback: bool,
    ) -> Optional[MediaFile]:

        mutated_ytdlp_metadata = {
            "id": ytdlp_metadata.get("id"),
            "extractor": ytdlp_metadata.get("extractor"),
            "uploader": ytdlp_metadata.get("uploader", "unknown_uploader"),
            "title": ytdlp_metadata.get("title", "unknown_title"),
            "url": ytdlp_metadata["url"],
            "origin": "advanced",
            "thumbnail": ytdlp_metadata.get("thumbnail"),
        }

        if _is_thumbnail_fallback:
            mutated_ytdlp_metadata["meta_duration"] = ytdlp_metadata.get("duration")
            mutated_ytdlp_metadata["meta_size"] = ytdlp_metadata.get("filesize_approx")
            mutated_ytdlp_metadata["origin"] = "advanced-thumbnail-fallback"
            mutated_ytdlp_metadata["thumbnail"] = None

        return self._create_media_object(
            data, other_metadata=mutated_ytdlp_metadata, file_metadata=ffmpeg_metadata
        )

    async def _process_thumbnail_media(
        self, data: bytes, url: str, ffmpeg_metadata: FfmpegMetadata
    ) -> Optional[MediaFile]:
        url_uuid = uuid.uuid5(uuid.NAMESPACE_URL, url)
        self.log.info("in thumbnail process")

        metadata = {
            "id": str(url_uuid),
            "extractor": urlparse(url).netloc.split(":")[0],
            "uploader": "unknown_uploader",
            "title": "unknown_title",
            "url": url,
            "origin": "thumbnail",
        }

        return self._create_media_object(data, metadata, ffmpeg_metadata)

    async def _primary_media_controller(
        self, url: str, modifier=None
    ) -> Optional[MediaFile]:
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
            data = await self._download_simple_media(url)
            if data:
                result = await self._post_process(data, modifier=modifier)
                if result:
                    data, metadata = result
                    return await self._process_simple_media(
                        data, ffmpeg_metadata=metadata, url=url
                    )

        ytdlp_metadata = await self._query_advanced_media(url)
        if ytdlp_metadata:
            data, _is_thumbnail_fallback = await self._download_advanced_media(
                ytdlp_metadata=ytdlp_metadata
            )
            if data:
                result = await self._post_process(data, modifier=modifier)
                if result:
                    data, metadata = result
                    return await self._process_advanced_media(
                        data,
                        ffmpeg_metadata=metadata,
                        ytdlp_metadata=ytdlp_metadata,
                        _is_thumbnail_fallback=_is_thumbnail_fallback,
                    )

        return None

    async def _thumbnail_media_controller(
        self, primary_media_object: MediaFile, modifier=None
    ) -> Optional[MediaFile]:
        if (
            primary_media_object.metadata.origin == "advanced"
            and primary_media_object.metadata.thumbnail_url
        ):
            data = await self._download_simple_media(
                primary_media_object.metadata.thumbnail_url
            )
            if data:
                result = await self._post_process(data)
                if result:
                    _, metadata = result
                    return await self._process_thumbnail_media(
                        data,
                        ffmpeg_metadata=metadata,
                        url=primary_media_object.metadata.url,
                    )

        if modifier is not None and modifier == "force_audio_only":
            return

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
                result = await self._post_process(data)
                if result:
                    _, metadata = result
                    return await self._process_thumbnail_media(
                        data,
                        ffmpeg_metadata=metadata,
                        url=primary_media_object.metadata.url,
                    )

        return None

    async def process_url(self, url: str, modifier=None) -> Optional[Media]:
        await self._handle_cookies()

        primary_file_object = await self._primary_media_controller(
            url, modifier=modifier
        )
        if not primary_file_object:
            self.log.warning("Failed to process primary media.")
            return None

        thumbnail_file_object = await self._thumbnail_media_controller(
            primary_file_object, modifier=modifier
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
