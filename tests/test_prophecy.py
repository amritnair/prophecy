"""Builds a small throwaway repo and checks the whole pipeline against it.

Run with: python tests/test_prophecy.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

# so the suite runs from a checkout without installing the package first
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import prophecy
from prophecy import agent, backfill, branches, llm, mcp, merge, store
from prophecy import demo, risk_engine

FIXTURE = {
    "api/middleware.py": (
        "def authenticate(request, token):\n"
        '    """Check the caller token."""\n'
        "    return bool(token)\n\n"
        "def rate_limit(request, limit=100):\n"
        "    return True\n"
    ),
    "api/handlers.py": (
        "from api.middleware import authenticate, rate_limit\n\n"
        "def handle_request(request):\n"
        "    return authenticate(request, request.token)\n"
    ),
    "billing/invoice.py": "def charge(customer, cents):\n    return cents\n",
    "migrations/0001_add_users.sql": "CREATE TABLE users (id INT);\n",
    "requirements.txt": "flask\n",
    "tests/test_middleware.py": (
        "from api.middleware import rate_limit\n\n"
        "def test_rate_limit():\n    assert rate_limit(None)\n"
    ),
}


def build_repo(root):
    for rel, body in FIXTURE.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True,
                                    capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@t.t")
    run("config", "user.name", "t")
    run("add", "-A")
    run("commit", "-qm", "fixture")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build_repo(root)
        repo = prophecy.scan(root)

        # scan finds symbols, signatures and who imports what
        assert len(repo["sha"]) == 40
        assert "api/middleware.py" in repo["files"]
        signatures = {s["signature"] for s in repo["files"]["api/middleware.py"]["symbols"]}
        # defaults survive into the signature: whether a parameter is optional
        # is the difference between a safe change and one that breaks callers
        assert "rate_limit(request, limit=...)" in signatures, signatures
        assert repo["callers"]["api/middleware.py"] == [
            "api/handlers.py", "tests/test_middleware.py",
        ], repo["callers"]
        assert repo["schema_files"] == ["migrations/0001_add_users.sql"]
        assert repo["files"]["tests/test_middleware.py"]["is_test"]

        # a task lands on the right file, with evidence and honest gaps
        rate = prophecy.predict(repo, "Add rate limiting to the API")
        assert rate["files"][0]["file"] == "api/middleware.py", rate["files"]
        assert any("rate_limit" in e for e in rate["files"][0]["evidence"])
        assert "sms" in prophecy.predict(repo, "Send SMS reminders")["unsupported_terms"]

        # same file, different functions -> flagged, but not as a contract break
        auth = prophecy.predict(repo, "Add authentication middleware")
        found = prophecy.risks(repo, [rate, auth])
        top = found[0]
        assert top["risk_type"] == "shared_file", top
        assert top["risk_level"] == "medium", top
        assert any("import" in e for e in top["evidence"]), top["evidence"]
        # ids are content hashes, so they survive a rerun and `explain` keeps working
        assert top["id"] == prophecy.risks(repo, [rate, auth])[0]["id"]
        assert top["id"].startswith("R") and len(top["id"]) == 7

        # same function in scope for both -> contract risk, and it outranks the above
        reauth = prophecy.predict(repo, "Refactor how requests authenticate")
        contract = prophecy.risks(repo, [auth, reauth])[0]
        assert contract["risk_type"] == "shared_api_contract", contract
        assert contract["risk_level"] == "high", contract
        assert "authenticate" in contract["recommendation"], contract
        assert contract["risk_score"] > top["risk_score"]

        billing = prophecy.predict(repo, "Charge the customer an invoice")
        assert not prophecy.risks(repo, [billing, rate])

        # strategies and capsule render without blowing up
        assert len(prophecy.strategies(repo, [rate, auth], found)) == 3
        text = prophecy.capsule(repo, rate, found)
        assert "api/middleware.py" in text and "Coordination" in text

        check_hints(repo)
        check_branches(root, repo)
        check_backfill(root)
        check_brief(repo)
        check_llm(repo)
        check_sharing(root, repo)
        check_mcp(root)
        check_slice_and_brevity(root)
        check_mcp_http(root)

    check_risk_engine()
    check_clone_urls()
    check_auth()
    check_contrast()

    print("ok")


def rpc(root, *calls):
    """Drive the MCP server the way a client does: JSON-RPC lines in and out."""
    import io, json as _json
    lines = "\n".join(_json.dumps(c) for c in calls) + "\n"
    out = io.StringIO()
    mcp.serve(root, stdin=io.StringIO(lines), stdout=out)
    return [_json.loads(l) for l in out.getvalue().splitlines()]


def check_mcp(root):
    """An agent joins, learns something, and the next agent is told — over the
    wire, not by calling the functions directly."""
    init, listed = rpc(root,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert init["result"]["serverInfo"]["name"] == "prophecy"
    names = {t["name"] for t in listed["result"]["tools"]}
    assert {"join_repo_session", "share_finding", "check_overlap",
            "leave_repo_session"} <= names, names
    assert {"analyze_change", "get_repository_risk", "get_change_interactions",
            "get_dependency_context"} <= names, names
    for tool in listed["result"]["tools"]:
        assert tool["description"].strip(), tool["name"]

    def call(tool, **args):
        reply = rpc(root, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                           "params": {"name": tool, "arguments": args}})[0]
        assert "error" not in reply, reply
        return reply["result"]["content"][0]["text"]

    first = call("join_repo_session", agent="ada",
                 task="Add rate limiting to the API")
    assert "api/middleware.py" in first
    # the fixture already has branches, and a joining agent should hear about
    # them, not only about other live sessions
    assert "Also in flight" in first and "(branch)" in first, first[-400:]
    assert "ada" not in first.split("Also in flight")[1]  # not told about itself

    call("share_finding", agent="ada", file="api/middleware.py",
         finding="rate_limit is a stub that always returns True")

    second = call("join_repo_session", agent="grace",
                  task="Add rate limiting to the API")
    assert "always returns True" in second, "ada's finding did not reach grace"
    assert "Also in flight" in second
    tail = second.split("Also in flight")[1]
    assert "ada" in tail and "(session)" in tail  # the live agent, labelled
    assert "(branch)" in tail  # and the branches, still

    # a path the repo does not have is refused rather than recorded
    refused = call("share_finding", agent="ada", file="nope/nothing.py",
                   finding="...")
    assert "not a code file" in refused

    overlap = call("check_overlap", agent="grace")
    assert "ada" in overlap or "Overlapping" in overlap, overlap

    # a risk profile addressed to an agent rides back on its next call, whatever
    # that call happens to be, and is only handed over once
    db = store.connect(root)
    sent = store.queue_message(db, "sha", "grace", "Risk profile for signup",
                               "97/100 critical combined with ada's change")
    again = store.queue_message(db, "sha", "grace", "Risk profile for signup",
                                "97/100 critical combined with ada's change")
    assert again == sent, "the same unread profile was queued twice"
    delivered = call("check_overlap", agent="grace")
    assert "For you, from your team" in delivered, delivered[:200]
    assert "97/100 critical" in delivered
    assert "For you" not in call("check_overlap", agent="grace")  # not twice
    assert "For you" not in call("check_overlap", agent="ada")  # not to anyone else

    # reconnecting is the same session, not a second one
    db = store.connect(root)
    call("join_repo_session", agent="ada", task="Add rate limiting to the API")
    live = store.live_sessions(db)
    assert sorted(s["agent"] for s in live) == ["ada", "grace"], live

    call("leave_repo_session", agent="ada")
    assert [s["agent"] for s in store.live_sessions(db)] == ["grace"]

    from prophecy.mcp import client_name
    assert client_name("claude-code") == "Claude Code"
    assert client_name("cursor") == "Cursor"
    assert client_name("ChatGPT") == "ChatGPT"
    assert client_name("brand-new-agent") == "brand-new-agent"

    # handshake and join have to share a process, the way a real client does
    rpc(root,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"clientInfo": {"name": "claude-code", "version": "1.0"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "join_repo_session",
                    "arguments": {"agent": "ada", "task": "Add rate limiting"}}},
    )
    db = store.connect(root)
    ada = next(s for s in store.live_sessions(db) if s["agent"] == "ada")
    assert ada["client"] == "Claude Code", ada
    rpc(root, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
               "params": {"name": "join_repo_session",
                          "arguments": {"agent": "ada",
                                        "task": "Add rate limiting",
                                        "tool": "Cursor"}}})
    ada = next(s for s in store.live_sessions(db) if s["agent"] == "ada")
    assert ada["client"] == "Cursor", ada

    # an unknown method answers with an error, not a crash
    bad = rpc(root, {"jsonrpc": "2.0", "id": 3, "method": "nonsense"})[0]
    assert bad["error"]["code"] == -32000


def check_mcp_http(root):
    """An agent reaches Prophecy over HTTP, the way a remote client does."""
    import json as _json
    import threading
    import urllib.request
    from functools import partial
    from http.server import ThreadingHTTPServer

    from prophecy.mcp import Server, handle
    from prophecy.server import Handler

    listed = handle(Server(str(root)),
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "join_repo_session" in {t["name"] for t in listed["result"]["tools"]}

    httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                partial(Handler, repo=str(root)))
    httpd.mcp = Server(str(root))
    httpd.mcp_session = "test-session"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]
        url = f"http://127.0.0.1:{port}/mcp"

        def post(obj, extra=None):
            req = urllib.request.Request(
                url, data=_json.dumps(obj).encode(),
                headers={"Content-Type": "application/json", **(extra or {})},
                method="POST")
            with urllib.request.urlopen(req) as r:
                return _json.loads(r.read()), r.headers

        info = urllib.request.urlopen(url)
        about = _json.loads(info.read())
        assert about["name"] == "prophecy" and about["url"].endswith("/mcp")

        tools, headers = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert tools["result"]["tools"]
        assert headers.get("Mcp-Session-Id") == "test-session"
        assert headers.get("Access-Control-Allow-Origin") == "*"

        joined, _ = post({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "join_repo_session",
                       "arguments": {"agent": "ada", "task": "http test"}},
        })
        text = joined["result"]["content"][0]["text"]
        assert "api/middleware.py" in text, text[:400]
    finally:
        httpd.shutdown()
        httpd.server_close()


def check_sharing(root, repo):
    """What one agent works out, the next one is told — and the accounting
    only counts a prefix as reused when the bytes really did repeat."""
    db = store.connect(root)
    rate = prophecy.predict(repo, "Add rate limiting to the API")
    found = prophecy.risks(repo, [rate])

    first = agent.brief(repo, [rate], found, db=db, agent="agent-a", store=store)
    assert first["suffixes"][0]["notes_pulled"] == 0
    assert "other agents have already found" not in first["suffixes"][0]["text"]

    store.add_note(db, repo["sha"], "agent-a", "api/middleware.py",
                   "rate_limit returns True unconditionally; it is a stub")

    second = agent.brief(repo, [rate], found, db=db, agent="agent-b", store=store)
    assert second["suffixes"][0]["notes_pulled"] == 1
    assert "it is a stub" in second["suffixes"][0]["text"]
    assert "agent-a" in second["suffixes"][0]["text"]

    # an agent is never handed its own note back
    again = agent.brief(repo, [rate], found, db=db, agent="agent-a", store=store)
    assert again["suffixes"][0]["notes_pulled"] == 0, "agent-a got its own note"

    # the note rides in the volatile half; the cached half must not move
    assert first["prefix_hash"] == second["prefix_hash"] == again["prefix_hash"]
    assert first["prefix"] == second["prefix"]

    data = store.usage(db)
    assert data["briefs"] == 3 and data["agents"] == 2
    assert data["prefix_first_time"] == 1, data
    assert data["prefix_reused"] == 2, data
    assert data["tokens_avoided"] > 0
    assert data["notes_written"] == 1 and data["notes_pulled"] == 1
    assert "not confirmation that it did" in data["note"]


class StubProvider:
    """Answers with one real file and one that does not exist."""
    name, model = "stub", "stub-1"

    def __init__(self, payload):
        self.payload = payload
        self.seen = None

    def complete(self, stable, volatile, max_tokens=1500):
        self.seen = (stable, volatile)
        return self.payload


def check_llm(repo):
    """A model may not name a file into existence."""
    provider = StubProvider(
        'Sure! ```json\n{"files":['
        '{"path":"api/middleware.py","why":"throttling lives here","confidence":0.9},'
        '{"path":"api/ratelimit/redis_backend.py","why":"invented","confidence":0.8}'
        ']}\n```'
    )
    out = llm.semantic_predict(repo, "throttle incoming requests", provider)
    assert [f["file"] for f in out["files"]] == ["api/middleware.py"], out
    assert out["invented"] == ["api/ratelimit/redis_backend.py"], out
    assert "stub" in out["files"][0]["evidence"][0]
    assert out["files"][0]["source"] == "model"

    # the model was shown the real inventory, and the task went after it
    stable, volatile = provider.seen
    assert "api/middleware.py" in stable and "rate_limit" in stable
    assert "throttle" in volatile and "throttle" not in stable
    assert stable == llm.inventory(repo)  # byte-stable, so it stays cached

    # merging keeps repository evidence ahead of model inference
    lexical = prophecy.predict(repo, "Charge the customer an invoice")
    merged = llm.merge_forecasts(lexical, out)
    assert merged["files"][0]["source"] == "repo", merged["files"]
    assert merged["files"][-1]["source"] == "model"
    assert "1 invented path dropped" in merged["grounding"], merged["grounding"]

    # a model that answers with nothing usable is not an error
    empty = llm.semantic_predict(repo, "anything", StubProvider("no idea, sorry"))
    assert empty["files"] == [] and empty["invented"] == []


def check_brief(repo):
    """The cached half must be byte-identical between runs, and the task half
    must carry everything that differs."""
    rate = prophecy.predict(repo, "Add rate limiting to the API")
    auth = prophecy.predict(repo, "Add authentication middleware")
    found = prophecy.risks(repo, [rate, auth])
    data = agent.brief(repo, [rate, auth], found)

    # a prefix that differs between calls is a cache miss, every time
    again = agent.brief(repo, [rate, auth], found)
    assert data["prefix"] == again["prefix"]
    assert agent.stable_prefix(repo) == agent.stable_prefix(repo)

    # the stable half says nothing about either task
    assert "rate limiting" not in data["prefix"]
    assert "authentication" not in data["prefix"]
    assert "api/middleware.py" in data["prefix"]  # it is load-bearing

    # the volatile half carries the task, its files and its coordination
    first = data["suffixes"][0]
    assert first["task"] == "Add rate limiting to the API"
    assert "rate_limit(request, limit=...)" in first["text"]
    # the agent is told to work from the slice rather than open the files, and
    # that instruction is per-task, so it must sit in the suffix
    assert "Only open the rest of a file" in first["text"]
    assert "Only open the rest of a file" not in data["prefix"]
    assert "Agree on who owns" in first["text"]
    assert data["suffixes"][1]["text"] != first["text"]

    # this fixture is tiny, so it cannot clear the cache floor and must say so
    assert not data["cacheable"]
    assert data["warnings"] and "do not apply" in data["warnings"][0]

    # the request puts the breakpoint on the repo half, not the task half
    req = agent.request_skeleton(data)
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["system"][0]["text"] == data["prefix"]
    assert req["messages"][0]["content"] == first["text"]
    assert "cache_control" not in req["messages"][0]


def check_backfill(root):
    """Land a real conflicting merge, then replay it and check we would have
    called it in advance, without the replay ever seeing the merge commit."""
    run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True,
                                    capture_output=True)
    target = root / "api/middleware.py"

    run("checkout", "-q", "limits")
    merged = subprocess.run(["git", "-C", str(root), "merge", "--no-commit", "buckets"],
                            capture_output=True, text=True)
    assert merged.returncode != 0, "fixture should conflict"
    target.write_text("def authenticate(request, token):\n    return bool(token)\n\n"
                      "def rate_limit(request, limit, window, bucket):\n    return True\n")
    run("add", "-A")
    run("commit", "-qm", "merge buckets into limits")
    run("checkout", "-q", "main")

    found = backfill.merge_commits(root, 10, ref="limits")
    assert len(found) == 1, found

    result = backfill.replay_one(root, found[0])
    assert result["conflicted"] == ["api/middleware.py"], result
    assert result["caught"] == ["api/middleware.py"], result
    assert not result["missed"], result
    assert result["levels"]["api/middleware.py"] in ("medium", "high"), result
    assert result["lead_seconds"] >= 0

    # a sample this small must refuse to produce a rate
    summary = backfill.report([result])
    assert summary["sample_too_small"]
    assert "anecdote" in summary["verdict"], summary
    assert "sanity check" in summary["recall_note"]

    # with enough runs it reports, and it calls an inverted score inverted
    inverted = [dict(result, merge=f"x{i}", conflicted=["a.py"], caught=["a.py"],
                     levels={"a.py": "medium", "b.py": "high"},
                     flagged_no_conflict=["b.py"], predicted_files=["a.py", "b.py"])
                for i in range(12)]
    summary = backfill.report(inverted)
    assert not summary["sample_too_small"]
    assert summary["by_level"]["high (code)"]["rate"] == 0.0
    assert summary["by_level"]["medium (code)"]["rate"] == 1.0
    assert "inverted" in summary["verdict"], summary["verdict"]

    # lockfiles and changelogs are kept out of the source-file comparison, so a
    # changelog that conflicts every time cannot make the score look broken
    noisy = [dict(result, merge=f"y{i}", conflicted=["CHANGES.rst"],
                  caught=["CHANGES.rst"], predicted_files=["CHANGES.rst", "b.py"],
                  levels={"CHANGES.rst": "medium", "b.py": "high"},
                  flagged_no_conflict=["b.py"])
             for i in range(12)]
    summary = backfill.report(noisy)
    assert summary["by_level"]["medium (non-code)"]["rate"] == 1.0
    assert "inverted" not in summary["verdict"], summary["verdict"]
    assert "Only one risk level" in summary["verdict"], summary["verdict"]


def check_branches(root, repo):
    """Two branches that edit the same signature: forecast it, then merge it
    for real and see whether the forecast was right."""
    run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True,
                                    capture_output=True)
    target = root / "api/middleware.py"
    original = target.read_text()

    run("checkout", "-qb", "limits", "main")
    target.write_text(original.replace("def rate_limit(request, limit=100):",
                                       "def rate_limit(request, limit, window):"))
    run("commit", "-qam", "window argument")

    run("checkout", "-qb", "buckets", "main")
    target.write_text(original.replace("def rate_limit(request, limit=100):",
                                       "def rate_limit(request, bucket):"))
    run("commit", "-qam", "bucket argument")
    run("checkout", "-q", "main")

    found = branches.branches(root, "main")
    assert set(found) == {"limits", "buckets"}, found
    limits = found["limits"]
    assert limits["ahead"] == 1 and limits["behind"] == 0, limits
    assert limits["signature_changes"] == [{
        "file": "api/middleware.py", "symbol": "rate_limit",
        "before": "rate_limit(request, limit=...)",
        "after": "rate_limit(request, limit, window)",
        "kind": "changed",
    }], limits["signature_changes"]

    # a signature change with callers is a risk on its own, no second task needed
    forecasts = [branches.as_forecast(b) for b in found.values()]
    observed = prophecy.risks(repo, forecasts)
    sig = [r for r in observed if r["risk_type"] == "api_signature_change"]
    assert len(sig) == 2, observed
    assert "caller" in sig[0]["recommendation"], sig[0]

    # a symbol that leaves one file and lands in another moved; it was not
    # deleted, and a refactor must not read as a pile of removals
    run("checkout", "-qb", "relocate", "main")
    (root / "api/limits.py").write_text(
        "def rate_limit(request, limit=100):\n    return True\n")
    (root / "api/middleware.py").write_text(
        "def authenticate(request, token):\n    return bool(token)\n")
    run("add", "-A")
    run("commit", "-qm", "move rate_limit out of middleware")
    run("checkout", "-q", "main")

    moved = branches.branch(root, "relocate", "main")["signature_changes"]
    assert [c["kind"] for c in moved] == ["moved"], moved
    assert moved[0]["moved_to"] == "api/limits.py", moved
    assert "moved to api/limits.py" in moved[0]["after"]

    # and the two branches really do collide, in the file we said they would
    outcome = merge.trial_merge(root, "limits", "buckets")
    assert not outcome["merged_clean"], outcome
    assert outcome["conflicted_files"] == ["api/middleware.py"], outcome
    result = merge.compare(observed, outcome, forecasts)
    assert result["predicted_and_conflicted"] == ["api/middleware.py"], result
    assert not result["conflicted_unpredicted"], result

    # merging one branch alone is clean, and we say so without claiming a win
    clean = merge.trial_merge(root, "main", "limits")
    assert clean["merged_clean"], clean
    assert "never the claim" in " ".join(
        merge.compare(observed, clean, forecasts)["notes"])

    # the worktree is gone afterwards, whatever happened
    worktrees = subprocess.run(["git", "-C", str(root), "worktree", "list"],
                               capture_output=True, text=True).stdout
    assert worktrees.count("\n") == 1, worktrees

    # risks survive a round trip, so `explain <id>` works on a later run
    db = store.connect(root)
    store.save_risks(db, observed)
    back = store.get_risk(db, sig[0]["id"])
    assert back["evidence"] == sig[0]["evidence"], back
    assert store.get_risk(db, "Rnope") is None
    store.save_outcome(db, repo, outcome, result)
    assert store.insights(db)["merges_run"] == 1
    # a merge run by hand is not history; only backfill can grade the forecast
    assert store.insights(db)["merges_replayed"] == 0
    assert "no history replayed yet" in store.insights(db)["accuracy"]
    store.save_outcome(db, repo, outcome, result, source="backfill")
    assert store.insights(db)["merges_run"] == 1
    assert store.insights(db)["merges_replayed"] == 1




def check_auth():
    """The gate, from outside: what an unsigned request can reach.

    Every line here is a way in that should not be one. The GitHub half is
    not exercised — that needs their servers — so this drives the sessions
    the callback would have created and checks everything downstream of it.
    """
    import datetime
    import threading
    import urllib.error
    import urllib.request
    from functools import partial
    from http.server import ThreadingHTTPServer

    from prophecy import auth, store
    from prophecy.mcp import Server as McpServer
    from prophecy.server import Handler

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "app"
        assert "error" not in demo.build(root)

        access = {"client_id": "id", "client_secret": "secret",
                  "owner": "ada", "redirect_uri": ""}
        httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(Handler, repo=str(root), access=access))
        httpd.mcp = McpServer(str(root))
        httpd.mcp_session = "test"
        port = httpd.socket.getsockname()[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        def call(path, body=None, headers=None, cookie=None):
            url = f"http://127.0.0.1:{port}{path}"
            data = json.dumps(body).encode() if body is not None else None
            request = urllib.request.Request(
                url, data=data, method="POST" if data else "GET")
            if data:
                request.add_header("Content-Type", "application/json")
            for key, value in (headers or {}).items():
                request.add_header(key, value)
            if cookie:
                request.add_header("Cookie", f"prophecy_session={cookie}")
            try:
                with urllib.request.urlopen(request, timeout=15) as answer:
                    return answer.status, json.loads(answer.read() or b"{}")
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                try:
                    return exc.code, json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    return exc.code, {}

        db = store.connect(root)
        expires = (datetime.datetime.now(datetime.timezone.utc)
                   + datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")

        def sign_in(login, role):
            person = {"login": login, "github_id": 1, "name": login,
                      "avatar": "", "orgs": [], "teams": []}
            account_id = store.upsert_account(db, person, role)
            token = auth.new_session_token()
            store.start_session(db, auth.hash_token(token), account_id, expires)
            return token

        # nothing at all without a session, read or write
        status, body = call("/api/risk")
        assert status == 401 and body["code"] == "not_signed_in", body
        assert call("/api/save", {"path": "a.py", "content": "x"})[0] == 401

        owner = sign_in("ada", "owner")
        status, me = call("/api/me", cookie=owner)
        assert status == 200 and me["role"] == "owner" and me["may_write"], me

        # a session alone is not enough to write: a cookie travels on any
        # request, the header only on one this page made
        assert call("/api/save", {"path": "app/models.py", "content": "# x"},
                    cookie=owner)[1]["code"] == "bad_csrf"
        csrf = {"X-Prophecy-CSRF": me["csrf"]}
        assert call("/api/save", {"path": "app/models.py",
                                  "content": "# written\n"},
                    csrf, cookie=owner)[0] == 200

        # expired is its own answer: sign in again, not ask for access
        stale = auth.new_session_token()
        store.start_session(db, auth.hash_token(stale),
                            store.accounts(db)[0]["id"], "2020-01-01 00:00:00")
        assert call("/api/risk", cookie=stale)[1]["code"] == "session_expired"

        viewer = sign_in("vic", "viewer")
        _, vme = call("/api/me", cookie=viewer)
        assert call("/api/save", {"path": "app/models.py", "content": "# no"},
                    {"X-Prophecy-CSRF": vme["csrf"]},
                    cookie=viewer)[1]["code"] == "read_only"
        assert call("/api/access", cookie=viewer)[1]["code"] == "not_allowed"

        # an agent cannot sign in, so it carries a token belonging to a person
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert call("/mcp", rpc)[0] == 401
        assert call("/mcp", rpc, {"Authorization": "Bearer nonsense"})[0] == 401
        _, issued = call("/api/agent_token", {"label": "ada's agent"},
                         csrf, cookie=owner)
        carried = {"Authorization": "Bearer " + issued["token"]}
        status, answer = call("/mcp", rpc, carried)
        assert status == 200 and len(answer["result"]["tools"]) == 8, answer

        # and it cannot file its work under somebody else's name: the token
        # says whose agent this is, the argument is a string it chose
        call("/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "join_repo_session",
                                 "arguments": {"agent": "vic",
                                               "task": "impersonation"}}},
             carried)
        live = [s["agent"] for s in store.live_sessions(store.connect(root))]
        assert "ada" in live, live
        assert "vic" not in live, live

        httpd.shutdown()


def check_contrast():
    """Text has to be readable, and a colour is easy to darken by accident.

    WCAG AA is 4.5:1 for normal text. The cards are the harder surface, so
    they are what this measures against.
    """
    import re

    page = (Path(__file__).resolve().parent.parent
            / "prophecy" / "dashboard.html").read_text()

    def luminance(colour):
        colour = colour.lstrip("#")
        channels = [int(colour[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        channels = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
                    for c in channels]
        return (0.2126 * channels[0] + 0.7152 * channels[1]
                + 0.0722 * channels[2])

    def contrast(one, two):
        a, b = luminance(one), luminance(two)
        return (max(a, b) + 0.05) / (min(a, b) + 0.05)

    def token(name):
        found = re.search(rf"--{name}:\s*(#[0-9A-Fa-f]{{6}})", page)
        assert found, f"no --{name} in the stylesheet"
        return found.group(1)

    card = token("card")
    for name in ("text", "text-2", "text-3"):
        ratio = contrast(token(name), card)
        assert ratio >= 4.5, f"--{name} is {ratio:.2f}:1 on the cards, under AA"


def check_clone_urls():
    """What the clone endpoint will and will not fetch.

    This one takes a URL from whoever is looking at the page and hands it to
    git, so the refusals matter more than the acceptances: a local path, a
    file URL or git's ext:: transport are all ways to make it do something
    other than fetch a repository.
    """
    from prophecy.clone import normalise

    assert normalise("pallets/flask") == "https://github.com/pallets/flask"
    assert normalise("github.com/pallets/flask") == "https://github.com/pallets/flask"
    assert normalise("https://gitlab.com/a/b.git") == "https://gitlab.com/a/b.git"
    assert normalise("git@github.com:a/b.git") == "git@github.com:a/b.git"

    for hostile in ("/etc", "file:///etc/passwd", "../../etc/passwd",
                    "ext::sh -c whoami", "https://host/a b", "",
                    "  ", "./local/path"):
        assert normalise(hostile) is None, hostile


def check_risk_engine():
    """The four scenarios from the brief, against a repository built for them.

    The one that matters most is the last: a tool that calls everything risky
    is no more useful than one that calls nothing risky.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "app"
        built = demo.build(root)
        assert "error" not in built, built
        repo = prophecy.scan(root)

        # 1 — a field stops being optional while callers still omit it
        a = risk_engine.analyze_change(repo, "main", "agent-a/require-email",
                                       label="A", agent="ada")
        assert a["risk_band"] == "critical", a["risk_score"]
        assert a["risk_range"]["min"] < a["risk_score"] < a["risk_range"]["max"]
        titles = " ".join(f["title"] for f in a["potential_failures"])
        assert "create_user no longer accepts a missing email" in titles, titles
        hit = next(f for f in a["potential_failures"] if "create_user" in f["title"])
        assert "api/signup.py" in hit["affected"], hit
        assert hit["severity"] == "critical"
        # every score carries the evidence that produced it
        assert a["criticality_evidence"] and a["uncertainty"]
        assert 0 < a["confidence"] <= 0.92

        # 2 — the message says one thing, the diff does another
        contradicted = risk_engine.analyze_change(
            repo, "main", "agent-a/require-email", label="A",
            stated_intent="make email optional during signup")
        assert contradicted["intent_contradictions"], contradicted["intent"]

        # 3 — two changes that are quiet alone and loud together
        b = risk_engine.analyze_change(repo, "main", "agent-b/signup-redesign",
                                       label="B", agent="grace")
        c = risk_engine.analyze_change(repo, "main", "agent-c/user-cleanup",
                                       label="C", agent="linus")
        assert b["risk_band"] == "low" and c["risk_band"] == "low", (b, c)
        found = risk_engine.interactions([a, b, c])
        assert found, "no interaction found between three related changes"
        top = found[0]
        assert top["combined_score"] > max(top["individual"])
        assert top["escalates"], top
        assert top["evidence"], top
        # they meet without editing the same file, which is the whole point
        pair = next((i for i in found if not i["shared_files"]), None)
        assert pair and pair["meeting_points"], found
        # one row, one reason: the contract line already says one side
        # changes storage, so nothing repeats it
        for i in found:
            assert len(set(i["evidence"])) == len(i["evidence"]), i
            stored = [e for e in i["evidence"] if "how it is stored" in e
                      or "what is stored" in e]
            assert len(stored) <= 1, i["evidence"]

        # a is in every pair, so it is named once with the others under it
        groups = risk_engine.meet_groups(found)
        assert [g["hub"] for g in groups].count("A") == 1, groups
        hub = next(g for g in groups if g["hub"] == "A")
        assert len(hub["pairs"]) >= 2, groups
        # every pair lands in exactly one group, and none is lost
        flat = [n for g in groups for n in g["pairs"]]
        assert sorted(flat) == list(range(len(found))), groups

        overall = risk_engine.repository_risk([a, b, c], found)
        assert overall["band"] == "critical", overall
        assert overall["drivers"]

        # 4 — an isolated change stays quiet
        run = lambda *args: subprocess.run(["git", "-C", str(root), *args],
                                           check=True, capture_output=True)
        run("checkout", "-qb", "quiet", "main")
        (root / "README.md").write_text("# demo app\n\nA sentence.\n")
        run("add", "-A")
        run("commit", "-qm", "Reword the readme")
        run("checkout", "-q", "main")
        quiet = risk_engine.analyze_change(repo, "main", "quiet", label="quiet")
        assert quiet["risk_band"] == "low", quiet
        assert not quiet["potential_failures"], quiet["potential_failures"]

        # the dependency picture behind the score
        layers = risk_engine.blast_radius(repo, ["app/models.py"])
        assert "api/signup.py" in layers[0]
        crit, signals = risk_engine.criticality(repo, "app/models.py", layers)
        assert crit > 0 and signals

def check_hints(repo):
    """Signals the caller already has, for tasks the words alone would miss."""
    task = "Add rate limiting to the API"

    # without hints nothing moves: the same files, ranked the same way
    plain = prophecy.predict(repo, task)
    assert plain["files"][0]["file"] == "api/middleware.py", plain["files"]
    before = [f["file"] for f in plain["files"]]
    assert prophecy.predict(repo, task, hints=None)["files"] == plain["files"]
    assert "billing/invoice.py" not in before, before

    # a file the task shares no words with still lands, because it is dirty
    dirty = prophecy.predict(repo, task,
                             hints={"dirty": ["billing/invoice.py"]})
    hit = next((f for f in dirty["files"] if f["file"] == "billing/invoice.py"),
               None)
    assert hit, [f["file"] for f in dirty["files"]]
    assert any("uncommitted" in e for e in hit["evidence"]), hit["evidence"]
    assert not hit["lexical"]

    # so does one another session has claimed
    claimed = prophecy.predict(repo, task,
                               hints={"claimed": ["billing/invoice.py"]})
    hit = next(f for f in claimed["files"] if f["file"] == "billing/invoice.py")
    assert any("already in this file" in e for e in hit["evidence"]), hit

    # dirty outranks merely recent, so git log does not fill the slice. Said
    # on a task the words miss entirely, because when the words do hit they
    # are meant to win: api/handlers.py matching "API" is not a tie to break.
    neutral = prophecy.predict(repo, "zzzz qqqq", hints={
        "dirty": ["billing/invoice.py"], "recent": ["api/handlers.py"]})
    order = [f["file"] for f in neutral["files"]]
    assert order.index("billing/invoice.py") < order.index("api/handlers.py"), order

    # and a lexical hit still outranks a file that is only recent
    lexical_wins = prophecy.predict(repo, task,
                                    hints={"recent": ["billing/invoice.py"]})
    ranked = [f["file"] for f in lexical_wins["files"]]
    assert ranked[0] == "api/middleware.py", ranked

    # a path this repository does not have is dropped, not guessed at
    bogus = prophecy.predict(repo, task, hints={"dirty": ["nope/gone.py"]})
    assert [f["file"] for f in bogus["files"]] == before

    # words set the ceiling: a signal never inflates a forecast the words
    # already made
    assert dirty["confidence"] == plain["confidence"]

    # but a forecast carried entirely by signals is not zero-confidence, and
    # it stays low enough that nobody acts on it without looking
    signal_only = prophecy.predict(repo, "zzzz qqqq",
                                   hints={"dirty": ["billing/invoice.py"]})
    assert signal_only["files"], signal_only
    assert 0.3 < signal_only["confidence"] < 0.5, signal_only["confidence"]


def check_slice_and_brevity(root):
    """The agent is handed a slice, and a short answer unless it asks."""
    reply = rpc(root, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "get_dependency_context",
                                  "arguments": {"path": "api/middleware.py"}}})[0]
    slice_text = reply["result"]["content"][0]["text"]
    assert "rate_limit(request, limit=...)" in slice_text, slice_text
    assert "(line " in slice_text, slice_text
    assert "Open the file itself only if" in slice_text

    def analyze(**extra):
        out = rpc(root, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": "analyze_change", "arguments": dict(
                             {"head": "limits", "base": "main"}, **extra)}})[0]
        return out["result"]["content"][0]["text"]

    short, long = analyze(), analyze(detail=True)
    assert len(short) < len(long), (len(short), len(long))
    assert "risk" in short and "/100" in short
    assert "detail: true" in short
    # the reasoning is what costs tokens on every later turn, so it is the
    # part that waits to be asked for
    assert "What could break" not in short
    assert "Not known" not in short
    assert "What could break" in long and "Not known" in long


if __name__ == "__main__":
    main()
