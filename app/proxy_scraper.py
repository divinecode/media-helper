import asyncio
import re
import logging

import httpx
from bs4 import BeautifulSoup
from typing import List

logger = logging.getLogger(__name__)

class Scraper:

    def __init__(self, method, _url):
        self.method = method
        self._url = _url

    def get_url(self, **kwargs):
        return self._url.format(**kwargs, method=self.method)

    async def get_response(self, client):
        return await client.get(self.get_url())

    async def handle(self, response):
        return response.text

    async def scrape(self, client):
        response = await self.get_response(client)
        proxies = await self.handle(response)
        pattern = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?")
        return re.findall(pattern, proxies)


# From spys.me
class SpysMeScraper(Scraper):

    def __init__(self, method):
        super().__init__(method, "https://spys.me/{mode}.txt")

    def get_url(self, **kwargs):
        mode = "proxy" if self.method == "http" else "socks" if self.method == "socks" else "unknown"
        if mode == "unknown":
            raise NotImplementedError
        return super().get_url(mode=mode, **kwargs)


# From proxyscrape.com
class ProxyScrapeScraper(Scraper):

    def __init__(self, method, timeout=1000, country="All"):
        self.timout = timeout
        self.country = country
        super().__init__(method,
                         "https://api.proxyscrape.com/?request=getproxies"
                         "&proxytype={method}"
                         "&timeout={timout}"
                         "&country={country}")

    def get_url(self, **kwargs):
        return super().get_url(timout=self.timout, country=self.country, **kwargs)

# From geonode.com - A little dirty, grab http(s) and socks but use just for socks
class GeoNodeScraper(Scraper):

    def __init__(self, method, limit="500", page="1", sort_by="lastChecked", sort_type="desc"):
        self.limit = limit
        self.page = page
        self.sort_by = sort_by
        self.sort_type = sort_type
        super().__init__(method,
                         "https://proxylist.geonode.com/api/proxy-list?"
                         "&limit={limit}"
                         "&page={page}"
                         "&sort_by={sort_by}"
                         "&sort_type={sort_type}")

    def get_url(self, **kwargs):
        return super().get_url(limit=self.limit, page=self.page, sort_by=self.sort_by, sort_type=self.sort_type, **kwargs)

# From proxy-list.download
class ProxyListDownloadScraper(Scraper):

    def __init__(self, method, anon):
        self.anon = anon
        super().__init__(method, "https://www.proxy-list.download/api/v1/get?type={method}&anon={anon}")

    def get_url(self, **kwargs):
        return super().get_url(anon=self.anon, **kwargs)


# For websites using table in html
class GeneralTableScraper(Scraper):

    async def handle(self, response):
        soup = BeautifulSoup(response.text, "html.parser")
        proxies = set()
        table = soup.find("table", attrs={"class": "table table-striped table-bordered"})
        for row in table.findAll("tr"):
            count = 0
            proxy = ""
            for cell in row.findAll("td"):
                if count == 1:
                    proxy += ":" + cell.text.replace("&nbsp;", "")
                    proxies.add(proxy)
                    break
                proxy += cell.text.replace("&nbsp;", "")
                count += 1
        return "\n".join(proxies)


scrapers = [
    SpysMeScraper("http"),
    SpysMeScraper("socks"),
    ProxyScrapeScraper("http"),
    ProxyScrapeScraper("socks4"),
    ProxyScrapeScraper("socks5"),
    GeoNodeScraper("socks"),
    ProxyListDownloadScraper("https", "elite"),
    ProxyListDownloadScraper("http", "elite"),
    ProxyListDownloadScraper("http", "transparent"),
    ProxyListDownloadScraper("http", "anonymous"),
    GeneralTableScraper("https", "http://sslproxies.org"),
    GeneralTableScraper("http", "http://free-proxy-list.net"),
    GeneralTableScraper("http", "http://us-proxy.org"),
    GeneralTableScraper("socks", "http://socks-proxy.net"),
]

async def scrape_by_type(method: str) -> List[str]:
    """Scrape proxies of specific type and return as list."""
    methods = [method]
    if method == "socks":
        methods += ["socks4", "socks5"]
        
    proxy_scrapers = [s for s in scrapers if s.method in methods]
    if not proxy_scrapers:
        raise ValueError(f"Method {method} not supported")
        
    proxies = []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        tasks = []
        
        async def scrape_scraper(scraper):
            try:
                logger.debug(f"Scraping from {scraper.get_url()}")
                proxies.extend(await scraper.scrape(client))
            except Exception as e:
                logger.warning(f"Failed to scrape from {scraper.get_url()}: {e}")

        for scraper in proxy_scrapers:
            tasks.append(asyncio.create_task(scrape_scraper(scraper)))

        await asyncio.gather(*tasks)
        
    return list(set(proxies))  # Remove duplicates

async def scrape_proxies() -> List[str]:
    """Scrape both HTTP and SOCKS proxies and return formatted list."""
    try:
        http_proxies = await scrape_by_type("http")
        socks_proxies = await scrape_by_type("socks")
        
        formatted_proxies = []
        formatted_proxies.extend([f"http://{p}" for p in http_proxies])
        formatted_proxies.extend([f"socks5://{p}" for p in socks_proxies])
        
        logger.info(f"Scraped {len(formatted_proxies)} proxies ({len(http_proxies)} HTTP, {len(socks_proxies)} SOCKS)")
        return formatted_proxies
        
    except Exception as e:
        logger.error(f"Error scraping proxies: {e}")
        return []

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    proxies = asyncio.run(scrape_proxies())
    print(f"Found {len(proxies)} proxies:")
    for proxy in proxies:
        print(proxy)
