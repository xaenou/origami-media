import asyncio
import json
import re
import subprocess
from io import BytesIO
from typing import Optional, Tuple
from urllib.parse import urlparse

from aiohttp import ClientSession
from mautrix.util.ffmpeg import probe_bytes

from .media_models import ThumbnailData, ThumbnailMetadata, VideoData, VideoMetadata


class MediaHandler:
    def __init__(self, config, log):
        self.config = config
        self.log = log

    def _create_ytdlp_command(self, url: str) -> Optional[str]:
        commands = self.config.ytdlp.get("commands", [])
        if not commands or "command" not in commands[0]:
            self.log.error(
                "MediaProcessor._create_ytdlp_command: Invalid yt-dlp command configuration."
            )
            return None
        command_template = commands[0]["command"]
        return f"{command_template} {url}"

    async def _run_ytdlp_command(self, command: str) -> Optional[dict]:
        try:
            self.log.info(f"Running yt-dlp command: {command}")
            process = await asyncio.create_subprocess_shell(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                self.log.error(f"yt-dlp failed: {stderr.decode().strip()}")
                return None

            output = stdout.decode().strip()
            if not output:
                self.log.error("yt-dlp output is empty.")
                return None

            self.log.debug(f"yt-dlp output: {output}")
            return json.loads(output)
        except json.JSONDecodeError as e:
            self.log.error(f"Failed to parse yt-dlp output: {e}")
            return None
        except Exception as e:
            self.log.exception(f"MediaProcessor._run_ytdlp_command: {e}")
            return None

    async def _stream_to_memory(self, stream_url: str) -> BytesIO | None:
        video_data = BytesIO()
        max_retries = 1

        for attempt in range(1, max_retries + 1):
            try:
                async with ClientSession() as session:
                    async with session.get(stream_url, timeout=30) as response:
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
                                "max_file_size", 10485760
                            ):
                                self.log.warning(
                                    f"Stream exceeded max size limit of {self.config.other.get('max_file_size', 10485760)} bytes. Aborting."
                                )
                                return None

                            video_data.write(chunk)

                video_data.seek(0)
                return video_data

            except Exception as e:
                self.log.warning(f"Attempt {attempt}: Error streaming video: {e}")
                await asyncio.sleep(1)

        self.log.error(
            f"MediaProcessor._stream_to_memory: Failed to stream video after {max_retries} attempts."
        )
        return None

    async def _get_media_metadata(self, data: bytes, media_type: str) -> dict:
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
                    f"MediaProcessor._get_media_metadata: No video stream found in {media_type}."
                )
                return {}

            return {
                "width": stream.get("width"),
                "height": stream.get("height"),
                "duration": float(stream.get("duration", 0)),
            }
        except Exception as e:
            self.log.exception(f"MediaProcessor._get_media_metadata: {e}")
            return {}

    async def process_url(
        self, url: str
    ) -> Tuple[Optional[VideoData], Optional[ThumbnailData]]:
        if not url.strip():
            self.log.warning("MediaProcessor.process_url: Invalid URL.")
            raise Exception

        command = self._create_ytdlp_command(url)
        if not command:
            self.log.warning(
                "MediaProcessor.process_url: Invalid command, check config."
            )
            raise Exception

        video_info = await self._run_ytdlp_command(command)
        if not video_info:
            self.log.warning(
                "MediaProcessor.process_url: Failed to find video with yt_dlp"
            )
            raise Exception

        video_data = await self._stream_to_memory(video_info["url"])
        if not video_data:
            self.log.warning("MediaProcessor.process_url: Failed to download video.")
            raise Exception

        video_metadata = await self._get_media_metadata(video_data.getvalue(), "video")

        if video_data.getbuffer().nbytes > self.config.other.get(
            "max_file_size", 10485760
        ):
            self.log.warning(
                f"MediaProcessor.process_url: {url} exceeded file size, aborting."
            )
            raise Exception

        video = VideoData(
            stream=video_data,
            info=VideoMetadata(
                url=video_info["url"],
                id=video_info["id"],
                title=video_info.get("title"),
                uploader=video_info.get("uploader"),
                ext=video_info.get("ext"),
                duration=video_metadata.get("duration"),
                width=video_metadata.get("width"),
                height=video_metadata.get("height"),
            ),
            size=video_data.getbuffer().nbytes,
        )

        thumbnail: Optional[ThumbnailData] = None
        if video_info.get("thumbnail"):
            thumbnail_data = await self._stream_to_memory(video_info["thumbnail"])
            if thumbnail_data:
                thumbnail_metadata = await self._get_media_metadata(
                    thumbnail_data.getvalue(), "thumbnail"
                )
                thumbnail = ThumbnailData(
                    stream=thumbnail_data,
                    info=ThumbnailMetadata(
                        url=video_info.get("thumbnail"),
                        width=thumbnail_metadata.get("width"),
                        height=thumbnail_metadata.get("height"),
                    ),
                    size=thumbnail_data.getbuffer().nbytes,
                )
            else:
                self.log.warning(
                    "MediaProcessor.process_url: Failed to download thumbnail."
                )

            self.log.info(
                f"Video Found:\n- Title: {video.info.title}\n- Uploader: {video.info.uploader}\n"
                f"- Resolution: {video.info.width}x{video.info.height}\n"
                f"- Duration: {video.info.duration}s"
            )

        return (video, thumbnail)


class SynapseHandler:
    def __init__(self, log, client):
        self.log = log
        self.client = client

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
