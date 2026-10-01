"""Bake the dashboard into files GitHub Pages can serve.

The engine shells out to git, so it cannot run on a static host. What it can
do is answer every question once, here, and write the answers down. The page
then reads files instead of an API and behaves the same for everything that
does not need a live repository.

This runs the real server in-process and records its responses rather than
reimplementing the dispatch, so the baked JSON cannot drift from what a
running instance would say.

    python3 tools/export_static.py ~/prophecy-demo

Writes docs/index.html and docs/data/*.json.
"""

import datetime
import json
import re
import shutil
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prophecy.server import Handler  # noqa: E402
from prophecy.mcp import Server as McpServer  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DATA = DOCS / "data"

# Everything the page asks for with no arguments. Left out on purpose:
# backfill replays hundreds of merges and would take longer than the whole
# export, and the write endpoints have nothing to say on a read-only host.
PLAIN = [
    "risk", "scan", "sessions", "people", "usage", "sharing", "history",
    "status", "insights", "fleet", "worktree", "setup", "mcp_config",
    "repos", "sample", "messages",
]


SITE = "https://amritnair.github.io/prophecy/"
CODESPACE = "https://codespaces.new/amritnair/prophecy?quickstart=1"

# Only the published copy can know where it lives, so the tags that need an
# absolute URL are written here rather than carried in the page itself.
HEAD_TAGS = f"""<link rel="canonical" href="{SITE}">
<meta property="og:url" content="{SITE}">
<meta property="og:image" content="{SITE}logo.png">
<meta name="twitter:image" content="{SITE}logo.png">
<script type="application/ld+json">
{{"@context": "https://schema.org",
  "@type": "SoftwareApplication",
  "name": "Prophecy",
  "applicationCategory": "DeveloperApplication",
  "operatingSystem": "macOS, Linux, Windows",
  "url": "{SITE}",
  "codeRepository": "https://github.com/amritnair/prophecy",
  "license": "https://opensource.org/licenses/MIT",
  "offers": {{"@type": "Offer", "price": "0", "priceCurrency": "USD"}},
  "description": "Reads a repository and answers what a change would break, given everything else in flight."}}
</script>
"""

# Appended to the published page only. The local dashboard has an engine
# behind it and needs none of this.
FULL_VERSION = """
<div id="fullVersion">
  <strong>You are reading recorded results.</strong>
  Editing, commits, your own repositories and MCP need Prophecy running on a
  machine with a checkout.
  Run one and the MCP tab here will connect your own agent to it.
  <code id="runCmd">docker run -p 8000:8000 ghcr.io/amritnair/prophecy</code>
  <button type="button" class="copyRun" onclick="
    var cmd = document.getElementById('runCmd'), btn = this;
    var done = function (label) {
      btn.textContent = label;
      setTimeout(function () { btn.textContent = 'Copy'; }, 1800);
    };
    var select = function () {
      // clipboard access can be refused, and telling somebody to press
      // Cmd-C with nothing selected is no help at all
      var range = document.createRange();
      range.selectNodeContents(cmd);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      done('Selected, press Cmd-C');
    };
    try {
      navigator.clipboard.writeText(cmd.textContent)
        .then(function () { done('Copied'); }, select);
    } catch (e) { select(); }
  ">Copy</button>
  <a href="%s">Or open it in a browser</a>, free, on GitHub's hours.
  <button type="button" onclick="this.parentNode.remove()"
          aria-label="Dismiss">&times;</button>
</div>
<style>
  #fullVersion {
    position: fixed; right: 16px; bottom: 16px; z-index: 40; max-width: 370px;
    padding: 13px 34px 13px 15px; border: 1px solid var(--line, #22272f);
    border-radius: 10px; background: var(--raised, #11151b);
    color: var(--text-2, #9aa4b2);
    font: 12.5px/1.6 var(--ui, ui-sans-serif, system-ui, sans-serif);
    box-shadow: 0 10px 30px rgba(0, 0, 0, .45);
  }
  #fullVersion strong { color: var(--text, #e8ecf2); font-weight: 600; }
  #fullVersion a { color: var(--accent, #7DBBFF); }
  #fullVersion code {
    display: block; margin-top: 7px; font-size: 11.5px;
    color: var(--text-3, #6f747c); word-break: break-all;
  }
  #fullVersion button {
    position: absolute; top: 7px; right: 9px; border: 0; background: none;
    color: var(--text-3, #6f747c); font-size: 16px; cursor: pointer;
    line-height: 1;
  }
  #fullVersion .copyRun {
    position: static; margin: 6px 0 2px; padding: 4px 11px; font-size: 11.5px;
    border: 1px solid var(--line, #22272f); border-radius: 6px;
    background: var(--bg, #07090d); color: var(--text-2, #9aa4b2);
  }
  #fullVersion .copyRun:hover { color: var(--text, #e8ecf2); }
  @media (max-width: 640px) { #fullVersion { display: none; } }
</style>
""" % CODESPACE


def key(cmd, params=None):
    """The filename a request maps to. Mirrored exactly in dashboard.html."""
    parts = []
    for k, v in sorted((params or {}).items()):
        if v in (None, "", []):
            continue
        parts.append(f"{k}-{'_'.join(v) if isinstance(v, list) else v}")
    name = f"{cmd}__{'__'.join(parts)}" if parts else cmd
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def write_site_files():
    """The small files a crawler, a reader or a model goes looking for.

    Written here rather than kept by hand, so they cannot drift away from
    the page they describe.
    """
    today = datetime.date.today().isoformat()

    # Nothing here is private and nothing is behind a login, so there is no
    # reason to turn anybody away, including the model crawlers. Blocking
    # them is how a project stops being quotable.
    (DOCS / "robots.txt").write_text(
        "User-agent: *\n"
        "Allow: /\n"
        "\n"
        f"Sitemap: {SITE}sitemap.xml\n")

    (DOCS / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url>\n    <loc>{SITE}</loc>\n"
        f"    <lastmod>{today}</lastmod>\n"
        "    <changefreq>weekly</changefreq>\n  </url>\n"
        "</urlset>\n")

    (DOCS / "llms.txt").write_text(f"""# Prophecy

> Reads a repository and answers one question: given everything else
> happening here, what would this change break?

This page is a recording. Every answer on it was produced by running the
real engine once and writing the result down, because the engine shells out
to git against a checkout and a static host has none.

## What it does

- Scans the files git tracks for symbols, signatures and imports. Python
  through `ast`, TypeScript and JavaScript by pattern matching.
- Scores a change from 0 to 100 with a range and a confidence, and carries
  the evidence that produced the score.
- Finds pairs of changes that are calm alone and dangerous together,
  including pairs that share no file and no import but meet at the same
  stored field. That is the case a diff review cannot catch.
- Serves MCP, so a coding agent asks what it is walking into before it
  edits, and is handed a slice instead of exploring the tree.

## What it does not do

- Nothing here observes runtime. It knows how many files import a symbol
  and not how often any of them runs, which is the largest source of the
  range on every score.
- Scores are a ranking heuristic. They have not earned the word calibrated.
- TypeScript is read with pattern matching rather than a parser, so
  references in it are missed, and the analysis says so rather than
  quietly rounding up.

## Links

- Source: https://github.com/amritnair/prophecy
- Run it: docker run -p 8000:8000 ghcr.io/amritnair/prophecy
- License: MIT
""")

    (DOCS / "404.html").write_text("""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Not here &mdash; Prophecy</title>
<meta name="robots" content="noindex">
<link rel="icon" type="image/png" href="/prophecy/logo.png">
<style>
  :root { color-scheme: dark; }
  body { margin: 0; min-height: 100vh; display: grid; place-content: center;
         gap: 14px; text-align: center; padding: 24px; background: #000;
         color: #e8ecf2;
         font: 15px/1.6 ui-sans-serif, -apple-system, system-ui, sans-serif; }
  h1 { font-size: 22px; font-weight: 600; margin: 0; letter-spacing: -.02em; }
  p { margin: 0; color: #9aa4b2; max-width: 42ch; }
  a { color: #7DBBFF; }
</style>
<h1>Nothing at that address</h1>
<p>Which is at least a collision Prophecy cannot be blamed for.</p>
<p><a href="/prophecy/">Back to the dashboard</a> &middot;
   <a href="https://github.com/amritnair/prophecy">the source</a></p>
""")


def main(repo):
    repo = str(Path(repo).expanduser().resolve())
    DATA.mkdir(parents=True, exist_ok=True)
    for stale in DATA.glob("*.json"):
        stale.unlink()

    # port 0: let the OS pick one, so this never collides with a dashboard
    # the person already has running
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, repo=repo))
    httpd.daemon_threads = True
    httpd.mcp = McpServer(repo)
    httpd.mcp_session = "static-export"
    port = httpd.socket.getsockname()[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def grab(cmd, params=None):
        query = f"repo={urllib.parse.quote(repo)}"
        for k, v in (params or {}).items():
            for one in (v if isinstance(v, list) else [v]):
                query += f"&{k}={urllib.parse.quote(str(one))}"
        url = f"http://127.0.0.1:{port}/api/{cmd}?{query}"
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                body = r.read()
        except (urllib.error.URLError, socket.timeout) as exc:
            print(f"  skipped {cmd} ({exc})")
            return None
        (DATA / f"{key(cmd, params)}.json").write_bytes(body)
        return json.loads(body)

    print(f"exporting {repo}")
    for cmd in PLAIN:
        grab(cmd)
        print(f"  {cmd}")

    # The export ran on an ephemeral port, and handing a reader
    # 127.0.0.1:41337 as their MCP endpoint is worse than saying nothing.
    # What they would actually get from `prophecy serve` is port 8000.
    local = "http://127.0.0.1:8000/mcp"
    (DATA / "mcp_config.json").write_text(json.dumps({
        "config": {"mcpServers": {"prophecy": {"type": "http", "url": local}}},
        "url": local,
        "repo": repo,
    }))

    # One per branch, so clicking a change on the Risk tab still opens.
    risk = json.loads((DATA / "risk.json").read_text())
    for change in risk.get("changes", []):
        label = change.get("label", "")
        target = label.replace("branch ", "")
        if target:
            grab("analyze", {"target": target})
    print(f"  analyze x{len(risk.get('changes', []))}")

    # One per file, so clicking a node on the Map still opens.
    tracked = subprocess.run(
        ["git", "-C", repo, "ls-files"],
        capture_output=True, text=True).stdout.split()
    for path in tracked:
        grab("file", {"path": path})
    print(f"  file x{len(tracked)}")

    httpd.shutdown()

    page = (ROOT / "prophecy" / "dashboard.html").read_text()
    # The page is identical to the live one apart from knowing it is static
    # and which project to open, both of which it reads off window. There is
    # no </head> to hang this on, so it goes before the first script, which
    # is the first thing that could read it.
    boot = (
        "<script>window.PROPHECY_STATIC = true;"
        f"window.PROPHECY_REPO = {json.dumps(repo)};</script>\n"
    )
    # After the title, not before the first <script>: the first script tag
    # in this file lives inside a hidden <textarea> holding the MCP demo
    # page as text, so anything injected there is content rather than code.
    # Going in after the title also leaves the charset where it belongs,
    # which is first.
    anchor = "<title>Prophecy</title>\n"
    assert page.startswith('<!doctype html>\n<html lang="en">\n'), \
        "dashboard.html does not open as expected"
    assert anchor in page, "no title to hang the published tags on"
    page = page.replace(anchor, anchor + boot + HEAD_TAGS, 1)
    # Half the product needs a checkout and a process. Saying so once, in a
    # corner, beats a reader concluding the editor is broken.
    page += FULL_VERSION
    # Pages serves this from /prophecy/, where an absolute asset path is a 404
    page = page.replace('="/logo.png"', '="logo.png"')
    (DOCS / "index.html").write_text(page)
    for asset in ("logo.png",):
        src = ROOT / "prophecy" / asset
        if src.exists():
            shutil.copy(src, DOCS / asset)

    write_site_files()

    size = sum(f.stat().st_size for f in DATA.glob("*.json"))
    print(f"\ndocs/index.html + {len(list(DATA.glob('*.json')))} files "
          f"({size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
