from __future__ import annotations

import asyncio
import os
from io import BytesIO
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.main import Config


class Native:
    def __init__(self, config: "Config", log: "TraceLogger", http: "ClientSession"):
        self.config = config
        self.log = log
        self.http = http

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

    async def is_magic(self, url: str) -> bool:
        try:
            async with self.http.head(
                url,
                allow_redirects=True,
            ) as response:
                ctype = response.headers.get("Content-Type", "").lower()
                self.log.debug(
                    f"MediaProcessor._is_image: HEAD Content-Type for {url}: {ctype}"
                )
                if ctype.startswith("image/"):
                    return True

            async with self.http.get(url, allow_redirects=True) as response:
                ctype = response.headers.get("Content-Type", "").lower()
                if ctype.startswith("image/"):
                    return True

                # Read the first 12 bytes to check the magic number
                first_bytes = await response.content.read(12)
                if self._is_image_magic_number(first_bytes):
                    return True

        except Exception as e:
            self.log.warning(
                f"MediaProcessor._is_image: Failed to determine if URL is an image ({url}): {e}"
            )
            return False

        return False

    async def client_download(self, url, platform_config: dict) -> bytes:
        max_retries = 1
        max_file_size = self.config.file.get("max_in_memory_file_size", 0)

        proxy = None
        if platform_config["enable_proxy"]:
            proxy = platform_config.get("proxy")

        headers = {}
        if platform_config["enable_custom_user_agent"]:
            user_agent = platform_config["custom_user_agent"]
            if user_agent:
                headers["User-Agent"] = user_agent

        self.log.info(
            f"client_download: Starting stream from '{url}' to memory. "
            f"client_download: Max size limit: {max_file_size} bytes"
        )

        for attempt in range(1, max_retries + 1):
            try:
                response = await self.http.get(url, proxy=proxy, headers=headers)
                if response.status != 200:
                    self.log.warning(
                        f"client_download: Attempt {attempt}: {url}: {response.status}"
                    )
                    continue

                total_bytes = 0

                output = BytesIO()
                async for chunk in response.content.iter_chunked(8192):
                    chunk_size = len(chunk)
                    total_bytes += chunk_size

                    if max_file_size > 0 and total_bytes > max_file_size:
                        self.log.error(
                            f"client_download: Stream size exceeded limit ({total_bytes} > {max_file_size} bytes). Aborting."
                        )
                        raise

                    output.write(chunk)

                output.seek(0)
                self.log.info(
                    f"client_download: Streamed {total_bytes} bytes into memory."
                )
                return output.getvalue()

            except Exception as e:
                self.log.warning(
                    f"client_download: Attempt {attempt}: Error streaming data: {e}"
                )

            await asyncio.sleep(1)

        self.log.error(
            f"client_download: Failed to stream data after {max_retries} attempts."
        )
        raise

    def write_to_directory(self, content, directory, file_name):
        if not os.path.exists(directory):
            os.makedirs(directory)

        file_path = os.path.join(directory, file_name)

        try:
            with open(file_path, "w") as file:
                file.write(content)

            self.log.info(f"Successfully written to {file_path}")
            return True
        except Exception as e:
            self.log.info(f"An error occurred: {e}")
            return False

    def read_from_file(self, directory: str, file_name: str) -> str:
        file_path = os.path.join(directory, file_name)
        try:
            with open(file_path, "r") as file:
                return file.read()
        except FileNotFoundError as e:
            self.log.info(
                f"python: File {file_path} not found. Please ensure it exists."
            )
            raise e
        except Exception as e:
            self.log.info(f"python: An error occurred: {e}")
            raise e

    def file_exists(self, directory: str, file_name: str) -> bool:
        file_path = os.path.join(directory, file_name)
        return os.path.isfile(file_path)
