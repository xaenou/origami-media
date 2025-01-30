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
        self,
        query: str,
        provider: str,
        data_dict: dict,
        api_key: Optional[str] = None,
    ) -> Optional[str]:
        async def fetch_url(url):
            proxy = None
            if self.config.platform_configs["query"]["enable_proxy"]:
                proxy = self.config.platform_configs["query"].get("proxy")

            headers = {}
            if self.config.platform_configs["query"]["enable_custom_user_agent"]:
                user_agent = self.config.platform_configs["query"]["custom_user_agent"]
                if user_agent:
                    headers["User-Agent"] = user_agent

            async with self.http.get(url, proxy=proxy, headers=headers) as response:
                if response.status != 200:
                    self.log.error(f"Failed request to {url}: {await response.text()}")
                    return None
                return await response.json()

        if provider == "tenor":
            rating = "off"
            api_version = "v2"
            url_params = urllib.parse.urlencode(
                {"q": query, "key": api_key, "contentfilter": rating}
            )
            base_url = f"https://g.tenor.com/{api_version}/search?{url_params}"
            data = await fetch_url(base_url)
            if not data:
                return None
            results = data.get("results", [])
            if not results:
                return None
            result = random.choice(results)
            gif = (
                result["media_formats"]["gif"]
                if api_version == "v2"
                else result["media"][0]["gif"]
            )
            return gif["url"]

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
            data = await fetch_url(base_url)
            if not data:
                return None
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
            return gif["url"]

        if provider == "unsplash":
            url_params = urllib.parse.urlencode({"query": query, "client_id": api_key})
            base_url = f"https://api.unsplash.com/search/photos?{url_params}"
            data = await fetch_url(base_url)
            if not data:
                return None
            results = data.get("results", [])
            if not results:
                return None
            result = random.choice(results)
            return result["urls"]["regular"]

        if provider == "lexica":
            url_params = urllib.parse.urlencode({"q": query})
            base_url = f"https://lexica.art/api/v1/search?{url_params}"
            data = await fetch_url(base_url)
            if not data:
                return None
            results = data.get("images", [])
            if not results:
                return None
            result = random.choice(results)
            return result["src"]

        if provider == "waifu":
            url_params = urllib.parse.urlencode(
                {
                    "included_tags": "waifu",
                }
            )
            base_url = f"https://api.waifu.im/search?{url_params}"
            data = await fetch_url(base_url)
            if not data:
                return None
            results = data.get("images", [])
            if not results:
                return None
            result = random.choice(results)
            return result.get("url")

        if provider == "danbooru":
            base_url = "https://danbooru.donmai.us/posts.json"
            default_search_tags = ["1girl", "solo"]
            attempts = 4
            page_types = ["b", "a"]

            edge_post = await fetch_url(base_url)
            if not edge_post:
                return None
            edge_post_id = edge_post[0].get("id")
            if not edge_post_id:
                return None

            random_post_range = [200, edge_post[1].get("id")]

            if not query:
                queries = []
            else:
                queries = query.split()

                for q in queries[:]:
                    if q.lower() == "-solo":
                        default_search_tags.remove("solo")
                        queries.remove(q)
                    if q.lower() == "-1girl":
                        default_search_tags.remove("1girl")
                        queries.remove(q)

                for q in queries[:]:
                    if q.lower().startswith("date"):
                        date_query = q
                        encoded_date_query = urllib.parse.quote(date_query, safe="")
                        date_post = await fetch_url(
                            base_url + "?tags=" + encoded_date_query
                        )
                        date_post_old = await fetch_url(
                            base_url + "?tags=order%3Aid+" + encoded_date_query
                        )
                        if not date_post_old or not date_post:
                            return
                        random_post_range[0] = date_post_old[1].get("id")
                        random_post_range[1] = date_post[1].get("id")
                        queries.remove(q)

            formatted_query = urllib.parse.quote(" ".join(queries), safe="")

            filtered_data = []
            for attempt in range(attempts):
                page_type = page_types[attempt % 2]
                random_post_id = random.randrange(
                    random_post_range[0], random_post_range[1]
                )

                full_url = (
                    base_url
                    + "?tags=rating%3Ag%2Cs"
                    + "+"
                    + "+".join(formatted_query.split())
                    + "&limit=200"
                    + f"&page={page_type}"
                    + str(random_post_id)
                )
                self.log.info(f"Attempt {attempt + 1}: Trying {full_url}")

                raw_data = await fetch_url(full_url)
                if raw_data:
                    for item in raw_data:
                        post_id = item.get("id")
                        if random_post_range[0] <= post_id <= random_post_range[1]:
                            if all(
                                elem in item["tag_string"]
                                for elem in default_search_tags
                            ):
                                filtered_data.append(item)

                if filtered_data:
                    break

            if not filtered_data:
                return None

            data = random.choice(filtered_data)
            file_url = data.get("file_url")
            post_id = data.get("id")
            data_dict["post_url"] = f"https://danbooru.donmai.us/posts/{post_id}"

            return file_url

        if provider == "searx":
            url_params = urllib.parse.urlencode(
                {"q": query, "format": "json", "category_images": 1}
            )
            base_url = f"{api_key}?{url_params}"
            data = await fetch_url(base_url)
            if not data:
                return None
            results = data.get("results", [])
            if not results:
                return None
            result = random.choice(results)
            return result.get("img_src")

        self.log.error(f"Unsupported provider: {provider}")
        return None

    async def query_image_controller(
        self, query: str, provider: str, data_dict: dict
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
                    query=query,
                    provider=p,
                    api_key=searx_instance_url,
                    data_dict=data_dict,
                )
            else:
                api_key = query_api_dict.get(f"{p}_api_key", None)
                url = await self._query_image(
                    query=query,
                    provider=p,
                    api_key=api_key,
                    data_dict=data_dict,
                )

            if url:
                return url

        raise Exception("No URL was obtained from any provider.")
