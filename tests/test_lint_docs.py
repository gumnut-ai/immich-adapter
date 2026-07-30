"""Tests for scripts/lint_docs.py.

The freshness tests are integration tests, ported from the bash harness this file
replaces (`scripts/lint_docs_test.sh`, since removed as spent): each builds a
throwaway git repo with a `main` branch, makes working-tree edits, and asserts the
linter's report. Running the real script against a real repo is what pins the
git-plumbing behavior (merge base, `--diff-filter`, `git show <rev>:<path>`) that a
unit test of the pure helpers would miss.

The clock is pinned via `LINT_DOCS_TODAY` / `LINT_DOCS_NOW_EPOCH` so a fixture
and the assertions cannot straddle a date boundary.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lint_docs import (
    blank_frontmatter,
    bump_date_line,
    collect_anchors,
    has_last_updated_key,
    parse_frontmatter,
    raw_cell_count,
    slugify_heading,
    strip_date_line,
)

# Resolved from the repo root rather than this file's directory, so this test file
# stays copyable verbatim into repos that keep their tests somewhere other than
# alongside the script (immich-adapter collects from `tests/`). Each repo's pytest
# config puts `scripts/` on the import path for the `lint_docs` import above.
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "lint_docs.py"

NOW_EPOCH = int(time.time())


def utc_date(offset_seconds: int = 0) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(NOW_EPOCH + offset_seconds))


TODAY = time.strftime("%Y-%m-%d", time.localtime(NOW_EPOCH))
UTC_TODAY = utc_date()
EARLIEST_CURRENT_DATE = utc_date(-43_200)
LATEST_CURRENT_DATE = utc_date(50_400)


def _outside_timezone_window() -> str:
    """A date no timezone currently considers "today".

    Adjacent UTC dates are usually outside the window, but during the short
    interval where all three are current somewhere, two days ago still isn't.
    """
    current = {EARLIEST_CURRENT_DATE, UTC_TODAY, LATEST_CURRENT_DATE}
    for candidate in (utc_date(-86_400), utc_date(86_400)):
        if candidate not in current:
            return candidate
    return utc_date(-172_800)


OUTSIDE_TIMEZONE_WINDOW = _outside_timezone_window()

# Every check on, no exemptions — the fixture repos are built to exercise one
# check at a time, so inheriting this repo's config would only add noise.
FIXTURE_CONFIG = """
strip_prefixes = ["repo-root "]
self_prefixes = []
template_paths = ["docs/design-docs/TEMPLATE.md"]

[checks]
freshness = true
links = true
anchors = true
map_paths = true
map_status_section = true
map_cells = true
frontmatter = true

[limits]
consult_cell_chars = 250
"""


# --------------------------------------------------------------------------
# Fixture repo plumbing
# --------------------------------------------------------------------------


class FixtureRepo:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.config_path = path / "lint_docs.toml"
        self.config_path.write_text(FIXTURE_CONFIG, encoding="utf-8")

    def git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.path), *args], check=True, capture_output=True
        )

    def write(self, rel: str, content: str) -> None:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def read(self, rel: str) -> str:
        return (self.path / rel).read_text(encoding="utf-8")

    def write_doc(self, rel: str, last_updated: str | None, body: str) -> None:
        lines = ["---", "title: Example"]
        if last_updated is not None:
            lines.append(f"last-updated: {last_updated}")
        lines += ["---", "", body, ""]
        self.write(rel, "\n".join(lines))

    def commit_all(self, message: str = "baseline") -> None:
        self.git("add", "-A")
        self.git("commit", "-qm", message)

    def lint(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--config", str(self.config_path), *args],
            cwd=self.path,
            capture_output=True,
            text=True,
            # A non-zero exit is the thing under test.
            check=False,
            env={
                # A minimal env, so a developer's own LINT_DOCS_* overrides can't
                # unpin the clock the assertions depend on.
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(self.path),
                "LINT_DOCS_TODAY": TODAY,
                "LINT_DOCS_NOW_EPOCH": str(NOW_EPOCH),
            },
        )


@pytest.fixture
def repo(tmp_path: Path) -> FixtureRepo:
    fixture = FixtureRepo(tmp_path)
    fixture.git("-c", "init.defaultBranch=main", "init", "-q")
    fixture.git("config", "user.email", "test@example.com")
    fixture.git("config", "user.name", "Test")
    return fixture


# --------------------------------------------------------------------------
# freshness — the cases the bash harness pinned
# --------------------------------------------------------------------------


@pytest.fixture
def freshness_repo(repo: FixtureRepo) -> FixtureRepo:
    """The replaced harness's baseline and working-tree edits, ported verbatim."""
    repo.write_doc("clean.md", "2020-01-01", "untouched body")
    repo.write_doc("no_bump.md", "2020-01-01", "original body")
    repo.write_doc("bumped.md", "2020-01-01", "original body")
    repo.write_doc("date_only.md", "2020-01-01", "stable body")
    repo.write_doc("bad_date.md", "2020-01-01", "original body")
    repo.write_doc("date_only_bad.md", "2020-01-01", "stable body")
    repo.write_doc("blank_value.md", "2020-01-01", "stable body")
    repo.write_doc("same_day.md", TODAY, "original body")
    repo.write_doc("timezone_behind.md", EARLIEST_CURRENT_DATE, "original body")
    repo.write_doc("timezone_ahead.md", LATEST_CURRENT_DATE, "original body")
    repo.write_doc("stale_unchanged.md", OUTSIDE_TIMEZONE_WINDOW, "original body")
    repo.write_doc("backward_bump.md", "2020-01-01", "original body")
    repo.write_doc("impossible_date.md", "2020-01-01", "stable body")
    # A doc that documents the convention: no frontmatter field, but the body
    # mentions `last-updated:`. Must be out of scope.
    repo.write(
        "conventions.md",
        "---\ntitle: Conventions\n---\n\n"
        "Docs must include `last-updated: 2020-01-01` in frontmatter.\n",
    )
    repo.commit_all()

    repo.write_doc("no_bump.md", "2020-01-01", "EDITED body")
    repo.write_doc("bumped.md", TODAY, "EDITED body")
    repo.write_doc("date_only.md", TODAY, "stable body")
    repo.write_doc("bad_date.md", "not-a-date", "EDITED body")
    repo.write_doc("date_only_bad.md", "not-a-date", "stable body")
    # Key present, value blanked out, body unchanged.
    repo.write(
        "blank_value.md",
        "---\ntitle: Example\nlast-updated:\n---\n\nstable body\n",
    )
    repo.write_doc("same_day.md", TODAY, "EDITED body")
    repo.write_doc("timezone_behind.md", EARLIEST_CURRENT_DATE, "EDITED body")
    repo.write_doc("timezone_ahead.md", LATEST_CURRENT_DATE, "EDITED body")
    repo.write_doc("stale_unchanged.md", OUTSIDE_TIMEZONE_WINDOW, "EDITED body")
    # Body edited and the date *did* change from its base value — but backwards, to
    # another stale date. Keying on "differs from base" would accept this.
    repo.write_doc("backward_bump.md", "2019-12-31", "EDITED body")
    # Date-only edit to a value that is ISO-*shaped* but not a real calendar date.
    repo.write_doc("impossible_date.md", "2026-02-31", "stable body")
    repo.write(
        "conventions.md",
        "---\ntitle: Conventions\n---\n\n"
        "EDITED body, still cites `last-updated: 2020-01-01`.\n",
    )
    return repo


@pytest.mark.parametrize(
    ("doc", "reason"),
    [
        ("no_bump.md", "body changed with no bump"),
        ("bad_date.md", "body changed and the date is junk"),
        ("date_only_bad.md", "date-only edit to a junk value"),
        ("blank_value.md", "date value blanked out"),
        ("stale_unchanged.md", "date is outside the timezone window"),
        ("backward_bump.md", "date changed from base, but backwards to a stale date"),
        ("impossible_date.md", "ISO-shaped but not a real calendar date"),
    ],
)
def test_freshness_flags(freshness_repo: FixtureRepo, doc: str, reason: str) -> None:
    result = freshness_repo.lint("--check", "freshness", "--base", "main")
    assert result.returncode == 1
    assert doc in result.stderr, f"{doc} should be flagged: {reason}"


@pytest.mark.parametrize(
    ("doc", "reason"),
    [
        ("bumped.md", "body changed and the date was bumped"),
        ("date_only.md", "only the date changed"),
        ("same_day.md", "same-day re-edit escape hatch"),
        ("timezone_behind.md", "UTC-12 boundary is current"),
        ("timezone_ahead.md", "UTC+14 boundary is current"),
        ("conventions.md", "no frontmatter field, only a body mention"),
        ("clean.md", "not edited"),
    ],
)
def test_freshness_passes(freshness_repo: FixtureRepo, doc: str, reason: str) -> None:
    result = freshness_repo.lint("--check", "freshness", "--base", "main")
    assert doc not in result.stderr, f"{doc} should not be flagged: {reason}"


def test_freshness_fix_bumps_offenders_and_preserves_bodies(
    freshness_repo: FixtureRepo,
) -> None:
    result = freshness_repo.lint("--check", "freshness", "--base", "main", "--fix")
    assert result.returncode == 0

    for doc in (
        "no_bump.md",
        "bad_date.md",
        "date_only_bad.md",
        "blank_value.md",
        "stale_unchanged.md",
        "backward_bump.md",
        "impossible_date.md",
    ):
        assert f"last-updated: {TODAY}" in freshness_repo.read(doc)

    # --fix must touch only the date line.
    assert "EDITED body" in freshness_repo.read("no_bump.md")
    assert "stable body" in freshness_repo.read("date_only_bad.md")
    assert "stable body" in freshness_repo.read("blank_value.md")


def test_freshness_recheck_after_fix_is_clean(freshness_repo: FixtureRepo) -> None:
    freshness_repo.lint("--check", "freshness", "--base", "main", "--fix")
    result = freshness_repo.lint("--check", "freshness", "--base", "main")
    assert result.returncode == 0, result.stderr


def test_fix_reports_when_nothing_needs_a_bump(repo: FixtureRepo) -> None:
    repo.write_doc("clean.md", "2020-01-01", "body")
    repo.commit_all()
    result = repo.lint("--check", "freshness", "--base", "main", "--fix")
    assert result.returncode == 0
    assert "no docs needed a last-updated bump" in result.stderr


# --------------------------------------------------------------------------
# freshness — the new-file blind spot the bash version had
# --------------------------------------------------------------------------
#
# The bash predecessor skipped any doc absent at the merge-base, so a doc created
# on day 1 and edited on day 2 kept its day-1 date and every run — including --fix
# — reported clean. Observed on a real branch before this was fixed.


def _branch_with_added_doc(repo: FixtureRepo, date: str) -> None:
    repo.write_doc("seed.md", TODAY, "seed")
    repo.commit_all()
    repo.git("checkout", "-q", "-b", "feature")
    repo.write_doc("added.md", date, "brand new doc")
    repo.git("add", "added.md")


def test_new_doc_added_and_edited_same_day_passes(repo: FixtureRepo) -> None:
    _branch_with_added_doc(repo, TODAY)
    result = repo.lint("--check", "freshness", "--base", "main")
    assert result.returncode == 0, result.stderr


def test_new_doc_edited_next_day_without_bump_fails(repo: FixtureRepo) -> None:
    _branch_with_added_doc(repo, OUTSIDE_TIMEZONE_WINDOW)
    result = repo.lint("--check", "freshness", "--base", "main")
    assert result.returncode == 1
    assert "added.md" in result.stderr
    assert "added on this branch" in result.stderr


def test_new_doc_edited_next_day_with_bump_passes(repo: FixtureRepo) -> None:
    _branch_with_added_doc(repo, OUTSIDE_TIMEZONE_WINDOW)
    repo.write_doc("added.md", TODAY, "brand new doc, edited")
    result = repo.lint("--check", "freshness", "--base", "main")
    assert result.returncode == 0, result.stderr


def test_new_doc_stale_date_is_fixable(repo: FixtureRepo) -> None:
    _branch_with_added_doc(repo, OUTSIDE_TIMEZONE_WINDOW)
    result = repo.lint("--check", "freshness", "--base", "main", "--fix")
    assert result.returncode == 0
    assert f"last-updated: {TODAY}" in repo.read("added.md")


def test_preexisting_doc_with_unchanged_body_and_stale_date_passes(
    repo: FixtureRepo,
) -> None:
    repo.write_doc("stale.md", OUTSIDE_TIMEZONE_WINDOW, "body")
    repo.commit_all()
    result = repo.lint("--check", "freshness", "--base", "main")
    assert result.returncode == 0, result.stderr


def test_pure_rename_does_not_require_a_bump(repo: FixtureRepo) -> None:
    """Relocating a doc edits no prose, so it owes no date bump.

    Rename detection is on by default, so the doc arrives under its new path,
    absent at the merge-base. Treating that as "added" would fail every
    content-preserving move — exactly what a docs reorganization is made of.
    """
    repo.write_doc("docs/references/a.md", OUTSIDE_TIMEZONE_WINDOW, "body")
    repo.commit_all()
    repo.git("checkout", "-q", "-b", "feature")
    repo.git("mv", "docs/references/a.md", "docs/references/b.md")
    result = repo.lint("--check", "freshness", "--base", "main")
    assert result.returncode == 0, result.stderr


def test_rename_with_a_body_edit_still_requires_a_bump(repo: FixtureRepo) -> None:
    repo.write_doc("docs/references/a.md", OUTSIDE_TIMEZONE_WINDOW, "body")
    repo.commit_all()
    repo.git("checkout", "-q", "-b", "feature")
    repo.git("mv", "docs/references/a.md", "docs/references/b.md")
    repo.write_doc("docs/references/b.md", OUTSIDE_TIMEZONE_WINDOW, "EDITED body")
    result = repo.lint("--check", "freshness", "--base", "main")
    assert result.returncode == 1
    assert "docs/references/b.md" in result.stderr


def test_template_is_exempt_from_freshness(repo: FixtureRepo) -> None:
    """A template's placeholder date must not be rewritten into a real one."""
    repo.write_doc("docs/design-docs/TEMPLATE.md", "YYYY-MM-DD", "body")
    repo.commit_all()
    repo.write_doc("docs/design-docs/TEMPLATE.md", "YYYY-MM-DD", "EDITED body")
    result = repo.lint("--check", "freshness", "--base", "main", "--fix")
    assert result.returncode == 0
    assert "last-updated: YYYY-MM-DD" in repo.read("docs/design-docs/TEMPLATE.md")


# --------------------------------------------------------------------------
# links and anchors
# --------------------------------------------------------------------------


def test_broken_link_target_is_flagged(repo: FixtureRepo) -> None:
    repo.write_doc("docs/references/a.md", TODAY, "See [b](./nope.md).")
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "nope.md" in result.stderr


def test_resolvable_link_passes(repo: FixtureRepo) -> None:
    repo.write_doc("docs/references/a.md", TODAY, "See [b](./b.md).")
    repo.write_doc("docs/references/b.md", TODAY, "body")
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 0, result.stderr


def test_external_links_are_not_resolved(repo: FixtureRepo) -> None:
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "[x](https://example.com/nope) [y](mailto:a@example.com)",
    )
    repo.commit_all()
    assert repo.lint("--check", "links").returncode == 0


def test_links_inside_fenced_blocks_are_ignored(repo: FixtureRepo) -> None:
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "```markdown\n[example](./does-not-exist.md)\n```",
    )
    repo.commit_all()
    assert repo.lint("--check", "links").returncode == 0


def test_links_inside_code_spans_are_ignored(repo: FixtureRepo) -> None:
    repo.write_doc("docs/references/a.md", TODAY, "Write `[label](./gone.md)` to link.")
    repo.commit_all()
    assert repo.lint("--check", "links").returncode == 0


def test_link_resolves_through_a_project_root(repo: FixtureRepo) -> None:
    """The convention permits a project doc citing its own project-relative path.

    From `subproj/docs/design-docs/`, `../architecture/x.md` is the project copy.
    """
    repo.write_doc(
        "subproj/docs/design-docs/a.md", TODAY, "See [x](../architecture/x.md)."
    )
    repo.write_doc("subproj/docs/architecture/x.md", TODAY, "body")
    repo.commit_all()
    assert repo.lint("--check", "links").returncode == 0


def test_broken_same_file_anchor_is_flagged(repo: FixtureRepo) -> None:
    repo.write_doc("docs/references/a.md", TODAY, "## Real\n\nSee [x](#not-real).")
    repo.commit_all()
    result = repo.lint("--check", "anchors")
    assert result.returncode == 1
    assert "#not-real" in result.stderr


def test_broken_cross_file_anchor_is_flagged(repo: FixtureRepo) -> None:
    repo.write_doc("docs/references/a.md", TODAY, "See [x](./b.md#gone).")
    repo.write_doc("docs/references/b.md", TODAY, "## Here")
    repo.commit_all()
    result = repo.lint("--check", "anchors")
    assert result.returncode == 1
    assert "#gone" in result.stderr


def test_ampersand_heading_anchor_resolves(repo: FixtureRepo) -> None:
    """Whitespace runs do not collapse — `A & B` slugifies to `a--b`."""
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "## Migration & Rollout Plan\n\nSee [x](#migration--rollout-plan).",
    )
    repo.commit_all()
    assert repo.lint("--check", "anchors").returncode == 0


def test_underscore_heading_anchor_resolves(repo: FixtureRepo) -> None:
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "## Archival raw dims (`asset_metadata.raw_width`)\n\n"
        "See [x](#archival-raw-dims-asset_metadataraw_width).",
    )
    repo.commit_all()
    assert repo.lint("--check", "anchors").returncode == 0


def test_html_anchor_resolves(repo: FixtureRepo) -> None:
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        '<a id="dd-name-required"></a>\n\nSee [x](#dd-name-required).',
    )
    repo.commit_all()
    assert repo.lint("--check", "anchors").returncode == 0


def test_duplicate_headings_get_numbered_anchors(repo: FixtureRepo) -> None:
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "## Notes\n\n## Notes\n\nSee [first](#notes) and [second](#notes-1).",
    )
    repo.commit_all()
    assert repo.lint("--check", "anchors").returncode == 0


# --------------------------------------------------------------------------
# Documentation Maps
# --------------------------------------------------------------------------


def _map(section: str, rows: str, *, level: int = 2) -> str:
    hashes = "#" * level
    return (
        f"{hashes} Documentation Map\n\n"
        f"{hashes}# {section}\n\n"
        "| Topic | Document | Consult when... |\n"
        "|-------|----------|-----------------|\n"
        f"{rows}"
    )


def test_unresolvable_map_row_is_flagged(repo: FixtureRepo) -> None:
    repo.write(
        "AGENTS.md",
        _map("References", "| Thing | `docs/references/gone.md` | why |\n"),
    )
    repo.commit_all()
    result = repo.lint("--check", "map_paths")
    assert result.returncode == 1
    assert "gone.md" in result.stderr


def test_resolvable_map_row_passes(repo: FixtureRepo) -> None:
    repo.write(
        "AGENTS.md",
        _map("References", "| Thing | `docs/references/a.md` | why |\n"),
    )
    repo.write_doc("docs/references/a.md", TODAY, "body")
    repo.commit_all()
    assert repo.lint("--check", "map_paths").returncode == 0


def test_map_is_found_at_any_heading_level(repo: FixtureRepo) -> None:
    """A nested map may use `#` + `##` where the root map uses `##` + `###`.

    Keying section detection on a fixed level reported 81 phantom unmapped docs.
    """
    repo.write(
        "subproj/AGENTS.md",
        _map("References", "| Thing | `docs/references/gone.md` | why |\n", level=1),
    )
    repo.commit_all()
    result = repo.lint("--check", "map_paths")
    assert result.returncode == 1
    assert "gone.md" in result.stderr


def test_escaped_pipe_in_a_cell_still_validates_the_row(repo: FixtureRepo) -> None:
    r"""A `\|` is cell content, not a delimiter.

    Splitting on it yields four cells, and an unexpected cell count skips the row —
    so the row would silently opt out of path validation rather than fail.
    """
    repo.write(
        "AGENTS.md",
        _map("References", r"| Thing | `docs/references/gone.md` | A \| B |" + "\n"),
    )
    repo.commit_all()
    result = repo.lint("--check", "map_paths")
    assert result.returncode == 1
    assert "gone.md" in result.stderr


def test_map_section_without_a_table_is_tolerated(repo: FixtureRepo) -> None:
    """A repo whose real map lives elsewhere keeps the heading and a pointer."""
    repo.write(
        "AGENTS.md",
        "## Documentation Map\n\nThis repo's docs are mapped from root-agents.md.\n",
    )
    repo.commit_all()
    assert repo.lint("--check", "map_paths").returncode == 0


def test_plural_documentation_maps_heading_is_not_a_map(repo: FixtureRepo) -> None:
    """`## Documentation Maps` documents the convention; it carries no map.

    A substring match picks it up, and the three-column table below would then be
    validated as map rows — reporting a phantom unresolvable path.
    """
    repo.write(
        "docs/references/documentation-conventions.md",
        f"---\ntitle: C\nlast-updated: {TODAY}\n---\n\n"
        "## Documentation Maps\n\n"
        "| Rule | Example | Notes |\n"
        "|---|---|---|\n"
        "| Row path form | `docs/references/not-a-real-doc.md` | illustrative |\n",
    )
    repo.commit_all()
    result = repo.lint("--check", "map_paths")
    # The doc itself is unmapped (no map exists here), which is a separate rule.
    # What matters is that the illustrative path was not validated as a map row.
    assert "not-a-real-doc.md" not in result.stderr


@pytest.mark.parametrize(
    "heading",
    ["Documentation Maps", "Documentation Map Sections"],
    ids=["plural", "suffixed"],
)
def test_headings_that_only_start_with_the_phrase_are_not_maps(
    repo: FixtureRepo, heading: str
) -> None:
    """Prose *about* the convention shares the phrase but carries no map.

    A prefix match takes both, and then the non-map tables beneath them are parsed
    as rows — a two-column table's rows are the wrong shape, and a three-column
    one has its cells resolved as document paths.
    """
    repo.write(
        "docs/references/documentation-conventions.md",
        f"---\ntitle: C\nlast-updated: {TODAY}\n---\n\n"
        f"## {heading}\n\n"
        "| Section | Holds |\n"
        "|---------|-------|\n"
        "| `Architecture` | `docs/architecture/` |\n",
    )
    repo.commit_all()
    result = repo.lint("--check", "map_paths")
    # The doc is unmapped here (no map exists), which is a separate rule with its
    # own test. What matters is that the two-column prose table was not parsed as
    # map rows — which would report a wrong column count and a directory target.
    assert "columns" not in result.stderr
    assert "docs/architecture/" not in result.stderr


def test_map_row_without_a_backticked_path_is_flagged(repo: FixtureRepo) -> None:
    """A Document cell with no citation must fail, not be skipped.

    Skipping meant the row's target was never resolved, so a row that lost its
    backticks could point at a nonexistent doc and still pass.
    """
    repo.write(
        "AGENTS.md",
        _map("References", "| Broken | docs/references/missing.md | why |\n"),
    )
    repo.commit_all()
    result = repo.lint("--check", "map_paths")
    assert result.returncode == 1
    assert "no backticked document path" in result.stderr


def test_historical_section_must_identify_design_docs(repo: FixtureRepo) -> None:
    """`Deprecated APIs` is not the historical *design-doc* section.

    Matching "Historical" or "Deprecated" alone let an unrelated section satisfy
    the routing check — the opposite of routing.
    """
    repo.write(
        "AGENTS.md",
        _map("Deprecated APIs", "| T | `docs/design-docs/a.md` | why |\n"),
    )
    repo.write(
        "docs/design-docs/a.md",
        f"---\ntitle: A\nstatus: deprecated\ncreated: 2020-01-01\n"
        f"last-updated: {TODAY}\n---\n\nbody\n",
    )
    repo.commit_all()
    result = repo.lint("--check", "map_status_section")
    assert result.returncode == 1
    assert "Historical" in result.stderr


def test_unclosed_frontmatter_is_flagged(repo: FixtureRepo) -> None:
    """An unterminated block swallows the body and is not frontmatter at all.

    Returning the fields collected before EOF let a truncated doc carrying the
    required keys pass the frontmatter check.
    """
    repo.write(
        "docs/references/a.md",
        f"---\ntitle: A\nlast-updated: {TODAY}\n\nbody with no closing delimiter\n",
    )
    repo.commit_all()
    result = repo.lint("--check", "frontmatter")
    assert result.returncode == 1
    assert "never closed" in result.stderr


def test_unclosed_frontmatter_yields_no_fields() -> None:
    """The parser must not hand back a half-parsed block for others to trust."""
    assert parse_frontmatter("---\ntitle: A\nbody\n") == {}
    assert parse_frontmatter("---\ntitle: A\n---\nbody\n") == {"title": "A"}


def test_untracked_doc_is_discovered(repo: FixtureRepo) -> None:
    """A new doc must be checked before it is staged.

    `git ls-files` describes the index, so an unstaged new doc was omitted from
    every check — the documented pre-commit run green-lit a malformed doc that CI
    then rejected.
    """
    repo.write(
        "AGENTS.md", _map("References", "| T | `docs/references/x.md` | why |\n")
    )
    repo.write_doc("docs/references/x.md", TODAY, "an existing tracked doc")
    repo.commit_all()
    # Never staged. In scope because `docs/references/` is already a doc root.
    repo.write("docs/references/new.md", "---\ntitle: N\n---\n\nbody\n")
    result = repo.lint("--check", "frontmatter")
    assert result.returncode == 1
    assert "new.md" in result.stderr
    assert "last-updated" in result.stderr


def test_untracked_doc_needs_a_current_date(repo: FixtureRepo) -> None:
    """Freshness covers it too: an untracked doc has no base blob to compare."""
    repo.write(
        "AGENTS.md", _map("References", "| T | `docs/references/x.md` | why |\n")
    )
    repo.write_doc("docs/references/x.md", TODAY, "an existing tracked doc")
    repo.commit_all()
    repo.write_doc("docs/references/new.md", "2020-01-01", "body")
    result = repo.lint("--check", "freshness", "--base", "main")
    assert result.returncode == 1
    assert "new.md" in result.stderr


def test_unstaged_deletion_is_not_linted_as_empty(repo: FixtureRepo) -> None:
    """A deleted-but-unstaged doc must leave scope, not lint as an empty file."""
    repo.write("AGENTS.md", "# A\n")
    repo.write_doc("docs/references/gone.md", TODAY, "body")
    repo.commit_all()
    (repo.path / "docs/references/gone.md").unlink()
    result = repo.lint("--check", "frontmatter")
    assert result.returncode == 0, result.stderr


def test_docs_in_hidden_directories_are_ignored(repo: FixtureRepo) -> None:
    """Untracked scope must not sweep in tooling scratch space.

    An agent worktree parked under `.claude/` would otherwise be linted as part of
    this repo, and no `.gitignore` necessarily covers it.
    """
    repo.write("AGENTS.md", "# A\n")
    repo.commit_all()
    repo.write(".claude/worktrees/copy/docs/references/x.md", "no frontmatter\n")
    assert repo.lint("--check", "frontmatter").returncode == 0


def test_non_iso_created_is_flagged(repo: FixtureRepo) -> None:
    """The tables define these as ISO dates; populated is not the same as valid."""
    repo.write(
        "docs/design-docs/a.md",
        f"---\ntitle: A\nstatus: active\ncreated: yesterday\n"
        f"last-updated: {TODAY}\n---\n\nbody\n",
    )
    repo.write(
        "AGENTS.md",
        _map("Active Design Docs", "| T | `docs/design-docs/a.md` | why |\n"),
    )
    repo.commit_all()
    result = repo.lint("--check", "frontmatter")
    assert result.returncode == 1
    assert "created" in result.stderr


def test_template_placeholder_dates_are_exempt(repo: FixtureRepo) -> None:
    """`YYYY-MM-DD` is the template's placeholder, not a malformed date."""
    repo.write(
        "docs/design-docs/TEMPLATE.md",
        "---\ntitle: T\nstatus: active\ncreated: YYYY-MM-DD\n"
        "last-updated: YYYY-MM-DD\n---\n\nbody\n",
    )
    repo.commit_all()
    assert repo.lint("--check", "frontmatter").returncode == 0


def test_map_row_citing_a_directory_is_flagged(repo: FixtureRepo) -> None:
    """A directory satisfies `exists()` while routing a reader to no document."""
    repo.write("AGENTS.md", _map("References", "| T | `docs/references/` | why |\n"))
    repo.write_doc("docs/references/real.md", TODAY, "body")
    repo.commit_all()
    result = repo.lint("--check", "map_paths")
    assert result.returncode == 1
    assert "docs/references/" in result.stderr


def test_prose_link_to_a_directory_still_resolves(repo: FixtureRepo) -> None:
    """The file requirement is scoped to map rows; a README linking `docs/` is fine."""
    repo.write("README.md", "See [the docs](docs/).\n")
    repo.write_doc("docs/references/real.md", TODAY, "body")
    repo.commit_all()
    assert repo.lint("--check", "links").returncode == 0


def test_angle_bracket_link_destination_is_checked(repo: FixtureRepo) -> None:
    """CommonMark's form for destinations with spaces must not skip validation."""
    repo.write_doc("docs/references/a.md", TODAY, "See [x](<missing file.md>).")
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "missing file.md" in result.stderr


def test_map_row_with_an_empty_document_cell_is_flagged(repo: FixtureRepo) -> None:
    """An empty cell is not the table separator.

    Testing the document cell with `set(cell) <= set("-: ")` matched an empty cell
    too, so `| Broken |  | why |` was dropped as though it were the `|---|` row and
    its missing path was never reported.
    """
    repo.write("AGENTS.md", _map("References", "| Broken |  | why |\n"))
    repo.commit_all()
    result = repo.lint("--check", "map_paths")
    assert result.returncode == 1
    assert "Broken" in result.stderr


def test_real_separator_rows_are_still_skipped(repo: FixtureRepo) -> None:
    """Alignment markers must not become violations."""
    repo.write(
        "AGENTS.md",
        "## Documentation Map\n\n### References\n\n"
        "| Topic | Document | Consult when... |\n"
        "|:------|:--------:|----------------:|\n"
        "| T | `docs/references/a.md` | why |\n",
    )
    repo.write_doc("docs/references/a.md", TODAY, "body")
    repo.commit_all()
    assert repo.lint("--check", "map_paths").returncode == 0


def test_template_exemption_is_scoped_to_the_configured_path(
    repo: FixtureRepo,
) -> None:
    """A `TEMPLATE.md` elsewhere must not inherit the exemption.

    Keyed on basename, any future template anywhere in the tree was exempt from
    date, status, and map enforcement — so a real doc could keep placeholder dates
    and evade the map.
    """
    repo.write(
        "example-service/docs/design-docs/TEMPLATE.md",
        "---\ntitle: T\nstatus: active\ncreated: YYYY-MM-DD\n"
        "last-updated: YYYY-MM-DD\n---\n\nbody\n",
    )
    repo.commit_all()
    result = repo.lint("--check", "frontmatter")
    assert result.returncode == 1
    assert "example-service/docs/design-docs/TEMPLATE.md" in result.stderr


@pytest.mark.parametrize(
    "section",
    ["Not Active Design Docs", "Not Historical Design Docs", "Deprecated APIs"],
)
def test_map_sections_are_matched_exactly(repo: FixtureRepo, section: str) -> None:
    """A containment test accepted negated and unrelated headings.

    `Not Active Design Docs` contains both `Active` and `Design Doc`, so a doc
    mapped under it satisfied status routing — the opposite of routing.
    """
    repo.write("AGENTS.md", _map(section, "| T | `docs/design-docs/a.md` | why |\n"))
    repo.write(
        "docs/design-docs/a.md",
        f"---\ntitle: A\nstatus: active\ncreated: 2020-01-01\n"
        f"last-updated: {TODAY}\n---\n\nbody\n",
    )
    repo.commit_all()
    assert repo.lint("--check", "map_status_section").returncode == 1


def test_canonical_map_sections_are_accepted(repo: FixtureRepo) -> None:
    """The exact names must still pass, including the `&` in the historical one."""
    repo.write(
        "AGENTS.md",
        _map(
            "Historical & Deprecated Design Docs",
            "| T | `docs/design-docs/a.md` | why |\n",
        ),
    )
    repo.write(
        "docs/design-docs/a.md",
        f"---\ntitle: A\nstatus: deprecated\ncreated: 2020-01-01\n"
        f"last-updated: {TODAY}\n---\n\nbody\n",
    )
    repo.commit_all()
    assert repo.lint("--check", "map_status_section").returncode == 0


def test_config_key_stranded_in_a_table_is_rejected(repo: FixtureRepo) -> None:
    """TOML scopes bare keys to the table above them.

    A top-level setting appended after an `[[ignore]]` header becomes a key of that
    entry and silently does nothing — the config reads as written while having no
    effect. Found by making this mistake with `template_paths`.
    """
    repo.config_path.write_text(
        FIXTURE_CONFIG
        + '\n[[ignore]]\npath = "docs/x.md"\nchecks = ["links"]\n'
        + 'reason = "r"\ntemplate_paths = ["docs/design-docs/TEMPLATE.md"]\n',
        encoding="utf-8",
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "template_paths" in result.stderr


def test_bare_string_where_a_list_belongs_is_rejected(repo: FixtureRepo) -> None:
    """A quoted scalar is iterable, so it silently became ten one-char prefixes.

    `strip_prefixes = "repo-root "` mangled every citation the linter resolved,
    with no error anywhere. Quoting a single value instead of bracketing it reads
    perfectly natural in TOML, which is what made this reachable.
    """
    repo.config_path.write_text(
        FIXTURE_CONFIG.replace(
            'strip_prefixes = ["repo-root "]', 'strip_prefixes = "repo-root "'
        ),
        encoding="utf-8",
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "strip_prefixes" in result.stderr
    # The message has to name the fix, since the value itself is not wrong.
    assert "brackets" in result.stderr


def test_list_containing_a_non_string_is_rejected(repo: FixtureRepo) -> None:
    repo.config_path.write_text(
        FIXTURE_CONFIG.replace(
            'strip_prefixes = ["repo-root "]', "strip_prefixes = [42]"
        ),
        encoding="utf-8",
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "strip_prefixes" in result.stderr


def test_valid_list_config_still_loads(repo: FixtureRepo) -> None:
    """The other direction: the guard must not reject the correct form."""
    repo.write_doc(
        "docs/references/a.md", TODAY, "See `repo-root docs/references/a.md`."
    )
    repo.commit_all()
    assert repo.lint("--check", "links").returncode == 0


@pytest.mark.parametrize("value", ['"wide"', "true", "0", "-5", "1.5"])
def test_non_positive_int_limit_is_rejected(repo: FixtureRepo, value: str) -> None:
    """A mistyped limit raised a bare ValueError traceback out of `int()`.

    A traceback reads as the linter being broken rather than the config being
    wrong. `true` is in the list because `bool` is an `int` subclass, so it would
    otherwise silently mean a 1-character cell limit.
    """
    repo.config_path.write_text(
        FIXTURE_CONFIG.replace(
            "consult_cell_chars = 250", f"consult_cell_chars = {value}"
        ),
        encoding="utf-8",
    )
    repo.commit_all()
    result = repo.lint("--check", "map_cells")
    assert result.returncode == 1
    assert "consult_cell_chars" in result.stderr
    assert "Traceback" not in result.stderr


def test_unknown_check_name_in_config_is_rejected(repo: FixtureRepo) -> None:
    """A typo'd check name would otherwise silently configure nothing."""
    repo.config_path.write_text(
        FIXTURE_CONFIG.replace("map_cells = true", "map_cell = true"),
        encoding="utf-8",
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "map_cell" in result.stderr


@pytest.mark.parametrize(
    ("link", "should_fail"),
    [
        ("[a](missing.md 'title')", True),
        ('[a](missing.md "title")', True),
        ("[a](missing.md (title))", True),
        ("[a](<missing file.md>)", True),
        ("[a](real_(paren).md)", False),
        ("[a](real_(paren).md 'title')", False),
        ("[a](missing_(one(two)).md)", True),
        ("[a](missing_((v2)).md)", True),
        ("[a](real_(a(b)).md)", False),
    ],
    ids=[
        "single-quoted",
        "double-quoted",
        "paren-title",
        "angle",
        "balanced-parens",
        "balanced-parens-titled",
        "nested-parens",
        "doubled-parens",
        "nested-parens-resolving",
    ],
)
def test_inline_link_forms_are_parsed(
    repo: FixtureRepo, link: str, should_fail: bool
) -> None:
    """Every destination and title form CommonMark allows must be resolved.

    An unmatched form is not judged valid, it is never seen — so a broken target
    passed. Truncating a bare destination at the first `)` did the opposite and
    reported a false break on a file that exists.
    """
    repo.write_doc("docs/references/a.md", TODAY, f"See {link}.")
    repo.write_doc("docs/references/real_(paren).md", TODAY, "body")
    repo.write_doc("docs/references/real_(a(b)).md", TODAY, "body")
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == (1 if should_fail else 0), result.stderr


@pytest.mark.parametrize(
    "tag",
    ['<a data-id="target">', '<a data-name="target">', '<a aria-name="target">'],
    ids=["data-id", "data-name", "aria-name"],
)
def test_hyphenated_attributes_do_not_define_anchors(
    repo: FixtureRepo, tag: str
) -> None:
    """`\\b` matched the tail of `data-id`, so its value became a fragment anchor.

    A link to a fragment the rendered doc does not have then passed. In
    `<a data-id="x" id="t">` the first match was even `x`, so the real anchor was
    missed as well.
    """
    repo.write_doc("docs/references/a.md", TODAY, f"{tag}</a>\n\n[x](#target)")
    repo.commit_all()
    result = repo.lint("--check", "anchors")
    assert result.returncode == 1, tag
    assert "target" in result.stderr


def test_inline_code_anchor_example_is_not_a_real_anchor(repo: FixtureRepo) -> None:
    """An `<a id>` shown as an inline-code example renders no anchor.

    Counting it let a link to a nonexistent fragment pass — a false negative, the
    mirror of the code-span handling the link check already had.
    """
    repo.write_doc(
        "docs/references/a.md", TODAY, 'Write `<a id="fake"></a>` then [x](#fake).'
    )
    repo.commit_all()
    result = repo.lint("--check", "anchors")
    assert result.returncode == 1
    assert "fake" in result.stderr


def test_heading_with_inline_code_keeps_its_slug(repo: FixtureRepo) -> None:
    """Blanking spans before slugging would break every such heading.

    `slugify_heading` strips backticks itself, so the heading scan must read the text
    with its code spans intact — otherwise `## The `foo` helper` slugs as
    `the-xxxxx-helper` and every link to it breaks. This is the regression the scoped
    fix above avoids.
    """
    repo.write_doc(
        "docs/references/a.md", TODAY, "## The `foo` helper\n\n[x](#the-foo-helper)"
    )
    repo.commit_all()
    assert repo.lint("--check", "anchors").returncode == 0


def test_both_id_and_name_on_one_tag_are_collected(repo: FixtureRepo) -> None:
    """`<a name="old" id="new">` defines two fragments; taking the first missed one."""
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        '<a name="old" id="new"></a>\n\n[a](#old) and [b](#new)',
    )
    repo.commit_all()
    assert repo.lint("--check", "anchors").returncode == 0


@pytest.mark.parametrize(
    "tag",
    [
        '<a id="target">',
        '<a name="target">',
        '<a class="permalink" id="target">',
        "<a id = 'target'>",
        '<a class="c" href="#other" name="target">',
    ],
    ids=["id", "name", "id-after-class", "spaced-equals", "name-last"],
)
def test_explicit_anchor_is_found_regardless_of_attribute_order(
    repo: FixtureRepo, tag: str
) -> None:
    """Requiring the attribute first made a valid link report broken.

    A false positive blocks CI, which is worse than a miss.
    """
    repo.write_doc("docs/references/a.md", TODAY, f"{tag}Heading</a>\n\n[x](#target)")
    repo.commit_all()
    assert repo.lint("--check", "anchors").returncode == 0, tag


def test_unmapped_non_design_doc_is_flagged(repo: FixtureRepo) -> None:
    """Scoped to design docs, this missed architecture/reference/guide docs.

    Those have no other backstop at all — a design doc at least also goes through
    the status/section check.
    """
    repo.write(
        "AGENTS.md", _map("References", "| T | `docs/references/a.md` | why |\n")
    )
    repo.write_doc("docs/references/a.md", TODAY, "mapped")
    repo.write_doc("docs/architecture/orphan.md", TODAY, "not mapped")
    repo.commit_all()
    result = repo.lint("--check", "map_paths")
    assert result.returncode == 1
    assert "orphan.md" in result.stderr


def test_generated_docs_are_not_required_to_be_mapped(repo: FixtureRepo) -> None:
    """The conventions explicitly do not map `generated/`."""
    repo.write("AGENTS.md", "# A\n")
    repo.write(
        "docs/generated/schema.md",
        f"---\ntitle: S\nlast-updated: {TODAY}\ngenerated: true\n---\n\nbody\n",
    )
    repo.commit_all()
    assert repo.lint("--check", "map_paths").returncode == 0


def test_escaped_bracket_is_not_a_link(repo: FixtureRepo) -> None:
    r"""Prose showing literal markdown outside a code span renders no link.

    Matching at the `[` regardless reported the example's destination as broken —
    a false positive on correct explanatory prose.
    """
    repo.write_doc(
        "docs/references/a.md", TODAY, r"Write it as \[example](missing.md) in prose."
    )
    repo.commit_all()
    assert repo.lint("--check", "links").returncode == 0


def test_escaped_bracket_does_not_mask_a_later_real_link(
    repo: FixtureRepo,
) -> None:
    """The escape must suppress only its own match, not the rest of the line."""
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        r"Literal \[a](ignored.md) then real [c](./gone.md).",
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "gone.md" in result.stderr
    assert "ignored.md" not in result.stderr


def test_longer_fence_swallows_a_shorter_one(repo: FixtureRepo) -> None:
    """A ``` line inside a ```` block is content, not a closing fence.

    Toggling on every fence-shaped line turned blanking off mid-block and exposed the
    rest to the link parser, so an example link inside a longer fence was reported as
    a real broken link. There is a live instance of this nesting in the shared skills
    docs.
    """
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "````\nouter\n\n```suggestion\n[x](./gone.md)\n```\n````\n",
    )
    repo.commit_all()
    assert repo.lint("--check", "links").returncode == 0


def test_tilde_fence_does_not_close_a_backtick_fence(repo: FixtureRepo) -> None:
    """Different markers cannot close each other."""
    repo.write_doc(
        "docs/references/a.md", TODAY, "```\n~~~\n[x](./gone.md)\n~~~\n```\n"
    )
    repo.commit_all()
    assert repo.lint("--check", "links").returncode == 0


def test_fences_still_close_normally(repo: FixtureRepo) -> None:
    """Tracking the opener must not leave the rest of a file blanked.

    A link *after* a closed block has to be checked, or the fix would trade a false
    positive for a silent miss.
    """
    repo.write_doc(
        "docs/references/a.md", TODAY, "```\nin code\n```\n\nReal [x](./gone.md)."
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "gone.md" in result.stderr


def test_map_row_citing_a_non_doc_file_is_flagged(repo: FixtureRepo) -> None:
    """A real file is not enough; a map row must route to a document."""
    repo.write("scripts/tool.py", "x = 1\n")
    repo.write(
        "AGENTS.md",
        _map(
            "References",
            "| T | `docs/references/a.md` | why |\n| Bad | `scripts/tool.py` | why |\n",
        ),
    )
    repo.write_doc("docs/references/a.md", TODAY, "body")
    repo.commit_all()
    result = repo.lint("--check", "map_paths")
    assert result.returncode == 1
    assert "tool.py" in result.stderr


def test_superseded_by_may_name_a_non_markdown_successor(repo: FixtureRepo) -> None:
    """A deprecated doc's canonical text can be a rendered page.

    So the doc-suffix requirement is scoped to map rows; applying it here broke real
    `superseded-by` targets pointing at `.astro` pages.
    """
    repo.write(
        "AGENTS.md",
        _map(
            "Historical & Deprecated Design Docs",
            "| T | `docs/design-docs/a.md` | why |\n",
        ),
    )
    repo.write("src/pages/privacy.astro", "<html></html>\n")
    repo.write(
        "docs/design-docs/a.md",
        f"---\ntitle: A\nstatus: deprecated\ncreated: 2020-01-01\n"
        f"last-updated: {TODAY}\nsuperseded-by: ../../src/pages/privacy.astro\n"
        f"---\n\nbody\n",
    )
    repo.commit_all()
    assert repo.lint("--check", "map_paths").returncode == 0


def test_untracked_tree_cannot_introduce_a_doc_root(repo: FixtureRepo) -> None:
    """Vendored or scratch trees must not pull unrelated files into scope.

    A deleted `.gitignore` can leave a `node_modules` tree untracked-but-visible; if
    its packages' `docs/` directories became doc roots, the local lint would fail on
    thousands of unrelated files.
    """
    repo.write(
        "AGENTS.md", _map("References", "| T | `docs/references/x.md` | why |\n")
    )
    repo.write_doc("docs/references/x.md", TODAY, "body")
    repo.commit_all()
    repo.write("vendored/pkg/docs/README.md", "# Vendored\n\n[x](./gone.md)\n")
    assert repo.lint().returncode == 0


def test_doc_mapped_only_from_another_project_is_flagged(repo: FixtureRepo) -> None:
    """Each project's docs are mapped from that project's own map.

    Listed only elsewhere, the doc is undiscoverable to an agent consulting the map
    responsible for it. Extra cross-references from other maps stay fine.
    """
    repo.write(
        "AGENTS.md",
        _map("References", "| Cross | `subproj/docs/references/a.md` | why |\n"),
    )
    repo.write(
        "subproj/AGENTS.md", _map("References", "| none | `AGENTS.md` | why |\n")
    )
    repo.write_doc("subproj/docs/references/a.md", TODAY, "body")
    repo.commit_all()
    result = repo.lint("--check", "map_paths")
    assert result.returncode == 1
    assert "a map inside `subproj/`" in result.stderr


def test_repo_level_doc_mapped_only_from_a_project_is_flagged(
    repo: FixtureRepo,
) -> None:
    """Ownership is symmetric: a repo-level doc needs a repo-level map row.

    Project maps legitimately mirror repo-level rows, so being cited by one is not
    evidence the owning root map still lists it — deleting the root row left the doc
    undiscoverable from the map responsible for it.
    """
    repo.write("AGENTS.md", "# Root, with no map row for the repo-level doc\n")
    repo.write(
        "subproj/AGENTS.md",
        _map(
            "References",
            "| Mirror | `repo-root docs/references/a.md` | why |\n"
            "| Own | `docs/references/own.md` | why |\n",
        ),
    )
    # A docs/ dir is what makes `subproj` a project root.
    repo.write_doc("subproj/docs/references/own.md", TODAY, "body")
    repo.write_doc("docs/references/a.md", TODAY, "body")
    repo.commit_all()
    result = repo.lint("--check", "map_paths")
    assert result.returncode == 1
    assert "repo-level map" in result.stderr


def test_project_map_may_mirror_a_repo_level_row(repo: FixtureRepo) -> None:
    """With the root row present, a mirrored project row is legitimate."""
    repo.write(
        "AGENTS.md",
        _map(
            "References",
            "| Own | `docs/references/a.md` | why |\n"
            "| Sub | `subproj/AGENTS.md` | why |\n",
        ),
    )
    repo.write(
        "subproj/AGENTS.md",
        _map("References", "| Mirror | `repo-root docs/references/a.md` | why |\n"),
    )
    repo.write_doc("docs/references/a.md", TODAY, "body")
    repo.commit_all()
    assert repo.lint("--check", "map_paths").returncode == 0


def test_cross_reference_alongside_an_owning_row_is_fine(repo: FixtureRepo) -> None:
    """The rule is per-doc, not per-row: extra cross-project rows are legitimate."""
    repo.write(
        "AGENTS.md",
        _map("References", "| Cross | `subproj/docs/references/a.md` | why |\n"),
    )
    repo.write(
        "subproj/AGENTS.md",
        _map("References", "| Own | `docs/references/a.md` | why |\n"),
    )
    repo.write_doc("subproj/docs/references/a.md", TODAY, "body")
    repo.commit_all()
    assert repo.lint("--check", "map_paths").returncode == 0


def test_language_tagged_fence_does_not_close_a_same_length_block(
    repo: FixtureRepo,
) -> None:
    """A closing fence carries no info string.

    Accepting any suffix meant `````python`` closed a same-length ```` block, ending it
    early and exposing the rest to the link parser — so documentation demonstrating a
    language-tagged fence failed a now-required check.
    """
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "````\nouter\n````python\n[x](./gone.md)\n````\n",
    )
    repo.commit_all()
    assert repo.lint("--check", "links").returncode == 0


def test_untagged_same_length_fence_still_closes(repo: FixtureRepo) -> None:
    """The whitespace rule must not stop a real closer from closing."""
    repo.write_doc(
        "docs/references/a.md", TODAY, "````\nin code\n````   \n\nReal [x](./gone.md)."
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "gone.md" in result.stderr


def test_commented_out_link_is_ignored(repo: FixtureRepo) -> None:
    """An HTML-commented link renders nothing, so its target is unreachable anyway."""
    repo.write_doc(
        "docs/references/a.md", TODAY, "<!-- [draft](./missing.md) -->\n\nText."
    )
    repo.commit_all()
    assert repo.lint("--check", "links").returncode == 0


def test_commented_out_anchor_and_heading_define_nothing(repo: FixtureRepo) -> None:
    """Neither a commented `<a id>` nor a commented heading is rendered."""
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        '<!-- <a id="fake"></a>\n## Hidden -->\n\n[x](#fake)',
    )
    repo.commit_all()
    result = repo.lint("--check", "anchors")
    assert result.returncode == 1
    assert "fake" in result.stderr


def test_block_comment_closing_line_is_raw_html(repo: FixtureRepo) -> None:
    """A comment starting a line is an HTML *block*, running to that line's end.

    So markdown-looking text after `-->` on the same line renders as raw HTML, not a
    link. An earlier version of this test asserted the opposite and was wrong.
    """
    repo.write_doc("docs/references/a.md", TODAY, "<!-- note --> then [x](./gone.md).")
    repo.commit_all()
    assert repo.lint("--check", "links").returncode == 0


def test_inline_comment_leaves_the_rest_of_its_line_live(repo: FixtureRepo) -> None:
    """A comment *not* starting a line is inline HTML, so its line stays markdown.

    Same delimiters, different position, and the surrounding text is real markdown here.
    """
    repo.write_doc(
        "docs/references/a.md", TODAY, "Prose <!-- note --> then [x](./gone.md)."
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "gone.md" in result.stderr


def test_link_after_a_block_comment_on_a_later_line_is_checked(
    repo: FixtureRepo,
) -> None:
    """Blanking must end with the closing line, not run to the end of the file."""
    repo.write_doc(
        "docs/references/a.md", TODAY, "<!-- note -->\n\nThen [x](./gone.md)."
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "gone.md" in result.stderr


def test_unterminated_comment_is_reported(repo: FixtureRepo) -> None:
    """Per CommonMark an unclosed `<!--` block runs to the end of the document.

    So everything after it renders as nothing and drops out of every check. That is
    spec-correct but silent, so the unclosed marker itself is reported — an authoring
    slip that hides content from readers should not also hide it from the linter.
    """
    repo.write_doc(
        "docs/references/a.md", TODAY, "<!-- stray marker\n\nThen [x](./gone.md)."
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "never closed" in result.stderr


def test_unclosed_inline_comment_marker_is_literal_text(repo: FixtureRepo) -> None:
    """Only a line-start opener can begin an HTML block.

    A mid-prose `<!--` with no close is an incomplete *inline* candidate, which
    CommonMark renders literally. Carrying it across paragraphs meant a later `-->`
    blanked every live link in between, and without one the linter rejected valid
    markdown as an unterminated comment.
    """
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "A stray <!-- marker\n\nThen [x](./gone.md) and later -->.",
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "gone.md" in result.stderr
    assert "never closed" not in result.stderr


def test_comment_between_link_tokens_is_a_boundary(repo: FixtureRepo) -> None:
    """A comment separates tokens; eliding it invented link syntax.

    `[x]<!-- c -->(./y)` is not a link — `]` and `(` are not adjacent in the source — so
    reporting its target rejected valid markdown.
    """
    repo.write_doc(
        "docs/references/a.md", TODAY, "See [x]<!-- note -->(./missing.md) here."
    )
    repo.commit_all()
    assert repo.lint("--check", "links").returncode == 0


def test_comment_before_a_hash_run_is_not_a_heading(repo: FixtureRepo) -> None:
    """A line starting with raw HTML is not a heading, whatever follows.

    Eliding the comment made `<!-- c --> ## Hidden` look like one, so a broken
    `#hidden` fragment would have passed — a silent miss.
    """
    repo.write_doc(
        "docs/references/a.md", TODAY, "<!-- note --> ## Hidden\n\n[x](#hidden)"
    )
    repo.commit_all()
    result = repo.lint("--check", "anchors")
    assert result.returncode == 1
    assert "hidden" in result.stderr


def test_indented_code_is_not_special_cased(repo: FixtureRepo) -> None:
    """Indented code blocks are deliberately *not* detected.

    Detecting them needs list-container tracking — indentation is relative to the
    enclosing list marker, so a four-space line inside a list item is a paragraph, not
    code. Getting that wrong blanks live prose and silently drops its links, which is
    worse than what the detection bought: across all three repos, zero indented-code
    lines contain a comment delimiter, and zero contain links. So a delimiter shown in an
    *indented* example is read as markup, and a fenced example is the supported form.

    This test pins the trade rather than the ideal, so a future change that adds
    detection has to confront the list-indentation problem rather than rediscover it.
    """
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "An example:\n\n    <!-- shown as indented code\n\nBack to prose.",
    )
    repo.commit_all()
    # Not reported — but for a better reason than indented-code detection. Four columns
    # of indent means this is no line-start HTML-block opener, and an unclosed *inline*
    # candidate is literal text. The block/inline distinction subsumes what the removed
    # detection was for.
    assert repo.lint("--check", "links").returncode == 0


def test_fenced_example_is_the_supported_form_for_delimiters(
    repo: FixtureRepo,
) -> None:
    """The form documentation should use, and the one that works."""
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "An example:\n\n```\n<!-- shown in a fence\n```\n\nBack to prose.",
    )
    repo.commit_all()
    assert repo.lint("--check", "links").returncode == 0


def test_list_item_paragraph_keeps_its_links(repo: FixtureRepo) -> None:
    """Four-space indent inside a list item is list content, not code.

    This is what indented-code detection got wrong, and why it was removed rather than
    extended: the link here is live and its broken target must be reported.
    """
    repo.write_doc(
        "docs/references/a.md", TODAY, "- item\n\n    see [x](./gone.md) here"
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "gone.md" in result.stderr


def test_multiline_comment_keeps_heading_lines_aligned(repo: FixtureRepo) -> None:
    """The two renderings are indexed by line number, so both must keep line count.

    Eliding a multi-line comment's newlines desynced them, so a heading took its text
    from a later line — wrong slugs plus invented duplicate suffixes. Here `#a` resolved
    to nothing and a phantom `c-1` appeared.
    """
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "<!--\nnote\n-->\n\n## A\n\n## B\n\n## C\n\n[x](#a) [y](#b) [z](#c)",
    )
    repo.commit_all()
    assert repo.lint("--check", "anchors").returncode == 0


@pytest.mark.parametrize("form", ["<!-->", "<!--->"], ids=["3-dash", "4-dash"])
def test_short_empty_comment_forms_are_complete(repo: FixtureRepo, form: str) -> None:
    """`<!-->` and `<!--->` are complete comments whose terminator overlaps the opener.

    Searching for `-->` past the fourth character found neither, so both read as
    unterminated: a spurious violation, and the links after them skipped.
    """
    repo.write_doc("docs/references/a.md", TODAY, f"Prose {form} then [x](./gone.md).")
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "gone.md" in result.stderr
    assert "never closed" not in result.stderr


def test_heading_is_its_own_block_for_inline_scanning(repo: FixtureRepo) -> None:
    """Inline constructs cannot span a heading boundary.

    Grouped with the following line, an unmatched backtick in each paired up and blanked
    the live link between them — a silent miss on valid markdown.
    """
    repo.write_doc(
        "docs/references/a.md", TODAY, "## Heading `\n\nsee [x](./gone.md) `"
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "gone.md" in result.stderr


def test_heading_with_a_real_code_span_still_slugs(repo: FixtureRepo) -> None:
    """Flushing at headings must not disturb a span *within* one."""
    repo.write_doc(
        "docs/references/a.md", TODAY, "## The `foo` helper\n\n[x](#the-foo-helper)"
    )
    repo.commit_all()
    assert repo.lint("--check", "anchors").returncode == 0


def test_unmatched_backtick_leaves_following_links_live(repo: FixtureRepo) -> None:
    """An unclosed run is literal text, so the links after it still render.

    Carrying a tentative span line by line blanked the rest of the paragraph
    permanently, so one stray backtick silently dropped every link after it.
    """
    repo.write_doc(
        "docs/references/a.md", TODAY, "Before ` typo then [x](./gone.md) here."
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "gone.md" in result.stderr


def test_unmatched_backtick_across_lines_leaves_links_live(
    repo: FixtureRepo,
) -> None:
    """Same, where the stray run and the link are on different lines."""
    repo.write_doc(
        "docs/references/a.md", TODAY, "Before ` typo\nthen [x](./gone.md) here."
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "gone.md" in result.stderr


def test_three_space_indent_is_still_prose(repo: FixtureRepo) -> None:
    """Under four columns is not code, so its links stay in scope."""
    repo.write_doc(
        "docs/references/a.md", TODAY, "Prose:\n\n   see [x](./gone.md) here"
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "gone.md" in result.stderr


def test_list_continuation_is_not_treated_as_indented_code(
    repo: FixtureRepo,
) -> None:
    """Indented code cannot interrupt a paragraph, so a continuation keeps its links.

    Without the after-a-blank-line bound this would be blanked and its broken link
    missed — trading a false positive for a silent miss.
    """
    repo.write_doc("docs/references/a.md", TODAY, "- item\n    see [x](./gone.md) here")
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "gone.md" in result.stderr


def test_escaped_comment_opener_is_not_a_comment(repo: FixtureRepo) -> None:
    r"""`\<!--` renders the delimiter as text, so the links after it stay live.

    Treating it as an opener swallowed them — a silent miss.
    """
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        r"Write \<!-- then [x](./gone.md) and --> to comment out.",
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "gone.md" in result.stderr


def test_escaped_backtick_does_not_open_a_span(repo: FixtureRepo) -> None:
    r"""Same rule for ``\```: an escaped delimiter is literal text."""
    repo.write_doc("docs/references/a.md", TODAY, r"\`not a span [x](./gone.md)")
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "gone.md" in result.stderr


def test_code_span_may_cross_a_line_break(repo: FixtureRepo) -> None:
    """CommonMark spans may contain line endings, so a link inside one is not real.

    This is the shape 66 docs in the real corpus rely on — a `{ a,\\nb }` literal
    wrapped across lines in prose.
    """
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "Write `example\ntext [x](./gone.md)` and it is code.",
    )
    repo.commit_all()
    assert repo.lint("--check", "links").returncode == 0


def test_mid_line_comment_marker_does_not_break_a_multiline_span(
    repo: FixtureRepo,
) -> None:
    """Only a *line-initial* `<!--` can begin an HTML block.

    Mid-prose it cannot interrupt the paragraph, so the span still closes and the
    marker is literal text inside it.
    """
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "Write `example\nx <!-- draft\ntext` and it is code.",
    )
    repo.commit_all()
    assert repo.lint("--check", "links").returncode == 0


def test_line_initial_comment_interrupts_a_paragraph(repo: FixtureRepo) -> None:
    """An HTML block interrupts a paragraph, so it can cut a code span in half.

    `<!--` at the start of a line is an HTML block start condition, and blocks of
    that type may interrupt a paragraph — so the span opened on the line before
    never closes, and the comment runs to the end of the document. Reporting the
    unterminated marker is correct: everything after it renders as nothing.
    """
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "Write `example\n<!-- draft\ntext` and it is code.",
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "never closed" in result.stderr


def test_link_after_a_multiline_span_is_still_checked(repo: FixtureRepo) -> None:
    """Carrying span state must not blank past the span's close."""
    repo.write_doc("docs/references/a.md", TODAY, "`a\nb` then [x](./gone.md).")
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "gone.md" in result.stderr


def test_link_line_number_is_the_link_s_own_line(repo: FixtureRepo) -> None:
    """Inline tokens carry no position, so the line must be found within the block.

    Reporting the enclosing paragraph's first line instead measured as 24% of the
    corpus's links wrong, one of them 33 lines adrift — far enough that the
    violation points at unrelated prose.
    """
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "prose line one\nprose line two\nprose line three [x](./gone.md) here",
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    # Body starts at line 6: three frontmatter lines, its closing `---`, then a
    # blank. So the link on the body's third line is line 8, not the block's 6.
    assert "a.md:8:" in result.stderr, result.stderr


def test_image_before_a_link_does_not_steal_its_line(repo: FixtureRepo) -> None:
    """An image spends one `](`, so it must consume one from the block's supply.

    Otherwise its position is handed to the next link and the violation is
    reported on the image's line. markdown-it also percent-encodes an
    angle-bracket destination, so matching the href text cannot rescue it.
    """
    repo.write_doc(
        "docs/references/a.md", TODAY, "![img](ok.png)\n[x](<missing file.md>)"
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "a.md:7:" in result.stderr, result.stderr


def test_link_syntax_inside_image_alt_text_is_not_a_link(repo: FixtureRepo) -> None:
    """Alt text renders as `<img alt="...">`, with no hyperlink to resolve.

    markdown-it still tokenizes link syntax inside an image's children, so
    recursing into them reported the destination as broken — a false positive on
    something that is not a link.
    """
    repo.write_doc(
        "docs/references/a.md", TODAY, "![alt [text][ref]](image.png)\n\n[ref]: gone.md"
    )
    repo.commit_all()
    assert repo.lint("--check", "links").returncode == 0, "link inside image alt text"


def test_linked_image_reports_the_links_own_line(repo: FixtureRepo) -> None:
    """Offsets are spent in *source* order, which is not token order.

    A link spends its `](` at the **close** — everything nested in the label comes
    first in the source — so `[![alt](img.png)\\n](dest.md)` is emitted
    link_open, image, link_close while the source runs image-first. Consuming at
    link_open handed the link its image's position and reported it on line 1.
    """
    repo.write_doc("docs/references/a.md", TODAY, "[![alt](image.png)\n](missing.md)")
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "a.md:7:" in result.stderr, result.stderr


def test_prose_mention_of_a_target_does_not_steal_its_line(repo: FixtureRepo) -> None:
    """Anchoring on link *syntax*, not the destination text.

    An unrestricted search for the destination finds a prose mention of the same
    path earlier in the block and reports the violation there.
    """
    repo.write_doc(
        "docs/references/a.md", TODAY, "gone.md is stale\nand [doc](gone.md) too"
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "a.md:7:" in result.stderr, result.stderr


def test_autolink_does_not_consume_a_later_links_position(repo: FixtureRepo) -> None:
    """An autolink has no `](`, so consuming one would steal a real link's line."""
    repo.write_doc(
        "docs/references/a.md", TODAY, "<https://example.com> then\n[x](gone.md)"
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "a.md:7:" in result.stderr, result.stderr


def test_encoded_hash_in_a_filename_is_not_a_fragment(repo: FixtureRepo) -> None:
    """A real `#` in a filename must be encoded, so split before decoding.

    Decoding first reintroduces the delimiter: `foo%23bar.md` becomes
    `foo#bar.md`, the partition reads `#bar.md` as a fragment, and the linter
    rejects a valid link by trying to resolve `foo`.
    """
    repo.write_doc("docs/references/foo#bar.md", TODAY, "target")
    repo.write_doc("docs/references/a.md", TODAY, "See [x](foo%23bar.md).")
    repo.commit_all()
    assert repo.lint("--check", "links").returncode == 0, "encoded # in a filename"


def test_reference_link_does_not_mislocate_a_later_link(repo: FixtureRepo) -> None:
    """A reference link spends no `](`, so the pairing must not be trusted.

    Trusting it would slip by one and report the *inline* link on the reference
    link's line. The supply/demand check falls the whole block back to its own
    line instead — an imprecise line, never a wrong one.
    """
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "text [a][r] more\nand [b](gone.md)\n\n[r]: ./also-gone.md",
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    # Both fall back to the block's first line rather than being mispaired.
    assert "a.md:8:" not in result.stderr, result.stderr


def test_repeated_target_in_one_block_gets_distinct_lines(repo: FixtureRepo) -> None:
    """The cursor must advance, or every repeat matches the first occurrence."""
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "one [x](./gone.md)\ntwo\nthree [y](./gone.md)",
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "a.md:6:" in result.stderr, result.stderr
    assert "a.md:8:" in result.stderr, result.stderr


def test_blank_line_ends_an_unclosed_span(repo: FixtureRepo) -> None:
    """A span cannot cross a paragraph break, so the backtick was literal.

    Without this bound, one stray backtick would blank the rest of the file.
    """
    repo.write_doc("docs/references/a.md", TODAY, "`unclosed\n\nThen [x](./gone.md).")
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "gone.md" in result.stderr


def test_longer_backtick_run_cannot_close_a_shorter_span(
    repo: FixtureRepo,
) -> None:
    """A closing run must be exactly the opener's length, checked on both sides.

    Testing only the character *after* a candidate accepted a suffix of a longer run, so
    the span closed early and its code content was scanned as live markdown — failing a
    required check on valid input.
    """
    repo.write_doc("docs/references/a.md", TODAY, "`foo`` [x](./gone.md)`")
    repo.commit_all()
    assert repo.lint("--check", "links").returncode == 0


def test_commented_out_map_row_is_not_validated(repo: FixtureRepo) -> None:
    """A row inside an HTML comment renders nothing, so it routes no one.

    Map parsing previously blanked only fenced blocks.
    """
    repo.write(
        "AGENTS.md",
        _map("References", "| T | `docs/references/a.md` | why |\n")
        + "<!-- | Old | `docs/references/gone.md` | why | -->\n",
    )
    repo.write_doc("docs/references/a.md", TODAY, "body")
    repo.commit_all()
    assert repo.lint("--check", "map_paths").returncode == 0


def test_comment_delimiter_shown_as_code_is_not_a_comment(repo: FixtureRepo) -> None:
    """A `<!--` displayed as code must not pair with a later real `-->`.

    Blanking comments as a separate earlier pass let it do exactly that, erasing the
    live links in between — a silent miss, and the worst outcome available.
    """
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "Write `<!--` then [x](./gone.md) and `-->` to comment out.",
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "gone.md" in result.stderr


def test_comment_delimiter_in_a_fence_does_not_reach_outside(
    repo: FixtureRepo,
) -> None:
    """Same defect across a fenced example rather than an inline span."""
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "```\n<!--\n```\n\nThen [x](./gone.md).\n\n-->\n",
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "gone.md" in result.stderr


def test_frontmatter_is_not_a_setext_heading(repo: FixtureRepo) -> None:
    """Frontmatter is YAML, and its closing `---` must not underline it.

    Read as Markdown, a `---` after text makes the block above it a setext H2 — which
    invented an anchor like `title-a-last-updated-...` on 215 of the 293 docs in the
    real corpus. A link to that phantom fragment must not resolve.
    """
    repo.write_doc(
        "docs/references/a.md", TODAY, "[x](#title-a-last-updated-2026-01-01)"
    )
    repo.commit_all()
    result = repo.lint("--check", "anchors")
    assert result.returncode == 1
    assert "no matching heading" in result.stderr


def test_hash_comment_in_frontmatter_is_not_a_heading(repo: FixtureRepo) -> None:
    """A `#` line inside the YAML block is a comment, not an H1.

    Parsing one as a heading is how six phantom anchors per daemon file appeared.
    """
    repo.write(
        "docs/references/a.md",
        f"---\ntitle: A\n# Daily, not every 6h\nlast-updated: {TODAY}\n---\n\n"
        "[x](#daily-not-every-6h)\n",
    )
    repo.commit_all()
    result = repo.lint("--check", "anchors")
    assert result.returncode == 1
    assert "no matching heading" in result.stderr


def test_setext_heading_defines_an_anchor(repo: FixtureRepo) -> None:
    """GitHub renders setext headings and gives them anchors.

    The previous ATX-only scan missed them, so a valid link to one was reported
    broken.
    """
    repo.write_doc(
        "docs/references/a.md", TODAY, "Real Heading\n===\n\n[x](#real-heading)"
    )
    repo.commit_all()
    assert repo.lint("--check", "anchors").returncode == 0


def test_literal_link_syntax_in_a_heading_keeps_both_words() -> None:
    """`slugify_heading` receives *rendered text*, so it must not re-strip markup.

    Verified against GitHub's renderer: `# \\[foo\\]\\(bar\\)` renders as the text
    `[foo](bar)`, which slugs to `foobar`. A link-stripping regex reduced it to
    `foo`, so a valid `#foobar` link was reported broken.
    """
    assert slugify_heading("[foo](bar)") == "foobar"
    # A real link still contributes only its label, because heading_text already
    # dropped the destination before slugging.
    assert collect_anchors("# See [the docs](x.md) now") == {"see-the-docs-now"}


def test_image_in_a_heading_contributes_no_slug_text() -> None:
    """Alt text is an attribute, not text, so GitHub's anchor ignores it.

    Verified against GitHub's own renderer: `# ![Setup](icon.png)` becomes
    `<h1><a ...><img alt="Setup"></a></h1>`, whose *text content* — what the anchor
    is slugged from — is empty. Including alt text would invent a `#setup` anchor
    GitHub does not have, letting a broken link pass and shifting the `-1` dedup
    suffix of any later real `Setup` heading.

    The mixed case keeps the two spaces that flanked the image, so the slug carries
    a double hyphen — whitespace runs do not collapse.
    """
    assert collect_anchors("# ![Setup](icon.png)") == set()
    assert collect_anchors("# Mixed ![icon](i.png) Heading") == {"mixed--heading"}


def test_heading_that_renders_no_text_defines_no_anchor(repo: FixtureRepo) -> None:
    """`# <Title>` is raw inline HTML, so GitHub gives it no usable anchor.

    Adding the empty slug would both invent a fragment and consume a dedup slot,
    shifting the `-1` suffix of every later duplicate.
    """
    repo.write_doc("docs/references/a.md", TODAY, "# <Title>\n\n[x](#title)")
    repo.commit_all()
    result = repo.lint("--check", "anchors")
    assert result.returncode == 1
    assert "no matching heading" in result.stderr


def test_fence_inside_a_real_comment_does_not_open_a_fence(
    repo: FixtureRepo,
) -> None:
    """The other ordering has the mirror bug, which is why this is one pass.

    Blanking fences first would let a ``` inside a genuine comment open a block and
    swallow the real prose after it.
    """
    repo.write_doc(
        "docs/references/a.md", TODAY, "<!--\n```\n-->\n\nThen [x](./gone.md)."
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "gone.md" in result.stderr


@pytest.mark.parametrize(
    ("heading", "fragment"),
    [
        ("## Setup <!-- old -->", "#setup"),
        ("## Foo <!-- n --> Bar", "#foo--bar"),
    ],
    ids=["trailing", "mid-heading"],
)
def test_heading_sharing_a_line_with_a_comment_slugs_as_rendered(
    repo: FixtureRepo, heading: str, fragment: str
) -> None:
    """The comment is removed, not filled.

    Offset-preserving filler left the comment's width in the heading text, so the slug
    carried filler characters (or, with spaces, its width in hyphens — GitHub does not
    collapse whitespace runs). Either way a link to the rendered id was rejected.
    """
    repo.write_doc("docs/references/a.md", TODAY, f"{heading}\n\n[x]({fragment})")
    repo.commit_all()
    assert repo.lint("--check", "anchors").returncode == 0, heading


@pytest.mark.parametrize("indent", ["", " ", "  ", "   "], ids=["0", "1", "2", "3"])
def test_indented_heading_still_defines_its_anchor(
    repo: FixtureRepo, indent: str
) -> None:
    """CommonMark permits up to three spaces before an ATX heading."""
    repo.write_doc("docs/references/a.md", TODAY, f"{indent}## Setup\n\n[x](#setup)")
    repo.commit_all()
    assert repo.lint("--check", "anchors").returncode == 0, f"indent={len(indent)}"


def test_four_space_indent_is_a_code_block_not_a_heading(repo: FixtureRepo) -> None:
    """Four spaces is an indented code block, so it defines no anchor."""
    repo.write_doc("docs/references/a.md", TODAY, "    ## Setup\n\n[x](#setup)")
    repo.commit_all()
    result = repo.lint("--check", "anchors")
    assert result.returncode == 1
    assert "setup" in result.stderr


def test_slug_suffix_avoids_a_literal_heading_of_that_name(
    repo: FixtureRepo,
) -> None:
    """A generated suffix must not collide with a heading that already slugs to it.

    Headings `Foo`, `Foo`, `Foo-1` render as `foo`, `foo-1`, `foo-1-1`. Counting per
    base emitted `foo-1` twice — impossible for HTML ids — and never `foo-1-1`, so a
    valid link to the third heading was reported broken.
    """
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "# Foo\n\n# Foo\n\n# Foo-1\n\n[a](#foo) [b](#foo-1) [c](#foo-1-1)",
    )
    repo.commit_all()
    assert repo.lint("--check", "anchors").returncode == 0


def test_multi_backtick_code_span_is_blanked(repo: FixtureRepo) -> None:
    """A code span is closed by a run of the same length as its opener.

    Matching only single backticks left a double-backtick span unblanked, so a link
    written inside one as an example was parsed as a real link and reported broken —
    a false positive on correct content.
    """
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "Write a link as ``[example](./gone.md)`` in prose.",
    )
    repo.commit_all()
    assert repo.lint("--check", "links").returncode == 0, "double-backtick span"


def test_backticked_label_still_parses_its_target(repo: FixtureRepo) -> None:
    """The offset-preserving blank must not hide a real target.

    `[`foo.md`](gone.md)` has a code span in its *label*; the target is real and must
    still be resolved, so widening the span pattern cannot regress this.
    """
    repo.write_doc("docs/references/a.md", TODAY, "See [`gone.md`](./gone.md).")
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "gone.md" in result.stderr


def test_map_row_with_wrong_column_count_is_flagged(repo: FixtureRepo) -> None:
    """A malformed row must fail, not be skipped.

    Discarding it silently means its cited path is never resolved, so one stray
    unescaped `|` turns a row citing a nonexistent doc into a passing one. Outside
    `design-docs/` nothing else would catch it — there is no unmapped-doc backstop
    for an `architecture/` or `references/` doc.
    """
    repo.write(
        "AGENTS.md",
        _map("References", "| Broken | `docs/references/missing.md` | A | B |\n"),
    )
    repo.commit_all()
    result = repo.lint("--check", "map_paths")
    assert result.returncode == 1
    assert "4 columns" in result.stderr


def test_map_row_with_too_few_columns_is_flagged(repo: FixtureRepo) -> None:
    """The parser pads a short row, so the source line is the only authority.

    Against a three-column header a two-cell row arrives as `['a', 'b', '']` —
    indistinguishable from a deliberately empty third cell.
    """
    repo.write("AGENTS.md", _map("References", "| Broken | `docs/references/a.md` |\n"))
    repo.commit_all()
    result = repo.lint("--check", "map_paths")
    assert result.returncode == 1
    assert "2 columns" in result.stderr


def test_map_row_with_an_escaped_pipe_is_well_formed(repo: FixtureRepo) -> None:
    """The other direction: `\\|` is a literal pipe, not a fourth column."""
    repo.write_doc("docs/references/a.md", TODAY, "body")
    repo.write(
        "AGENTS.md",
        _map(
            "References", r"| Topic A | `docs/references/a.md` | why \| really |" + "\n"
        ),
    )
    repo.commit_all()
    assert repo.lint("--check", "map_paths").returncode == 0


def test_dev_root_relative_cross_repo_link_is_skipped(repo: FixtureRepo) -> None:
    """A path climbing out of the repo names another repo, so it is unverifiable.

    The team conventions prescribe exactly this form for a cross-repo
    `superseded-by:`. CI clones only one repo, so demanding resolution would fail
    every such reference no matter how it were written.
    """
    repo.write_doc(
        "docs/design-docs/a.md",
        TODAY,
        "Live answer: [x](../../../sibling/docs/architecture/successor.md).",
    )
    repo.commit_all()
    assert repo.lint("--check", "links").returncode == 0


def test_org_qualified_cross_repo_link_is_skipped(repo: FixtureRepo) -> None:
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "See [x](gumnut-ai/sibling/docs/architecture/successor.md).",
    )
    repo.commit_all()
    assert repo.lint("--check", "links").returncode == 0


def test_cross_repo_verdict_does_not_depend_on_a_sibling_clone(
    repo: FixtureRepo,
) -> None:
    """The escape test is path arithmetic, not a filesystem probe.

    Otherwise the same doc would pass on a dev box that happens to have the
    sibling repo cloned and fail in CI, which clones only this one.
    """
    sibling = repo.path.parent / "sibling" / "docs" / "architecture"
    sibling.mkdir(parents=True, exist_ok=True)
    (sibling / "successor.md").write_text("outside\n", encoding="utf-8")
    repo.write_doc(
        "docs/design-docs/a.md",
        TODAY,
        "See [x](../../../sibling/docs/architecture/successor.md).",
    )
    repo.commit_all()
    # Same verdict as the test above, where the sibling does not exist.
    assert repo.lint("--check", "links").returncode == 0


def test_cross_repo_superseded_by_is_skipped(repo: FixtureRepo) -> None:
    repo.write(
        "AGENTS.md",
        _map(
            "Historical & Deprecated Design Docs",
            "| Thing | `docs/design-docs/a.md` | why |\n",
        ),
    )
    repo.write(
        "docs/design-docs/a.md",
        f"---\ntitle: A\nstatus: deprecated\ncreated: 2020-01-01\n"
        f"last-updated: {TODAY}\n"
        f"superseded-by: ../../../sibling/docs/architecture/successor.md\n"
        f"---\n\nbody\n",
    )
    repo.commit_all()
    assert repo.lint("--check", "map_paths").returncode == 0


def test_in_repo_broken_link_still_fails_alongside_cross_repo_ones(
    repo: FixtureRepo,
) -> None:
    """The cross-repo skip must not become a blanket amnesty."""
    repo.write_doc(
        "docs/references/a.md",
        TODAY,
        "Fine: [x](gumnut-ai/sibling/docs/a.md). Broken: [y](./gone.md).",
    )
    repo.commit_all()
    result = repo.lint("--check", "links")
    assert result.returncode == 1
    assert "gone.md" in result.stderr
    assert "gumnut-ai" not in result.stderr


def test_repo_root_prefix_is_stripped(repo: FixtureRepo) -> None:
    repo.write(
        "subproj/AGENTS.md",
        _map(
            "References",
            "| Thing | `repo-root docs/references/a.md` | why |\n",
        ),
    )
    repo.write_doc("docs/references/a.md", TODAY, "body")
    repo.commit_all()
    assert repo.lint("--check", "map_paths").returncode == 0


def test_repo_root_prefix_wins_over_a_nearer_same_named_file(
    repo: FixtureRepo,
) -> None:
    """The collision the marker exists for: same filename at two levels.

    `repo-root ` means the root copy. Stripping the marker and then resolving
    citing-directory-first sends it to the project copy instead — validating the
    wrong file, and still passing after the intended root target is deleted.
    """
    # The root copy is repo-level, so it needs a row in a repo-level map as well.
    repo.write(
        "AGENTS.md", _map("References", "| Root | `docs/references/a.md` | why |\n")
    )
    # Both forms in one map, which is the disambiguation the marker exists for:
    # the prefixed citation must reach the root copy, the bare one the project copy.
    repo.write(
        "subproj/AGENTS.md",
        _map(
            "References",
            "| Root | `repo-root docs/references/a.md` | why |\n"
            "| Project | `docs/references/a.md` | why |\n",
        ),
    )
    # Both exist, so a wrong base still resolves — the bug is silent.
    repo.write_doc("subproj/docs/references/a.md", TODAY, "project copy")
    repo.write_doc("docs/references/a.md", TODAY, "root copy")
    repo.commit_all()
    assert repo.lint("--check", "map_paths").returncode == 0

    # Delete only the root copy. The row must now fail: if it resolved to the
    # project copy it would keep passing, which is exactly the defect.
    (repo.path / "docs/references/a.md").unlink()
    repo.commit_all()
    result = repo.lint("--check", "map_paths")
    assert result.returncode == 1
    assert "docs/references/a.md" in result.stderr


def test_active_design_doc_under_historical_section_is_flagged(
    repo: FixtureRepo,
) -> None:
    repo.write(
        "AGENTS.md",
        _map(
            "Historical & Deprecated Design Docs",
            "| Thing | `docs/design-docs/a.md` | why |\n",
        ),
    )
    repo.write(
        "docs/design-docs/a.md",
        f"---\ntitle: A\nstatus: active\ncreated: 2020-01-01\n"
        f"last-updated: {TODAY}\n---\n\nbody\n",
    )
    repo.commit_all()
    result = repo.lint("--check", "map_status_section")
    assert result.returncode == 1
    assert "Active Design Docs" in result.stderr


def test_deprecated_design_doc_under_active_section_is_flagged(
    repo: FixtureRepo,
) -> None:
    repo.write(
        "AGENTS.md",
        _map("Active Design Docs", "| Thing | `docs/design-docs/a.md` | why |\n"),
    )
    repo.write(
        "docs/design-docs/a.md",
        f"---\ntitle: A\nstatus: deprecated\ncreated: 2020-01-01\n"
        f"last-updated: {TODAY}\nsuperseded-by: ../references/b.md\n---\n\nbody\n",
    )
    repo.write_doc("docs/references/b.md", TODAY, "body")
    repo.commit_all()
    result = repo.lint("--check", "map_status_section")
    assert result.returncode == 1
    assert "Historical" in result.stderr


def test_correctly_sectioned_design_docs_pass(repo: FixtureRepo) -> None:
    repo.write(
        "AGENTS.md",
        "## Documentation Map\n\n"
        "### Active Design Docs\n\n"
        "| Topic | Document | Consult when... |\n"
        "|---|---|---|\n"
        "| A | `docs/design-docs/a.md` | why |\n\n"
        "### Historical & Deprecated Design Docs\n\n"
        "| Topic | Document | Consult when... |\n"
        "|---|---|---|\n"
        "| B | `docs/design-docs/b.md` | why |\n",
    )
    repo.write(
        "docs/design-docs/a.md",
        f"---\ntitle: A\nstatus: proposed\ncreated: 2020-01-01\n"
        f"last-updated: {TODAY}\n---\n\nbody\n",
    )
    repo.write(
        "docs/design-docs/b.md",
        f"---\ntitle: B\nstatus: completed\ncreated: 2020-01-01\n"
        f"last-updated: {TODAY}\n---\n\nbody\n",
    )
    repo.commit_all()
    result = repo.lint("--check", "map_status_section")
    assert result.returncode == 0, result.stderr


def test_template_is_exempt_from_status_section(repo: FixtureRepo) -> None:
    repo.write(
        "AGENTS.md",
        _map(
            "Historical & Deprecated Design Docs",
            "| T | `docs/design-docs/TEMPLATE.md` | why |\n",
        ),
    )
    repo.write(
        "docs/design-docs/TEMPLATE.md",
        "---\ntitle: T\nstatus: active\ncreated: YYYY-MM-DD\n"
        "last-updated: YYYY-MM-DD\n---\n\nbody\n",
    )
    repo.commit_all()
    assert repo.lint("--check", "map_status_section").returncode == 0


def test_unmapped_design_doc_fails(repo: FixtureRepo) -> None:
    """The conventions require a row for every design doc, whatever its status.

    Reported as a warning this would exit 0, letting an unreachable doc merge —
    the map is the only route agents have to it.
    """
    repo.write(
        "docs/design-docs/orphan.md",
        f"---\ntitle: O\nstatus: active\ncreated: 2020-01-01\n"
        f"last-updated: {TODAY}\n---\n\nbody\n",
    )
    repo.commit_all()
    result = repo.lint("--check", "map_paths")
    assert result.returncode == 1
    assert "orphan.md" in result.stderr


def test_unmapped_template_is_exempt(repo: FixtureRepo) -> None:
    """The template is the sole documented exception; its status is placeholder."""
    repo.write(
        "docs/design-docs/TEMPLATE.md",
        "---\ntitle: T\nstatus: active\ncreated: 2020-01-01\n"
        "last-updated: YYYY-MM-DD\n---\n\nbody\n",
    )
    repo.commit_all()
    assert repo.lint("--check", "map_paths").returncode == 0


def test_overlong_consult_cell_is_flagged(repo: FixtureRepo) -> None:
    repo.write(
        "AGENTS.md",
        _map("References", f"| T | `docs/references/a.md` | {'x' * 251} |\n"),
    )
    repo.write_doc("docs/references/a.md", TODAY, "body")
    repo.commit_all()
    result = repo.lint("--check", "map_cells")
    assert result.returncode == 1
    assert "251-char" in result.stderr


def test_consult_cell_at_the_limit_passes(repo: FixtureRepo) -> None:
    repo.write(
        "AGENTS.md",
        _map("References", f"| T | `docs/references/a.md` | {'x' * 250} |\n"),
    )
    repo.write_doc("docs/references/a.md", TODAY, "body")
    repo.commit_all()
    assert repo.lint("--check", "map_cells").returncode == 0


def test_multiline_consult_cell_is_flagged(repo: FixtureRepo) -> None:
    repo.write(
        "AGENTS.md",
        _map("References", "| T | `docs/references/a.md` | one<br>two |\n"),
    )
    repo.write_doc("docs/references/a.md", TODAY, "body")
    repo.commit_all()
    result = repo.lint("--check", "map_cells")
    assert result.returncode == 1
    assert "multi-line" in result.stderr


# --------------------------------------------------------------------------
# frontmatter
# --------------------------------------------------------------------------


def test_reference_doc_missing_title_is_flagged(repo: FixtureRepo) -> None:
    repo.write("docs/references/a.md", f"---\nlast-updated: {TODAY}\n---\n\nbody\n")
    repo.commit_all()
    result = repo.lint("--check", "frontmatter")
    assert result.returncode == 1
    assert "title" in result.stderr


def test_list_valued_field_is_not_treated_as_empty(repo: FixtureRepo) -> None:
    """A YAML block sequence is a populated value, not a blank one.

    The scalar-only parse read `title:` followed by `- items` as the empty string,
    so a required field written as a list would be reported "present but empty" —
    a real doc failing on a shape YAML allows.
    """
    repo.write(
        "docs/references/a.md",
        f"---\ntitle:\n  - First\n  - Second\nlast-updated: {TODAY}\n---\n\nbody\n",
    )
    repo.commit_all()
    result = repo.lint("--check", "frontmatter")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "block",
    [
        # The single-item case is the one that matters: joining items bare made it
        # indistinguishable from a scalar, so malformed frontmatter linted clean.
        "last-updated:\n  - 2026-01-01\n",
        "last-updated:\n  - 2026-01-01\n  - 2026-01-02\n",
    ],
    ids=["single-item", "multi-item"],
)
def test_list_valued_date_is_still_rejected(repo: FixtureRepo, block: str) -> None:
    """Populated is not the same as valid — a sequence is no ISO date.

    Every required field is scalar by convention, so the *shape* has to survive
    parsing rather than be flattened away.
    """
    repo.write("docs/references/a.md", f"---\ntitle: A\n{block}---\n\nbody\n")
    repo.commit_all()
    result = repo.lint("--check", "frontmatter")
    assert result.returncode == 1
    assert "last-updated" in result.stderr


def test_design_doc_missing_created_is_flagged(repo: FixtureRepo) -> None:
    repo.write(
        "docs/design-docs/a.md",
        f"---\ntitle: A\nstatus: active\nlast-updated: {TODAY}\n---\n\nbody\n",
    )
    repo.commit_all()
    result = repo.lint("--check", "frontmatter")
    assert result.returncode == 1
    assert "created" in result.stderr


def test_deprecated_design_doc_may_omit_superseded_by(repo: FixtureRepo) -> None:
    """The conventions require it only when a replacement exists.

    A pure decision record is deprecated without a destination, so demanding the
    field unconditionally would fail a supported case on every run.
    """
    repo.write(
        "docs/design-docs/a.md",
        f"---\ntitle: A\nstatus: deprecated\ncreated: 2020-01-01\n"
        f"last-updated: {TODAY}\n---\n\nbody\n",
    )
    repo.commit_all()
    assert repo.lint("--check", "frontmatter").returncode == 0


def test_blank_superseded_by_is_rejected(repo: FixtureRepo) -> None:
    """Present-but-empty claims a successor and names none, routing nowhere."""
    repo.write(
        "docs/design-docs/a.md",
        f"---\ntitle: A\nstatus: deprecated\ncreated: 2020-01-01\n"
        f"last-updated: {TODAY}\nsuperseded-by:\n---\n\nbody\n",
    )
    repo.commit_all()
    result = repo.lint("--check", "map_paths")
    assert result.returncode == 1
    assert "superseded-by" in result.stderr


def test_generated_doc_needs_generated_flag(repo: FixtureRepo) -> None:
    repo.write("docs/generated/a.md", "---\ntitle: A\n---\n\nbody\n")
    repo.commit_all()
    result = repo.lint("--check", "frontmatter")
    assert result.returncode == 1
    assert "generated" in result.stderr


def test_generated_doc_needs_title_and_last_updated(repo: FixtureRepo) -> None:
    """The generated-docs table requires all three fields, not just the flag.

    Requiring only `generated` let a doc carrying nothing else pass — and
    `freshness` skips it too, since it has no `last-updated` key to check.
    """
    repo.write("docs/generated/a.md", "---\ngenerated: true\n---\n\nbody\n")
    repo.commit_all()
    result = repo.lint("--check", "frontmatter")
    assert result.returncode == 1
    assert "title" in result.stderr
    assert "last-updated" in result.stderr


def test_unknown_design_doc_status_is_rejected(repo: FixtureRepo) -> None:
    """A typo matches no recognized branch, so section routing goes unchecked."""
    repo.write(
        "docs/design-docs/a.md",
        f"---\ntitle: A\nstatus: complete\ncreated: 2020-01-01\n"
        f"last-updated: {TODAY}\n---\n\nbody\n",
    )
    repo.commit_all()
    result = repo.lint("--check", "frontmatter")
    assert result.returncode == 1
    assert "complete" in result.stderr


def test_unknown_status_also_fails_the_section_check(repo: FixtureRepo) -> None:
    """Each check fails closed on its own; either can be disabled independently."""
    repo.write(
        "AGENTS.md",
        _map("Active Design Docs", "| T | `docs/design-docs/a.md` | why |\n"),
    )
    repo.write(
        "docs/design-docs/a.md",
        f"---\ntitle: A\nstatus: actve\ncreated: 2020-01-01\n"
        f"last-updated: {TODAY}\n---\n\nbody\n",
    )
    repo.commit_all()
    result = repo.lint("--check", "map_status_section")
    assert result.returncode == 1
    assert "actve" in result.stderr


def test_blank_required_field_is_rejected(repo: FixtureRepo) -> None:
    """A key present with an empty value satisfies nothing the field exists for."""
    repo.write(
        "docs/references/a.md", f"---\ntitle:\nlast-updated: {TODAY}\n---\n\nx\n"
    )
    repo.commit_all()
    result = repo.lint("--check", "frontmatter")
    assert result.returncode == 1
    assert "empty" in result.stderr


def test_agents_md_is_skipped_by_frontmatter(repo: FixtureRepo) -> None:
    """AGENTS.md and README.md carry no frontmatter by convention."""
    repo.write("AGENTS.md", "# Instructions\n\nbody\n")
    repo.write("README.md", "# Readme\n\nbody\n")
    repo.commit_all()
    assert repo.lint("--check", "frontmatter").returncode == 0


def test_unresolvable_superseded_by_is_flagged(repo: FixtureRepo) -> None:
    repo.write(
        "docs/design-docs/a.md",
        f"---\ntitle: A\nstatus: deprecated\ncreated: 2020-01-01\n"
        f"last-updated: {TODAY}\nsuperseded-by: ../references/gone.md\n---\n\nbody\n",
    )
    repo.commit_all()
    result = repo.lint("--check", "map_paths")
    assert result.returncode == 1
    assert "superseded-by" in result.stderr


# --------------------------------------------------------------------------
# CLI surface
# --------------------------------------------------------------------------


def test_list_checks_reports_config_state(repo: FixtureRepo) -> None:
    result = repo.lint("--list-checks")
    assert result.returncode == 0
    assert "freshness: enabled" in result.stdout


def test_disabled_check_is_skipped(repo: FixtureRepo) -> None:
    repo.config_path.write_text(
        FIXTURE_CONFIG.replace("links = true", "links = false"), encoding="utf-8"
    )
    repo.write_doc("docs/references/a.md", TODAY, "See [b](./nope.md).")
    repo.commit_all()
    # Config off -> clean.
    assert repo.lint("--check", "anchors").returncode == 0
    # An explicit --check overrides config, which is how a new rule gets swept.
    assert repo.lint("--check", "links").returncode == 1


def test_missing_config_file_fails_loudly(repo: FixtureRepo) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--config", str(repo.path / "nope.toml")],
        cwd=repo.path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "config file not found" in result.stderr


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("Simple Heading", "simple-heading"),
        # Whitespace runs do not collapse: the `&` is dropped, both spaces stay.
        ("Migration & Rollout Plan", "migration--rollout-plan"),
        ("Caching & Cache Invalidation", "caching--cache-invalidation"),
        # `_` is a word character and survives; `.`, `(`, `)`, `/` do not.
        (
            "Archival raw dims (`asset_metadata.raw_width` / `raw_height`)",
            "archival-raw-dims-asset_metadataraw_width--raw_height",
        ),
        ("Layer 1: `AsyncGumnut` + 429 retry", "layer-1-asyncgumnut--429-retry"),
        ("**Bold** and *italic*", "bold-and-italic"),
        # Link syntax reaching this function is *literal* text — `heading_text`
        # already reduced a real link to its label — so both words survive, which
        # is what GitHub renders for `# A \[linked\]\(http://x\) word`.
        ("A [linked](http://x) word", "a-linkedhttpx-word"),
        ("Trailing punctuation!", "trailing-punctuation"),
    ],
)
def test_slugify_heading(heading: str, slug: str) -> None:
    assert slugify_heading(heading) == slug


def test_collect_anchors_numbers_duplicates() -> None:
    text = "## Notes\n\n## Notes\n\n## Notes\n"
    assert collect_anchors(text) == {"notes", "notes-1", "notes-2"}


def test_collect_anchors_ignores_fenced_headings() -> None:
    text = "## Real\n\n```\n## Fake\n```\n"
    assert collect_anchors(text) == {"real"}


def test_parse_frontmatter_stops_at_the_closing_delimiter() -> None:
    text = "---\ntitle: A\n---\n\nBody mentions status: not-frontmatter\n"
    assert parse_frontmatter(text) == {"title": "A"}


def test_parse_frontmatter_unquotes_values() -> None:
    assert parse_frontmatter('---\ntitle: "A B"\n---\n') == {"title": "A B"}


def test_parse_frontmatter_requires_a_leading_delimiter() -> None:
    assert parse_frontmatter("# Heading\n\ntitle: A\n") == {}


def test_has_last_updated_key_detects_a_blank_value() -> None:
    assert has_last_updated_key("---\ntitle: A\nlast-updated:\n---\n")


def test_has_last_updated_key_ignores_a_body_mention() -> None:
    assert not has_last_updated_key("---\ntitle: A\n---\n\nlast-updated: 2020-01-01\n")


def test_strip_date_line_removes_only_the_frontmatter_date() -> None:
    text = "---\ntitle: A\nlast-updated: 2020-01-01\n---\n\nlast-updated: keep\n"
    assert strip_date_line(text) == "---\ntitle: A\n---\n\nlast-updated: keep\n"


def test_bump_date_line_preserves_indentation() -> None:
    text = "---\ntitle: A\n  last-updated: 2020-01-01\n---\n\nbody\n"
    assert "  last-updated: 2026-01-01" in bump_date_line(text, "2026-01-01")


def _script_metadata() -> dict:
    """The script's PEP 723 block, parsed."""
    import tomllib

    source = SCRIPT.read_text(encoding="utf-8")
    block = re.search(r"^# /// script$(.*?)^# ///$", source, re.M | re.S)
    assert block is not None, "lint_docs.py must carry a PEP 723 script block"
    # Exactly one comment level, per PEP 723's reference algorithm. Stripping two
    # (`removeprefix("# ").removeprefix("#")`) mangles a TOML comment *inside* the
    # block into bare text, which then fails to parse.
    return tomllib.loads(
        "\n".join(
            line[2:] if line.startswith("# ") else line[1:]
            for line in block.group(1).strip().split("\n")
        )
    )


def test_pep723_dependency_matches_test_environment() -> None:
    """The dependency is declared twice, so pin the two together.

    The PEP 723 header serves `uv run`; the test environment serves this suite,
    which imports `lint_docs` as a module rather than shelling out. Nothing else
    would notice if they drifted until a version-specific behavior diverged.
    """
    from markdown_it import __version__ as installed

    meta = _script_metadata()
    specs = [d for d in meta["dependencies"] if d.startswith("markdown-it-py")]
    assert len(specs) == 1, meta["dependencies"]

    bounds = re.search(r">=(\d+),<(\d+)", specs[0])
    assert bounds is not None, specs[0]
    lower, upper = bounds.groups()
    major = int(installed.split(".")[0])
    assert int(lower) <= major < int(upper), (
        f"installed markdown-it-py {installed} is outside the script's "
        f"declared range {specs[0]}"
    )


def test_script_lock_pins_the_tested_version() -> None:
    """CI must run the parser the suite actually exercised.

    Without the adjacent lockfile, `uv run` resolves the range in a fresh
    ephemeral environment on every run, so the repo-wide check could execute a
    release no test ever saw — and skip the resolver cooldown while doing it.
    This asserts the lock and the test environment agree on the exact version.
    """
    import tomllib

    from markdown_it import __version__ as installed

    lock_path = SCRIPT.with_name(SCRIPT.name + ".lock")
    assert lock_path.is_file(), (
        f"{lock_path.name} is missing — regenerate with "
        f"`uv lock --script scripts/lint_docs.py`"
    )
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    pinned = {p["name"]: p["version"] for p in lock["package"]}
    assert pinned.get("markdown-it-py") == installed, (
        f"lockfile pins markdown-it-py {pinned.get('markdown-it-py')} but the test "
        f"environment has {installed}; re-lock both so CI runs what the suite tested"
    )


def test_script_declares_the_supply_chain_cooldown() -> None:
    """The ephemeral resolve would otherwise bypass the repo's cooldown policy."""
    assert _script_metadata()["tool"]["uv"]["exclude-newer"] == "14 days"


def test_blank_frontmatter_preserves_line_count() -> None:
    """Token line numbers are file line numbers, so the blanking cannot shift them."""
    text = "---\ntitle: A\nlast-updated: 2026-01-01\n---\n\n# Heading\n\nbody\n"
    blanked = blank_frontmatter(text)
    assert len(blanked.split("\n")) == len(text.split("\n"))
    assert blanked.split("\n")[5] == "# Heading"


def test_blank_frontmatter_leaves_a_doc_without_frontmatter_alone() -> None:
    text = "# Heading\n\nbody\n"
    assert blank_frontmatter(text) == text


def test_blank_frontmatter_leaves_an_unterminated_block_alone() -> None:
    """Blanking to EOF would hide the whole doc from every other check.

    `check_frontmatter` reports the missing delimiter as its own violation.
    """
    text = "---\ntitle: A\n\n# Heading\n"
    assert blank_frontmatter(text) == text


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("| a | b | c |", 3),
        ("| a | b |", 2),
        ("| a | b | c | d |", 4),
        (r"| a | b \| c | d |", 3),
        (r"| a | `x \| y` | c |", 3),
        ("| a | x | y | c |", 4),
        # Trailing-pipe escaping is decided by backslash *parity*. An even run
        # means the pipe is the row delimiter: a final cell ending in a literal
        # backslash counted four cells for a valid three-column row.
        ("| a | b | c" + "\\" * 2 + "|", 3),
        # An odd run escapes it, so there is no closing delimiter and the last
        # cell is `c|`.
        ("| a | b | c" + "\\" + "|", 3),
    ],
)
def test_raw_cell_count_counts_unescaped_delimiters(line: str, expected: int) -> None:
    """The parser normalizes column counts away, so the source line is the authority.

    A three-column table pads a short row and *truncates* a long one, so without
    this both shapes would pass.
    """
    assert raw_cell_count(line) == expected
