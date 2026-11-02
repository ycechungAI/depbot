"""`depbot update` end to end, against throwaway repos and a fake gh (see conftest.py)."""
import datetime
import signal

import pytest

import depbot
from conftest import LOCK, git


@pytest.fixture(autouse=True)
def deadline():
    """A run that loops forever fails here instead of hanging the suite."""
    def stop(*_):
        raise TimeoutError("depbot didn't finish within 60 s")
    signal.signal(signal.SIGALRM, stop)
    signal.alarm(60)
    yield
    signal.alarm(0)


def update(**kw):
    return depbot.cmd_update("app", kw.get("dry_run", False), kw.get("as_pr", False), kw.get("run_ci", False))


def test_lands_safe_updates_holds_back_breakage_and_reports_majors(world):
    o = world.origin
    o.add_pr(1, {"deps/a.txt": "1.0.1\n"}, to="1.0.1")
    o.add_pr(2, {"deps/b.txt": "1.1.0\n"}, to="1.1.0")
    o.add_pr(3, {"deps/c.txt": "broken\n"}, to="1.0.1")
    o.add_pr(4, {"deps/d.txt": "2.0.0\n"}, pkg="d", to="2.0.0")
    assert update() == 0
    assert o.pr_landed(1) and o.pr_landed(2)
    assert not o.pr_landed(3) and not o.pr_landed(4)
    assert o.on_main("deps/a.txt") == "1.0.1\n" and o.on_main("deps/d.txt") == ""
    log = o.log_main()
    assert "chore(deps): land 2 tested minor/patch updates [skip ci]" in log and "Includes #1 #2." in log
    changelog = o.on_main("DEPENDENCY_UPDATES.md")
    assert "| a | 1.0.0 | 1.0.1 | patch | #1 |" in changelog and "| b | 1.0.0 | 1.1.0 | minor | #2 |" in changelog
    report = world.report()
    assert "Pushed to `main`" in report
    assert "- #3 Bump c from 1.0.0 to 1.0.1: new failures: tests/test_deps.py::test_c" in report
    assert "- #4 [major] Bump d from 1.0.0 to 2.0.0: **passes**" in report
    assert "Checked: install, `sh check.sh`" in report


def test_dry_run_pushes_nothing(world):
    o = world.origin
    o.add_pr(1, {"deps/a.txt": "1.0.1\n"})
    before = o.main_sha()
    assert update(dry_run=True) == 0
    assert o.main_sha() == before
    assert "Dry run: not pushed" in world.report()
    assert not world.state.calls("pr", "create")


def test_conflicting_prs_land_one_and_hold_the_other(world):
    o = world.origin
    o.add_pr(1, {"deps/a.txt": "1.0.1\n"}, to="1.0.1")
    o.add_pr(2, {"deps/a.txt": "1.0.2\n"}, to="1.0.2")
    assert update() == 0
    assert o.pr_landed(1) and not o.pr_landed(2)
    assert "- #2 Bump a from 1.0.0 to 1.0.2: conflicts in deps/a.txt" in world.report()


def test_lockfile_conflict_is_regenerated(world, monkeypatch):
    o = world.origin
    o.write({"lock.sh": LOCK})
    git(o.work, "add", "-A")
    git(o.work, "commit", "-q", "-m", "lock.sh")
    git(o.work, "push", "-q", "origin", "main")
    git(o.work, "-c", "core.hooksPath=/dev/null", "status")  # keep the tree clean
    import subprocess
    subprocess.run(["sh", "lock.sh"], cwd=o.work, check=True)
    o.commit_and_push("add lockfile")

    def pr_with_lock(n, pkg, ver):
        files = {f"deps/{pkg}.txt": ver + "\n"}
        o.write(files)  # compute the PR's own lockfile from main + this change
        lock = subprocess.run(["sh", "lock.sh", "-"], cwd=o.work, capture_output=True, text=True).stdout
        git(o.work, "checkout", "-q", "--", "deps")
        o.add_pr(n, {**files, "package-lock.json": lock}, pkg=pkg, to=ver)

    pr_with_lock(1, "a", "1.0.1")
    pr_with_lock(2, "b", "1.0.1")  # its lockfile line sits next to #1's: a textual conflict

    class LockToolchain(depbot.Toolchain):
        def __init__(self, root, cfg):
            super().__init__(root, cfg)
            self.lockfile, self.relock = "package-lock.json", "sh lock.sh"

    monkeypatch.setattr(depbot, "Toolchain", LockToolchain)
    assert update() == 0
    assert o.pr_landed(1) and o.pr_landed(2)
    assert o.on_main("package-lock.json") == "a==1.0.1\nb==1.0.1\nc==1.0.0\n"
    assert "#2: lockfile regenerated" in world.report()


def test_prs_that_fail_only_together(world):
    o = world.origin
    o.add_pr(1, {"deps/a.txt": "x2\n"})
    o.add_pr(2, {"deps/b.txt": "x2\n"})
    assert update() == 0
    assert o.pr_landed(1) and not o.pr_landed(2)
    assert "- #2 Bump b from 1.0.0 to 1.0.1: passes alone but fails in the batch" in world.report()


def test_flaky_test_on_a_single_pr_doesnt_loop_forever(world):
    o = world.origin
    o.add_pr(1, {"deps/flaky.txt": "flaky\n"}, pkg="flaky")
    before = o.main_sha()
    assert update() == 0
    assert o.main_sha() == before
    assert "flaky tests?" in world.report()


def test_forked_lookalike_branches_are_ignored(world):
    """A stranger's PR from a fork, with a branch named like Dependabot's, is never run or landed."""
    o = world.origin
    o.add_pr(1, {"deps/a.txt": "pwned\n"}, author="mallory", cross=True, head="dependabot/npm/a-1.0.1")
    o.add_pr(2, {"deps/b.txt": "pwned\n"}, author="mallory", head="dependabot/npm/b-1.0.1")
    before = o.main_sha()
    assert update() == 0
    assert o.main_sha() == before
    assert not (world.tmp / "cache" / "app").exists()  # never even cloned


def test_rebased_pr_on_a_later_run(world):
    """Dependabot rewrites a PR's branch when it rebases it; the next run must pick up the new head."""
    o = world.origin
    o.add_pr(1, {"deps/a.txt": "1.0.1\n"})
    assert update(dry_run=True) == 0
    o.rewrite_pr(1, {"deps/a.txt": "1.0.2\n"})
    assert update() == 0
    assert o.on_main("deps/a.txt") == "1.0.2\n"


def test_refused_push_falls_back_to_a_pr(world, monkeypatch):
    """Someone pushes to main while depbot is testing: the push is refused, so a PR is opened."""
    o = world.origin
    o.add_pr(1, {"deps/a.txt": "1.0.1\n"})
    real = depbot.write_changelog

    def meanwhile(path, landed):
        o.write({"README.md": "someone else's change\n"})
        o.commit_and_push("meanwhile")
        real(path, landed)

    monkeypatch.setattr(depbot, "write_changelog", meanwhile)
    moved_to = None
    assert update() == 0
    assert o.on_main("README.md") == "someone else's change\n" and not o.pr_landed(1)
    created = world.state.calls("pr", "create")
    assert len(created) == 1 and f"depbot/{datetime.date.today()}" in created[0]
    report = world.report()
    assert "Couldn't push to `main`" in report and "Opened https://github.com/octo/app/pull/" in report
    assert git(o.bare, "rev-parse", f"depbot/{datetime.date.today()}")  # the branch is there


def test_pr_mode_twice_in_a_day_updates_the_same_pr(world):
    o = world.origin
    o.add_pr(1, {"deps/a.txt": "1.0.1\n"})
    assert update(as_pr=True) == 0
    o.add_pr(2, {"deps/b.txt": "1.0.1\n"})
    assert update(as_pr=True) == 0
    assert len(world.state.calls("pr", "create")) == 1
    assert "Updated https://github.com/octo/app/pull/" in world.report()
    branch = f"depbot/{datetime.date.today()}"
    assert git(o.bare, "show", f"{branch}:deps/b.txt") == "1.0.1"


def test_default_branch_that_wont_install_stops_the_run(world):
    o = world.origin
    o.write({"deps/z.txt": "noinstall\n"})
    o.commit_and_push("break the install")
    o.add_pr(1, {"deps/a.txt": "1.0.1\n"})
    before = o.main_sha()
    assert update() == 1
    assert o.main_sha() == before
    assert "Stopped: the default branch doesn't install" in world.report()


def test_without_tests_ci_runs_and_the_report_says_so(world):
    world.cfg["app"]["test"] = []
    o = world.origin
    o.add_pr(1, {"deps/a.txt": "1.0.1\n"})
    o.add_pr(2, {"deps/d.txt": "2.0.0\n"}, pkg="d", to="2.0.0")
    assert update() == 0
    assert o.pr_landed(1)
    assert "[skip ci]" not in o.log_main()
    report = world.report()
    assert "No tests ran locally for this repo, so CI runs on this change." in report
    assert "**no tests to run; the other checks pass**" in report


def test_nothing_checkable_goes_in_as_a_pr(world):
    world.cfg["app"] = {}
    o = world.origin
    o.add_pr(1, {"deps/a.txt": "1.0.1\n"})
    before = o.main_sha()
    assert update() == 0
    assert o.main_sha() == before and len(world.state.calls("pr", "create")) == 1
    assert "Nothing could be checked locally, so this goes in as a PR." in world.report()


def test_returns_to_the_default_branch(world):
    o = world.origin
    o.add_pr(1, {"deps/a.txt": "1.0.1\n"})
    update()
    clone = world.tmp / "cache" / "app"
    assert git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(clone, "status", "--porcelain") == ""


def test_a_pr_that_cant_be_fetched_is_reported_and_the_run_goes_on(world, monkeypatch):
    monkeypatch.setattr(depbot.time, "sleep", lambda s: None)
    o = world.origin
    o.add_pr(1, {"deps/a.txt": "1.0.1\n"})
    o.add_pr(2, {"deps/d.txt": "2.0.0\n"}, pkg="d", to="2.0.0")
    git(o.bare, "update-ref", "-d", "refs/pull/2/head")  # GitHub didn't answer for this one
    assert update() == 0
    assert o.pr_landed(1)
    assert "- #2 [major] Bump d from 1.0.0 to 2.0.0: **couldn't fetch the PR:" in world.report()


def test_an_error_mid_run_still_writes_the_report(world, monkeypatch):
    o = world.origin
    o.add_pr(1, {"deps/a.txt": "1.0.1\n"})

    def broken(*a, **kw):
        raise RuntimeError("gh: HTTP 502")

    monkeypatch.setattr(depbot, "open_batch_pr", broken)
    assert update(as_pr=True) == 2
    report = world.report()
    assert "- #1 Bump a from 1.0.0 to 1.0.1" in report and "**Stopped by an error:** gh: HTTP 502" in report
    clone = world.tmp / "cache" / "app"
    assert git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_workflow_changes_are_left_to_ci(world):
    o = world.origin
    o.add_pr(1, {".github/workflows/ci.yml": "uses: actions/setup-node@v7\n"}, pkg="actions/setup-node",
             frm="4", to="7")
    o.add_pr(2, {".github/workflows/release.yml": "uses: actions/cache@v4.2\n"}, pkg="actions/cache",
             frm="4.1.0", to="4.2.0")
    assert update() == 0
    assert o.pr_landed(2) and not o.pr_landed(1)
    assert "[skip ci]" not in o.log_main()
    report = world.report()
    assert "#2 changes GitHub Actions workflows, which only CI can check, so CI runs on this change." in report
    assert "**only changes GitHub Actions workflows, which local checks can't test**" in report
