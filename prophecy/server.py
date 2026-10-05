"""A dashboard, served by the standard library.

The same functions the CLI calls, behind a few JSON endpoints, plus one
static HTML file. No build step, no node_modules, nothing to install.
"""

import datetime
import hmac
import json
import os
import socket
import subprocess
import traceback
import uuid
from argparse import Namespace
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import auth as auth_mod
from . import store
from .errors import (BadRequest, NotAllowed, NotFound, NotSignedIn, ProphecyError,
                     ReadOnly, SessionExpired, internal)
from .scan import scan

def _escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


PAGE = Path(__file__).parent / "dashboard.html"
LOGO = Path(__file__).parent / "logo.png"
DEMO = Path(__file__).parent / "mcp-demo.html"


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, repo=".", public=False, open_served=False,
                 access=None, **kwargs):
        self.repo_path = repo
        # None when nobody has to sign in, which is the default and what a
        # laptop and the demo container want.
        self.access = access
        # open the repository it was started for, instead of asking
        self.open_served = open_served
        # Served to strangers: the repo is fixed and nothing writes. Every
        # endpoint here otherwise acts as the person who started the server.
        self.public = public
        super().__init__(*args, **kwargs)

    def log_message(self, *args):
        pass  # the dashboard polls; the default log is just noise

    # ── who is asking ────────────────────────────────────────────────────

    def _redirect(self, target):
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _secret(self):
        return auth_mod.secret(self.repo_path)

    def _secure_request(self):
        """Whether the cookie should insist on https.

        Behind a proxy that terminates TLS the connection here is plain, so
        the header it sets is the only evidence. Local http is the one case
        where a Secure cookie would simply never come back.
        """
        forwarded = (self.headers.get("X-Forwarded-Proto") or "").lower()
        if forwarded:
            return forwarded == "https"
        host = (self.headers.get("Host") or "").split(":")[0]
        return host not in ("localhost", "127.0.0.1", "::1", "")

    def _session(self):
        """The signed-in person, or None. Raises when a session has expired,
        because that needs different words than never having signed in."""
        if not self.access:
            return None
        token = auth_mod.read_cookie(self.headers.get("Cookie"))
        if not token:
            return None
        db = store.connect(self.repo_path)
        found = store.session_account(db, auth_mod.hash_token(token))
        if not found:
            return None
        if not found.get("live"):
            store.end_session(db, auth_mod.hash_token(token))
            raise SessionExpired()
        found["session_token"] = token
        return found

    def _require(self, write=False):
        """The person, having checked they may do this. The whole gate."""
        if not self.access:
            return None
        person = self._session()
        if not person:
            raise NotSignedIn()
        if write and not auth_mod.may_write(person["role"]):
            raise ReadOnly()
        return person

    def _check_csrf(self, person):
        """A cookie travels on any request; a header only travels on ours.

        Without this, another site can make your browser POST here with your
        session attached, and the write succeeds.
        """
        if not self.access or not person:
            return
        sent = self.headers.get("X-Prophecy-CSRF") or ""
        want = auth_mod.csrf_token(self._secret(), person["session_token"])
        # compare_digest rather than !=, which returns as soon as two
        # strings differ and so leaks how much of a guess was right
        if not sent or not hmac.compare_digest(sent, want):
            raise BadRequest(
                "That request did not carry a valid token for this session.",
                hint="Reload the page and try again.", code="bad_csrf")

    def _fail(self, error):
        """One shape for every failure the page has to understand."""
        if not isinstance(error, ProphecyError):
            traceback.print_exc()
            error = internal()
        return self._send(error.status, "application/json",
                          json.dumps(error.payload()).encode())

    # ── signing in ───────────────────────────────────────────────────────

    def _auth_route(self, url):
        config = self.access or {}
        here = f"{'https' if self._secure_request() else 'http'}://" \
               f"{self.headers.get('Host') or '127.0.0.1'}"
        redirect_uri = config.get("redirect_uri") or f"{here}/auth/callback"

        if url.path == "/auth/login":
            state = auth_mod.make_state(self._secret())
            self.send_response(302)
            self.send_header("Location", auth_mod.authorize_url(
                config["client_id"], redirect_uri, state))
            # the state is checked on the way back, so it has to survive the
            # round trip somewhere the callback can read
            self.send_header("Set-Cookie",
                             f"prophecy_state={state}; Path=/; HttpOnly; "
                             f"SameSite=Lax; Max-Age=600"
                             + ("; Secure" if self._secure_request() else ""))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if url.path == "/auth/logout":
            token = auth_mod.read_cookie(self.headers.get("Cookie"))
            if token:
                db = store.connect(self.repo_path)
                store.end_session(db, auth_mod.hash_token(token))
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", auth_mod.cookie_header(
                "", self._secure_request(), max_age=0))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if url.path == "/auth/callback":
            return self._auth_callback(url, config, redirect_uri)

        return self._send(404, "text/plain", b"not found")

    def _auth_callback(self, url, config, redirect_uri):
        query = parse_qs(url.query)
        if query.get("error"):
            return self._signin_page(
                "GitHub did not complete the sign in.",
                query.get("error_description", [""])[0])

        cookie_state = ""
        for piece in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = piece.strip().partition("=")
            if name == "prophecy_state":
                cookie_state = value
        sent_state = query.get("state", [""])[0]
        if (not sent_state or sent_state != cookie_state
                or not auth_mod.check_state(self._secret(), sent_state)):
            return self._signin_page(
                "That sign in link was not one this page started.",
                "Start again from the sign in button.")

        code = query.get("code", [""])[0]
        if not code:
            return self._signin_page("GitHub sent no code back.", "")

        got = auth_mod.exchange_code(
            config["client_id"], config["client_secret"], code, redirect_uri)
        if got.get("error"):
            return self._signin_page("GitHub refused the sign in.",
                                     got["error"])

        person = auth_mod.identity(got["token"])
        if person.get("error") or not person.get("login"):
            return self._signin_page("Could not read your GitHub account.",
                                     person.get("error", ""))

        db = store.connect(self.repo_path)
        rules = store.access_rules(db)
        role = auth_mod.role_for(person, rules, config.get("owner", ""))
        if not role:
            return self._signin_page(
                f"{person['login']} does not have access to this Prophecy.",
                "Whoever runs it can add you, your team or your "
                "organisation.", status=403)

        account_id = store.upsert_account(db, person, role)
        token = auth_mod.new_session_token()
        expires = (datetime.datetime.now(datetime.timezone.utc)
                   + datetime.timedelta(days=auth_mod.SESSION_DAYS))
        store.start_session(db, auth_mod.hash_token(token), account_id,
                            expires.strftime("%Y-%m-%d %H:%M:%S"),
                            self.headers.get("User-Agent", ""))
        store.sweep_sessions(db)

        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie",
                         auth_mod.cookie_header(token, self._secure_request()))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _signin_page(self, headline, detail="", status=200):
        """The one page somebody sees before they are anybody."""
        body = f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Prophecy</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-content: center;
         gap: 15px; text-align: center; padding: 24px; background: #07090d;
         color: #e8ecf2; font: 15px/1.6 ui-sans-serif, system-ui, sans-serif; }}
  h1 {{ font-size: 21px; font-weight: 600; margin: 0; letter-spacing: -.02em; }}
  p {{ margin: 0; color: #9aa4b2; max-width: 44ch; }}
  a {{ display: inline-block; margin-top: 8px; padding: 10px 18px;
      border: 1px solid #7DBBFF; border-radius: 8px; color: #7DBBFF;
      text-decoration: none; }}
</style>
<h1>{_escape(headline)}</h1>
{f'<p>{_escape(detail)}</p>' if detail else ''}
<a href="/auth/login">Sign in with GitHub</a>
"""
        return self._send(status, "text/html", body.encode())

    def do_GET(self):
        url = urlparse(self.path)
        if url.path.startswith("/auth/"):
            if not self.access:
                return self._send(404, "text/plain", b"not found")
            try:
                return self._auth_route(url)
            except ProphecyError as exc:
                return self._fail(exc)
            except Exception:
                traceback.print_exc()
                return self._fail(internal())
        if url.path in ("/", "/index.html"):
            # An anonymous visitor is shown a door, not a dashboard that
            # fails one request at a time behind their back.
            if self.access:
                try:
                    if not self._session():
                        return self._signin_page(
                            "Sign in to Prophecy",
                            "This instance is for the people who work on "
                            "this repository.")
                except SessionExpired:
                    return self._signin_page(
                        "Your session has expired.",
                        "Sign in again to pick up where you were.")
            # Started for one repository and told to open it: the page reads
            # ?repo= on load, so saying so in the URL is the whole change.
            # Container and one-box installs want this; a dashboard on your
            # laptop does not, because which repo you meant is a real choice.
            if self.open_served and not url.query:
                target = "/?repo=" + quote(self.repo_path)
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._send(200, "text/html", PAGE.read_bytes())
        if url.path == "/logo.png":
            return self._send(200, "image/png", LOGO.read_bytes())
        if url.path == "/mcp-demo.html":
            return self._send(200, "text/html", DEMO.read_bytes())
        if url.path == "/mcp":
            return self._mcp_get()
        if not url.path.startswith("/api/"):
            return self._send(404, "text/plain", b"not found")

        query = {k: v for k, v in parse_qs(url.query).items()}
        try:
            person = self._require()
            payload = self._call(url.path[5:], query, person)
        except ProphecyError as exc:
            return self._fail(exc)
        except Exception:
            return self._fail(internal())
        return self._send(200, "application/json",
                          json.dumps(payload, default=str).encode())

    def do_OPTIONS(self):
        if urlparse(self.path).path == "/mcp":
            return self._send(204, "text/plain", b"", cors=True)
        return self._send(404, "text/plain", b"not found")

    def do_POST(self):
        """MCP is JSON-RPC in the body; file edits are too big for a query string."""
        url = urlparse(self.path)
        if url.path == "/mcp":
            return self._mcp_post()
        if not url.path.startswith("/api/"):
            return self._send(404, "text/plain", b"not found")
        try:
            size = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(size) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, "application/json",
                              json.dumps({"error": "unreadable request"}).encode())
        try:
            person = self._require(write=True)
            self._check_csrf(person)
            payload = self._write(url.path[5:], body, person)
        except ProphecyError as exc:
            return self._fail(exc)
        except Exception:
            return self._fail(internal())
        return self._send(200, "application/json",
                          json.dumps(payload, default=str).encode())

    def _write(self, name, body, person=None):
        """The endpoints that change the repository rather than read it."""
        from . import workspace
        if self.public:
            raise ReadOnly("change anything on this instance",
                           code="instance_read_only")
        if name in ("access_add", "access_drop"):
            return self._access_change(name, body, person)
        if name in ("agent_token", "revoke_agent_token"):
            return self._agent_token(name, body, person)
        if name == "clone":
            # not about a repo we already have, so it is handled before the
            # path check the others need
            from .clone import clone
            return clone(body.get("url", ""))
        path = (body.get("repo") or self.repo_path or "").strip()
        problem = _not_a_repo(path)
        if problem:
            return {"error": problem}
        if name == "save":
            return workspace.write_file(scan(path), body.get("path", ""),
                                        body.get("content", ""))
        if name == "commit":
            return workspace.commit(path, body.get("paths") or [],
                                    body.get("message", ""))
        if name == "revert":
            return workspace.revert_file(path, body.get("path", ""))
        return {"error": f"unknown command {name}"}

    def _me(self, person):
        """What the page needs to know about the person looking at it."""
        if not self.access:
            # nobody signs in, so everybody may do everything, and the page
            # should not draw an account menu for a person who does not exist
            return {"auth": False, "role": "owner", "may_write": True}
        if not person:
            raise NotSignedIn()
        db = store.connect(self.repo_path)
        return {
            "auth": True,
            "login": person["login"],
            "name": person["name"],
            "avatar": person["avatar"],
            "role": person["role"],
            "may_write": auth_mod.may_write(person["role"]),
            "csrf": auth_mod.csrf_token(self._secret(),
                                        person["session_token"]),
            "tokens": [
                {k: t[k] for k in ("label", "created_at", "last_used", "revoked")}
                for t in store.agent_tokens(db, person["id"])
            ],
        }

    def _access_change(self, name, body, person):
        """Owners decide who else gets in."""
        if self.access and (not person or person["role"] != "owner"):
            raise NotAllowed("Only an owner can change who has access.")
        kind = (body.get("kind") or "").strip().lower()
        value = (body.get("value") or "").strip()
        if kind not in ("user", "org", "team"):
            raise BadRequest("Access is granted to a user, an org or a team.")
        if not value:
            raise BadRequest(f"Name the {kind} to grant access to.")
        db = store.connect(self.repo_path)
        if name == "access_drop":
            store.drop_access_rule(db, kind, value)
        else:
            role = (body.get("role") or "member").strip().lower()
            if role not in auth_mod.ROLES:
                raise BadRequest(
                    "A role is owner, member or viewer.",
                    hint="member can edit and commit; viewer can only read.")
            store.add_access_rule(db, kind, value, role,
                                  person["login"] if person else "")
        return {"rules": store.access_rules(db)}

    def _agent_token(self, name, body, person):
        """A token an agent carries, belonging to a person.

        An agent cannot sign in through a browser, so this is how its work
        gets attributed to somebody rather than to nobody.
        """
        if not self.access or not person:
            raise BadRequest(
                "This instance does not require agents to sign in.",
                hint="Start it with --auth github to issue tokens.")
        db = store.connect(self.repo_path)
        if name == "revoke_agent_token":
            store.revoke_agent_token(db, body.get("token_hash", ""),
                                     person["id"])
            return {"tokens": store.agent_tokens(db, person["id"])}
        token = auth_mod.new_session_token()
        store.issue_agent_token(db, auth_mod.hash_token(token), person["id"],
                                body.get("label") or "an agent")
        # the only time the token itself exists outside the agent that will
        # carry it: it is hashed the moment it is stored
        return {"token": token, "tokens": store.agent_tokens(db, person["id"])}

    def _call(self, name, query, person=None):
        if name == "me":
            return self._me(person)
        if name == "access":
            if self.access and (not person or person["role"] != "owner"):
                raise NotAllowed(
                    "Only an owner can see who has access here.")
            db = store.connect(self.repo_path)
            return {"rules": store.access_rules(db),
                    "people": store.accounts(db)}
        from .cli import COMMANDS
        # The dashboard can point at any repo on this machine. The server is
        # bound to localhost and acts as the person running it, so the check
        # here is for a useful error message, not for isolation.
        # A public instance answers for its own repository and nothing else;
        # the parameter is how you would otherwise read any repo on the host.
        path = (self.repo_path if self.public
                else (query.get("repo", [""])[0] or self.repo_path)).strip()
        problem = _not_a_repo(path)
        if problem:
            return {"error": problem}
        repo = scan(path)
        if name == "file":
            return _file_detail(repo, query.get("path", [""])[0])
        if name == "setup":
            return _setup_state(path)
        if name == "new":
            from .create import new_project
            return new_project(
                query.get("path", [""])[0],
                query.get("name", [None])[0],
                query.get("github", [None])[0] or None,
                query.get("private", ["1"])[0] != "0",
            )
        if name == "sample":
            return _sample_project()
        if name == "repos":
            return {"repos": _discover_repos()}
        if name == "mcp_config":
            from .mcp import config_snippet
            token = os.environ.get("PROPHECY_MCP_TOKEN", "")
            url = self._public_mcp_url()
            return {"config": config_snippet(path, url=url, token=token),
                    "url": url, "repo": path}
        if name == "notify":
            return _notify_agent(
                path, repo,
                query.get("agent", [""])[0],
                query.get("target", [""])[0],
                query.get("base", ["main"])[0],
            )
        if name == "read":
            from . import workspace
            return workspace.read_file(repo, query.get("path", [""])[0])
        if name == "worktree":
            from . import workspace
            return workspace.worktree(path)
        if name == "filediff":
            from . import workspace
            return {"diff": workspace.diff_file(path, query.get("path", [""])[0])}
        if name == "sharing":
            return store.sharing(store.connect(path))
        if name == "messages":
            db = store.connect(path)
            return {
                "messages": store.messages(db),
                "live": [s["agent"] for s in store.live_sessions(db)],
            }
        if name not in COMMANDS:
            return {"error": f"unknown command {name}"}
        db = store.connect(path)
        store.save_snapshot(db, repo)
        args = Namespace(
            json=True, repo=path,
            base=query.get("base", ["main"])[0],
            task=query.get("task", [""])[0],
            tasks=query.get("task", []),
            against=query.get("against", []),
            branch=query.get("branch", []),
            risk_id=query.get("risk_id", [""])[0],
            test=None, exact=False, request=False,
            llm=query.get("llm", ["0"])[0] in ("1", "true"),
            provider=query.get("provider", [None])[0],
            numbers=[int(n) for n in query.get("number", []) if n.isdigit()],
            comment=None,
            agent=query.get("agent", [None])[0],
            file=query.get("file", [""])[0],
            text=query.get("text", [""])[0],
            confirm=query.get("confirm", ["0"])[0] in ("1", "true"),
            action=query.get("action", ["list"])[0],
            name=query.get("name", [""])[0],
            github=query.get("github", [""])[0],
            role=query.get("role", [""])[0],
            target=query.get("target", ["HEAD"])[0],
            sha=query.get("sha", [""])[0],
            paths=[x for x in query.get("path", []) if x],
            force=query.get("force", ["0"])[0] in ("1", "true"),
            preview=query.get("preview", ["0"])[0] in ("1", "true"),
            max=int(query.get("max", ["70"])[0]),
            config=False,
            limit=int(query.get("limit", ["25"])[0]),
            ref=query.get("ref", ["HEAD"])[0],
        )
        if name == "verify":
            args.branch = query.get("branch", [""])[0]
        result = COMMANDS[name](repo, args, db)
        if name in ("scan", "status"):
            result["graph"] = _graph(repo)
        return result

    def _send(self, code, kind, body, cors=False, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods",
                             "GET, POST, OPTIONS, DELETE")
            self.send_header("Access-Control-Allow-Headers",
                             "Content-Type, Accept, Authorization, "
                             "Mcp-Session-Id, MCP-Protocol-Version")
            self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _public_mcp_url(self):
        host = (self.headers.get("Host") or "127.0.0.1").strip()
        proto = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip()
        return f"{proto}://{host}/mcp"

    def _mcp_headers(self):
        sid = getattr(self.server, "mcp_session", None)
        return {"Mcp-Session-Id": sid} if sid else {}

    def _mcp_server(self):
        from .mcp import Server
        held = getattr(self.server, "mcp", None)
        if held is None:
            self.server.mcp = Server(self.repo_path)
            held = self.server.mcp
        return held

    def _mcp_auth(self):
        """Whether this MCP call may proceed, and on whose behalf.

        With sign in turned on, an agent carries a token belonging to a
        person, so its sessions and findings are attributed rather than
        anonymous. Without it, this is the old shared-secret check, and an
        instance with no secret set answers anybody — which is right for a
        container on your own machine and wrong anywhere else.
        """
        sent = (self.headers.get("Authorization") or "").strip()
        bearer = sent[7:].strip() if sent.lower().startswith("bearer ") else ""

        if self.access:
            if not bearer:
                return False
            db = store.connect(self.repo_path)
            owner = store.agent_token_owner(db, auth_mod.hash_token(bearer))
            if owner:
                self.mcp_person = owner
                return True
            # a shared token still works, so an instance can be moved to
            # sign in without every agent breaking at the same moment
            shared = os.environ.get("PROPHECY_MCP_TOKEN", "")
            return bool(shared) and hmac.compare_digest(bearer, shared)

        token = os.environ.get("PROPHECY_MCP_TOKEN", "")
        if not token:
            return True
        return hmac.compare_digest(bearer, token)

    def _mcp_get(self):
        """Browsers get a description. MCP clients that want SSE can POST."""
        from .mcp import PROTOCOL
        body = json.dumps({
            "name": "prophecy",
            "transport": "streamable-http",
            "protocolVersion": PROTOCOL,
            "url": self._public_mcp_url(),
        }).encode()
        return self._send(200, "application/json", body, cors=True,
                          headers=self._mcp_headers())

    def _mcp_post(self):
        from .mcp import handle
        if not self._mcp_auth():
            return self._send(401, "application/json", json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32001, "message": "unauthorized"},
            }).encode(), cors=True)
        try:
            size = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(size) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, "application/json", json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "parse error"},
            }).encode(), cors=True, headers=self._mcp_headers())
        batch = isinstance(payload, list)
        replies = []
        owner = getattr(self, "mcp_person", None)
        for req in (payload if batch else [payload]):
            reply = handle(self._mcp_server(), _attributed(req, owner))
            if reply is not None:
                replies.append(reply)
        if not replies:
            self.send_response(202)
            self.send_header("Access-Control-Allow-Origin", "*")
            for key, value in self._mcp_headers().items():
                self.send_header(key, value)
            self.end_headers()
            return
        body = json.dumps(replies if batch else replies[0]).encode()
        return self._send(200, "application/json", body, cors=True,
                          headers=self._mcp_headers())


def _attributed(request, owner):
    """Make a tool call say who is really making it.

    The token proves which person an agent belongs to. The `agent` argument
    is just a string the client chose, so without this an agent can file its
    session, its findings and its warnings under somebody else's name — on
    the one kind of instance where names are supposed to mean something.

    Only reached when sign in is on. Anywhere else the caller names itself,
    which is the right answer for a container on your own machine.
    """
    if not owner or not isinstance(request, dict):
        return request
    params = request.get("params")
    if not isinstance(params, dict):
        return request
    arguments = params.get("arguments")
    if not isinstance(arguments, dict) or "agent" not in arguments:
        return request
    login = owner.get("login")
    if not login or arguments.get("agent") == login:
        return request
    # copied rather than mutated: the request came off the wire and the
    # caller is entitled to see its own payload unchanged
    return {**request,
            "params": {**params,
                       "arguments": {**arguments, "agent": login}}}


SKIP = {"node_modules", "venv", ".venv", "vendor", "Library", "Applications"}


def _sample_project():
    """Build the demo project on demand, or hand back the one already there.

    Somewhere stable rather than a temp directory, so the link a person keeps
    open still works tomorrow.
    """
    from .demo import build, seed
    home = Path.home() / "prophecy-demo"
    if home.exists() and (home / ".git").exists():
        return {"path": str(home), "existed": True}
    built = build(home)
    if built.get("error"):
        return built
    try:
        seed(built["path"])
    except Exception:
        pass  # the project is still worth opening without its seeded history
    return {"path": built["path"], "existed": False,
            "branches": built.get("branches", [])}


def _setup_state(path):
    """How far through setup this project is, and what is left to do."""
    from . import github, store as store_mod
    problem = _not_a_repo(path)
    if problem:
        return {"repo_ok": False, "problem": problem, "github": None,
                "agents": 0, "people": 0}

    slug, remote_error = None, None
    try:
        slug = github.slug(path)
        if not slug:
            remote_error = "This project has no GitHub remote yet."
        else:
            github.pull_requests(path, limit=1)
    except github.GitHubUnavailable as exc:
        remote_error = str(exc)

    db = store_mod.connect(path)
    sessions = db.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"]
    return {
        "repo_ok": True,
        "problem": None,
        # Slug from origin counts as linked even when `gh` auth is stale —
        # PR reads need auth, but the repo connection itself is the remote.
        "github": slug,
        "github_auth": bool(slug) and not remote_error,
        "github_problem": remote_error,
        "agents": sessions,
        "live": len(store_mod.live_sessions(db)),
        "people": len(store_mod.members(db)),
    }


def _discover_repos(limit=60):
    """Git repositories under the places people actually keep them.

    Depth-limited on purpose: walking a whole home directory to populate a
    dropdown is a good way to make the page feel broken.
    """
    home = Path.home()
    roots = [home, *(home / n for n in (
        "Projects", "projects", "code", "Code", "src", "dev", "Developer",
        "repos", "work", "Documents", "Desktop", "git",
    ))]
    found, seen = [], set()

    def looks_like_repo(d):
        return (d / ".git").exists()

    for root in roots:
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir())
        except PermissionError:
            continue
        for entry in entries:
            if len(found) >= limit:
                break
            if not entry.is_dir() or entry.name.startswith(".") \
                    or entry.name in SKIP:
                continue
            candidates = [entry]
            if not looks_like_repo(entry):
                try:  # one level further down, for ~/code/org/repo layouts
                    candidates = [c for c in sorted(entry.iterdir())
                                  if c.is_dir() and not c.name.startswith(".")][:40]
                except PermissionError:
                    continue
            for candidate in candidates:
                key = str(candidate.resolve())
                if key in seen or not looks_like_repo(candidate):
                    continue
                seen.add(key)
                found.append({"name": candidate.name, "path": key})
    return sorted(found, key=lambda r: r["name"].lower())


def _not_a_repo(path):
    """A sentence the person can act on, or None if the path is fine."""
    directory = Path(path).expanduser()
    if not directory.exists():
        return f"No such directory: {directory}"
    if not directory.is_dir():
        return f"{directory} is a file, not a directory"
    inside = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "--git-dir"],
        capture_output=True, text=True,
    )
    if inside.returncode:
        return f"{directory} is not a git repository. Run git init, or pick another folder."
    if subprocess.run(["git", "-C", str(directory), "rev-parse", "HEAD"],
                      capture_output=True).returncode:
        return f"{directory} is a git repository with no commits yet."
    return None


def _file_detail(repo, path):
    """Everything known about one file, for the detail drawer."""
    from .scan import is_regenerated
    info = repo["files"].get(path)
    if not info:
        return {"error": f"{path} is not a code file in this repo"}
    return {
        "path": path,
        "lines": info["lines"],
        "symbols": info["symbols"],
        "imports": info["imports"],
        "callers": repo["callers"].get(path, []),
        "is_test": info["is_test"],
        "is_schema": path in repo["schema_files"],
        "is_regenerated": is_regenerated(path),
    }


def _notify_agent(path, repo, agent, target, base):
    """Queue the risk profile of one change for the agent doing that change.

    Deliberately runs the same command the dashboard's own list runs and picks
    the matching change out of it, rather than analysing the branch a second
    time here. Two code paths would be two chances to send an agent a number
    the person looking at the page never saw.
    """
    from .cli import COMMANDS
    from .mcp import _render_analysis

    if not agent:
        return {"error": "who is this for?"}
    if not target:
        return {"error": "which change?"}

    db = store.connect(path)
    args = Namespace(json=True, repo=path, base=base, tasks=[])
    found = COMMANDS["risk"](repo, args, db)
    changes = found.get("changes", [])
    analysis = next((c for c in changes if target in (c.get("label") or "")), None)
    if not analysis:
        return {"error": f"{target} is not in flight here"}

    body = _render_analysis(analysis, detail=True)

    # Two changes can edit different files and still collide through what they
    # reach. That is the part an agent cannot work out alone, so it is the part
    # worth sending: its own reading plus what it looks like next to everyone
    # else's.
    label = analysis["label"]
    meets = [i for i in found.get("interactions", []) if label in i["between"]]
    if meets:
        body += "\n\nTogether with other work in flight:"
        for i in meets:
            others = [b for b in i["between"] if b != label]
            body += (f"\n- with {', '.join(others)}: {i['combined_score']}/100 "
                     f"({i['combined_band']}) combined, against "
                     f"{analysis['risk_score']} alone"
                     + (", worse together than apart" if i.get("escalates")
                        else ""))
            for line in i.get("evidence", [])[:3]:
                body += f"\n    {line}"

    subject = (f"Risk profile for {target}: {analysis['risk_score']}/100 "
               f"({analysis['risk_band']})")
    _, created = store.enqueue_message(db, repo["sha"], agent, subject, body)
    if created:
        store.log(db, repo["sha"], "notified", agent,
                  f"sent the risk profile for {target} "
                  f"({analysis['risk_score']}/100)")
    live = any(s["agent"] == agent for s in store.live_sessions(db))
    return {
        "queued": True,
        "agent": agent,
        "subject": subject,
        "body": body,
        "live": live,
        "score": analysis["risk_score"],
        "band": analysis["risk_band"],
    }


def _graph(repo):
    """Nodes and edges for the dependency picture. Files only — a task graph
    that redraws on every keystroke is not worth the wire."""
    edges = [
        {"from": caller, "to": target}
        for target, callers in repo["callers"].items() for caller in callers
    ]
    degree = {}
    for edge in edges:
        degree[edge["to"]] = degree.get(edge["to"], 0) + 1
    nodes = [
        {
            "id": rel,
            "depended_on_by": degree.get(rel, 0),
            "symbols": len(info["symbols"]),
            "is_test": info["is_test"],
            "is_schema": rel in repo["schema_files"],
        }
        for rel, info in repo["files"].items()
    ]
    nodes.sort(key=lambda n: -n["depended_on_by"])
    return {"nodes": nodes, "edges": edges}


def _lan_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def access_config(auth, owner, allow_orgs, allow_users, repo_path):
    """Turn the flags into the thing the handler checks, or refuse to start.

    An instance that asks people to sign in and then lets in whoever signs
    in first is worse than one that asks nobody: it looks like a door.
    """
    if not auth:
        return None
    if auth != "github":
        raise SystemExit(f"Unknown --auth {auth}. The one there is: github.")

    client_id = os.environ.get("PROPHECY_GITHUB_CLIENT_ID", "").strip()
    client_secret = os.environ.get("PROPHECY_GITHUB_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise SystemExit(
            "--auth github needs PROPHECY_GITHUB_CLIENT_ID and "
            "PROPHECY_GITHUB_CLIENT_SECRET in the environment.\n"
            "Register an OAuth app at "
            "https://github.com/settings/developers with the callback "
            "<your-url>/auth/callback.")

    db = store.connect(repo_path)
    for org in allow_orgs or []:
        store.add_access_rule(db, "org", org, "member", "command line")
    for login in allow_users or []:
        store.add_access_rule(db, "user", login, "member", "command line")
    if owner:
        store.add_access_rule(db, "user", owner, "owner", "command line")

    if not owner and not store.access_rules(db):
        raise SystemExit(
            "--auth github with nobody allowed in would ask people to sign "
            "in and then refuse all of them.\n"
            "Say who it is for: --owner <your-github-login>, and optionally "
            "--allow-org <org> or --allow-user <login>.")

    return {"client_id": client_id, "client_secret": client_secret,
            "owner": owner,
            "redirect_uri": os.environ.get("PROPHECY_REDIRECT_URI", "").strip()}


def serve(repo_path, port, host="0.0.0.0", public=False, open_served=False,
          access=None):
    from .mcp import Server
    # Threaded, because browsers hold idle speculative connections open and a
    # single-threaded server would sit waiting on one instead of answering.
    httpd = ThreadingHTTPServer(
        (host, port),
        partial(Handler, repo=repo_path, public=public,
                open_served=open_served, access=access))
    httpd.daemon_threads = True
    httpd.mcp = Server(repo_path)
    httpd.mcp_session = str(uuid.uuid4())
    lan = _lan_ip()
    if public:
        print(f"public mode: pinned to {repo_path}, writes refused")
    if access:
        print("sign in with GitHub required; agents need a token from the "
              "dashboard")
    print(f"prophecy dashboard  http://127.0.0.1:{port}")
    print(f"MCP for agents      http://{lan}:{port}/mcp")
    print(f"                    claude mcp add --transport http prophecy "
          f"http://{lan}:{port}/mcp")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    return 0
