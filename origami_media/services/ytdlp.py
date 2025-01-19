from __future__ import annotations

import asyncio
import glob
import json
import os
import shlex
from typing import TYPE_CHECKING, List

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
        self,
        url: str,
        command_type: str,
        platform_config: dict,
        uuid: str,
        modifier=None,
    ) -> List[dict]:
        if command_type not in ("query", "download"):
            raise ValueError("command_type must be 'query' or 'download'")

        formats = platform_config.get("ytdlp_formats")
        if not formats:
            raise ValueError(f"No formats set for {platform_config['name']}")

        result_commands = []
        escaped_url = shlex.quote(url)
        query_flags = "-s -j"

        output_arg = f"/tmp/{uuid}"

        # Optional configurations
        proxy = (
            f"--proxy '{platform_config.get('proxy')}'"
            if platform_config.get("enable_proxy")
            else ""
        )
        user_agent = (
            f"--user-agent '{platform_config.get('custom_user_agent')}'"
            if platform_config.get("enable_custom_user_agent")
            else ""
        )
        cookies = (
            f"--cookies '/tmp/{platform_config['name']}-cookies.txt'"
            if platform_config.get("enable_cookies")
            else ""
        )

        if command_type == "query":
            if modifier == "force_audio_only":
                result_commands.append(
                    {
                        "command": f"yt-dlp -q --no-warnings {query_flags} {cookies} {user_agent} {proxy} -x {escaped_url}",
                        "selected_format": "audio_only",
                    }
                )
            else:
                for format_entry in formats:
                    if not format_entry:
                        raise ValueError(
                            f"Format missing for {platform_config['name']}"
                        )
                    result_commands.append(
                        {
                            "command": f"yt-dlp -q --no-warnings {query_flags} {cookies} {user_agent} {proxy} -f '{format_entry}' {escaped_url}",
                            "selected_format": format_entry,
                        }
                    )

        elif command_type == "download":
            if modifier == "force_audio_only":
                output_option = f"-P '{output_arg}'"
                result_commands.append(
                    {
                        "command": f"yt-dlp -q --no-warnings {cookies} {user_agent} {proxy} -x --audio-format mp3 --embed-thumbnail {output_option} {escaped_url}",
                        "selected_format": "audio_only",
                    }
                )
            else:
                for format_entry in formats:
                    output_option = f"-P '{output_arg}'"
                    result_commands.append(
                        {
                            "command": f"yt-dlp -q --no-warnings {cookies} {user_agent} {proxy} -f '{format_entry}' {output_option} {escaped_url}",
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

    async def ytdlp_execute_download(self, commands: List[dict], uuid: str) -> bytes:
        last_exception = None
        download_dir = f"/tmp/{uuid}/"

        for command_entry in commands:
            command = command_entry.get("command")
            format = command_entry.get("selected_format")

            if not command:
                self.log.warning("Skipping empty download command.")
                continue

            self.log.info(f"Executing yt-dlp download command {format} → {command}")

            process = None
            try:
                if not os.path.exists(download_dir):
                    os.makedirs(download_dir, exist_ok=True)

                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=1024 * 1024 * 10,
                )

                stdout, stderr = await process.communicate()

                if process.returncode != 0:
                    error_message = (
                        stderr.decode().strip() or "No error message captured."
                    )
                    self.log.warning(f"Download failed: {error_message}")

                    # Non-retryable error handling
                    if any(code in error_message for code in ["403"]):
                        self.log.error(
                            "Non-retryable error detected. Stopping retries."
                        )
                        break

                    raise Exception(
                        f"Download failed with return code {process.returncode}."
                    )

                # Locate the downloaded file
                downloaded_files = glob.glob(os.path.join(download_dir, "*"))
                if not downloaded_files:
                    raise FileNotFoundError(f"No files found in {download_dir}")

                if len(downloaded_files) > 1:
                    raise RuntimeError(
                        f"Multiple files found in {download_dir}, unable to determine correct file: {downloaded_files}"
                    )

                file_path = downloaded_files[0]
                self.log.info(f"Located downloaded file: {file_path}")

                # Read the file content as bytes
                with open(file_path, "rb") as f:
                    video_data = f.read()

                self.log.info(f"Downloaded file size: {len(video_data)} bytes")
                return video_data

            except Exception as e:
                self.log.exception(f"An error occurred with command {command}: {e}")
                last_exception = e

            finally:
                if os.path.exists(download_dir):
                    try:
                        for file in os.listdir(download_dir):
                            file_path = os.path.join(download_dir, file)
                            os.remove(file_path)
                        os.rmdir(download_dir)
                        self.log.debug(f"Cleaned up directory {download_dir}")
                    except Exception as cleanup_error:
                        self.log.warning(
                            f"Failed to clean up {download_dir}: {cleanup_error}"
                        )

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

        if last_exception:
            raise RuntimeError(
                "No valid yt-dlp download command succeeded. See logs for details."
            ) from last_exception
        else:
            raise RuntimeError("No valid yt-dlp download command succeeded.")
