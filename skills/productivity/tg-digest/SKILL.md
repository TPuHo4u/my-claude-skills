---
name: tg-digest
version: 2.0.0
description: |
  Create a digest from Telegram chat messages using MapReduce with subagents.
  Uses Telethon directly for rich metadata (reactions, sender names, dates, replies).
  Use when the user asks to "make a digest", "create a digest", "summarize chat",
  "telegram digest", "tg digest", "chat summary", "what happened in chat",
  or wants to summarize Telegram conversations.
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Grep
  - Glob
  - Task
  - AskUserQuestion
  - mcp__exa__web_search_exa
  - WebFetch
  - WebSearch
---

# Telegram Chat Digest (MapReduce with Subagents)

You are creating a digest of Telegram chat messages using a MapReduce approach with parallel subagents. This method prevents hallucinations by processing small clusters independently and then combining results.

**Architecture:** FETCH (Telethon) -> PREPARE (cluster) -> MAP (parallel subagents) -> REDUCE (main model) -> VERIFY (subagent) -> DELIVER

**Telethon script**: `~/.claude/skills/tg-digest/fetch_messages.py`

$ARGUMENTS

---

## Phase 1: SELECT - Choose chat and time range

**Goal**: Identify which chat to digest and the time range.

**Actions**:
1. Run Telethon script to list dialogs:
   ```bash
   python3 ~/.claude/skills/tg-digest/fetch_messages.py --list-dialogs
   ```
2. Present the list to the user and ask which chat to digest
3. Ask the user for the time range (e.g., "last 2 days", "since Monday", "unread", specific dates)
4. Confirm the selection before proceeding

---

## Phase 2: FETCH & PREPARE - Fetch messages and organize into clusters

**Goal**: Load all messages with full metadata and prepare them for analysis.

**Actions**:

### Step 1: Fetch via Telethon

Run the fetch script with appropriate flags:

```bash
# For unread messages:
python3 ~/.claude/skills/tg-digest/fetch_messages.py --chat "chat_name" --unread --output /tmp/tg-digest-raw.json

# For last N days:
python3 ~/.claude/skills/tg-digest/fetch_messages.py --chat "chat_name" --days 3 --output /tmp/tg-digest-raw.json

# By chat ID with limit:
python3 ~/.claude/skills/tg-digest/fetch_messages.py --chat-id -1001187714594 --limit 500 --output /tmp/tg-digest-raw.json
```

The script returns rich metadata per message:
- `id` — message ID (used to build source links)
- `sender` — display name
- `date` — ISO timestamp
- `reactions` — list of `{emoji, count}`
- `total_reactions` — sum of all reactions
- `reply_to_msg_id`, `reply_to_text`, `reply_to_sender` — reply context (auto-resolved)
- `engagement_score` — pre-calculated: replies*2.0 + reactions*1.5 + forwards*1.0 + views*0.001
- `forward_count`, `views`

Also returns aggregate stats: `top_senders`, `unique_senders`, `date_range`, `chat_id`.

### Source message links

After fetching, compute the **chat link base** for building source links:
```python
raw_chat_id = data['chat_id']  # e.g. -1001187714594 or 1187714594
# Strip -100 prefix for supergroups/channels
str_id = str(abs(raw_chat_id))
if str_id.startswith('100') and len(str_id) > 10:
    str_id = str_id[3:]
chat_link_base = f"https://t.me/c/{str_id}"
# Save for later phases
```
Each message link is then `{chat_link_base}/{msg_id}`.
Save `chat_link_base` to `/tmp/tg-digest-meta.json` alongside other metadata for use in MAP, REDUCE, and DELIVER phases.

### Step 2: Cluster messages

1. Read `/tmp/tg-digest-raw.json`
2. **Calculate cluster size dynamically**: `cluster_size = total_messages / TARGET_CLUSTERS` where `TARGET_CLUSTERS = 15`. This ensures 10-15 clusters regardless of chat size. Do NOT hardcode 20-25 messages per cluster — that creates too many subagents for large chats.
3. Keep reply chains together where possible, but prioritize hitting the target cluster count
4. Save clusters to `/tmp/tg-digest-clusters.json`

**Cluster format** (save as JSON):
```json
{
  "chat_name": "...",
  "time_range": "...",
  "total_messages": 123,
  "unique_senders": 45,
  "top_senders": [["Name", 30], ["Name2", 25]],
  "clusters": [
    {
      "id": 1,
      "messages": [
        {
          "id": 123,
          "sender": "Username",
          "text": "message text",
          "date": "2025-01-15T10:30:00",
          "reply_to_text": "original message text if reply",
          "reactions": [{"emoji": "👍", "count": 5}],
          "engagement_score": 4.5
        }
      ]
    }
  ]
}
```

### Step 3: Extract all links with engagement and context

Extract all URLs from messages using **regex** (`https?://[^\s\)\]"<>]+`), NOT simple `text.split()`. This is critical because many Telegram messages contain markdown links like `[text](url)` that won't be found by splitting on whitespace.

For each link, capture the **reactions on the message** that contained the link. Sort by `total_reactions` descending. Save to `/tmp/tg-digest-links.json`:
```json
[
  {
    "url": "https://...",
    "sender": "Name",
    "engagement_score": 12.5,
    "total_reactions": 8,
    "reactions_detail": "🔥:31, ❤:6, 👍:3",
    "context": "full message text where the link appeared"
  },
  ...
]
```

**Important**:
- **Normalize URLs before deduplication**: strip trailing `/`, `.git`, `#fragment`, `?t=NNN` (YouTube timestamps). Use this regex normalization:
  ```python
  norm = re.sub(r'\.git(#.*)?$', '', url.rstrip('/'))
  norm = re.sub(r'\?t=\d+$', '', norm)
  norm = re.sub(r'#.*$', '', norm)
  ```
- Deduplicate by normalized URL, keeping the entry with the highest `total_reactions`
- Filter out `t.me/channel_name` links (just channel references, not content)
- The `reactions_detail` string should be included in the final HTML links section

This data will be used in the MAP and REDUCE phases to generate **annotated link descriptions**.

---

## Phase 3: MAP - Parallel cluster analysis with subagents

**Goal**: Analyze each cluster independently using cheap, fast subagents.

**Actions**:

1. Launch **parallel Agent subagents** (one per cluster) with `model: "haiku"`.

2. **Subagent prompt template** (customize per cluster). CRITICAL: The prompt MUST include the exact JSON schema AND a validation instruction. Haiku tends to invent its own JSON structure if not forced:

```
You are analyzing a cluster of Telegram chat messages to extract the main topics and highlights for a digest.

Chat: {chat_name}
Period: {time_range}
Cluster {cluster_id} of {total_clusters}

Read the file /tmp/tg-digest-cluster-{cluster_id}.txt for the messages.

You extract THREE types of knowledge. Each type answers a DIFFERENT question — do NOT duplicate info across them:
- **topics** = "О чём говорили?" — discussions, opinions, consensus. Do NOT include how-to details here if they belong in solutions.
- **annotated_links** = "Что шарили?" — URL + what the resource is + who would find it useful. Only about the resource itself, not the discussion around it.
- **solutions** = "Что кто-то СДЕЛАЛ и какой результат?" — only when someone describes a concrete action they took, a pipeline they built, a workaround they found, or a configuration that worked/failed. Must have problem→approach→result structure.

DEDUPLICATION RULE: Topics, solutions, and annotated_links must NOT repeat the same information:
- If someone BUILT something (PicoClaw robot) → full details go in `solutions` (problem→approach→result). In `topics`, only mention it briefly as an argument: "Valerii showed PicoClaw robot working on RPi". Do NOT repeat the setup steps.
- If a URL was shared → description goes in `annotated_links`. In `topics`, just reference it: "Valerii shared the repo". Do NOT repeat what the link is about.
- If there was NO discussion around a solution (someone just posted "I did X, here's the result") → it's a `solution` only, NOT a topic.
- A topic requires MULTIPLE people discussing/debating/reacting. One person posting ≠ a topic.

Your task:
1. Identify the main topic(s) discussed — opinions, debates, conclusions
2. For every URL/link: annotate what it IS and who would find it useful
3. Extract solutions: when someone shares HOW they solved a problem, built something, or found a workaround
4. Flag humor, irony, or sarcasm explicitly
5. Highlight messages with high engagement (score > 3.0)
6. For each topic, collect the message IDs of the KEY messages that support it (the `[msg_id]` at the start of each line in the cluster file). These will be used to generate source links in the final digest.

CRITICAL: You MUST output EXACTLY this JSON structure. Do NOT rename fields. Do NOT add extra top-level keys. Do NOT use "main_topics" instead of "topics". The downstream pipeline will break if you change the schema.

Write a JSON file to /tmp/tg-digest-map-{cluster_id}.json with EXACTLY this structure:
{
  "cluster_id": N,
  "topics": [
    {
      "title": "Short topic title",
      "summary": "2-3 sentence summary of the DISCUSSION (opinions, debate, consensus)",
      "key_participants": ["user1", "user2"],
      "key_facts": ["fact1", "fact2"],
      "source_msg_ids": [12345, 12367, 12389],
      "links": ["url1"],
      "sentiment": "positive/negative/neutral/mixed",
      "is_ironic": false,
      "engagement_level": "high/medium/low"
    }
  ],
  "annotated_links": [
    {
      "url": "https://...",
      "name": "Short name",
      "description": "What it is, what problem it solves, why it was shared",
      "category": "tool|article|video|docs|service|repo",
      "sender": "Username",
      "engagement_score": 4.5,
      "total_reactions": 3,
      "source_msg_id": 12345
    }
  ],
  "solutions": [
    {
      "problem": "What problem was being solved (1 sentence)",
      "approach": "What the person actually did — tools, config, steps (2-3 sentences)",
      "result": "What happened — did it work? numbers, performance, outcome (1-2 sentences)",
      "author": "Username",
      "tools": ["Tool1", "Tool2"],
      "type": "solution|pipeline|workaround|case|antipattern",
      "engagement_score": 4.5,
      "source_msg_ids": [12345, 12367]
    }
  ]
}

**source_msg_ids**: Array of message IDs from the cluster file (the number in `[msg_id]` prefix). Include 2-5 most relevant messages per topic/solution — the ones where key claims, opinions, or facts originate. These are used to build `https://t.me/c/{channel_id}/{msg_id}` links in the final digest so readers can jump to the original discussion.

Types for solutions:
- solution: someone solved a problem (setup, config, integration)
- pipeline: a workflow or chain of tools (A → B → C)
- workaround: bypass for a limitation or restriction
- case: real deployment with numbers (hardware, performance, cost)
- antipattern: something that does NOT work (valuable negative knowledge)

VALIDATION: After writing the file, read it back and verify it has exactly these top-level keys: "cluster_id", "topics", "annotated_links", "solutions". If not, rewrite it.
```

3. **Prepare cluster files BEFORE launching subagents**: Save each cluster as `/tmp/tg-digest-cluster-{id}.txt` (human-readable format with `[msg_id][date] sender (eng:X): text | reactions: emoji:count`). Include the message ID at the start of each line so subagents can reference source messages. Do NOT embed message data in the prompt — it bloats the prompt and hits token limits.

4. Collect all subagent results. **Validate JSON structure** after collection:
   ```python
   # After all subagents complete, validate and normalize:
   for i in range(1, num_clusters + 1):
       with open(f'/tmp/tg-digest-map-{i}.json') as f:
           data = json.load(f)
       # Normalize if subagent used wrong field names
       if 'topics' not in data and 'main_topics' in data:
           data['topics'] = data.pop('main_topics')
       if 'annotated_links' not in data:
           data['annotated_links'] = data.get('links_and_annotations',
                                     data.get('links_with_annotations',
                                     data.get('urls_and_links',
                                     data.get('links', []))))
       # Ensure topics have required fields
       for topic in data.get('topics', []):
           topic.setdefault('title', topic.get('topic', topic.get('name', 'Unknown')))
           topic.setdefault('summary', topic.get('description', ''))
           topic.setdefault('key_participants', topic.get('participants', []))
           topic.setdefault('key_facts', [])
           topic.setdefault('engagement_level', 'medium')
       # Normalize solutions
       if 'solutions' not in data:
           data['solutions'] = data.get('practical_solutions',
                               data.get('cases',
                               data.get('workarounds', [])))
       for sol in data.get('solutions', []):
           sol.setdefault('problem', sol.get('challenge', sol.get('question', '')))
           sol.setdefault('approach', sol.get('method', sol.get('steps', '')))
           sol.setdefault('result', sol.get('outcome', ''))
           sol.setdefault('author', sol.get('user', sol.get('sender', '')))
           sol.setdefault('tools', [])
           sol.setdefault('type', 'solution')
   ```
   This normalization step is essential because Haiku subagents frequently deviate from the requested schema despite explicit instructions.

5. Save all normalized MAP results to `/tmp/tg-digest-map-results.json`

**Important**: Launch all subagents in parallel using multiple Agent tool calls in a single message with `run_in_background: true`. This is the key performance optimization.

---

## Phase 4: REDUCE - Combine into final digest

**Goal**: Synthesize all cluster analyses into a coherent, readable digest.

**Actions**:

1. Read all MAP results from `/tmp/tg-digest-map-results.json`
2. **Merge and deduplicate** topics that appear across multiple clusters
3. **Rank topics** by:
   - Engagement level (high > medium > low)
   - Number of participants
   - Number of clusters the topic spans
4. **Read `chat_link_base`** from `/tmp/tg-digest-meta.json` (saved in Phase 2). This is used to build source links for topics and solutions.

5. **Write the digest** following this structure:

```markdown
# Digest: {chat_name}
## {date_range}

### {Top Topic Title}
{2-4 sentences summarizing the discussion. Attribute key opinions to specific users. Include relevant links.}
[Источники: msg1, msg2, msg3]

### {Second Topic Title}
{...}
[Источники: msg1, msg2]

### {Third Topic Title}
{...}

---
**Stats**: {total_messages} messages from {unique_senders} participants
**Most active**: {top_3_senders}
```

**Source links**: For each topic and solution, use the `source_msg_ids` from MAP results to generate clickable links to the original messages: `{chat_link_base}/{msg_id}`. In the digest, render them as "Источники" at the end of each topic/solution section. In HTML, use `<a href="...">Источник</a>`. In Telegram, use `<a href="...">Источник</a>` tags. If a topic spans multiple clusters, collect all source_msg_ids from all contributing MAP results.

**Rules for the digest**:
- Maximum 4000 characters (excluding links section)
- 4-7 topics maximum, prioritized by engagement
- Always attribute statements to specific usernames
- Include links that were shared
- If a topic involves irony/humor, convey the actual meaning, not the literal text
- Use the language that dominates in the chat messages (if the chat is in Russian, write the digest in Russian)
- Do NOT invent details not present in the MAP results
- **MANDATORY: Every topic MUST end with a takeaway/conclusion** — "к чему пришли", "что решили", "консенсус чата". If there was no consensus, write what the main opposing positions were. Do NOT just list facts without a conclusion. Example:
  - ❌ Bad: "Mac потребляет 600-700W, GPU — до 3kW. Qwen3 235B — 25 tok/s на V100."
  - ✅ Good: "Mac потребляет 600-700W, GPU — до 3kW. Консенсус: Mac Studio оправдан для тихого 24/7 инференса с приватными данными, но по tok/s за доллар GPU выигрывают. Для старта рекомендовали Cloud.ru VPS от 140₽/мес."

### CRITICAL: Deduplication between Topics and Solutions

Topics and solutions answer DIFFERENT questions and MUST NOT duplicate each other:

- **Topic** = "О чём ОБСУЖДАЛИ?" — дискуссия, мнения, дебаты, консенсус. Упоминает кейсы как АРГУМЕНТЫ в дискуссии: "Nikolay привёл пример, что Codex решил задачу с эквайрингом". НЕ включает детали problem→approach→result.
- **Solution** = "Что кто-то СДЕЛАЛ?" — конкретное действие с результатом. НЕ повторяет контекст дискуссии.

**Anti-duplication algorithm** (run AFTER writing topics, BEFORE writing solutions):
1. For each solution from MAP results, check if its core content already appears in a topic
2. If a solution is ALREADY used as an example/argument in a topic: EXCLUDE it from the solutions section
3. Only include in solutions section things that are NOT mentioned in topics OR that have significant additional detail (problem→approach→result) beyond what the topic covers
4. When a topic uses a case as evidence, keep it brief in the topic: "**Nikolay**: Codex решил эквайринг Альфа-банка за одну сессию" — the full problem→approach→result stays ONLY in solutions section, IF it's included there
5. A solution should appear in EITHER topics OR solutions, not both

**Example of correct dedup:**
- ❌ BAD: Topic says "Rustam показал 25k строк FastAPI за 12 часов с 649 тестами через Paperclip" AND Solutions section has "[case] 25k строк FastAPI — @Rustam: Problem: нужен бэкенд. Approach: AI-команда 12 часов. Result: 25k строк, 649 тестов" → DUPLICATE
- ✅ GOOD Option A: Topic mentions "Rustam показал впечатляющий кейс с Paperclip" (brief) + Solutions has the full card with details → detail in solutions only
- ✅ GOOD Option B: Topic includes the full story as a key argument + Solutions OMITS this case → detail in topic only

**Practical rule**: If a topic already tells the story well enough, do NOT create a solution card for it. Solutions section is for cases that DIDN'T make it into topics or that have important technical details (tools, config, steps) that would bloat the topic.

### Solutions section in REDUCE

After writing the main digest topics, compile the **solutions section**:

1. Collect all `solutions` from MAP results across all clusters
2. Deduplicate by similarity (same author + same tools = likely same solution)
3. **CRITICAL: Remove solutions already covered in topics** (see anti-duplication algorithm above)
4. Sort by `engagement_score` descending
5. Group by type: `solution`, `pipeline`, `workaround`, `case`, `antipattern`
6. For each solution, write a compact card:

```markdown
### 🔧 Решения и кейсы

**[solution]** PicoClaw на Raspberry Pi — @Valerii Kovalskii
*Проблема*: Запустить AI-агента на дешёвом железе
*Подход*: RPi 4 8GB + PicoClaw + LangFuse трейсинг через RustDesk
*Результат*: Работает, настройка 3 часа. Робот фотографирует закаты по расписанию.
[Источник]({chat_link_base}/{source_msg_id})

**[case]** 4x Mac Studio для Kimi K2.5 — @Нейро Ковальский
*Конфигурация*: 4x Mac Studio 512GB (2TB) + Exo framework
*Числа*: 600-700W, запускает Kimi K2.5
[Источник]({chat_link_base}/{source_msg_id})

**[workaround]** ChatGPT Plus → Claude Code — @Securiteru
*Проблема*: Нет доступа к API в РФ
*Подход*: codex-openai-proxy (Rust) проксирует ChatGPT Plus токены
*Результат*: Работает с CLINE и Claude Code
[Источник]({chat_link_base}/{source_msg_id})
```

**Rules for solutions**:
- Only include if someone actually DID something (not just discussed)
- Must have problem→approach→result structure
- Do NOT duplicate details already in topics — topics reference solutions, not repeat them
- Antipatterns are valuable: "OpenClaw теряется на длинных задачах" saves someone hours of debugging

### Links section in REDUCE

After writing the main digest topics and solutions, compile the **annotated links section**:

1. Collect all `annotated_links` from MAP results across all clusters
2. Deduplicate by URL
3. Sort by `engagement_score` descending (highest engagement first)
4. Group into categories: `repo` (GitHub repos), `tool/service`, `article`, `docs`, `video`, `other`
5. For each link, write: **name** — 1-2 sentence description of what it is and why it's useful. Include engagement indicator (reactions count if > 0).
6. Skip internal Telegram links (`t.me/c/...`) and digest/summary links (`telegra.ph/Sammari...`)
7. Save the compiled links list as part of the draft digest

5. Save the draft digest to `/tmp/tg-digest-draft.md`

---

## Phase 4.5: ENRICH - Annotate links (chat context + web lookup)

**Goal**: For each link, generate a 1-2 sentence description combining **what people said about it in the chat** AND **what the page actually contains**. The annotation should answer: что это, для кого, чем полезно.

**Actions**:

1. Take the deduplicated links list from REDUCE (sorted by reactions)
2. For **every link** (prioritize top 20-30 by `total_reactions`, but annotate all):

   **Step A — Chat context**: Collect what participants said about this link from MAP results (`annotated_links`, quotes, summaries). This gives the "why it was shared" and community opinion.

   **Step B — Web lookup** (3-tier fallback):
   1. First try `mcp__exa__web_search_exa` with the URL or its title — Exa often has good summaries for tech content, papers, GitHub repos
   2. If Exa returns nothing useful, use `WebFetch` to visit the URL directly and read the page content (title, meta description, first paragraphs)
   3. If WebFetch also fails (paywall, 403, etc.), use `WebSearch` to search for the URL and get a snippet from search results

   **Step C — Combine**: Write annotation in Russian, **strictly 2 sentences** following this template:
   - **Sentence 1**: Что это — конкретное описание ресурса (из web lookup) + зачем поделились (из chat context)
   - **Sentence 2**: Кому полезно — конкретная целевая аудитория и практическое применение

   The "кому полезно" part is MANDATORY. Do NOT skip it. Be specific: "разработчикам AI-агентов", "тем, кто собирает локальный инференс", "фронтендерам, работающим с Claude Code" — NOT generic "всем, кто интересуется ИИ".

   Example good annotations:
   > Фреймворк для распределённого инференса LLM через несколько машин с авто-обнаружением пиров. Полезен тем, кто собирает кластер из нескольких Mac Studio или GPU-серверов для запуска больших моделей локально.

   > Прокси на Rust для использования ChatGPT Plus токенов с Claude Code/CLINE через OpenAI API. Пригодится разработчикам в РФ, у которых проблемы с прямым доступом к API.

   > Ультралёгкий AI-агент на Go (<10MB RAM), работает на Raspberry Pi и RISC-V. Для тех, кто хочет запустить агента на дешёвом железе или встроить в IoT-устройство.

   Example bad annotations (too vague, no audience):
   > ❌ Платформа в области ИИ.
   > ❌ Инструмент для работы с API. Интересный проект.
   > ❌ GitHub репозиторий с полезным кодом.

3. Save enriched links to `/tmp/tg-digest-links-enriched.json`:
```json
[
  {
    "url": "https://...",
    "title": "Short title",
    "annotation": "Sentence 1: что это + зачем поделились. Sentence 2: кому полезно (конкретная аудитория + применение)",
    "total_reactions": 84,
    "reactions_detail": "❤42 😎24 ⚡11",
    "category": "article|tool|repo|video|docs|news"
  }
]
```

**Parallelization**: Launch 5-10 web lookups in parallel using multiple tool calls in a single message. This is the key optimization — don't do them one by one.

**Important**: Do NOT skip this phase. Links without descriptions are useless in the Telegram message. Every link must have at minimum a title and a 1-sentence annotation that explains what it is.

---

## Phase 5: VERIFY - Fact-check the digest

**Goal**: Verify that the digest accurately represents the original messages.

**Actions**:

1. Launch a single Task subagent with `model: "haiku"` and `subagent_type: "general-purpose"`:

```
You are a fact-checker for a Telegram chat digest. Your job is to verify every claim in the digest against the original messages.

DIGEST:
{draft_digest}

ORIGINAL MESSAGES (clusters):
{all_clusters_data}

For each factual claim in the digest:
1. Find the source message(s) that support it
2. Mark as: CONFIRMED (found source), NO_SOURCE (no matching message), HALLUCINATION (contradicts messages), MISINTERPRETED (irony/sarcasm read literally)

Output format:
{
  "total_claims": N,
  "confirmed": N,
  "no_source": N,
  "hallucinations": N,
  "misinterpreted": N,
  "issues": [
    {
      "claim": "what the digest says",
      "type": "HALLUCINATION|NO_SOURCE|MISINTERPRETED",
      "explanation": "what actually happened",
      "suggested_fix": "corrected text"
    }
  ]
}
```

2. **If issues are found**:
   - Fix HALLUCINATION and MISINTERPRETED issues immediately
   - For NO_SOURCE: keep the claim only if it's a reasonable synthesis of multiple messages; otherwise remove
3. Present the verification results to the user
4. Save the final digest

---

## Phase 6: ARCHIVE - Store knowledge for reuse

**Goal**: Persist extracted links, solutions and insights in SQLite DB and Obsidian vault BEFORE delivery, so knowledge is safe even if Telegram send fails.

**Actions**:
1. Run archive script:
   ```bash
   python3 ~/.claude/skills/tg-digest/archive.py \
     --map-results /tmp/tg-digest-map-results.json \
     --enriched-links /tmp/tg-digest-links-enriched.json \
     --raw-messages /tmp/tg-digest-raw.json \
     --chat-name "{chat_name}" \
     --date-range "{date_range}"
   ```
   Add `--html-digest <path>` if the HTML file was already generated.
   Add `--raw-messages /tmp/tg-digest-raw-messages.json` if the raw file has a different name.
   Add `--embed` to generate semantic embeddings. Optional env vars:
   - `EMBEDDING_API_KEY` or `OPENAI_API_KEY`
   - `EMBEDDING_MODEL` (default: `text-embedding-3-small`)
   - `EMBEDDING_BASE_URL` (OpenAI-compatible base URL or full `/embeddings` endpoint)
   - `EMBEDDING_DIMENSIONS`

2. Script stores to SQLite (`~/Downloads/Проекты/tg-digest/digest.db`):
   - All links with provenance (link_mentions per digest/post)
   - All solutions with provenance
   - Key insights from topics
   - FTS5 indexes for search
   - Optional `embeddings` table for semantic search over links, solutions and insights

3. Script creates Obsidian notes (`~/Documents/Obsidian Vault/Resources/Inbox/`):
   - Links with total_reactions >= 5 or engagement_score >= 10
   - ALL solutions/cases/workarounds/antipatterns
   - Uses Resource Candidate template, deduplicates by URL in frontmatter

4. Report: "Archived X links (Y new, Z enriched), N solutions, M insights"

**After DELIVER completes** (not here): clean up `/tmp/tg-digest-*` files.

---

## Phase 7: DELIVER - Save HTML, open, and send to Telegram

**Goal**: Save the final digest as a styled HTML file, open it in the browser, and send a text version to Telegram via bot.

**Actions**:

1. **Read links data** from `/tmp/tg-digest-links-enriched.json` (prepared in Phase 4.5 ENRICH). Fall back to `/tmp/tg-digest-links.json` (Phase 2) only if the enriched file doesn't exist.
2. **Generate an HTML file** from the digest using the light theme template below. **Include a "Links" section** at the end, sorted by `total_reactions` descending. Group links by category (GitHub repos, docs, articles, tools, videos). For each link show the **actual reaction emojis and counts** from the message (e.g., `🔥31 ❤6 👍3 — 40`), NOT approximate labels like "🔥высокий". The reactions come from the `reactions_detail` field. Use `title` and `annotation` from the enriched data when available.
3. Save to a meaningful path: `~/Downloads/Проекты/tg-digest/digest-{chat_name}-{date}.html`
4. **Automatically open** the file in the default browser using Bash: `open <filepath>`
5. **Send a text version to Telegram** via Bot API (see Telegram Bot section below)
6. Show verification stats in chat: "X facts confirmed, Y without direct source, Z corrected"
7. **Clean up temporary files** in `/tmp/tg-digest-*` (AFTER both ARCHIVE and DELIVER have completed)

### Telegram Bot delivery

Send the digest as a formatted text message using the Telegram Bot API via `curl`.

**Bot config**:
- Token: `8318566506:AAErTkwhdMFD0zvbObnWXc5IGF_d9MdVI-A`
- Chat ID: `312650860`

**Send via Python `urllib`** (do NOT use `requests` — it may not be installed):
```python
import json, urllib.request

data = json.dumps({
    "chat_id": CHAT_ID,
    "parse_mode": "HTML",
    "text": message_text,
    "disable_web_page_preview": True  # ALWAYS set to True for ALL messages, not just links
}).encode('utf-8')

req = urllib.request.Request(
    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
    data=data,
    headers={"Content-Type": "application/json"}
)
resp = urllib.request.urlopen(req)
result = json.loads(resp.read())
```

**CRITICAL: Telegram HTML escaping**. In `parse_mode: "HTML"`, raw `<` and `>` in text content break parsing (HTTP 400). Always escape them:
- `<10MB` → `&lt;10MB`
- `A > B` → `A &gt; B`
- `&` → `&amp;` (if not part of entity)

Only `<b>`, `<i>`, `<a>`, `<code>`, `<blockquote>` are allowed HTML tags.

**Text format template** (use Telegram HTML: `<b>`, `<i>`, `<code>`, `<a>`, `<blockquote>`):

```
<b>Дайджест: {chat_name}</b>
{date_range} · ~{total_messages} сообщений

— — —

{emoji} <b>{topic_title}</b>

{summary}

<blockquote>«{quote}»</blockquote>

<a href="{url}">{link_title}</a> · <a href="{url2}">{link_title2}</a>
📎 <a href="{chat_link_base}/{msg_id1}">Источник</a> · <a href="{chat_link_base}/{msg_id2}">Источник</a>

— — —

{...repeat for each topic...}

<b>~{total_messages}</b> сообщений · <b>{participants}</b> участников · <b>{num_topics}</b> тем
✅ Верификация: {confirmed} подтверждены / {no_source} без источника / {hallucinations} галлюцинаций
```

**Emoji mapping** for topics:
- hot/trending: 🔥
- new/release: 🆕
- warning/pain: ⚠️
- info/deep: 🔬
- tools: 🛠
- stream: 📺

**IMPORTANT: Send THREE messages to Telegram** (each must be under 4096 chars):

**Message 1**: Topics digest (the template above)
**Message 2**: Solutions & cases. Template:

```
🔧 <b>Решения и кейсы</b>

<b>[solution]</b> {title} — @{author}
<i>Проблема:</i> {problem}
{approach}
✅ <i>Результат:</i> {result}
🏷 {tool1}, {tool2}
📎 <a href="{chat_link_base}/{source_msg_id}">Источник</a>

— — —

<b>[workaround]</b> {title} — @{author}
<i>Проблема:</i> {problem}
{approach}
✅ <i>Результат:</i> {result}
📎 <a href="{chat_link_base}/{source_msg_id}">Источник</a>

— — —

<b>[antipattern]</b> {title} — @{author}
⚠️ {what_doesnt_work}
📎 <a href="{chat_link_base}/{source_msg_id}">Источник</a>
```

**Message 3**: Links section — all links with **annotations** and reaction counts. Use enriched data from Phase 4.5. Template:

```
🔗 <b>Ссылки дайджеста {chat_name}</b> (по реакциям)

<b>🏆 Топ (50+ реакций)</b>
• <a href="{url}">{title}</a> — {emoji}{count} {emoji}{count} ({total})
{annotation — 1-2 предложения}

• <a href="{url}">{title}</a> — {emoji}{count} ({total})
{annotation}
...

<b>📊 Средние (20-50)</b>
• <a href="{url}">{title}</a> — ({total})
{annotation}
...
```

If any message exceeds 4096 chars, split it into additional messages. Solutions in Message 2 (if no solutions found, skip Message 2 and send links as Message 2). Links in the last message.

Always add `"disable_web_page_preview": true` to **ALL** messages (topics, solutions, AND links) to avoid Telegram generating link previews that clutter the digest.

**Rules**:
- Maximum 4096 characters per message (Telegram limit)
- Use `━━━` line separators between topics
- Use `<blockquote>` for memorable quotes (1-2 per topic)
- Use `<a href="...">` for links, separate with ` · `
- Use `•` for bullet lists
- The JSON payload must have properly escaped quotes and newlines
- Show actual reaction emojis and counts for links, not approximate labels

**HTML Template** (light theme):

```html
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Дайджест: {chat_name} | {date_range}</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #fafafa;
            color: #1a1a1a;
            line-height: 1.7;
            padding: 2rem;
        }
        .container { max-width: 760px; margin: 0 auto; }
        header {
            border-bottom: 1px solid #e0e0e0;
            padding-bottom: 1.5rem;
            margin-bottom: 2rem;
        }
        h1 {
            font-size: 1.8rem;
            font-weight: 700;
            color: #111;
            margin-bottom: 0.3rem;
        }
        .period { font-size: 1rem; color: #777; }
        .badge {
            display: inline-block;
            background: #eef0ff;
            border: 1px solid #d0d4ff;
            color: #4a50c7;
            font-size: 0.75rem;
            padding: 0.2rem 0.6rem;
            border-radius: 4px;
            margin-top: 0.5rem;
        }
        section {
            margin-bottom: 1.5rem;
            padding: 1.5rem;
            background: #fff;
            border-radius: 10px;
            border: 1px solid #e8e8e8;
            box-shadow: 0 1px 3px rgba(0,0,0,0.04);
        }
        section:hover { border-color: #ccc; }
        h2 {
            font-size: 1.2rem;
            font-weight: 600;
            color: #111;
            margin-bottom: 0.8rem;
        }
        p { color: #444; margin-bottom: 0.6rem; }
        .quote {
            border-left: 3px solid #4a50c7;
            padding-left: 0.8rem;
            margin: 0.8rem 0;
            color: #555;
            font-style: italic;
        }
        a { color: #4a50c7; text-decoration: none; word-break: break-all; }
        a:hover { text-decoration: underline; }
        .links {
            margin-top: 0.8rem;
            padding-top: 0.6rem;
            border-top: 1px solid #eee;
        }
        .links a {
            display: inline-block;
            font-size: 0.85rem;
            margin-right: 1rem;
            margin-bottom: 0.3rem;
        }
        .sources {
            margin-top: 0.5rem;
            font-size: 0.8rem;
            color: #999;
        }
        .sources a {
            color: #999;
            text-decoration: none;
            border-bottom: 1px dotted #ccc;
        }
        .sources a:hover {
            color: #4a50c7;
            border-bottom-color: #4a50c7;
        }
        .tag {
            display: inline-block;
            font-size: 0.7rem;
            padding: 0.15rem 0.5rem;
            border-radius: 3px;
            margin-right: 0.3rem;
            margin-bottom: 0.3rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.03em;
        }
        .tag-hot { background: #fff0f0; color: #d63031; border: 1px solid #ffcccc; }
        .tag-new { background: #f0fff0; color: #27ae60; border: 1px solid #c8f7c8; }
        .tag-warn { background: #fff8e6; color: #b8860b; border: 1px solid #ffe4a0; }
        .tag-info { background: #f0f4ff; color: #2d5faa; border: 1px solid #c8d8ff; }
        footer {
            margin-top: 2rem;
            padding-top: 1.5rem;
            border-top: 1px solid #e0e0e0;
            color: #888;
            font-size: 0.85rem;
        }
        .stats { display: flex; gap: 2rem; margin-bottom: 1rem; }
        .stat-item span { color: #111; font-weight: 600; }
        .verify {
            background: #f0fff0;
            border: 1px solid #c8f7c8;
            border-radius: 6px;
            padding: 0.8rem 1rem;
            margin-top: 1rem;
            font-size: 0.85rem;
            color: #27ae60;
        }
        ul { list-style: none; padding: 0; }
        ul li {
            padding: 0.3rem 0;
            padding-left: 1rem;
            position: relative;
            color: #444;
        }
        ul li::before {
            content: "\2022";
            color: #4a50c7;
            position: absolute;
            left: 0;
        }
        code {
            background: #f0f0f0;
            border: 1px solid #ddd;
            border-radius: 3px;
            padding: 0.1rem 0.4rem;
            font-family: 'SF Mono', Menlo, monospace;
            font-size: 0.85rem;
            color: #333;
        }
        .kbd {
            background: #f0f0f0;
            border: 1px solid #ccc;
            border-bottom: 2px solid #bbb;
            border-radius: 3px;
            padding: 0.1rem 0.4rem;
            font-family: 'SF Mono', Menlo, monospace;
            font-size: 0.85rem;
            color: #333;
        }
        strong { color: #111; }
        .solutions-section { margin-top: 2rem; }
        .solutions-section h2 { font-size: 1.4rem; margin-bottom: 1rem; }
        .solution-card {
            padding: 1rem 1.5rem;
            margin-bottom: 1rem;
            background: #fff;
            border-radius: 10px;
            border: 1px solid #e8e8e8;
            border-left: 4px solid #4a50c7;
            box-shadow: 0 1px 3px rgba(0,0,0,0.04);
        }
        .solution-card.antipattern { border-left-color: #d63031; }
        .solution-card.workaround { border-left-color: #b8860b; }
        .solution-card.pipeline { border-left-color: #27ae60; }
        .solution-card .sol-header {
            display: flex; justify-content: space-between; align-items: center;
            margin-bottom: 0.5rem;
        }
        .solution-card .sol-type {
            font-size: 0.7rem; font-weight: 600; text-transform: uppercase;
            padding: 0.15rem 0.5rem; border-radius: 3px;
        }
        .sol-type-solution { background: #eef0ff; color: #4a50c7; }
        .sol-type-pipeline { background: #f0fff0; color: #27ae60; }
        .sol-type-workaround { background: #fff8e6; color: #b8860b; }
        .sol-type-case { background: #f0f4ff; color: #2d5faa; }
        .sol-type-antipattern { background: #fff0f0; color: #d63031; }
        .solution-card .sol-problem { color: #666; font-style: italic; margin-bottom: 0.4rem; }
        .solution-card .sol-approach { color: #444; margin-bottom: 0.4rem; }
        .solution-card .sol-result { color: #27ae60; font-weight: 500; }
        .solution-card .sol-result.negative { color: #d63031; }
        .solution-card .sol-author { font-size: 0.85rem; color: #888; margin-top: 0.4rem; }
        .solution-card .sol-tools { margin-top: 0.4rem; }
        .solution-card .sol-tools span {
            display: inline-block; font-size: 0.7rem; padding: 0.1rem 0.4rem;
            background: #f0f0f0; border: 1px solid #ddd; border-radius: 3px;
            margin-right: 0.3rem; margin-bottom: 0.2rem; color: #555;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Дайджест: {chat_name}</h1>
            <div class="period">{date_range}</div>
            <div class="badge">~{total_messages} сообщений &bull; MapReduce &bull; {num_clusters} субагентов</div>
        </header>

        <!-- For each topic, create a <section> -->
        <section>
            <h2>{topic_title}</h2>
            <span class="tag tag-{type}">{label}</span>
            <p>{summary text with <strong>bold</strong> highlights}</p>
            <div class="quote">&laquo;{quoted message}&raquo;</div>
            <div class="links">
                <a href="{url}">{link_title}</a>
            </div>
            <div class="sources">
                📎 <a href="{chat_link_base}/{msg_id1}">Источник</a> · <a href="{chat_link_base}/{msg_id2}">Источник</a>
            </div>
        </section>
        <!-- Repeat for each topic -->

        <!-- Solutions section -->
        <div class="solutions-section">
            <h2>🔧 Решения и кейсы</h2>
            <div class="solution-card {type}">
                <div class="sol-header">
                    <strong>{solution_title}</strong>
                    <span class="sol-type sol-type-{type}">{type}</span>
                </div>
                <div class="sol-problem">Проблема: {problem}</div>
                <div class="sol-approach">{approach}</div>
                <div class="sol-result">Результат: {result}</div>
                <div class="sol-author">— @{author}</div>
                <div class="sol-tools">
                    <span>{tool1}</span> <span>{tool2}</span>
                </div>
                <div class="sources">
                    📎 <a href="{chat_link_base}/{source_msg_id}">Источник</a>
                </div>
            </div>
            <!-- Repeat for each solution -->
        </div>

        <footer>
            <div class="stats">
                <div class="stat-item"><span>{total_messages}</span> сообщений</div>
                <div class="stat-item"><span>{participants}</span> участников</div>
                <div class="stat-item"><span>{num_topics}</span> тем</div>
            </div>
            <div class="verify">
                Верификация: {confirmed} фактов подтверждены / {no_source} без прямого источника / {hallucinations} галлюцинаций
            </div>
            <p style="margin-top: 1rem;">
                Сгенерировано MapReduce-пайплайном: {num_clusters} параллельных Haiku-субагентов (MAP) &rarr; Opus (REDUCE) &rarr; Haiku (VERIFY)
            </p>
        </footer>
    </div>
</body>
</html>
```

**Tag types** for topics (choose based on content):
- `tag-hot` — trending/viral topics (red)
- `tag-new` — new releases, announcements (green)
- `tag-warn` — problems, pain points (amber)
- `tag-info` — informational, deep dives, tools (blue)

**Rules**:
- Each topic from the REDUCE phase becomes a `<section>`
- Include 1-3 `<div class="quote">` for memorable quotes from the chat
- Include `<div class="links">` with shared URLs
- Use `<ul><li>` lists for collections of tips/facts
- Footer must include verification stats from the VERIFY phase
- The HTML must be self-contained (no external CSS/JS)

---

## Error Handling

- **Too many messages (>1000)**: Use dynamic cluster size (`total / 15`), NOT fixed 25 per cluster. Target 10-15 clusters.
- **Too few messages (<20)**: Skip MAP phase, analyze directly without subagents
- **Subagent timeout**: Re-run failed subagent once, then include partial results with a note
- **Chat in multiple languages**: Write digest in the dominant language, transliterate names consistently
- **MAP subagent returns wrong JSON format**: Always run the normalization step (see Phase 3, step 4). Haiku frequently renames `topics` → `main_topics`, `annotated_links` → `links_and_annotations`, etc. The normalization code handles all known variants.
- **Telegram HTTP 400 on sendMessage**: Most likely unescaped `<` or `>` in text. Escape ALL angle brackets that are NOT part of allowed HTML tags (`<b>`, `<i>`, `<a>`, `<code>`, `<blockquote>`).
- **`requests` module not found**: Use `urllib.request` from stdlib instead. Never assume third-party packages are installed.
- **Duplicate URLs in links**: Always normalize before dedup (strip `.git`, `#fragment`, `?t=NNN`, trailing `/`). Same YouTube video with different timestamps = one link.

---

## Cost Optimization Notes

- Haiku subagents are ~6x cheaper than Sonnet ($0.25 vs $1.50 per 1M input tokens)
- A typical digest of ~500 messages costs approximately $0.02-0.05
- The MAP phase runs in parallel, so wall-clock time is determined by the slowest cluster, not the sum
- VERIFY uses a single Haiku call, adding minimal cost
