import re
from urllib.parse import urlparse, urljoin, urldefrag
from bs4 import BeautifulSoup

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

    # Resolve relative links against the final page URL.
    page_url = raw.url or resp.url or url
    if not is_valid(page_url):
        return []
    soup = BeautifulSoup(raw.content, "html.parser")
    words = extract_words(soup)
    links = set()
    for anchor in soup.find_all("a", href=True):
        absolute_url = urljoin(page_url, anchor["href"].strip())
        clean_url, _ = urldefrag(absolute_url)
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
        return not re.match(
            r".*\.(css|js|bmp|gif|jpe?g|ico"
            + r"|png|tiff?|mid|mp2|mp3|mp4"
            + r"|wav|avi|mov|mpeg|ram|m4v|mkv|ogg|ogv|pdf"
            + r"|ps|eps|tex|ppt|pptx|doc|docx|xls|xlsx|names"
            + r"|data|dat|exe|bz2|tar|msi|bin|7z|psd|dmg|iso"
            + r"|epub|dll|cnf|tgz|sha1"
            + r"|thmx|mso|arff|rtf|jar|csv"
            + r"|rm|smil|wmv|swf|wma|zip|rar|gz)$", parsed.path.lower())

    except TypeError:
        print ("TypeError for ", parsed)
        raise

def extract_words(soup):
    # Exclude code and content that isn't normally displayed.
    for element in soup(["script", "style", "noscript", "template"]):
        element.decompose()
    text = soup.get_text(separator=" ", strip=True).lower()

    # Treat alphabetic sequences, including contractions, as words.
    return re.findall(r"\b[a-z]+(?:'[a-z]+)?\b", text)