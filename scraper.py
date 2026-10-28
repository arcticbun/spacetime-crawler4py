import re
import xml.etree.ElementTree as ET
from collections import Counter
from urllib.parse import urlparse, urljoin, parse_qsl, unquote, urlsplit, urlunsplit, quote
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from crawl_stats import record_page, classify_page, consume_family_budget

# Domains (including all of their subdomains) we are allowed to crawl.
ALLOWED_DOMAINS = ("ics.uci.edu", "cs.uci.edu", "informatics.uci.edu", "stat.uci.edu")

# Responses larger than this are skipped: they are almost always data dumps,
# logs or binaries with little textual value.
MAX_CONTENT_BYTES = 5 * 1024 * 1024

# Pages with fewer visible words than this are low information. They still
# count as unique pages, but their links are only followed while their page
# family is within budget (see crawl_stats.consume_family_budget).
LOW_INFO_WORDS = 50

# Parsed robots.txt rules, keyed by lowercase hostname.
ROBOTS_PARSERS = {}
CRAWLER_USER_AGENT = "*"

def set_user_agent(ua):
    """Sets the global user agent for robots.txt checking."""
    global CRAWLER_USER_AGENT
    if ua:
        CRAWLER_USER_AGENT = ua

def register_robots(hostname, content):
    """Parses and caches robots.txt content for a domain."""
    rfp = RobotFileParser()
    rfp.parse(content.splitlines())
    ROBOTS_PARSERS[hostname.lower()] = rfp

def is_allowed_by_robots(url, user_agent=None):
    """Checks if a URL is allowed by the domain's robots.txt rules using configured user agent."""
    if user_agent is None:
        user_agent = CRAWLER_USER_AGENT
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        rfp = ROBOTS_PARSERS.get(hostname)
        if rfp:
            return rfp.can_fetch(user_agent, url)
    except Exception:
        pass
    return True

def get_crawl_delay(hostname):
    """Returns the robots.txt Crawl-delay (seconds) for a host, or None if unset."""
    rfp = ROBOTS_PARSERS.get((hostname or "").lower())
    if rfp is None:
        return None
    try:
        return rfp.crawl_delay(CRAWLER_USER_AGENT)
    except Exception:
        return None

def scraper(url, resp):
    """Entry point called by the worker: returns the valid links found on a page."""
    links = extract_next_links(url, resp)
    return [link for link in links if is_valid(link)]

def extract_next_links(url, resp):
    """Parses a response, records page statistics, and returns outgoing links.

    Handles redirects, sitemaps and HTML pages. Exact duplicates are not
    expanded; near-duplicate and low-information pages are expanded only
    while their page family is within budget.
    """
    raw = resp.raw_response
    if raw is None:
        return []

    # 1. Handle HTTP 3xx Redirects: queue the target so its content is indexed
    if resp.status in (301, 302, 303, 307, 308):
        location = raw.headers.get("Location") if raw.headers else None
        if location:
            redirect_url = prepare_url(location, url)
            if redirect_url:
                return [redirect_url]
        return []

    # Skip non-200 responses and empty content
    if resp.status != 200 or not raw.content:
        return []

    # Resolve relative links against the final URL (after any redirects
    # the cache server followed)
    page_url = prepare_url(raw.url or resp.url or url)
    if page_url is None or not is_valid(page_url):
        return []

    # 2. Check size limit BEFORE XML or HTML parsing
    content_length = raw.headers.get("Content-Length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_CONTENT_BYTES:
        return []
    if len(raw.content) > MAX_CONTENT_BYTES:
        return []

    content_type = raw.headers.get("Content-Type", "").lower()

    # Handle Sitemap XML files: their <loc> entries go straight to the frontier
    is_xml = "xml" in content_type or page_url.split('?')[0].endswith(".xml") or raw.content.strip().startswith(b"<?xml")
    if is_xml and (b"<urlset" in raw.content or b"<sitemapindex" in raw.content):
        sitemap_urls = extract_sitemap_urls(raw.content)
        valid_links = set()
        for loc in sitemap_urls:
            clean = prepare_url(loc, page_url)
            if clean:
                valid_links.add(clean)
        return list(valid_links)

    # Only HTML pages carry text we want to index
    if content_type and not any(kind in content_type for kind in ("text/html", "application/xhtml+xml")):
        return []

    soup = BeautifulSoup(raw.content, "html.parser")

    # Extract links before extract_words strips script/style elements
    links = set()
    for anchor in soup.find_all("a", href=True):
        clean_url = prepare_url(anchor["href"], page_url)
        if clean_url is not None:
            links.add(clean_url)

    words = extract_words(soup)

    # Dead page: 200 status but no visible text
    if not words:
        return []

    # Every page with text counts toward the report (uniqueness is by URL)
    record_page(page_url, words)

    # Access-denied and login pages have no content and no useful links
    if is_access_wall(soup):
        return []

    # Same text as a page we already expanded: its links add nothing new
    status = classify_page(page_url, words)
    if status == "exact":
        return []

    # Near-duplicates and low-information pages may still link to real
    # content (e.g. index pages), so follow them until their family proves
    # to be a repetitive, low-value set of pages
    if status == "near" or len(words) < LOW_INFO_WORDS:
        if not consume_family_budget(page_url):
            return []

    return list(links)

def is_access_wall(soup):
    """Detects "access denied" and login pages.

    A page is an access wall when its first main heading is a denial message,
    when a login/denial heading appears together with a password field, or
    when a password field is the only thing outside the site chrome
    (header, footer, sidebar, forms).
    """
    content = soup.find("main") or soup.find("article") or soup.body or soup
    content = content.find("article") or content

    # Elements that hold site-wide navigation rather than page content
    chrome = {"head", "header", "footer", "aside", "form"}
    if content.name not in {"main", "article"} and content.find(["header", "footer", "aside"]):
        chrome.add("nav")

    # Normalized h1/h2 text from the content area, in document order
    headings = [
        " ".join(heading.get_text(" ", strip=True).lower().split()).rstrip(".!:")
        for heading in content.find_all(["h1", "h2"])
        if not any(parent.name in chrome for parent in heading.parents)
    ]
    denial_headings = {"insufficient access privileges", "access denied",
                       "permission denied", "authentication required"}
    if content.name != "article" and headings and headings[0] in denial_headings:
        return True

    # Password fields outside navigation areas mark a login form
    password_form = any(
        not any(parent.name in {"nav", "header", "footer", "aside"} for parent in field.parents)
        for field in content.find_all("input", type=re.compile(r"^password$", re.I))
    )
    if not password_form:
        return False

    auth_headings = headings[:1] if content.name == "article" else headings
    if set(auth_headings) & (denial_headings | {"login", "log in", "sign in"}):
        return True

    # A password form with no other visible text outside the chrome
    has_content_text = any(
        re.search(r"[a-z]", text, re.I) and not any(parent.name in chrome for parent in text.parents)
        for text in content.strings
    )
    return not has_content_text

def is_valid(url):
    """Decides whether a URL should be crawled.

    Checks the scheme, the allowed domains, trap heuristics, robots.txt
    rules and file extensions that are unlikely to be HTML.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False

        # Host must be one of the allowed domains or a subdomain of one
        hostname = (parsed.hostname or "").lower()
        if not any(hostname == domain or hostname.endswith("." + domain) for domain in ALLOWED_DOMAINS):
            return False

        if len(url) > 2048 or is_url_trap(parsed):
            return False

        if not is_allowed_by_robots(url):
            return False

        path = parsed.path.lower()

        # Directory names need boundaries; kernel filenames can have version/build suffixes.
        if re.search(
            r"/(?:boot|firmware|binaries)(?:/|$)"
            r"|/(?:zimage|vmlinuz)(?:[_.0-9-][^/]*)?(?:/|$)", unquote(path).lower()
        ):
            return False

        # Non-HTML file extensions
        clean_path = path.rstrip('/')
        return not re.search(
            r"\.(css|js|bmp|gif|jpe?g|ico|svg|webp"
            + r"|png|tiff?|mid|mp2|mp3|mp4"
            + r"|wav|avi|mov|mpeg|ram|m4v|mkv|ogg|ogv|pdf"
            + r"|ps|eps|tex|ppt|pptx|pps|ppsx|doc|docx|xls|xlsx|names"
            + r"|odt|odp|ods"
            + r"|data|dat|exe|bz2|tar|msi|bin|7z|psd|dmg|iso|apk|war"
            + r"|epub|dll|cnf|tgz|sha1"
            + r"|thmx|mso|arff|rtf|jar|csv|lif|ipynb|ff|bib"
            + r"|mat|pkl|npy|npz|h5|sql|json"
            + r"|rm|smil|wmv|swf|wma|zip|rar|gz)$", clean_path)

    except (TypeError, ValueError):
        return False

def extract_words(soup):
    """Returns the lowercase words of a page's visible text (markup excluded)."""
    for element in soup(["script", "style", "noscript", "template"]):
        element.decompose()
    text = soup.get_text(separator=" ", strip=True).lower()
    return re.findall(r"\b[a-z]+(?:'[a-z]+)?\b", text)

def is_url_trap(parsed):
    """Detects URL patterns known to produce infinite or low-value page sets."""
    hostname = (parsed.hostname or "").lower()
    path_lower = parsed.path.lower()
    query_lower = parsed.query.lower()
    try:
        parameters = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=15)
    except ValueError:
        # More than 15 query parameters
        return True
    keys = [key.lower() for key, value in parameters]

    # 1. Cyclic repeating path sequences
    if re.search(r"^.*?/(.*?/)\1{2,}", path_lower):
        return True

    # 2. Path segment limits & segment repetition
    segments = [part for part in unquote(parsed.path).split("/") if part]
    if len(segments) > 15:
        return True
    if segments and max(Counter(segments).values()) >= 3:
        return True

    # 3. Source control repository browsers (only on git hosts, so ordinary
    #    pages like /about/history/ are not caught)
    if hostname.startswith(("gitlab.", "github.", "git.")) or "/-/" in path_lower:
        if re.search(r"/(commit|commits|tree|blob|blame|raw|history|branches|tags|compare|network|graphs)(/|$)", path_lower):
            return True

    # 4. Mailing list archive traps
    if re.search(r"/(pipermail|mailman/listinfo|mailman/options|mailman/admin)/", path_lower):
        return True

    # 5a. Calendar export downloads (.ics files behind a query parameter)
    if {"ical", "outlook-ical"}.intersection(keys):
        return True

    # 5b. Calendar listing navigation, including full dates and missing trailing
    #     slashes. Individual event slugs and ordinary dated articles stay crawlable.
    calendar_path = unquote(path_lower).lower()
    calendar_route = re.search(r"/(?:events?|calendar)(?:\.php)?(?:/|$)", calendar_path)
    calendar_root = calendar_path.rstrip("/") in {"", "/index.php"} and {"month", "year"}.issubset(keys)
    # eventdisplay=past&paged=N pages back through every past event, forever
    calendar_keys = {"calendar", "eventdate", "eventdisplay", "tribe-bar-date", "tribe_event_display"}
    date_keys = calendar_keys | {"date", "day", "week", "month", "year"}
    if (calendar_route or calendar_root or calendar_keys.intersection(keys)) and (
        date_keys.intersection(keys) or re.search(
            r"/\d{4}(?:-\d{1,2}(?:-\d{1,2})?|/\d{1,2}(?:/\d{1,2})?)?"
            r"(?:/page/\d+)?/?$", calendar_path
        )
    ):
        return True

    # 6. Wiki traps (DokuWiki, Trac, & MediaWiki actions/revisions/diffs/versions)
    if re.search(r"(^|[?&;])(do=(media|diff|revisions|edit|index|recent|backlink|export|admin|login|register)|tab_details=|tab_files=|image=|action=(edit|history|info|diff)|diff=|oldid=|rev=|version=|idx=)", query_lower):
        return True

    # 7. WordPress traps
    if re.search(r"(^|[?&;])(replytocom|attachment_id)=", query_lower) or "format=xml" in query_lower or "format=json" in query_lower:
        return True

    # 8. Dynamic sorting, filtering, themes, pagination, limits, & subpage traps
    if re.search(r"(^|[?&;])(sort|order|orderby|dir|display|mode|view|skin|theme|lang|limit|offset|subpage|sub_page)=", query_lower):
        return True

    # 9. Query string limits
    if len(query_lower) > 150:
        return True

    # 10. Apache directory index sorting (?C=N;O=D and friends)
    if re.search(r"(^|[?&;])[co]=", query_lower):
        return True

    # 11. Authentication & login pages (strict boundary check)
    if re.search(r"/(wp-login\.php|cas/login|(?:login|logout|signin|signup|auth)(?:/|[\?#]|$))", path_lower):
        return True

    # 12. Redirection parameters
    if re.search(r"(^|[?&;])(redirect_to|redirect|return_to|return|next|destination)=", query_lower):
        return True

    # 13. Search/keyword query parameters
    if re.search(r"(^|[?&;])(keywords|search|query)=", query_lower):
        return True

    # 14. Static photo gallery wrappers
    if re.search(r"/(pix|gallery|photos|albums)/", path_lower):
        return True

    # 15. PowerPoint HTML presentation slides
    if re.search(r"/(sld\d+|v\d+_slide\d+|slide\d+|frame)\.htm[l]?$", path_lower):
        return True

    # 16. Session IDs and repeated query keys
    session_keys = {"sid", "sessionid", "phpsessid", "jsessionid"}
    if any(key in session_keys for key in keys):
        return True
    if keys and max(Counter(keys).values()) >= 3:
        return True

    return False

def prepare_url(url, base_url=None):
    """Turns a raw href into a clean absolute ASCII URL, or None if unusable.

    Resolves relative links against base_url, drops the fragment, rejects
    credentials and non-standard ports, IDNA-encodes the host, percent-encodes
    the path and query, and finally applies is_valid.
    """
    if not isinstance(url, str):
        return None

    # Control characters and characters that cannot appear in a real URL
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

        # Send only ASCII hostnames to the server
        hostname = parsed.hostname.encode("idna").decode("ascii")
        if not re.fullmatch(r"[a-z0-9.-]+", hostname):
            return None

        # Drop the port when it is the scheme's default
        if port is None or (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443):
            authority = hostname
        else:
            authority = f"{hostname}:{port}"

        # An empty path is the same page as "/"; the fragment is discarded
        path = quote(parsed.path or "/", safe="/:@!$&'()*+,;=%")
        query = quote(parsed.query, safe="/?:@!$&'()*+,;=%")
        prepared = urlunsplit((parsed.scheme, authority, path, query, ""))
        return prepared if is_valid(prepared) else None

    except (TypeError, ValueError):
        return None

def extract_sitemap_urls(sitemap_xml_content):
    """Returns every <loc> entry in a sitemap or sitemap index."""
    urls = []
    try:
        root = ET.fromstring(sitemap_xml_content)
        for elem in root.iter():
            if elem.tag.endswith('loc') and elem.text:
                urls.append(elem.text.strip())
    except (ET.ParseError, Exception):
        pass
    return urls
