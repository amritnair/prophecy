# Prophecy

[![Tests](https://github.com/amritnair/prophecy/actions/workflows/tests.yml/badge.svg)](https://github.com/amritnair/prophecy/actions/workflows/tests.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Two people pick up two tickets on Monday. Neither finds out they both rewrite
the same auth helper until Thursday, when one of them rebases. With coding
agents in the mix it is worse, because three of them can be editing the same
contract inside an hour and none of them knows the others exist.

Prophecy reads a repository the way a reviewer would, and answers one
question: given everything else happening here, what would this change break?
It works from a sentence describing work nobody has started yet, from a
branch, from an open pull request, or from a coding agent that asks over MCP
before it commits.

## live demo

**https://amritnair.github.io/prophecy/**

The engine shells out to git against a real checkout, so it cannot run on a
static host. What it can do is answer every question once and write the
answers down: `tools/export_static.py` runs the real server, records its
responses, and bakes them next to the page. That link is the result — the
whole dashboard, reading files instead of an API, free and always up.

Everything that reads works: the risk view, the map, who is in what, the
context slices, the ledger. What needs a live repository does not, and says
so when you try: editing files, committing, reverting, connecting your own
project, and MCP, which is a POST endpoint with no static equivalent.

For those you need a machine with a checkout on it. Three ways, none of
which cost anything:

**In a browser, nothing installed.** [Open a codespace][cs] and the dashboard
starts on port 8000 with the demo project built and five branches in flight.
Free on GitHub's hours, and you can point it at any repository you clone in
there.

[cs]: https://codespaces.new/amritnair/prophecy?quickstart=1

**With Docker.** One command, nothing cloned and nothing installed:

```
docker run -p 8000:8000 ghcr.io/amritnair/prophecy
```

Then open http://127.0.0.1:8000. It starts on the demo project. To look at
your own code, paste its URL into the project picker — `owner/project` is
enough — and it will be fetched and opened, with every branch that is in
flight rather than only the default one.

Repositories fetched that way land in `/repos` inside the container, which
goes away when the container does. Keep them:

```
docker run -p 8000:8000 -v ~/prophecy-repos:/repos ghcr.io/amritnair/prophecy
```

A repository already on your disk can be mounted directly instead, which is
the better way round when you want Prophecy watching code you are editing:

```
docker run -p 8000:8000 -v /path/to/repo:/repo -e PROPHECY_REPO=/repo \
  ghcr.io/amritnair/prophecy
```

Then go to the MCP tab and connect whatever you code with: it prints the
settings to copy, a one-click install for Cursor, and the line for Claude
Code.

```
claude mcp add --transport http prophecy http://127.0.0.1:8000/mcp
```

**For a team, one instance is the point.** Sessions, findings and warnings
are shared through it, so an agent only knows what another agent is doing if
they are pointed at the same place. One person runs it and the rest connect
to that host instead of their own:

```
claude mcp add --transport http prophecy http://<that-machine>:8000/mcp
```

It binds on every interface, so a laptop on the same network is enough.
`tools/deploy.sh` is the version that survives the laptop closing.

**Locally.** Python 3.10 or newer, no dependencies:

```
pip install -e .
prophecy demo /tmp/demo --serve
```

Or put it on a box permanently with `tools/deploy.sh`, which installs a
systemd service, gets TLS through Caddy, and runs with `serve --public` so
the API stays pinned to one repository and refuses writes.

- Demo app on GitHub: https://github.com/amritnair/prophecy-demo

```
$ prophecy risk

repository risk 97/100, CRITICAL
  branch agent-a/require-email is critical on its own
  branch agent-c/user-cleanup and branch chore/drop-dead-helper together read as medium

in flight (5)
  ████  94  ada            branch agent-a/require-email               critical
  █      7  grace          branch agent-b/signup-redesign             low
  █     26  linus          branch agent-c/user-cleanup                low
  █     12  sam            branch chore/drop-dead-helper              low
  █      0  priya          branch feat/judge-dashboard                low

where they meet (4)
  branch agent-a/require-email meets 3 others
  !!   branch agent-b/signup-redesign
       alone 88 and 1, together 97 (critical)
       - different files, but both reach api/signup.py
       - both work on email; one of them changes how it is stored
```

That last pair is the part worth having. Neither change looks dangerous, they
share no file and no import, and no diff review would put them side by side.

## what it reads

`scan.py` walks the files git tracks and pulls out symbols, signatures and
imports: `ast` for Python, pattern matching for TypeScript and JavaScript.
From the imports it builds a reverse index, so it knows that changing
`models.py` reaches fifteen other files. Signatures keep their defaults,
which matters more than it sounds: `create_user(id, name, email=None)` and
`create_user(id, name, email)` are the same name and a different contract,
and the second one breaks every existing call.

`branches.py` diffs any two commits down to that signature level and can tell
a move from a deletion, so a refactor that relocates twelve classes reads as
one thing rather than as twelve removals.

`work.py` is the answer to "who else is here". Three places hold part of it
and none holds all of it: live agent sessions know what is happening this
minute, branches know what is half finished, open pull requests know what is
waiting on review.

## what it decides

`risk_engine.py` scores a change from 0 to 100 with a range and a confidence.
The range is not decoration. The engine knows how many files import a symbol
and cannot know how often any of them runs, and the gap between those two is
what the range is for. Every score carries the evidence that produced it:
blast radius by layer, an explainable criticality model, the failures that
would follow, and a list of what remains unknown.

Two things it does that overlap detection cannot.

It reads intent from commit messages, branch names, or whatever an agent says
it is doing, and flags a diff that contradicts it. Tell it "make email
optional" while the change removes a default and it will say so.

It finds changes that are calm alone and dangerous together, including pairs
that share no file and no import but meet at the same stored field. That is
the case nobody catches by reading diffs.

```
$ prophecy analyze agent-a/require-email

agent-a/require-email
  risk 94/100, CRITICAL   (range 84-97, confidence 92%)
  criticality of what it touches: critical   blast radius: large (15 direct, 17 indirect)

  what could break
    [critical] create_user no longer accepts a missing email
               email used to have a default. Every call that relied on it has
               to pass one now, and 15 file(s) import app/models.py.
               api/profile.py, api/signup.py, api/submissions.py, api/teams.py
```

`prophecy check` is the same thing with an exit code, for running before a
commit. It holds above a threshold you choose, and says so in those terms
rather than pretending to be a verdict.

## agents

`prophecy serve` puts the dashboard on a port and serves MCP from the same
process at `/mcp`. Any agent that can POST JSON-RPC connects over HTTP, so it
does not have to be on your machine:

```
claude mcp add --transport http prophecy http://HOST:8000/mcp
```

The tools that matter are `join_repo_session`, `share_finding`,
`check_overlap`, `analyze_change` and `get_dependency_context`.

When there is no server to connect to, the shell is the integration. The
CLI is the same engine, and an agent with a terminal needs one thing it does
not have: a reason to run any of it. `prophecy guide` prints the short
version — the four commands that matter, in the order a session needs them —
and `prophecy guide --write` puts it in `AGENTS.md`, which is where an agent
opening a repository already looks:

```
prophecy guide --write        # writes AGENTS.md, safe to run twice
```

It is a marked section, so running it again updates that section and leaves
the rest of the file alone. Prefer MCP when a server is running: same
answers, no shell, and `join_repo_session` hands over the slice directly.

Join is where the work happens. Instead of letting an agent explore the tree
to orient itself, Prophecy hands back the slice: the files this task is
predicted to touch, the symbols in them, and their line numbers. The
prediction comes from the dependency graph plus three signals about right
now, which is what lets it land when the words miss entirely:

```
$ prophecy plan "throttle inbound traffic"

"throttle inbound traffic"  confidence 0.35
    8.7  app/notify.py
         uncommitted in this working tree
         another session is already in this file
```

Nothing in that task shares a letter with `notify.py`. It surfaces because
the file is uncommitted and somebody else is already in it, and it says which
of those is the reason. Confidence stays low, because a signal tells you
where work is happening, not that this task belongs there.

Findings pin to files. When an agent works something out that took effort to
see, it calls `share_finding`, and the next agent sent to that file is told
before it starts rather than deriving it again.

`analyze_change` answers short by default: score, reach, who else is in the
same code, and the two worst findings. The full evidence comes back only when
an agent asks with `detail: true`, because a few hundred tokens of reasoning
is re-billed on every later turn of that conversation.

`prophecy agents` runs three agents as three real MCP processes against one
project and prints what each of them sees, which is the fastest way to watch
context move between them.

## about token saving, precisely

There are two savings here and they are worth keeping apart.

On the MCP path an agent gets a tool result, not a cached system prompt. What
it saves is the reading it did not do: it is pointed at a slice instead of
exploring, it is handed a teammate's finding instead of re-deriving it, and
it gets a short answer unless it asks for the long one. That is real, and it
is not a prompt-cache hit. Anything claiming otherwise is wrong.

On the CLI path the cache arithmetic is true. `brief` splits context in two,
because prompt caching is a prefix match and one changed byte near the front
invalidates everything after it. The repository half is byte-identical for
every agent on a commit, so it goes in front of the breakpoint; each agent's
task, files and notes go behind it. `brief --request` prints a Messages
request with `cache_control` already placed, so the split is something you
run rather than reimplement.

On flask, two agents: a 1,533-token cached prefix plus about 1,100 tokens of
task each, against 294,652 tokens if both read every code file to orient
themselves. If the prefix lands under the roughly 1,024 tokens a model needs
before it caches anything, the command says so and tells you the savings
figures do not apply, because a request under that floor succeeds at full
price without mentioning it.

## checking the forecasts

`prophecy backfill` replays merges that already happened. For each one it
rewinds to where the two sides diverged, scans the code as it was then,
forecasts, and re-runs the merge for real. The forecast never sees the merge
commit, so it cannot work backwards from the answer.

Two things it refuses to do. It does not report recall on text conflicts as
an achievement: a text conflict requires both sides to edit one file, which
is the exact condition that makes this fire, so catching them all is
arithmetic rather than skill. And it reports no rate at all until at least
ten conflicting merges have been replayed.

What it does report is the conflict rate by risk level, which is the only
number that tests whether the scoring means anything. Across 250 flask
merges, among source files, `high` conflicted 6.1% of the time and `medium`
0.0%. The first run of that said the opposite, because the `medium` bucket
was full of lockfiles and changelogs that conflict constantly for reasons
unrelated to risk. Separating generated files from source fixed both the
measurement and a detector that had been giving bad advice.

## the dashboard

Seven screens, one question each. Everything on them comes from the same
engine as the command line.

**Home** is the landing page. An animated eye watches a network of files as
you scroll: an agent asks first, gets consequences rather than a lint, and
flags light up where changes collide. A strip underneath shows what has been
read (files understood, dependencies traced, changes in flight, the riskiest
one), and the setup steps connect GitHub (read-only), link your team, or
start a project already wired for agents.

**MCP** has a scripted walkthrough of three agents (Ada's Claude Code,
Grace's Cursor, Linus's Codex) and what each is told. It is a script, not a
live model. Under it is the real connection: one URL, a one-click install for
Cursor, or the settings to paste into any other MCP client.

**Risk** is the main screen. It opens with a verdict in plain words, the
severity out of 100 and the worst collision. A graph joins every change in
flight to the ones its work meets, with critical pairs dashed in red. The
list beside it opens one change at a time: the score as a range, the
evidence, who else is in the same code, and a button that queues its risk
profile in that agent's inbox for its next call. Below are the agents
working here, where changes meet (grouped around the branch that keeps
colliding), and three tools: check one branch, forecast work nobody has
started, and list the branches.

**Crew** is the view for a product manager: one row per workflow, with who is
on it and how risky it is, as a list or as cards. It has a feed of what has
happened and a summary of who is in the same files.

**Context** shows what agents are sent. One bar sets what it would cost for
every agent to read the whole codebase against what was actually sent, and a
diagram shows which findings passed from which agent to which. The numbers
are the CLI path, where the cache arithmetic is true (see above).

**Map** is the dependency graph. A file is bigger the more files import it
and red when two people are in it. You can search, filter, zoom, click a file
for what reaches it, and double-click to edit and commit.

**Ledger** is the record: what Prophecy said, who changed what, and a button
that replays real merges to grade the forecasts, described above.

## who gets in

By default nobody signs in, because by default this is a process on your own
machine acting as you. The moment it is somewhere a team can reach, that
assumption is wrong, and the endpoints that matter write files, commit them
and fetch repositories.

```
prophecy serve --auth github --owner your-login --allow-org your-org
```

Identity comes from GitHub rather than from a password table here. Nobody
wants another password, and an organisation there is already the group this
wants to talk about: members of `--allow-org` get in, `--allow-user` names
individuals, and a rule naming a person beats a rule naming their org, so
somebody removed by name stays removed.

Three roles. `owner` decides who else gets in, `member` reads and writes,
`viewer` reads. It needs `PROPHECY_GITHUB_CLIENT_ID` and
`PROPHECY_GITHUB_CLIENT_SECRET` from an OAuth app whose callback is
`<your-url>/auth/callback`, and it refuses to start if you turn sign-in on
without saying who it is for, because an instance that asks for a login and
then admits whoever arrives first is worse than one that asks for nothing.

Sessions are random tokens stored hashed, in an HttpOnly SameSite cookie,
`Secure` whenever the request is not plain local http. Writes carry a header
derived from the session, because a cookie rides along on any request a
browser is told to make and a header does not.

**Agents cannot sign in.** So an agent carries a token belonging to a person,
created from the MCP tab, and everything it does is recorded under that name.
With sign-in on, `/mcp` requires one. With sign-in off, MCP stays open unless
`PROPHECY_MCP_TOKEN` is set — which is right for a container on your own
machine, and worth remembering before putting one anywhere else, since the
server binds every interface.

`serve --public` is the other direction: pinned to one repository, every
write refused, for an instance that is meant to be read by strangers.

## running it

Python 3.10 or newer. No dependencies.

```
pip install -e .

prophecy demo /tmp/demo --serve   # a sample project, seeded, dashboard on it
prophecy serve                    # dashboard and MCP on :8000

prophecy risk                     # everything in flight, and where it meets
prophecy analyze <branch>         # one change in full
prophecy check <branch> --max 70  # exit non-zero above a threshold you choose
prophecy plan "Add SMS reminders" # where work that does not exist yet lands
prophecy brief "task one" "task two" --request
prophecy agents                   # three agents over MCP, one project
prophecy backfill --limit 250     # grade forecasts against real history
prophecy history                  # who changed what, and what Prophecy said
prophecy restore <sha> --preview   # put an old version back in the tree
```

`-C <path>` points any command at another repository. `--json` gives you the
whole structure instead of the summary. `--llm` adds an optional semantic
matcher (Anthropic or OpenAI) on top of the lexical one; it is off unless you
ask for it, it is never used on the MCP path, and anything it names that is
not in the repository is dropped.

The dashboard is one HTML file served by the standard library. No build step,
no node_modules. Everything is scoped to one project: history, findings and
settings live in `.prophecy/` inside that repository and are shared with
nothing else.

`prophecy serve` listens on every network interface, so that an agent on
another machine can reach `/mcp`. The dashboard has no login, and it can read,
edit and commit files in any repository on the machine it runs on. Do not put
it on the public internet as it is. Setting `PROPHECY_MCP_TOKEN` in the
environment makes `/mcp` demand a bearer token; it does not protect the
dashboard.

```
python tests/test_prophecy.py
```

builds fixture repositories in temp directories, drives the MCP server over
its own wire protocol, replays a real conflicting merge, checks the sign-in
gate from outside, and checks that the cached prefix stays byte-identical
between runs. It runs on every push and pull request.

`CONTRIBUTING.md` has the house rules and what lives where.

## what it does not do

The default matcher is lexical, with no model behind it and no understanding
of what your code means. It knows that a task mentioning "rate limiting" and
a function called `rate_limit` are probably related, and that is most of the
trick. The repository signals carry it where the words miss. It will still
miss work that shares nothing with either, and it will occasionally point at
a file that merely sounds relevant. Every match prints the evidence that
produced it, so the bad ones take a second to throw out.

TypeScript and JavaScript are read with pattern matching rather than a
parser, so references in them are missed. The analysis says so in its own
uncertainty list rather than quietly rounding up.

Nothing here observes runtime. It knows how many files import a symbol and
not how often any of them runs, which is the single largest source of the
range on every score.

Scores are heuristics, not probabilities. `0.53` means "more than the other
thing on this list", not "53% chance". Nothing reaches 100, because 100 would
claim a certainty none of this has.
