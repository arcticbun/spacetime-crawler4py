import os
import shelve

from utils import get_logger, get_urlhash
from scraper import prepare_url

class Frontier(object):
    """Single-threaded frontier backed by a shelve file so a crawl can resume.

    Each URL is stored under its hash as (url, completed). URLs are popped
    from the end of to_be_downloaded (LIFO).
    """
    def __init__(self, config, restart):
        self.logger = get_logger("FRONTIER")
        self.config = config
        self.to_be_downloaded = list()
        
        if not os.path.exists(self.config.save_file) and not restart:
            # Save file does not exist, but request to load save.
            self.logger.info(
                f"Did not find save file {self.config.save_file}, "
                f"starting from seed.")
        elif os.path.exists(self.config.save_file) and restart:
            # Save file does exists, but request to start from seed.
            self.logger.info(
                f"Found save file {self.config.save_file}, deleting it.")
            os.remove(self.config.save_file)
        # Load existing save file, or create one if it does not exist.
        self.save = shelve.open(self.config.save_file)
        if restart:
            for url in self.config.seed_urls:
                self.add_url(url)
        else:
            # Set the frontier state with contents of save file.
            self._parse_save_file()
            if not self.save:
                for url in self.config.seed_urls:
                    self.add_url(url)

    def _parse_save_file(self):
        ''' Reloads uncompleted URLs, dropping any that the current filters now reject. '''
        total_count = len(self.save)
        tbd_count = 0
        for url, completed in self.save.values():
            if not completed and url == prepare_url(url):
                self.to_be_downloaded.append(url)
                tbd_count += 1
        self.logger.info(f"Found {tbd_count} urls to be downloaded from {total_count} " f"total urls discovered.")

    def get_tbd_url(self):
        """Returns the next URL to download, or None when the frontier is empty."""
        try:
            return self.to_be_downloaded.pop()
        except IndexError:
            return None

    def add_url(self, url):
        """Cleans a URL and queues it if it is valid and has never been seen."""
        url = prepare_url(url)
        if url is None:
            return
        urlhash = get_urlhash(url)
        if urlhash not in self.save:
            self.save[urlhash] = (url, False)
            self.save.sync()
            self.to_be_downloaded.append(url)
    
    def mark_url_complete(self, url):
        """Records that a URL has been processed so it is never fetched again."""
        urlhash = get_urlhash(url)
        if urlhash not in self.save:
            # This should not happen.
            self.logger.error(
                f"Completed url {url}, but have not seen it before.")

        self.save[urlhash] = (url, True)
        self.save.sync()

    def requeue_url(self, url):
        """Re-enqueues an uncompleted URL that failed due to a network drop."""
        url = prepare_url(url)
        if url is None:
            return
        urlhash = get_urlhash(url)
        # Re-queue only if it exists in save file and has not been completed
        if urlhash in self.save:
            saved_url, completed = self.save[urlhash]
            if not completed:
                self.to_be_downloaded.append(saved_url)
        else:
            self.add_url(url)
