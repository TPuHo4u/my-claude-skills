#!/usr/bin/env python3
"""
Telegram bot for answering questions from digest.db + Obsidian vault.

The bot combines:
1. semantic retrieval from digest.db embeddings
2. direct lexical retrieval from the Obsidian vault
3. optional LLM answer synthesis on top of retrieved context
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from obsidian_search import search_notes
from archive import env_first
from semantic_search import (
    EmbeddingClient,
    fetch_result_record,
    get_embedding_config,
    load_embedding_rows,
    normalize_embedding_endpoint,
    cosine_similarity,
    parse_entity_types,
)


def telegram_api(token, method, payload):
    """Call Telegram Bot API."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram API HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Telegram API error: {exc.reason}") from exc

    if not body.get("ok"):
        raise RuntimeError(f"Telegram API returned error: {body}")
    return body["result"]


def get_updates(token, offset=None, timeout=30):
    """Fetch bot updates using long polling."""
    payload = {"timeout": timeout}
    if offset is not None:
        payload["offset"] = offset
    return telegram_api(token, "getUpdates", payload)


def send_message(token, chat_id, text):
    """Send a plain-text Telegram message."""
    return telegram_api(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    })


def log(message):
    """Write a line-oriented runtime log."""
    print(f"[knowledge-bot] {message}", flush=True)


def is_get_updates_conflict(exc):
    """Detect Telegram long-poll conflicts from duplicate consumers."""
    text = str(exc)
    return "HTTP 409" in text or "terminated by other getUpdates request" in text


def normalize_chat_endpoint(base_url: str) -> str:
    """Accept either an API base URL or a full chat-completions endpoint."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return "https://api.openai.com/v1/chat/completions"
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def get_llm_config(args):
    """Build runtime LLM config from CLI flags and environment."""
    endpoint = args.llm_base_url or env_first(
        "LLM_BASE_URL",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        default="https://api.openai.com/v1",
    )
    api_key = args.llm_api_key or env_first(
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "EMBEDDING_API_KEY",
    )
    model = args.llm_model or env_first(
        "LLM_MODEL",
        "OPENAI_LLM_MODEL",
        default="gpt-4.1-mini",
    )
    timeout_seconds = float(
        args.llm_timeout_seconds
        if args.llm_timeout_seconds is not None
        else env_first("LLM_TIMEOUT_SECONDS", default="45")
    )
    temperature = float(
        args.llm_temperature
        if args.llm_temperature is not None
        else env_first("LLM_TEMPERATURE", default="0.2")
    )
    max_tokens = int(
        args.llm_max_tokens
        if args.llm_max_tokens is not None
        else env_first("LLM_MAX_TOKENS", default="500")
    )
    return {
        "endpoint": endpoint,
        "api_key": api_key,
        "model": model,
        "timeout_seconds": timeout_seconds,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }


class ChatClient:
    """Minimal OpenAI-compatible chat-completions client."""

    def __init__(self, model: str, endpoint: str, api_key=None, timeout_seconds=45.0,
                 temperature=0.2, max_tokens=500):
        self.model = model
        self.endpoint = normalize_chat_endpoint(endpoint)
        self.api_key = api_key or ""
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self.max_tokens = max_tokens

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "tg-digest-knowledge-bot/1.0",
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
            raise RuntimeError(f"LLM request failed with HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM request failed: {exc.reason}") from exc

        try:
            return body["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected LLM response: {body}") from exc


def semantic_results(conn, client, query, entity_types, top_k):
    """Run semantic retrieval and format top results."""
    query_vector = client.embed(query)
    scored = []
    for entity_type, entity_id, embedding in load_embedding_rows(conn, client.model, entity_types):
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


def truncate(text, limit=280):
    """Trim long strings for Telegram replies."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def format_semantic_item(item):
    """Format one semantic result for a Telegram reply."""
    parts = [f"- [{item['entity_type']}] {item['title']}"]
    if item.get("url"):
        parts.append(f"  {item['url']}")
    if item.get("summary"):
        parts.append(f"  {truncate(item['summary'], 220)}")
    if item.get("sources"):
        parts.append(f"  source: {item['sources'][0]}")
    return "\n".join(parts)


def format_obsidian_item(item):
    """Format one Obsidian result for a Telegram reply."""
    parts = [f"- [obsidian] {item['title']}"]
    parts.append(f"  {item['path']}")
    if item.get("snippet"):
        parts.append(f"  {truncate(item['snippet'], 220)}")
    return "\n".join(parts)


def build_context_payload(question, semantic, obsidian):
    """Create compact structured context for LLM synthesis."""
    semantic_payload = []
    for item in semantic[:6]:
        semantic_payload.append({
            "type": item.get("entity_type"),
            "title": item.get("title"),
            "score": item.get("score"),
            "category": item.get("category"),
            "url": item.get("url"),
            "summary": truncate(item.get("summary", ""), 500),
            "sources": item.get("sources", [])[:3],
            "obsidian_note": item.get("obsidian_note"),
        })

    obsidian_payload = []
    for item in obsidian[:5]:
        obsidian_payload.append({
            "title": item.get("title"),
            "path": item.get("path"),
            "type": item.get("type"),
            "topics": item.get("topics", []),
            "snippet": truncate(item.get("snippet", ""), 400),
        })

    return {
        "question": question,
        "semantic_results": semantic_payload,
        "obsidian_results": obsidian_payload,
    }


def build_llm_prompts(question, semantic, obsidian):
    """Build prompts for concise retrieval-grounded answering."""
    system_prompt = (
        "Ты помощник по личной базе знаний. "
        "Отвечай только по предоставленному контексту из SQLite и Obsidian. "
        "Не выдумывай инструменты, заметки или факты, которых нет в контексте. "
        "Пиши по-русски, коротко и по делу. "
        "Если в контексте есть явные кандидаты, предложи 2-4 лучших. "
        "Если информации мало, честно скажи это. "
        "Если уместно, в конце добавь блок 'Где смотреть' со ссылками или путями к заметкам. "
        "Не упоминай cosine similarity, embeddings, retrieval или внутреннюю механику."
    )
    user_prompt = json.dumps(
        build_context_payload(question, semantic, obsidian),
        ensure_ascii=False,
        indent=2,
    )
    return system_prompt, user_prompt


def build_llm_reply(chat_client, question, semantic, obsidian):
    """Ask the LLM to synthesize a user-facing answer from retrieved context."""
    system_prompt, user_prompt = build_llm_prompts(question, semantic, obsidian)
    return chat_client.complete(system_prompt, user_prompt)


def build_reply(question, semantic, obsidian):
    """Compose a deterministic answer from retrieved context."""
    lines = [f"Вопрос: {question}", ""]

    if semantic:
        lines.append("Нашёл в базе знаний:")
        for item in semantic[:4]:
            lines.append(format_semantic_item(item))
        lines.append("")

    if obsidian:
        lines.append("Нашёл прямо в Obsidian:")
        for item in obsidian[:3]:
            lines.append(format_obsidian_item(item))
        lines.append("")

    if not semantic and not obsidian:
        lines.append("Ничего релевантного не нашёл в digest.db и Obsidian.")
    else:
        lines.append("Если хочешь, могу сузить ответ: только tools, только frameworks, только Obsidian или только Telegram knowledge.")

    return "\n".join(lines).strip()


def answer_query(conn, client, vault_path, question, top_k, entity_types, obsidian_top_k,
                 answer_with_llm=False, chat_client=None):
    """Run combined retrieval for one user question."""
    semantic = semantic_results(conn, client, question, entity_types, top_k)
    obsidian = search_notes(vault_path, question, top_k=obsidian_top_k)
    if answer_with_llm and chat_client:
        try:
            return build_llm_reply(chat_client, question, semantic, obsidian)
        except Exception as exc:
            fallback = build_reply(question, semantic, obsidian)
            return f"{fallback}\n\n[LLM fallback] {exc}"
    return build_reply(question, semantic, obsidian)


def run_once(args, conn, client, chat_client=None):
    """Local test mode without Telegram polling."""
    reply = answer_query(
        conn,
        client,
        args.vault,
        args.once_query,
        args.top_k,
        parse_entity_types(args.types),
        args.obsidian_top_k,
        answer_with_llm=args.answer_with_llm,
        chat_client=chat_client,
    )
    print(reply)


def run_polling(args, conn, client, chat_client=None):
    """Main bot loop."""
    token = args.telegram_bot_token
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required for polling mode")

    offset = None
    entity_types = parse_entity_types(args.types)
    log(
        "starting polling"
        f" db={args.db}"
        f" vault={args.vault}"
        f" allowed_chat_id={args.allowed_chat_id or 'any'}"
        f" llm={'on' if args.answer_with_llm else 'off'}"
    )
    while True:
        try:
            updates = get_updates(token, offset=offset, timeout=args.poll_timeout)
        except Exception as exc:
            if is_get_updates_conflict(exc):
                log("getUpdates conflict detected; another consumer polled concurrently, retrying")
                time.sleep(max(args.poll_sleep_seconds, 2.0))
                continue
            raise
        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message") or update.get("edited_message")
            if not message:
                continue
            text = (message.get("text") or "").strip()
            if not text:
                continue

            chat_id = message["chat"]["id"]
            if args.allowed_chat_id and str(chat_id) != str(args.allowed_chat_id):
                log(f"ignored message from unauthorized chat_id={chat_id}")
                continue

            if text.startswith("/start"):
                log(f"handling /start for chat_id={chat_id}")
                send_message(
                    token,
                    chat_id,
                    "Задай вопрос по инструментам, агентам, фреймворкам или заметкам из Obsidian.",
                )
                continue

            if text.startswith("/help"):
                log(f"handling /help for chat_id={chat_id}")
                send_message(
                    token,
                    chat_id,
                    "Примеры:\n- инструмент для оркестрации AI-агентов\n- как запускать агента локально\n- что у меня есть про ситуационное лидерство",
                )
                continue

            question = text[5:].strip() if text.startswith("/ask ") else text
            log(f"answering chat_id={chat_id} question={question[:120]!r}")
            try:
                reply = answer_query(
                    conn,
                    client,
                    args.vault,
                    question,
                    args.top_k,
                    entity_types,
                    args.obsidian_top_k,
                    answer_with_llm=args.answer_with_llm,
                    chat_client=chat_client,
                )
            except Exception as exc:
                log(f"search error for chat_id={chat_id}: {exc}")
                reply = f"Ошибка при поиске: {exc}"
            send_message(token, chat_id, reply[:3900])
            log(f"sent reply to chat_id={chat_id} chars={len(reply[:3900])}")

        time.sleep(args.poll_sleep_seconds)


def main():
    parser = argparse.ArgumentParser(description="Telegram knowledge bot over digest.db + Obsidian")
    parser.add_argument(
        "--db",
        default=str(Path.home() / "Downloads" / "Проекты" / "tg-digest" / "digest.db"),
        help="SQLite database path",
    )
    parser.add_argument(
        "--vault",
        default=str(Path.home() / "Documents" / "Obsidian Vault"),
        help="Obsidian vault root path",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Semantic result count",
    )
    parser.add_argument(
        "--obsidian-top-k",
        type=int,
        default=3,
        help="Obsidian lexical result count",
    )
    parser.add_argument(
        "--types",
        help="Comma-separated entity types: link,solution,insight",
    )
    parser.add_argument(
        "--telegram-bot-token",
        default=None,
        help="Telegram bot token. Defaults to TELEGRAM_BOT_TOKEN env if omitted.",
    )
    parser.add_argument(
        "--allowed-chat-id",
        help="Optional allowlist for one chat id",
    )
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=30,
        help="Telegram long polling timeout",
    )
    parser.add_argument(
        "--poll-sleep-seconds",
        type=float,
        default=1.0,
        help="Delay between polling cycles",
    )
    parser.add_argument(
        "--once-query",
        help="Run one local query and exit without Telegram polling",
    )
    parser.add_argument(
        "--answer-with-llm",
        action="store_true",
        help="Synthesize a final answer with an OpenAI-compatible chat model",
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
    parser.add_argument(
        "--llm-model",
        help="LLM model name override",
    )
    parser.add_argument(
        "--llm-base-url",
        help="LLM API base URL or full /chat/completions endpoint",
    )
    parser.add_argument(
        "--llm-api-key",
        help="LLM API key (optional; env vars are preferred)",
    )
    parser.add_argument(
        "--llm-timeout-seconds",
        type=float,
        help="LLM API timeout in seconds",
    )
    parser.add_argument(
        "--llm-temperature",
        type=float,
        help="LLM sampling temperature",
    )
    parser.add_argument(
        "--llm-max-tokens",
        type=int,
        help="LLM max output tokens",
    )
    args = parser.parse_args()

    if not args.telegram_bot_token:
        args.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not args.allowed_chat_id:
        args.allowed_chat_id = os.getenv("TELEGRAM_ALLOWED_CHAT_ID")

    config = get_embedding_config(args)
    endpoint = normalize_embedding_endpoint(config["endpoint"])
    if endpoint.startswith("https://api.openai.com") and not config["api_key"]:
        parser.error(
            "knowledge bot needs EMBEDDING_API_KEY or OPENAI_API_KEY for the default OpenAI endpoint"
        )

    client = EmbeddingClient(
        model=config["model"],
        endpoint=config["endpoint"],
        api_key=config["api_key"],
        timeout_seconds=config["timeout_seconds"],
        dimensions=config["dimensions"],
    )

    chat_client = None
    if args.answer_with_llm:
        llm_config = get_llm_config(args)
        llm_endpoint = normalize_chat_endpoint(llm_config["endpoint"])
        if llm_endpoint.startswith("https://api.openai.com") and not llm_config["api_key"]:
            parser.error(
                "LLM answer mode needs LLM_API_KEY or OPENAI_API_KEY for the default OpenAI endpoint"
            )
        chat_client = ChatClient(
            model=llm_config["model"],
            endpoint=llm_config["endpoint"],
            api_key=llm_config["api_key"],
            timeout_seconds=llm_config["timeout_seconds"],
            temperature=llm_config["temperature"],
            max_tokens=llm_config["max_tokens"],
        )

    conn = __import__("sqlite3").connect(args.db)
    try:
        if args.once_query:
            run_once(args, conn, client, chat_client=chat_client)
            return
        if not args.allowed_chat_id:
            parser.error(
                "polling mode requires --allowed-chat-id or TELEGRAM_ALLOWED_CHAT_ID"
            )
        run_polling(args, conn, client, chat_client=chat_client)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
