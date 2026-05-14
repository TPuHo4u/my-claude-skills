# Contributing

## Adding a new skill

1. **Pick a category.** Use an existing folder under `skills/` (`codex/`, `seo/`, `frontend/`, `swiftui/`) or open a PR proposing a new category if none fits.
2. **Create the skill folder.** Name it in `kebab-case` and match the folder name to the `name:` field in frontmatter:

   ```
   skills/<category>/<skill-name>/SKILL.md
   ```

3. **Write `SKILL.md` with valid frontmatter.** The frontmatter is what Claude Code reads to decide when to load the skill. Minimum required:

   ```markdown
   ---
   name: my-skill-name
   description: |
     One paragraph describing what the skill does, when to use it, and what it produces.
     Be specific about trigger phrases ("use when the user says X, Y, or Z") — this is
     what Claude pattern-matches against to decide whether to invoke.
   license: MIT
   metadata:
     version: "1.0.0"
   ---

   # Skill Title

   ...full instruction body...
   ```

4. **Update the root README.** Add a row to your category's table with a one-line description (you can lift it from the frontmatter).
5. **Bundle supporting files in the same folder** if needed (templates, examples, reference data). Keep paths relative to the skill folder so installation stays a single `cp -R`.

## Guidelines

- **Be specific about triggers.** A vague `description` ("helps with code") gets loaded too rarely or too often. Name the exact phrases or task shapes that should fire the skill.
- **Keep the body actionable.** Skills are instructions for an LLM that has full project context but no memory of past conversations. Write what to do, not what is.
- **Avoid project-specific paths.** A skill should be portable. If it needs a specific repo layout, state that as a precondition at the top.
- **Version your changes.** Bump `metadata.version` (semver) when you change behaviour.

## Format checks before PR

- Folder name matches `name:` in frontmatter.
- Frontmatter `description` clearly states when to invoke.
- README updated with the new skill.
- License declared (MIT preferred for consistency with the repo).
