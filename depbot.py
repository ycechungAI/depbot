#!/usr/bin/env python3
"""depbot — test dependency-update PRs locally, then land the safe ones in one push.

Usage:
  depbot scan [REPO ...]                 list open dependency PRs and their risk
  depbot update REPO [--dry-run] [--pr]  test + land minor/patch PRs, report majors
  depbot check REPO                      run the repo's install/test gate on the default branch

REPO is `name` (your own repo, or DEPBOT_OWNER's) or `owner/name`.

Flow for `update`:
  1. Baseline: install + test the default branch. Failures already present there
     are recorded so they don't block updates. A default branch that won't install
     stops the run: nothing can be tested, so nothing lands.
  2. Merge every open minor/patch Dependabot PR into one local batch branch. When
     lockfiles collide, the package manager regenerates the lockfile instead of
     trusting git's textual merge (which can leave duplicate keys behind).
  3. Gate: frozen install, tests, and every changed requirements file must still
     resolve. A PR that breaks the gate is dropped and the rest are retried.
  4. Append the landed updates to DEPENDENCY_UPDATES.md, commit, and push to the
     default branch (or open one PR with --pr, or when the push is refused). One
     push = one CI run, and GitHub marks the included PRs as merged.
  5. Major / pre-release PRs are tested one by one and only reported, never landed.

Only needs: git, gh (logged in), node/npx for JS repos, uv for Python repos, go for Go.
Per-repo overrides live in repos.json (see the README for where depbot looks).
"""
import argparse
import contextlib
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

VERSION = "0.1.0"
HERE = Path(__file__).resolve().parent
CHANGELOG = "DEPENDENCY_UPDATES.md"
BOT_LOGINS = {"app/dependabot", "dependabot[bot]", "dependabot"}
LOCKFILES = ("pnpm-lock.yaml", "yarn.lock", "package-lock.json", "uv.lock")
# Where clones come from; {repo} is owner/name. Override for GitHub Enterprise or a mirror.
GIT_URL = os.environ.get("DEPBOT_GIT_URL", "https://github.com/{repo}.git")
q = shlex.quote


def _default_cache():
    legacy = Path.home() / "dev" / "_depbot-cache"  # where depbot kept its clones before 0.1
    if legacy.is_dir():
        return legacy
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "depbot"


CACHE = Path(os.environ["DEPBOT_CACHE"]) if os.environ.get("DEPBOT_CACHE") else _default_cache()


# ---------- shell helpers ----------

def sh(cmd, cwd=None, check=True, quiet=True, timeout=1800):
    """Run a shell command; return (ok, combined output). A timeout counts as a failure."""
    try:
        p = subprocess.run(cmd, cwd=cwd, shell=True, text=True, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           env={**os.environ, "CI": "true", "COREPACK_ENABLE_STRICT": "0",
                                "HUSKY": "0", "GIT_TERMINAL_PROMPT": "0"})
        ok, out = p.returncode == 0, p.stdout
    except subprocess.TimeoutExpired as e:
        out = e.output.decode(errors="replace") if isinstance(e.output, bytes) else (e.output or "")
        ok, out = False, out + f"\nerror: timed out after {timeout} s"
    if not quiet:
        print(out)
    if check and not ok:
        raise RuntimeError(f"`{cmd}` failed in {cwd}:\n{out[-3000:]}")
    return ok, out


def sh_retry(cmd, cwd=None, check=True, tries=3):
    """For commands that use the network: a dropped connection gets two more tries."""
    for i in range(tries):
        ok, out = sh(cmd, cwd=cwd, check=False)
        if ok or i == tries - 1:
            break
        log(f"retrying in {2 * (i + 1)} s: {_last_err(out)}")
        time.sleep(2 * (i + 1))
    if check and not ok:
        raise RuntimeError(f"`{cmd}` failed in {cwd}:\n{out[-3000:]}")
    return ok, out


def gh_json(args):
    ok, out = sh_retry(f"gh {args}")
    return json.loads(out or "null")


def log(msg):
    print(f"[depbot] {msg}", flush=True)


# ---------- repos and settings ----------

_owner = os.environ.get("DEPBOT_OWNER")


def owner():
    """The owner of repos named without one: DEPBOT_OWNER, else the user gh is logged in as."""
    global _owner
    if not _owner:
        _owner = sh("gh api user --jq .login")[1].strip()
    return _owner


def full_name(repo):
    return repo if "/" in repo else f"{owner()}/{repo}"


def short_name(repo):
    return repo.split("/")[-1]


def repo_dir(repo):
    """The clone in the cache. Another owner's repo gets its own folder, so two repos with the
    same name never share a clone."""
    o, n = full_name(repo).split("/", 1)
    return CACHE / (n if o == owner() else f"{o}__{n}")


def config_file():
    """repos.json: DEPBOT_REPOS, else the current folder, next to depbot.py, or ~/.config/depbot."""
    if os.environ.get("DEPBOT_REPOS"):
        return Path(os.environ["DEPBOT_REPOS"])
    conf = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "depbot"
    for f in (Path.cwd() / "repos.json", HERE / "repos.json", conf / "repos.json"):
        if f.exists():
            return f
    return None


def load_overrides():
    f = config_file()
    return json.loads(f.read_text()) if f else {}


def repo_cfg(repo):
    o = load_overrides()
    return o.get(repo) or o.get(full_name(repo)) or o.get(short_name(repo)) or {}


@contextlib.contextmanager
def repo_lock(repo):
    """One run per clone: two runs on the same clone would reset each other's branches."""
    try:
        import fcntl
    except ImportError:  # Windows: no locking
        yield
        return
    CACHE.mkdir(parents=True, exist_ok=True)
    with open(CACHE / f".{repo_dir(repo).name}.lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"another depbot run is already working on {full_name(repo)}")
        yield


# ---------- PR classification ----------

# Anchored to line starts so bumps quoted inside release notes ("> Bumps ...") are ignored.
BUMP_RE = re.compile(r"^(?:Updates?|Bumps?) `?\[?([@\w./-]+?)\]?(?:\([^)]*\))?`? from ([\w.+-]+?) to ([\w.+-]+?)\.?(?=\s|$)",
                     re.M | re.I)


# Grouped PRs open with a complete table; the "Updates ..." lines below it get truncated.
TABLE_RE = re.compile(r"^\|\s*\[?([@\w./-]+)\]?(?:\([^)]*\))?\s*\|\s*`([\w.+-]+)`\s*\|\s*`([\w.+-]+)`\s*\|", re.M)


def bumps_of(pr):
    """[(package, from, to)] parsed from a Dependabot PR's title and body."""
    found = TABLE_RE.findall(pr.get("body") or "") or BUMP_RE.findall(pr.get("body") or "")
    if not found:
        title = re.sub(r"^[\w()-]+:\s*", "", pr["title"])  # drop "chore(deps): "
        found = BUMP_RE.findall(title)
    seen, out = set(), []
    for b in found:
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out


def _parts(v):
    v = v.lstrip("v")
    return [int(x) if x.isdigit() else x for x in re.split(r"[.+-]", v)]


def risk(frm, to):
    """'prerelease' | 'major' | 'minor' | 'patch' for one version change."""
    # A pinned commit (GitHub Actions, Docker digests): nothing to compare, so never auto-land.
    if any(re.fullmatch(r"[0-9a-f]{7,64}", v) and re.search(r"[a-f]", v) for v in (frm, to)):
        return "major"
    if re.search(r"(rc|alpha|beta|dev|a\d|b\d)", to, re.I) and not re.search(r"(rc|alpha|beta|dev)", frm, re.I):
        return "prerelease"
    f, t = _parts(frm), _parts(to)
    if not f or not t or not isinstance(f[0], int) or not isinstance(t[0], int):
        return "major"
    if f[0] != t[0]:
        return "major"
    # 0.x: a minor change is breaking by semver convention
    if f[0] == 0 and len(f) > 1 and len(t) > 1 and f[1] != t[1]:
        return "major"
    # 0.0.x: so is every change
    if f[:2] == [0, 0] and f != t:
        return "major"
    # calendar versions (certifi 2026.7.22) move the "major" every year; treat as patch
    if len(f) > 1 and len(t) > 1 and f[1:2] != t[1:2]:
        return "minor"
    return "patch"


RANK = {"patch": 0, "minor": 1, "major": 2, "prerelease": 3}


def pr_risk(pr):
    bumps = bumps_of(pr)
    if not bumps:
        return "major", bumps  # unknown shape: never auto-land
    worst = max((risk(f, t) for _, f, t in bumps), key=RANK.get)
    # calendar-versioned packages like certifi bump year → still safe
    if worst == "major" and all(re.match(r"20\d\d\.", f) and re.match(r"20\d\d\.", t) for _, f, t in bumps):
        worst = "minor"
    return worst, bumps


def open_dep_prs(repo):
    prs = gh_json(f"pr list -R {q(full_name(repo))} --state open --limit 100 "
                  f"--json number,title,body,author,headRefName,mergeable,isDraft,isCrossRepository")
    # Dependabot's own PRs only. depbot installs and runs a PR's code, so a branch that is
    # merely named dependabot/... (anyone can open one from a fork) must not count.
    return [p for p in prs if p["author"]["login"] in BOT_LOGINS and not p.get("isCrossRepository")]


# ---------- repo toolchain detection ----------

class Toolchain:
    """Install / lockfile / test commands for one project directory."""

    def __init__(self, root: Path, cfg: dict):
        self.root = root
        self.dir = root / cfg.get("dir", ".")
        self.cfg = cfg
        self.kind = None
        pkg = self.dir / "package.json"
        if pkg.exists():
            meta = json.loads(pkg.read_text())
            pm = meta.get("packageManager", "")
            scripts = meta.get("scripts", {})
            if (self.dir / "pnpm-lock.yaml").exists() or pm.startswith("pnpm"):
                ver = re.match(r"pnpm@(\d+)", pm)
                run = f"npx -y pnpm@{ver.group(1) if ver else 10}"
                self.kind, self.lockfile = "pnpm", "pnpm-lock.yaml"
                self.frozen, self.relock = f"{run} install --frozen-lockfile", f"{run} install --lockfile-only"
                self.loose = f"{run} install --no-frozen-lockfile"
            elif pm.startswith("yarn@") and not pm.startswith("yarn@1"):
                run = f"npx -y -p @yarnpkg/cli-dist@{pm.split('@')[1].split('+')[0]} yarn"
                self.kind, self.lockfile = "yarn-berry", "yarn.lock"
                self.frozen, self.relock = f"{run} install --immutable", f"{run} install --mode=update-lockfile"
                self.loose = f"{run} install"
            elif (self.dir / "yarn.lock").exists():
                run = "npx -y yarn@1.22.22"
                self.kind, self.lockfile = "yarn", "yarn.lock"
                self.frozen = f"{run} install --frozen-lockfile --ignore-engines --non-interactive"
                self.relock = self.loose = f"{run} install --ignore-engines --non-interactive"
            else:
                run = "npm"
                self.kind, self.lockfile = "npm", "package-lock.json"
                self.frozen, self.relock = "npm ci --no-audit --no-fund", "npm install --package-lock-only --no-audit --no-fund"
                self.loose = "npm install --no-audit --no-fund"
            default_tests = []
            for s in ("lint", "typecheck", "test"):
                if s in scripts and "no test specified" not in scripts[s]:
                    default_tests.append(f"{run} run {s}")
            if not default_tests and "build" in scripts:
                default_tests.append(f"{run} run build")
            self.tests = cfg.get("test", default_tests)
        elif (self.dir / "go.mod").exists():
            self.kind, self.lockfile = "go", "go.sum"
            self.frozen = self.loose = "go mod download"
            self.relock = "go mod tidy"
            self.tests = cfg.get("test", ["go vet ./...", "go test ./..."])
        else:
            self.kind, self.lockfile = "python", None
            self.frozen = self.loose = self.relock = None
            default_tests = []
            # A uv project can be installed and tested, not just resolved.
            if (self.dir / "uv.lock").exists():
                self.lockfile = "uv.lock"
                self.frozen, self.loose, self.relock = "uv sync --frozen", "uv sync", "uv lock"
                if (self.dir / "tests").is_dir() or (self.dir / "pytest.ini").exists():
                    default_tests.append("uv run --frozen pytest -q")
            self.tests = cfg.get("test", default_tests)
        if isinstance(self.tests, str):
            self.tests = [self.tests]
        if cfg.get("install"):
            self.frozen = self.loose = cfg["install"]
        # Extra install steps the repo's CI runs before testing (e.g. a nested package).
        self.setup = cfg.get("setup", [])
        if isinstance(self.setup, str):
            self.setup = [self.setup]

    # Python projects: every pinned requirements file must still resolve.
    def requirement_files(self):
        out = sh("git ls-files -z '*requirements*.txt' '**/requirements*.txt' 'pyproject.toml' '**/pyproject.toml'",
                 cwd=self.root, check=False)[1].split("\0")
        return [f for f in out if f and "node_modules" not in f]

    def resolves(self, req):
        if not shutil.which("uv"):
            return True, "uv not installed; skipped"
        last = ""
        for py in self.cfg.get("python", ["3.11", "3.10", "3.12", "3.9"]):
            ok, out = sh(f"uv pip compile {q(req)} --python-version {py} "
                         f"--python-platform x86_64-manylinux_2_28 -q -o /dev/null",
                         cwd=self.root, check=False, timeout=600)
            if ok:
                return True, f"py{py}"
            last = last or out  # keep the first (newest-Python) error, the most useful one
        return False, last.strip().splitlines()[-1] if last.strip() else "unresolvable"


# ---------- test gate ----------

FAIL_PATTERNS = [
    re.compile(r"^\s*● (.+ › .+)$", re.M),          # jest
    re.compile(r"^\s*(?:×|✗|✕|FAIL)\s+(.+?)(?:\s+\(?\d+(?:\.\d+)?\s?ms\)?)?$", re.M),  # vitest / jest / generic
    re.compile(r"^--- FAIL: (\S+)", re.M),          # go
    re.compile(r"^(?:FAILED|ERROR) (\S+::\S+)", re.M),  # pytest -q summary
]


ANSI = re.compile(r"\x1b\[[0-9;]*m")
ESLINT_FILE = re.compile(r"^(/\S+|[\w./-]+\.(?:[cm]?[jt]sx?|vue|svelte))$")
ESLINT_ERR = re.compile(r"^\s+\d+:\d+\s+error\s+.*?\s{2,}(\S+)\s*$")
TSC_ERR = re.compile(r"^(\S+?)\(\d+,\d+\): error (TS\d+)", re.M)
# Timings differ from run to run; without this an unparsed failure would look new every time.
TIMING = re.compile(r"\d+(?:\.\d+)?\s?(?:ms|s|sec|seconds)\b")


def failing_tests(output, root=""):
    """Names of failing tests / lint rules / type errors, comparable across runs."""
    output = ANSI.sub("", output)
    names = set()
    for pat in FAIL_PATTERNS:
        names.update(m.strip() for m in pat.findall(output))
    current = None
    for line in output.splitlines():  # eslint "stylish": file header, then indented errors
        if ESLINT_FILE.match(line.strip()) and not line.startswith(" "):
            current = line.strip().replace(root + "/", "")
        elif current and (m := ESLINT_ERR.match(line)):
            names.add(f"lint {current}: {m.group(1)}")
    names.update(f"tsc {f}: {code}" for f, code in TSC_ERR.findall(output))
    return names


class Gate:
    """Result of install + tests on one tree."""

    def __init__(self, tc: Toolchain, reqs=None):
        self.ok, self.notes, self.failures, self.frozen_ok = True, [], set(), True
        self.installed, self.unresolved = True, {}
        # What this gate actually checked, for the report.
        self.checks = ([("install" if tc.frozen else "")] + [f"`{s}`" for s in tc.setup]
                       + [f"`{t}`" for t in tc.tests]
                       + ([f"{len(reqs)} requirements file(s) resolve"] if reqs else []))
        self.checks = [c for c in self.checks if c]
        self.tested = bool(tc.tests)
        if tc.frozen:
            fok, out = sh(tc.frozen, cwd=tc.dir, check=False)
            self.frozen_ok = fok
            if not fok:
                self.notes.append("frozen install failed: " + _last_err(out))
                lok, out = sh(tc.loose, cwd=tc.dir, check=False)
                if not lok:
                    self.ok = self.installed = False
                    self.notes.append("install failed: " + _last_err(out))
                    return
        for s in tc.setup:
            sok, out = sh(s, cwd=tc.dir, check=False)
            if not sok:
                self.ok = self.installed = False
                self.notes.append(f"setup `{s}` failed: " + _last_err(out))
                return
        for t in tc.tests:
            tok, out = sh(t, cwd=tc.dir, check=False, timeout=1800)
            if not tok:
                self.ok = False
                f = failing_tests(out, str(tc.dir))
                self.failures |= f or {f"`{t}` exited non-zero: " + TIMING.sub("…", _last_err(out))}
        for req in reqs or []:
            rok, why = tc.resolves(req)
            if not rok:
                self.unresolved[req] = why

    def summary(self):
        return ", ".join(self.checks) or "nothing (no install, tests or requirements found)"

    def no_worse_than(self, base: "Gate"):
        """True if this tree introduces no new failure compared to the baseline."""
        if not self.installed:
            return False, "; ".join(self.notes)
        if base.frozen_ok and not self.frozen_ok:
            return False, "lockfile no longer installs frozen (was fine before)"
        new = self.failures - base.failures
        if new:
            return False, "new failures: " + "; ".join(sorted(new)[:5])
        broke = {r: w for r, w in self.unresolved.items() if r not in base.unresolved}
        if broke:
            return False, "requirements stopped resolving: " + "; ".join(f"{r} ({w})" for r, w in broke.items())
        if not self.ok and base.ok and not new:
            return False, "tests failed: " + "; ".join(self.notes or ["see log"])
        return True, ""


def _last_err(out):
    lines = [l for l in out.strip().splitlines() if re.search(r"err|fail|cannot|invalid", l, re.I)]
    return (lines[-1] if lines else (out.strip().splitlines() or [""])[-1])[:200]


# ---------- git work ----------

def checkout(repo):
    path = repo_dir(repo)
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        sh_retry(f"git clone -q {q(GIT_URL.format(repo=full_name(repo)))} {q(str(path))}")
    default = gh_json(f"repo view {q(full_name(repo))} --json defaultBranchRef")["defaultBranchRef"]["name"]
    # A run that was killed mid-merge leaves the merge behind; clear it before switching.
    sh("git merge --abort", cwd=path, check=False)
    sh_retry(f"git fetch -q origin {q(default)}", cwd=path)
    sh(f"git checkout -q -f -B {q(default)} origin/{q(default)} && git clean -qfdx -e node_modules", cwd=path)
    return path, default


def reset_to(path, ref):
    sh(f"git checkout -q -f {q(ref)} && git clean -qfdx -e node_modules", cwd=path)


def merge_pr(path, tc: Toolchain, pr):
    """Merge one PR head into HEAD; repair lockfile conflicts by regenerating. Returns (ok, note)."""
    n = pr["number"]
    # "+": Dependabot rebases its PRs, so the head may have been rewritten since the last run.
    fok, out = sh_retry(f"git fetch -q origin +pull/{n}/head:depbot-pr{n}", cwd=path, check=False)
    if not fok:
        return False, "couldn't fetch the PR: " + _last_err(out)
    ok, out = sh(f"git merge -q --no-edit depbot-pr{n}", cwd=path, check=False)
    if ok:
        return True, ""
    conflicted = sh("git diff --name-only -z --diff-filter=U", cwd=path, check=False)[1].split("\0")
    conflicted = [f for f in conflicted if f]
    lock_only = all(Path(f).name in LOCKFILES for f in conflicted)
    if not conflicted or not lock_only or not tc.relock:
        sh("git merge --abort", cwd=path, check=False)
        return False, "conflicts in " + ", ".join(conflicted or ["?"])
    # Lockfile-only conflict: keep our lockfile, let the package manager re-resolve
    # against the merged package.json files.
    files = " ".join(q(f) for f in conflicted)
    sh(f"git checkout --ours -- {files}", cwd=path)
    rok, rout = sh(tc.relock, cwd=tc.dir, check=False)
    if not rok:
        sh("git merge --abort", cwd=path, check=False)
        return False, "lockfile regeneration failed: " + _last_err(rout)
    sh(f"git add -A -- {files} && git commit -q --no-edit", cwd=path)
    return True, "lockfile regenerated"


def relock_if_broken(path, tc: Toolchain, base_gate):
    """Textual merges of lockfiles can succeed yet leave an invalid file. Repair it."""
    if not tc.frozen or not tc.relock or not base_gate.frozen_ok:
        return ""
    ok, _ = sh(tc.frozen, cwd=tc.dir, check=False)
    if ok:
        return ""
    sh(tc.relock, cwd=tc.dir, check=False)
    if tc.lockfile and sh(f"git status --porcelain -- {q(tc.lockfile)}", cwd=tc.dir, check=False)[1].strip():
        sh(f"git add {q(tc.lockfile)} && git commit -q -m "
           + q(f"chore(deps): regenerate {tc.lockfile} after merging updates"), cwd=tc.dir)
        return f"regenerated {tc.lockfile}"
    return ""


# ---------- changelog ----------

def write_changelog(path, landed):
    f = path / CHANGELOG
    today = datetime.date.today().isoformat()
    head = "# Dependency updates\n\nMinor and patch updates landed by `depbot` after local tests passed.\n"
    body = f.read_text() if f.exists() else head
    rows = [f"| {pkg} | {frm} | {to} | {lvl} | #{pr['number']} |"
            for pr, lvl, bumps in landed for pkg, frm, to in bumps]
    section = (f"\n## {today}\n\n| Package | From | To | Type | PR |\n|---|---|---|---|---|\n"
               + "\n".join(rows) + "\n")
    # newest first, right after the header paragraph
    idx = body.find("\n## ")
    body = body + section if idx == -1 else body[:idx] + section + body[idx:]
    f.write_text(body)
    sh(f"git add {CHANGELOG}", cwd=path)


def open_batch_pr(repo, path, default, title, body):
    """Push HEAD to today's depbot branch and open a PR for it, or reuse the open one."""
    branch = f"depbot/{datetime.date.today()}"
    sh(f"git push -q -f origin HEAD:refs/heads/{q(branch)}", cwd=path)
    existing = gh_json(f"pr list -R {q(full_name(repo))} --head {q(branch)} --state open --json url")
    if existing:
        return existing[0]["url"], "updated"
    url = sh(f"gh pr create -R {q(full_name(repo))} --base {q(default)} --head {q(branch)} "
             f"--title {q(title)} --body {q(body)}", cwd=path)[1].strip()
    return url, "opened"


# ---------- commands ----------

def cmd_scan(repos):
    for repo in repos:
        prs = open_dep_prs(repo)
        if not prs:
            continue
        print(f"\n{repo}")
        for pr in sorted(prs, key=lambda p: p["number"]):
            lvl, bumps = pr_risk(pr)
            what = ", ".join(f"{p} {f}→{t}" for p, f, t in bumps[:3]) + (" …" if len(bumps) > 3 else "")
            print(f"  #{pr['number']:<5} {lvl:<10} {pr['mergeable']:<11} {what or pr['title']}")


def cmd_check(repo):
    with repo_lock(repo):
        path, _ = checkout(repo)
        tc = Toolchain(path, repo_cfg(repo))
        g = Gate(tc, tc.requirement_files() if tc.kind == "python" else [])
    log(f"{repo} [{tc.kind}] ok={g.ok} checked: {g.summary()}; frozen={g.frozen_ok} "
        f"failures={sorted(g.failures)} unresolved={g.unresolved} notes={g.notes}")
    return 0 if g.ok else 1


def cmd_update(repo, dry_run=False, as_pr=False, run_ci=False):
    with repo_lock(repo):
        return _update(repo, dry_run, as_pr, run_ci)


def _update(repo, dry_run, as_pr, run_ci):
    prs = [p for p in open_dep_prs(repo) if not p["isDraft"]]
    if not prs:
        log(f"{repo}: no open dependency PRs")
        return 0
    path, default = checkout(repo)
    tc = Toolchain(path, repo_cfg(repo))
    log(f"{repo}: {len(prs)} dependency PRs, toolchain={tc.kind}, tests={tc.tests or 'none'}")

    safe, risky = [], []
    for pr in prs:
        lvl, bumps = pr_risk(pr)
        (safe if lvl in ("patch", "minor") else risky).append((pr, lvl, bumps))

    pr_files = {pr["number"]: [f["path"] for f in gh_json(f"pr view {pr['number']} -R {q(full_name(repo))} "
                                                          f"--json files")["files"]] for pr in prs}
    reqs = sorted({f for files in pr_files.values() for f in files
                   if re.search(r"requirements.*\.txt$|pyproject\.toml$", f)})

    def workflows_only(pr):
        """Changes nothing but GitHub Actions workflows, which no local check can run."""
        files = pr_files.get(pr["number"]) or []
        return bool(files) and all(f.startswith(".github/") for f in files)
    log("baseline: install + tests on " + default)
    base = Gate(tc, reqs)
    log(f"baseline ok={base.ok} frozen={base.frozen_ok} known failures={len(base.failures)} "
        f"unresolved={list(base.unresolved)}")
    base_sha = sh("git rev-parse HEAD", cwd=path)[1].strip()

    report = [f"# depbot report: {repo} ({datetime.date.today()})", "",
              f"Baseline `{default}` @ {base_sha[:7]}: "
              + ("pass" if base.ok else f"already failing ({'; '.join(sorted(base.failures)[:3]) or '; '.join(base.notes)})"),
              f"Checked: {base.summary()}", ""]

    if not base.installed:
        # Nothing can be tested, and "no worse than a broken baseline" would let anything through.
        report += ["**Stopped: the default branch doesn't install, so no update could be tested. "
                   "Nothing landed.**", ""]
        return _finish(repo, path, default, report, code=1)

    try:
        # Land the safe batch; drop offenders until the batch passes.
        landed, dropped = [], []
        candidates = list(safe)
        while candidates:
            reset_to(path, base_sha)
            sh(f"git checkout -q -B depbot/batch", cwd=path)
            merged, notes = [], []
            for item in candidates:
                ok, note = merge_pr(path, tc, item[0])
                if ok:
                    merged.append(item)
                    if note:
                        notes.append(f"#{item[0]['number']}: {note}")
                else:
                    dropped.append((item, note))
            fix = relock_if_broken(path, tc, base)
            if fix:
                notes.append(fix)
            if not merged:
                break
            log(f"testing batch of {len(merged)}: " + " ".join(f"#{p['number']}" for p, _, _ in merged))
            g = Gate(tc, reqs)
            good, why = g.no_worse_than(base)
            if good:
                landed = merged
                report += [f"Batch notes: {'; '.join(notes)}", ""] if notes else []
                break
            log(f"batch failed ({why}); testing PRs one at a time to find the culprit")
            culprits = []
            for item in merged:
                reset_to(path, base_sha)
                sh(f"git checkout -q -B depbot/single", cwd=path)
                ok, note = merge_pr(path, tc, item[0])
                single_ok, single_why = (False, note) if not ok else Gate(tc, reqs).no_worse_than(base)
                if not single_ok:
                    culprits.append((item, single_why))
            if not culprits:
                if len(merged) == 1:  # failed with the batch, passed alone: the tests are flaky
                    dropped.append((merged[0], "failed, then passed when tested again (flaky tests?): " + why))
                    break
                # interaction between PRs; land them one at a time over the next runs
                dropped += [(i, "passes alone but fails in the batch: " + why) for i in merged[1:]]
                candidates = merged[:1]
                continue
            dropped += culprits
            bad = {c[0][0]["number"] for c in culprits}
            candidates = [i for i in merged if i[0]["number"] not in bad]

        if landed:
            write_changelog(path, landed)
            nums = " ".join(f"#{p['number']}" for p, _, _ in landed)
            # The local gate already ran the repo's tests; skip CI unless asked, no tests ran, or
            # a workflow changed (only CI can check that).
            ci_only = [p for p, _, _ in landed if any(f.startswith(".github/") for f in pr_files.get(p["number"], []))]
            skip = " [skip ci]" if base.tested and not run_ci and not ci_only else ""
            title = f"chore(deps): {len(landed)} tested minor/patch updates"
            sh(f"git commit -q -m " + q(f"chore(deps): land {len(landed)} tested minor/patch updates{skip}\n\n"
                                        f"Tested locally by depbot against {default}@{base_sha[:7]}. Includes {nums}."),
               cwd=path)
            report += [f"## Landed (minor/patch; no new failures in: {base.summary()})", ""]
            report += [f"- #{p['number']} {p['title']}" for p, _, _ in landed]
            if not base.tested:
                report += ["", "_No tests ran locally for this repo, so CI runs on this change._"]
            elif ci_only:
                report += ["", "_" + " ".join(f"#{p['number']}" for p in ci_only)
                           + " changes GitHub Actions workflows, which only CI can check, so CI runs on this change._"]
            # With nothing checked at all, don't push straight to the default branch: a PR lets CI
            # check it first.
            if not base.checks and not as_pr:
                as_pr = True
                report += ["", "_Nothing could be checked locally, so this goes in as a PR._"]
            if dry_run:
                report += ["", f"_Dry run: not pushed. Local branch `depbot/batch` in {path}._"]
            else:
                pushed = False
                if not as_pr:
                    # Fast-forward only: refused if the default branch moved while we were testing.
                    pushed, out = sh(f"git push -q origin HEAD:refs/heads/{q(default)}", cwd=path, check=False)
                    if pushed:
                        report += ["", f"Pushed to `{default}`; GitHub marks the included PRs as merged."]
                    else:
                        report += ["", f"Couldn't push to `{default}` ({_last_err(out)}), so this goes in as a PR."]
                if not pushed:
                    url, how = open_batch_pr(repo, path, default, title,
                                             f"Landed by depbot after local tests. Includes {nums}. See {CHANGELOG}.")
                    report += ["", f"{how.capitalize()} {url}"]
        else:
            report += ["Nothing landed.", ""]

        if dropped:
            report += ["", "## Held back", ""] + [f"- #{p['number']} {p['title']}: {why}" for (p, _, _), why in dropped]

        if risky:
            report += ["", f"## Major / pre-release (tested alone, never auto-landed; checks: {base.summary()})", ""]
            for pr, lvl, bumps in risky:
                reset_to(path, base_sha)
                sh("git checkout -q -B depbot/major", cwd=path)
                ok, note = merge_pr(path, tc, pr)
                if not ok:
                    verdict = note
                elif workflows_only(pr):
                    verdict = "only changes GitHub Actions workflows, which local checks can't test"
                else:
                    good, why = Gate(tc, reqs).no_worse_than(base)
                    verdict = (("passes" if base.tested else "no tests to run; the other checks pass") if good
                               else "fails: " + why)
                report.append(f"- #{pr['number']} [{lvl}] {pr['title']}: **{verdict}**")

    except RuntimeError as e:
        # Say what happened in the report too, so a failed run still leaves a record.
        report += ["", f"**Stopped by an error:** {str(e)[:500]}"]
        return _finish(repo, path, default, report, code=2)

    return _finish(repo, path, default, report)


def _finish(repo, path, default, report, code=0):
    sh("git merge --abort", cwd=path, check=False)  # in case an error stopped a merge halfway
    try:
        reset_to(path, default)
    except RuntimeError as e:  # the report matters more than tidying the clone
        log(f"couldn't switch the clone back to {default}: {e}")
    out = CACHE / "reports" / f"{repo_dir(repo).name}-{datetime.date.today()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(report) + "\n")
    print("\n".join(report))
    log(f"report: {out}")
    return code


def main(argv=None):
    ap = argparse.ArgumentParser(prog="depbot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"depbot {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan"); s.add_argument("repos", nargs="*")
    c = sub.add_parser("check"); c.add_argument("repo")
    u = sub.add_parser("update"); u.add_argument("repo")
    u.add_argument("--dry-run", action="store_true", help="test and commit locally, don't push")
    u.add_argument("--pr", action="store_true", help="open one batch PR instead of pushing to the default branch")
    u.add_argument("--ci", action="store_true", help="let CI run on the batch commit (default: [skip ci])")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "scan":
            repos = a.repos or list(load_overrides())
            if not repos:
                sys.exit("no repos given, and no repos.json found (see the README for where depbot looks)")
            cmd_scan(repos)
        elif a.cmd == "check":
            sys.exit(cmd_check(a.repo))
        else:
            sys.exit(cmd_update(a.repo, a.dry_run, a.pr, a.ci))
    except RuntimeError as e:
        log(f"error: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
