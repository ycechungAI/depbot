"""PR parsing and risk levels, checked against real Dependabot PRs (tests/fixtures/prs.json holds
the part of each PR body that Dependabot wrote; release notes are cut)."""
import json
from pathlib import Path

import pytest

import depbot

PRS = json.loads((Path(__file__).parent / "fixtures" / "prs.json").read_text())


@pytest.mark.parametrize("key,level,first,count", [
    ("npm-group-table-61", "minor", ("@radix-ui/react-alert-dialog", "1.2.0", "1.2.1"), 17),  # grouped, table
    ("npm-group-79", "minor", ("axios", "1.20.0", "1.21.0"), 2),        # grouped, "Updates" lines only
    ("npm-security-112", "patch", ("markdown-it", "14.3.2", "14.3.3"), 1),   # security group of one
    ("actions-78", "major", ("actions/setup-node", "4", "7"), 1),     # GitHub Action
    ("electron-80", "major", ("electron", "44.9.1", "45.1.0"), 1),
    ("google-research-1", "major", ("tensorflow", "1.15rc2", "2.12.1"), 1),  # from a release candidate
    ("google-research-48", "minor", ("fsspec", "2025.12.0", "2026.6.0"), 1),  # calendar version
    ("google-research-18", "minor", ("torch", "2.6.0", "2.13.0"), 1),
    ("awesome-llm-apps-5", "minor", ("pillow", "10.0.0", "10.3.0"), 1),
    ("GPT3-wellbeingAssist-27", "major", ("pillow", "12.3.0", "13.0.0"), 1),
    ("OpenManus-7", "minor", ("axios", "1.8.4", "1.21.0"), 1),
    ("cmd-ssg-docs-39", "minor", ("baseline-browser-mapping", "2.9.15", "2.11.21"), 1),
])
def test_real_prs(key, level, first, count):
    lvl, bumps = depbot.pr_risk(PRS[key])
    assert (lvl, bumps[0], len(bumps)) == (level, first, count)


def test_grouped_table_lists_every_package_once():
    _, bumps = depbot.pr_risk(PRS["npm-group-table-61"])
    names = [b[0] for b in bumps]
    assert len(names) == len(set(names)) == 17
    assert ("vitest", "5.0.3", "5.0.5") in bumps


def test_bumps_quoted_in_release_notes_are_ignored():
    pr = {"title": "Bump foo from 1.0.0 to 1.0.1",
          "body": "Bumps [foo](u) from 1.0.0 to 1.0.1.\n<blockquote>\n> Bumps bar from 1.0.0 to 9.0.0\n"
                  "  Updates baz from 1.0.0 to 9.0.0\n</blockquote>\n"}
    assert depbot.pr_risk(pr) == ("patch", [("foo", "1.0.0", "1.0.1")])


def test_title_is_the_fallback():
    pr = {"title": "chore(deps): bump requests from 2.31.0 to 2.32.3", "body": ""}
    assert depbot.pr_risk(pr) == ("minor", [("requests", "2.31.0", "2.32.3")])


def test_unknown_shape_is_never_auto_landed():
    assert depbot.pr_risk({"title": "Update dependencies", "body": "Some text"}) == ("major", [])


@pytest.mark.parametrize("frm,to,level", [
    ("1.2.3", "1.2.4", "patch"),
    ("v1.2.3", "v1.3.0", "minor"),
    ("1.2.3", "2.0.0", "major"),
    ("0.4.1", "0.5.0", "major"),       # 0.x minor is breaking
    ("0.4.1", "0.4.2", "patch"),
    ("0.0.3", "0.0.4", "major"),       # 0.0.x: every change is breaking
    ("1.2.3", "1.3.0rc1", "prerelease"),
    ("1.2.3", "2.0.0-beta.2", "prerelease"),
    ("1.0.0rc1", "1.0.0rc2", "patch"),  # already on a release candidate
    ("8e5e7e5", "0ad4b8f", "major"),   # pinned commit hash: unknown, never auto-landed
    ("20-alpine", "22-alpine", "major"),
    ("2026.7.22", "2026.10.18", "minor"),
])
def test_risk(frm, to, level):
    assert depbot.risk(frm, to) == level


def test_calendar_year_change_counts_as_minor():
    pr = {"title": "Bump certifi from 2025.4.26 to 2026.10.18", "body": ""}
    assert depbot.pr_risk(pr)[0] == "minor"
