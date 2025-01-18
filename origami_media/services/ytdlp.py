from __future__ import annotations

import asyncio
import json
import shlex
from io import BytesIO
from typing import TYPE_CHECKING, List
from urllib.parse import urlparse

if TYPE_CHECKING:
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.main import Config


class DownloadSizeExceededError(Exception):
    def __init__(self, name: str, total_size: int, max_file_size: int):
        super().__init__(
            f"{name}: File size limit exceeded ({total_size} > {max_file_size} bytes)."
        )
        self.name = name
        self.total_size = total_size
        self.max_file_size = max_file_size


class Ytdlp:
    def __init__(self, config: "Config", log: "TraceLogger"):
        self.config = config
        self.log = log

    def create_ytdlp_commands(
        self, url: str, command_type: str, platform_config: dict
    ) -> List[dict]:
        if command_type not in ("query", "download"):
            raise ValueError("command_type must be 'query' or 'download'")

        formats = platform_config.get("ytdlp_formats")
        if not formats:
            raise ValueError(f"No formats set for {platform_config['name']}")

        result_commands = []
        escaped_url = shlex.quote(url)
        query_flags = "-s -j"
        output_arg = "-"

        proxy = ""
        if platform_config.get("enable_proxy", False):
            proxy_config = platform_config.get("proxy")
            if proxy_config:
                proxy = f"--proxy '{proxy_config}'"

        user_agent = ""
        if platform_config.get("enable_custom_user_agent"):
            custom_user_agent = platform_config.get("custom_user_agent")
            if custom_user_agent:
                user_agent = f"--user-agent '{custom_user_agent}'"

        cookies = ""
        if platform_config.get("enable_cookies"):
            cookies_dir = f"/tmp/{platform_config['name']}-cookies.txt"
            if cookies_dir:
                cookies = f"--cookies '{cookies_dir}'"

        if command_type == "query":
            for format_entry in formats:
                if not format_entry:
                    raise ValueError(f"Format missing for {platform_config['name']}")
                result_commands.append(
                    {
                        "command": f"yt-dlp -q --no-warnings {query_flags} {cookies} {user_agent} {proxy} -f '{format_entry}' {escaped_url}",
                        "selected_format": format_entry,
                    }
                )
        else:
            for format_entry in formats:
                result_commands.append(
                    {
                        "command": f"yt-dlp -q --no-warnings {cookies} {user_agent} {proxy} -f '{format_entry}' -o '{output_arg}' {escaped_url}",
                        "selected_format": format_entry,
                    }
                )

        return result_commands

    async def ytdlp_execute_query(self, commands: List[dict]) -> dict:
        for command_entry in commands:
            command = command_entry.get("command")
            format = command_entry.get("selected_format")

            if not command:
                self.log.warning("Skipping empty command entry.")
                continue

            self.log.info(f"Running yt-dlp command {format} → {command}")

            try:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=30
                )

                if process.returncode != 0:
                    error_message = (
                        stderr.decode().strip() or "No error message captured."
                    )
                    self.log.warning(f"failed: {error_message}")

                    if any(code in error_message for code in ["403"]):
                        self.log.error(
                            "Non-retryable error detected. Stopping retries."
                        )
                        return {"error": error_message}

                    # Skip this command and move to the next
                    continue

                output = stdout.decode().strip()
                if not output:
                    self.log.warning("Command produced empty output.")
                    continue

                ytdlp_dict = json.loads(output)
                if not ytdlp_dict:
                    continue
                ytdlp_dict["selected_format"] = format

                return ytdlp_dict

            except Exception as e:
                self.log.exception(f"An error occurred: {e}")
                # Skip this command and move to the next
                continue

            finally:
                if process and process.returncode is None:
                    self.log.warning("Process still running. Forcing termination.")
                    try:
                        process.kill()
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        self.log.error(
                            "Process termination timed out. Skipping cleanup."
                        )
                    except Exception as e:
                        self.log.exception(
                            "Unexpected error during process termination: {e}"
                        )
                    finally:
                        if process.returncode is None:
                            self.log.critical(
                                "Process is stuck and could not be terminated after multiple attempts."
                            )

        raise RuntimeError("No valid yt-dlp query command succeeded.")

    async def ytdlp_execute_download(self, commands: List[dict]) -> bytes:
        for command_entry in commands:
            command = command_entry.get("command")
            format = command_entry.get("selected_format")

            if not command:
                self.log.warning("Skipping empty download command.")
                continue

            self.log.info(f"Executing yt-dlp download command {format} → {command}")

            process = None
            try:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=1024 * 1024 * 10,
                )

                if not process.stdout:
                    raise Exception(
                        "Process stdout is None, cannot proceed with download."
                    )

                video_data = BytesIO()
                total_size = 0
                max_file_size = self.config.file.get("max_in_memory_file_size", 0)

                while True:
                    chunk = await process.stdout.read(1024 * 1024)
                    if not chunk:
                        break

                    chunk_size = len(chunk)
                    total_size += chunk_size

                    if max_file_size > 0 and total_size > max_file_size:
                        raise DownloadSizeExceededError(
                            "name", total_size, max_file_size
                        )

                    video_data.write(chunk)

                stdout, stderr = await process.communicate()

                if process.returncode != 0:
                    error_message = (
                        stderr.decode().strip() or "No error message captured."
                    )
                    self.log.warning(f"download failed: {error_message}")

                    if any(code in error_message for code in ["403"]):
                        self.log.error(
                            "Non-retryable error detected. Stopping retries."
                        )
                        break

                    raise Exception(
                        f"Download failed with return code {process.returncode}."
                    )

                if total_size == 0:
                    self.log.warning("Downloaded data is empty.")
                    continue

                video_data.seek(0)
                self.log.info(
                    f"Final BytesIO size: {video_data.getbuffer().nbytes} bytes"
                )
                return video_data.getvalue()

            except Exception as e:
                self.log.exception(f"An unexpected error occurred: {e}")
                raise

            finally:
                if process and process.returncode is None:
                    self.log.warning("Process still running. Forcing termination.")
                    process.kill()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        self.log.error("Process termination timed out. Moving on.")
                    except Exception as e:
                        self.log.exception(
                            f"Unexpected error during process termination: {e}"
                        )
                    finally:
                        if process.returncode is None:
                            self.log.critical(
                                "Process is stuck and could not be terminated."
                            )

        raise RuntimeError("No valid yt-dlp download command succeeded.")
