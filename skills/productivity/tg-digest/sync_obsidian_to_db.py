#!/usr/bin/env python3
"""
Sync selected Obsidian notes into digest.db.

This script seeds the SQLite agent layer from the current Obsidian vault so
existing resource candidates and atomic concepts become searchable even before
new tg-digest archive runs populate the database.
"""

import argparse
import json
import re
from pathlib import Path

from archive import (
    insert_link_mention,
    insert_solution_mention,
    setup_db,
    upsert_digest,
    upsert_link,
    upsert_solution,
)


SKIP_FILENAMES = {
    "Входящие ресурсы.md",
    "Каталог ресурсов.md",
    "Каталог концептов.md",
}


def clean_yaml_scalar(value):
    """Strip simple YAML quoting."""
    value = value.strip()
    if value in {"[]", "null", "None"}:
        return []
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def parse_frontmatter(text):
    """Parse the simple frontmatter shape used in this vault."""
    match = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
    if not match:
        return {}, text

    frontmatter = {}
    lines = match.group(1).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if not line.startswith(" ") and ":" in line:
            key, raw_value = line.split(":", 1)
            raw_value = raw_value.strip()
            if raw_value == "":
                items = []
                i += 1
                while i < len(lines):
                    nested = lines[i]
                    stripped = nested.strip()
                    if not stripped:
                        i += 1
                        continue
                    if nested.startswith("  - "):
                        items.append(clean_yaml_scalar(stripped[2:]))
                        i += 1
                        continue
                    break
                frontmatter[key.strip()] = items
                continue
            frontmatter[key.strip()] = clean_yaml_scalar(raw_value)
        i += 1
    return frontmatter, match.group(2)


def ensure_list(value):
    """Normalize a scalar-or-list frontmatter field into a list."""
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        return [item for item in value if item not in (None, "", [])]
    return [value]


def first_value(value, default=""):
    """Take the first value from a scalar-or-list field."""
    values = ensure_list(value)
    return values[0] if values else default


def parse_sections(body):
    """Extract markdown h2 sections."""
    sections = {}
    for match in re.finditer(r"^## (.+?)\n(.*?)(?=^## |\Z)", body, re.MULTILINE | re.DOTALL):
        sections[match.group(1).strip()] = match.group(2).strip()
    return sections


def extract_heading(body, fallback):
    """Extract the first h1 title."""
    match = re.search(r"^# (.+)$", body, re.MULTILINE)
    return match.group(1).strip() if match else fallback


def normalize_markdown_text(text):
    """Convert markdown-ish content to compact plain text."""
    text = text or ""
    text = re.sub(r"\[\[([^\]|]+)(?:\|.*?)?\]\]", r"\1", text)
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[\^[^\]]+\]", "", text)
    text = re.sub(r"^\s*-\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def infer_link_category(what_found):
    """Infer link category from the imported candidate note."""
    match = re.search(r"Тип:\s*`?([a-zA-Z0-9_-]+)`?", what_found)
    if match:
        return match.group(1).lower()
    return "resource"


def build_link_annotation(sections):
    """Build the canonical link annotation from note sections."""
    parts = []
    for heading in ("Что найдено", "Почему это может быть полезно"):
        section = normalize_markdown_text(sections.get(heading, ""))
        if section:
            parts.append(section)
    return " ".join(parts).strip()


def parse_solution_fields(what_found):
    """Extract problem/approach/result from a solution-style candidate note."""
    def extract(label):
        match = re.search(
            rf"\*\*{label}:\*\*\s*(.*?)(?=\n\n\*\*|\Z)",
            what_found,
            re.DOTALL,
        )
        return normalize_markdown_text(match.group(1)) if match else ""

    return extract("Проблема"), extract("Подход"), extract("Результат")


def get_digest_id(conn, cache, chat_name, date_range, html_path=""):
    """Reuse synthetic digests across many imported notes."""
    key = (chat_name, date_range, html_path)
    if key not in cache:
        cache[key] = upsert_digest(
            conn,
            chat_name=chat_name,
            chat_id=None,
            date_range=date_range,
            total_messages=0,
            unique_senders=0,
            html_path=html_path,
        )
    return cache[key]


def sync_resource_candidates(conn, vault_path):
    """Import resource candidates from Resources/Inbox."""
    inbox = Path(vault_path) / "Resources" / "Inbox"
    digest_cache = {}
    stats = {"links": 0, "solutions": 0, "skipped": 0}

    for note_path in sorted(inbox.glob("*.md")):
        if note_path.name in SKIP_FILENAMES:
            continue
        frontmatter, body = parse_frontmatter(note_path.read_text(encoding="utf-8"))
        if frontmatter.get("type") != "resource-candidate":
            continue

        title = frontmatter.get("canonical_name") or extract_heading(body, note_path.stem)
        source_name = first_value(frontmatter.get("source_name"), "obsidian_resource_sync")
        source_date = first_value(frontmatter.get("source_date"), "vault-sync")
        source_path = first_value(frontmatter.get("source_path"), str(note_path))
        evidence_refs = ensure_list(frontmatter.get("evidence_refs"))
        sections = parse_sections(body)
        digest_id = get_digest_id(conn, digest_cache, str(source_name), str(source_date), str(source_path))
        telegram_url = next((item for item in evidence_refs if isinstance(item, str) and item.startswith("http")), None)

        url = first_value(frontmatter.get("url"))
        if url:
            link_id = upsert_link(
                conn,
                url=url,
                title=title,
                annotation=build_link_annotation(sections),
                category=infer_link_category(sections.get("Что найдено", "")),
            )
            insert_link_mention(
                conn,
                link_id=link_id,
                digest_id=digest_id,
                message_id=None,
                telegram_url=telegram_url,
                sender=str(source_name),
                total_reactions=0,
                engagement_score=0.0,
                reactions_detail=None,
                snippet=normalize_markdown_text(sections.get("Что найдено", ""))[:200],
            )
            stats["links"] += 1
            continue

        problem, approach, result = parse_solution_fields(sections.get("Что найдено", ""))
        if not problem or not approach:
            stats["skipped"] += 1
            continue

        solution_id = upsert_solution(
            conn,
            problem=problem,
            approach=approach,
            result=result,
            author="",
            tools=[],
            sol_type="solution",
        )
        insert_solution_mention(
            conn,
            solution_id=solution_id,
            digest_id=digest_id,
            message_id=None,
            telegram_url=telegram_url,
            engagement_score=0.0,
        )
        stats["solutions"] += 1

    return stats


def sync_concepts(conn, vault_path):
    """Import atomic concept notes as reusable insights."""
    concepts_dir = Path(vault_path) / "Concepts"
    digest_id = upsert_digest(
        conn,
        chat_name="obsidian_concepts",
        chat_id=None,
        date_range="vault-sync",
        total_messages=0,
        unique_senders=0,
        html_path=str(concepts_dir),
    )
    stats = {"insights": 0, "skipped": 0}

    for note_path in sorted(concepts_dir.glob("*.md")):
        if note_path.name in SKIP_FILENAMES:
            continue
        frontmatter, body = parse_frontmatter(note_path.read_text(encoding="utf-8"))
        if frontmatter.get("type") != "concept":
            continue

        sections = parse_sections(body)
        title = extract_heading(body, note_path.stem)
        aliases = ensure_list(frontmatter.get("aliases"))
        summary = normalize_markdown_text(sections.get("Суть", ""))
        practice = normalize_markdown_text(sections.get("Как применять", "")) or normalize_markdown_text(
            sections.get("Когда применять", "")
        )
        text = " ".join(part for part in [summary, practice] if part).strip()
        if not text:
            stats["skipped"] += 1
            continue

        conn.execute(
            """INSERT OR IGNORE INTO insights
               (text, topic_title, participants_json, engagement_level, digest_id)
               VALUES (?, ?, ?, ?, ?)""",
            (text, title, json.dumps(aliases, ensure_ascii=False), "reference", digest_id),
        )
        stats["insights"] += 1

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Sync Obsidian resource candidates and concepts into digest.db"
    )
    parser.add_argument(
        "--vault",
        default=str(Path.home() / "Documents" / "Obsidian Vault"),
        help="Obsidian vault root path",
    )
    parser.add_argument(
        "--db",
        default=str(Path.home() / "Downloads" / "Проекты" / "tg-digest" / "digest.db"),
        help="SQLite database path",
    )
    args = parser.parse_args()

    conn = setup_db(args.db)
    try:
        resource_stats = sync_resource_candidates(conn, args.vault)
        concept_stats = sync_concepts(conn, args.vault)
        conn.commit()
    finally:
        conn.close()

    print(
        f"Synced Obsidian -> DB: links={resource_stats['links']}, "
        f"solutions={resource_stats['solutions']}, "
        f"concept_insights={concept_stats['insights']}, "
        f"skipped={resource_stats['skipped'] + concept_stats['skipped']}"
    )
    print(f"DB: {args.db}")


if __name__ == "__main__":
    main()
