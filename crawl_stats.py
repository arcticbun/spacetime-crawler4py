import json
import sqlite3
import hashlib
from collections import Counter
from contextlib import closing
from urllib.parse import urldefrag, urlparse
from hashlib import sha256


def record_page(url, words, db_path="crawl_stats.sqlite3"):
    url, _ = urldefrag(url)
    hostname = (urlparse(url).hostname or "").lower()
    counts = json.dumps(dict(Counter(words)))

    with closing(sqlite3.connect(db_path)) as db:
        with db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS pages (
                    url TEXT PRIMARY KEY,
                    hostname TEXT NOT NULL,
                    word_count INTEGER NOT NULL,
                    word_counts TEXT NOT NULL
                )
            """)

            db.execute("""
                INSERT OR IGNORE INTO pages
                    (url, hostname, word_count, word_counts)
                VALUES (?, ?, ?, ?)
            """, (url, hostname, len(words), counts))

def should_expand_page(url, words, db_path="crawl_stats.sqlite3", *, allow_expansion=None):
    """Use similarity unless the scraper explicitly identifies content or an access wall."""
    url, _ = urldefrag(url)
    exact_fingerprint = sha256(" ".join(words).encode("utf-8")).hexdigest()
    simhash_fp = compute_simhash_64(words)
    simhash_hex = f"{simhash_fp:016x}"

    with closing(sqlite3.connect(db_path)) as db:
        with db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS content_checks (
                    fingerprint TEXT NOT NULL,
                    simhash TEXT NOT NULL,
                    url TEXT NOT NULL,
                    expand INTEGER NOT NULL,
                    PRIMARY KEY (fingerprint, url)
                )
            """)

            expand = allow_expansion
            if expand is None:
                existing = db.execute("""
                    SELECT expand FROM content_checks
                    WHERE fingerprint = ? AND url = ?
                """, (exact_fingerprint, url)).fetchone()
                if existing is not None:
                    return bool(existing[0])

                rows = db.execute("""
                    SELECT simhash FROM content_checks WHERE expand = 1
                """).fetchall()
                expand = not any(
                    hamming_distance(simhash_fp, int(previous, 16)) <= 4
                    for (previous,) in rows
                )

            # Store the actual expansion decision, including overrides of older decisions.
            db.execute("""
                INSERT INTO content_checks (fingerprint, simhash, url, expand)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(fingerprint, url) DO UPDATE SET expand = excluded.expand
            """, (exact_fingerprint, simhash_hex, url, int(expand)))
            return bool(expand)

def compute_simhash_64(words):
    if not words:
        return 0

    v = [0] * 64
    word_counts = Counter(words)

    for word, count in word_counts.items():
        word_hash = int(hashlib.md5(word.encode('utf-8')).hexdigest()[:16], 16)
        
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
    x = f1 ^ f2
    distance = 0
    while x > 0:
        distance += x & 1
        x >>= 1
    return distance
