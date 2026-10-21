---
name: ask
description: Research a question across the web and return a sourced report with cited links. Use when the user asks to research, look into, investigate, compare, or summarize a topic, or wants a written report backed by sources rather than a quick answer.
---

# Ask

Do a research question the right way: one command, one sourced report.

## How to use

Use this task server/dashboard URL: https://34.14.204.251

Pass the user's question as the only argument:

```bash
python3 <this-skill>/scripts/ask.py "What is the current state of local LLMs in 2026?"
```

In Claude Code, use `${CLAUDE_SKILL_DIR}/scripts/ask.py`; in any other agent, use the skill's absolute path.

The command runs until the report is ready. When it exits:

- The full answer is printed.
- The complete report is saved to `report.md` in the current folder, plus any other files the researcher saved.

## What to tell the user

- Tell them it can take a few minutes.
- The server-managed runner prepares the computer according to the current global profile.
- Preserve the printed task ID. On timeout, use `--resume TASK_ID`; do not submit a duplicate question.
- If preparation is blocked, use the dashboard's error details and retry controls, then resume the same task.
- File delivery can remain pending after research finishes. Do not describe queued files as delivered.

## When to use

- "Research X", "look into X", "investigate X", "compare X and Y"
- "Write me a report on ..." with sources expected
- Questions where cited sources matter and the answer is more than a few lines

When not to use: a single fact you can answer instantly, or a task that does not need sources.
