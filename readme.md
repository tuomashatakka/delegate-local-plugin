# delegate-local-skill

Local subagents protocol. Spawn asynchronous `opencode` subagent processes.

A Claude Code skill that wraps a locally installed [opencode](https://opencode.ai) runtime
as a background subagent: send a self-contained task brief, get a handle back immediately,
poll it, and collect a structured result. Multi-turn continuation and parallel fan-out
included.

## Install

Download the packaged skill from the latest release with the GitHub CLI:

```bash
gh release download --repo tuomashatakka/delegate-local-skill --pattern '*.skill'
unzip delegate-local.skill -d ~/.claude/skills/
chmod +x ~/.claude/skills/delegate-local/scripts/*
```

While this repository is private, that authenticated path is the only one that works. If it
is ever made public, the release asset also becomes reachable at a stable unauthenticated
URL:

```bash
curl -fsSL -o delegate-local.skill \
  https://github.com/tuomashatakka/delegate-local-skill/releases/latest/download/delegate-local.skill
```

The `chmod` is a safety net — `unzip` restores the executable bit on most platforms, but
Python's `zipfile.extractall` and some GUI extractors do not, and this skill runs from its
scripts.

For local development, symlink the source instead so edits apply without reinstalling:

```bash
ln -s "$PWD/skill" ~/.claude/skills/delegate-local
```

Requires `opencode`, `jq`, and `python3` on `PATH`. Run
`~/.claude/skills/delegate-local/scripts/delegate.sh doctor` to check the runtime.

## Usage

```bash
# spawn — returns a handle immediately
id=$(delegate.sh spawn --dir src/api "…task brief…" | jq -r .run_id)

# poll, or join
delegate.sh status "$id"
delegate.sh wait   "$id"

# continue the same opencode session
delegate.sh send "$id" "That missed the error path. Handle it and re-report."
```

Subcommands: `spawn`, `status`, `result`, `wait`, `send`, `list`, `cancel`, `logs`, `doctor`.

See [`skill/SKILL.md`](skill/SKILL.md) for the full protocol and
[`skill/references/opencode-cli.md`](skill/references/opencode-cli.md) for the
reverse-engineered `opencode run` flag and event-stream contract.

## Layout

```
skill/                        the skill itself — this is what gets packaged
  SKILL.md
  scripts/delegate.sh         the protocol, single entry point
  scripts/_events.py          NDJSON event stream -> structured result
  references/opencode-cli.md
scripts/package_skill.py      builds delegate-local.skill
.github/workflows/            packages and releases on push to main
```

## Packaging

```bash
python3 scripts/package_skill.py skill --check            # validate only
python3 scripts/package_skill.py skill --output dist      # build dist/delegate-local.skill
```

The archive's top-level directory comes from the `name:` field in `SKILL.md`, not from the
source folder, so `skill/` unpacks as `delegate-local/`. Builds are deterministic — fixed
timestamps and sorted entries — so an unchanged skill produces a byte-identical file.

Pushing to `main` runs the same steps in CI and attaches the result to a new release.
