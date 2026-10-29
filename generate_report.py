import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path


def is_garbled(word_counts, word_count):
    """True when most of a page's "words" are single letters.

    That happens when a page was decoded with the wrong encoding (UTF-16
    read as UTF-8): every character is separated by a NUL byte and becomes
    its own token. Normal English text is only a few percent single letters.
    """
    singles = sum(count for word, count in word_counts.items() if len(word) == 1)
    return word_count >= 100 and singles > word_count / 2


def build_report(db_path="crawl_stats.sqlite3", stopwords_path="stopwords.txt"):
    stopwords = set(Path(stopwords_path).read_text(encoding="utf-8").lower().split())
    frequencies = Counter()
    subdomains = Counter()
    total = 0
    longest = None
    garbled = []

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
            word_counts = json.loads(word_counts)
            if hostname.endswith(".ics.uci.edu"):
                subdomains[hostname] += 1

            # Garbled pages still count as unique URLs, but their letters are
            # not words, so they are left out of the word-based answers.
            if is_garbled(word_counts, word_count):
                garbled.append((url, word_count))
                continue

            # Page length includes stop words.
            if longest is None or word_count > longest[1]:
                longest = (url, word_count)

            # Exclude stop words only from the frequency report.
            frequencies.update({word: count for word, count in word_counts.items() if word not in stopwords})

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
    if garbled:
        lines.extend(["", "Excluded from longest page and word counts (wrong text encoding, every letter counted as a word):"])
        lines.extend(f"{url} ({count} tokens)" for url, count in garbled)
    return "\n".join(lines)


if __name__ == "__main__":
    print(build_report())