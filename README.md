[![skills.sh](https://skills.sh/b/random-react-dev/ask)](https://skills.sh/random-react-dev/ask)

# ask

One command. One research question. A sourced report: findings, citations,
and an honest "what was not covered" section.

## Install

```sh
npx skills add random-react-dev/ask
```

## Use

```sh
python3 <skill-path>/scripts/ask.py "your question"
```

That is the whole interface: the question as the only argument, the answer
and the saved files when it finishes. The command runs until the report is
ready — give it a few minutes.

On the first question on a computer, the command runs a short one-time check
(1–3 minutes, once only). It keeps a small, private set of files under
`~/Library/Application Support/`. Re-running the command when that computer
is idle just refreshes bundled files and keeps the earlier check; nothing
else changes.

## What you get

- The finished answer printed when the command exits.
- `report.md` in the folder where you ran the command: findings, source URLs
  with every claim, and a coverage-gaps section.
- Any other file the researcher saved, next to it.

If it exits with an issue, it prints one short line; the service keeps the
task on its side and the next question starts a fresh one.

## Requires

- macOS on an Apple silicon computer
- Python 3.10 or newer
- Codex CLI 0.163.0+ (preferred) or Claude Code 2.1.294+
- Outbound HTTPS to `https://34.14.204.251`

## Notes

- Questions should be one focused topic, 2048 characters or fewer.
- Each run is a separate job; re-running starts a new one.
- The command waits by default up to one hour per run; set
  `ASK_TIMEOUT_SECONDS` to change that.
