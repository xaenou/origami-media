from __future__ import annotations

import random
import urllib.parse
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.main import Config


class QueryHandler:

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

        if provider == "giphy":
            rating = "r"
            if not query:
                endpoint = "random"
                url_params = urllib.parse.urlencode(
                    {"api_key": api_key, "rating": rating}
                )
            else:
                endpoint = "search"
                url_params = urllib.parse.urlencode(
                    {"q": query, "api_key": api_key, "rating": rating}
                )
            self.log.info(f"Giphy:{endpoint} Query: {query} ")
            base_url = f"https://api.giphy.com/v1/gifs/{endpoint}?{url_params}"

            async with self.http.get(base_url) as response:
                data = await response.json()
                if endpoint == "random" or endpoint == "translate":
                    result = data.get("data")
                elif endpoint == "search":
                    results = data.get("data", [])
                    if not results:
                        return None
                    result = random.choice(results)
                else:
                    return None

                if not result or (endpoint != "random" and "images" not in result):
                    return None

                gif = result["images"]["original"] if "images" in result else result
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

        if provider == "searx":
            searx_url = api_key
            url_params = urllib.parse.urlencode(
                {"q": query, "format": "json", "category_images": 1}
            )
            base_url = f"{searx_url}?{url_params}"
            async with self.http.get(base_url) as response:
                data = await response.json()
                results = data.get("results", [])
                if not results:
                    return None
                result = random.choice(results)
                link = result.get("img_src")
            return link

        self.log.error(f"Unsupported provider: {provider}")
        return None

    async def query_image_controller(
        self,
        query: str,
        provider: str,
    ) -> str:
        query_api_dict: dict = self.config.command["query_image"]

        if "|" in provider:
            split_providers = provider.split("|")
        else:
            split_providers = [provider]

        for p in split_providers:
            if p == "searx":
                searx_instance_url = query_api_dict.get("searx_instance")
                if not searx_instance_url:
                    self.log.error("No Searx instance URL configured.")
                    continue

                url = await self._query_image(
                    query=query, provider=p, api_key=searx_instance_url
                )
            else:
                api_key = query_api_dict.get(f"{p}_api_key", None)
                url = await self._query_image(query=query, provider=p, api_key=api_key)

            if url:
                return url

        raise Exception("No URL was obtained from any provider.")
