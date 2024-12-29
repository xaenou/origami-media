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

    URL_REGEX = re.compile('\bhttps?:\/\/[^\s<>"]+')

    def _extract_urls(self, message):
        urls = re.findall(self.URL_REGEX, message)
        return urls

    def _validate_domain(self, url: str) -> str | None:
        domain = urlparse(url).netloc.split(":")[0].split(".")[-2:]
        domain = ".".join(domain)

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
        self.log.info("Processing message for URLs...")
        valid_urls = []

        urls = self._extract_urls(event.content.body)
        if len(urls) > self.config.queue.get("max_message_url_count", 3):
            self.log.warning("URLs exceed message limit.")
            return valid_urls
        if not urls:
            self.log.warning("No URLs found in the message.")
            return valid_urls

        for url in urls:
            try:
                domain = self._validate_domain(url)
                if not domain:
                    continue

                if domain == "youtube.com":
                    processed_url = self._process_youtube_url(url)
                    if processed_url:
                        valid_urls.append(processed_url)
                        self.log.info(f"Valid YouTube URL: {processed_url}")
                else:
                    valid_urls.append(url)
                    self.log.info(f"Valid URL: {url}")
            except Exception as e:
                self.log.error(f"Error processing URL {url}: {e}")

        return valid_urls, event
