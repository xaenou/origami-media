from __future__ import annotations

import os
import subprocess
import sys
from types import ModuleType
from typing import Optional


class DependencyHandler:
    def __init__(self, log):
        self.log = log

    async def _respond_success(self, event, title: str, details: str):
        message = f"""**{title}** {details}"""
        await event.respond(message)

    async def _respond_error(self, event, title: str, error: str):
        message = f"""**Error Details:** {error} **{title}**"""
        await event.respond(message)

    async def _format_check_result(self, title: str, result: dict) -> str:
        if result["status"]:
            details = (
                f"**Location:** `{result['location']}`"
                f"**Version:** `{result['version']}`"
                f"**Status:** Available"
            )
        else:
            details = "**Status:** Unavailable\n" f"**Error:** {result['error']}"

        return f"**{title}**\n{details}"

    async def _check_python_version(self, event=None) -> dict:
        try:
            version = sys.version.split()[0]
            location = sys.executable

            self.log.info(
                f"DependencyHandler._check_python_version: Version={version}, Location={location}"
            )

            if event:
                await self._respond_success(
                    event,
                    "Python Environment Status",
                    f"- Version: {version}\n- Executable Location: {location}",
                )
            return {"status": True, "version": version, "location": location}
        except Exception as e:
            self.log.error(
                f"DependencyHandler._check_python_version: Failed to check Python version: {str(e)}"
            )
            if event:
                await self._respond_error(
                    event,
                    "Failed to Check Python Version",
                    str(e),
                )
            return {"status": False, "error": str(e)}

    async def check_yt_import(self, event=None) -> dict:
        try:
            import yt_dlp  # type: ignore

            version = getattr(yt_dlp, "__version__", "Unknown")
            location = os.path.dirname(yt_dlp.__file__)

            self.log.info(
                f"DependencyHandler.check_yt_import: Version={version}, Location={location}"
            )

            if event:
                await self._respond_success(
                    event,
                    "Python yt-dlp Library Status",
                    f"- Version: {version}\n- Location: {location}",
                )
            return {"status": True, "version": version, "location": location}
        except Exception as e:
            self.log.error(
                f"DependencyHandler.check_yt_import: Failed to check yt-dlp library: {str(e)}"
            )
            if event:
                await self._respond_error(
                    event,
                    "Failed to Check yt-dlp Library",
                    str(e),
                )
            return {"status": False, "error": str(e)}

    async def check_yt_cli(self, event=None) -> dict:
        try:
            result = subprocess.run(
                ["yt-dlp", "--version"], capture_output=True, text=True, check=True
            )
            version = result.stdout.strip() if result.stdout else "Unknown"

            yt_dlp_location = subprocess.run(
                ["which", "yt-dlp"], capture_output=True, text=True, check=True
            ).stdout.strip()

            self.log.info(
                f"DependencyHandler.check_yt_cli: Version={version}, Location={yt_dlp_location}"
            )

            if event:
                await self._respond_success(
                    event,
                    "Command-line yt-dlp Tool Status",
                    f"- Version: {version}\n- Location: {yt_dlp_location}",
                )
            return {"status": True, "version": version, "location": yt_dlp_location}
        except Exception as e:
            self.log.error(
                f"DependencyHandler.check_yt_cli: Failed to check yt-dlp CLI: {str(e)}"
            )
            if event:
                await self._respond_error(
                    event,
                    "Failed to Check yt-dlp CLI",
                    str(e),
                )
            return {"status": False, "error": str(e)}

    async def check_ffmpeg_import(self, event=None) -> dict:
        try:
            from mautrix.util import ffmpeg

            location = os.path.dirname(ffmpeg.__file__)
            version = "Available (via mautrix.util.ffmpeg)"

            self.log.info(
                f"DependencyHandler.check_ffmpeg_import: Version={version}, Location={location}"
            )

            if event:
                await self._respond_success(
                    event,
                    "Python FFmpeg Library Status",
                    f"- Version: {version}\n- Location: {location}",
                )
            return {"status": True, "version": version, "location": location}
        except Exception as e:
            self.log.error(
                f"DependencyHandler.check_ffmpeg_import: Failed to check FFmpeg import: {str(e)}"
            )
            if event:
                await self._respond_error(
                    event,
                    "Failed to Check FFmpeg Library",
                    str(e),
                )
            return {"status": False, "error": str(e)}

    async def check_ffmpeg_cli(self, event=None) -> dict:
        try:
            result = subprocess.run(
                ["ffmpeg", "-version"], capture_output=True, text=True, check=True
            )
            version_line = result.stdout.splitlines()[0]
            version = (
                version_line.split(" ")[2] if "version" in version_line else "Unknown"
            )

            ffmpeg_location = subprocess.run(
                ["which", "ffmpeg"], capture_output=True, text=True, check=True
            ).stdout.strip()

            self.log.info(
                f"DependencyHandler.check_ffmpeg_cli: Version={version}, Location={ffmpeg_location}"
            )

            if event:
                await self._respond_success(
                    event,
                    "Command-line FFmpeg Tool Status",
                    f"- Version: {version}\n- Location: {ffmpeg_location}",
                )
            return {"status": True, "version": version, "location": ffmpeg_location}
        except Exception as e:
            self.log.error(
                f"DependencyHandler.check_ffmpeg_cli: Failed to check FFmpeg CLI: {str(e)}"
            )
            if event:
                await self._respond_error(
                    event,
                    "Failed to Check FFmpeg CLI",
                    str(e),
                )
            return {"status": False, "error": str(e)}

    async def run_all_checks(self, event=None) -> dict:
        self.log.info("DependencyHandler.run_all_checks: Starting dependency checks.")

        results = {
            "python": await self._check_python_version(),
            "yt_dlp_lib": await self.check_yt_import(),
            "yt_dlp_cli": await self.check_yt_cli(),
            "ffmpeg_lib": await self.check_ffmpeg_import(),
            "ffmpeg_cli": await self.check_ffmpeg_cli(),
        }

        if event:
            success_count = sum(1 for r in results.values() if r["status"])
            total_count = len(results)

            header = (
                "🔍 **Dependency Check Summary**\n"
                f"✅ **{success_count}/{total_count} dependencies available**\n\n"
                "**📋 Detailed Results:**\n"
            )

            sections = "\n\n".join(
                [
                    await self._format_check_result(
                        "**Python Environment**", results["python"]
                    ),
                    await self._format_check_result(
                        "**yt-dlp Library**", results["yt_dlp_lib"]
                    ),
                    await self._format_check_result(
                        "**yt-dlp CLI**", results["yt_dlp_cli"]
                    ),
                    await self._format_check_result(
                        "**FFmpeg Library**", results["ffmpeg_lib"]
                    ),
                    await self._format_check_result(
                        "**FFmpeg CLI**", results["ffmpeg_cli"]
                    ),
                ]
            )

            message = f"{header}\n{sections}\n"
            await event.respond(message)

        self.log.info(f"DependencyHandler.run_all_checks: Results: {results}")
        return results

    async def get_ytdlp(self) -> Optional[ModuleType]:
        try:
            import yt_dlp  # type: ignore

            self.log.info("yt-dlp Python package is available")
            return yt_dlp
        except ImportError:
            self.log.warning("yt-dlp Python package is not available")
        return None
