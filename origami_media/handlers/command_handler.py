import random
import urllib.parse
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.origami_media import Config


class CommandHandler:

    def __init__(self, config: "Config", log: "TraceLogger", http: "ClientSession"):
        self.config = config
        self.log = log
        self.http = http

    async def _query_image(
        self, query: str, provider: str, api_key: Optional[str] = None
    ) -> Optional[str]:

        if provider == "tenor":
            rating = "off"
            api_version = "v2"
            url_params = urllib.parse.urlencode(
                {"q": query, "key": api_key, "contentfilter": rating}
            )
            base_url = f"https://g.tenor.com/{api_version}/search?{url_params}"
            async with self.http.get(base_url) as response:
                data = await response.json()
                results = data.get("results", [])
                if not results:
                    return None
                result = random.choice(results)
                gif = (
                    result["media_formats"]["gif"]
                    if api_version == "v2"
                    else result["media"][0]["gif"]
                )
                link = gif["url"]
            return link

        if provider == "unsplash":
            url_params = urllib.parse.urlencode({"query": query, "client_id": api_key})
            base_url = f"https://api.unsplash.com/search/photos?{url_params}"
            async with self.http.get(base_url) as response:
                data = await response.json()
                results = data.get("results", [])
                if not results:
                    return None
                result = random.choice(results)
                link = result["urls"]["regular"]
            return link

        if provider == "lexica":
            url_params = urllib.parse.urlencode({"q": query})
            base_url = f"https://lexica.art/api/v1/search?{url_params}"
            async with self.http.get(base_url) as response:
                data = await response.json()
                results = data.get("images", [])
                if not results:
                    return None
                result = random.choice(results)
                link = result["src"]
            return link

        if provider == "waifu":
            url_params = urllib.parse.urlencode(
                {
                    "included_tags": "waifu",
                }
            )
            base_url = f"https://api.waifu.im/search?{url_params}"
            async with self.http.get(base_url) as response:
                data = await response.json()
                results = data.get("images", [])
                if not results:
                    return None
                result = random.choice(results)
                link = result.get("url")
            return link

        self.log.error(f"Unsupported provider: {provider}")
        return None

    async def query_image_controller(
        self,
        query: str,
        provider: str,
    ) -> str:
        if not query:
            query = "."

        api_key = None
        if provider in ["tenor", "unsplash"]:
            api_key = self.config.command["query_image"][f"{provider}_api_key"]

        url = await self._query_image(query=query, provider=provider, api_key=api_key)
        if not url:
            raise Exception("No url was obtained.")

        return url
