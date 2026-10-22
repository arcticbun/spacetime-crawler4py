import requests
import cbor
import logging
import pickle

from utils.response import Response

def download(url, config, logger=None):
    logger = logger or logging.getLogger(__name__)
    host, port = config.cache_server

    try:
        resp = requests.get(f"http://{host}:{port}/", params=[("q", url), ("u", config.user_agent)], timeout=(10, 60), allow_redirects=False)
        if resp.status_code != 200 or not resp.content:
            raise ValueError(f"Empty or unsuccessful cache response: " f"HTTP {resp.status_code}")

        data = cbor.loads(resp.content)
        if (not isinstance(data, dict) or not isinstance(data.get("url"), str) or type(data.get("status")) is not int):
            raise ValueError("Malformed cache response")
        return Response(data)

    except (requests.RequestException, EOFError, ValueError, TypeError, KeyError, pickle.UnpicklingError) as error:
        logger.error("Cache download failed for %s: %s", url, error)

        # Internal status 0 means no usable cache response arrived.
        return Response({"url": url, "status": 0, "error": str(error)})