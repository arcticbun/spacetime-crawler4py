import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path


def build_report(db_path="crawl_stats.sqlite3", stopwords_path="stopwords.txt"):
    stopwords = set(Path(stopwords_path).read_text(encoding="utf-8").lower().split())
    frequencies = Counter()
    subdomains = Counter()
    total = 0
    longest = None

    # Read-only mode prevents accidentally creating an empty database.
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        rows = db.execute("""
            SELECT url, hostname, word_count, word_counts
            FROM pages
            ORDER BY url
        """)
        for url, hostname, word_count, word_counts in rows:
            total += 1

            # Page length includes stop words.
            if longest is None or word_count > longest[1]:
                longest = (url, word_count)

            # Exclude stop words and single letters (e.g. "s" from "it's" split
            # by markup) only from the frequency report.
            frequencies.update({word: count for word, count in json.loads(word_counts).items() if word not in stopwords and len(word) > 1})
            if hostname.endswith(".ics.uci.edu"):
                subdomains[hostname] += 1

    lines = [f"Unique pages: {total}"]
    if longest:
        lines.append(f"Longest page: {longest[0]} ({longest[1]} words)")
    else:
        lines.append("Longest page: none")
    lines.extend(["", "Top 50 words (excluding stop words):"])

    # Break frequency ties alphabetically for reproducible output.
    top_words = sorted(frequencies.items(), key=lambda item: (-item[1], item[0]))[:50]
    lines.extend(f"{word}, {count}" for word, count in top_words)
    lines.extend(["", f"ICS subdomains: {len(subdomains)}"])
    lines.extend(f"http://{host}, {subdomains[host]}" for host in sorted(subdomains))
    return "\n".join(lines)


if __name__ == "__main__":
    print(build_report())