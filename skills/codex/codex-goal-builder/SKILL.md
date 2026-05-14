---
name: codex-goal-builder
description: |
  Build production-ready Codex CLI `/goal` prompts for long-running autonomous work.
  Use when the user wants to: create a Codex goal, write a `/goal` prompt, set up
  Codex follow-goals mode, hand off a multi-hour autonomous task to Codex, or asks
  about how `/goal`, "follow goals", autonomous Codex execution, or Ralph loop works.
  Output is always two artifacts: (1) a long contract file in the repo, (2) a short
  `/goal` command (≤4000 chars) that references that file.
license: MIT
metadata:
  version: "1.0.0"
---

# Codex Goal Builder

You are helping the user create a Codex CLI `/goal` prompt that lets Codex work autonomously for hours toward a verifiable stopping condition.

## When to apply

Trigger on:
- "make a Codex goal", "write a /goal", "build a goal prompt"
- "follow-goals", "Codex autonomous", "Ralph loop"
- "run this plan in Codex", "hand off this slice to Codex"
- After producing an implementation plan and the user wants to execute it via Codex

## The hard constraints you must respect

1. **`/goal` body has a 4000-character limit.** Codex CLI rejects longer goals with: `Goal objective is too long: N characters. Limit: 4 000 characters. Put longer instructions in a file and refer to that file in the goal`.
2. **Output is ALWAYS two artifacts**, never just one:
   - A long-form **contract file** at `docs/superpowers/plans/<date>-<slice>-goal.md` (or wherever the user's project keeps planning artifacts).
   - A short **`/goal` command** (≤1500 chars to leave room for the user to add context) that references the file path.
3. **`/goal` is not safer than a regular Codex run.** OpenAI docs are explicit: *"`/goal` is not a replacement for good handoffs, tests, or review."* Always include scope boundaries and a stopping condition.

## Two paths: Sketch (default) or Detailed

**Default = Sketch.** Most slices need a 1-2 page execution sketch, not a 30-task per-step plan. Codex `/goal` mode runs its own plan→act→test→review loop; pre-writing per-step code blocks is duplicate work that Codex repeats anyway. Per-step detail does NOT improve outcome quality — **API verification against real code** does.

**Escalate to Detailed only when ALL three are true:**
1. Slice introduces new domain types referenced from >3 places.
2. Slice changes data model + caller wiring + tests in the same change.
3. Slice replaces an existing component (not just augments).

When you propose to use Detailed, state the reason verbatim ("data-model migration + N caller-site wirings + replacement of X — all three conditions met"). Otherwise go Sketch.

**User override:** the user can force either path with `--detailed` or `--sketch` in their request. Honour that without arguing.

### Why this matters (the lesson)

Detailed per-step plans burn ~50% of preparation time on work Codex repeats inside `/goal`. The two things that catch real bugs are (a) reading actual repo source before handoff, and (b) explicit ordering constraints. Both live in the Sketch.

## Pre-flight checklist — refuse to build until all 5 are answered

Before writing either Sketch or Detailed contract, gather these from the user (one question at a time if unclear):

1. **Measurable artifact** — What file, diff, or output marks completion? (Not "the feature works" — "src/X.swift exists with method Y and tests in OktaTests/X pass.")
2. **Verification command** — What single command proves correctness end-to-end? (e.g., `xcodebuild test ...`, `npm test && npm run lint`, `cargo test --all`.)
3. **Write-scope boundaries** — Exact directory list Codex may modify; everything else is read-only.
4. **Stop condition** — The literal sentence/state Codex should produce when done. Multiple conjuncts allowed but each must be verifiable by command, not by feel.
5. **Pause condition** — What triggers `/goal pause` (e.g., "if any test outside the slice goes red, pause and report").

If any of these is vague — STOP and ask the user. From OpenAI's own docs: *"If your objective is vague, the checklist will be vague, the audit will be vague, and the goal will be marked achieved based on a vague satisfaction."*

## The 5-line core (SmartScope pattern)

Both Sketch and Detailed contracts must specify, in order:

1. **Behavior** — what the system can do after completion (one sentence)
2. **Scope** — which files/dirs only (whitelist, not blacklist)
3. **Verification** — exact commands that prove correctness
4. **Evidence** — what artifacts/reports Codex should produce
5. **Stop rule** — the literal completion criterion

The Sketch wraps these in execution constraints (phase order, validation gates). The Detailed contract adds per-task code-block examples on top.

## Sketch template (default, ~1-2 pages)

Use this for most slices. Codex `/goal` fills in step-level detail inside its own loop.

```markdown
# <Slice name> Goal — Execution Sketch

Referenced from: `/goal ... follow contract in <path>`.

## Authoritative documents
- Spec: `path/to/spec.md`
- Plan / sketch: this file
- Conventions: `CLAUDE.md`, `.claude/rules/`

## Behavior (one sentence)
<what the system can do after completion>

## Stopping condition (all must be true, each verifiable by command)
(a) <condition 1>
(b) <condition 2>
...

## Phase order (one line per phase)
1. Phase 1: <name> — <what gets built in one line>
2. Phase 2: <name> — <what gets built in one line>
...

## Task tracking (recommended; Codex picks the depth)
At goal start, create durable plan/task entries covering every phase, so
progress is visible (especially after `/compact`) and recovery is easy.
Codex chooses the structure: phase-level only, OR phase + sub-tasks for
parallelizable phases. Strong recommendation: sub-tasks for any phase
that uses subagent fan-out (one sub-task per worker / file / directory),
because each sub-task naturally becomes one subagent's contract.

Suggested structure:
- One parent task per phase (8 phases → 8 parent tasks for a typical slice).
- Sub-tasks inside parallelizable phases (e.g., Phase 3 fixture fill,
  Phase 6 directory-split rename) — one per unit of independent work.
- Mark each phase / sub-task in_progress when starting, completed on
  green per-phase gate.
- Keep the checkpoint status line (see "Checkpoint reporting") aligned
  with the task list.

If Codex's environment provides a native plan/todo (e.g., `/plan` in the
TUI), use it; otherwise track in a `.plan` scratch file in the working
directory. Either way the user can inspect "where are we" at any moment.

## File map
- **Create**: `path/to/new1.ext`, `path/to/new2.ext`
- **Modify**: `path/to/existing1.ext` (what changes), `path/to/existing2.ext` (what changes)
- **Delete**: `path/to/dead.ext`

## API verification (real-code references that Codex must respect)
The following references were verified against repo HEAD before handoff:
- `<Type/Method>` in `<file:line>` — signature: `<exact signature>`
- `<API call>` in `<file:line>` — used as: `<example usage>`
- ...

If Codex finds reality differs, edit the sketch in the same commit that fixes
the code and note "sketch adjusted" in the commit message.

## Validation commands (use these exact forms)
\`\`\`bash
<lint command>
<focused test command>
<full test command>
<build command>
<sanity grep / jq commands>
\`\`\`

## Per-phase gate
After each phase: lint must pass + **only the tests that exercise the code touched by this phase** must pass (focused, e.g. `-only-testing:<bundle>` or `pytest tests/<module>`). The **full** test suite — especially anything with UI/integration runs — belongs at the **ship gate only** (final phase), not at every phase. Running hundreds of tests on every gate floods agent context with framework noise (xcodebuild's `IDETestOperationsObserver`/`LaunchParametersSnapshot`, swiftlint per-file lines, runner crashdumps on retry) and is a confirmed cause of mid-`/goal` compact-stream failures.

If the repo ships a quiet wrapper (e.g. `scripts/quiet-gate.sh`) that writes full output to disk and returns a summary, prefer it over raw tool invocation in every gate line of the goal.

Do not advance to the next phase with red lint or red focused tests. If the focused-test scope is unclear for a phase, name the exact bundle/file glob in the phase's task entry rather than defaulting to the full suite.

## Scope boundaries — DO NOT TOUCH
- <directory/feature not in scope>
- <"no new X" constraints>
- <compliance / safety constraints>

## Anti-patterns — stop if doing any of these
- <project-specific anti-pattern 1>
- <project-specific anti-pattern 2>
- Using `--no-verify` on commits or skipping hooks
- Skipping failing tests instead of fixing them
- Spawning subagents on tasks with shared file state or ordering dependencies
- Mandating the **full** test suite (especially UI/integration) as a per-phase gate when focused tests for the phase's code would suffice — bloats agent context with framework noise and was a confirmed cause of mid-`/goal` compact-stream failure
- Piping raw `xcodebuild`/`swiftlint`/`pytest -v` output into the agent's context instead of a summary-only wrapper

## Subagent strategy (omit if not applicable)
For phase <N>, parallelize via subagents because <K> independent files have no shared state. Per the Reasoning-effort policy: workers `high`, reviewers `xhigh`, scanners `medium`. Per the Return-size policy: every spawn carries an explicit return contract (no reasoning trace, no file contents in return; root re-reads files from disk).
\`\`\`
Spawn <K> worker subagents in parallel; wait for ALL:
1. <subagent 1: file path, task, stop condition> — effort: high
2. <subagent 2: file path, task, stop condition> — effort: high

Each worker returns ONLY:
{ "task": "<id>", "files_written": [...], "lines_changed": N,
  "status": "ok|error", "error": "<one-line if any>" }
Max ~150 tokens per return.

After workers join, spawn 1 reviewer:
- reviewer (effort: xhigh): audit combined diff vs spec §<N>.
  Reviewer returns ONLY:
  { "verdict": "ok|needs-attention|blocked",
    "concerns": [{ "file": "<path>:<line>", "issue": "<one sentence>", "severity": "cosmetic|consistency|blocker" }],
    "recommendation": "<one line>" }
  Max ~300 tokens. No diff echo.
\`\`\`
Other phases run single-agent (sequential dependencies).

## Branch + PR
- Branch: `<branch name>` off current HEAD.
- PR title: `<title>`. Body links spec, lists phase outcomes, includes screenshots.

## Checkpoint reporting
After each phase write one line:
```
Phase N/M done · K commits · lint clean · tests T passed
```
If blocked:
```
Phase N/M BLOCKED · <one-sentence reason> · <files touched>
```

## Success looks like
\`\`\`
<grep / count / state-check commands and their expected output>
\`\`\`

When that state is reached and PR is open, the goal is complete. Stop.
```

## Detailed contract template (rare — when escalation criteria met)

Use this skeleton **only** when all three escalation conditions are true. It adds per-task code blocks to the Sketch — useful when Codex needs to thread through tight cross-file dependencies in exact order.

```markdown
# <Slice name> Goal — Execution Contract

Referenced from: `/goal ... follow contract in <path>`.

## Authoritative documents
- Spec: `path/to/spec.md`
- Plan: `path/to/plan.md`
- Conventions: `CLAUDE.md`, `.claude/rules/`

## Stopping condition (all must be true)
(a) <verifiable condition 1>
(b) <verifiable condition 2>
... (one line each, all measurable by command)

## Execution contract
1. <ordering rule — e.g., "phases 1→N strict order, tasks within phase sequential">
2. <discipline rule — e.g., "TDD per task: failing test → minimum impl → green → lint → commit">
3. <commit rule — e.g., "one atomic commit per task; messages from plan verbatim; never amend">
4. <gate rule — e.g., "per-phase: focused tests for code touched this phase (`-only-testing:<bundle>`) + strict lint must be green; FULL test suite only at the ship gate, not at every phase">
5. <build-tool rule — e.g., "after creating any new file, run xcodegen generate">
6. <output-hygiene rule — e.g., "use scripts/quiet-gate.sh wrapper (or equivalent) for every gate; never let raw xcodebuild/swiftlint output land in agent context — it bloats compact payload and risks stream failure">

## Task tracking (recommended; Codex picks the depth)
Same recommendation as Sketch — at goal start, create durable plan/task
entries for every phase. Codex chooses phase-level only OR phase +
sub-tasks; sub-tasks strongly recommended where subagent fan-out is
listed (Phase 3 / Phase 6 in typical migration slices). Keep status
synced with the checkpoint reporting line. Use the TUI's `/plan` or a
`.plan` scratch file.

## Scope boundaries — DO NOT TOUCH
- <directory/feature not in scope>
- <explicit "no new X" rules>
- <compliance / safety constraints>

## Validation commands (use these exact forms)
```bash
<lint command>
<focused test command>
<full test command>
<build command>
<sanity grep / jq commands>
```

## Checkpoint reporting
After each <unit, e.g., phase>:
```
<Unit> N/M done · K tasks · J commits · lint clean · tests T passed
```
If blocked:
```
<Unit> N/M BLOCKED · <one-sentence reason> · <files touched>
```

## Subagent strategy (omit this section entirely if not applicable)
For phase <N>, parallelize via subagents because <K> independent files have no shared state. Per the Reasoning-effort policy: workers `high`, reviewers `xhigh`, scanners `medium`. Per the Return-size policy: every spawn carries an explicit return contract (no reasoning trace, no file contents in return). Make every effort explicit and every return shape explicit; do not let Codex pick defaults.
```
Spawn <K> worker subagents in parallel and wait for ALL results:
1. <subagent 1: file path, task, stop condition> — effort: high
2. <subagent 2: file path, task, stop condition> — effort: high
...
Each writes its file, writes its test, runs the focused test, commits with the
plan's commit message.

Each worker returns ONLY:
{ "task": "<id>", "files_written": [...], "files_modified": [...],
  "lines_changed": N, "test_run": "passed|failed|skipped",
  "commit_sha": "<short sha>" | null, "status": "ok|error",
  "error": "<one-line if any>" }
Max ~200 tokens per worker return.

After all workers return:
- reviewer (effort: xhigh): audit combined diff for phase <N> against spec §<N>.
  Reviewer returns ONLY:
  { "verdict": "ok|needs-attention|blocked",
    "concerns": [{ "file": "<path>:<line>", "issue": "<one sentence>", "severity": "cosmetic|consistency|blocker" }],
    "recommendation": "<one line>" }
  Max ~300 tokens. No diff echo.
  If verdict != ok, pause and report.

Then return control to root agent for the per-phase gate.
```
Other phases run single-agent (sequential dependencies).

## Anti-patterns — stop if doing any of these
- <project-specific anti-pattern 1>
- <project-specific anti-pattern 2>
- Using `--no-verify` on commits or skipping hooks
- Adding feature flags when the locked decision is full replacement
- Skipping failing tests instead of fixing them
- Spawning subagents on tasks with shared file state or ordering dependencies

## Branch + PR
- Start on new branch: `<branch name>` off current HEAD.
- All work on that branch. Do not push to main.
- End of last phase: push branch, open PR titled `<title>` with screenshots/logs of validation.

## Progress log
Tick `[ ]` → `[x]` in the plan as steps complete. If reality diverges (signature differs, etc.), edit the plan in the same commit and note "plan adjusted" in the message body.

## Success looks like
```
<grep / count / state-check commands and their expected output>
```

When that state is reached and PR is open, the goal is complete. Stop.
```

## The short `/goal` command pattern (≤1500 chars target)

```
/goal Implement <slice name> end-to-end by following the contract in <path/to/contract>.md.
Authoritative docs (read both first; spec wins on conflict):
- Spec: <path>
- Plan: <path>
Stopping condition: ALL of <one-line summary of (a)-(f) from contract>.
Follow the contract strictly. Work <unit-by-unit> in listed order. TDD per task:
failing test → minimum impl → green → lint → atomic commit (message from plan).
After each <unit> write one status line per the contract format. If status becomes
vague, tighten the goal in the contract file rather than adding ad-hoc instructions
to /goal.
```

## Subagents — when and how to instruct Codex to parallelize

Codex CLI supports **subagent workflows** for parallel task execution. They are powerful but consume more tokens (each subagent runs its own model + tools), so use them only when decomposable parallelism actually exists.

### Critical rule

> **Codex does NOT auto-spawn subagents.** They activate only when the goal/contract explicitly requests parallelization.

This means: if your contract benefits from parallel work, you must say so explicitly inside the contract file. Otherwise Codex runs sequentially.

### Reasoning-effort policy for spawned agents (default rule)

Per-agent reasoning effort is the lever that distinguishes "do the work" from "judge the work". Apply this rule whenever a contract spawns subagents:

| Agent role | Effort | Why |
|------------|--------|-----|
| **Worker** (writes code/files, mechanical edits, test scaffolding, decomposed implementation) | **`high`** | Workers need solid code, but most of their decisions are local; `xhigh` here burns budget without proportional quality gain. |
| **Reviewer / auditor / verifier** (security review, code review, spec-vs-impl check, lint/audit triage, second-pass critique) | **`xhigh`** | Reviewers must catch the things the workers missed. Their entire value is depth of reasoning over a finished artifact. |
| **Read-only scanner / explorer** (grep/listing/inventory; no judgment, no code change) | **`medium`** or whatever the global default is | Pure mechanical lookups don't benefit from deeper reasoning. |

**The skill MUST emit explicit per-agent effort in every spawn block in the generated contract.** Don't rely on Codex defaults — be explicit so a future maintainer reading the contract can audit the call cost and the rationale.

How to express it inside the contract (two valid forms; use the form the user's setup supports):

- **In the spawn instruction itself** (works in any Codex setup): append `effort: <high|xhigh>` next to each subagent description. Example: `agent-validator-tests: write OktaTests/...SchemaVersionTests.swift; effort: high`.
- **In a custom agent TOML** (`~/.codex/agents/<name>.toml`): set `model_reasoning_effort = "high"` (or `"xhigh"`). When the agent is spawned by name, the per-agent value wins over the global default.

If the user has set a non-default global `model_reasoning_effort` in `~/.codex/config.toml`, document it in the contract's "Subagent strategy" section and explain that worker agents downshift to `high` and reviewer agents stay at the global value (or `xhigh`, whichever is higher).

### Return-size policy for spawned agents (Level 1, in-spawn constraints)

When a subagent finishes, its `report_agent_job_result` payload is injected back into the root agent's context. **Eight workers each returning 5–10K tokens of "here is the file I wrote + my reasoning trace" adds 40–80K to root context per phase step.** On a 400K model that can crash the goal at the next auto-compact attempt (as observed on gpt-5.5 in Codex — `Error running remote compact task: stream disconnected`).

**The skill MUST emit an explicit return contract for every spawned agent.** Treat the spawn block as the production wire format: name exactly what the worker is allowed to put in its return.

Generic worker return template (apply to all Pattern A / Pattern C worker spawns by default):

```
Return ONLY the following JSON object; nothing else (no reasoning trace, no
file contents, no prose):
{
  "task": "<short identifier from spawn instruction>",
  "files_written":  ["<path1>", "<path2>", ...],   // omit if none
  "files_modified": ["<path1>"],                    // omit if none
  "lines_changed": N,                               // integer
  "status": "ok" | "error",
  "error": "<one-line message if status=error; omit otherwise>"
}
Max ~150 tokens. Root agent will Read files directly from disk if it
needs file contents.
```

Generic reviewer return template (apply to all reviewer spawns):

```
Return ONLY the following object; nothing else:
{
  "verdict": "ok" | "needs-attention" | "blocked",
  "concerns": [
    { "file": "<path>:<line>", "issue": "<one sentence>", "severity": "cosmetic|consistency|blocker" }
  ],   // 0 to 3 items
  "recommendation": "<one line>"
}
Max ~300 tokens. Do NOT include the diff. Do NOT echo the spec section.
```

Generic scanner return template (Pattern B mechanical lookups):

```
Return ONLY a flat bullet list of file:line references (or "none" if no
matches). One line per item. No prose, no reasoning. Max ~200 tokens.
```

The skill should **inline a return contract block under every spawn block** in the generated contract, not just reference the templates. Inlining survives spec drift better than a "see policy" pointer, and Codex obeys exact spawn-prompt text more reliably than it obeys cross-references.

These Level-1 constraints work on any model (gpt-5.3-codex / gpt-5.4 / gpt-5.5) and remove most of the context-bloat risk. Level 2 (custom agent TOML) and Level 3 (global `[agents]` config) reinforce them but the in-spawn contract is the load-bearing one.

### When subagents help (good fit)

- **Independent file creation** — multiple new files with no shared state (e.g., 3 different View files for the same pattern).
- **Multi-perspective review** — security review + code-quality review + perf review of the same diff.
- **Codebase exploration** — mapping affected paths + finding callers + collecting docs.
- **Parallel test suites** — each suite has its own fixtures and runs independently.
- **Independent specialized lookups** — read spec + read plan + read existing convention files at once.

### When subagents hurt (skip)

- **Sequential dependencies** — Phase B needs Phase A's output. Parallel = wasted tokens.
- **Same file edits** — two subagents both append to `SignalExtractors.swift` → merge conflicts.
- **Single-agent already efficient** — small unit, low fan-out → token overhead exceeds benefit.
- **Build/test cycles** — same workspace, can't parallelize a single test run.
- **Token-sensitive work** — weekly quota near limit. Subagents amplify burn.

### Prompt patterns to put in the contract file

**Pattern A — Spawn N workers per task list (effort: high, return-size constrained):**
```
Spawn one subagent per item below and wait for ALL results before continuing.
Each subagent runs at reasoning effort `high` (worker role per the policy above).
1. <task 1 with file paths and stop condition> — effort: high
2. <task 2 with file paths and stop condition> — effort: high
3. <task 3 with file paths and stop condition> — effort: high

Each worker MUST return ONLY this JSON object; no reasoning, no file contents:
{
  "task": "<task identifier>",
  "files_written":  ["<path>", ...],   // omit if none
  "files_modified": ["<path>", ...],   // omit if none
  "lines_changed": N,
  "status": "ok" | "error",
  "error": "<one-line message if status=error>"
}
Max ~150 tokens per return. Root agent reads files from disk if it needs
contents.
```

**Pattern B — Specialized roles (mixed effort by role, role-specific return shapes):**
```
For this phase, run specialized agents in parallel. Use the per-role
reasoning effort in the brackets — do not let Codex pick.
- spec_reader (effort: medium): read <spec path>, extract requirements for <section>. Pure read; no judgment.
- conventions_reader (effort: medium): scan .claude/rules/*.md for patterns matching <topic>; pure lookup.
- impl_finder (effort: medium): grep for existing <X> implementations across Okta/, return file:line list.
- code_reviewer (effort: xhigh): review the produced diff against the spec.

Return contracts by role (no prose, no reasoning, exact format):
- spec_reader / conventions_reader / impl_finder: flat bullet list of
  `file:line — one-line excerpt`. Max ~200 tokens.
- code_reviewer: {
    "verdict": "ok" | "needs-attention" | "blocked",
    "concerns": [ { "file": "<path>:<line>", "issue": "<one sentence>", "severity": "cosmetic|consistency|blocker" } ],   // 0 to 3
    "recommendation": "<one line>"
  }. Max ~300 tokens. Do NOT include the diff in the return.

Wait for all results; then proceed with implementation using the combined output.
```

**Pattern C — Decomposed implementation + reviewer (mixed effort, both ends constrained):**
```
Phase N has K independent files to create. Spawn K worker subagents
plus 1 reviewer at the end:
- worker 1 (effort: high): Create file X with content matching plan task A
- worker 2 (effort: high): Create file Y with content matching plan task B
- ...
Each worker: write file → write its test → run focused test → commit.

Worker return contract (same for all K, no reasoning, no file contents):
{
  "task": "<task id>",
  "files_written":  ["<path>", ...],
  "files_modified": ["<path>", ...],
  "lines_changed": N,
  "test_run":       "passed" | "failed" | "skipped",
  "commit_sha":     "<short sha>" | null,
  "status":         "ok" | "error",
  "error":          "<one-line message if status=error>"
}
Max ~200 tokens per worker return.

After all workers return:
- reviewer (effort: xhigh): read the combined diff for this phase, audit it
  against the spec section. Reviewer return contract:
  {
    "verdict": "ok" | "needs-attention" | "blocked",
    "concerns": [ { "file": "<path>:<line>", "issue": "<one sentence>", "severity": "cosmetic|consistency|blocker" } ],
    "recommendation": "<one line>"
  }. Max ~300 tokens. Do NOT include the diff.
  If verdict != ok, pause and report to the root agent before advancing.

Do NOT spawn workers for tasks that share a file or have ordering dependencies.
```

### Codex CLI config for subagents

Defaults are reasonable, but explicit config in `~/.codex/config.toml`:

```toml
[agents]
max_threads = 6                              # max concurrent subagents
max_depth = 1                                # 0 = root only; 1 = root can spawn (default)
job_max_runtime_seconds = 1800               # per-worker timeout (30 min)
default_worker_reasoning_effort = "high"     # workers default per the role policy above
default_reviewer_reasoning_effort = "xhigh"  # reviewers default per the role policy above
```

These defaults apply when a spawn instruction in the contract does not name a custom agent and does not specify per-spawn effort. The skill should still emit explicit per-spawn `effort: <level>` in the contract because it makes the contract auditable independent of host config.

### Optional: define custom specialized agents

For repeated patterns across goals, define reusable agents in `~/.codex/agents/<name>.toml`. **Set `model_reasoning_effort` per the role policy above** — workers `high`, reviewers `xhigh`, mechanical scanners `medium`.

Scanner example (mechanical lookup, no judgment, low effort):

```toml
name = "swift_lint_checker"
description = "Read-only SwiftLint runner that returns violation list"
model = "gpt-5.4-mini"                # cheaper for mechanical work
model_reasoning_effort = "medium"     # pure scanner per the role policy
sandbox_mode = "read-only"
developer_instructions = """
Run swiftlint --strict on the workspace. Return ONLY:
- Files with violations (with line numbers)
- Violation rule names
- Total count
Do not propose fixes. Do not edit any files.
"""
```

Reviewer example (audit role, deep reasoning):

```toml
name = "spec_vs_impl_reviewer"
description = "Reviews a diff against the relevant spec section, returns verdict"
model = "gpt-5.5"                     # full model for audit
model_reasoning_effort = "xhigh"      # reviewer role per the policy
sandbox_mode = "read-only"
developer_instructions = """
Read the diff on the current branch. Read the spec section at the path
given in the spawn prompt. Return strictly:
- verdict: ok | needs-attention | blocked
- top 1-3 concerns with file:line citation
- one-line recommendation
Do not edit files. Do not run commands beyond git diff and read.
"""
```

Then in the contract: `Spawn swift_lint_checker after each phase to audit lint state. Spawn spec_vs_impl_reviewer at the end of each phase before the per-phase gate.`

### Runtime control (in interactive Codex session)

- `/agent` — inspect active agent threads, switch between them
- Ask Codex directly: "stop the slowest agent", "close all read-only agents", "what is agent 3 doing?"

### Cost-aware default

When you draft a contract, default to **single-agent sequential**. Add subagent instructions only when:
1. You have ≥2 truly independent units in that phase
2. They live in different files / different directories
3. The phase's combined token cost without parallelism exceeds ~5min wall-time

If unsure, omit. Codex handles sequential work efficiently.

## Configuration commands to give the user

Provide these AFTER the goal artifacts, not before. The user runs them in their shell.

### Enable the feature (one-time)

```bash
# Either:
codex
> /experimental
# Toggle "goals" on.

# Or in ~/.codex/config.toml:
```

```toml
[features]
goals = true
```

### Recommended runtime flags (auto-approval, sandboxed)

```bash
codex --ask-for-approval never --sandbox workspace-write
```

| Flag | Effect |
|------|--------|
| `--ask-for-approval never` (`-a never`) | No interactive prompts for any action. |
| `--sandbox workspace-write` (`-s workspace-write`) | File writes scoped to project dir; commands run normally. |
| `--dangerously-bypass-approvals-and-sandbox` / `--yolo` | Removes BOTH approvals AND sandbox. Only inside hardened env (Docker, ephemeral VM). DO NOT recommend by default. |

Persistent config in `~/.codex/config.toml`:

```toml
approval_mode = "never"
sandbox_mode = "workspace-write"
```

### Goal lifecycle commands

```
/goal <objective>     # set
/goal                 # status
/goal pause           # halt without losing state
/goal resume          # continue
/goal clear           # remove
```

## Known failure modes (warn the user when relevant)

| Failure | Symptom | Mitigation |
|---------|---------|------------|
| Vague objective | "Marked achieved" with no real progress | Refuse to build until all 5 pre-flight items concrete |
| Plan mode suppresses continuation (issue #20656) | `/goal` looks active but nothing happens | User must exit Plan mode in TUI before goal will continue |
| Mid-turn compaction loses goal prompt (issue #19910) | Audit requirements disappear after long context | Keep the contract file as the durable source of truth; tell user to point Codex back to it: "re-read the goal file" |
| Quota burn on broad work | Weekly limit hit before completion | Tight scope boundaries + measurable stop condition; run on scratch branch |
| Codex marks done prematurely | Stopping condition is satisfied by trivial change | Stop condition must require validation command pass, not just "compiles" |
| Goal-mode is NOT safer than regular run | Same risks as ad-hoc Codex use | Always use scratch branch + sandbox; treat /goal as background process |

## Anti-patterns in user goal requests

When the user asks for a goal, watch for these red flags and refuse / push back:

- **"Make X better"** → no audit closure. Convert to specific behavior + verification.
- **"Run this plan"** without an implementation plan in the repo → write the plan first via writing-plans skill, then build the goal.
- **Multi-objective**: "Migrate to React and improve tests and add CI" → split into 3 goals, sequence them.
- **No validation command**: "until it looks right" → not auditable. Force user to provide a command.
- **Open scope**: "edit anywhere needed" → forces tight whitelist before proceeding.

## Best-fit vs poor-fit work

**Good fit** (proven by community testing):
- Migrations with parity tests (e.g., framework upgrade with snapshot tests)
- Coverage raises (`raise auth coverage from 38% to 75%`)
- Failing-test repairs (`make all OktaTests green`)
- Prompt/eval optimization loops (run eval → fix → re-run until threshold)
- Multi-file features with TDD plan ready
- Documentation updates with template

**Poor fit** (push back):
- Design decisions ("which architecture pattern?")
- Broad quality improvements without endpoint
- Exploratory research
- Anything requiring human judgment mid-stream
- Tasks where verification command can't be defined

## Process when invoked

1. Confirm the user has an existing spec in the repo. If not, suggest `superpowers:writing-plans` first.
2. **Choose path** — Sketch (default) or Detailed (only if all 3 escalation conditions met). State the chosen path with one-line justification. Respect explicit user override (`--detailed` / `--sketch`).
3. Walk through the 5 pre-flight checklist questions. Don't skip even on small slices.
4. **Subagent fit audit** — scan the spec/plan for phases that meet ALL three criteria: (a) ≥2 truly independent units, (b) different files/dirs, (c) combined sequential time > ~5min. List the parallelizable phases. For each, draft the explicit "Spawn N subagents" block from the patterns above. **Two mandatory enrichments per spawn block:**
   - **Effort**: classify each agent's role (worker / reviewer / scanner) and emit `effort: <level>` per the Reasoning-effort policy — workers `high`, reviewers `xhigh`, scanners `medium`. Never leave effort implicit.
   - **Return shape**: inline an explicit return contract per the Return-size policy (no reasoning trace, no file contents, max ~150–300 tokens depending on role). Root agent will Read files from disk if it needs contents. Never leave return shape implicit — that is the #1 cause of root-context bloat and downstream auto-compact failure (observed on gpt-5.5 in Codex).

   If a phase produces a non-trivial diff and the contract spawns workers, also spawn a reviewer at `xhigh` after the workers join, so the per-phase gate has a deep audit pass before lint/build/test. If no phase qualifies for parallelism, omit the subagent section entirely.
5. **API verification gate (MANDATORY before drafting the contract)** — for every type, method, file path, init signature, and config the contract will reference, `grep` or `Read` the real repo source and record the actual signature. Cite line numbers in the contract's "API verification" section. Skipping this step is the #1 cause of Codex burning tokens on wrong API names. Do NOT trust spec abstractions — verify against HEAD.
6. Draft the contract file using the Sketch template (or Detailed skeleton if path is Detailed). Customize every section — no template language left in. **Keep the `Task tracking (recommended)` section** in the contract; it tells Codex to materialize the plan up-front and (in slices with subagent fan-out) decompose into sub-tasks per worker. Codex picks the depth; the recommendation just guarantees the plan exists and survives `/compact`.
7. Draft the short `/goal` command. Stay ≤1500 chars to leave headroom.
8. Provide ALL artifacts to the user in this order:
   - **a. Codex launch command** (most important — without it, user gets prompted on every action):
     ```bash
     codex --ask-for-approval never --sandbox workspace-write
     ```
     Plus optional persistent config in `~/.codex/config.toml`.
   - **b. Contract file** content (markdown code block) + path where it lives.
   - **c. Short `/goal` command** ready to paste inside Codex.
   - **d. Subagent config** (`[agents]` block + optional custom-agent TOML files) — only if subagents used.
   - **e. Goal lifecycle commands** (`/goal pause`, `/goal resume`, `/goal clear`) for the user's reference.
9. Add explicit warnings specific to this goal: long-running → mention quota; subagents → mention token burn; macOS Downloads/ folder → mention TCC permissions; Plan-mode in TUI → tell user to exit before starting goal (issue #20656).

## Output format example

Use this structure verbatim when delivering to the user. **The Codex launch command must be section 1** — without it, the user gets approval prompts on every action and the whole autonomous-execution promise breaks.

```
## 1. Launch Codex with auto-approval (run this FIRST)

\`\`\`bash
codex --ask-for-approval never --sandbox workspace-write
\`\`\`

Optional — persist in `~/.codex/config.toml` so you don't pass flags each session:

\`\`\`toml
approval_mode = "never"
sandbox_mode = "workspace-write"
\`\`\`

Inside Codex, enable goals once (if not already):
- Type `/experimental` and toggle goals on, OR
- Add to `~/.codex/config.toml`:
\`\`\`toml
[features]
goals = true
\`\`\`

## 2. Save contract file

Path: `<absolute path>`

\`\`\`markdown
<full contract content>
\`\`\`

## 3. Inside Codex, run the goal

\`\`\`
/goal Implement ...
\`\`\`

## 4. Goal lifecycle (reference)

- `/goal` — status
- `/goal pause` — halt without losing state
- `/goal resume` — continue
- `/goal clear` — remove

## 5. Warnings specific to this goal

- <e.g., long-running ~N hours, may eat weekly quota — start with `--sandbox workspace-write` (not `--yolo`) to keep blast radius small>
- <e.g., before `/goal`, exit TUI Plan mode — issue #20656 silently suppresses continuation>
- <e.g., if status becomes vague, tell Codex "re-read the goal file at <path>" rather than adding ad-hoc instructions to /goal>
- <e.g., if context compaction loses goal state — issue #19910 — re-issue: `/goal follow contract in <path>` >
```

## Sources

- [Follow a goal — OpenAI Codex docs](https://developers.openai.com/codex/use-cases/follow-goals)
- [Subagents — OpenAI Codex docs](https://developers.openai.com/codex/subagents)
- [Codex CLI features (subagents, modes, exec)](https://developers.openai.com/codex/cli/features)
- [Command line options — Codex CLI reference](https://developers.openai.com/codex/cli/reference)
- [Codex /goal practical guide — SmartScope](https://smartscope.blog/en/generative-ai/chatgpt/codex-goal-practical-guide/)
- [Codex /goal feature review — J.D. Hodges](https://www.jdhodges.com/blog/codex-goal-feature-review/)
- [Codex /goal meta-prompting — Aditya Bawankule](https://www.adityabawankule.io/blog/codex-goal-meta-prompting)
- [Ralph loop / autonomous coding — Ralphable](https://ralphable.com/blog/codex-goal-command-ralph-loop-openai-built-in-autonomous-coding-agent-2026)
- Known issues:
  - [Plan mode suppresses goal continuation (#20656)](https://github.com/openai/codex/issues/20656)
  - [Goal continuation lost after compaction (#19910)](https://github.com/openai/codex/issues/19910)
