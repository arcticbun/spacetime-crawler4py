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
        """Fetches robots.txt once per domain and queues ONLY explicitly declared sitemaps."""
        if not hostname or hostname in PROCESSED_HOSTS:
            return

        # Single request via standard HTTPS
        robots_url = f"https://{hostname}/robots.txt"
        resp = download(robots_url, self.config, self.logger)
        time.sleep(self.config.time_delay)  # Politeness delay

        # Network connection failure (status == 0) -> retry on next pass
        if resp.status == 0:
            self.logger.warning("Network connection failed while fetching robots.txt for %s. Will retry.", hostname)
            return

        # Mark hostname processed so we only check robots.txt once
        PROCESSED_HOSTS.add(hostname)

        if resp.status == 200 and resp.raw_response and resp.raw_response.content:
            try:
                robots_content = resp.raw_response.content.decode("utf-8", errors="ignore")
                scraper.register_robots(hostname, robots_content)

                # ONLY queue sitemaps explicitly declared via "Sitemap:" directives in robots.txt
                found_sitemaps = re.findall(r"(?i)^\s*Sitemap:\s*(https?://\S+)", robots_content, re.MULTILINE)
                for sm_url in found_sitemaps:
                    clean_sm = scraper.prepare_url(sm_url.strip())
                    if clean_sm and scraper.is_valid(clean_sm):
                        self.frontier.add_url(clean_sm)
            except Exception:
                pass

    def run(self):
        while True:
            tbd_url = self.frontier.get_tbd_url()
            if not tbd_url:
                self.logger.info("Frontier is empty. Stopping Crawler.")
                break

            # Perform domain discovery (robots.txt & sitemap) on new hostnames
            hostname = (urlparse(tbd_url).hostname or "").lower()
            if hostname and hostname not in PROCESSED_HOSTS:
                self.process_domain_discovery(hostname)

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