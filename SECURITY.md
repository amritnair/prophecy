# Security

## Reporting something

Open a [security advisory](https://github.com/amritnair/prophecy/security/advisories/new),
or email amritnair23@gmail.com. Please do not open a public issue for
anything exploitable.

## What this thing is, in security terms

Prophecy shells out to git against a real checkout, reads every file that
repository tracks, and — unless told otherwise — writes files, commits them
and fetches other repositories. Treat an instance as having the access of
the account running it.

Three modes, and the difference matters:

**On your own machine, the default.** No sign-in. The server acts as you,
because it is you. It binds every interface, though, so anyone on the same
network can reach it; that is fine on a laptop at home and not fine on
shared wifi.

**Reachable by a team: `serve --auth github`.** GitHub is the identity.
Sessions are random tokens stored hashed, in an `HttpOnly`, `SameSite=Lax`
cookie marked `Secure` off localhost. Writes carry a header derived from the
session, because a cookie travels on any request a browser is told to make
and a header does not. Agents cannot sign in, so they carry a token
belonging to a person and their work is attributed to them. It refuses to
start if sign-in is on and nobody is allowed in.

**Read by strangers: `serve --public`.** Pinned to one repository, every
write endpoint refused, the `repo` parameter ignored. Without it that
parameter accepts any path on the host, which is correct for a tool running
as you and wrong the moment it is not.

## What is stored, and how to delete it

Everything Prophecy keeps about a project lives in `.prophecy/` inside that
repository: a SQLite database of risks, agent sessions, findings, messages
and verdicts, and a `secret` file when sign-in is on. Nothing is sent
anywhere, and there is no account to close.

```
rm -rf .prophecy/          # every trace of Prophecy in that project
```

Deleting it loses the history and the shared findings, and costs nothing
else — the next run rebuilds what it can from git.

The published demo at https://amritnair.github.io/prophecy/ is static. It
sets no cookies and has no server to send anything to. The page keeps your
last project and tab in `localStorage` so it opens where you left it; clear
site data and that is gone too. The one third party is Google Fonts, which
sees an IP address when the page loads, as it does on any site that uses it.

## What has been attacked on purpose

Prophecy will fetch a repository by URL and then render what is in it, so
the repository is untrusted input. A test repo was built with a branch named
`feat/<img/src=x/onerror=...>`, a tracked file named
`<img src=x onerror=...>.py`, a commit message carrying the same, and an
agent joining over MCP under that name. None of it executed: every one
arrives entity-escaped and inert, because the page escapes where strings are
built rather than where they are interpolated.

Session and token comparisons use `hmac.compare_digest`, since `!=` returns
as soon as two strings differ and tells an attacker how much of a guess was
right.

Worth repeating what is not covered: the GitHub round trip has never run
against GitHub, and there is no rate limiting on sign-in attempts.

## Known limits

`--public` is about paths and writes, not about secrets. Anything in the
repository it is pinned to — file names, symbols, commit history — is served
to whoever asks, by design. Pin it to something you are content to publish.

Fetching a repository by URL runs `git clone` on request. Local paths,
`file://` and git's `ext::` transport are refused, and a public instance
refuses the endpoint outright, but it will still use disk and reach the
host the URL names.

The optional model matcher in `llm.py` sends task text and file names to
Anthropic or OpenAI. It is off unless asked for, and never on the MCP path.

Nothing here observes runtime, and scores are heuristics. Do not wire
`prophecy check` into anything that must not be wrong.

## Supported versions

Pre-1.0: fixes land on `main`.
