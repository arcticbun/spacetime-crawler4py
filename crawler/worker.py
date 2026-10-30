from collections import Counter
from threading import Thread
from inspect import getsource
from urllib.parse import urlparse
from utils.download import download
from utils import get_logger
import scraper
import time
import re

# Hostnames whose robots.txt has been fetched (or given up on) during this run.
PROCESSED_HOSTS = set()

# How many times a page or robots.txt download may fail before we give up on it.
MAX_RETRIES = 3

# Seconds to back off after a network failure before trying again.
RETRY_BACKOFF = 5

# Seconds to wait before trying again while the cache server is unreachable.
OUTAGE_BACKOFF = 60

class Worker(Thread):
    def __init__(self, worker_id, config, frontier):
        self.logger = get_logger(f"Worker-{worker_id}", "Worker")
        self.config = config
        self.frontier = frontier

        # Failure counts so a permanently broken URL or host can't stall the crawl
        self.url_failures = Counter()
        self.robots_failures = Counter()

        # Configure global user agent in scraper for robots.txt checking consistency
        ua = getattr(self.config, "user_agent", "*")
        scraper.set_user_agent(ua)

        # basic check for requests in scraper
        assert {getsource(scraper).find(req) for req in {"from requests import", "import requests"}} == {-1}, "Do not use requests in scraper.py"
        assert {getsource(scraper).find(req) for req in {"from urllib.request import", "import urllib.request"}} == {-1}, "Do not use urllib.request in scraper.py"
        super().__init__(daemon=True)

    def delay_for(self, hostname):
        """Politeness delay for a host: the configured delay, or robots.txt Crawl-delay if longer."""
        crawl_delay = scraper.get_crawl_delay(hostname)
        return max(self.config.time_delay, crawl_delay or 0)

    def cache_is_reachable(self, failed_url):
        """Tells a cache outage apart from one bad URL.

        Downloads a seed URL other than the one that failed; if that also gets
        no response, the cache server is down and the failure is not the URL's
        fault. Callers must already have waited the politeness delay since
        their last request, because the probe may hit the same host.
        """
        # Seeds are cleaned like any queued URL (config.ini omits the trailing
        # slash), so the failed URL is recognized and never probed again, and
        # seeds disallowed by robots.txt are skipped
        seeds = [scraper.prepare_url(seed.strip()) or seed.strip() for seed in self.config.seed_urls]
        candidates = [seed for seed in seeds if seed != failed_url and scraper.is_allowed_by_robots(seed)]
        probe_url = candidates[0] if candidates else seeds[0]
        resp = download(probe_url, self.config, self.logger)
        time.sleep(self.delay_for((urlparse(probe_url).hostname or "").lower()))
        return resp.status != 0

    def process_domain_discovery(self, hostname):
        """Fetches a host's robots.txt once, registering its rules and sitemaps.

        Returns "outage" if the cache server is unreachable, "retry" if the
        fetch failed temporarily, otherwise "ready".
        If robots.txt cannot be used (redirect loop, redirect off-domain,
        repeated failures) the host is crawled with no robots rules rather
        than stopping the crawler.
        """
        if not hostname or hostname in PROCESSED_HOSTS:
            return "ready"

        robots_url = f"https://{hostname}/robots.txt"
        visited = set()

        # Follow at most five redirects, always through the cache.
        for _ in range(6):
            if robots_url in visited:
                self.logger.error("robots.txt redirect loop for %s", hostname)
                break
            visited.add(robots_url)

            resp = download(robots_url, self.config, self.logger)
            time.sleep(self.config.time_delay)

            # The cache itself is down: wait without counting it against the host
            if resp.status == 0 and not self.cache_is_reachable(robots_url):
                return "outage"

            # Temporary failure: retry later, but only a few times
            if resp.status == 0 or 500 <= resp.status <= 599:
                self.robots_failures[hostname] += 1
                if self.robots_failures[hostname] >= MAX_RETRIES:
                    self.logger.error("Giving up on robots.txt for %s after %d failures", hostname, MAX_RETRIES)
                    break
                return "retry"

            if resp.status in (301, 302, 303, 307, 308):
                raw = resp.raw_response
                location = raw.headers.get("Location") if raw is not None else None
                target = scraper.prepare_url(location, robots_url) if location else None
                if target is None:
                    self.logger.error("Missing or filtered robots redirect: %s", robots_url)
                    break
                robots_url = target
                continue

            raw = resp.raw_response
            if resp.status == 200 and raw is not None and raw.content:
                content = raw.content.decode("utf-8", errors="ignore")
                scraper.register_robots(hostname, content)

                # Sitemaps listed in robots.txt seed the frontier for this host
                for sitemap in re.findall(
                    r"^\s*Sitemap:\s*(https?://\S+)", content, re.I | re.M
                ):
                    clean = scraper.prepare_url(sitemap)
                    if clean:
                        self.frontier.add_url(clean)

            PROCESSED_HOSTS.add(hostname)
            return "ready"
        else:
            self.logger.error("robots.txt exceeded five redirects for %s", hostname)

        # robots.txt is unusable: crawl the host as if it had no rules
        PROCESSED_HOSTS.add(hostname)
        return "ready"

    def run(self):
        while True:
            tbd_url = self.frontier.get_tbd_url()
            if not tbd_url:
                self.logger.info("Frontier is empty. Stopping Crawler.")
                break

            # Perform domain discovery (robots.txt & sitemap) on new hostnames
            hostname = (urlparse(tbd_url).hostname or "").lower()
            discovery = self.process_domain_discovery(hostname)
            if discovery == "outage":
                self.logger.error("Cache server unreachable; waiting %ds before retrying %s", OUTAGE_BACKOFF, tbd_url)
                self.frontier.requeue_url(tbd_url)
                time.sleep(OUTAGE_BACKOFF)
                continue
            if discovery == "retry":
                self.frontier.requeue_url(tbd_url)
                time.sleep(RETRY_BACKOFF)
                continue

            # Check robots.txt permissions before fetching. No request is
            # made for a disallowed URL, so no politeness sleep is needed.
            if not scraper.is_allowed_by_robots(tbd_url):
                self.logger.info("Skipping robots.txt disallowed URL: %s", tbd_url)
                self.frontier.mark_url_complete(tbd_url)
                continue

            resp = download(tbd_url, self.config, self.logger)
            self.logger.info(
                f"Downloaded {tbd_url}, status <{resp.status}>, "
                f"using cache {self.config.cache_server}.")
            delay = self.delay_for(hostname)

            # Handle Spacetime caching server status codes (600-606)
            if 600 <= resp.status <= 699:
                self.logger.info("Skipping disallowed/cache-error URL: %s (Status %s)", tbd_url, resp.status)
                self.frontier.mark_url_complete(tbd_url)
                time.sleep(delay)
                continue

            # Handle network connection drop (status == 0)
            if resp.status == 0:
                # The failed request may still have reached the host, so wait
                # the politeness delay before probing the cache
                time.sleep(delay)

                # The cache is down: keep the URL and wait, without counting
                # the failure against it
                if not self.cache_is_reachable(tbd_url):
                    self.logger.error("Cache server unreachable; waiting %ds before retrying %s", OUTAGE_BACKOFF, tbd_url)
                    self.frontier.requeue_url(tbd_url)
                    time.sleep(OUTAGE_BACKOFF)
                    continue

                # Only this URL fails: retry a few times, then give up so one
                # bad URL can't loop forever
                self.url_failures[tbd_url] += 1
                if self.url_failures[tbd_url] >= MAX_RETRIES:
                    self.logger.error("Giving up on %s after %d failures: %s", tbd_url, MAX_RETRIES, resp.error)
                    self.frontier.mark_url_complete(tbd_url)
                    time.sleep(delay)
                else:
                    self.logger.error("Network connection failed for %s: %s. Re-queueing...", tbd_url, resp.error)
                    self.frontier.requeue_url(tbd_url)
                    time.sleep(max(RETRY_BACKOFF, delay))
                continue

            scraped_urls = scraper.scraper(tbd_url, resp)
            for scraped_url in scraped_urls:
                self.frontier.add_url(scraped_url)
            self.frontier.mark_url_complete(tbd_url)
            time.sleep(delay)
