"""Tests for scripts/lint_docs.py.

The freshness tests are integration tests, ported from the `lint_docs_test.sh`
harness this file replaces: each builds a throwaway git repo with a `main`
branch, makes working-tree edits, and asserts the linter's report. Running the
real script against a real repo is what pins the git-plumbing behavior (merge
base, `--diff-filter`, `git show <rev>:<path>`) that a unit test of the pure
helpers would miss.

The clock is pinned via `LINT_DOCS_TODAY` / `LINT_DOCS_NOW_EPOCH` so a fixture
and the assertions cannot straddle a date boundary.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lint_docs import (
    blank_code_spans,
    blank_fenced_blocks,
    bump_date_line,
    collect_anchors,
    has_last_updated_key,
    parse_frontmatter,
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
    """The `lint_docs_test.sh` baseline and working-tree edits, ported verbatim."""
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
# `lint_docs.sh` skipped any doc absent at the merge-base, so a doc created on
# day 1 and edited on day 2 kept its day-1 date and every run — including --fix —
# reported clean. Caught in the wild on photos#1480.


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
    assert result.returncode == 0, result.stderr


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
    assert result.returncode == 0, result.stderr


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
    repo.write("AGENTS.md", "# A\n")
    repo.commit_all()
    # Never staged.
    repo.write("docs/references/new.md", "---\ntitle: N\n---\n\nbody\n")
    result = repo.lint("--check", "frontmatter")
    assert result.returncode == 1
    assert "new.md" in result.stderr
    assert "last-updated" in result.stderr


def test_untracked_doc_needs_a_current_date(repo: FixtureRepo) -> None:
    """Freshness covers it too: an untracked doc has no base blob to compare."""
    repo.write("AGENTS.md", "# A\n")
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
    repo.write(
        "subproj/AGENTS.md",
        _map("References", "| Thing | `repo-root docs/references/a.md` | why |\n"),
    )
    # Both exist, so a wrong base still resolves — the bug is silent.
    repo.write_doc("subproj/docs/references/a.md", TODAY, "project copy")
    repo.write_doc("docs/references/a.md", TODAY, "root copy")
    repo.commit_all()
    assert repo.lint("--check", "map_paths").returncode == 0

    # Delete only the root copy. The row must now fail: if it resolved to the
    # project copy it would keep passing, which is exactly the defect.
    (repo.path / "docs/references/a.md").unlink()
    repo.write(
        "AGENTS.md",
        _map("References", "| P | `subproj/docs/references/a.md` | why |\n"),
    )
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
        ("A [linked](http://x) word", "a-linked-word"),
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


def test_blank_fenced_blocks_preserves_line_count() -> None:
    text = "a\n```\nb\n```\nc\n"
    assert len(blank_fenced_blocks(text).split("\n")) == len(text.split("\n"))


def test_blank_code_spans_preserves_offsets() -> None:
    text = "see `[x](y.md)` here"
    blanked = blank_code_spans(text)
    assert len(blanked) == len(text)
    assert "[x](y.md)" not in blanked
