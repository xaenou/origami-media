from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse

if TYPE_CHECKING:
    from maubot.matrix import MaubotMessageEvent
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.main import Config


class UrlHandler:
    def __init__(self, config: "Config", log: "TraceLogger"):
        self.config = config
        self.log = log

    DETECT_YOUTUBE_TRACKERS_SI = re.compile(
        r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([a-zA-Z0-9_-]+).*?\?si=([a-zA-Z0-9_-]+)",
        re.IGNORECASE,
    )

    EXTRACT_YOUTUBE_VIDEO_ID = re.compile(
        r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([a-zA-Z0-9_-]+)"
    )

    TIMESTAMP_REGEX = re.compile(r"[?&]t=(\d+)")

    REMOVE_BACKTICKS_REGEX = re.compile(r"`.*?`|```.*?```", re.DOTALL | re.IGNORECASE)

    URL_REGEX = re.compile(r"\bhttps?:\/\/(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?:\/\S*)?\b")

    def _extract_urls(self, message):
        clean_message = self.REMOVE_BACKTICKS_REGEX.sub("", message)
        urls = re.findall(self.URL_REGEX, clean_message)
        self.log.info(f"Filtered: {urls}")
        return list(dict.fromkeys(urls))

    def _validate_domain(self, url: str, check_whitelist: bool) -> str | None:
        domain = urlparse(url).netloc.split(":")[0].split(".")[-2:]
        domain = ".".join(domain).lower()

        if check_whitelist:
            whitelist = {platform["domain"] for platform in self.config.platforms}
            if domain not in whitelist:
                self.log.warning(f"Invalid or unwhitelisted domain: {domain}")
                return None

        return domain

    def _process_youtube_url(self, url: str) -> str | None:
        video_match = self.EXTRACT_YOUTUBE_VIDEO_ID.search(url)
        if not video_match:
            self.log.warning(f"Invalid YouTube URL: {url}.")
            return None

        video_id = video_match.group(1)
        timestamp_match = self.TIMESTAMP_REGEX.search(url)
        timestamp = f"&t={timestamp_match.group(1)}" if timestamp_match else ""
        return f"https://www.youtube.com/watch?v={video_id}{timestamp}"

    def process(
        self, event: "MaubotMessageEvent"
    ) -> Optional[tuple[list[str], str, bool, bool]]:
        valid_urls = []
        message = str(event.content.body)
        exceeds_url_limit = False
        whitelist = False
        if self.config.meta.get("use_platform_domains_as_whitelist", True):
            whitelist = True

        urls = self._extract_urls(message)
        if len(urls) > self.config.queue.get("max_message_url_count", 1):
            self.log.warning("urls exceed message limit.")
            exceeds_url_limit = True

        if not urls:
            self.log.warning("No urls found in message.")
            return None

        sanitized_message = message
        url_mapping = {}
        should_censor = False

        for url in urls:
            try:
                domain = self._validate_domain(url, check_whitelist=whitelist)
                if not domain:
                    continue

                processed_url = url

                if domain in ["youtube.com", "youtu.be"]:
                    processed_url = self._process_youtube_url(url)
                    if processed_url:
                        valid_urls.append(processed_url)
                        url_mapping[url] = processed_url
                else:
                    valid_urls.append(url)

            except Exception:
                self.log.error(f"Error processing {url}")

        if self.config.meta.get(
            "censor_trackers", True
        ) and self.DETECT_YOUTUBE_TRACKERS_SI.search(message):
            for original, processed in url_mapping.items():
                sanitized_message = sanitized_message.replace(original, processed)
                should_censor = True

        if not valid_urls:
            self.log.warning("No valid urls were processed.")
            return None

        unique_valid_urls = list(dict.fromkeys(valid_urls))

        return unique_valid_urls, sanitized_message, should_censor, exceeds_url_limit

    def process_query_url_string(self, message: str) -> list[str]:
        valid_urls = []
        urls = self._extract_urls(message)

        if len(urls) > self.config.queue.get("max_message_url_count", 3):
            raise Exception("urls exceed message limit.")

        if not urls:
            raise Exception("No urls found in message.")

        url_mapping = {}

        for url in urls:
            try:
                domain = self._validate_domain(url, check_whitelist=False)
                if not domain:
                    continue

                processed_url = url

                if domain in ["youtube.com", "youtu.be"]:
                    processed_url = self._process_youtube_url(url)
                    if processed_url:
                        valid_urls.append(processed_url)
                        url_mapping[url] = processed_url
                else:
                    valid_urls.append(url)

            except Exception:
                self.log.error(f"Error processing {url}")

        if not valid_urls:
            raise Exception("No valid urls were processed.")

        unique_valid_urls = list(dict.fromkeys(valid_urls))

        return unique_valid_urls
