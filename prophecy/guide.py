"""What to tell a coding agent about this CLI.

MCP is the better road when it is available: one connection, discovery and
auth handled, and the agent is told what exists rather than guessing. But an
agent cannot connect to a server nobody is running, and plenty of sessions
are a terminal and nothing else. In that case the shell is the integration,
and the only thing missing is that the agent has no idea this exists.

`prophecy --help` lists twenty-seven commands with six words each, which
tells an agent what the names are and not when to use any of them. So this
is the short version: the four commands that matter, in the order a session
actually needs them, and the honest note about which ones cost real time.

`prophecy guide --write` puts it in AGENTS.md, which is where an agent
opening a repository already looks.
"""

import re
from pathlib import Path

BEGIN = "<!-- prophecy:begin -->"
END = "<!-- prophecy:end -->"

GUIDE = """## Prophecy

Before you edit a file in this repository, ask what else is already in it.
Prophecy reads the branches, the open pull requests and the other agents at
work, and answers one question: given everything else happening here, what
would this change break?

### Before you start

```
prophecy plan "what you are about to do"
```

Prints the files that work is predicted to touch, with the symbols in them
and their line numbers, plus who else is already in those files. Start from
that instead of reading the tree: it is the same information for a fraction
of the tokens, and it says why each file surfaced so you can throw out the
ones that are wrong.

### Before you commit

```
prophecy analyze <branch>        # what this change would break, and how badly
prophecy check <branch> --max 70 # the same, with an exit code
```

`check` exits non-zero above the threshold you give it, so it belongs in a
pre-commit hook or a CI step. It is a threshold you chose, not a verdict.

### When somebody else is in the same code

```
prophecy risk          # everything in flight, and where it meets
prophecy sessions      # which agents are working here right now
```

The pairs worth reading are the ones that share no file and no import but
meet at the same stored field. Git will merge those cleanly and they will
still break.

### When you work something out

```
prophecy note <file> "what you learned"
```

Pins it to the file so the next agent sent there is told, instead of
deriving it again at full price.

### Notes

- `--json` on any command for machine-readable output.
- `-C <path>` points any command at another repository.
- Scores are heuristics with a range, not probabilities. Every one carries
  the evidence that produced it; read that before acting on the number.
- `prophecy backfill` and `prophecy verify` really merge things and take
  minutes. Everything else above is seconds.
- If a Prophecy server is running, prefer MCP: the same answers arrive
  without a shell, and `join_repo_session` hands you the slice directly.
"""


def text():
    """The guide on its own, for printing."""
    return GUIDE


def document():
    """The guide wrapped in markers, so writing it twice replaces rather
    than repeats."""
    return f"{BEGIN}\n{GUIDE}{END}\n"


def install(repo_path, filename="AGENTS.md"):
    """Put the guide in the file an agent reads when it opens a repository.

    Idempotent on purpose: the markers mean a second run updates the section
    instead of appending another copy, and anything else in the file is left
    exactly as it was.
    """
    target = Path(repo_path).expanduser().resolve() / filename
    block = document()

    if not target.exists():
        target.write_text(f"# Agent notes\n\n{block}")
        return {"path": str(target), "action": "created"}

    existing = target.read_text()
    if BEGIN in existing and END in existing:
        updated = re.sub(
            re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?",
            block, existing, count=1, flags=re.S)
        if updated == existing:
            return {"path": str(target), "action": "unchanged"}
        target.write_text(updated)
        return {"path": str(target), "action": "updated"}

    separator = "" if existing.endswith("\n\n") else (
        "\n" if existing.endswith("\n") else "\n\n")
    target.write_text(existing + separator + block)
    return {"path": str(target), "action": "appended"}
