import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from hashlib import md5, sha256
from urllib.parse import urlparse, parse_qsl

from utils import url_key

DB_PATH = "crawl_stats.sqlite3"

# Two pages whose 64-bit SimHashes differ in at most this many bits are
# considered near-duplicates.
NEAR_DUPLICATE_DISTANCE = 4

# How many near-duplicate or low-information pages one page family (see
# page_family) may contribute before we stop following links out of them.
FAMILY_BUDGET = 25

# Database paths whose tables have already been created during this run.
_initialized = set()

# In-memory copy of the SimHashes of every unique page, keyed by database
# path, so near-duplicate checks don't re-read the whole table per page.
_simhashes = {}


def _connect(db_path):
    """Opens the stats database, creating its tables the first time."""
    db = sqlite3.connect(db_path)
    if db_path not in _initialized:
        with db:
            # One row per unique URL; feeds every answer in the report.
            db.execute("""
                CREATE TABLE IF NOT EXISTS pages (
                    url TEXT PRIMARY KEY,
                    hostname TEXT NOT NULL,
                    word_count INTEGER NOT NULL,
                    word_counts TEXT NOT NULL
                )
            """)
            # Content fingerprints and the duplicate verdict for each URL.
            db.execute("""
                CREATE TABLE IF NOT EXISTS page_checks (
                    url TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    simhash TEXT NOT NULL,
                    status TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS page_checks_fingerprint
                ON page_checks (fingerprint)
            """)
            # Low-value pages charged to each page family, with the decision
            # made for each, so reprocessing a page after a restart repeats it.
            db.execute("""
                CREATE TABLE IF NOT EXISTS family_pages (
                    url TEXT PRIMARY KEY,
                    family TEXT NOT NULL,
                    allowed INTEGER NOT NULL
                )
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS family_pages_family
                ON family_pages (family)
            """)
        _initialized.add(db_path)
    return db


def record_page(url, words, db_path=DB_PATH):
    """Stores a crawled page's word counts, once per unique URL."""
    hostname = (urlparse(url).hostname or "").lower()
    counts = json.dumps(dict(Counter(words)))

    with closing(_connect(db_path)) as db:
        with db:
            db.execute("""
                INSERT OR IGNORE INTO pages
                    (url, hostname, word_count, word_counts)
                VALUES (?, ?, ?, ?)
            """, (url_key(url), hostname, len(words), counts))


def classify_page(url, words, links=(), db_path=DB_PATH):
    """Classifies a page's content as "unique", "exact" or "near".

    "exact" means another URL already had exactly the same text and the same
    outgoing links (SHA-256 of both), so expanding it would add nothing.
    "near" means another unique page's SimHash is within
    NEAR_DUPLICATE_DISTANCE bits. The verdict is stored, so asking again for
    the same URL returns the same answer.
    """
    key = url_key(url)
    exact_content = " ".join(words) + "\n" + "\n".join(sorted(links))
    exact_fingerprint = sha256(exact_content.encode("utf-8")).hexdigest()
    simhash_fp = compute_simhash_64(words)

    with closing(_connect(db_path)) as db:
        with db:
            # Same URL reached again (e.g. through a redirect): reuse verdict.
            existing = db.execute(
                "SELECT status FROM page_checks WHERE url = ?", (key,)
            ).fetchone()
            if existing is not None:
                return existing[0]

            simhashes = _load_simhashes(db, db_path)

            # 1. Exact duplicate: identical text and links already seen at another URL.
            if db.execute(
                "SELECT 1 FROM page_checks WHERE fingerprint = ? LIMIT 1",
                (exact_fingerprint,),
            ).fetchone():
                status = "exact"
            # 2. Near duplicate: SimHash close to some unique page's SimHash.
            elif any(hamming_distance(simhash_fp, prev) <= NEAR_DUPLICATE_DISTANCE
                     for prev in simhashes):
                status = "near"
            else:
                status = "unique"
                simhashes.append(simhash_fp)

            db.execute("""
                INSERT INTO page_checks (url, fingerprint, simhash, status)
                VALUES (?, ?, ?, ?)
            """, (key, exact_fingerprint, f"{simhash_fp:016x}", status))
            return status


def _load_simhashes(db, db_path):
    """Loads unique pages' SimHashes from disk once, then serves them from memory."""
    if db_path not in _simhashes:
        rows = db.execute(
            "SELECT simhash FROM page_checks WHERE status = 'unique'"
        ).fetchall()
        _simhashes[db_path] = [int(simhash_hex, 16) for (simhash_hex,) in rows]
    return _simhashes[db_path]


def page_family(url):
    """Groups URLs that are likely generated by the same template.

    The family is the host, the parent directory with digits collapsed to
    "#", and the sorted names of the query parameters. For example
    /events/2023/page?id=7 and /events/2024/page?id=9 share a family, and so
    do /catalog/alpha/ and /catalog/beta (a trailing slash is ignored).
    """
    parsed = urlparse(url)
    parent = re.sub(r"\d+", "#", parsed.path.rstrip("/").rsplit("/", 1)[0] + "/")
    keys = sorted({key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)})
    return f"{(parsed.hostname or '').lower()}{parent}?{'&'.join(keys)}"


def consume_family_budget(url, db_path=DB_PATH):
    """Counts one low-value page against its family's budget.

    Returns True while the family is still within FAMILY_BUDGET, i.e. its
    links are still worth following. A page is charged only once: asking
    again for the same URL returns the original decision.
    """
    key = url_key(url)
    family = page_family(url)
    with closing(_connect(db_path)) as db:
        with db:
            existing = db.execute(
                "SELECT allowed FROM family_pages WHERE url = ?", (key,)
            ).fetchone()
            if existing is not None:
                return bool(existing[0])

            (count,) = db.execute(
                "SELECT COUNT(*) FROM family_pages WHERE family = ?", (family,)
            ).fetchone()
            allowed = count < FAMILY_BUDGET
            db.execute(
                "INSERT INTO family_pages (url, family, allowed) VALUES (?, ?, ?)",
                (key, family, int(allowed)),
            )
    return allowed


def compute_simhash_64(words):
    """Computes a 64-bit SimHash of a page, weighting each word by its frequency.

    Each word is hashed to 64 bits (the first 16 hex digits of its MD5). For
    every bit position the word's count is added when the bit is 1 and
    subtracted when it is 0; the fingerprint keeps a 1 wherever the total
    ends up positive.
    """
    if not words:
        return 0

    v = [0] * 64
    word_counts = Counter(words)

    for word, count in word_counts.items():
        word_hash = int(md5(word.encode('utf-8')).hexdigest()[:16], 16)

        for i in range(64):
            bit = (word_hash >> i) & 1
            if bit == 1:
                v[i] += count
            else:
                v[i] -= count

    fingerprint = 0
    for i in range(64):
        if v[i] > 0:
            fingerprint |= (1 << i)

    return fingerprint


def hamming_distance(f1, f2):
    """Number of bit positions in which two fingerprints differ."""
    return bin(f1 ^ f2).count("1")
