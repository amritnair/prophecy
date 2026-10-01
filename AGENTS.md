# Agent notes

<!-- prophecy:begin -->
## Prophecy

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
<!-- prophecy:end -->
