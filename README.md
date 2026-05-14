# my-claude-skills

Personal collection of [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) skills.

A **skill** is a reusable instruction packet that Claude Code can load on demand. Each skill lives in its own folder with a `SKILL.md` file whose YAML frontmatter declares when the skill should be invoked. When a user request matches the trigger description, Claude loads the full skill content and follows it.

## Installation

Skills live in `~/.claude/skills/<skill-name>/SKILL.md`. To install a skill from this repo, copy or symlink its folder:

```bash
# Copy a single skill
cp -R skills/codex/codex-goal-builder ~/.claude/skills/

# Or symlink — picks up updates from `git pull` automatically
ln -s "$(pwd)/skills/codex/codex-goal-builder" ~/.claude/skills/codex-goal-builder
```

Restart Claude Code (or run `/help` to confirm the skill is visible). Skills are auto-discovered by name; the on-disk folder name must match the `name:` field in `SKILL.md` frontmatter.

> **Note on categories**: this repo groups skills into category folders (`codex/`, `seo/`, ...) for navigation. Claude Code itself expects a flat layout under `~/.claude/skills/`, so when installing you copy only the inner skill folder — not the category folder.

### Cloning with submodules

Some skills live in their own dedicated repositories and are linked here as git submodules (e.g. [`robokassa-readiness`](skills/payments/robokassa-readiness/)). Clone with `--recurse-submodules` to populate them:

```bash
git clone --recurse-submodules https://github.com/TPuHo4u/my-claude-skills.git

# Or, if you've already cloned without submodules:
git submodule update --init --recursive
```

## Skills

### codex

| Skill | Description |
|-------|-------------|
| [`codex-goal-builder`](skills/codex/codex-goal-builder/) | Build production-ready Codex CLI `/goal` prompts for long-running autonomous work. Output is two artifacts: a long contract file + a short `/goal` command (≤4000 chars). |

### productivity

| Skill | Description |
|-------|-------------|
| [`tg-digest`](skills/productivity/tg-digest/) | Create a digest from Telegram chat messages using MapReduce with subagents. Uses Telethon directly for rich metadata (reactions, sender names, dates, replies). Bundled Python helpers for archiving, semantic search, and Obsidian sync. |

### payments

| Skill | Description |
|-------|-------------|
| [`robokassa-readiness`](skills/payments/robokassa-readiness/) ↗︎ | Pre-launch readiness audit for Russian payment surfaces (landing page, offer, privacy, refunds, requisites) before connecting Robokassa. Supports ИП, ООО, самозанятый. **Lives in its own repo** — installed here as a git submodule. |

### seo

_No skills yet._

### frontend

_No skills yet._

### swiftui

_No skills yet._

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the format, frontmatter requirements, and how to add a new skill.

## License

[MIT](LICENSE) — use, modify, and share freely with attribution.
