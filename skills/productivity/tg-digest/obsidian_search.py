#!/usr/bin/env python3
"""
Lexical search over selected Obsidian note folders.

This complements the SQLite semantic layer: notes that are not yet synced into
digest.db can still be retrieved directly from the vault.
"""

import argparse
import json
import re
from pathlib import Path


DEFAULT_NOTE_DIRS = (
    "Concepts",
    "Resources",
    "Knowledge",
    "Library/Sber Mini MBA",
)

SKIP_DIRS = {
    ".obsidian",
    "Templates",
    "Daily Notes",
    "Contacts",
    "MBA",
}

SKIP_FILES = {
    "Home.md",
    "Входящие ресурсы.md",
    "Каталог ресурсов.md",
    "Каталог концептов.md",
    "Материалы Sber Mini MBA.md",
}


def tokenize(text):
    """Extract normalized search tokens."""
    return re.findall(r"[a-zA-Zа-яА-Я0-9_-]{2,}", (text or "").lower())


def parse_frontmatter(text):
    """Parse simple YAML frontmatter into a flat dict."""
    match = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
    if not match:
        return {}, text

    metadata = {}
    lines = match.group(1).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if not line.startswith(" ") and ":" in line:
            key, value = line.split(":", 1)
            value = value.strip()
            if value == "":
                items = []
                i += 1
                while i < len(lines):
                    nested = lines[i].strip()
                    if not nested:
                        i += 1
                        continue
                    if lines[i].startswith("  - "):
                        items.append(nested[2:].strip().strip('"'))
                        i += 1
                        continue
                    break
                metadata[key.strip()] = items
                continue
            metadata[key.strip()] = value.strip('"')
        i += 1
    return metadata, match.group(2)


def extract_title(body, fallback):
    """Extract first markdown h1 title."""
    match = re.search(r"^# (.+)$", body, re.MULTILINE)
    return match.group(1).strip() if match else fallback


def strip_markdown(text):
    """Convert markdown-ish text to plain text."""
    text = re.sub(r"\[\[([^\]|]+)(?:\|.*?)?\]\]", r"\1", text)
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[\^[^\]]+\]", "", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"^\s*-\s+", "", text, flags=re.MULTILINE)
    return re.sub(r"\s+", " ", text).strip()


def iter_note_files(vault_path, note_dirs):
    """Yield markdown notes from selected folders."""
    vault = Path(vault_path)
    for note_dir in note_dirs:
        base = vault / note_dir
        if not base.exists():
            continue
        for path in base.rglob("*.md"):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.name in SKIP_FILES:
                continue
            yield path


def score_note(query_tokens, title, metadata, body):
    """Lightweight lexical relevance score."""
    title_text = title.lower()
    body_text = body.lower()
    topic_text = " ".join(metadata.get("topics", [])) if isinstance(metadata.get("topics"), list) else str(metadata.get("topics", ""))
    alias_text = " ".join(metadata.get("aliases", [])) if isinstance(metadata.get("aliases"), list) else str(metadata.get("aliases", ""))
    score = 0.0
    hits = 0
    for token in query_tokens:
        token_hits = 0
        if token in title_text:
            score += 4.0
            token_hits += 1
        if token in topic_text.lower():
            score += 2.5
            token_hits += 1
        if token in alias_text.lower():
            score += 2.0
            token_hits += 1
        body_count = body_text.count(token)
        if body_count:
            score += min(body_count, 5) * 0.7
            token_hits += 1
        if token_hits:
            hits += 1
    if hits:
        score += hits * 1.5
    return score


def make_snippet(body, query_tokens, limit=240):
    """Create a compact snippet around the first matching token."""
    text = strip_markdown(body)
    if not text:
        return ""
    lower = text.lower()
    positions = [lower.find(token) for token in query_tokens if token in lower]
    positions = [pos for pos in positions if pos >= 0]
    start = max(min(positions) - 60, 0) if positions else 0
    snippet = text[start:start + limit].strip()
    return snippet + ("..." if start + limit < len(text) else "")


def search_notes(vault_path, query, top_k=5, note_dirs=None):
    """Search Obsidian notes and return ranked results."""
    query_tokens = tokenize(query)
    results = []
    for path in iter_note_files(vault_path, note_dirs or DEFAULT_NOTE_DIRS):
        text = path.read_text(encoding="utf-8")
        metadata, body = parse_frontmatter(text)
        title = extract_title(body, path.stem)
        score = score_note(query_tokens, title, metadata, body)
        if score <= 0:
            continue
        results.append({
            "title": title,
            "path": str(path),
            "score": round(score, 4),
            "type": metadata.get("type", ""),
            "topics": metadata.get("topics", []),
            "snippet": make_snippet(body, query_tokens),
        })

    results.sort(key=lambda item: item["score"], reverse=True)
    return results[:top_k]


def main():
    parser = argparse.ArgumentParser(description="Search selected Obsidian note folders")
    parser.add_argument("query", help="Search query")
    parser.add_argument(
        "--vault",
        default=str(Path.home() / "Documents" / "Obsidian Vault"),
        help="Obsidian vault root path",
    )
    parser.add_argument("--top-k", type=int, default=5, help="Number of results to return")
    parser.add_argument(
        "--dirs",
        help="Comma-separated vault-relative folders to search",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON")
    args = parser.parse_args()

    note_dirs = tuple(item.strip() for item in args.dirs.split(",")) if args.dirs else DEFAULT_NOTE_DIRS
    results = search_notes(args.vault, args.query, top_k=args.top_k, note_dirs=note_dirs)

    if args.json:
        print(json.dumps({"query": args.query, "results": results}, ensure_ascii=False, indent=2))
        return

    print(f"query: {args.query}")
    print(f"results: {len(results)}")
    print()
    for index, item in enumerate(results, start=1):
        print(f"{index}. {item['title']}")
        print(f"score: {item['score']}")
        print(f"path: {item['path']}")
        if item["type"]:
            print(f"type: {item['type']}")
        if item["topics"]:
            print(f"topics: {', '.join(item['topics'])}")
        if item["snippet"]:
            print(f"snippet: {item['snippet']}")
        if index != len(results):
            print()


if __name__ == "__main__":
    main()
