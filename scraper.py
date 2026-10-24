import re
from urllib.parse import urlparse, urljoin, urldefrag, parse_qsl, unquote, urlsplit, urlunsplit, quote
from bs4 import BeautifulSoup
from collections import Counter
from crawl_stats import record_page, should_expand_page
import xml.etree.ElementTree as ET

def scraper(url, resp):
    links = extract_next_links(url, resp)
    return [link for link in links if is_valid(link)]

def extract_next_links(url, resp):
    raw = resp.raw_response
    if raw is None:
        return []

    # 1. Handle HTTP 3xx Redirects
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

    # Resolve relative links against final URL
    page_url = prepare_url(raw.url or resp.url or url)
    if page_url is None or not is_valid(page_url):
        return []

    # 2. Check 5 MiB size limit BEFORE XML or HTML parsing
    content_length = raw.headers.get("Content-Length")
    if content_length and content_length.isdigit() and int(content_length) > 5 * 1024 * 1024:
        return []
    if len(raw.content) > 5 * 1024 * 1024:
        return []

    content_type = raw.headers.get("Content-Type", "").lower()

    # Handle Sitemap XML files
    is_xml = "xml" in content_type or page_url.split('?')[0].endswith(".xml") or raw.content.strip().startswith(b"<?xml")
    if is_xml and (b"<urlset" in raw.content or b"<sitemapindex" in raw.content):
        sitemap_urls = extract_sitemap_urls(raw.content)
        valid_links = set()
        for loc in sitemap_urls:
            clean = prepare_url(loc, page_url)
            if clean:
                valid_links.add(clean)
        return list(valid_links)

    if content_type and not any(kind in content_type for kind in ("text/html", "application/xhtml+xml")):
        return []

    soup = BeautifulSoup(raw.content, "html.parser")
    words = extract_words(soup)

    # Reject responses containing no extracted text
    if not words:
        return []

    # Record page stats and evaluate near-duplicate expansion
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
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False

        allowed_domains = ("ics.uci.edu", "cs.uci.edu", "informatics.uci.edu", "stat.uci.edu")
        hostname = (parsed.hostname or "").lower()
        if not any(hostname == domain or hostname.endswith("." + domain) for domain in allowed_domains):
            return False

        if len(url) > 2048 or is_url_trap(parsed):
            return False

        path = parsed.path.lower()

        # Strict boundary check for extensionless binary/kernel paths
        if re.search(r"/(zimage|vmlinuz|boot|firmware|binaries)(?:/|[\?#]|$)", path):
            return False

        clean_path = path.rstrip('/')
        return not re.search(
            r"\.(css|js|bmp|gif|jpe?g|ico"
            + r"|png|tiff?|mid|mp2|mp3|mp4"
            + r"|wav|avi|mov|mpeg|ram|m4v|mkv|ogg|ogv|pdf"
            + r"|ps|eps|tex|ppt|pptx|doc|docx|xls|xlsx|names"
            + r"|data|dat|exe|bz2|tar|msi|bin|7z|psd|dmg|iso"
            + r"|epub|dll|cnf|tgz|sha1"
            + r"|thmx|mso|arff|rtf|jar|csv|lif|ipynb|ff|bib"
            + r"|rm|smil|wmv|swf|wma|zip|rar|gz)$", clean_path)

    except (TypeError, ValueError):
        return False

def extract_words(soup):
    for element in soup(["script", "style", "noscript", "template"]):
        element.decompose()
    text = soup.get_text(separator=" ", strip=True).lower()
    return re.findall(r"\b[a-z]+(?:'[a-z]+)?\b", text)

def is_url_trap(parsed):
    path_lower = parsed.path.lower()
    query_lower = parsed.query.lower()
    full_url_lower = path_lower + "?" + query_lower

    # 1. Cyclic repeating path sequences
    if re.search(r"^.*?/(.*?/)\1{2,}", path_lower):
        return True

    # 2. Path segment limits & segment repetition
    segments = [part for part in unquote(parsed.path).split("/") if part]
    if len(segments) > 15:
        return True
    if segments and max(Counter(segments).values()) >= 3:
        return True

    # 3. Source control & repository browser traps
    if re.search(r"/(commit|commits|tree|blob|blame|raw|history|branches|tags)/", path_lower):
        return True

    # 4. Mailing list archive traps
    if re.search(r"/(pipermail|mailman/listinfo|mailman/options|mailman/admin)/", path_lower):
        return True

    # 5. Calendar and date infinite navigation
    if re.search(r"(\?.*?(date|ical|outlook-ical|calendar|day|month|year)=|/\d{4}-\d{2}/|/\d{4}/\d{2}/)", full_url_lower):
        return True

    # 6. Wiki traps (DokuWiki, Trac, & MediaWiki actions/revisions/diffs/versions)
    if re.search(r"(do=(media|diff|revisions|edit|index|recent|backlink|export|admin|login|register)|tab_details=|tab_files=|image=|action=(edit|history|info|diff)|diff=|oldid=|rev=|version=|idx=)", query_lower):
        return True

    # 7. WordPress traps
    if re.search(r"(^|[?&;])(replytocom|attachment_id)=", query_lower) or "format=xml" in query_lower or "format=json" in query_lower:
        return True

    # scraper.py -> inside is_url_trap()

    # 8. Dynamic sorting, filtering, themes, pagination, & limits traps
    if re.search(r"(^|[?&;])(sort|order|orderby|dir|display|mode|view|skin|theme|lang|limit|offset)=", query_lower):
        return True

    # 9. Query string limits
    if len(query_lower) > 150:
        return True

    # 10. Apache directory index sorting
    if re.search(r"(^|[?&;])(c=[ndms]|o=[ad]|c=|o=)", query_lower):
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

    try:
        parameters = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=15)
        keys = [key.lower() for key, value in parameters]
        session_keys = {"sid", "sessionid", "phpsessid", "jsessionid"}
        if any(key in session_keys for key in keys):
            return True
        if keys and max(Counter(keys).values()) >= 3:
            return True
    except ValueError:
        return True

    return False

def prepare_url(url, base_url=None):
    if not isinstance(url, str):
        return None

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

        hostname = parsed.hostname.encode("idna").decode("ascii")
        if not re.fullmatch(r"[a-z0-9.-]+", hostname):
            return None
            
        if port is None or (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443):
            authority = hostname
        else:
            authority = f"{hostname}:{port}"

        prepared = urlunsplit((parsed.scheme, authority, quote(parsed.path, safe="/:@!$&'()*+,;=%"), quote(parsed.query, safe="/?:@!$&'()*+,;=%"), ""))
        return prepared if is_valid(prepared) else None
    
    except (TypeError, ValueError):
        return None

def extract_sitemap_urls(sitemap_xml_content):
    urls = []
    try:
        root = ET.fromstring(sitemap_xml_content)
        for elem in root.iter():
            if elem.tag.endswith('loc') and elem.text:
                urls.append(elem.text.strip())
    except (ET.ParseError, Exception):
        pass
    return urls