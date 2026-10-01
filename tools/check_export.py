"""Refuse to publish an export that is broken in ways nobody would notice.

The published site has no server to fail loudly, so a bad export looks fine
and is wrong: a page that never opens a project, a risk view with nothing in
it, an MCP endpoint pointing at a port that existed for one minute. Each
check here exists because that failure already happened or nearly did.

    python3 tools/check_export.py            # after an export

Exits non-zero with the reason, so CI stops before committing.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"
DATA = DOCS / "data"

problems = []


def check(condition, complaint):
    # a set: one broken thing said once, however many times it is found
    if not condition and complaint not in problems:
        problems.append(complaint)


def load(name):
    path = DATA / f"{name}.json"
    if not path.exists():
        problems.append(f"{name}.json was not written")
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        problems.append(f"{name}.json is not valid JSON: {exc}")
        return {}


page = (DOCS / "index.html").read_text() if (DOCS / "index.html").exists() else ""
check(page, "docs/index.html is missing")

# Without the flag the page asks a server that is not there, and sits on
# "no project yet" forever. This is the one that has actually happened.
check("window.PROPHECY_STATIC = true" in page,
      "the page is not marked static, so it will look for an API")
check(re.search(r'window\.PROPHECY_REPO = "[^"]+"', page or ""),
      "no project is baked in, so the page opens empty")
check('="/logo.png"' not in page,
      "the logo path is absolute and 404s under /prophecy/")

# The small things a crawler, a link preview and a reader look for. Each is
# invisible when missing, which is why they go in a check rather than in
# somebody's memory.
check('<html lang=' in page, "no lang on the document")
check('name="description"' in page, "no meta description")
check('rel="canonical"' in page, "no canonical link")
check('property="og:image"' in page, "no og:image, so links preview bare")
check('application/ld+json' in page, "no structured data")
check(page.count("<h1") == 1,
      f"{page.count('<h1')} h1 elements, expected exactly one")
# guarded: a missing tag is a complaint above, not a traceback here
if "charset" in page and "canonical" in page:
    check(page.index("charset") < page.index("canonical"),
          "the charset declaration has been pushed below the published tags")
for name in ("robots.txt", "sitemap.xml", "llms.txt", "404.html"):
    check((DOCS / name).exists(), f"{name} was not written")
if (DOCS / "robots.txt").exists():
    check("Sitemap:" in (DOCS / "robots.txt").read_text(),
          "robots.txt does not point at the sitemap")
    check("Disallow: /" not in (DOCS / "robots.txt").read_text(),
          "robots.txt turns crawlers away from the whole site")

risk = load("risk")
check(len(risk.get("changes", [])) >= 5,
      f"risk.json has {len(risk.get('changes', []))} changes, expected the "
      "demo's five branches")
check(risk.get("interactions"),
      "risk.json has no interactions: the pairs that meet are the demo")
score = risk.get("repository", {}).get("score", 0)
check(0 < score <= 97, f"repository score {score} is outside 1..97")

# Sessions go stale after thirty minutes, so an export from a repo that was
# never seeded publishes an empty Crew tab.
check(load("sessions").get("live"),
      "no live agents: the demo project was not seeded before exporting")
check(load("people").get("people"), "no people recorded")

# One recorded answer per branch and per file, or clicking either does
# nothing on the published site.
for change in risk.get("changes", []):
    target = change.get("label", "").replace("branch ", "")
    key = re.sub(r"[^A-Za-z0-9._-]", "_", f"analyze__target-{target}")
    check((DATA / f"{key}.json").exists(),
          f"no recorded analysis for {target}")

repo = (re.search(r'window\.PROPHECY_REPO = "([^"]+)"', page or "")
        or [None, ""])[1]
if repo and Path(repo).exists():
    tracked = subprocess.run(["git", "-C", repo, "ls-files"],
                             capture_output=True, text=True).stdout.split()
    recorded = len(list(DATA.glob("file__*.json")))
    check(recorded >= len(tracked),
          f"{recorded} files recorded but the repo tracks {len(tracked)}")

# An ephemeral port is the exporter's own, and meaningless to a reader. 8000
# is what `prophecy serve` offers, so it is the only one worth publishing.
for path in DATA.glob("*.json"):
    for port in re.findall(r"127\.0\.0\.1:(\d+)", path.read_text()):
        check(port == "8000",
              f"{path.name} publishes 127.0.0.1:{port}, a port that existed "
              "only while the export ran")

if problems:
    print("this export should not be published:\n")
    for p in problems:
        print(f"  - {p}")
    sys.exit(1)

print(f"export looks publishable: {len(list(DATA.glob('*.json')))} recorded "
      f"answers, {len(risk.get('changes', []))} changes, "
      f"{len(risk.get('interactions', []))} interactions")
