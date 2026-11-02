"""Failure parsing, the gate, and the shell and git helpers."""
import subprocess
import time
from types import SimpleNamespace

import pytest

import depbot


def test_jest_vitest_go_pytest_eslint_tsc():
    out = "\n".join([
        "  ● Cart › adds an item",
        " FAIL  src/cart.test.ts > Cart > removes an item 12ms",
        "    ✕ totals the cart (5 ms)",
        "--- FAIL: TestParse (0.00s)",
        "FAILED tests/test_api.py::test_login - AssertionError: nope",
        "/repo/src/app.ts",
        "  12:5  error  'x' is never used  @typescript-eslint/no-unused-vars",
        "src/a.ts(3,7): error TS2322: Type 'string' is not assignable to type 'number'.",
    ])
    names = depbot.failing_tests(out, "/repo")
    assert {"Cart › adds an item", "src/cart.test.ts > Cart > removes an item", "totals the cart", "TestParse",
            "tests/test_api.py::test_login", "lint src/app.ts: @typescript-eslint/no-unused-vars",
            "tsc src/a.ts: TS2322"} <= names


def test_colors_dont_change_names():
    assert depbot.failing_tests("\x1b[31m--- FAIL: TestX\x1b[0m (0.01s)") == {"TestX"}


def tc(tmp_path, frozen=None, loose=None, tests=(), setup=(), relock=None):
    return SimpleNamespace(dir=tmp_path, root=tmp_path, frozen=frozen, loose=loose or frozen, tests=list(tests),
                           setup=list(setup), relock=relock, lockfile=None, cfg={},
                           resolves=lambda req: (True, "py3.11"))


def test_install_failure_is_never_no_worse(tmp_path):
    """The default branch already fails its tests and the update breaks the install: that's worse."""
    base = depbot.Gate(tc(tmp_path, frozen="true", tests=["exit 1"]))
    broken = depbot.Gate(tc(tmp_path, frozen="false", tests=["exit 1"]))
    assert not base.ok and not broken.ok
    ok, why = broken.no_worse_than(base)
    assert not ok and "install failed" in why


def test_unparsed_failures_ignore_timings(tmp_path):
    """A test runner depbot can't parse: the same failure on two runs must compare equal."""
    run = 'echo "Error: 3 failed in $(cat took)s"; exit 1'
    (tmp_path / "took").write_text("1.24")
    first = depbot.Gate(tc(tmp_path, tests=[run]))
    (tmp_path / "took").write_text("2.71")
    second = depbot.Gate(tc(tmp_path, tests=[run]))
    assert second.no_worse_than(first) == (True, "")


def test_new_failure_is_worse(tmp_path):
    base = depbot.Gate(tc(tmp_path, tests=["echo '--- FAIL: TestA'; exit 1"]))
    new = depbot.Gate(tc(tmp_path, tests=["echo '--- FAIL: TestA'; echo '--- FAIL: TestB'; exit 1"]))
    ok, why = new.no_worse_than(base)
    assert not ok and "TestB" in why and "TestA" not in why


def test_frozen_lockfile_regression_is_worse(tmp_path):
    base = depbot.Gate(tc(tmp_path, frozen="true"))
    new = depbot.Gate(tc(tmp_path, frozen="false", loose="true"))
    assert new.no_worse_than(base)[0] is False


def test_summary_says_what_was_checked(tmp_path):
    g = depbot.Gate(tc(tmp_path, frozen="true", tests=["true"], setup=["true"]), reqs=["requirements.txt"])
    assert g.summary() == "install, `true`, `true`, 1 requirements file(s) resolve" and g.tested
    empty = depbot.Gate(tc(tmp_path))
    assert empty.summary().startswith("nothing") and not empty.tested


def test_timeout_is_a_failure_not_a_crash():
    start = time.time()
    ok, out = depbot.sh("echo started; sleep 5", check=False, timeout=1)
    assert not ok and "timed out" in out and time.time() - start < 4


def test_requirement_files_with_spaces(tmp_path):
    subprocess.run("git init -q && mkdir -p 'my dir' && touch 'my dir/requirements.txt' pyproject.toml "
                   "&& git add -A", shell=True, cwd=tmp_path, check=True)
    t = depbot.Toolchain(tmp_path, {})
    assert sorted(t.requirement_files()) == ["my dir/requirements.txt", "pyproject.toml"]


def test_toolchain_detection(tmp_path):
    (tmp_path / "package.json").write_text('{"packageManager": "pnpm@9.1.0", "scripts": {"test": "vitest", "lint": "eslint ."}}')
    t = depbot.Toolchain(tmp_path, {})
    assert t.kind == "pnpm" and t.frozen == "npx -y pnpm@9 install --frozen-lockfile"
    assert t.tests == ["npx -y pnpm@9 run lint", "npx -y pnpm@9 run test"]
    (tmp_path / "package.json").write_text('{"scripts": {"test": "echo \\"Error: no test specified\\"", "build": "tsc"}}')
    assert depbot.Toolchain(tmp_path, {}).tests == ["npm run build"]


def test_owner_names_and_clone_folders(monkeypatch):
    monkeypatch.setattr(depbot, "_owner", "octo")
    assert depbot.full_name("app") == "octo/app"
    assert depbot.full_name("someone/app") == "someone/app"
    assert depbot.repo_dir("app").name == "app"
    assert depbot.repo_dir("octo/app").name == "app"
    assert depbot.repo_dir("someone/app").name == "someone__app"  # never shares octo/app's clone


def test_one_run_per_clone(tmp_path, monkeypatch):
    monkeypatch.setattr(depbot, "CACHE", tmp_path)
    monkeypatch.setattr(depbot, "_owner", "octo")
    with depbot.repo_lock("app"):
        with pytest.raises(RuntimeError, match="already working on octo/app"):
            with depbot.repo_lock("app"):
                pass
        with depbot.repo_lock("other"):  # another repo is fine
            pass


def test_config_file_lookup(tmp_path, monkeypatch):
    monkeypatch.delenv("DEPBOT_REPOS", raising=False)
    monkeypatch.setattr(depbot, "HERE", tmp_path / "nowhere")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "conf"))
    monkeypatch.chdir(tmp_path)
    assert depbot.config_file() is None
    (tmp_path / "conf" / "depbot").mkdir(parents=True)
    (tmp_path / "conf" / "depbot" / "repos.json").write_text('{"x": {"dir": "web"}}')
    assert depbot.load_overrides() == {"x": {"dir": "web"}}
    (tmp_path / "repos.json").write_text('{"y": {}}')  # the current folder wins
    assert depbot.load_overrides() == {"y": {}}
    monkeypatch.setenv("DEPBOT_REPOS", str(tmp_path / "conf" / "depbot" / "repos.json"))
    assert depbot.load_overrides() == {"x": {"dir": "web"}}


def test_cli_version_and_missing_config(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        depbot.main(["--version"])
    assert e.value.code == 0 and capsys.readouterr().out.strip() == f"depbot {depbot.VERSION}"
    monkeypatch.setattr(depbot, "load_overrides", lambda: {})
    with pytest.raises(SystemExit) as e:
        depbot.main(["scan"])
    assert "no repos given" in str(e.value.code)


def test_network_commands_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(depbot.time, "sleep", lambda s: None)
    count = tmp_path / "n"
    flaky = f'n=$(cat {count} 2>/dev/null || echo 0); echo $((n+1)) > {count}; [ $n -ge 2 ]'
    assert depbot.sh_retry(flaky)[0] is True and count.read_text().strip() == "3"
    count.unlink()
    assert depbot.sh_retry(flaky, tries=2, check=False)[0] is False
    count.unlink()
    with pytest.raises(RuntimeError):
        depbot.sh_retry(flaky, tries=1)
