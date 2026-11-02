# depbot

Test Dependabot's PRs on your own machine, then land the safe ones in a single push.

Dependabot opens one PR per update, and each PR runs CI. depbot merges every open minor and patch
update into one batch, runs the repo's own install and tests on it, and pushes the batch to the
default branch in **one push** if nothing got worse. A push like that is one CI run instead of one
per PR (or none: the commit carries `[skip ci]`, because the tests already ran). GitHub marks the
included PRs as merged. Majors and prereleases are tested one at a time and reported, never landed.
Each landed update is logged in the repo's `DEPENDENCY_UPDATES.md`.

## Install

```bash
uvx --from git+https://github.com/ycechungAI/depbot@v0.1.0 depbot --help   # run it without installing (needs uv)
pipx install git+https://github.com/ycechungAI/depbot@v0.1.0              # or install it
```

depbot isn't on PyPI yet; these install the tagged release from GitHub.

Or clone the repo and run `./run.sh` (it runs `depbot scan`, or any depbot command you pass it).

depbot needs `git`, and [`gh`](https://cli.github.com) logged in (`gh auth login`). It runs each
repo's own tools: `node`/`npx` for JavaScript, `uv` for Python, `go` for Go.

## Commands

```bash
depbot scan                        # open dependency PRs in every repo in repos.json, with risk level
depbot scan my-app octo/other      # just these repos
depbot check my-app                # run the install and tests on the default branch only
depbot update my-app --dry-run     # test and commit locally, don't push
depbot update my-app               # test, then push to the default branch
depbot update my-app --pr          # open one batch PR instead of pushing
depbot update my-app --ci          # don't add [skip ci] to the batch commit
```

A repo is `name` (one of yours) or `owner/name`. Reports go to `<cache>/reports/<repo>-<date>.md`.

## What `update` does

1. **Baseline.** Install and test the default branch. Failures that are already there (a flaky
   network test, a broken lint rule) are recorded, so they don't block updates. If the default
   branch doesn't install at all, depbot stops: nothing can be tested, so nothing lands.
2. **Batch.** Merge every open minor/patch Dependabot PR into one branch. If two PRs conflict only
   in a lockfile, the package manager regenerates it. If git merges lockfiles cleanly but leaves an
   invalid file, depbot catches that with a frozen install and regenerates it.
3. **Gate.** The batch must not add new failures compared to the baseline:
   - the frozen install still works,
   - the `lint`, `typecheck` and `test` scripts (or `build` if there are no tests) pass; failures
     are compared per test name, per ESLint file and rule, and per `tsc` error,
   - every changed `requirements*.txt` / `pyproject.toml` still resolves (`uv pip compile`, Linux
     target), so a bump that strands a pinned sibling (torch and torchvision) is rejected.

   If the batch fails, each PR is tested alone and the ones that break it are held back. PRs that
   pass alone but fail together land one at a time over the next runs. A PR whose test fails and
   then passes on the same code is held back as flaky.
4. **Land.** Append to `DEPENDENCY_UPDATES.md`, commit, and push. If the push is refused (branch
   protection, or someone pushed while depbot was testing), depbot opens a PR instead. Running
   `--pr` twice on the same day updates the same PR.
5. **Majors and prereleases** are tested one by one and reported. They're never landed
   automatically.

Every report says exactly what was checked. A repo with no tests gets "no tests to run" rather
than "passes", its batch commit lets CI run, and a repo where nothing at all could be checked gets
a PR instead of a push. A PR that only changes GitHub Actions workflows is reported as something
only CI can check, and a batch that touches a workflow lets CI run.

A network hiccup doesn't end a run: GitHub calls are retried, a PR that still can't be fetched is
reported and skipped, and an error part-way still writes the report.

Risk levels come from the version numbers: a major change, any `0.x` minor change, any `0.0.x`
change, any `rc`/`alpha`/`beta` target and any pinned commit hash count as risky. Calendar versions
such as certifi's `2026.7.22 → 2026.10.18` count as minor.

## Safety

- Only PRs opened by Dependabot itself, from the same repo, are touched. A PR from a fork whose
  branch is named `dependabot/...` is ignored, since depbot installs and runs a PR's code.
- The push to the default branch is fast-forward only: it never overwrites someone else's work.
- One run at a time per repo; a second run on the same repo stops with a message.
- A command that runs past its time limit (30 minutes) counts as a failure, not a crash.

## Settings

Toolchains are detected automatically (pnpm, Yarn 1, Yarn Berry via `packageManager`, npm, Go,
Python). Override only what differs, in `repos.json` (see [`repos.example.json`](https://github.com/ycechungAI/depbot/blob/main/repos.example.json)):

```json
{
  "my-app": {},
  "octo/site": { "dir": "web", "setup": "npm ci --prefix worker" },
  "ml-lab": { "python": ["3.10", "3.11"] }
}
```

| Key | Meaning | Example |
|---|---|---|
| `dir` | Project directory inside the repo | `"node"` |
| `setup` | Extra install steps CI runs before tests | `"npm ci --prefix worker"` |
| `test` | Replace the detected check commands | `["npm test", "npm run build"]` |
| `install` | Replace the detected install command | `"make deps"` |
| `python` | Python versions to resolve against, in order | `["3.10", "3.11"]` |

Match `setup` and `test` to what the repo's CI runs, or the local gate will disagree with CI.

depbot reads the first `repos.json` it finds: `$DEPBOT_REPOS`, then the current folder, then next
to `depbot.py`, then `~/.config/depbot/`.

| Variable | Default |
|---|---|
| `DEPBOT_OWNER` | the user `gh` is logged in as |
| `DEPBOT_CACHE` (clones and reports) | `~/.cache/depbot` |
| `DEPBOT_REPOS` | see above |
| `DEPBOT_GIT_URL` (for GitHub Enterprise or a mirror) | `https://github.com/{repo}.git` |

## GitHub Action

Run depbot in the repo itself, for example every Monday:

```yaml
name: depbot
on:
  schedule: [{ cron: "0 6 * * 1" }]
  workflow_dispatch:
permissions:
  contents: write
  pull-requests: write
jobs:
  depbot:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-node@v4   # whatever your tests need
        with: { node-version: 22 }
      - uses: ycechungAI/depbot@v0.1.0
        with:
          mode: push   # or pr
```

The default `GITHUB_TOKEN` can't push changes to `.github/workflows/`. If Dependabot updates your
Actions, pass a token with the `workflow` scope as `token`.

## Fewer Dependabot PRs

[`examples/dependabot.yml`](https://github.com/ycechungAI/depbot/blob/main/examples/dependabot.yml) turns routine version PRs off and groups
security fixes into one PR per ecosystem, so depbot handles the routine updates. Copy it to a
repo's `.github/dependabot.yml` and list that repo's ecosystems and directories.

## Tests

```bash
uv run --no-project --with pytest python -m pytest -q
```

The tests run depbot end to end against throwaway git repos and a fake `gh`: batching, conflicts,
lockfile regeneration, flaky tests, rebased PRs, refused pushes, forked look-alike PRs and a
default branch that won't install. The parsing tests use real Dependabot PRs.

## License

MIT
