import os
import logging
from hashlib import sha256
from urllib.parse import urldefrag, urlsplit, urlunsplit

def get_logger(name, filename=None):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not os.path.exists("Logs"):
        os.makedirs("Logs")
    fh = logging.FileHandler(f"Logs/{filename if filename else name}.log")
    fh.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    formatter = logging.Formatter(
       "%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)
    # add the handlers to the logger
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def url_key(url):
    """Returns the identity of a URL used for uniqueness checks.

    Per the assignment, a unique page is its URL without the fragment. The
    scheme and host are lowercased and an empty path becomes "/", since
    those spell the same URL. For URLs cleaned by scraper.prepare_url this
    is the URL itself.
    """
    clean_url, _ = urldefrag(url)
    parsed = urlsplit(clean_url)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


def get_urlhash(url):
    """Hashes a URL's identity so the frontier stores each page only once."""
    return sha256(url_key(url).encode("utf-8")).hexdigest()

def normalize(url):
    if url.endswith("/"):
        return url.rstrip("/")
    return url
