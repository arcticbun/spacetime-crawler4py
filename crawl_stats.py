import json
import sqlite3
from collections import Counter
from contextlib import closing
from urllib.parse import urldefrag, urlparse
from hashlib import sha256


def record_page(url, words, db_path="crawl_stats.sqlite3"):
    # URL uniqueness ignores fragments but preserves everything else.
    url, _ = urldefrag(url)
    hostname = (urlparse(url).hostname or "").lower()
    counts = json.dumps(dict(Counter(words)))

    with closing(sqlite3.connect(db_path)) as db:
        # Commit the record, or roll back if something fails.
        with db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS pages (
                    url TEXT PRIMARY KEY,
                    hostname TEXT NOT NULL,
                    word_count INTEGER NOT NULL,
                    word_counts TEXT NOT NULL
                )
            """)

            # Keep the first record if this URL was already processed.
            db.execute("""
                INSERT OR IGNORE INTO pages
                    (url, hostname, word_count, word_counts)
                VALUES (?, ?, ?, ?)
            """, (url, hostname, len(words), counts))

def should_expand_page(url, words, db_path="crawl_stats.sqlite3"):
    url, _ = urldefrag(url)
    fingerprint = sha256(" ".join(words).encode("utf-8")).hexdigest()
    with closing(sqlite3.connect(db_path)) as db:
        with db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS content_checks (
                    fingerprint TEXT NOT NULL,
                    url TEXT NOT NULL,
                    expand INTEGER NOT NULL,
                    PRIMARY KEY (fingerprint, url)
                )
            """)

            # Reprocessing the same URL preserves its earlier decision.
            existing = db.execute("""
                SELECT expand FROM content_checks
                WHERE fingerprint = ? AND url = ?
            """, (fingerprint, url)).fetchone()
            if existing is not None:
                return bool(existing[0])
            copies = db.execute("""
                SELECT COUNT(*) FROM content_checks
                WHERE fingerprint = ? AND expand = 1
            """, (fingerprint,)).fetchone()[0]

            # Allow three distinct URLs with identical word sequences.
            expand = copies < 3
            db.execute("""
                INSERT INTO content_checks
                    (fingerprint, url, expand)
                VALUES (?, ?, ?)
            """, (fingerprint, url, int(expand)))
            return expand