#!/usr/bin/env python3
"""
Semantic search over tg-digest SQLite data.

Uses the same OpenAI-compatible embeddings config as archive.py and searches
the local `embeddings` table with cosine similarity. This is the retrieval
layer that a Telegram bot can call before formatting an answer.
"""

import argparse
import json
import math
import sqlite3
from pathlib import Path

from archive import EmbeddingClient, get_embedding_config, normalize_embedding_endpoint


DEFAULT_ENTITY_TYPES = ("link", "solution", "insight")


def cosine_similarity(left, right):
    """Compute cosine similarity between two vectors."""
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def parse_entity_types(raw_value):
    """Normalize the --types argument."""
    if not raw_value:
        return DEFAULT_ENTITY_TYPES
    items = []
    for item in raw_value.split(","):
        value = item.strip().lower()
        if value:
            items.append(value)
    return tuple(items or DEFAULT_ENTITY_TYPES)


def placeholder_sql(items):
    """Create ?,?,?... placeholders for IN (...) queries."""
    return ",".join("?" for _ in items)


def load_embedding_rows(conn, model, entity_types):
    """Load stored vectors for the requested entity types."""
    rows = conn.execute(
        f"""SELECT entity_type, entity_id, embedding_json
            FROM embeddings
            WHERE model = ?
              AND entity_type IN ({placeholder_sql(entity_types)})""",
        (model, *entity_types),
    ).fetchall()
    parsed = []
    for entity_type, entity_id, embedding_json in rows:
        parsed.append((entity_type, entity_id, json.loads(embedding_json)))
    return parsed


def fetch_link_result(conn, entity_id):
    """Fetch display data for a link result."""
    row = conn.execute(
        """SELECT id, title, url, annotation, category, obsidian_note
           FROM links
           WHERE id = ?""",
        (entity_id,),
    ).fetchone()
    if not row:
        return None

    sources = [
        value[0]
        for value in conn.execute(
            """SELECT telegram_url
               FROM link_mentions
               WHERE link_id = ? AND telegram_url IS NOT NULL
               ORDER BY id DESC
               LIMIT 3""",
            (entity_id,),
        ).fetchall()
    ]
    return {
        "entity_type": "link",
        "entity_id": row[0],
        "title": row[1] or row[2] or f"Link {row[0]}",
        "url": row[2],
        "summary": row[3] or "",
        "category": row[4] or "",
        "obsidian_note": row[5],
        "sources": sources,
    }


def fetch_solution_result(conn, entity_id):
    """Fetch display data for a solution result."""
    row = conn.execute(
        """SELECT id, problem, approach, result, author, type, obsidian_note
           FROM solutions
           WHERE id = ?""",
        (entity_id,),
    ).fetchone()
    if not row:
        return None

    sources = [
        value[0]
        for value in conn.execute(
            """SELECT telegram_url
               FROM solution_mentions
               WHERE solution_id = ? AND telegram_url IS NOT NULL
               ORDER BY id DESC
               LIMIT 3""",
            (entity_id,),
        ).fetchall()
    ]
    summary = row[2] or ""
    if row[3]:
        summary = f"{summary} Result: {row[3]}".strip()
    return {
        "entity_type": "solution",
        "entity_id": row[0],
        "title": row[1],
        "url": None,
        "summary": summary,
        "category": row[5] or "solution",
        "obsidian_note": row[6],
        "sources": sources,
        "author": row[4] or "",
    }


def fetch_insight_result(conn, entity_id):
    """Fetch display data for an insight result."""
    row = conn.execute(
        """SELECT i.id, i.topic_title, i.text, i.participants_json, d.chat_name, d.html_path
           FROM insights i
           LEFT JOIN digests d ON d.id = i.digest_id
           WHERE i.id = ?""",
        (entity_id,),
    ).fetchone()
    if not row:
        return None

    participants = json.loads(row[3]) if row[3] else []
    source_label = row[4] or row[5]
    return {
        "entity_type": "insight",
        "entity_id": row[0],
        "title": row[1] or f"Insight {row[0]}",
        "url": None,
        "summary": row[2] or "",
        "category": "insight",
        "obsidian_note": None,
        "sources": [source_label] if source_label else [],
        "participants": participants,
    }


def fetch_result_record(conn, entity_type, entity_id):
    """Dispatch to the proper entity fetcher."""
    if entity_type == "link":
        return fetch_link_result(conn, entity_id)
    if entity_type == "solution":
        return fetch_solution_result(conn, entity_id)
    if entity_type == "insight":
        return fetch_insight_result(conn, entity_id)
    return None


def search(conn, client, query, entity_types, top_k):
    """Run semantic search over stored embeddings."""
    query_vector = client.embed(query)
    rows = load_embedding_rows(conn, client.model, entity_types)
    scored = []
    for entity_type, entity_id, embedding in rows:
        similarity = cosine_similarity(query_vector, embedding)
        scored.append((similarity, entity_type, entity_id))

    scored.sort(key=lambda item: item[0], reverse=True)
    results = []
    for similarity, entity_type, entity_id in scored[:top_k]:
        record = fetch_result_record(conn, entity_type, entity_id)
        if not record:
            continue
        record["score"] = round(similarity, 6)
        results.append(record)
    return results


def format_result_text(result):
    """Format one result for CLI output."""
    lines = [
        f"[{result['entity_type']}] {result['title']}",
        f"score: {result['score']}",
    ]
    if result.get("category"):
        lines.append(f"category: {result['category']}")
    if result.get("url"):
        lines.append(f"url: {result['url']}")
    if result.get("author"):
        lines.append(f"author: {result['author']}")
    if result.get("summary"):
        lines.append(f"summary: {result['summary']}")
    if result.get("sources"):
        lines.append("sources:")
        lines.extend(f"  - {value}" for value in result["sources"])
    if result.get("obsidian_note"):
        lines.append(f"obsidian_note: {result['obsidian_note']}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Semantic search over tg-digest digest.db"
    )
    parser.add_argument("query", help="Natural-language query")
    parser.add_argument(
        "--db",
        default=str(Path.home() / "Downloads" / "Проекты" / "tg-digest" / "digest.db"),
        help="SQLite database path",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Number of results to return",
    )
    parser.add_argument(
        "--types",
        help="Comma-separated entity types: link,solution,insight",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON instead of text",
    )
    parser.add_argument(
        "--embedding-model",
        help="Embedding model name override",
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
    args = parser.parse_args()

    config = get_embedding_config(args)
    endpoint = normalize_embedding_endpoint(config["endpoint"])
    if endpoint.startswith("https://api.openai.com") and not config["api_key"]:
        parser.error(
            "semantic search needs EMBEDDING_API_KEY or OPENAI_API_KEY for the default OpenAI endpoint"
        )

    client = EmbeddingClient(
        model=config["model"],
        endpoint=config["endpoint"],
        api_key=config["api_key"],
        timeout_seconds=config["timeout_seconds"],
        dimensions=config["dimensions"],
    )
    entity_types = parse_entity_types(args.types)
    conn = sqlite3.connect(args.db)
    try:
        results = search(conn, client, args.query, entity_types, args.top_k)
    finally:
        conn.close()

    if args.json:
        print(json.dumps({
            "query": args.query,
            "model": client.model,
            "results": results,
        }, ensure_ascii=False, indent=2))
        return

    print(f"query: {args.query}")
    print(f"model: {client.model}")
    print(f"results: {len(results)}")
    print()
    for index, result in enumerate(results, start=1):
        print(f"{index}. {format_result_text(result)}")
        if index != len(results):
            print()


if __name__ == "__main__":
    main()
