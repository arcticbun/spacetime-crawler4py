import json
import sqlite3
from collections import Counter
from contextlib import closing
from urllib.parse import urldefrag, urlparse


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