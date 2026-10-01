"""Given everything else happening in this repository, what would this change
break? Works from a branch, a pull request, a sentence describing work nobody
has started, or a coding agent asking over MCP before it commits."""

import argparse
import json
import shlex
import sys

from . import store
from . import github, history, llm
from .agent import brief, observations, request_skeleton
from .create import new_project
from .demo import build as build_demo, seed as seed_demo
from .history import commits, preview_restore, restore
from .risk_engine import (analyze_change, interactions, meet_groups,
                          repository_risk)
from .mcp import queue_risk_warning
from .work import in_flight
from .backfill import replay, report
from .branches import as_forecast, branches
from .merge import compare, trial_merge
from .predict import predict
from .risk import capsule, risks, strategies
from .scan import scan

MARK = {"high": "!!", "medium": " !", "low": "  "}


def build_parser():
    parser = argparse.ArgumentParser(prog="prophecy", description=__doc__)
    parser.add_argument("--version", action="version",
                        version=f"prophecy {version()}")
    parser.add_argument("-C", "--repo", default=".", help="repository to analyse")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--base", default="main", help="branch to compare against")
    parser.add_argument("--llm", action="store_true",
                        help="also ask a model where the work lands; "
                             "costs tokens, off by default")
    parser.add_argument("--provider", choices=sorted(llm.PROVIDERS),
                        help="which model provider --llm should use")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # `prophecy risk --json` is what anyone writes, and what an agent writes
    # every time, so it has to mean the same as `prophecy --json risk`.
    # SUPPRESS matters: without it the subcommand's default would overwrite
    # the flag when it was given before the subcommand instead.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--json", action="store_true",
                        default=argparse.SUPPRESS,
                        help="machine-readable output")

    def add_parser(name, **kwargs):
        kwargs.setdefault("parents", [shared])
        return sub.add_parser(name, **kwargs)

    guide = add_parser("guide", help=(
        "how an agent should use this, short enough to paste"))
    guide.add_argument("--write", action="store_true", help=(
        "write it into AGENTS.md in this repository, where an agent "
        "opening the project will read it"))
    guide.add_argument("--file", default="AGENTS.md",
                       help="write somewhere other than AGENTS.md")

    add_parser("scan", help="summarise what is in the repo")
    add_parser("status", help="what every branch is doing right now")
    add_parser("insights", help="what this repo has taught us so far")

    plan = add_parser("plan", help="forecast the impact of one task")
    plan.add_argument("task")

    add_parser("predict", help="risks between the branches that already exist")

    pr = add_parser("pr", help="check open pull requests against each other")
    pr.add_argument("numbers", nargs="*", type=int,
                    help="specific PRs; default is every open one")
    pr.add_argument("--comment", type=int, metavar="N",
                    help="post the findings as a comment on PR N")

    sim = add_parser("simulate", help="check several tasks against each other")
    sim.add_argument("tasks", nargs="*")
    sim.add_argument("--branch", action="append", default=[],
                     help="include a real branch in the comparison (repeatable)")

    ctx = add_parser("context", help="markdown briefing for a task")
    ctx.add_argument("task")
    ctx.add_argument("--against", action="append", default=[],
                     help="other in-flight task (repeatable)")

    ag = add_parser("brief",
                        help="cache-shaped context for one or more agents")
    ag.add_argument("tasks", nargs="+")
    ag.add_argument("--branch", action="append", default=[],
                    help="include a real branch as another agent's work")
    ag.add_argument("--agent", help="who this brief is for; used to keep an "
                                    "agent from being handed its own notes")
    ag.add_argument("--budget", type=int, metavar="N",
                    help="cap each agent's task section at N tokens")
    ag.add_argument("--exact", action="store_true",
                    help="count tokens through the API instead of estimating")
    ag.add_argument("--request", action="store_true",
                    help="print a Messages request with the cache breakpoint placed")

    note = add_parser("note",
                          help="record what an agent found, for the next one")
    note.add_argument("file")
    note.add_argument("text")
    note.add_argument("--agent", required=True, help="who found it")

    add_parser("usage", help="how agents used this and what sharing saved")

    fleet = add_parser("fleet",
                           help="find the work in flight and brief everyone on it")
    fleet.add_argument("tasks", nargs="*", help="extra work not yet in a branch")
    fleet.add_argument("--confirm", action="store_true",
                       help="apply the plan; without this it only describes it")

    explain = add_parser("explain", help="show one risk in full")
    explain.add_argument("risk_id")

    verify = add_parser("verify", help="really merge a branch and grade the forecast")
    verify.add_argument("branch")
    verify.add_argument("--test", help="command to run after a clean merge")

    back = add_parser("backfill",
                          help="replay old merges and grade the forecast")
    back.add_argument("--limit", type=int, default=50,
                      help="how many merge commits to replay")
    back.add_argument("--ref", default="HEAD", help="history to walk")

    mcp_cmd = add_parser(
        "mcp", help="run as an MCP server so agents can reach this mid-session")
    mcp_cmd.add_argument("--config", action="store_true",
                         help="print the client config instead of running")

    add_parser("sessions", help="which agents are working here right now")

    an = add_parser("analyze",
                        help="what could this change break, and how badly")
    an.add_argument("target", nargs="?", default="HEAD",
                    help="a branch, a commit, or base...head")

    add_parser("risk", help="risk across everything in flight right now")

    add_parser("history",
                   help="who changed what, and what prophecy said about it")

    rs = add_parser("restore", help="put an old version back in your tree")
    rs.add_argument("sha")
    rs.add_argument("paths", nargs="*")
    rs.add_argument("--force", action="store_true",
                    help="restore even with uncommitted changes")
    rs.add_argument("--preview", action="store_true",
                    help="show what would change without writing anything")

    chk = add_parser("check",
                         help="run before committing; exits non-zero if risky")
    chk.add_argument("target", nargs="?", default="HEAD")
    chk.add_argument("--max", type=int, default=70,
                     help="fail above this score (default 70)")

    new = add_parser("new", help="start a project already wired for agents")
    new.add_argument("path")
    new.add_argument("--name")
    new.add_argument("--github", metavar="OWNER/NAME",
                     help="also create it on GitHub (this publishes a repo)")
    new.add_argument("--public", action="store_true",
                     help="make the GitHub repo public instead of private")

    dm = add_parser("demo", help="build a sample project with three "
                                     "conflicting changes already in it")
    dm.add_argument("path")
    dm.add_argument("--serve", action="store_true",
                    help="seed it and open the dashboard on it")
    dm.add_argument("--port", type=int, default=8800)

    ag = add_parser("agents",
                        help="watch three agents coordinate through prophecy")
    ag.add_argument("--repo", dest="agents_repo",
                    help="which project; defaults to the one -C points at")

    people = add_parser("people", help="who is on this project")
    people.add_argument("action", nargs="?", default="list",
                        choices=["list", "add", "remove"])
    people.add_argument("name", nargs="?")
    people.add_argument("--github", default="", help="their GitHub handle")
    people.add_argument("--role", default="")

    serve = add_parser("serve", help="dashboard and MCP on the network")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--auth", choices=["github"], help=(
        "Require people to sign in with GitHub. Needs "
        "PROPHECY_GITHUB_CLIENT_ID and PROPHECY_GITHUB_CLIENT_SECRET."))
    serve.add_argument("--owner", help=(
        "GitHub login of whoever runs this instance. They can always get "
        "in, and they decide who else does."))
    serve.add_argument("--allow-org", action="append", dest="allow_orgs",
                       metavar="ORG", help=(
                           "Let members of this GitHub organisation in. "
                           "Repeatable."))
    serve.add_argument("--allow-user", action="append", dest="allow_users",
                       metavar="LOGIN",
                       help="Let this GitHub user in. Repeatable.")
    serve.add_argument("--open", dest="open_served", action="store_true",
                       help=("Open the repository this was started for, "
                             "instead of asking which project to use. For "
                             "containers and single-repo installs."))
    serve.add_argument("--public", action="store_true", help=(
        "Serve to strangers. Pins the server to this one repository and "
        "refuses every endpoint that writes, because the default assumes "
        "it is talking to the person who started it."))
    return parser


def version():
    """The installed version, without making a dependency of it."""
    try:
        from importlib.metadata import version as installed
        return installed("prophecy")
    except Exception:
        return "0.1.0 (not installed)"


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd == "guide":
        from . import guide as guide_mod
        if not args.write:
            print(guide_mod.text())
            return 0
        done = guide_mod.install(args.repo, args.file)
        if args.json:
            json.dump(done, sys.stdout, indent=2)
            print()
        else:
            print(f"{done['action']} {done['path']}")
            if done["action"] != "unchanged":
                print("  an agent opening this project will read it there")
        return 0
    if args.cmd == "agents":
        from .agents_demo import run as run_agents
        run_agents(args.agents_repo or args.repo)
        return 0
    if args.cmd == "demo":
        out = build_demo(args.path)
        if args.json:
            json.dump(out, sys.stdout, indent=2)
            print()
        elif out.get("error"):
            print(out["error"], file=sys.stderr)
            return 1
        else:
            print(f"Built a demo project at {out['path']}")
            for b in out["branches"]:
                print(f"  {b['author']:<8} {b['branch']:<28} {b['message']}")
            seeded = seed_demo(out["path"])
            print(f"  seeded {seeded['sessions']} agent session(s) and "
                  f"{seeded['people']} people")
            if args.serve:
                from .server import serve as serve_dash
                print(f"\n  http://127.0.0.1:{args.port}\n")
                return serve_dash(out["path"], args.port)
            print(f"\nTry:  prophecy -C {out['path']} risk")
            print(f"  or:  prophecy demo {out['path']}-2 --serve")
        return 0
    if args.cmd == "new":
        result = new_project(args.path, args.name, args.github, not args.public)
        if args.json:
            json.dump(result, sys.stdout, indent=2)
            print()
        elif result.get("error"):
            print(result["error"], file=sys.stderr)
            return 1
        else:
            print(f"Created {result['name']} at {result['path']}")
            print("  agent settings committed in .mcp.json: any agent opening "
                  "this project joins on its own")
            if result.get("remote"):
                print(f"  pushed to {result['remote']}")
            elif result.get("github_error"):
                print(f"  GitHub step failed: {result['github_error']}",
                      file=sys.stderr)
        return 0
    if args.cmd == "mcp":
        from .mcp import config_snippet, serve as serve_mcp
        if args.config:
            print(json.dumps(config_snippet(args.repo), indent=2))
            return 0
        return serve_mcp(args.repo)
    if args.cmd == "serve":
        from .server import serve
        from .server import access_config
        return serve(args.repo, args.port, public=args.public,
                     open_served=args.open_served,
                     access=access_config(args.auth, args.owner,
                                          args.allow_orgs, args.allow_users,
                                          args.repo))

    repo = scan(args.repo)
    db = store.connect(args.repo)
    store.save_snapshot(db, repo)
    out = COMMANDS[args.cmd](repo, args, db)
    if args.json:
        json.dump(out, sys.stdout, indent=2, default=str)
        print()
    return 0


def cmd_scan(repo, args, db):
    files = repo["files"]
    if not args.json:
        print(f"{repo['repo']} @ {repo['sha'][:10]} ({repo['branch']})")
        print(f"  {len(files)} code files, "
              f"{sum(len(f['symbols']) for f in files.values())} symbols")
        print(f"  {sum(1 for f in files.values() if f['is_test'])} test files, "
              f"{len(repo['schema_files'])} schema/migration files")
        hot = sorted(repo["callers"].items(), key=lambda kv: -len(kv[1]))[:5]
        if hot:
            print("  most depended on:")
            for rel, callers in hot:
                print(f"    {rel} <- {len(callers)} file(s)")
    return {k: v for k, v in repo.items() if k != "files"} | {"file_count": len(files)}


def cmd_status(repo, args, db):
    found = branches(repo["repo"], args.base)
    live = {n: b for n, b in found.items() if "error" not in b}
    solo = risks(repo, [as_forecast(b) for b in live.values()])
    store.save_risks(db, solo)
    if not args.json:
        print(f"base {args.base}, {len(live)} other branch(es)")
        for name, b in live.items():
            print(f"\n  {name}  +{b['ahead']}/-{b['behind']}  "
                  f"{b['author']}, {b['last_commit']}")
            print(f"    {b['subject']}")
            print(f"    {len(b['changed_files'])} file(s) changed")
            for change in b["signature_changes"][:3]:
                print(f"    signature: {change['before']} -> {change['after']}")
        if solo:
            print(f"\n{len(solo)} risk(s) from branches on their own:")
            for r in solo:
                print(f"  {MARK[r['risk_level']]} [{r['id']}] {r['risk_type']}: "
                      f"{r['evidence'][0]}")
    return {"base": args.base, "branches": found, "risks": solo}


def _repo_hints(repo, db=None):
    """Signals this machine already has about where work is happening.

    Only reachable when we hold a real path, which the CLI always does. A
    lookup that fails costs one signal, not the forecast.
    """
    hints = {"dirty": [], "recent": [], "claimed": []}
    try:
        hints["dirty"] = history.dirty_paths(repo["repo"])
    except Exception:
        pass
    try:
        hints["recent"] = [f for commit in history.commits(repo["repo"], 8)
                           for f in commit["files"]]
    except Exception:
        pass
    if db is not None:
        try:
            hints["claimed"] = [
                f["file"] for w in in_flight(repo, "main", db, store)
                for f in w["forecast"]["files"]
            ]
        except Exception:
            pass
    return hints


def _forecast(repo, task, args, db=None):
    """Lexical always; the model only when asked, and never silently."""
    lexical = predict(repo, task, hints=_repo_hints(repo, db))
    if not getattr(args, "llm", False):
        return lexical
    try:
        provider = llm.get_provider(args.provider)
        semantic = llm.semantic_predict(repo, task, provider)
    except llm.NoProvider as exc:
        print(f"no model used: {exc}", file=sys.stderr)
        return lexical
    except Exception as exc:
        print(f"model call failed, falling back to repository evidence: {exc}",
              file=sys.stderr)
        return lexical
    return llm.merge_forecasts(lexical, semantic)


def cmd_plan(repo, args, db):
    forecast = _forecast(repo, args.task, args, db)
    if not args.json:
        _print_forecast(forecast)
    return forecast


def cmd_pr(repo, args, db):
    try:
        open_prs = github.pull_requests(repo["repo"])
    except github.GitHubUnavailable as exc:
        print(f"github unavailable: {exc}", file=sys.stderr)
        return {"error": str(exc)}

    wanted = [p for p in open_prs if not args.numbers or p["number"] in args.numbers]
    if not wanted:
        if not args.json:
            print("no open pull requests" if not open_prs
                  else f"none of {args.numbers} are open")
        return {"pull_requests": open_prs, "risks": []}

    forecasts = []
    for pull in wanted:
        try:
            forecasts.append(github.forecast(repo["repo"], pull))
        except Exception as exc:
            print(f"could not read PR #{pull['number']}: {exc}", file=sys.stderr)
    found = risks(repo, forecasts)
    store.save_risks(db, found)

    if args.comment:
        body = github.comment_body(args.comment, found, repo["sha"])
        url = github.post_comment(repo["repo"], args.comment, body)
        if not args.json:
            print(f"commented on #{args.comment}: {url}")
        return {"commented": args.comment, "body": body, "risks": found}

    if not args.json:
        print(f"{len(wanted)} open pull request(s)")
        for pull in wanted:
            print(f"  #{pull['number']} {pull['title']}"
                  f"  ({(pull.get('author') or {}).get('login', '?')}"
                  f"{', draft' if pull.get('isDraft') else ''})")
        print(f"\n{len(found)} predicted risk(s) between them")
        for r in found:
            print(f"\n  {MARK[r['risk_level']]} [{r['id']}] {r['risk_type']}")
            print(f"     {'  <->  '.join(r['tasks'])}")
            for line in r["evidence"]:
                print(f"     - {line}")
            print(f"     -> {r['recommendation']}")
        if found:
            print("\nto leave this on a pull request: "
                  f"prophecy pr --comment {wanted[0]['number']}")
    return {"pull_requests": wanted, "risks": found}


def cmd_predict(repo, args, db):
    found = branches(repo["repo"], args.base)
    forecasts = [as_forecast(b) for b in found.values() if "error" not in b]
    return _collide(repo, args, db, forecasts, header="branches in flight")


def cmd_simulate(repo, args, db):
    found = branches(repo["repo"], args.base)
    forecasts = [_forecast(repo, t, args, db) for t in args.tasks]
    forecasts += [as_forecast(found[n]) for n in args.branch if n in found]
    if not forecasts:
        print("give me some tasks, or --branch <name>", file=sys.stderr)
        return {}
    return _collide(repo, args, db, forecasts)


def _collide(repo, args, db, forecasts, header=None):
    found = risks(repo, forecasts)
    plans = strategies(repo, forecasts, found)
    store.save_risks(db, found)
    if not args.json:
        if header:
            print(f"{header}: {', '.join(f['task'] for f in forecasts) or 'none'}\n")
        else:
            for forecast in forecasts:
                _print_forecast(forecast)
                print()
        print(f"{len(found)} predicted risk(s)")
        for r in found:
            print(f"\n  {MARK[r['risk_level']]} [{r['id']}] {r['risk_type']} "
                  f"({r['risk_score']}, confidence {r['confidence']})")
            print(f"     {'  <->  '.join(r['tasks'])}")
            for line in r["evidence"]:
                print(f"     - {line}")
            print(f"     -> {r['recommendation']}")
        print("\nexecution strategies")
        for plan in plans:
            print(f"\n  {plan['strategy']}: {plan['open_risks']} risk(s) left open")
            for step in plan["coordination_steps"]:
                print(f"     - {step}")
            print(f"     {plan['note']}")
    return {"forecasts": forecasts, "risks": found, "strategies": plans}


def cmd_context(repo, args, db):
    hints = _repo_hints(repo, db)
    forecast = predict(repo, args.task, hints=hints)
    others = [predict(repo, t, hints=hints) for t in args.against]
    found = risks(repo, [forecast, *others])
    store.save_risks(db, found)
    markdown = capsule(repo, forecast, found)
    if not args.json:
        print(markdown)
    return {"forecast": forecast, "risks": found, "markdown": markdown}


def cmd_brief(repo, args, db):
    found = branches(repo["repo"], args.base)
    hints = _repo_hints(repo, db)
    forecasts = [predict(repo, t, hints=hints) for t in args.tasks]
    forecasts += [as_forecast(found[n]) for n in args.branch if n in found]
    found_risks = risks(repo, forecasts)
    store.save_risks(db, found_risks)
    data = brief(repo, forecasts, found_risks, exact=args.exact,
                 db=db, agent=args.agent, store=store, budget=args.budget)

    if args.request:
        if not args.json:
            print(json.dumps(request_skeleton(data), indent=2))
        return request_skeleton(data)

    if not args.json:
        e = data["economics"]
        print("=" * 62)
        print("CACHED PREFIX: same bytes for every agent on this commit")
        print("=" * 62)
        print(data["prefix"])
        for s in data["suffixes"]:
            print()
            print("=" * 62)
            print(f"AFTER THE BREAKPOINT: {s['task']}")
            print("=" * 62)
            print(s["text"])
        print()
        print("-" * 62)
        label = "counted" if data["exact"] else "estimated"
        print(f"tokens ({label})")
        print(f"  cached prefix                  {data['prefix_tokens']:>8}"
              f"   #{data['prefix_hash']}")
        for s in data["suffixes"]:
            shared = (f"   +{s['notes_pulled']} note(s) from other agents"
                      if s.get("notes_pulled") else "")
            if s.get("trimmed"):
                shared += f"   -{len(s['trimmed'])} file(s) over budget"
            print(f"  task: {s['task'][:24]:<24} {s['tokens']:>8}{shared}")
        print(f"  {e['agents']} agent(s) reading every code file "
              f"{e['repo_if_each_agent_reads_every_file']:>8}")
        print(f"  this brief, first call         {e['brief_first_call']:>8}")
        print(f"  this brief, once cache is warm {e['brief_per_later_call']:>8}")
        print(f"  difference per later call      {e['saved_per_later_call']:>8}"
              f"   ~${e['dollars_per_later_call']} vs "
              f"${e['dollars_if_each_agent_reads_every_file']}")
        print(f"\n{e['note']}")
        for warning in data["warnings"]:
            print(f"\nwarning: {warning}")
    return data


def _work_in_flight(repo, args, db=None):
    """Live sessions, branches, open pull requests, plus anything typed in."""
    return in_flight(repo, args.base, db, store, getattr(args, "tasks", []) or [])


def cmd_fleet(repo, args, db):
    work = _work_in_flight(repo, args, db)
    if not work:
        message = ("Nothing is in flight here: no branches with changes, no "
                   "open pull requests. Name some work and I will plan for it.")
        if not args.json:
            print(message)
        return {"work": [], "summary": [message]}

    forecasts = [w["forecast"] for w in work]
    found = risks(repo, forecasts)

    # Key overlap on the work, not the person. One developer running two
    # agents on the same file is the exact case this is for, and keying on
    # who owns the branch makes that collision disappear.
    touching = {}
    for w in work:
        for f in w["forecast"]["files"]:
            touching.setdefault(f["file"], []).append(w)
    overlap = sorted(
        ({"file": path, "work": [w["label"] for w in items],
          "agents": sorted({w["agent"] for w in items})}
         for path, items in touching.items() if len(items) > 1),
        key=lambda o: (-len(o["work"]), o["file"]),
    )

    # what prophecy can tell the next agent without anyone writing it down
    derived = observations(repo, work)
    fresh = [o for o in derived if not store.note_exists(db, o["file"], o["note"])]

    apply_it = args.confirm
    data = brief(repo, forecasts, found,
                 db=db if apply_it else None,
                 agent=work[0]["agent"] if apply_it else None,
                 store=store if apply_it else None)
    if apply_it:
        for note in fresh:
            store.add_note(db, repo["sha"], note["agent"], note["file"], note["note"])
            store.log(db, repo["sha"], "shared", "prophecy",
                      f"noted about {note['file']}: {note['note']}")
        store.save_risks(db, found)
        for w, suffix in zip(work[1:], data["suffixes"][1:]):
            store.record_brief(
                db, repo["sha"], w["agent"], w["forecast"]["task"],
                data["prefix_hash"], data["prefix_tokens"], suffix["tokens"],
                data["economics"]["repo_if_each_agent_reads_every_file"]
                // max(1, len(work)),
            )

    e = data["economics"]
    people = sorted({w["agent"] for w in work})
    shared_risks = [r for r in found if len(r["tasks"]) == 2]
    high_shared = [r for r in shared_risks if r["risk_level"] == "high"]
    solo_high = [r for r in found
                 if len(r["tasks"]) == 1 and r["risk_level"] == "high"]
    sessions = [w for w in work if w["kind"] == "session"]
    branch_work = [w for w in work if w["kind"] == "branch"]
    pulls = [w for w in work if w["kind"] == "pull_request"]

    def count(n, one, many=None):
        return f"{n} {one}" if n == 1 else f"{n} {many or one + 's'}"

    # Narration rather than fields: this is the part a person reads first, and
    # "3 piece(s) of work" reads like a form even when the numbers are right.
    sources = []
    if sessions:
        sources.append(count(len(sessions), "live agent session"))
    if branch_work:
        sources.append(count(len(branch_work), "branch", "branches"))
    if pulls:
        sources.append(count(len(pulls), "open pull request"))
    listed = (", ".join(sources[:-1]) + " and " + sources[-1]
              if len(sources) > 1 else sources[0])
    # "there is 5 branches" and "5 people (four names)" both read as bugs
    single = len(work) == 1
    named = ", ".join(people) if len(people) <= 6 else (
        ", ".join(people[:5]) + f" and {len(people) - 5} more")
    opening = (
        f"Right now there {'is' if single else 'are'} {listed}"
        f" in flight here, across {count(len(people), 'person', 'people')}"
        f" ({named})."
    )
    summary = [opening]

    if overlap:
        where = ", ".join(o["file"] for o in overlap[:3])
        summary.append(
            f"They are not all in separate corners: {where} "
            f"{'is' if len(overlap) == 1 else 'are'} being changed by more "
            "than one of them."
        )
    else:
        summary.append(
            "Nobody is changing the same file as anyone else, so there is "
            "nothing to sequence today."
        )

    if high_shared:
        summary.append(
            f"{count(len(high_shared), 'of those overlaps looks', 'of those overlaps look')}"
            " worth settling before the work lands rather than at merge time. "
            "They are in the same functions, not just the same files."
        )
    if solo_high:
        summary.append(
            f"Separately, {count(len(solo_high), 'change')} here "
            f"{'alters' if len(solo_high) == 1 else 'alter'} an interface "
            "other files import, which reaches people who are not working on "
            "it at all."
        )
    if fresh:
        summary.append(
            f"I can pass on {count(len(fresh), 'thing')} to whoever works here "
            "next: who else is in each file, which signatures are about to "
            "change, which files want regenerating rather than merging. All of "
            "it is read from the repository, so nobody has to write it down."
        )
    summary.append(
        f"Everyone working here needs the same {data['prefix_tokens']:,} tokens "
        "of background about this codebase. Briefed rather than left to read "
        f"the repository, each agent costs about {e['brief_per_later_call']:,} "
        f"tokens a turn against the {e['repo_if_each_agent_reads_every_file']:,} "
        "it takes to find its own way in. On the brief path that background is "
        "a cached prefix as well; over MCP it is the slice that saves the "
        "reading."
    )
    if not data["cacheable"]:
        summary.append(
            "One caveat: this repository is small enough that the shared half "
            "lands under the size a model will cache, so treat that as a "
            "briefing convenience rather than a billing one."
        )

    computed = list(summary)

    # Opt-in: a model rewrites the briefing from the facts above. The computed
    # version is kept either way, so a reader can always see what the prose was
    # made from.
    summary_source = "computed"
    if getattr(args, "llm", False):
        facts = {
            "work": [{"agent": w["agent"], "label": w["label"], "kind": w["kind"],
                      "files": [f["file"] for f in w["forecast"]["files"]]}
                     for w in work],
            "files_touched_by_more_than_one": overlap,
            "risks": [{k: r[k] for k in
                       ("risk_type", "risk_level", "tasks", "evidence",
                        "recommendation")} for r in found[:12]],
            "observations_to_pass_on": fresh,
            "tokens": e,
        }
        try:
            written = llm.narrate(facts, llm.get_provider(args.provider))
            if written:
                summary, summary_source = written, "model"
        except llm.NoProvider as exc:
            print(f"no model used: {exc}", file=sys.stderr)
        except Exception as exc:
            print(f"model narration failed, using the computed one: {exc}",
                  file=sys.stderr)

    out = {
        "computed_summary": computed,
        "summary_source": summary_source,
        "work": [{k: v for k, v in w.items() if k != "forecast"} for w in work],
        "assignments": [
            {"agent": w["agent"], "label": w["label"], "kind": w["kind"],
             "files": [f["file"] for f in w["forecast"]["files"]],
             "tokens": suffix["tokens"],
             "notes_pulled": suffix.get("notes_pulled", 0)}
            for w, suffix in zip(work, data["suffixes"])
        ],
        "overlap": overlap,
        "risks": found,
        "economics": e,
        "prefix_tokens": data["prefix_tokens"],
        "cacheable": data["cacheable"],
        "observations": fresh,
        "recorded": apply_it,
        "summary": summary,
    }
    owners = {w["forecast"]["task"]: store.whose(db, w["agent"]) for w in work}
    for r in found:
        people = sorted({owners[t] for t in r["tasks"] if t in owners})
        r["owners"] = people
        if len(people) > 1:
            r["recommendation"] += f" Talk to {' and '.join(people)} first."
        elif people and len(r["tasks"]) == 1:
            r["recommendation"] += f" {people[0]} owns this one."

    if not args.json:
        for line in summary:
            print(line)
        print()
        for row in out["assignments"]:
            print(f"  {row['agent'][:22]:<22} {row['label'][:40]:<40} "
                  f"{len(row['files'])} file(s)")
        if overlap:
            print("\n  shared ground:")
            for item in overlap:
                print(f"    {item['file']:<44} {', '.join(item['agents'])}")
        if found:
            print("\n  who needs to talk to whom:")
            for r in found[:6]:
                print(f"    {MARK[r['risk_level']]} {r['risk_type']:<22} "
                      f"{r['recommendation'][:88]}")
        if fresh:
            print("\n  would tell the next agent:")
            for note in fresh[:6]:
                print(f"    {note['file']:<32} {note['note'][:70]}")
        print("\n" + ("Applied: briefs recorded and findings shared."
                       if apply_it else
                       "Nothing recorded yet. Re-run with --confirm to apply."))
    return out


BAR = {"critical": "████", "high": "███ ", "medium": "██  ", "low": "█   "}


def _split_target(target, base):
    if "..." in target:
        left, right = target.split("...", 1)
        return left or base, right or "HEAD"
    if ".." in target:
        left, right = target.split("..", 1)
        return left or base, right or "HEAD"
    return base, target


def _print_analysis(a):
    print(f"{a['label']}")
    print(f"  risk {a['risk_score']}/100, {a['risk_band'].upper()}"
          f"   (range {a['risk_range']['min']}-{a['risk_range']['max']},"
          f" confidence {int(a['confidence'] * 100)}%)")
    print(f"  criticality of what it touches: {a['criticality_band']}"
          f"   blast radius: {a['blast_radius']['size']}"
          f" ({len(a['blast_radius']['direct'])} direct,"
          f" {len(a['blast_radius']['indirect'])} indirect)")

    print(f"\n  intent, as written: {a['intent']['text'][:90]}")
    for line in a["intent_contradictions"]:
        print(f"    !! {line}")

    if a["potential_failures"]:
        print("\n  what could break")
        for f in a["potential_failures"]:
            print(f"    [{f['severity']:>8}] {f['title']}")
            print(f"               {f['detail']}")
            if f["affected"]:
                print(f"               {', '.join(f['affected'][:4])}")

    if a["criticality_evidence"]:
        print("\n  why this code matters")
        for line in a["criticality_evidence"]:
            print(f"    - {line}")

    if a["concurrent_overlap"]:
        print("\n  happening at the same time")
        for item in a["concurrent_overlap"]:
            print(f"    - {item['agent']} on {item['with']}: "
                  f"{', '.join(item['shared_files'][:3])}")

    if a["history"]:
        print("\n  history")
        for row in a["history"]:
            print(f"    - {row['file']} changed {row['commits']} time(s) recently")

    print("\n  what is not known")
    for line in a["uncertainty"]:
        print(f"    - {line}")

    if a["recommendations"]:
        print("\n  suggested")
        for line in a["recommendations"]:
            print(f"    -> {line}")


def cmd_analyze(repo, args, db):
    base, head = _split_target(args.target, args.base)
    work = _work_in_flight(repo, args, db)
    mine = next((w for w in work if head in w["label"]), None)
    owner = store.whose(db, mine["agent"]) if mine else None
    concurrent = [w for w in work
                  if head not in w["label"]
                  and (owner is None or store.whose(db, w["agent"]) != owner)]
    result = analyze_change(repo, base, head, label=head, agent=owner,
                            concurrent=concurrent)
    store.save_verdict(db, repo["sha"], result)
    if not args.json:
        _print_analysis(result)
    return result


def cmd_risk(repo, args, db):
    work = _work_in_flight(repo, args, db)
    analyses = []
    for item in work:
        if item["kind"] not in ("branch", "pull_request"):
            continue
        head = item["label"].split()[-1] if item["kind"] == "branch" else None
        if not head:
            continue
        owner = store.whose(db, item["agent"])
        analyses.append(analyze_change(
            repo, args.base, head, label=item["label"], agent=owner,
            # somebody's own live session is not a collaborator to coordinate
            # with; without this, ada gets told to talk to ada
            concurrent=[w for w in work
                        if w is not item and store.whose(db, w["agent"]) != owner],
        ))
    found = interactions(analyses)
    overall = repository_risk(analyses, found)
    for a in analyses:
        store.save_verdict(db, repo["sha"], a)
        mine = [i for i in found if a["label"] in i["between"]]
        a["auto_warned"] = bool(queue_risk_warning(db, repo, a, mine))

    if not args.json:
        print(f"repository risk {overall['score']}/100, {overall['band'].upper()}")
        for line in overall["drivers"]:
            print(f"  {line}")
        print(f"\nin flight ({len(analyses)})")
        for a in analyses:
            print(f"  {BAR[a['risk_band']]} {a['risk_score']:>3}  "
                  f"{(a['agent'] or '?')[:14]:<14} {a['label'][:42]:<42} "
                  f"{a['risk_band']}")
        if found:
            print(f"\nwhere they meet ({len(found)})")
            for group in meet_groups(found):
                hub = group["hub"]
                if hub:
                    print(f"  {hub} meets {len(group['pairs'])} others")
                pad = "  " if hub else ""
                for n in group["pairs"]:
                    i = found[n]
                    arrow = "!!" if i["escalates"] else "  "
                    other = [b for b in i["between"] if b != hub]
                    print(f"  {arrow} {pad}"
                          f"{' <-> '.join(other if hub else i['between'])}")
                    print(f"     {pad}alone {i['individual'][0]} and "
                          f"{i['individual'][1]}, together "
                          f"{i['combined_score']} ({i['combined_band']})")
                    for line in i["evidence"]:
                        print(f"     {pad}- {line}")
        else:
            print("\nnothing in flight meets anything else.")
    return {"repository": overall, "changes": analyses, "interactions": found,
            "meet_groups": meet_groups(found)}


def cmd_check(repo, args, db):
    base, head = _split_target(args.target, args.base)
    concurrent = [w for w in _work_in_flight(repo, args, db)
                  if head not in w["label"]]
    result = analyze_change(repo, base, head, label=head, concurrent=concurrent)
    over = result["risk_score"] > args.max
    if not args.json:
        if over:
            print(f"HOLD: risk {result['risk_score']}/100 "
                  f"(range {result['risk_range']['min']}-"
                  f"{result['risk_range']['max']}, "
                  f"confidence {int(result['confidence'] * 100)}%), "
                  f"over the {args.max} you set.")
            for f in result["potential_failures"][:3]:
                print(f"  [{f['severity']}] {f['title']}")
            for line in result["recommendations"][:3]:
                print(f"  -> {line}")
            print("\nThis is a threshold you chose, not a verdict. "
                  "--max raises it.")
        else:
            print(f"OK: risk {result['risk_score']}/100, under {args.max}.")
    result["over_threshold"] = over
    return result


def cmd_history(repo, args, db):
    said = store.verdicts(db)
    log = commits(repo["repo"], 25)
    by_label = {}
    for v in said:
        by_label.setdefault(v["label"], v)

    if not args.json:
        if said:
            print("what prophecy said")
            for v in said[:12]:
                print(f"  {v['at'][:16]}  {(v['agent'] or '?')[:12]:<12} "
                      f"{v['label'][:34]:<34} {v['risk_score']:>3} "
                      f"{v['risk_band']}")
                for line in (v["said"] or [])[:1]:
                    print(f"      -> {line[:96]}")
        else:
            print("nothing analyzed yet: run `prophecy risk`.")
        print("\nwho changed what")
        for c in log[:12]:
            verdict = by_label.get(c["subject"])
            mark = f"  [{verdict['risk_band']}]" if verdict else ""
            print(f"  {c['short']}  {c['author'][:12]:<12} {c['when'][:14]:<14} "
                  f"{c['subject'][:44]}{mark}")
            print(f"      {len(c['files'])} file(s): "
                  f"{', '.join(c['files'][:3])}")
        print("\nto put a version back:  prophecy restore <sha> [paths]")
    return {"verdicts": said, "commits": log}


def cmd_restore(repo, args, db):
    if args.preview:
        out = preview_restore(repo["repo"], args.sha, args.paths)
    else:
        out = restore(repo["repo"], args.sha, args.paths, args.force)
    if not args.json:
        if out.get("error"):
            print(out["error"], file=sys.stderr)
            for line in out.get("uncommitted", [])[:6]:
                print(f"  {line}")
            return out
        if args.preview:
            print(f"restoring {args.sha[:10]} would change:")
            print(out["diffstat"] or "  nothing: the tree already matches")
        else:
            print(f"restored {', '.join(out['restored'])} from {args.sha[:10]}")
            print(f"  {out['note']}")
    return out


def cmd_people(repo, args, db):
    if args.action == "add":
        if not args.name:
            print("who? prophecy people add <name> [--github handle]",
                  file=sys.stderr)
            return {"error": "no name"}
        store.add_member(db, args.name, args.github, args.role)
    elif args.action == "remove" and args.name:
        store.remove_member(db, args.name)

    people = store.members(db)
    if not args.json:
        if not people:
            print("Nobody added yet. `prophecy people add <name> "
                  "--github <handle>` links a person to the commits and pull "
                  "requests they author.")
        for person in people:
            print(f"  {person['name'][:22]:<22} "
                  f"{('@' + person['github']) if person['github'] else '':<20}"
                  f"{person['role']}")
    return {"people": people}


def cmd_sessions(repo, args, db):
    live = store.live_sessions(db)
    events = store.feed(db, 15)
    if not args.json:
        if not live:
            print("No agent sessions are live here. Start one with "
                  "`prophecy mcp` wired into an agent, or see `mcp --config`.")
        for row in live:
            print(f"  {row['agent'][:20]:<20} {row['task'][:46]:<46} "
                  f"since {row['joined_at']}")
        if events:
            print("\n  recent")
            for e in reversed(events):
                print(f"    {e['at']}  {e['kind']:<8} {e['agent'][:16]:<16} "
                      f"{e['detail'][:60]}")
    return {"live": live, "events": events}


def cmd_note(repo, args, db):
    if args.file not in repo["files"]:
        print(f"{args.file} is not a code file in this repo", file=sys.stderr)
        return {"error": "unknown file"}
    store.add_note(db, repo["sha"], args.agent, args.file, args.text)
    if not args.json:
        print(f"recorded against {args.file}. The next agent sent to that file "
              "gets it in their brief.")
    return {"file": args.file, "agent": args.agent, "note": args.text}


def cmd_usage(repo, args, db):
    data = store.usage(db)
    if not args.json:
        if not data["briefs"]:
            print(data["note"])
            return data
        print(f"{data['briefs']} brief(s) issued to {data['agents']} agent(s)")
        print(f"  prefix sent in full        {data['prefix_first_time']:>8}")
        print(f"  prefix already seen        {data['prefix_reused']:>8}"
              f"   ({data['reuse_rate'] * 100:.0f}% reuse)")
        print(f"  tokens actually sent       {data['tokens_sent']:>8}")
        print(f"  if each read the repo      "
              f"{data['tokens_if_each_agent_read_the_repo']:>8}")
        print(f"  avoided                    {data['tokens_avoided']:>8}")
        print(f"\n  notes written {data['notes_written']}, "
              f"pulled into briefs {data['notes_pulled']}")
        if data["per_agent"]:
            print("\n  per agent")
            for row in data["per_agent"]:
                print(f"    {row['agent'][:28]:<28} {row['briefs']:>3} brief(s)"
                      f"  {row['task_tokens'] or 0:>7} task tokens"
                      f"  {row['notes_pulled'] or 0:>3} note(s) received")
        if data["shared_files"]:
            print("\n  files more than one agent has been into")
            for row in data["shared_files"]:
                print(f"    {row['file']:<40} {row['agents']} agent(s), "
                      f"{row['n']} note(s)")
        print(f"\n{data['note']}")
    return data


def cmd_explain(repo, args, db):
    risk = store.get_risk(db, args.risk_id)
    if not risk:
        print(f"no risk {args.risk_id}. Run scan, simulate or status first.",
              file=sys.stderr)
        return {}
    if not args.json:
        print(f"[{risk['id']}] {risk['risk_type']}  "
              f"{risk['risk_level']} ({risk['risk_score']})")
        print(f"  between: {'  <->  '.join(risk['tasks'])}")
        print(f"  repo at: {risk['sha'][:10]}")
        print(f"  first seen {risk['first_seen']}, last seen {risk['last_seen']}")
        print("  evidence:")
        for line in risk["evidence"]:
            print(f"    - {line}")
        print(f"  recommendation: {risk['recommendation']}")
        print(f"  confidence {risk['confidence']}: a heuristic, not a probability.")
    return risk


def cmd_verify(repo, args, db):
    found = branches(repo["repo"], args.base)
    others = [as_forecast(b) for n, b in found.items()
              if "error" not in b and n != args.branch]
    target = found.get(args.branch)
    if not target or "error" in target:
        print(f"no branch {args.branch}", file=sys.stderr)
        return {}
    forecasts = [as_forecast(target), *others]
    predicted = risks(repo, forecasts)
    store.save_risks(db, predicted)

    outcome = trial_merge(repo["repo"], args.base, args.branch,
                          shlex.split(args.test) if args.test else None)
    result = compare(predicted, outcome, forecasts)
    store.save_outcome(db, repo, outcome, result)

    if not args.json:
        print(f"merging {args.branch} into {args.base} in a scratch worktree")
        if outcome["error"]:
            print(f"  failed: {outcome['error']}")
        print(f"  clean merge: {outcome['merged_clean']}")
        if outcome["conflicted_files"]:
            print("  conflicts:")
            for rel in outcome["conflicted_files"]:
                print(f"    {rel}")
        if outcome["tests"]:
            print(f"  tests ({outcome['tests']['command']}): "
                  f"{'passed' if outcome['tests']['passed'] else 'FAILED'}")
        print(f"\nforecast said {len(predicted)} risk(s) over "
              f"{len(result['predicted_files'])} file(s)")
        print(f"  predicted and conflicted: {result['predicted_and_conflicted'] or '-'}")
        print(f"  conflicted, unpredicted:  {result['conflicted_unpredicted'] or '-'}")
        print(f"  predicted, no conflict:   {result['predicted_no_conflict'] or '-'}")
        for note in result["notes"]:
            print(f"  note: {note}")
    return {"predicted": predicted, "outcome": outcome, "comparison": result}


def cmd_backfill(repo, args, db):
    if not args.json:
        print(f"replaying up to {args.limit} merge(s) from {args.ref}")

    def progress(run):
        if args.json:
            return
        mark = "conflict" if run["conflicted"] else "clean   "
        detail = ""
        if run["conflicted"]:
            detail = (f"  caught {len(run['caught'])}/{len(run['conflicted'])}"
                      f"{'  MISSED ' + ', '.join(run['missed']) if run['missed'] else ''}")
        print(f"  {run['merge'][:8]}  {mark}  {run['risks']} risk(s){detail}")

    runs = replay(repo["repo"], args.limit, args.ref, progress)
    summary = report(runs)
    for run in runs:
        store.save_outcome(
            db, repo,
            {"base": run["merge"][:8] + "^1", "branch": run["merge"][:8] + "^2",
             "merged_clean": not run["conflicted"]},
            {"tests_passed": None, "predicted_and_conflicted": run["caught"],
             "conflicted_unpredicted": run["missed"],
             "predicted_no_conflict": run["flagged_no_conflict"], "notes": []},
            source="backfill",
        )
    if not args.json:
        print(f"\n{summary['merges_replayed']} merge(s) replayed, "
              f"{summary['merges_with_conflicts']} of them conflicted")
        print(f"  files touched across both sides:  {summary['files_in_play']}")
        print(f"  files flagged:                    {summary['files_flagged']}")
        print(f"  files that really conflicted:     {summary['conflicted_files']}"
              f" ({summary['files_caught']} flagged, {summary['files_missed']} missed)")
        if summary["median_lead_hours"] is not None:
            print(f"  median warning lead time:         "
                  f"{summary['median_lead_hours']} hours before the merge")
        if summary["by_level"]:
            print("\n  does the score mean anything?")
            for level, bucket in summary["by_level"].items():
                print(f"    {level:<7} {bucket['flagged']:>5} flagged  "
                      f"{bucket['conflicted']:>4} conflicted  "
                      f"{bucket['rate'] * 100:>5.1f}%")
        print(f"\n{summary['recall_note']}")
        print(f"\n{summary['verdict']}")
    return {"runs": runs, "summary": summary}


def cmd_insights(repo, args, db):
    data = store.insights(db)
    if not args.json:
        print(f"{data['snapshots']} snapshot(s), "
              f"{data['risks_recorded']} risk(s) recorded")
        for kind, n in data["by_type"].items():
            print(f"  {n:>3}  {kind}")
        print(f"\n{data['merges_run']} merge(s) run by hand, "
              f"{data['merged_clean']} clean")
        print(f"{data['merges_replayed']} merge(s) replayed from history")
        print(f"forecast accuracy: {data['accuracy']}")
        if data["recurring"]:
            print("\nrisks that keep coming back:")
            for row in data["recurring"]:
                print(f"  [{row['id']}] {row['risk_type']}: {row['tasks']}")
    return data


def _print_forecast(forecast):
    print(f'"{forecast["task"]}"  confidence {forecast["confidence"]}')
    if not forecast["files"]:
        print("  nothing in this repo matched.")
    for f in forecast["files"]:
        print(f"  {f['score']:>5}  {f['file']}")
        for line in f["evidence"][:2]:
            print(f"         {line}")
    if forecast["tests"]:
        print(f"  tests: {', '.join(forecast['tests'])}")
    if forecast["unsupported_terms"]:
        print(f"  no repo evidence for: {', '.join(forecast['unsupported_terms'])}")
    if forecast.get("grounding"):
        print(f"  {forecast['grounding']}")
    if forecast.get("model_invented"):
        print(f"  model named files that do not exist, dropped: "
              f"{', '.join(forecast['model_invented'])}")


COMMANDS = {
    "scan": cmd_scan, "status": cmd_status, "plan": cmd_plan,
    "predict": cmd_predict, "simulate": cmd_simulate, "context": cmd_context,
    "explain": cmd_explain, "verify": cmd_verify, "insights": cmd_insights,
    "backfill": cmd_backfill, "brief": cmd_brief, "pr": cmd_pr,
    "note": cmd_note, "usage": cmd_usage, "fleet": cmd_fleet,
    "sessions": cmd_sessions, "people": cmd_people,
    "analyze": cmd_analyze, "risk": cmd_risk, "check": cmd_check,
    "history": cmd_history, "restore": cmd_restore,
}

if __name__ == "__main__":
    sys.exit(main())
