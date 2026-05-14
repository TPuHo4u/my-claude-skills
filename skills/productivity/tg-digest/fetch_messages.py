#!/usr/bin/env python3
"""
Fetch Telegram messages with full metadata via Telethon.
Used by tg-digest skill instead of MCP for richer data (reactions, sender, dates).

Usage:
  python3 fetch_messages.py --list-dialogs
  python3 fetch_messages.py --chat "вайбкодеры" --unread --output /tmp/tg-digest-raw.json
  python3 fetch_messages.py --chat-id -1001187714594 --days 3 --output /tmp/tg-digest-raw.json
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient
try:
    from telethon.tl.types import MessageReactionEmoji, MessageReactionCustomEmoji
except ImportError:
    MessageReactionEmoji = None
    MessageReactionCustomEmoji = None

# --- Config ---
ENV_PATH = Path.home() / "Downloads" / "Project" / "Telegram_news" / ".env"
SESSION_PATHS = [
    str(Path.home() / ".local" / "state" / "mcp-telegram" / "mcp_telegram_session"),
    str(Path.home() / "Downloads" / "Project" / "Telegram_news" / "telegram_digest"),
]


def load_env(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def get_client() -> TelegramClient:
    env = load_env(ENV_PATH)
    api_id = int(os.environ.get("TELEGRAM_API_ID", env.get("TELEGRAM_API_ID", "")))
    api_hash = os.environ.get("TELEGRAM_API_HASH", env.get("TELEGRAM_API_HASH", ""))
    if not api_id or not api_hash:
        print("Error: TELEGRAM_API_ID / TELEGRAM_API_HASH not found", file=sys.stderr)
        sys.exit(1)
    # Try each session path, use first that exists
    for sp in SESSION_PATHS:
        if Path(sp + ".session").exists():
            return TelegramClient(sp, api_id, api_hash)
    # Fallback to first path
    return TelegramClient(SESSION_PATHS[0], api_id, api_hash)


def reaction_to_str(r) -> str:
    if MessageReactionEmoji and isinstance(r, MessageReactionEmoji):
        return r.emoticon
    if MessageReactionCustomEmoji and isinstance(r, MessageReactionCustomEmoji):
        return f"custom:{r.document_id}"
    # Fallback for older telethon versions
    if hasattr(r, "emoticon"):
        return r.emoticon
    if hasattr(r, "document_id"):
        return f"custom:{r.document_id}"
    return str(r)


def serialize_message(msg) -> dict:
    """Convert a Telethon Message to a JSON-serializable dict."""
    # Sender
    sender_name = ""
    if msg.sender:
        if hasattr(msg.sender, "first_name"):
            parts = [msg.sender.first_name or ""]
            if msg.sender.last_name:
                parts.append(msg.sender.last_name)
            sender_name = " ".join(parts).strip()
        elif hasattr(msg.sender, "title"):
            sender_name = msg.sender.title or ""

    # Reactions
    reactions = []
    if msg.reactions and msg.reactions.results:
        for r in msg.reactions.results:
            reactions.append({
                "emoji": reaction_to_str(r.reaction),
                "count": r.count,
            })

    # Total reaction count for sorting
    total_reactions = sum(r["count"] for r in reactions)

    # Reply
    reply_to_msg_id = None
    if msg.reply_to:
        reply_to_msg_id = getattr(msg.reply_to, "reply_to_msg_id", None)

    # Forwards
    forward_count = 0
    if msg.forwards:
        forward_count = msg.forwards

    return {
        "id": msg.id,
        "sender": sender_name,
        "text": msg.text or "",
        "date": msg.date.isoformat() if msg.date else "",
        "reply_to_msg_id": reply_to_msg_id,
        "reactions": reactions,
        "total_reactions": total_reactions,
        "forward_count": forward_count,
        "views": msg.views or 0,
    }


async def list_dialogs(client: TelegramClient):
    dialogs = await client.get_dialogs()
    result = []
    for d in dialogs:
        result.append({
            "name": d.name,
            "id": d.id,
            "unread": d.unread_count,
            "type": "channel" if d.is_channel else ("group" if d.is_group else "user"),
        })
    print(json.dumps(result, ensure_ascii=False, indent=2))


async def fetch_messages(
    client: TelegramClient,
    chat_id: int = None,
    chat_name: str = None,
    unread: bool = False,
    days: int = None,
    limit: int = 3000,
    output: str = None,
):
    # Resolve chat
    if chat_id:
        entity = await client.get_entity(chat_id)
    elif chat_name:
        dialogs = await client.get_dialogs()
        entity = None
        for d in dialogs:
            if chat_name.lower() in d.name.lower():
                entity = d.entity
                break
        if not entity:
            print(f"Error: chat '{chat_name}' not found", file=sys.stderr)
            sys.exit(1)
    else:
        print("Error: specify --chat or --chat-id", file=sys.stderr)
        sys.exit(1)

    # Determine min_date
    min_date = None
    if days:
        min_date = datetime.now(timezone.utc) - timedelta(days=days)

    # Determine effective limit for unread
    effective_limit = limit
    if unread:
        dialogs = await client.get_dialogs()
        for d in dialogs:
            if d.entity.id == entity.id or (hasattr(entity, 'id') and d.id == chat_id):
                effective_limit = min(limit, d.unread_count) if d.unread_count > 0 else limit
                break

    # Fetch messages
    messages = []
    async for msg in client.iter_messages(entity, limit=effective_limit, offset_date=None, reverse=False):
        if not msg.text:
            continue
        if min_date and msg.date and msg.date < min_date:
            break
        messages.append(serialize_message(msg))

    # Reverse to chronological order (oldest first)
    messages.reverse()

    # Build reply context: for each reply, inline the original text
    msg_by_id = {m["id"]: m for m in messages}
    for m in messages:
        if m["reply_to_msg_id"] and m["reply_to_msg_id"] in msg_by_id:
            orig = msg_by_id[m["reply_to_msg_id"]]
            m["reply_to_text"] = orig["text"][:200]
            m["reply_to_sender"] = orig["sender"]
        else:
            m["reply_to_text"] = None
            m["reply_to_sender"] = None

    # Calculate engagement score
    reply_counts = {}
    for m in messages:
        if m["reply_to_msg_id"]:
            reply_counts[m["reply_to_msg_id"]] = reply_counts.get(m["reply_to_msg_id"], 0) + 1

    for m in messages:
        replies = reply_counts.get(m["id"], 0)
        m["engagement_score"] = round(
            replies * 2.0 + m["total_reactions"] * 1.5 + m["forward_count"] * 1.0 + m["views"] * 0.001,
            2,
        )

    # Get chat name
    resolved_name = getattr(entity, 'title', getattr(entity, 'first_name', str(chat_id or chat_name)))

    # Unique senders
    senders = set(m["sender"] for m in messages if m["sender"])

    result = {
        "chat_name": resolved_name,
        "chat_id": chat_id or entity.id,
        "total_messages": len(messages),
        "unique_senders": len(senders),
        "top_senders": sorted(
            [(s, sum(1 for m in messages if m["sender"] == s)) for s in senders],
            key=lambda x: -x[1],
        )[:10],
        "date_range": {
            "from": messages[0]["date"] if messages else "",
            "to": messages[-1]["date"] if messages else "",
        },
        "messages": messages,
    }

    out = json.dumps(result, ensure_ascii=False, indent=2)
    if output:
        Path(output).write_text(out, encoding="utf-8")
        # Print summary to stdout
        print(json.dumps({
            "status": "ok",
            "chat_name": result["chat_name"],
            "total_messages": result["total_messages"],
            "unique_senders": result["unique_senders"],
            "top_senders": result["top_senders"][:5],
            "date_range": result["date_range"],
            "output_file": output,
        }, ensure_ascii=False, indent=2))
    else:
        print(out)


async def main():
    parser = argparse.ArgumentParser(description="Fetch Telegram messages via Telethon")
    parser.add_argument("--list-dialogs", action="store_true", help="List available dialogs")
    parser.add_argument("--chat", type=str, help="Chat name (partial match)")
    parser.add_argument("--chat-id", type=int, help="Chat ID")
    parser.add_argument("--unread", action="store_true", help="Fetch unread messages only")
    parser.add_argument("--days", type=int, help="Fetch messages from last N days")
    parser.add_argument("--limit", type=int, default=3000, help="Max messages to fetch")
    parser.add_argument("--output", type=str, help="Output file path")
    args = parser.parse_args()

    client = get_client()
    await client.connect()
    if not await client.is_user_authorized():
        print("Error: Telethon session not authorized. Run the script interactively first to log in.", file=sys.stderr)
        sys.exit(1)

    try:
        if args.list_dialogs:
            await list_dialogs(client)
        else:
            await fetch_messages(
                client,
                chat_id=args.chat_id,
                chat_name=args.chat,
                unread=args.unread,
                days=args.days,
                limit=args.limit,
                output=args.output,
            )
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
