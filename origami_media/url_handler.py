import re
from urllib.parse import urlparse


class UrlHandler:
    def __init__(self, config, log):
        self.config = config
        self.log = log

    DETECT_YOUTUBE_TRACKERS = re.compile(
        r"https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]+)(?:[?&]\S+)+",
        re.IGNORECASE,
    )

    EXTRACT_YOUTUBE_VIDEO_ID = re.compile(
        r"https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]+)"
    )

    TIMESTAMP_REGEX = re.compile(r"[?&]t=(\d+)")

    URL_REGEX = re.compile(r"\bhttps?:\/\/(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?:\/\S*)?\b")

    def _extract_urls(self, message):
        parts = re.split(r"<code>.*?</code>", message, flags=re.DOTALL | re.IGNORECASE)
        urls = []

        for i, part in enumerate(parts):
            if i % 2 == 0:
                found_urls = re.findall(self.URL_REGEX, part)
                urls.extend(found_urls)

        return urls

    def _validate_domain(self, url: str) -> str | None:
        domain = urlparse(url).netloc.split(":")[0].split(".")[-2:]
        domain = ".".join(domain).lower()

        if domain not in self.config.whitelist:
            self.log.warning(f"Invalid or unwhitelisted domain: {domain}")
            return None

        return domain

    def _process_youtube_url(self, url: str) -> str | None:
        video_match = self.EXTRACT_YOUTUBE_VIDEO_ID.search(url)
        if not video_match:
            self.log.warning("Invalid YouTube URL.")
            return None

        video_id = video_match.group(1)
        timestamp_match = self.TIMESTAMP_REGEX.search(url)
        timestamp = f"&t={timestamp_match.group(1)}" if timestamp_match else ""
        return f"https://www.youtube.com/watch?v={video_id}{timestamp}"

    async def process(self, event):
        valid_urls = []
        message = event.content.body

        urls = self._extract_urls(message)
        if len(urls) > self.config.queue.get("max_message_url_count", 3):
            self.log.warning("URLs exceed message limit.")
            return valid_urls

        if not urls:
            self.log.warning("No URLs found in the message.")
            return valid_urls

        sanitized_message = message
        url_mapping = {}

        for url in urls:
            try:
                domain = self._validate_domain(url)
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
                self.log.error(f"Error processing URL {url}")

        if self.config.meta.get(
            "censor_trackers", True
        ) and self.DETECT_YOUTUBE_TRACKERS.search(message):
            for original, processed in url_mapping.items():
                sanitized_message = sanitized_message.replace(original, processed)

            await event.redact(reason="Redacted for tracking URL.")
            await event.reply(content=sanitized_message)

        unique_valid_urls = list(dict.fromkeys(valid_urls))

        return unique_valid_urls, event
