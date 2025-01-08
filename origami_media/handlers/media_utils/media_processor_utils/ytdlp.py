import asyncio
import json
import shlex
from io import BytesIO
from typing import TYPE_CHECKING, List

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

        active_preset = self.config.ytdlp.get("active_preset")

        if not active_preset:
            raise ValueError("No active preset configured.")

        presets = self.config.ytdlp.get("presets", [])
        if not presets:
            raise ValueError("No commands configured.")

        active_formats = next(
            (p["formats"] for p in presets if p["name"] == active_preset), None
        )
        if not active_formats:
            raise ValueError(f"No formats found for preset '{active_preset}'.")

        result_commands = []
        escaped_url = shlex.quote(url)
        query_flags = "-s -j"
        output_arg = "-"

        proxy = ""
        if self.config.ytdlp.get("proxy_enabled", False):
            proxy_config = self.config.ytdlp.get("proxy")
            if proxy_config:
                proxy = f"--proxy {shlex.quote(proxy_config)}"

        for format_entry in active_formats:
            if not format_entry:
                self.log.warning(
                    f"MediaProcessor.create_ytdlp_commands: Format missing in command {active_preset}"
                )
                continue

            if command_type == "query":
                result_commands.append(
                    {
                        "name": active_preset,
                        "command": f"yt-dlp -q --no-warnings {query_flags} {proxy} -f '{format_entry}' {escaped_url}",
                    }
                )
            else:
                result_commands.append(
                    {
                        "name": active_preset,
                        "command": f"yt-dlp -q --no-warnings {proxy} -f '{format_entry}' -o '{output_arg}' {escaped_url}",
                    }
                )

        return result_commands

    async def ytdlp_execute_query(self, commands: List[dict]) -> dict:
        for command_entry in commands:
            name = command_entry.get("name", "Unnamed Command")
            command = command_entry.get("command")

            if not command:
                self.log.warning(f"Skipping empty command entry: {name}")
                continue

            self.log.info(f"Running yt-dlp command: {name} → {command}")

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
                    self.log.warning(f"{name} failed: {error_message}")

                    if any(code in error_message for code in ["403"]):
                        self.log.error(
                            f"{name}: Non-retryable error detected. Stopping retries."
                        )
                        break

                    raise Exception(
                        f"{name}: Command failed with return code {process.returncode}."
                    )

                output = stdout.decode().strip()
                if not output:
                    self.log.warning(f"{name}: Command produced empty output.")
                    continue

                return json.loads(output)

            except Exception as e:
                self.log.exception(f"{name}: An error occurred: {e}")
                raise Exception(f"{name}: {e}")

            finally:
                if process and process.returncode is None:
                    self.log.warning(
                        f"{name}: Process still running. Forcing termination."
                    )
                    try:
                        process.kill()
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        self.log.error(
                            f"{name}: Process termination timed out. Skipping cleanup."
                        )
                    except Exception as e:
                        self.log.exception(
                            f"{name}: Unexpected error during process termination: {e}"
                        )
                    finally:
                        if process.returncode is None:
                            self.log.critical(
                                f"{name}: Process is stuck and could not be terminated after multiple attempts."
                            )

        raise RuntimeError("No valid yt-dlp query command succeeded.")

    async def ytdlp_execute_download(self, commands: List[dict]) -> bytes:
        for command_entry in commands:
            name = command_entry.get("name", "Unnamed Command")
            command = command_entry.get("command")

            if not command:
                self.log.warning(f"Skipping empty download command: {name}")
                continue

            self.log.info(f"Executing yt-dlp download command: {name} → {command}")

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
                        f"{name}: Process stdout is None, cannot proceed with download."
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
                        raise Exception(
                            f"{name}: File size limit exceeded ({total_size} > {max_file_size} bytes)."
                        )

                    video_data.write(chunk)

                stdout, stderr = await process.communicate()

                if process.returncode != 0:
                    error_message = (
                        stderr.decode().strip() or "No error message captured."
                    )
                    self.log.warning(f"{name} download failed: {error_message}")

                    if any(code in error_message for code in ["403"]):
                        self.log.error(
                            f"{name}: Non-retryable error detected. Stopping retries."
                        )
                        break

                    raise Exception(
                        f"{name}: Download failed with return code {process.returncode}."
                    )

                if total_size == 0:
                    self.log.warning(f"{name}: Downloaded data is empty.")
                    continue

                video_data.seek(0)
                self.log.info(
                    f"Final BytesIO size: {video_data.getbuffer().nbytes} bytes"
                )
                return video_data.getvalue()

            except Exception as e:
                self.log.exception(f"{name}: An unexpected error occurred: {e}")
                raise

            finally:
                if process and process.returncode is None:
                    self.log.warning(
                        f"{name}: Process still running. Forcing termination."
                    )
                    process.kill()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        self.log.error(
                            f"{name}: Process termination timed out. Moving on."
                        )
                    except Exception as e:
                        self.log.exception(
                            f"{name}: Unexpected error during process termination: {e}"
                        )
                    finally:
                        if process.returncode is None:
                            self.log.critical(
                                f"{name}: Process is stuck and could not be terminated."
                            )

        raise RuntimeError("No valid yt-dlp download command succeeded.")
