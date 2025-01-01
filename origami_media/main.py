import asyncio
import grp
import os
import pwd
import stat
import subprocess
from typing import Any, Dict, Type, cast

from maubot.handlers import command, event
from maubot.matrix import MaubotMessageEvent
from maubot.plugin_base import Plugin
from mautrix.types import EventType
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from .dependency_handler import DependencyHandler
from .media_handler import MediaHandler
from .url_handler import UrlHandler


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper):
        helper.copy("meta")
        helper.copy("whitelist")
        helper.copy("ytdlp")
        helper.copy("ffmpeg")
        helper.copy("file")
        helper.copy("queue")

    @property
    def meta(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("meta", {}))

    @property
    def whitelist(self) -> list[str]:
        return cast(list[str], self.get("whitelist", []))

    @property
    def ytdlp(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("ytdlp", {}))

    @property
    def ffmpeg(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("ffmpeg", {}))

    @property
    def file(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("file", {}))

    @property
    def queue(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("queue", {}))


class OrigamiMedia(Plugin):
    config: Config

    async def start(self):
        self.log.info(f"Starting Origami Media")
        await super().start()

        if not self.config:
            raise Exception("Config is not initialized")

        self.config.load_and_update()

        self.dependency_handler = DependencyHandler(log=self.log)
        self.url_handler = UrlHandler(log=self.log, config=self.config)
        self.media_handler = MediaHandler(
            log=self.log, client=self.client, config=self.config
        )
        self.valid_urls = asyncio.Queue()
        self.event_queue = asyncio.Queue(maxsize=self.config.queue.get("max_size", 100))

        self.workers = [
            asyncio.create_task(self._message_worker()),
            asyncio.create_task(self._pipeline_worker()),
        ]

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    @cast(Any, event.on)(EventType.ROOM_MESSAGE)
    async def main(self, event: MaubotMessageEvent) -> None:
        if not self.config.meta.get("enable_passive", False):
            return
        if (
            not event.content.msgtype.is_text
            or event.sender == self.client.mxid
            or cast(str, event.content.body).startswith("!")
        ):
            return
        if "http" not in event.content.body and "www" not in event.content.body:
            return
        try:
            self.event_queue.put_nowait(event)
            self.log.info("Event added to the processing queue.")
        except asyncio.QueueFull:
            self.log.warning("Message queue is full. Dropping incoming message.")

    async def _message_worker(self) -> None:
        while True:
            try:
                event = await self.event_queue.get()
                valid_urls, event = await self.url_handler.process(event)
                if valid_urls:
                    for url in valid_urls:
                        await self.valid_urls.put((url, event))
                    self.log.info(f"[Message Worker] Stored valid URLs: {valid_urls}")
                else:
                    self.log.info("[Message Worker] No valid URLs found.")
            except asyncio.CancelledError:
                self.log.info("[Message Worker] Shutting down gracefully.")
                break
            except Exception as e:
                self.log.error(f"[Message Worker] Error: {e}")
            finally:
                self.event_queue.task_done()

    async def _pipeline_worker(self) -> None:
        while True:
            try:
                item = await self.valid_urls.get()
                url, event = item
                self.log.info(f"[Pipeline Worker] Sending URL to MediaPipeline: {url}")
                await self.media_handler.process(event=event, url=url)
            except asyncio.CancelledError:
                self.log.info("[Pipeline Worker] Shutting down gracefully.")
                break
            except Exception as e:
                self.log.error(
                    f"[Pipeline Worker] Failed to process URL {url} in MediaPipeline: {e}"
                )
            finally:
                self.valid_urls.task_done()

    async def stop(self) -> None:
        self.log.info("Shutting down workers...")
        for task in self.workers:
            task.cancel()

        results = await asyncio.gather(*self.workers, return_exceptions=True)
        for task, result in zip(self.workers, results):
            if isinstance(result, Exception) and not isinstance(
                result, asyncio.CancelledError
            ):
                self.log.error(
                    f"Task {task.get_name()} failed during shutdown: {result}"
                )

        self.log.info("All workers stopped cleanly.")
        await super().stop()

    @command.new(name="ov")
    async def ov(self, event: MaubotMessageEvent) -> None:
        if not self.config.meta.get("enable_active", False):
            await event.respond("Active commands are currently disabled.")
            self.log.info("Active commands are disabled. Ignoring `dl` command.")
            return

        content = (
            "**Origami Media Commands**\n\n"
            "**Available commands:**\n"
            "• `!ov dl <url>` — Download and post a video from a URL\n"
            "   Example: `!ov dl https://example.com/video`\n\n"
            "• `!ov check` — Check if all required dependencies are installed\n"
            "   Example: `!ov check` "
        )

        await event.respond(content)
        return

    @ov.subcommand(name="dl")
    @command.argument(name="url", pass_raw=True)
    async def dl(self, event: MaubotMessageEvent, url: str) -> None:
        if not self.config.meta.get("enable_active", False):
            await event.respond("Active commands are currently disabled.")
            self.log.info("Active commands are disabled. Ignoring `dl` command.")
            return

        await self.media_handler.process(event=event, url=url)

    @ov.subcommand(name="check")
    async def check(self, event: MaubotMessageEvent) -> None:
        if not self.config.meta.get("enable_active", False):
            await event.respond("Active commands are currently disabled.")
            self.log.info("Active commands are disabled. Ignoring `check` command.")
            return

        await self.dependency_handler.run_all_checks(event=event)

    @ov.subcommand(name="debug")
    async def debug(self, event: MaubotMessageEvent):
        if self.config.meta.get("debug", False):
            directory = "/media_tmp"
            self.log.info(f"[DEBUG] Starting directory checks for: {directory}")

            ## 1. User and Group Context
            try:
                uid = os.geteuid()
                gid = os.getegid()
                euid = os.geteuid()
                egid = os.getegid()

                try:
                    username = pwd.getpwuid(uid).pw_name
                except KeyError:
                    username = f"unknown_user (UID: {uid})"

                try:
                    groupname = grp.getgrgid(gid).gr_name
                except KeyError:
                    groupname = f"unknown_group (GID: {gid})"

                self.log.info(
                    f"[DEBUG] Script is running as user: {username} (UID: {uid}, GID: {gid})"
                )
                self.log.info(
                    f"[DEBUG] Effective User ID (EUID): {euid}, Effective Group ID (EGID): {egid}"
                )
            except Exception as e:
                self.log.exception(
                    f"[DEBUG] Error retrieving user and group information: {e}"
                )
                uid, gid, euid, egid = -1, -1, -1, -1
                username, groupname = "unknown_user", "unknown_group"

            ## 2. Directory Existence and Type
            try:
                if not os.path.exists(directory):
                    self.log.warning(f"[DEBUG] Directory '{directory}' does not exist.")
                    return

                if os.path.islink(directory):
                    real_path = os.readlink(directory)
                    self.log.warning(
                        f"[DEBUG] '{directory}' is a symbolic link pointing to '{real_path}'."
                    )
                    directory = real_path

                if not os.path.isdir(directory):
                    self.log.warning(
                        f"[DEBUG] Path '{directory}' exists but is not a directory."
                    )
                    return
                else:
                    self.log.info(f"[DEBUG] '{directory}' is a valid directory.")
            except Exception as e:
                self.log.exception(
                    f"[DEBUG] Error while checking directory properties: {e}"
                )

            ## 3. Directory Permissions
            try:
                can_read = os.access(directory, os.R_OK)
                can_write = os.access(directory, os.W_OK)
                can_execute = os.access(directory, os.X_OK)

                self.log.info(f"[DEBUG] Read Permission: {'Yes' if can_read else 'No'}")
                self.log.info(
                    f"[DEBUG] Write Permission: {'Yes' if can_write else 'No'}"
                )
                self.log.info(
                    f"[DEBUG] Execute Permission: {'Yes' if can_execute else 'No'}"
                )
            except Exception as e:
                self.log.exception(
                    f"[DEBUG] Error while checking directory permissions: {e}"
                )

            ## 4. Directory Ownership
            try:
                stat_info = os.stat(directory)
                owner_uid = stat_info.st_uid
                owner_gid = stat_info.st_gid
                permissions = oct(stat.S_IMODE(stat_info.st_mode))

                self.log.info(
                    f"[DEBUG] Directory Owner UID: {owner_uid}, GID: {owner_gid}"
                )
                self.log.info(f"[DEBUG] Directory Permissions (Octal): {permissions}")
            except Exception as e:
                self.log.exception(
                    f"[DEBUG] Error retrieving ownership and permissions: {e}"
                )

            ## 5. Filesystem Type
            try:
                result = subprocess.run(
                    ["df", "-T", directory],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=True,
                )
                self.log.info(f"[DEBUG] Filesystem info:\n{result.stdout}")
            except subprocess.CalledProcessError as e:
                self.log.warning(
                    f"[DEBUG] Failed to fetch filesystem info: {e.stderr.strip()}"
                )
            except Exception as e:
                self.log.exception(f"[DEBUG] Error retrieving filesystem type: {e}")

            ## 6. Directory Contents
            try:
                if can_execute:
                    contents = os.listdir(directory)
                    self.log.info(f"[DEBUG] Directory contents: {contents}")
                else:
                    self.log.warning(
                        "[DEBUG] No execute permission to list directory contents."
                    )
            except Exception as e:
                self.log.exception(
                    f"[DEBUG] Error while listing directory contents: {e}"
                )

            ## 7. Write Test
            write_test = False
            test_file_path = os.path.join(
                directory, f"debug_write_test_{os.getpid()}.txt"
            )
            try:
                if can_write:
                    with open(test_file_path, "w") as test_file:
                        test_file.write("Test Write Permission")
                    self.log.info(f"[DEBUG] Write test succeeded at '{test_file_path}'")
                    write_test = True
                else:
                    self.log.warning(
                        "[DEBUG] No write permission to perform write test."
                    )
            except PermissionError:
                self.log.warning("[DEBUG] Write test failed: Permission denied.")
            except Exception as e:
                self.log.exception(f"[DEBUG] Error while performing write test: {e}")
            finally:
                if os.path.exists(test_file_path):
                    try:
                        os.remove(test_file_path)
                        self.log.info(
                            f"[DEBUG] Cleanup: Test file removed at '{test_file_path}'"
                        )
                    except Exception as e:
                        self.log.warning(f"[DEBUG] Cleanup failed: {e}")

            ## 8. Final Debug Report
            self.log.info(
                f"\n📂 **Directory Debug Report for '{directory}'**"
                f"\n👤 User Context: {username} (UID: {uid}, Group: {groupname}, GID: {gid}, EUID: {euid}, EGID: {egid})"
                f"\n🟢 Exists: Yes"
                f"\n🟢 Is Directory: Yes"
                f"\n🟢 Read Permission: {'Yes' if can_read else 'No'}"
                f"\n🟢 Write Permission: {'Yes' if can_write else 'No'}"
                f"\n🟢 Execute (List) Permission: {'Yes' if can_execute else 'No'}"
                f"\n📝 Write Test: {'Success' if write_test else 'Failed'}"
                f"\n📄 Contents: {', '.join(contents) if can_execute and contents else 'No Contents'}"
                f"\n🔑 Owner UID: {owner_uid}, Owner GID: {owner_gid}"
                f"\n🔑 Permissions: {permissions}"
            )
