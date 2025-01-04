import asyncio
import json
import os
import shlex
import subprocess
from io import BytesIO
from typing import TYPE_CHECKING, List, Optional, Tuple, Union

from aiohttp import ClientError

if TYPE_CHECKING:
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.origami_media import Config


class Ytdlp:
    def __init__(self, config: "Config", log: "TraceLogger"):
        self.config = config
        self.log = log

    def create_ytdlp_commands(self, url: str, command_type: str) -> List[dict]:
        if command_type not in ("query", "download"):
            raise ValueError("command_type must be 'query' or 'download'")

        commands = self.config.ytdlp.get("presets", [])
        if not commands:
            self.log.error(
                "MediaProcessor.create_ytdlp_commands: No yt-dlp commands configured."
            )
            return []

        result_commands = []
        escaped_url = shlex.quote(url)
        query_flags = "-s -j"
        output_arg = self.config.file.get("output_path", "-")

        for command_entry in commands:
            name = command_entry.get("name", "Unnamed Command")
            base_format = command_entry.get("format")

            if not base_format:
                self.log.warning(
                    f"MediaProcessor.create_ytdlp_commands: Format missing in command {name}"
                )
                continue

            if command_type == "query":
                result_commands.append(
                    {
                        "name": name,
                        "command": f"yt-dlp -q --no-warnings {query_flags} -f '{base_format}' {escaped_url}",
                    }
                )
            else:
                result_commands.append(
                    {
                        "name": name,
                        "command": f"yt-dlp -q --no-warnings -f '{base_format}' -o '{output_arg}' {escaped_url}",
                    }
                )

            for idx, fallback_format in enumerate(
                command_entry.get("fallback_formats", [])
            ):
                fallback_name = f"{name} (Fallback {idx + 1})"
                if command_type == "query":
                    result_commands.append(
                        {
                            "name": fallback_name,
                            "command": f"yt-dlp -q --no-warnings {query_flags} -f '{fallback_format}' {escaped_url}",
                        }
                    )
                else:
                    result_commands.append(
                        {
                            "name": fallback_name,
                            "command": f"yt-dlp -q --no-warnings -f '{fallback_format}' -o '{output_arg}' {escaped_url}",
                        }
                    )

        return result_commands

    async def ytdlp_execute_query(self, commands: List[dict]) -> Optional[dict]:
        for command_entry in commands:
            name = command_entry.get("name", "Unnamed Command")
            command = command_entry.get("command")

            if not command:
                self.log.warning(
                    f"MediaProcessor._ytdlp_execute_query: Skipping empty command entry: {name}"
                )
                continue

            try:
                self.log.info(
                    f"MediaProcessor._ytdlp_execute_query: Running yt-dlp command: {name} → {command}"
                )
                process = await asyncio.create_subprocess_shell(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=100
                )

                if process.returncode != 0:
                    error_message = stderr.decode().strip()
                    self.log.warning(
                        f"MediaProcessor._ytdlp_execute_query: {name} failed: {error_message}"
                    )

                    if "403" in error_message or "404" in error_message:
                        self.log.error(
                            f"MediaProcessor._ytdlp_execute_query: {name}: Non-retryable error detected. Stopping retries."
                        )
                        break

                    continue

                output = stdout.decode().strip()
                if not output:
                    self.log.warning(
                        f"MediaProcessor._ytdlp_execute_query: {name} produced empty output."
                    )
                    continue

                return json.loads(output)

            except json.JSONDecodeError as e:
                self.log.error(
                    f"MediaProcessor._ytdlp_execute_query: {name} failed to parse JSON output: {e}"
                )
            except asyncio.TimeoutError:
                self.log.error(
                    f"MediaProcessor._ytdlp_execute_query: {name}: Command timed out. Killing process."
                )
                process.kill()
                await process.wait()
            except ClientError as e:
                self.log.warning(
                    f"MediaProcessor._ytdlp_execute_query: {name} encountered a network error: {e}"
                )
            except Exception as e:
                self.log.exception(
                    f"MediaProcessor._ytdlp_execute_query: {name} encountered an error: {e}"
                )

        self.log.error(
            "MediaProcessor._ytdlp_execute_query: All yt-dlp commands (including fallbacks) failed."
        )
        return None

    async def ytdlp_execute_download(
        self, commands: List[dict]
    ) -> Optional[Union[BytesIO, str]]:
        for command_entry in commands:
            name = command_entry.get("name", "Unnamed Command")
            command = command_entry.get("command")

            if not command:
                self.log.warning(
                    f"MediaProcessor._ytdlp_execute_download: Skipping empty download command: {name}"
                )
                continue

            try:
                self.log.info(
                    f"MediaProcessor._ytdlp_execute_download: Executing yt-dlp download command: {name} → {command}"
                )

                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                if process.stdout is None:
                    self.log.error(
                        f"MediaProcessor._ytdlp_execute_download: {name}: Process stdout is None, cannot proceed with download."
                    )
                    await process.wait()
                    return None

                video_data = BytesIO()
                total_size = 0
                output_path = self.config.file.get("output_path", "-")
                in_memory = output_path == "-"

                max_file_size_key = (
                    "max_in_memory_file_size" if in_memory else "max_file_size"
                )
                max_file_size = self.config.file.get(max_file_size_key, 0)

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
                            f"MediaProcessor._ytdlp_execute_download: {name}: File size limit exceeded ({total_size} > {max_file_size} bytes)."
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
                    self.log.warning(
                        f"MediaProcessor._ytdlp_execute_download: {name} download failed: {error_message}"
                    )

                    if "403" in error_message or "404" in error_message:
                        self.log.error(
                            f"MediaProcessor._ytdlp_execute_download: {name}: Non-retryable error detected. Stopping retries."
                        )
                        break
                    continue

                if total_size == 0:
                    self.log.warning(
                        f"MediaProcessor._ytdlp_execute_download: {name}: Downloaded data is empty."
                    )
                    return None

                video_data.seek(0)

                if in_memory:
                    self.log.info(
                        "MediaProcessor._ytdlp_execute_download: Saving output to memory (BytesIO)"
                    )
                    self.log.info(
                        f"MediaProcessor._ytdlp_execute_download: Final BytesIO size: {video_data.getbuffer().nbytes} bytes"
                    )
                    return video_data

                if output_path and output_path != "-":
                    resolved_path = os.path.abspath(output_path)
                    if os.path.exists(resolved_path):
                        self.log.info(
                            f"MediaProcessor._ytdlp_execute_download: {name}: Video downloaded successfully to '{resolved_path}'."
                        )
                        return resolved_path
                    else:
                        self.log.warning(
                            f"MediaProcessor._ytdlp_execute_download: {name}: Expected output file '{resolved_path}' does not exist."
                        )
                        continue

            except asyncio.TimeoutError:
                self.log.warning(
                    f"MediaProcessor._ytdlp_execute_download: {name}: Download timed out."
                )
            except Exception as e:
                self.log.exception(
                    f"MediaProcessor._ytdlp_execute_download: {name}: An unexpected error occurred: {e}"
                )
            finally:
                if process.returncode is None:
                    self.log.warning(
                        f"MediaProcessor._ytdlp_execute_download: {name}: Process still running. Forcing termination."
                    )
                    process.kill()
                    await process.wait()

        self.log.error(
            "MediaProcessor._ytdlp_execute_download: All yt-dlp download commands (including fallbacks) failed."
        )
        return None
