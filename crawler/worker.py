from threading import Thread
from inspect import getsource
from urllib.parse import urlparse
from utils.download import download
from utils import get_logger
import scraper
import time
import re

PROCESSED_HOSTS = set()

class Worker(Thread):
    def __init__(self, worker_id, config, frontier):
        self.logger = get_logger(f"Worker-{worker_id}", "Worker")
        self.config = config
        self.frontier = frontier
        
        # Configure global user agent in scraper for robots.txt checking consistency
        ua = getattr(self.config, "user_agent", "*")
        scraper.set_user_agent(ua)

        # basic check for requests in scraper
        assert {getsource(scraper).find(req) for req in {"from requests import", "import requests"}} == {-1}, "Do not use requests in scraper.py"
        assert {getsource(scraper).find(req) for req in {"from urllib.request import", "import urllib.request"}} == {-1}, "Do not use urllib.request in scraper.py"
        super().__init__(daemon=True)

    def process_domain_discovery(self, hostname):
        if not hostname or hostname in PROCESSED_HOSTS:
            return "ready"

        robots_url = f"https://{hostname}/robots.txt"
        visited = set()

        # Follow at most five redirects, always through the cache.
        for _ in range(6):
            if robots_url in visited:
                self.logger.error("robots.txt redirect loop for %s", hostname)
                return "stop"
            visited.add(robots_url)

            resp = download(robots_url, self.config, self.logger)
            time.sleep(self.config.time_delay)

            if resp.status == 0 or 500 <= resp.status <= 599:
                return "retry"

            if resp.status in (301, 302, 303, 307, 308):
                raw = resp.raw_response
                location = raw.headers.get("Location") if raw is not None else None
                target = scraper.prepare_url(location, robots_url) if location else None
                if target is None:
                    self.logger.error(
                        "Missing or filtered robots redirect: %s", robots_url
                    )
                    return "stop"
                robots_url = target
                continue

            raw = resp.raw_response
            if resp.status == 200 and raw is not None and raw.content:
                content = raw.content.decode("utf-8", errors="ignore")
                scraper.register_robots(hostname, content)

                for sitemap in re.findall(
                    r"^\s*Sitemap:\s*(https?://\S+)", content, re.I | re.M
                ):
                    clean = scraper.prepare_url(sitemap)
                    if clean:
                        self.frontier.add_url(clean)

            PROCESSED_HOSTS.add(hostname)
            return "ready"

        self.logger.error("robots.txt exceeded five redirects for %s", hostname)
        return "stop"

    def run(self):
        while True:
            tbd_url = self.frontier.get_tbd_url()
            if not tbd_url:
                self.logger.info("Frontier is empty. Stopping Crawler.")
                break

            # Perform domain discovery (robots.txt & sitemap) on new hostnames
            hostname = (urlparse(tbd_url).hostname or "").lower()
            discovery = self.process_domain_discovery(hostname)
            if discovery != "ready":
                self.frontier.requeue_url(tbd_url)
                if discovery == "stop":
                    break
                time.sleep(5)
                continue

            # Check robots.txt permissions before fetching (2. Consistent User Agent)
            if not scraper.is_allowed_by_robots(tbd_url):
                self.logger.info("Skipping robots.txt disallowed URL: %s", tbd_url)
                self.frontier.mark_url_complete(tbd_url)
                time.sleep(self.config.time_delay)
                continue

            resp = download(tbd_url, self.config, self.logger)
            self.logger.info(
                f"Downloaded {tbd_url}, status <{resp.status}>, "
                f"using cache {self.config.cache_server}.")
            
            # Handle Spacetime caching server status codes (600-606)
            if 600 <= resp.status <= 699:
                self.logger.info("Skipping disallowed/cache-error URL: %s (Status %s)", tbd_url, resp.status)
                self.frontier.mark_url_complete(tbd_url)
                time.sleep(self.config.time_delay)
                continue

            # Handle network connection drop (status == 0)
            if resp.status == 0:
                self.logger.error("Network connection failed for %s: %s. Re-queueing...", tbd_url, resp.error)
                self.frontier.requeue_url(tbd_url)
                time.sleep(5)
                continue
            
            scraped_urls = scraper.scraper(tbd_url, resp)
            for scraped_url in scraped_urls:
                self.frontier.add_url(scraped_url)
            self.frontier.mark_url_complete(tbd_url)
            time.sleep(self.config.time_delay)