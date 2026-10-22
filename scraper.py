import re
from urllib.parse import urlparse, urljoin, urldefrag, parse_qsl, unquote, urlsplit, urlunsplit, quote
from bs4 import BeautifulSoup
from collections import Counter
from crawl_stats import record_page, should_expand_page

def scraper(url, resp):
    links = extract_next_links(url, resp)
    return [link for link in links if is_valid(link)]

def extract_next_links(url, resp):
    raw = resp.raw_response

    # Skip unsuccessful responses and empty bodies.
    if resp.status != 200 or raw is None or not raw.content:
        return []
    content_type = raw.headers.get("Content-Type", "").lower()
    if content_type and not any(kind in content_type for kind in ("text/html", "application/xhtml+xml")):
        return []

    # Initial limit: 5 MiB of HTML.
    if len(raw.content) > 5 * 1024 * 1024:
        return []

    # Resolve relative links against the final page URL.
    page_url = prepare_url(raw.url or resp.url or url)
    if page_url is None:
        return []
    if not is_valid(page_url):
        return []
    soup = BeautifulSoup(raw.content, "html.parser")
    words = extract_words(soup)

    # Reject responses containing no extracted text.
    if not words:
        return []

    # Count distinct URLs before deciding whether to follow their links.
    record_page(page_url, words)
    if not should_expand_page(page_url, words):
        return []
    
    links = set()
    for anchor in soup.find_all("a", href=True):
        clean_url = prepare_url(anchor["href"], page_url)
        if clean_url is not None:
            links.add(clean_url)
    return list(links)

def is_valid(url):
    # Decide whether to crawl this url or not. 
    # If you decide to crawl it, return True; otherwise return False.
    # There are already some conditions that return False.
    try:
        parsed = urlparse(url)
        if parsed.scheme not in set(["http", "https"]):
            return False
        # Accept only the assigned domains and their subdomains.
        allowed_domains = ("ics.uci.edu", "cs.uci.edu", "informatics.uci.edu", "stat.uci.edu")
        hostname = (parsed.hostname or "").lower()
        if not any(hostname == domain or hostname.endswith("." + domain) for domain in allowed_domains):
            return False
        if len(url) > 2048 or is_url_trap(parsed):
            return False
        
        # Target specific infinite date navigation parameters and date-path patterns
        if re.search(r"(\?.*?(date|ical|outlook-ical|filter|share)=|/\d{4}-\d{2}/|/\d{4}/\d{2}/)", parsed.path.lower() + "?" + parsed.query.lower()):
            return False
        if len(parsed.query) > 200:
            return False
        
        return not re.match(
            r".*\.(css|js|bmp|gif|jpe?g|ico"
            + r"|png|tiff?|mid|mp2|mp3|mp4"
            + r"|wav|avi|mov|mpeg|ram|m4v|mkv|ogg|ogv|pdf"
            + r"|ps|eps|tex|ppt|pptx|doc|docx|xls|xlsx|names"
            + r"|data|dat|exe|bz2|tar|msi|bin|7z|psd|dmg|iso"
            + r"|epub|dll|cnf|tgz|sha1"
            + r"|thmx|mso|arff|rtf|jar|csv"
            + r"|rm|smil|wmv|swf|wma|zip|rar|gz)$", parsed.path.lower())

    except (TypeError, ValueError):
        # Skip malformed URLs rather than stopping the worker.
        return False

def extract_words(soup):
    # Exclude code and content that isn't normally displayed.
    for element in soup(["script", "style", "noscript", "template"]):
        element.decompose()
    text = soup.get_text(separator=" ", strip=True).lower()

    # Treat alphabetic sequences, including contractions, as words.
    return re.findall(r"\b[a-z]+(?:'[a-z]+)?\b", text)

def is_url_trap(parsed):
    # Decode paths so encoded slashes cannot hide repetition.
    segments = [part for part in unquote(parsed.path).split("/") if part]

    # Initial limits; review rejected URLs during test crawls.
    if len(segments) > 20:
        return True
    if segments and max(Counter(segments).values()) >= 4:
        return True

    # Raises ValueError if there are too many query fields.
    parameters = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=20)
    keys = [key.lower() for key, value in parameters]
    session_keys = {"sid", "sessionid", "phpsessid", "jsessionid"}
    if any(key in session_keys for key in keys):
        return True
    if keys and max(Counter(keys).values()) >= 4:
        return True
    return False

def prepare_url(url, base_url=None):
    if not isinstance(url, str):
        return None

    # Reject control characters, markup, and backslashes.
    if any(ord(char) < 32 or ord(char) == 127 or char in "<>\\" for char in url):
        return None

    try:
        absolute = (urljoin(base_url, url.strip()) if base_url else url.strip())
        parsed = urlsplit(absolute)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        port = parsed.port
        if port not in (None, 80, 443):
            return None

        # Convert international hostnames to their ASCII representation.
        hostname = parsed.hostname.encode("idna").decode("ascii")
        if not re.fullmatch(r"[a-z0-9.-]+", hostname):
            return None
        authority = (hostname if port is None else f"{hostname}:{port}")

        # Encode Unicode and spaces, preserving existing percent escapes.
        prepared = urlunsplit((parsed.scheme, authority, quote(parsed.path, safe="/:@!$&'()*+,;=%"), quote(parsed.query, safe="/?:@!$&'()*+,;=%"), ""))
        return prepared if is_valid(prepared) else None
    
    except (TypeError, ValueError):
        return None