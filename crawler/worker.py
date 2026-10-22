from threading import Thread

from inspect import getsource
from utils.download import download
from utils import get_logger
import scraper
import time


class Worker(Thread):
    def __init__(self, worker_id, config, frontier):
        self.logger = get_logger(f"Worker-{worker_id}", "Worker")
        self.config = config
        self.frontier = frontier
        # basic check for requests in scraper
        assert {getsource(scraper).find(req) for req in {"from requests import", "import requests"}} == {-1}, "Do not use requests in scraper.py"
        assert {getsource(scraper).find(req) for req in {"from urllib.request import", "import urllib.request"}} == {-1}, "Do not use urllib.request in scraper.py"
        super().__init__(daemon=True)
        
    def run(self):
        while True:
            tbd_url = self.frontier.get_tbd_url()
            if not tbd_url:
                self.logger.info("Frontier is empty. Stopping Crawler.")
                break
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
                self.logger.error("Network connection failed for %s: %s. Waiting before retry...", tbd_url, resp.error)
                time.sleep(5)  # Wait for VPN or server to recover instead of shutting down
                continue       # Retry the worker loop instead of returning
            
            scraped_urls = scraper.scraper(tbd_url, resp)
            for scraped_url in scraped_urls:
                self.frontier.add_url(scraped_url)
            self.frontier.mark_url_complete(tbd_url)
            time.sleep(self.config.time_delay)
