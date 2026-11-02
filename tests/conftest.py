"""Test harness: throwaway git "GitHub" repos and a fake `gh`.

Clones of https://github.com/<owner>/<repo>.git are redirected to bare repos in a temp folder
(git's url.insteadOf), and `gh` on PATH is a small script that answers from a JSON state file
and records every call. depbot itself runs unchanged: real git, real shell commands.

DEPBOT_SRC=<folder> runs the suite against another copy of depbot.py (e.g. an older version).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(os.environ.get("DEPBOT_SRC") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(SRC))
os.environ.setdefault("DEPBOT_OWNER", "octo")
import depbot  # noqa: E402

OWNER = "octo"

FAKE_GH = r'''#!{python}
import json, os, sys
path = os.environ["FAKE_GH_STATE"]
st = json.load(open(path))
a = sys.argv[1:]
st["calls"].append(a)

def opt(name):
    return a[a.index(name) + 1] if name in a else None

def done(out=None, code=0, err=None):
    json.dump(st, open(path, "w"), indent=1)
    if err:
        print(err, file=sys.stderr)
    if out is not None:
        print(out if isinstance(out, str) else json.dumps(out))
    sys.exit(code)

if a[:2] == ["api", "user"]:
    done(st["owner"])
if a[:2] == ["repo", "view"]:
    done({"defaultBranchRef": {"name": st["default"]}})
if a[:2] == ["pr", "list"]:
    prs = [p for p in st["prs"] if p["state"] == "OPEN"]
    if opt("--head"):
        prs = [p for p in prs if p["headRefName"] == opt("--head")]
    fields = opt("--json").split(",")
    done([{k: p.get(k) for k in fields} for p in prs])
if a[:2] == ["pr", "view"]:
    p = next(p for p in st["prs"] if p["number"] == int(a[2]))
    done({"files": [{"path": f} for f in p["files"]]})
if a[:2] == ["pr", "create"]:
    head = opt("--head")
    if any(p["headRefName"] == head and p["state"] == "OPEN" for p in st["prs"]):
        done(code=1, err=f'a pull request for branch "{head}" into branch "{opt("--base")}" already exists')
    n = max([p["number"] for p in st["prs"]] + [0]) + 1
    st["prs"].append({"number": n, "title": opt("--title"), "body": opt("--body"), "author": {"login": st["owner"]},
                      "headRefName": head, "isDraft": False, "isCrossRepository": False,
                      "mergeable": "MERGEABLE", "state": "OPEN", "files": [],
                      "url": f"https://github.com/{opt('-R')}/pull/{n}"})
    done(st["prs"][-1]["url"])
done(code=1, err="fake gh: unsupported: " + " ".join(a))
'''

INSTALL = """\
if grep -rqs noinstall deps; then echo "error: cannot install"; exit 1; fi
exit 0
"""

CHECK = """\
status=0
for f in deps/*.txt; do
  if grep -q broken "$f"; then echo "FAILED tests/test_deps.py::test_$(basename "$f" .txt) - broken"; status=1; fi
done
if grep -qs x2 deps/a.txt && grep -qs x2 deps/b.txt; then
  echo "FAILED tests/test_deps.py::test_together"; status=1
fi
if grep -qs flaky deps/flaky.txt; then  # fails every other run
  n=$(cat "$FLAKY_FILE" 2>/dev/null || echo 0); echo $((n+1)) > "$FLAKY_FILE"
  if [ $((n % 2)) -eq 0 ]; then echo "FAILED tests/test_deps.py::test_flaky"; status=1; fi
fi
if [ -f package-lock.json ] && [ "$(sh lock.sh -)" != "$(cat package-lock.json)" ]; then
  echo "FAILED tests/test_lock.py::test_lock_matches"; status=1
fi
exit $status
"""

# A stand-in package manager: the "lockfile" lists every package and its version.
LOCK = """\
out=$(for f in deps/*.txt; do echo "$(basename "$f" .txt)==$(cat "$f")"; done)
if [ "$1" = "-" ]; then echo "$out"; else echo "$out" > package-lock.json; fi
"""


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout.strip()


class Origin:
    """A bare repo standing in for github.com/<owner>/<name>, plus a work tree to change it."""

    def __init__(self, root: Path, state, name="app"):
        self.name, self.state = name, state
        self.bare = root / "origins" / OWNER / f"{name}.git"
        self.bare.parent.mkdir(parents=True)
        git(root, "init", "-q", "--bare", "-b", "main", str(self.bare))
        self.work = root / "work" / name
        git(root, "clone", "-q", str(self.bare), str(self.work))
        git(self.work, "checkout", "-q", "-b", "main")
        self.write({"install.sh": INSTALL, "check.sh": CHECK, "deps/a.txt": "1.0.0\n", "deps/b.txt": "1.0.0\n",
                    "deps/c.txt": "1.0.0\n"})
        self.commit_and_push("initial")

    def write(self, files):
        for rel, text in files.items():
            p = self.work / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)

    def commit_and_push(self, msg, ref="main"):
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", msg)
        git(self.work, "push", "-q", "-f", "origin", f"HEAD:refs/heads/{ref}" if "/" not in ref else f"HEAD:{ref}")

    def add_pr(self, n, files, frm="1.0.0", to="1.0.1", pkg=None, author="app/dependabot", cross=False,
               head=None, title=None):
        pkg = pkg or Path(next(iter(files))).stem
        git(self.work, "fetch", "-q", "origin")
        git(self.work, "checkout", "-q", "-B", f"pr{n}", "origin/main")
        self.write(files)
        self.commit_and_push(f"Bump {pkg} from {frm} to {to}", ref=f"refs/pull/{n}/head")
        git(self.work, "checkout", "-q", "main")
        self.state.update(lambda st: st["prs"].append({
            "number": n, "title": title or f"Bump {pkg} from {frm} to {to}",
            "body": f"Bumps [{pkg}](https://example.com/{pkg}) from {frm} to {to}.\n",
            "author": {"login": author}, "headRefName": head or f"dependabot/npm/{pkg}-{to}",
            "isDraft": False, "isCrossRepository": cross, "mergeable": "MERGEABLE", "state": "OPEN",
            "files": list(files), "url": f"https://github.com/{OWNER}/{self.name}/pull/{n}"}))

    def rewrite_pr(self, n, files):
        git(self.work, "checkout", "-q", "-B", f"pr{n}", "origin/main")
        self.write(files)
        self.commit_and_push(f"rebased #{n}", ref=f"refs/pull/{n}/head")
        git(self.work, "checkout", "-q", "main")

    def main_sha(self):
        return git(self.bare, "rev-parse", "main")

    def on_main(self, path):
        return subprocess.run(["git", "show", f"main:{path}"], cwd=self.bare, text=True,
                              capture_output=True).stdout

    def pr_landed(self, n):
        head = git(self.bare, "rev-parse", f"refs/pull/{n}/head")
        return subprocess.run(["git", "merge-base", "--is-ancestor", head, "main"], cwd=self.bare).returncode == 0

    def log_main(self):
        return git(self.bare, "log", "--format=%s%n%b", "main")


class State:
    def __init__(self, path: Path):
        self.path = path
        self.save({"owner": OWNER, "default": "main", "prs": [], "calls": []})

    def load(self):
        return json.loads(self.path.read_text())

    def save(self, st):
        self.path.write_text(json.dumps(st, indent=1))

    def update(self, fn):
        st = self.load()
        fn(st)
        self.save(st)

    def calls(self, *prefix):
        return [c for c in self.load()["calls"] if c[:len(prefix)] == list(prefix)]


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A fake GitHub with one repo, `app`, and depbot pointed at it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH.replace("{python}", sys.executable))
    gh.chmod(0o755)
    state = State(tmp_path / "gh-state.json")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_STATE": str(state.path),
        "FLAKY_FILE": str(tmp_path / "flaky-count"),
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.file://{tmp_path}/origins/.insteadOf",
        "GIT_CONFIG_VALUE_0": "https://github.com/",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(depbot, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(depbot, "OWNER", OWNER, raising=False)
    monkeypatch.setattr(depbot, "_owner", OWNER, raising=False)
    cfg = {"app": {"install": "sh install.sh", "test": ["sh check.sh"]}}
    monkeypatch.setattr(depbot, "load_overrides", lambda: cfg)
    origin = Origin(tmp_path, state)

    class World:
        pass

    w = World()
    w.origin, w.state, w.cfg, w.tmp = origin, state, cfg, tmp_path
    w.report = lambda: next((tmp_path / "cache" / "reports").glob("app-*.md")).read_text()
    return w
