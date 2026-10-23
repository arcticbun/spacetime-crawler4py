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

    # Skip unsuccessful responses and empty bodies.
    if resp.status != 200 or raw is None or not raw.content:
        return []

    # Resolve relative links against the final page URL.
    page_url = prepare_url(raw.url or resp.url or url)
    if page_url is None or not is_valid(page_url):
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

    # Initial limit: 5 MiB of HTML.
    if len(raw.content) > 5 * 1024 * 1024:
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
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False

        # Accept only assigned domains and subdomains
        allowed_domains = ("ics.uci.edu", "cs.uci.edu", "informatics.uci.edu", "stat.uci.edu")
        hostname = (parsed.hostname or "").lower()
        if not any(hostname == domain or hostname.endswith("." + domain) for domain in allowed_domains):
            return False

        # Overall URL length limit and trap check
        if len(url) > 2048 or is_url_trap(parsed):
            return False

        path = parsed.path.lower()

        # Block extensionless binary files and kernel images in path
        if re.search(r"/(zimage|vmlinuz|boot|firmware|binaries)", path):
            return False

        # Check path without trailing slash for non-HTML file extensions
        clean_path = path.rstrip('/')
        return not re.search(
            # Add 'ff' and 'bib' to the blacklisted file extensions
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
    # Exclude code and content that isn't normally displayed.
    for element in soup(["script", "style", "noscript", "template"]):
        element.decompose()
    text = soup.get_text(separator=" ", strip=True).lower()

    # Treat alphabetic sequences, including contractions, as words.
    return re.findall(r"\b[a-z]+(?:'[a-z]+)?\b", text)

def is_url_trap(parsed):
    path_lower = parsed.path.lower()
    query_lower = parsed.query.lower()
    full_url_lower = path_lower + "?" + query_lower

    # 1. Cyclic repeating path sequences (e.g., /dirA/dirB/dirA/dirB/)
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

    # 6. Wiki traps (DokuWiki & MediaWiki actions/revisions/diffs)
    if re.search(r"(do=(media|diff|revisions|edit|index|recent|backlink|export|admin|login|register)|tab_details=|tab_files=|image=|action=(edit|history|info)|diff=|oldid=|rev=|idx=)", query_lower):
        return True

    # 7. WordPress comment, attachment, & format traps
    if re.search(r"(^|[?&;])(replytocom|attachment_id)=", query_lower) or "format=xml" in query_lower or "format=json" in query_lower:
        return True

    # 8. Dynamic sorting, filtering, themes, & pagination traps
    if re.search(r"(^|[?&;])(sort|order|orderby|dir|display|mode|view|skin|theme|lang)=", query_lower):
        return True

    # 9. Query string limits
    if len(query_lower) > 150:
        return True

    # 10. Apache directory index sorting permutations (?C=N;O=D, ?C=M;O=A)
    if re.search(r"(^|[?&;])(c=[ndms]|o=[ad]|c=|o=)", query_lower):
        return True

    # 11. Authentication, login, and registration pages
    if re.search(r"/(wp-login\.php|login|logout|signin|signup|auth|cas/login)", path_lower):
        return True

    # 12. Redirection parameters (e.g., redirect_to=...)
    if re.search(r"(^|[?&;])(redirect_to|redirect|return_to|return|next|destination)=", query_lower):
        return True

    # 13. Block archive/dataset search and keyword query parameters
    if re.search(r"(^|[?&;])(keywords|search|query)=", query_lower):
        return True

    # 14. Block static photo gallery wrappers (e.g., ~eppstein/pix/)
    if re.search(r"/(pix|gallery|photos|albums)/", path_lower):
        return True

    # 15. Block PowerPoint HTML presentation slide wrappers and frames (sld01.htm, v3_slide001.htm, frame.htm)
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
            
        if port is None or (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443):
            authority = hostname
        else:
            authority = f"{hostname}:{port}"

        # Encode Unicode and spaces, preserving existing percent escapes and removing fragments.
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