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

The command registers this computer and downloads a verified runtime from
the configured service. A runner follows the global server profile before
starting research. Preparation jobs and optional local components are
managed on the server. Private state and cached releases live under
`~/Library/Application Support/DeviceAgent/`.

If the wait times out, keep the task ID and resume the same job:

```sh
python3 <skill-path>/scripts/ask.py --resume TASK_ID
```

Runtime releases are pinned for a session. Profile edits apply to new sessions;
the dashboard provides progress, errors, diagnostics, and retry controls.

## What you get

- The finished answer printed when the command exits.
- `report.md` in the folder where you ran the command: findings, source URLs
  with every claim, and a coverage-gaps section.
- Any other file the researcher saved, next to it.

The service retains tasks when the client disconnects. Exit status 0 means
the requested return path completed, 1 reports a failure or blocked runner,
and 2 means research or file delivery remains pending.

## Requires

- macOS on an Apple silicon computer
- Python 3.10 or newer
- Codex CLI 0.163.0+ (preferred) or Claude Code 2.1.294+

## Notes

- Questions should be one focused topic, 2048 UTF-8 bytes or fewer.
- Each run is a separate job; re-running starts a new one.
- The command waits by default up to one hour per run; set
  `ASK_TIMEOUT_SECONDS` to change that.
- This client requires the server's runner-profile protocol v2 and a published
  compatible release. It never falls back to executing unverified downloads.
