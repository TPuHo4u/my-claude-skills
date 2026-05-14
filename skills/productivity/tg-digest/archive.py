#!/usr/bin/env python3
"""
Archive digest knowledge to SQLite + Obsidian vault.

Persists links, solutions, and insights extracted during the tg-digest pipeline
BEFORE delivery, so knowledge is safe even if Telegram send fails.

Storage layers:
  1. SQLite DB (FTS5) — agent search layer, stores ALL data with full provenance
  2. Obsidian Vault — human knowledge layer, stores only filtered high-value Resource Candidates

Usage:
  python3 archive.py \
    --map-results /tmp/tg-digest-map-results.json \
    --raw-messages /tmp/tg-digest-raw.json \
    --chat-name "вайбкодеры" \
    --date-range "2026-03-10 -> 2026-03-13"

  # With enriched links (better annotations):
  python3 archive.py \
    --map-results /tmp/tg-digest-map-results.json \
    --enriched-links /tmp/tg-digest-links-enriched.json \
    --raw-messages /tmp/tg-digest-raw.json \
    --chat-name "вайбкодеры" \
    --date-range "2026-03-10 -> 2026-03-13"

  # With semantic embeddings:
  python3 archive.py \
    --map-results /tmp/tg-digest-map-results.json \
    --raw-messages /tmp/tg-digest-raw.json \
    --chat-name "вайбкодеры" \
    --date-range "2026-03-10 -> 2026-03-13" \
    --embed

  # Backfill embeddings for an existing digest.db:
  python3 archive.py \
    --db ~/Downloads/Проекты/tg-digest/digest.db \
    --embed \
    --embed-existing
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
-- Digest runs
CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_name TEXT NOT NULL,
    chat_id INTEGER,
    date_range TEXT NOT NULL,
    total_messages INTEGER,
    unique_senders INTEGER,
    html_path TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(chat_name, date_range)
);

-- Canonical links (deduplicated by URL)
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    title TEXT,
    annotation TEXT,
    category TEXT,
    obsidian_note TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Each mention of a link in a digest/post
CREATE TABLE IF NOT EXISTS link_mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id INTEGER NOT NULL REFERENCES links(id),
    digest_id INTEGER NOT NULL REFERENCES digests(id),
    message_id INTEGER,
    telegram_url TEXT,
    sender TEXT,
    total_reactions INTEGER DEFAULT 0,
    engagement_score REAL DEFAULT 0.0,
    reactions_detail TEXT,
    snippet TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(link_id, digest_id, message_id)
);

-- Canonical solutions (deduplicated by problem + author)
CREATE TABLE IF NOT EXISTS solutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    problem TEXT NOT NULL,
    approach TEXT NOT NULL,
    result TEXT,
    author TEXT,
    tools_json TEXT,
    type TEXT,
    obsidian_note TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(problem, author)
);

-- Each mention of a solution
CREATE TABLE IF NOT EXISTS solution_mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    solution_id INTEGER NOT NULL REFERENCES solutions(id),
    digest_id INTEGER NOT NULL REFERENCES digests(id),
    message_id INTEGER,
    telegram_url TEXT,
    engagement_score REAL DEFAULT 0.0,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(solution_id, digest_id, message_id)
);

-- Key insights from topics
CREATE TABLE IF NOT EXISTS insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    topic_title TEXT,
    participants_json TEXT,
    engagement_level TEXT,
    digest_id INTEGER REFERENCES digests(id),
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(text, digest_id)
);

-- Semantic embeddings for canonical entities
CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    model TEXT NOT NULL,
    input_text TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    embedding_json TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(entity_type, entity_id)
);

CREATE INDEX IF NOT EXISTS embeddings_hash_idx
ON embeddings(model, input_hash);

-- FTS5 indexes
CREATE VIRTUAL TABLE IF NOT EXISTS links_fts USING fts5(
    url, title, annotation, category,
    content='links', content_rowid='id'
);

CREATE VIRTUAL TABLE IF NOT EXISTS solutions_fts USING fts5(
    problem, approach, result, author, tools_json,
    content='solutions', content_rowid='id'
);

CREATE VIRTUAL TABLE IF NOT EXISTS insights_fts USING fts5(
    text, topic_title, participants_json,
    content='insights', content_rowid='id'
);

-- Triggers to keep FTS in sync: links
CREATE TRIGGER IF NOT EXISTS links_ai AFTER INSERT ON links BEGIN
    INSERT INTO links_fts(rowid, url, title, annotation, category)
    VALUES (new.id, new.url, new.title, new.annotation, new.category);
END;

CREATE TRIGGER IF NOT EXISTS links_au AFTER UPDATE ON links BEGIN
    INSERT INTO links_fts(links_fts, rowid, url, title, annotation, category)
    VALUES ('delete', old.id, old.url, old.title, old.annotation, old.category);
    INSERT INTO links_fts(rowid, url, title, annotation, category)
    VALUES (new.id, new.url, new.title, new.annotation, new.category);
END;

CREATE TRIGGER IF NOT EXISTS links_ad AFTER DELETE ON links BEGIN
    INSERT INTO links_fts(links_fts, rowid, url, title, annotation, category)
    VALUES ('delete', old.id, old.url, old.title, old.annotation, old.category);
END;

-- Triggers: solutions
CREATE TRIGGER IF NOT EXISTS solutions_ai AFTER INSERT ON solutions BEGIN
    INSERT INTO solutions_fts(rowid, problem, approach, result, author, tools_json)
    VALUES (new.id, new.problem, new.approach, new.result, new.author, new.tools_json);
END;

CREATE TRIGGER IF NOT EXISTS solutions_au AFTER UPDATE ON solutions BEGIN
    INSERT INTO solutions_fts(solutions_fts, rowid, problem, approach, result, author, tools_json)
    VALUES ('delete', old.id, old.problem, old.approach, old.result, old.author, old.tools_json);
    INSERT INTO solutions_fts(rowid, problem, approach, result, author, tools_json)
    VALUES (new.id, new.problem, new.approach, new.result, new.author, new.tools_json);
END;

CREATE TRIGGER IF NOT EXISTS solutions_ad AFTER DELETE ON solutions BEGIN
    INSERT INTO solutions_fts(solutions_fts, rowid, problem, approach, result, author, tools_json)
    VALUES ('delete', old.id, old.problem, old.approach, old.result, old.author, old.tools_json);
END;

-- Triggers: insights
CREATE TRIGGER IF NOT EXISTS insights_ai AFTER INSERT ON insights BEGIN
    INSERT INTO insights_fts(rowid, text, topic_title, participants_json)
    VALUES (new.id, new.text, new.topic_title, new.participants_json);
END;

CREATE TRIGGER IF NOT EXISTS insights_au AFTER UPDATE ON insights BEGIN
    INSERT INTO insights_fts(insights_fts, rowid, text, topic_title, participants_json)
    VALUES ('delete', old.id, old.text, old.topic_title, old.participants_json);
    INSERT INTO insights_fts(rowid, text, topic_title, participants_json)
    VALUES (new.id, new.text, new.topic_title, new.participants_json);
END;

CREATE TRIGGER IF NOT EXISTS insights_ad AFTER DELETE ON insights BEGIN
    INSERT INTO insights_fts(insights_fts, rowid, text, topic_title, participants_json)
    VALUES ('delete', old.id, old.text, old.topic_title, old.participants_json);
END;
"""


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def setup_db(db_path: str) -> sqlite3.Connection:
    """Create tables, FTS indexes, and triggers."""
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    migrate_schema(conn)
    conn.commit()
    return conn


def migrate_schema(conn):
    """Upgrade older archival tables that need stronger provenance semantics."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'solution_mentions'"
    ).fetchone()
    if not row:
        return

    table_sql = row[0] or ""
    if "UNIQUE(solution_id, digest_id))" in table_sql or "UNIQUE(solution_id, digest_id)" in table_sql:
        conn.execute("ALTER TABLE solution_mentions RENAME TO solution_mentions_old")
        conn.execute(
            """
            CREATE TABLE solution_mentions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                solution_id INTEGER NOT NULL REFERENCES solutions(id),
                digest_id INTEGER NOT NULL REFERENCES digests(id),
                message_id INTEGER,
                telegram_url TEXT,
                engagement_score REAL DEFAULT 0.0,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(solution_id, digest_id, message_id)
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO solution_mentions
                (solution_id, digest_id, message_id, telegram_url, engagement_score, created_at)
            SELECT solution_id, digest_id, message_id, telegram_url, engagement_score, created_at
            FROM solution_mentions_old
            """
        )
        conn.execute("DROP TABLE solution_mentions_old")


def upsert_digest(conn, chat_name, chat_id, date_range,
                   total_messages, unique_senders, html_path=None):
    """INSERT OR IGNORE digest, return digest_id."""
    conn.execute(
        """INSERT OR IGNORE INTO digests
           (chat_name, chat_id, date_range, total_messages, unique_senders, html_path)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (chat_name, chat_id, date_range, total_messages, unique_senders, html_path),
    )
    row = conn.execute(
        "SELECT id FROM digests WHERE chat_name = ? AND date_range = ?",
        (chat_name, date_range),
    ).fetchone()
    return row[0]


def upsert_link(conn, url, title=None, annotation=None, category=None):
    """INSERT or enrich existing link. Returns link_id."""
    existing = conn.execute(
        "SELECT id, title, annotation FROM links WHERE url = ?", (url,)
    ).fetchone()

    if existing:
        link_id, old_title, old_annotation = existing
        new_title = title if (title and (not old_title or len(title) > len(old_title))) else old_title
        new_annotation = annotation if (annotation and (not old_annotation or len(annotation) > len(old_annotation))) else old_annotation
        conn.execute(
            """UPDATE links SET title = ?, annotation = ?, category = COALESCE(?, category),
               updated_at = datetime('now') WHERE id = ?""",
            (new_title, new_annotation, category, link_id),
        )
        return link_id
    else:
        cur = conn.execute(
            "INSERT INTO links (url, title, annotation, category) VALUES (?, ?, ?, ?)",
            (url, title, annotation, category),
        )
        return cur.lastrowid


def insert_link_mention(conn, link_id, digest_id, message_id,
                         telegram_url, sender, total_reactions=0,
                         engagement_score=0.0, reactions_detail=None, snippet=None):
    """Insert or enrich a link mention, including synthetic mentions without raw messages."""
    if message_id is None:
        existing = conn.execute(
            """SELECT id, total_reactions, engagement_score, reactions_detail, snippet
               FROM link_mentions
               WHERE link_id = ? AND digest_id = ? AND message_id IS NULL""",
            (link_id, digest_id),
        ).fetchone()
    else:
        existing = conn.execute(
            """SELECT id, total_reactions, engagement_score, reactions_detail, snippet
               FROM link_mentions
               WHERE link_id = ? AND digest_id = ? AND message_id = ?""",
            (link_id, digest_id, message_id),
        ).fetchone()

    if existing:
        mention_id, old_reactions, old_engagement, old_detail, old_snippet = existing
        merged_detail = reactions_detail if reactions_detail and len(reactions_detail) > len(old_detail or "") else old_detail
        merged_snippet = snippet if snippet and len(snippet) > len(old_snippet or "") else old_snippet
        conn.execute(
            """UPDATE link_mentions
               SET telegram_url = COALESCE(?, telegram_url),
                   sender = COALESCE(?, sender),
                   total_reactions = ?,
                   engagement_score = ?,
                   reactions_detail = ?,
                   snippet = ?
               WHERE id = ?""",
            (
                telegram_url,
                sender,
                max(old_reactions or 0, total_reactions or 0),
                max(old_engagement or 0.0, engagement_score or 0.0),
                merged_detail,
                merged_snippet,
                mention_id,
            ),
        )
        return mention_id

    cur = conn.execute(
        """INSERT INTO link_mentions
           (link_id, digest_id, message_id, telegram_url, sender,
            total_reactions, engagement_score, reactions_detail, snippet)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (link_id, digest_id, message_id, telegram_url, sender,
         total_reactions, engagement_score, reactions_detail, snippet),
    )
    return cur.lastrowid


def upsert_solution(conn, problem, approach, result=None, author=None,
                     tools=None, sol_type=None):
    """INSERT or enrich existing solution. Returns solution_id."""
    # Coalesce None author to "" so UNIQUE(problem, author) works in SQLite
    author = author or ""
    tools_json = json.dumps(tools or [], ensure_ascii=False)
    existing = conn.execute(
        "SELECT id, result FROM solutions WHERE problem = ? AND author = ?",
        (problem, author),
    ).fetchone()

    if existing:
        sol_id, old_result = existing
        new_result = result if (result and (not old_result or len(result) > len(old_result))) else old_result
        conn.execute(
            """UPDATE solutions SET approach = ?, result = ?, tools_json = ?,
               type = COALESCE(?, type), updated_at = datetime('now') WHERE id = ?""",
            (approach, new_result, tools_json, sol_type, sol_id),
        )
        return sol_id
    else:
        cur = conn.execute(
            """INSERT INTO solutions (problem, approach, result, author, tools_json, type)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (problem, approach, result, author, tools_json, sol_type),
        )
        return cur.lastrowid


def insert_solution_mention(conn, solution_id, digest_id,
                             message_id=None, telegram_url=None,
                             engagement_score=0.0):
    """Insert or enrich a solution mention, including synthetic mentions without raw messages."""
    if message_id is None:
        existing = conn.execute(
            """SELECT id, telegram_url, engagement_score
               FROM solution_mentions
               WHERE solution_id = ? AND digest_id = ? AND message_id IS NULL""",
            (solution_id, digest_id),
        ).fetchone()
    else:
        existing = conn.execute(
            """SELECT id, telegram_url, engagement_score
               FROM solution_mentions
               WHERE solution_id = ? AND digest_id = ? AND message_id = ?""",
            (solution_id, digest_id, message_id),
        ).fetchone()

    if existing:
        mention_id, old_tg_url, old_engagement = existing
        conn.execute(
            """UPDATE solution_mentions
               SET telegram_url = COALESCE(?, telegram_url),
                   engagement_score = ?
               WHERE id = ?""",
            (
                telegram_url or old_tg_url,
                max(old_engagement or 0.0, engagement_score or 0.0),
                mention_id,
            ),
        )
        return mention_id

    cur = conn.execute(
        """INSERT INTO solution_mentions
           (solution_id, digest_id, message_id, telegram_url, engagement_score)
           VALUES (?, ?, ?, ?, ?)""",
        (solution_id, digest_id, message_id, telegram_url, engagement_score),
    )
    return cur.lastrowid


def insert_insights(conn, topics, digest_id):
    """Extract key_facts from topics and insert as insights."""
    count = 0
    insight_ids = set()
    for topic in topics:
        title = topic.get("title", "")
        participants = topic.get("key_participants", [])
        engagement = topic.get("engagement_level", "medium")
        for fact in topic.get("key_facts", []):
            cur = conn.execute(
                """INSERT OR IGNORE INTO insights (text, topic_title, participants_json,
                   engagement_level, digest_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (fact, title, json.dumps(participants, ensure_ascii=False),
                 engagement, digest_id),
            )
            if cur.rowcount > 0:
                count += 1
            row = conn.execute(
                "SELECT id FROM insights WHERE text = ? AND digest_id = ?",
                (fact, digest_id),
            ).fetchone()
            if row:
                insight_ids.add(row[0])
    return count, insight_ids


# ---------------------------------------------------------------------------
# Telegram URL builder
# ---------------------------------------------------------------------------

def build_telegram_url(chat_id, msg_id):
    """Build best-effort t.me URL for a message.

    Works for supergroups (most Telegram chats).
    TODO: enhance fetch_messages.py to store username/dialog_type for public channels.
    """
    if chat_id is None or msg_id is None:
        return None
    # Strip -100 prefix used by Telegram for supergroups
    stripped = str(chat_id).replace("-100", "", 1)
    return f"https://t.me/c/{stripped}/{msg_id}"


# ---------------------------------------------------------------------------
# Message matching helpers
# ---------------------------------------------------------------------------

def find_message_ids_for_url(url, raw_messages):
    """Scan raw messages for URL, return list of (msg_id, sender, reactions_count, snippet)."""
    results = []
    for msg in raw_messages:
        msg_urls = msg.get("urls", [])
        if url in msg_urls:
            results.append((
                msg.get("id"),
                msg.get("sender", ""),
                msg.get("reactions_count", 0) or msg.get("total_reactions", 0),
                (msg.get("text") or "")[:200],
            ))
    return results


def find_message_ids_for_solution(author, keywords, raw_messages):
    """Scan raw messages by author + keyword match, return list of (msg_id, engagement_score)."""
    results = []
    kw_lower = [k.lower() for k in keywords if k]
    for msg in raw_messages:
        sender = msg.get("sender", "")
        if author and author.lower() not in sender.lower():
            continue
        text = (msg.get("text") or "").lower()
        if any(kw in text for kw in kw_lower):
            eng = msg.get("engagement_score", 0.0)
            if eng == 0.0:
                eng = msg.get("reactions_count", 0) * 1.5
            results.append((msg.get("id"), eng))
    return results


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------

def load_json(path):
    """Load JSON file, return None if missing."""
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_inputs(args):
    """Load all input JSONs. Gracefully handle missing optional files."""
    map_results = load_json(args.map_results)
    if map_results is None:
        print(f"ERROR: --map-results file not found: {args.map_results}")
        sys.exit(1)

    enriched_links = load_json(args.enriched_links)  # optional
    raw_data = load_json(args.raw_messages)  # optional but recommended

    return map_results, enriched_links, raw_data


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

def env_first(*names, default=None):
    """Return the first non-empty environment variable."""
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def normalize_embedding_endpoint(base_url: str) -> str:
    """Accept either an API base URL or a full embeddings endpoint."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return "https://api.openai.com/v1/embeddings"
    if base.endswith("/embeddings"):
        return base
    return base + "/embeddings"


def normalize_embedding_text(text: str) -> str:
    """Compact whitespace before hashing or sending to the embedding API."""
    return re.sub(r"\s+", " ", (text or "")).strip()


def hash_embedding_text(text: str) -> str:
    """Stable hash used to skip redundant embedding requests."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class EmbeddingClient:
    """Minimal OpenAI-compatible embeddings client with no extra dependencies."""

    def __init__(self, model: str, endpoint: str, api_key=None, timeout_seconds=30.0,
                 dimensions=None):
        self.model = model
        self.endpoint = normalize_embedding_endpoint(endpoint)
        self.api_key = api_key or ""
        self.timeout_seconds = timeout_seconds
        self.dimensions = dimensions

    def embed(self, text: str):
        payload = {
            "model": self.model,
            "input": text,
        }
        if self.dimensions:
            payload["dimensions"] = self.dimensions

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "tg-digest-archive/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Embedding request failed with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Embedding request failed: {exc.reason}") from exc

        try:
            embedding = body["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected embedding response: {body}") from exc

        if not isinstance(embedding, list) or not embedding:
            raise RuntimeError(f"Embedding payload is empty: {body}")
        return embedding


def build_link_embedding_text(url, title, annotation, category):
    """Semantic text for link retrieval."""
    return normalize_embedding_text(
        "\n".join(
            part for part in [
                f"title: {title}" if title else "",
                f"annotation: {annotation}" if annotation else "",
                f"category: {category}" if category else "",
                f"url: {url}" if url else "",
            ] if part
        )
    )


def build_solution_embedding_text(problem, approach, result, author, tools, sol_type):
    """Semantic text for solution retrieval."""
    tool_text = ", ".join(tools or [])
    return normalize_embedding_text(
        "\n".join(
            part for part in [
                f"problem: {problem}" if problem else "",
                f"approach: {approach}" if approach else "",
                f"result: {result}" if result else "",
                f"author: {author}" if author else "",
                f"tools: {tool_text}" if tool_text else "",
                f"type: {sol_type}" if sol_type else "",
            ] if part
        )
    )


def build_insight_embedding_text(text, topic_title, participants, engagement_level):
    """Semantic text for insight retrieval."""
    participant_text = ", ".join(participants or [])
    return normalize_embedding_text(
        "\n".join(
            part for part in [
                f"topic: {topic_title}" if topic_title else "",
                f"insight: {text}" if text else "",
                f"participants: {participant_text}" if participant_text else "",
                f"engagement: {engagement_level}" if engagement_level else "",
            ] if part
        )
    )


def parse_dimensions(value):
    """Parse dimensions from args/env while tolerating empty values."""
    if value in (None, ""):
        return None
    return int(value)


def get_embedding_config(args):
    """Build runtime embedding config from CLI flags and environment."""
    endpoint = args.embedding_base_url or env_first(
        "EMBEDDING_BASE_URL",
        "EMBEDDING_API_BASE",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        default="https://api.openai.com/v1",
    )
    api_key = args.embedding_api_key or env_first(
        "EMBEDDING_API_KEY",
        "OPENAI_API_KEY",
    )
    model = args.embedding_model or env_first(
        "EMBEDDING_MODEL",
        "OPENAI_EMBEDDING_MODEL",
        default="text-embedding-3-small",
    )
    timeout_seconds = float(
        args.embedding_timeout_seconds
        if args.embedding_timeout_seconds is not None
        else env_first("EMBEDDING_TIMEOUT_SECONDS", default="30")
    )
    dimensions = parse_dimensions(
        args.embedding_dimensions
        if args.embedding_dimensions is not None
        else env_first("EMBEDDING_DIMENSIONS")
    )
    return {
        "endpoint": endpoint,
        "api_key": api_key,
        "model": model,
        "timeout_seconds": timeout_seconds,
        "dimensions": dimensions,
    }


def placeholder_sql(ids):
    """Create ?,?,?... placeholders for IN (...) queries."""
    return ",".join("?" for _ in ids)


def collect_embedding_items(conn, link_ids, solution_ids, insight_ids):
    """Load semantic text inputs only for entities touched by the current run."""
    items = []

    if link_ids:
        rows = conn.execute(
            f"""SELECT id, url, title, annotation, category
                FROM links
                WHERE id IN ({placeholder_sql(link_ids)})""",
            tuple(sorted(link_ids)),
        ).fetchall()
        for row in rows:
            entity_id, url, title, annotation, category = row
            text = build_link_embedding_text(url, title, annotation, category)
            if text:
                items.append(("link", entity_id, text))

    if solution_ids:
        rows = conn.execute(
            f"""SELECT id, problem, approach, result, author, tools_json, type
                FROM solutions
                WHERE id IN ({placeholder_sql(solution_ids)})""",
            tuple(sorted(solution_ids)),
        ).fetchall()
        for row in rows:
            entity_id, problem, approach, result, author, tools_json, sol_type = row
            tools = json.loads(tools_json) if tools_json else []
            text = build_solution_embedding_text(
                problem, approach, result, author, tools, sol_type
            )
            if text:
                items.append(("solution", entity_id, text))

    if insight_ids:
        rows = conn.execute(
            f"""SELECT id, text, topic_title, participants_json, engagement_level
                FROM insights
                WHERE id IN ({placeholder_sql(insight_ids)})""",
            tuple(sorted(insight_ids)),
        ).fetchall()
        for row in rows:
            entity_id, text, topic_title, participants_json, engagement_level = row
            participants = json.loads(participants_json) if participants_json else []
            embedding_text = build_insight_embedding_text(
                text, topic_title, participants, engagement_level
            )
            if embedding_text:
                items.append(("insight", entity_id, embedding_text))

    return items


def upsert_embedding(conn, entity_type, entity_id, text, client, cache):
    """Store or reuse an embedding for a canonical entity."""
    input_hash = hash_embedding_text(text)
    existing = conn.execute(
        """SELECT id, model, input_hash
           FROM embeddings
           WHERE entity_type = ? AND entity_id = ?""",
        (entity_type, entity_id),
    ).fetchone()

    if existing and existing[1] == client.model and existing[2] == input_hash:
        return "up_to_date"

    cached = cache.get((client.model, input_hash))
    if cached is None:
        cached = conn.execute(
            """SELECT embedding_json, dimensions
               FROM embeddings
               WHERE model = ? AND input_hash = ?
               ORDER BY id
               LIMIT 1""",
            (client.model, input_hash),
        ).fetchone()
        if cached:
            cached = (json.loads(cached[0]), cached[1])
            cache[(client.model, input_hash)] = cached

    status = "reused"
    if cached is None:
        vector = client.embed(text)
        cached = (vector, len(vector))
        cache[(client.model, input_hash)] = cached
        status = "generated"

    vector, dimensions = cached
    embedding_json = json.dumps(vector, ensure_ascii=False, separators=(",", ":"))

    if existing:
        conn.execute(
            """UPDATE embeddings
               SET model = ?, input_text = ?, input_hash = ?, dimensions = ?,
                   embedding_json = ?, updated_at = datetime('now')
               WHERE entity_type = ? AND entity_id = ?""",
            (
                client.model,
                text,
                input_hash,
                dimensions,
                embedding_json,
                entity_type,
                entity_id,
            ),
        )
    else:
        conn.execute(
            """INSERT INTO embeddings
               (entity_type, entity_id, model, input_text, input_hash, dimensions, embedding_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                entity_type,
                entity_id,
                client.model,
                text,
                input_hash,
                dimensions,
                embedding_json,
            ),
        )
    return status


def generate_embeddings(conn, args, link_ids, solution_ids, insight_ids):
    """Generate embeddings for entities touched by the current archival run."""
    config = get_embedding_config(args)
    endpoint = normalize_embedding_endpoint(config["endpoint"])
    if endpoint.startswith("https://api.openai.com") and not config["api_key"]:
        raise RuntimeError(
            "--embed requires EMBEDDING_API_KEY or OPENAI_API_KEY for the default OpenAI endpoint"
        )

    client = EmbeddingClient(
        model=config["model"],
        endpoint=config["endpoint"],
        api_key=config["api_key"],
        timeout_seconds=config["timeout_seconds"],
        dimensions=config["dimensions"],
    )
    items = collect_embedding_items(conn, link_ids, solution_ids, insight_ids)
    stats = {"generated": 0, "reused": 0, "up_to_date": 0, "total": len(items)}
    cache = {}

    for entity_type, entity_id, text in items:
        status = upsert_embedding(conn, entity_type, entity_id, text, client, cache)
        stats[status] += 1

    conn.commit()
    stats["model"] = client.model
    stats["endpoint"] = endpoint
    return stats


def collect_all_entity_ids(conn):
    """Collect every canonical entity id currently present in the database."""
    link_ids = {row[0] for row in conn.execute("SELECT id FROM links").fetchall()}
    solution_ids = {row[0] for row in conn.execute("SELECT id FROM solutions").fetchall()}
    insight_ids = {row[0] for row in conn.execute("SELECT id FROM insights").fetchall()}
    return link_ids, solution_ids, insight_ids


def backfill_embeddings(args):
    """Generate embeddings for all existing canonical entities in the database."""
    conn = setup_db(args.db)
    try:
        link_ids, solution_ids, insight_ids = collect_all_entity_ids(conn)
        total_entities = len(link_ids) + len(solution_ids) + len(insight_ids)
        if total_entities == 0:
            print(f"Embeddings: nothing to backfill in {args.db}")
            return

        stats = generate_embeddings(conn, args, link_ids, solution_ids, insight_ids)
        print(
            f"Backfilled embeddings for {total_entities} entities: "
            f"{stats['generated']} generated, "
            f"{stats['reused']} reused, "
            f"{stats['up_to_date']} up-to-date "
            f"(model={stats['model']})"
        )
        print(
            f"Entity counts: links={len(link_ids)}, "
            f"solutions={len(solution_ids)}, "
            f"insights={len(insight_ids)}"
        )
        print(f"DB: {args.db}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Obsidian note creation
# ---------------------------------------------------------------------------

def sanitize_filename(title):
    """Sanitize title for use as filename."""
    name = re.sub(r'[<>:"/\\|?*]', '-', title)
    name = re.sub(r'\s+', ' ', name).strip()
    return name[:80]


def extract_date_from_range(date_range):
    """Extract end date from date_range string like '2026-03-10 -> 2026-03-13'."""
    # Try to find a date pattern at the end
    dates = re.findall(r'\d{4}-\d{2}-\d{2}', date_range)
    if dates:
        return dates[-1]  # last date = end date
    return datetime.now().strftime("%Y-%m-%d")


def find_existing_note(inbox_path, url=None, canonical_name=None):
    """Scan .md files in inbox for matching url: or canonical_name: in frontmatter."""
    inbox = Path(inbox_path)
    if not inbox.exists():
        return None
    for md_file in inbox.glob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8")
            # Check first 20 lines for frontmatter fields
            for line in text.split("\n")[:20]:
                stripped = line.strip()
                if url and stripped.startswith("url:") and url in stripped:
                    return md_file
                if canonical_name and stripped.startswith("canonical_name:") and canonical_name in stripped:
                    return md_file
        except Exception:
            continue
    return None


def create_or_update_obsidian_note(
    inbox_path, url, title, annotation, category,
    chat_name, date_range, html_path, telegram_urls,
    note_type="link",
    problem=None, approach=None, result=None, tools=None,
):
    """Create or update an Obsidian Resource Candidate note."""
    inbox = Path(inbox_path)
    inbox.mkdir(parents=True, exist_ok=True)

    end_date = extract_date_from_range(date_range)
    display_title = title or (problem[:60] if problem else url or "Untitled")
    existing = find_existing_note(str(inbox), url=url, canonical_name=display_title)

    if existing:
        # Update existing note: append new evidence_refs
        content = existing.read_text(encoding="utf-8")
        for tg_url in telegram_urls:
            if tg_url and tg_url not in content:
                # Insert before closing --- or after evidence_refs
                ref_line = f'  - "{tg_url}"'
                if "evidence_refs:" in content:
                    content = content.replace(
                        "evidence_refs:", "evidence_refs:\n" + ref_line, 1
                    )
                    # Remove duplicate empty line
                    content = content.replace(
                        "evidence_refs:\n" + ref_line + "\n  -",
                        "evidence_refs:\n" + ref_line + "\n  -",
                    )
        # Update annotation if richer
        if annotation and len(annotation) > 50:
            old_section = re.search(
                r'## Что найдено\n(.*?)(?=\n## )', content, re.DOTALL
            )
            if old_section and len(annotation) > len(old_section.group(1).strip()):
                content = content.replace(
                    old_section.group(0),
                    f"## Что найдено\n{annotation}\n",
                )
        existing.write_text(content, encoding="utf-8")
        return str(existing), "updated"

    # Create new note
    safe_name = sanitize_filename(display_title)
    filename = f"{safe_name} ({end_date}).md"
    filepath = inbox / filename

    # Avoid overwriting
    counter = 1
    while filepath.exists():
        counter += 1
        filename = f"{safe_name} ({end_date}) {counter}.md"
        filepath = inbox / filename

    # Infer topics from category
    topic_map = {
        "repo": "Open Source",
        "tool": "Инструменты",
        "service": "Сервисы",
        "article": "Статьи",
        "video": "Видео",
        "docs": "Документация",
        "news": "Новости",
    }
    topic = topic_map.get(category, "AI-агенты и vibe coding")

    evidence_lines = "\n".join(f'  - "{u}"' for u in telegram_urls if u)

    # Build content sections
    if note_type == "solution" and problem:
        what_found = f"**Проблема:** {problem}\n\n**Подход:** {approach or ''}\n\n**Результат:** {result or ''}"
        why_useful = "Практическое решение из реального опыта участника чата."
        how_to_apply = "Инструменты: " + ", ".join(tools or []) if tools else "См. описание подхода выше."
    else:
        what_found = annotation or ""
        why_useful = f"Поделились в чате {chat_name} с высоким engagement."
        how_to_apply = f"Перейти по ссылке: {url}" if url else ""

    note_content = f"""---
type: resource-candidate
status: candidate
source_type: telegram_digest
source_name: "{chat_name}"
source_date: "{date_range}"
source_path: "{html_path or ''}"
canonical_name: "{display_title}"
url: "{url or ''}"
topics:
  - {topic}
related_projects: []
evidence_refs:
{evidence_lines}
---

# {display_title}

## Что найдено
{what_found}

## Почему это может быть полезно
{why_useful}

## Как проверить или применить
{how_to_apply}

## Решение
- [ ] review
- [ ] promote to canonical resource

## Связанные заметки
- [[Входящие ресурсы]]
- [[AI-агенты и vibe coding]]
"""

    filepath.write_text(note_content, encoding="utf-8")
    return str(filepath), "created"


def create_obsidian_notes(conn, vault_path, chat_name, date_range,
                           chat_id, html_path=None):
    """Create filtered Obsidian notes for high-value links and all solutions."""
    inbox_path = os.path.join(vault_path, "Resources", "Inbox")
    stats = {"links_created": 0, "links_updated": 0, "solutions_created": 0, "solutions_updated": 0}

    # High-engagement links: total_reactions >= 5 OR engagement_score >= 10
    rows = conn.execute("""
        SELECT l.id, l.url, l.title, l.annotation, l.category,
               MAX(lm.total_reactions) as max_reactions,
               MAX(lm.engagement_score) as max_engagement
        FROM links l
        JOIN link_mentions lm ON lm.link_id = l.id
        GROUP BY l.id
        HAVING max_reactions >= 5 OR max_engagement >= 10
    """).fetchall()

    for row in rows:
        link_id, url, title, annotation, category, _, _ = row
        # Gather telegram_urls for this link
        tg_urls = [r[0] for r in conn.execute(
            "SELECT telegram_url FROM link_mentions WHERE link_id = ? AND telegram_url IS NOT NULL",
            (link_id,)
        ).fetchall()]

        path, action = create_or_update_obsidian_note(
            inbox_path, url, title, annotation, category,
            chat_name, date_range, html_path, tg_urls,
        )
        conn.execute("UPDATE links SET obsidian_note = ? WHERE id = ?", (path, link_id))
        if action == "created":
            stats["links_created"] += 1
        else:
            stats["links_updated"] += 1

    # ALL solutions
    sol_rows = conn.execute("""
        SELECT s.id, s.problem, s.approach, s.result, s.author, s.tools_json, s.type
        FROM solutions s
    """).fetchall()

    for row in sol_rows:
        sol_id, problem, approach, result, author, tools_json, sol_type = row
        tools = json.loads(tools_json) if tools_json else []
        # Gather telegram_urls
        tg_urls = [r[0] for r in conn.execute(
            "SELECT telegram_url FROM solution_mentions WHERE solution_id = ? AND telegram_url IS NOT NULL",
            (sol_id,)
        ).fetchall()]

        sol_title = f"{problem[:50]} — {author or 'unknown'}"
        sol_annotation = f"**Проблема:** {problem}\n\n**Подход:** {approach or ''}\n\n**Результат:** {result or ''}"
        path, action = create_or_update_obsidian_note(
            inbox_path, None, sol_title, sol_annotation, None,
            chat_name, date_range, html_path, tg_urls,
            note_type="solution",
            problem=problem, approach=approach, result=result, tools=tools,
        )
        conn.execute("UPDATE solutions SET obsidian_note = ? WHERE id = ?", (path, sol_id))
        if action == "created":
            stats["solutions_created"] += 1
        else:
            stats["solutions_updated"] += 1

    conn.commit()
    return stats


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def archive(args):
    """Main archival logic."""
    map_results, enriched_links, raw_data = load_inputs(args)

    # Build enriched-links index by URL for fast lookup
    enriched_by_url = {}
    if enriched_links:
        for link in enriched_links:
            enriched_by_url[link.get("url", "")] = link

    # Extract raw messages list
    raw_messages = []
    chat_id = None
    total_messages = 0
    unique_senders = 0
    if raw_data:
        raw_messages = raw_data.get("messages", [])
        chat_id = raw_data.get("chat_id")
        total_messages = raw_data.get("total_messages", len(raw_messages))
        unique_senders = raw_data.get("unique_senders", 0)

    # Setup DB
    conn = setup_db(args.db)

    try:
        # 1. Upsert digest
        digest_id = upsert_digest(
            conn, args.chat_name, chat_id, args.date_range,
            total_messages, unique_senders, args.html_digest,
        )

        # Collect all clusters' data
        clusters = map_results if isinstance(map_results, list) else [map_results]
        # Handle the case where map_results is a single dict with cluster data
        all_topics = []
        all_links = []
        all_solutions = []
        for cluster in clusters:
            all_topics.extend(cluster.get("topics", []))
            all_links.extend(cluster.get("annotated_links", []))
            all_solutions.extend(cluster.get("solutions", []))

        # 2. Archive links
        links_new = 0
        links_enriched = 0
        touched_link_ids = set()
        for link_data in all_links:
            url = link_data.get("url", "")
            if not url:
                continue

            # Prefer enriched data if available
            enriched = enriched_by_url.get(url, {})
            title = enriched.get("title") or link_data.get("name", "")
            annotation = enriched.get("annotation") or link_data.get("description", "")
            category = enriched.get("category") or link_data.get("category", "")
            total_reactions = enriched.get("total_reactions") or link_data.get("total_reactions", 0)
            reactions_detail = enriched.get("reactions_detail", "")

            # Check if link existed before
            existed = conn.execute("SELECT 1 FROM links WHERE url = ?", (url,)).fetchone()
            link_id = upsert_link(conn, url, title, annotation, category)
            touched_link_ids.add(link_id)
            if existed:
                links_enriched += 1
            else:
                links_new += 1

            # Find message mentions from raw data
            mentions = find_message_ids_for_url(url, raw_messages)
            if mentions:
                for msg_id, sender, reactions, snippet in mentions:
                    tg_url = build_telegram_url(chat_id, msg_id)
                    insert_link_mention(
                        conn, link_id, digest_id, msg_id, tg_url,
                        sender, reactions,
                        link_data.get("engagement_score", 0.0),
                        reactions_detail, snippet,
                    )
            else:
                # No raw message match — insert a single mention from MAP data
                insert_link_mention(
                    conn, link_id, digest_id, None, None,
                    link_data.get("sender", ""),
                    total_reactions,
                    link_data.get("engagement_score", 0.0),
                    reactions_detail, "",
                )

        # 3. Archive solutions
        solutions_count = 0
        touched_solution_ids = set()
        for sol in all_solutions:
            problem = sol.get("problem", "")
            approach = sol.get("approach", "")
            if not problem or not approach:
                continue

            sol_id = upsert_solution(
                conn, problem, approach,
                result=sol.get("result"),
                author=sol.get("author"),
                tools=sol.get("tools"),
                sol_type=sol.get("type", "solution"),
            )
            touched_solution_ids.add(sol_id)

            # Find message mentions
            keywords = sol.get("tools", []) + [problem.split()[0]] if problem else []
            mentions = find_message_ids_for_solution(
                sol.get("author", ""), keywords, raw_messages
            )
            if mentions:
                for msg_id, eng in mentions:
                    tg_url = build_telegram_url(chat_id, msg_id)
                    insert_solution_mention(
                        conn, sol_id, digest_id, msg_id, tg_url, eng
                    )
            else:
                insert_solution_mention(
                    conn, sol_id, digest_id,
                    engagement_score=sol.get("engagement_score", 0.0),
                )
            solutions_count += 1

        # 4. Archive insights from topics
        insights_count, touched_insight_ids = insert_insights(conn, all_topics, digest_id)

        conn.commit()

        # 4.5 Generate semantic embeddings if requested. Failure should not roll
        # back the core archive, because FTS + provenance still provide value.
        embedding_stats = None
        embedding_error = None
        if args.embed:
            try:
                embedding_stats = generate_embeddings(
                    conn,
                    args,
                    touched_link_ids,
                    touched_solution_ids,
                    touched_insight_ids,
                )
            except Exception as exc:
                embedding_error = str(exc)

        # 5. Create Obsidian notes (unless --no-obsidian)
        obsidian_stats = {"links_created": 0, "links_updated": 0,
                          "solutions_created": 0, "solutions_updated": 0}
        if not args.no_obsidian:
            obsidian_stats = create_obsidian_notes(
                conn, args.vault, args.chat_name, args.date_range,
                chat_id, args.html_digest,
            )

        # Report
        total_links = links_new + links_enriched
        print(f"Archived {total_links} links ({links_new} new, {links_enriched} enriched), "
              f"{solutions_count} solutions, {insights_count} insights")
        if not args.no_obsidian:
            obs_total = (obsidian_stats["links_created"] + obsidian_stats["links_updated"]
                         + obsidian_stats["solutions_created"] + obsidian_stats["solutions_updated"])
            print(f"Obsidian: {obsidian_stats['links_created']} link notes created, "
                  f"{obsidian_stats['links_updated']} updated, "
                  f"{obsidian_stats['solutions_created']} solution notes created, "
                  f"{obsidian_stats['solutions_updated']} updated "
                  f"({obs_total} total)")
        if args.embed:
            if embedding_stats:
                print(
                    f"Embeddings: {embedding_stats['generated']} generated, "
                    f"{embedding_stats['reused']} reused, "
                    f"{embedding_stats['up_to_date']} up-to-date "
                    f"({embedding_stats['total']} total, model={embedding_stats['model']})"
                )
            if embedding_error:
                print(f"WARNING: Embeddings skipped: {embedding_error}", file=sys.stderr)
        print(f"DB: {args.db}")

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Archive tg-digest knowledge to SQLite + Obsidian"
    )
    parser.add_argument(
        "--map-results",
        help="Path to map-results JSON (combined MAP phase output)",
    )
    parser.add_argument(
        "--enriched-links",
        help="Path to enriched links JSON (optional, from ENRICH phase)",
    )
    parser.add_argument(
        "--raw-messages",
        help="Path to raw messages JSON (optional but recommended for provenance)",
    )
    parser.add_argument(
        "--html-digest",
        help="Path to HTML digest file (optional, may not exist yet)",
    )
    parser.add_argument("--chat-name", help="Chat display name")
    parser.add_argument("--date-range", help="Date range string")
    parser.add_argument(
        "--db",
        default=os.path.expanduser("~/Downloads/Проекты/tg-digest/digest.db"),
        help="SQLite database path",
    )
    parser.add_argument(
        "--vault",
        default=os.path.expanduser("~/Documents/Obsidian Vault"),
        help="Obsidian vault root path",
    )
    parser.add_argument(
        "--no-obsidian", action="store_true",
        help="Skip Obsidian note creation",
    )
    parser.add_argument(
        "--embed", action="store_true",
        help="Generate semantic embeddings via an OpenAI-compatible embeddings API",
    )
    parser.add_argument(
        "--embed-existing", action="store_true",
        help="Backfill embeddings for all canonical entities already stored in the database",
    )
    parser.add_argument(
        "--embedding-model",
        help="Embedding model name (default: EMBEDDING_MODEL or text-embedding-3-small)",
    )
    parser.add_argument(
        "--embedding-base-url",
        help="Embedding API base URL or full /embeddings endpoint",
    )
    parser.add_argument(
        "--embedding-api-key",
        help="Embedding API key (optional; env vars are preferred)",
    )
    parser.add_argument(
        "--embedding-dimensions",
        type=int,
        help="Optional dimensions override for compatible embedding models",
    )
    parser.add_argument(
        "--embedding-timeout-seconds",
        type=float,
        help="Embedding API timeout in seconds",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be done without writing",
    )
    args = parser.parse_args()

    if args.embed_existing and not args.embed:
        parser.error("--embed-existing requires --embed")

    if not args.embed_existing:
        missing = []
        if not args.map_results:
            missing.append("--map-results")
        if not args.chat_name:
            missing.append("--chat-name")
        if not args.date_range:
            missing.append("--date-range")
        if missing:
            parser.error(
                f"{', '.join(missing)} required unless --embed-existing is used"
            )

    if args.dry_run:
        if args.embed_existing:
            conn = setup_db(args.db)
            try:
                link_ids, solution_ids, insight_ids = collect_all_entity_ids(conn)
            finally:
                conn.close()
            print(
                f"[DRY RUN] Would backfill embeddings for "
                f"links={len(link_ids)}, solutions={len(solution_ids)}, insights={len(insight_ids)}"
            )
            print(f"[DRY RUN] DB: {args.db}")
            config = get_embedding_config(args)
            print(
                f"[DRY RUN] Embeddings enabled: model={config['model']} "
                f"endpoint={normalize_embedding_endpoint(config['endpoint'])}"
            )
            return

        map_results, enriched_links, raw_data = load_inputs(args)
        clusters = map_results if isinstance(map_results, list) else [map_results]
        total_links = sum(len(c.get("annotated_links", [])) for c in clusters)
        total_solutions = sum(len(c.get("solutions", [])) for c in clusters)
        total_insights = sum(
            len(fact)
            for c in clusters
            for t in c.get("topics", [])
            for fact in [t.get("key_facts", [])]
        )
        print(f"[DRY RUN] Would archive: {total_links} links, "
              f"{total_solutions} solutions, {total_insights} insights")
        print(f"[DRY RUN] DB: {args.db}")
        if not args.no_obsidian:
            print(f"[DRY RUN] Obsidian vault: {args.vault}")
        if args.embed:
            config = get_embedding_config(args)
            print(
                f"[DRY RUN] Embeddings enabled: model={config['model']} "
                f"endpoint={normalize_embedding_endpoint(config['endpoint'])}"
            )
        return

    if args.embed_existing:
        backfill_embeddings(args)
        return

    archive(args)


if __name__ == "__main__":
    main()
