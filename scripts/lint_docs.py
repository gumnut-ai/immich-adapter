#!/usr/bin/env python3
"""scripts/lint_docs.py

Documentation linter. Enforces the machine-checkable half of this repo's
documentation conventions; its `AGENTS.md` names where they live and which checks
run here. Deliberately no path to that doc: this file is byte-identical across
repos, so any repo-specific path in it is wrong in at least one of them.

Checks (each independently switchable in `lint_docs.toml`, so a new rule can
land dark and be enabled once its sweep is done):

  freshness           `last-updated:` is bumped on an edited doc, and current on
                      a doc added on this branch
  links               inline markdown link targets resolve
  anchors             `#fragment` targets resolve to a real heading or <a id>
  map_paths           every Documentation Map row's path resolves, as does every
                      `superseded-by:` frontmatter target
  map_status_section  a design doc's map section matches its `status:`
  map_cells           the "Consult when..." cell stays one short line
  frontmatter         per-directory required frontmatter fields

`freshness` is diff-scoped — it needs a merge-base to compare against. Every
other check runs repo-wide, which is the point: renaming a doc breaks inbound
links from files the PR never touched, and a diff-scoped check stays green while
it happens.

Stdlib only, so it runs anywhere `python3` does (>=3.11 for `tomllib`). Repo
layout differences live in `lint_docs.toml`, which keeps this file identical
across the Gumnut repos that use it.

Usage (runnable from anywhere inside the repo):
  scripts/lint_docs.py                        # check; non-zero exit on violations
  scripts/lint_docs.py --fix                  # bump last-updated on offenders
  scripts/lint_docs.py --base origin/main
  scripts/lint_docs.py --check freshness --check links
  scripts/lint_docs.py --list-checks
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import NoReturn

DEFAULT_BASE = "origin/main"
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DOC_SUFFIXES = (".md", ".mdx")


def is_in_hidden_dir(rel: str) -> bool:
    """Whether any directory component is dot-prefixed.

    Untracked files are in scope so a new doc is checked before it is staged, but
    that also exposes tooling scratch space that no `.gitignore` happens to cover
    — agent worktrees under `.claude/`, `.ruff_cache/`, framework caches. A
    checkout parked in one of those would be linted as part of this repo. No
    convention puts docs in a dotted directory, so excluding them costs nothing.
    """
    return any(part.startswith(".") for part in Path(rel).parts[:-1])


def is_iso_date(value: str) -> bool:
    """Whether a value is a real YYYY-MM-DD calendar date.

    Shape and calendar validity are both required. The regex alone accepts
    impossible dates such as `2026-02-31`, which would then be reported as valid;
    `date.fromisoformat` alone accepts forms the convention doesn't use (a full
    datetime, or an unpadded `2026-2-8`).
    """
    if not ISO_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


# At any instant, current local calendar dates range from UTC-12 through UTC+14.
# Accepting the dates at those boundaries (and UTC itself) lets contributors and
# CI runners in different timezones agree that a doc was updated today.
TZ_WINDOW_EARLIEST_SECONDS = 43_200
TZ_WINDOW_LATEST_SECONDS = 50_400

# A design-doc template carries placeholder frontmatter — its `status: active` is
# boilerplate rather than a live plan, and its dates are literally `YYYY-MM-DD` — so
# every check that reads those values must exempt it.
#
# Exempt paths are configured per repo (`template_paths` in `lint_docs.toml`) and
# default to none. Matching on basename alone would exempt any future `TEMPLATE.md`
# anywhere in the tree, letting a real doc keep placeholder dates and evade map
# enforcement; hardcoding one path here would name a file two of the three repos
# sharing this script do not have.


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Ignore:
    path: str
    checks: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Config:
    # Citation prefixes stripped before a path is resolved. `repo-root ` marks a
    # repo-level doc cited from a project that has a same-named file of its own.
    strip_prefixes: tuple[str, ...] = ("repo-root ",)
    # Prefixes a map uses to cite *this* repo's own files. A map symlinked into
    # the dev root cites through the repo name, which doesn't resolve from inside
    # the repo.
    self_prefixes: tuple[str, ...] = ()
    # Prefixes marking a citation of a *different* repo. The conventions qualify
    # those with the org (`<org>/<repo>/docs/...`) precisely because they cannot
    # resolve from inside this repo, so they are skipped rather than reported.
    # Paths that escape the repo root are treated the same way — see
    # Repo.is_cross_repo.
    cross_repo_prefixes: tuple[str, ...] = ("gumnut-ai/",)
    # Repo-relative paths of design-doc templates, whose frontmatter is placeholder
    # text. Empty by default: a repo that has one declares it.
    template_paths: tuple[str, ...] = ()
    enabled: frozenset[str] = frozenset()
    consult_cell_chars: int = 250
    ignores: tuple[Ignore, ...] = ()

    def ignored(self, rel_path: str, check: str) -> bool:
        for ig in self.ignores:
            if check not in ig.checks and "*" not in ig.checks:
                continue
            if rel_path == ig.path or fnmatch.fnmatch(rel_path, ig.path):
                return True
        return False

    def is_template(self, rel: str) -> bool:
        """Whether this exact path is a configured design-doc template."""
        return rel in self.template_paths

    def strip_citation(self, cite: str) -> tuple[str, bool]:
        """Strip any anchoring prefix, reporting whether one applied.

        A stripped prefix means the citation was **deliberately anchored to the
        repo root**, so the caller must resolve it there and nowhere else. Both
        prefix families say that: `repo-root ` marks a root-level doc cited from a
        project holding a same-named file, and a `self_prefixes` entry is a map
        citing this repo through its own name. Dropping the marker and then
        resolving relative-first would send the citation to the very same-named
        file the marker exists to disambiguate from.
        """
        out = cite.strip()
        anchored = False
        for prefix in (*self.strip_prefixes, *self.self_prefixes):
            if out.startswith(prefix):
                out = out[len(prefix) :].strip()
                anchored = True
        return out, anchored


CONFIG_TOP_KEYS = frozenset(
    {
        "strip_prefixes",
        "self_prefixes",
        "cross_repo_prefixes",
        "template_paths",
        "checks",
        "limits",
        "ignore",
    }
)
CONFIG_LIMIT_KEYS = frozenset({"consult_cell_chars"})
CONFIG_IGNORE_KEYS = frozenset({"path", "checks", "reason"})


def load_config(path: Path, all_checks: tuple[str, ...]) -> Config:
    raw: dict = {}
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)

    # An unrecognized key is an error, not something to ignore. TOML scopes bare
    # keys to the table above them, so a top-level setting appended after a
    # `[limits]` or `[[ignore]]` header silently becomes a key of that table and
    # does nothing — the exemption or setting reads as configured while having no
    # effect. A typo'd name fails the same silent way. (Written after doing exactly
    # this with `template_paths`.)
    unknown = sorted(set(raw) - CONFIG_TOP_KEYS)
    if unknown:
        fail(
            f"{path}: unknown top-level key(s): {', '.join(unknown)}. "
            f"A key placed after a `[table]` header belongs to that table — "
            f"top-level settings must come before the first one."
        )
    unknown_limits = sorted(set(raw.get("limits", {})) - CONFIG_LIMIT_KEYS)
    if unknown_limits:
        fail(f"{path}: unknown key(s) in [limits]: {', '.join(unknown_limits)}")
    unknown_checks = sorted(set(raw.get("checks", {})) - set(all_checks))
    if unknown_checks:
        fail(f"{path}: unknown check name(s) in [checks]: {', '.join(unknown_checks)}")
    # Where a stray top-level key most often lands, since `[[ignore]]` tends to be
    # last in the file — so this is the case that actually catches the mistake.
    for i, entry in enumerate(raw.get("ignore", [])):
        stray = sorted(set(entry) - CONFIG_IGNORE_KEYS)
        if stray:
            fail(
                f"{path}: unknown key(s) in [[ignore]] #{i + 1}: "
                f"{', '.join(stray)}. A top-level setting written after an "
                f"`[[ignore]]` header becomes a key of that entry and has no effect."
            )
        if "path" not in entry:
            fail(f"{path}: [[ignore]] #{i + 1} has no `path`")

    checks = raw.get("checks", {})
    # A check absent from config defaults to on, so adding a check to this file
    # doesn't silently do nothing in a repo whose config predates it.
    enabled = frozenset(name for name in all_checks if checks.get(name, True))

    ignores = tuple(
        Ignore(
            path=entry["path"],
            checks=tuple(entry.get("checks", ("*",))),
            reason=entry.get("reason", ""),
        )
        for entry in raw.get("ignore", [])
    )

    limits = raw.get("limits", {})
    return Config(
        strip_prefixes=tuple(raw.get("strip_prefixes", ("repo-root ",))),
        self_prefixes=tuple(raw.get("self_prefixes", ())),
        cross_repo_prefixes=tuple(raw.get("cross_repo_prefixes", ("gumnut-ai/",))),
        template_paths=tuple(raw.get("template_paths", ())),
        enabled=enabled,
        consult_cell_chars=int(limits.get("consult_cell_chars", 250)),
        ignores=ignores,
    )


# --------------------------------------------------------------------------
# Violations
# --------------------------------------------------------------------------


@dataclass
class Violation:
    check: str
    path: str
    message: str
    line: int | None = None
    warning: bool = False

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"  {where}: {self.message} [{self.check}]"


# --------------------------------------------------------------------------
# Markdown helpers
# --------------------------------------------------------------------------

# The whole delimiter run, not just its first three characters: a fence closes only
# on the same marker at least as long, so the run length has to be known.
# The delimiter run plus whatever follows it. A *closing* fence may carry only
# whitespace after the run, so a same-length language-tagged line (```` ````python ````
# inside a ```` block) is an opener, not a closer — treating it as one ended the block
# early and exposed the rest to the link parser.
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
# Up to three leading spaces, which CommonMark permits on an ATX heading. Anchored at
# column 0, an indented heading contributed no anchor and a valid link to it was
# reported broken. Four or more spaces is an indented code block, not a heading, so the
# bound matters.
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
# A complete HTML comment, possibly spanning lines. Only complete ones: blanking from an
# unterminated `<!--` would silently swallow the rest of the file.
# A code span is delimited by a *run* of backticks and closed by a run of the same
# length. Matching only single backticks left a ``double-backtick`` span unblanked,
# so a link written inside one as an example was parsed as a real link and reported
# broken — a false positive on correct content.
# Only the `[label](` prefix is matched here. The destination is scanned, not matched:
# a regex cannot balance arbitrary nesting, and each fixed-depth attempt left a real
# form unparsed — `foo_(bar).md` truncated at the first `)` (a *false* break on a file
# that exists), then one nesting level was accepted but `foo_(a(b)).md` matched nothing
# at all (a broken target never looked at). `(?<![!\\])` skips images and prose showing
# literal markdown, both of which render no link.
LINK_LABEL_RE = re.compile(r"(?<![!\\])\[([^\]\[]*)\]\(")
HTML_ANCHOR_TAG_RE = re.compile(r"<a\s[^>]*>", re.IGNORECASE | re.DOTALL)
# `(?<![\w-])`, not `\b`: a hyphen is a non-word character, so `\b` matched the tail
# of `data-id` / `aria-name` and recorded their values as fragment anchors — making a
# link to a nonexistent fragment pass. Worse, in `<a data-id="x" id="t">` the first
# match was `x`, so the *real* anchor was missed too.
HTML_ANCHOR_ATTR_RE = re.compile(
    r"(?<![\w-])(?:id|name)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
)
TABLE_ROW_RE = re.compile(r"^\|(.+)\|\s*$")
# Exact match, so a heading that merely *starts* with the phrase is not read as a
# map. Prose sections about the convention do (`Documentation Maps`,
# `Documentation Map Sections`), and their non-map tables would be parsed as rows.
# See is_map_heading.
MAP_HEADING_RE = re.compile(r"^Documentation Map$")


def blank_noncontent(
    text: str, *, blank_spans: bool = True, drop_comments: bool = False
) -> str:
    """Blank fenced blocks, HTML comments, and optionally inline code spans.

    **One pass, because these three contexts nest and no ordering of separate passes is
    correct.** Blanking comments first let a `<!--` shown as code pair with a later real
    `-->` and erase the live links between them (a silent miss); blanking fences first
    let a ``` inside a genuine comment open a spurious fence (also a silent miss). Here
    whichever construct opens first wins, which is what a Markdown renderer does.

    Line count is always preserved so reported line numbers stay right.

    `blank_spans` controls only whether a code span's *content* is replaced; spans are
    recognized either way, since that recognition is what stops a code literal from
    being read as a comment. Callers that slug headings pass False, because
    `slugify_heading` strips backticks itself and filler would corrupt the slug.

    Comments are **removed** rather than filled, so a heading sharing a line with one
    (`## Setup <!-- old -->`) slugs from `Setup` as it renders. Filling would leave the
    comment's width behind as hyphens, since GitHub does not collapse whitespace runs.
    """
    return "\n".join(_blank_scan(text, blank_spans, drop_comments)[0])


def has_unterminated_comment(text: str) -> bool:
    """Whether the doc opens an HTML comment it never closes.

    Per CommonMark an unterminated `<!--` block "continues until the end of the
    document", so everything after it renders as nothing — and therefore drops out of
    every check. That is spec-correct but silent, so it is reported: an unclosed comment
    is almost always an authoring slip, and it hides content from readers too.
    """
    return _blank_scan(text, True, False)[1]


def _blank_scan(
    text: str, blank_spans: bool, drop_comments: bool
) -> tuple[list[str], bool]:
    """The scan itself. Returns (blanked lines, ended inside an open comment).

    Three states persist across lines, each for a reason CommonMark dictates:

    * **fence** — a fenced block runs until a matching closing fence.
    * **comment** — an HTML comment block runs to `-->`, across blank lines.
    * **code span** — a span may contain line endings, but only within a paragraph, so
      a blank line ends the paragraph and an unclosed span was literal text after all.
      Fences are block constructs parsed before inlines, so a fence line also ends it.

    Carrying none of these was a real defect rather than a nicety: a span opened on one
    line and closed on the next left a `<!--` inside it looking like a live comment
    opener, which both reported a spurious unterminated-comment error and swallowed the
    links after the span.
    """
    out: list[str] = []
    fence: str | None = None
    in_comment = False
    span: str | None = None
    prev_blank = True
    in_indented_code = False
    for line in text.split("\n"):
        if in_comment:
            end = line.find("-->")
            if end == -1:
                out.append("")
                continue
            scanned, span, in_comment = _scan_inline(
                line[end + 3 :], blank_spans, drop_comments, None
            )
            out.append(" " * (end + 3) + scanned)
            continue
        match = FENCE_RE.match(line)
        if fence is not None:
            if (
                match
                and match.group(1)[0] == fence[0]
                and len(match.group(1)) >= len(fence)
                and not match.group(2).strip()
            ):
                fence = None
            out.append("")
            span = None
            continue
        if match:
            fence = match.group(1)
            out.append("")
            span = None
            continue
        if not line.strip():
            span = None
            prev_blank = True
            out.append(line)
            continue
        # An indented code block (four spaces, opening after a blank line) is code, so a
        # literal `<!--` in it opened no comment. Bounded by the blank line because
        # indented code cannot interrupt a paragraph — without that, a four-space list
        # continuation would be blanked and its real links lost.
        if (prev_blank or in_indented_code) and line.startswith("    "):
            in_indented_code = True
            prev_blank = False
            span = None
            out.append("")
            continue
        in_indented_code = False
        prev_blank = False
        scanned, span, in_comment = _scan_inline(line, blank_spans, drop_comments, span)
        out.append(scanned)
    return out, in_comment


def _find_closing_run(line: str, run: str, start: int) -> int:
    """Index of a backtick run of *exactly* `len(run)`, at or after `start`.

    Both sides are checked. Looking only at the character after the candidate accepted
    a suffix of a longer run — in `` `foo`` x` `` the double run cannot close a single
    backtick span, but the second of its two backticks passed the one-sided test, so the
    span was closed early and the code content after it was scanned as live markdown.
    """
    width, n = len(run), len(line)
    i = start
    while True:
        at = line.find(run, i)
        if at == -1:
            return -1
        if (at == 0 or line[at - 1] != "`") and (
            at + width >= n or line[at + width] != "`"
        ):
            return at
        i = at + 1


def _scan_inline(
    line: str, blank_spans: bool, drop_comments: bool, span: str | None
) -> tuple[str, str | None, bool]:
    """Blank code spans and comments in one line.

    Returns (line, code-span run still open, comment left open).
    """
    out: list[str] = []
    i, n = 0, len(line)
    if span is not None:
        close = _find_closing_run(line, span, 0)
        if close == -1:
            return ("x" * n if blank_spans else line), span, False
        end = close + len(span)
        out.append("x" * end if blank_spans else line[:end])
        i = end
    while i < n:
        char = line[i]
        # A backslash escape makes the next punctuation character literal, so `\<!--`
        # opens no comment and ``\` `` opens no span. Both were being read as markup,
        # each swallowing the live links after it.
        if char == "\\" and i + 1 < n and not line[i + 1].isalnum():
            out.append(line[i : i + 2])
            i += 2
            continue
        if char == "`":
            j = i
            while j < n and line[j] == "`":
                j += 1
            run = line[i:j]
            close = _find_closing_run(line, run, j)
            if close == -1:
                # Unclosed on this line: it may continue into the next one.
                out.append("x" * (n - i) if blank_spans else line[i:])
                return "".join(out), run, False
            end = close + len(run)
            out.append("x" * (end - i) if blank_spans else line[i:end])
            i = end
            continue
        if line.startswith("<!--", i):
            close = line.find("-->", i + 4)
            if close == -1:
                return "".join(out) + " " * (n - i), None, True
            # Spaces, not removal: a comment is a token boundary. Concatenating what
            # surrounds it invented syntax that does not render — `[x]<!--c-->(./y)`
            # became a link, and `<!--c--> ## H` became a heading. Callers that need the
            # heading *text* pass drop_comments and get the comment elided instead.
            if not drop_comments:
                out.append(" " * (close + 3 - i))
            i = close + 3
            continue
        out.append(char)
        i += 1
    return "".join(out), None, False


def slugify_heading(heading: str) -> str:
    """GitHub's heading-anchor slug.

    Two details matter, and getting either wrong produces confident false
    positives (both were caught by running this over the real tree):

    * runs of whitespace do NOT collapse. `## Migration & Rollout Plan` becomes
      `migration--rollout-plan`, because the `&` is dropped and each surrounding
      space still becomes a hyphen.
    * `_` survives. It is a word character, and inside a code span it is literal
      text, so `asset_metadata.raw_width` keeps both underscores.
    """
    s = heading.strip()
    # Link syntax in a heading contributes only its label.
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    # Inline-formatting markup is not part of the rendered text. `_` is excluded
    # deliberately — see the docstring.
    s = re.sub(r"[`*~]", "", s)
    s = s.lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    return s.strip().replace(" ", "-")


def collect_anchors(text: str) -> set[str]:
    """Every fragment a `#...` link in this file could target."""
    # Comments blanked for both scans below: a commented-out `<a id>` *or* heading
    # renders nothing, so neither defines a fragment.
    # Two renderings of the same text, same line count, used for different questions.
    # `body` has comments as spaces, which answers *is this line a heading* — a leading
    # comment pushes the `#` run past the three-space bound, and CommonMark agrees that
    # is no heading. `texts` has them elided, which answers *what is the heading text* —
    # spaces there would land in the slug as hyphens, since GitHub does not collapse
    # whitespace runs.
    body = blank_noncontent(text, blank_spans=False)
    texts = blank_noncontent(text, blank_spans=False, drop_comments=True).split("\n")
    anchors: set[str] = set()
    # Code spans are blanked for the *tag* scan only: an `<a id="fake">` shown as an
    # inline-code example renders no anchor, so counting it let `[x](#fake)` pass. The
    # heading scan below deliberately reads `body` with its spans intact, because
    # `slugify_heading` strips the backticks itself — slugging blanked text would turn
    # `## The \`foo\` helper` into `the-xxxxx-helper` and break every heading anchor
    # containing inline code.
    for tag in HTML_ANCHOR_TAG_RE.findall(blank_noncontent(text)):
        # Every id/name in the tag: `<a name="old" id="new">` defines both.
        for attr in HTML_ANCHOR_ATTR_RE.finditer(tag):
            anchors.add(attr.group(1))
    # GitHub disambiguates a repeated slug with -1, -2, ... leaving the first bare, and
    # the suffix must land on an id nothing else already has. Counting per base instead
    # collides with a *literal* heading of the suffixed name: headings `Foo`, `Foo`,
    # `Foo-1` are `foo`, `foo-1`, `foo-1-1`, but per-base counting emitted `foo-1` twice
    # and never `foo-1-1`, so a valid link to it was reported broken. Slugs are ids, so
    # they cannot repeat — dedupe against the whole set.
    used: set[str] = set()
    for lineno, line in enumerate(body.split("\n")):
        m = HEADING_RE.match(line)
        if not m:
            continue
        elided = HEADING_RE.match(texts[lineno]) if lineno < len(texts) else None
        base = slugify_heading((elided or m).group(2))
        if not base:
            continue
        slug, n = base, 0
        while slug in used:
            n += 1
            slug = f"{base}-{n}"
        used.add(slug)
        anchors.add(slug)
    return anchors


def parse_frontmatter(text: str) -> dict[str, str]:
    """Parse the leading YAML frontmatter block.

    Only the subset the conventions use: flat `key: value` pairs. A body mention
    of a key (e.g. a doc documenting the convention) is not frontmatter and is
    ignored, which is why parsing stops at the closing delimiter.

    An **unterminated** block yields no fields. Returning what was collected
    before EOF would treat a truncated doc — whose whole body is swallowed into an
    unclosed block, and which no renderer reads as frontmatter — as having valid
    frontmatter. `has_unclosed_frontmatter` reports that case as its own
    violation, so it fails with a message about the delimiter rather than only
    about the fields it appears to be missing.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        m = re.match(r"^\s*([A-Za-z0-9_-]+)\s*:(.*)$", line)
        if m:
            fields[m.group(1)] = unquote(m.group(2))
    return {}


def has_unclosed_frontmatter(text: str) -> bool:
    """Whether a doc opens a frontmatter block and never closes it."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return False
    return not any(line.strip() == "---" for line in lines[1:])


def unquote(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


# --------------------------------------------------------------------------
# Repo context
# --------------------------------------------------------------------------


@dataclass
class Repo:
    root: Path
    config: Config
    docs: tuple[str, ...] = ()
    doc_roots: tuple[str, ...] = ()
    project_roots: tuple[str, ...] = ()
    _text: dict[str, str] = field(default_factory=dict)
    _anchors: dict[str, set[str]] = field(default_factory=dict)

    def text(self, rel: str) -> str:
        if rel not in self._text:
            try:
                self._text[rel] = (self.root / rel).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                self._text[rel] = ""
        return self._text[rel]

    def anchors(self, rel: str) -> set[str]:
        if rel not in self._anchors:
            self._anchors[rel] = collect_anchors(self.text(rel))
        return self._anchors[rel]

    def frontmatter(self, rel: str) -> dict[str, str]:
        return parse_frontmatter(self.text(rel))

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            check=check,
        )

    def is_cross_repo(self, cite: str, relative_to: str) -> bool:
        """Whether a citation deliberately names a different repo.

        Such a path cannot be verified from inside this one, so it is skipped
        rather than reported. Two documented forms qualify:

        * the org-qualified form the conventions prescribe for prose citations
          (`<org>/<repo>/docs/...`), matched by `cross_repo_prefixes`;
        * a path that climbs out of the repo to a sibling checkout
          (`../../../<sibling-repo>/docs/...`), which the team conventions
          prescribe for a cross-repo `superseded-by:`.

        The escape test is **pure path arithmetic with no filesystem access**, so
        the verdict is identical on a dev box where the sibling repo happens to be
        cloned and in CI where it never is. Deciding this by existence instead
        would make the check environment-dependent — and since CI clones only this
        repo, every such path would fail there regardless of how it is written.
        """
        cite, _ = self.config.strip_citation(cite)
        if not cite:
            return False
        if any(cite.startswith(p) for p in self.config.cross_repo_prefixes):
            return True
        joined = os.path.join(str(Path(relative_to).parent), cite)
        return os.path.normpath(joined).startswith("..")

    def resolve(
        self,
        cite: str,
        relative_to: str,
        *,
        require_file: bool = False,
        require_doc: bool = False,
    ) -> str | None:
        """Resolve a cited path to a repo-relative path, or None.

        Tries, in order: relative to the citing file's directory, then each
        project root above it, then the repo root. The project-root rung is what
        makes a doc inside a nested project (`<project>/docs/`) citing
        `docs/architecture/foo.md` resolve — the conventions permit that form, and
        a resolver that only tries the citing directory and the repo root reports
        it as broken.

        A citation carrying an anchoring prefix skips that ladder entirely and
        resolves **only** at the repo root. The prefix exists precisely because a
        same-named file sits nearer the citing doc, so trying the nearer bases
        first would resolve to the file the author marked the citation to avoid —
        validating the wrong doc, and still passing after the intended target is
        deleted.
        """
        cite, root_anchored = self.config.strip_citation(cite)
        if not cite or cite.startswith("/"):
            return None
        if root_anchored:
            bases = [Path(".")]
        else:
            bases = [Path(relative_to).parent]
            bases.extend(
                Path(proj)
                for proj in self.project_roots
                if relative_to.startswith(proj + "/")
            )
            bases.append(Path("."))
        for base in bases:
            candidate = (self.root / base / cite).resolve()
            # `require_file` for citations that must name a *document*: a map row
            # or `superseded-by:` pointing at `docs/references/` satisfied a bare
            # `exists()` while routing a reader to no document at all, and outside
            # `design-docs/` nothing else would notice. Prose links stay lenient —
            # a README linking to `docs/` is deliberate and renders fine.
            if (require_file or require_doc) and not candidate.is_file():
                continue
            # `require_doc` additionally demands a documentation suffix, for a citation
            # whose whole purpose is to route a reader to a *doc*: a map row citing
            # `scripts/lint_docs.py` resolves to a real file and still routes nowhere.
            # Not applied to `superseded-by`, which legitimately names a non-markdown
            # successor — a deprecated doc whose canonical text is now a rendered page.
            if require_doc and not candidate.name.endswith(DOC_SUFFIXES):
                continue
            if not candidate.exists():
                continue
            try:
                return str(candidate.relative_to(self.root))
            except ValueError:
                # Exists, but outside the repo — a `../..` escape that happens to
                # land on a sibling clone in someone's dev root. Treating that as
                # resolved would pass here and fail for anyone who cloned only
                # this repo, so it counts as unresolved. Cross-repo citations are
                # qualified with the repo name by convention and never resolve.
                return None
        return None

    def invalidate(self, rel: str) -> None:
        """Drop cached content for a file this process just rewrote."""
        self._text.pop(rel, None)
        self._anchors.pop(rel, None)


def discover_repo(config: Config) -> Repo:
    try:
        root_out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        fail("not inside a git repository")
    root = Path(root_out.stdout.strip()).resolve()

    # The *working tree*, not the index. `ls-files` alone describes the index, so
    # running this before staging skipped a newly created doc entirely and still
    # listed an unstaged deletion — which then linted as an empty file. Either way
    # the local pre-commit run disagreed with CI, which is the one thing a
    # pre-commit check must not do. `--others --exclude-standard` adds untracked,
    # non-ignored files; the existence filter below drops deleted ones.
    listed = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    present = sorted({p for p in listed if p and (root / p).is_file()})
    tracked_only = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    tracked = [p for p in tracked_only if p and (root / p).is_file()]

    # Doc roots and project roots are discovered, not configured: any tracked
    # directory named `docs` is a doc root, and its parent is a project root.
    #
    # Discovered from *tracked* paths only. An untracked tree must not be able to
    # introduce a doc root — vendored packages a deleted `.gitignore` stopped
    # covering, or a checkout parked inside the repo, would otherwise pull thousands
    # of unrelated files into scope. Untracked docs are still linted, but only inside
    # a root the repo already has, so `.gitignore` completeness is not load-bearing.
    doc_roots: set[str] = set()
    project_roots: set[str] = set()
    for p in tracked:
        parts = Path(p).parts
        for i, part in enumerate(parts[:-1]):
            if part != "docs":
                continue
            doc_roots.add("/".join(parts[: i + 1]))
            project_roots.add("/".join(parts[:i]))
    project_roots.discard("")

    docs = tuple(
        p
        for p in present
        if p.endswith(DOC_SUFFIXES)
        and not is_in_hidden_dir(p)
        and (p in set(tracked) or any(p.startswith(dr + "/") for dr in doc_roots))
    )

    return Repo(
        root=root,
        config=config,
        docs=docs,
        doc_roots=tuple(sorted(doc_roots)),
        # Longest first, so `app/web` is tried before `app`.
        project_roots=tuple(sorted(project_roots, key=lambda s: (-len(s), s))),
    )


def fail(message: str) -> NoReturn:
    print(f"lint_docs: {message}", file=sys.stderr)
    raise SystemExit(1)


# --------------------------------------------------------------------------
# Clock
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Clock:
    today: str
    current_dates: frozenset[str]

    @classmethod
    def build(cls) -> Clock:
        # Tests pin the clock so a fixture and this process cannot straddle a
        # date boundary. Normal invocations use the system clock.
        now = int(os.environ.get("LINT_DOCS_NOW_EPOCH") or time.time())
        today = os.environ.get("LINT_DOCS_TODAY") or time.strftime(
            "%Y-%m-%d", time.localtime(now)
        )
        return cls(
            today=today,
            current_dates=frozenset(
                time.strftime("%Y-%m-%d", time.gmtime(now + delta))
                for delta in (
                    -TZ_WINDOW_EARLIEST_SECONDS,
                    0,
                    TZ_WINDOW_LATEST_SECONDS,
                )
            ),
        )

    def is_current(self, date: str) -> bool:
        return date in self.current_dates


# --------------------------------------------------------------------------
# freshness
# --------------------------------------------------------------------------


def strip_date_line(text: str) -> str:
    """Echo a doc with its frontmatter `last-updated` line removed.

    Lets two revisions be compared for changes *other than* the date.
    """
    out: list[str] = []
    in_fm = False
    for i, line in enumerate(text.split("\n")):
        if i == 0 and line.strip() == "---":
            in_fm = True
            out.append(line)
            continue
        if in_fm and line.strip() == "---":
            in_fm = False
            out.append(line)
            continue
        if in_fm and re.match(r"^\s*last-updated\s*:", line):
            continue
        out.append(line)
    return "\n".join(out)


def bump_date_line(text: str, today: str) -> str:
    """Rewrite the frontmatter `last-updated` value, preserving indentation."""
    out: list[str] = []
    in_fm = False
    done = False
    for i, line in enumerate(text.split("\n")):
        if i == 0 and line.strip() == "---":
            in_fm = True
            out.append(line)
            continue
        if in_fm and not done and line.strip() == "---":
            in_fm = False
            out.append(line)
            continue
        if in_fm and not done:
            m = re.match(r"^(\s*)last-updated\s*:", line)
            if m:
                out.append(f"{m.group(1)}last-updated: {today}")
                done = True
                continue
        out.append(line)
    return "\n".join(out)


def has_last_updated_key(text: str) -> bool:
    """Whether the leading frontmatter carries the key, whatever its value.

    Scoped on key *presence* so a blanked-out date stays in scope and fails the
    value check, rather than being mistaken for an unmarked doc.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line.strip() == "---":
            return False
        if re.match(r"^\s*last-updated\s*:", line):
            return True
    return False


def resolve_merge_base(repo: Repo, base: str) -> str:
    """The merge-base of `base` with HEAD, with fallbacks.

    Some ephemeral CI/agent worktrees are handed over without remotes or local
    base refs. Falling back to `main` and then `HEAD^` lets the linter still
    evaluate the branch's doc edits instead of failing before it scans.
    """
    got = repo.git("merge-base", base, "HEAD", check=False)
    if got.returncode == 0:
        return got.stdout.strip()
    if base == DEFAULT_BASE:
        got = repo.git("merge-base", "main", "HEAD", check=False)
        if got.returncode == 0:
            return got.stdout.strip()
        got = repo.git("rev-parse", "--verify", "HEAD^", check=False)
        if got.returncode == 0:
            print(
                f"lint_docs: warning: could not compute merge-base against "
                f"'{base}' or 'main'; falling back to HEAD^",
                file=sys.stderr,
            )
            return got.stdout.strip()
    fail(
        f"could not compute merge-base against '{base}'; "
        f"fetch the base branch or pass --base"
    )


def rename_sources(repo: Repo, merge_base: str) -> dict[str, str]:
    """Map each renamed doc's new path to the path it had at the merge-base.

    Rename detection is on by default, so a renamed doc reaches the changed-file
    list under its *new* path, which does not exist at the merge-base. Without
    this map a relocated doc looks brand-new, and the "an added doc's date must be
    current" rule would demand a bump for a pure move that edited nothing — a
    false positive on exactly the doc-reorganization work this linter exists to
    protect. Resolving the base blob through the old path makes a rename behave
    like an edit of a pre-existing file: content-preserving moves pass, and a
    move that also rewrites the body still has to bump.
    """
    fields = [
        f
        for f in repo.git(
            "diff",
            "-M",
            "--name-status",
            "-z",
            "--diff-filter=R",
            merge_base,
            "--",
            "*.md",
            "*.mdx",
        ).stdout.split("\0")
        if f
    ]
    # `--name-status -z` emits status, old path, new path as three NUL-separated
    # fields per rename (e.g. `R100`, `a.md`, `b.md`).
    renames: dict[str, str] = {}
    i = 0
    while i + 2 < len(fields):
        if not fields[i].startswith("R"):
            break
        renames[fields[i + 2]] = fields[i + 1]
        i += 3
    return renames


def check_freshness(
    repo: Repo, clock: Clock, base: str, fix: bool
) -> tuple[list[Violation], list[str]]:
    """Enforce `last-updated:` on docs changed versus the merge-base.

    Returns (violations, fixed_paths). With `fix`, offenders are bumped and no
    violations are reported.
    """
    merge_base = resolve_merge_base(repo, base)
    renames = rename_sources(repo, merge_base)
    changed = repo.git(
        "diff",
        "--name-only",
        "-z",
        "--diff-filter=d",
        merge_base,
        "--",
        "*.md",
        "*.mdx",
    ).stdout.split("\0")

    # An untracked doc is in no diff, so without this a brand-new doc's date went
    # unchecked locally and only failed once committed. It has no base blob, so it
    # takes the added-on-this-branch path below and must carry a current date.
    untracked = {
        rel
        for rel in repo.docs
        if repo.git("ls-files", "--error-unmatch", "--", rel, check=False).returncode
        != 0
    }
    scope = sorted({p for p in changed if p} | untracked)

    violations: list[Violation] = []
    fixed: list[str] = []

    for rel in scope:
        if repo.config.ignored(rel, "freshness"):
            continue
        # A template's date is placeholder text (`YYYY-MM-DD`); bumping it to a
        # real date would corrupt the template for the next doc copied from it.
        if repo.config.is_template(rel):
            continue
        text = repo.text(rel)
        if not has_last_updated_key(text):
            continue

        head_val = repo.frontmatter(rel).get("last-updated", "")
        # A renamed doc's base blob lives under its old path. See rename_sources.
        base_path = renames.get(rel, rel)
        at_base = repo.git("cat-file", "-e", f"{merge_base}:{base_path}", check=False)
        exists_at_base = at_base.returncode == 0

        message: str | None = None
        if not is_iso_date(head_val):
            # Checked before the body-change short-circuit below, so a date-only
            # edit to a junk, blank, or impossible value still fails closed.
            message = (
                f"last-updated is not a valid ISO YYYY-MM-DD date "
                f"(got '{head_val}'); set it to {clock.today}"
            )
        elif exists_at_base and strip_date_line(
            repo.git("show", f"{merge_base}:{base_path}").stdout
        ) == strip_date_line(text):
            # Only the date changed, and it is valid. Nothing to enforce — this is
            # the deliberate escape hatch for a pure freshness touch-up.
            continue
        elif not clock.is_current(head_val):
            # The body changed (or the doc is new), so the date must be current.
            # Keying on "differs from the base value" instead would accept any
            # change at all, including a bump *backwards* to another stale date.
            what = "body changed" if exists_at_base else "doc was added on this branch"
            message = (
                f"{what} but last-updated is {head_val}, not current; "
                f"set it to {clock.today}"
            )

        if message is None:
            continue
        if fix:
            (repo.root / rel).write_text(
                bump_date_line(text, clock.today), encoding="utf-8"
            )
            repo.invalidate(rel)
            fixed.append(rel)
        else:
            violations.append(Violation("freshness", rel, message))

    return violations, fixed


# --------------------------------------------------------------------------
# links / anchors
# --------------------------------------------------------------------------

EXTERNAL_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)


def parse_link_destination(line: str, i: int) -> tuple[str, int] | None:
    """Read a link destination starting just after `(`.

    Returns (target, index after the closing `)`), or None if this is not a complete
    inline link. Handles the `<...>` form, arbitrarily nested balanced parentheses in a
    bare destination, backslash escapes, and all three title delimiters.
    """
    n = len(line)
    while i < n and line[i] in " \t":
        i += 1
    if i < n and line[i] == "<":
        close = line.find(">", i + 1)
        if close == -1:
            return None
        target, i = line[i + 1 : close], close + 1
    else:
        start, depth = i, 0
        while i < n:
            char = line[i]
            if char == "\\" and i + 1 < n:
                i += 2
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    break
                depth -= 1
            elif char in " \t":
                break
            i += 1
        target = line[start:i]
        if not target:
            return None
    while i < n and line[i] in " \t":
        i += 1
    if i < n and line[i] in "\"'(":
        closer = {'"': '"', "'": "'", "(": ")"}[line[i]]
        close = line.find(closer, i + 1)
        if close == -1:
            return None
        i = close + 1
        while i < n and line[i] in " \t":
            i += 1
    if i < n and line[i] == ")":
        return target, i + 1
    return None


def iter_links(text: str):
    """Yield (line_number, target) for inline links worth resolving.

    The label is discarded — comparing a link's label to its target is a separate
    check, deliberately out of scope here.
    """
    body = blank_noncontent(text)
    for lineno, line in enumerate(body.split("\n"), 1):
        pos = 0
        while True:
            match = LINK_LABEL_RE.search(line, pos)
            if match is None:
                break
            parsed = parse_link_destination(line, match.end())
            if parsed is None:
                # Not a complete link; resume after the label so a real one later on
                # the same line is still found.
                pos = match.end()
                continue
            target, pos = parsed
            yield lineno, target.strip()


def check_links_and_anchors(repo: Repo, enabled: frozenset[str]) -> list[Violation]:
    violations: list[Violation] = []
    for rel in repo.docs:
        text = repo.text(rel)
        if (
            "links" in enabled
            and not repo.config.ignored(rel, "links")
            and has_unterminated_comment(text)
        ):
            # Reported rather than tolerated: everything after the marker renders as
            # nothing and so drops silently out of every check below.
            violations.append(
                Violation(
                    "links",
                    rel,
                    "an HTML comment is opened but never closed; everything after "
                    "`<!--` renders as nothing and is skipped by every check",
                )
            )
        for lineno, target in iter_links(text):
            if EXTERNAL_RE.match(target):
                continue
            path_part, _, frag = target.partition("#")

            if not path_part:
                # Same-file fragment.
                if "anchors" not in enabled or repo.config.ignored(rel, "anchors"):
                    continue
                if frag and frag not in repo.anchors(rel):
                    violations.append(
                        Violation(
                            "anchors",
                            rel,
                            f"link `{target}` has no matching heading or anchor "
                            f"in this file",
                            lineno,
                        )
                    )
                continue

            if repo.is_cross_repo(path_part, rel):
                # Names another repo; unverifiable from here, by convention.
                continue

            resolved = repo.resolve(path_part, rel)
            if resolved is None:
                if "links" in enabled and not repo.config.ignored(rel, "links"):
                    violations.append(
                        Violation(
                            "links",
                            rel,
                            f"link target `{path_part}` does not resolve",
                            lineno,
                        )
                    )
                continue

            if (
                frag
                and "anchors" in enabled
                and not repo.config.ignored(rel, "anchors")
                and resolved.endswith(DOC_SUFFIXES)
                and frag not in repo.anchors(resolved)
            ):
                violations.append(
                    Violation(
                        "anchors",
                        rel,
                        f"link `{target}` resolves to {resolved}, which has no "
                        f"`#{frag}` heading or anchor",
                        lineno,
                    )
                )
    return violations


# --------------------------------------------------------------------------
# Documentation Maps
# --------------------------------------------------------------------------


@dataclass
class MapRow:
    map_path: str
    line: int
    section: str
    topic: str
    doc_cell: str
    consult_cell: str
    # Cell count when it wasn't the expected 3; None for a well-formed row.
    bad_cell_count: int | None = None


def is_map_heading(heading: str) -> bool:
    """Whether a heading opens a Documentation Map.

    Matched exactly. Sections that *document* the convention rather than carry a
    map begin with the same phrase and must not be mistaken for one — both
    `Documentation Maps` and `Documentation Map Sections` exist in the team
    conventions. A prefix test picks those up, and then the non-map tables beneath
    them are parsed as rows: a two-column table's rows are the wrong shape, and a
    three-column one would have its cells resolved as document paths.

    Every real map heading is exactly this phrase, at whatever level the file
    nests it. If one later needs a suffix, this fails loudly — the map is not
    found, so its design docs report as unmapped — rather than silently matching
    prose.
    """
    return MAP_HEADING_RE.match(heading) is not None


def find_map_files(repo: Repo) -> list[str]:
    """Docs carrying a Documentation Map.

    Discovered by heading *text*, not filename. A map does not always live in
    `AGENTS.md`: a repo may keep it in a differently-named file (for instance one
    symlinked into a parent directory under another name), so hardcoding the
    filename would miss that repo's map entirely.
    """
    out: list[str] = []
    for rel in repo.docs:
        for line in blank_noncontent(repo.text(rel), blank_spans=False).split("\n"):
            m = HEADING_RE.match(line)
            if m and is_map_heading(m.group(2)):
                out.append(rel)
                break
    return out


def is_separator_row(cells: list[str]) -> bool:
    """Whether every cell is a `---` / `:---:` alignment marker.

    Requires at least one dash per cell, so an empty cell is not mistaken for a
    separator — that mistake dropped malformed rows before their paths were ever
    checked.
    """
    return all(re.fullmatch(r":?-+:?", c.strip()) is not None for c in cells)


def split_row_cells(row_body: str) -> list[str]:
    r"""Split a table row on unescaped pipes only.

    A `\|` inside a cell is a literal pipe, not a delimiter. Splitting naively
    yields an extra cell, and since a row whose cell count is unexpected is
    skipped, the row's cited path would go unvalidated — the row silently opts out
    of the check rather than failing it.
    """
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for ch in row_body:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            # Keep the backslash: cells are compared as written (a separator row
            # is detected by its character set) and only the split is at issue.
            current.append(ch)
            escaped = True
        elif ch == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    cells.append("".join(current).strip())
    return cells


def parse_map_rows(repo: Repo, map_rel: str) -> list[MapRow]:
    """Rows of every Documentation Map table in a file.

    A map's heading level is not fixed and both nestings are correct: one file
    puts `## Documentation Map` above `###` sections, another `#` above `##`. So a
    map is located by heading *text*, and its sections by "any deeper heading" —
    never by an absolute level. Keying on level instead drops every section of a
    differently-nested map, which measured as 81 docs falsely reported unmapped.
    """
    rows: list[MapRow] = []
    in_map = False
    map_level = 0
    section = ""
    for lineno, line in enumerate(
        blank_noncontent(repo.text(map_rel), blank_spans=False).split("\n"), 1
    ):
        m = HEADING_RE.match(line)
        if m:
            level, heading = len(m.group(1)), m.group(2)
            if is_map_heading(heading):
                in_map, map_level, section = True, level, ""
            elif in_map and level <= map_level:
                in_map = False
            elif in_map:
                section = heading
            continue
        if not in_map:
            continue
        rm = TABLE_ROW_RE.match(line)
        if not rm:
            continue
        cells = split_row_cells(rm.group(1))
        if len(cells) != 3:
            # Reported, not skipped. Discarding the row silently means its cited
            # path is never resolved, so a stray unescaped `|` turns a broken row
            # into a passing one — and outside `design-docs/` there is no
            # unmapped-doc backstop to catch the document another way.
            rows.append(MapRow(map_rel, lineno, section, "", "", "", len(cells)))
            continue
        topic, doc_cell, consult = cells
        # Skip the header row and the `|---|---|---|` separator — and no more than
        # those. Testing the document cell with `set(cell) <= set("-: ")` also
        # matched an *empty* cell, so `| Broken |  | why |` was dropped as though it
        # were the separator and its missing path went unreported. A row with an
        # empty document cell now falls through to the citation check below.
        if topic == "Topic" or is_separator_row(cells):
            continue
        rows.append(MapRow(map_rel, lineno, section, topic, doc_cell, consult))
    return rows


def row_cited_path(row: MapRow) -> str | None:
    m = re.search(r"`([^`]+)`", row.doc_cell)
    return m.group(1) if m else None


# The canonical map section names. Compared exactly (after whitespace
# normalization) rather than by substring: a containment test accepted
# `Not Active Design Docs` and `Not Historical Design Docs`, so a malformed heading
# satisfied status routing and a doc mapped there passed.
ACTIVE_SECTION = "Active Design Docs"
HISTORICAL_SECTION = "Historical & Deprecated Design Docs"


def normalize_section(section: str) -> str:
    return " ".join(section.split())


def is_active_section(section: str) -> bool:
    return normalize_section(section) == ACTIVE_SECTION


def is_historical_section(section: str) -> bool:
    return normalize_section(section) == HISTORICAL_SECTION


def check_maps(
    repo: Repo, enabled: frozenset[str]
) -> tuple[list[Violation], dict[str, set[str]]]:
    """Run the map checks.

    Returns (violations, {doc path: the map files citing it}). The citing maps are
    kept, not just the fact of being cited, so `check_unmapped` can tell a doc mapped
    by its *own* project from one only cross-referenced from elsewhere.
    """
    violations: list[Violation] = []
    mapped: dict[str, set[str]] = {}

    for map_rel in find_map_files(repo):
        # A map section may legitimately hold no table — a repo whose real map
        # lives elsewhere keeps the heading plus a pointer to it — so finding zero
        # rows here is not an error.
        for row in parse_map_rows(repo, map_rel):
            if row.bad_cell_count is not None:
                if "map_paths" in enabled and not repo.config.ignored(
                    map_rel, "map_paths"
                ):
                    violations.append(
                        Violation(
                            "map_paths",
                            map_rel,
                            f"map row has {row.bad_cell_count} columns, expected 3 "
                            f"(Topic | Document | Consult when...); escape any "
                            f"literal `|` in a cell as `\\|`",
                            row.line,
                        )
                    )
                continue

            if "map_cells" in enabled and not repo.config.ignored(map_rel, "map_cells"):
                violations.extend(check_map_cell(repo, row))

            cite = row_cited_path(row)
            if cite is None:
                # Reported, not skipped. A Document cell that lost its backticks
                # (or uses a link instead) yields no citation, and skipping meant
                # the row's target was never resolved — so a row pointing at a
                # nonexistent doc passed. One canonical form keeps that
                # unambiguous; a row needing another is a convention change.
                if "map_paths" in enabled and not repo.config.ignored(
                    map_rel, "map_paths"
                ):
                    violations.append(
                        Violation(
                            "map_paths",
                            map_rel,
                            f"map row '{row.topic}' has no backticked document "
                            f"path in its Document cell",
                            row.line,
                        )
                    )
                continue
            if repo.is_cross_repo(cite, map_rel):
                continue
            resolved = repo.resolve(cite, map_rel, require_doc=True)
            if resolved is None:
                if "map_paths" in enabled and not repo.config.ignored(
                    map_rel, "map_paths"
                ):
                    violations.append(
                        Violation(
                            "map_paths",
                            map_rel,
                            f"map row '{row.topic}' cites `{cite}`, which does "
                            f"not resolve",
                            row.line,
                        )
                    )
                continue
            mapped.setdefault(resolved, set()).add(map_rel)

            if "map_status_section" in enabled and not repo.config.ignored(
                map_rel, "map_status_section"
            ):
                violations.extend(check_row_status_section(repo, row, resolved))

    return violations, mapped


def check_map_cell(repo: Repo, row: MapRow) -> list[Violation]:
    """The "Consult when..." cell is a routing trigger, not a summary."""
    cap = repo.config.consult_cell_chars
    if "<br" in row.consult_cell.lower():
        return [
            Violation(
                "map_cells",
                row.map_path,
                f"map row '{row.topic}' has a multi-line Consult-when cell "
                f"(contains <br>); keep it to one line and put detail in the doc",
                row.line,
            )
        ]
    if len(row.consult_cell) > cap:
        return [
            Violation(
                "map_cells",
                row.map_path,
                f"map row '{row.topic}' has a {len(row.consult_cell)}-char "
                f"Consult-when cell (limit {cap}); it decides whether to open "
                f"the doc — move detail into the doc",
                row.line,
            )
        ]
    return []


def check_row_status_section(repo: Repo, row: MapRow, resolved: str) -> list[Violation]:
    """A design doc's map section follows its `status:` frontmatter."""
    if "/design-docs/" not in f"/{resolved}":
        return []
    if repo.config.is_template(resolved):
        # `status: active` here is placeholder text, not a live plan.
        return []
    status = repo.frontmatter(resolved).get("status", "").strip()
    if not status:
        return [
            Violation(
                "map_status_section",
                resolved,
                "design doc has no `status:` frontmatter, so its map section "
                "cannot be checked",
            )
        ]
    if status not in DESIGN_DOC_STATUSES:
        # Reported here as well as by `frontmatter`, deliberately: this check
        # routes on the value, so if an unrecognized one fell through to the
        # returns below it would report success for every section. Each check has
        # to fail closed on its own, since either can be disabled independently.
        return [
            Violation(
                "map_status_section",
                resolved,
                f"design doc status '{status}' is not one of "
                f"{', '.join(DESIGN_DOC_STATUSES)}, so its map section cannot "
                f"be checked",
            )
        ]
    if status in ("proposed", "active") and not is_active_section(row.section):
        return [
            Violation(
                "map_status_section",
                row.map_path,
                f"`{resolved}` is status={status} but is mapped under "
                f"'{row.section}'; it belongs under Active Design Docs",
                row.line,
            )
        ]
    if status in ("completed", "deprecated") and not is_historical_section(row.section):
        return [
            Violation(
                "map_status_section",
                row.map_path,
                f"`{resolved}` is status={status} but is mapped under "
                f"'{row.section}'; it belongs under Historical & Deprecated "
                f"Design Docs",
                row.line,
            )
        ]
    return []


def owning_project(rel: str, project_roots: tuple[str, ...]) -> str:
    """The deepest project root containing `rel`, or "" for a repo-level path."""
    best = ""
    for root in project_roots:
        if rel.startswith(root + "/") and len(root) > len(best):
            best = root
    return best


def check_unmapped(repo: Repo, mapped: dict[str, set[str]]) -> list[Violation]:
    """Fail on any doc under a doc root that no map routes to.

    Adding a doc means adding its map row in the same change: the map is the only
    route agents have to it, and for a design doc `status:` sorts it into the
    Historical section rather than out of the table. An unmapped doc is unreachable,
    and reporting that as a warning let one merge anyway, since a warnings-only run
    still exits 0.

    Scoped to design docs, this missed a new `architecture/`, `references/`, or
    `guides/` doc entirely — those have no other backstop at all, whereas a design
    doc at least also goes through the status/section check.

    Two exemptions: `generated/`, which the conventions explicitly do not map since
    it is regenerated rather than authored, and a configured template, whose
    frontmatter is placeholder text.
    """
    out: list[Violation] = []
    for rel in repo.docs:
        if not under_doc_root(repo, rel):
            continue
        if "/generated/" in f"/{rel}":
            continue
        if repo.config.is_template(rel):
            continue
        if repo.config.ignored(rel, "map_paths"):
            continue
        kind = "design doc" if "/design-docs/" in f"/{rel}" else "doc"
        citing = mapped.get(rel, set())
        if not citing:
            out.append(
                Violation(
                    "map_paths",
                    rel,
                    f"{kind} has no Documentation Map row; agents surface docs "
                    f"only through the maps",
                )
            )
            continue
        # Being cited *somewhere* is not enough. A doc is owned by the map at its own
        # level — a project's docs by a map inside that project, repo-level docs by a
        # repo-level map — so one listed only at the other level is undiscoverable to
        # an agent consulting the map responsible for it. Extra cross-references are
        # fine and common in both directions: project maps legitimately mirror
        # repo-level rows, and the root map cross-references project docs.
        project = owning_project(rel, repo.project_roots)
        if project:
            owned = any(m.startswith(project + "/") for m in citing)
            where = f"a map inside `{project}/`"
        else:
            owned = any(not owning_project(m, repo.project_roots) for m in citing)
            where = "a repo-level map"
        if not owned:
            out.append(
                Violation(
                    "map_paths",
                    rel,
                    f"{kind} is mapped only from {', '.join(sorted(citing))}; "
                    f"it needs a row in {where}",
                )
            )
    return out


# --------------------------------------------------------------------------
# frontmatter
# --------------------------------------------------------------------------


DESIGN_DOC_STATUSES = ("proposed", "active", "completed", "deprecated")


def required_fields(rel: str) -> tuple[str, ...]:
    """Fields the per-directory frontmatter tables require.

    `superseded-by` is deliberately absent for a deprecated design doc: the
    conventions require it only when a replacement actually exists, and explicitly
    allow a pure decision record to be deprecated without a destination. Demanding
    it unconditionally would fail that supported case on every run, pushing authors
    to invent a successor or bypass the check. Its value is still validated when
    present — see check_superseded_by.
    """
    slashed = f"/{rel}"
    if "/generated/" in slashed:
        return ("title", "last-updated", "generated")
    if "/design-docs/" in slashed:
        return ("title", "status", "created", "last-updated")
    return ("title", "last-updated")


def under_doc_root(repo: Repo, rel: str) -> bool:
    return any(rel.startswith(root + "/") for root in repo.doc_roots)


def check_frontmatter(repo: Repo) -> list[Violation]:
    violations: list[Violation] = []
    for rel in repo.docs:
        if not under_doc_root(repo, rel):
            # `AGENTS.md` / `README.md` carry no frontmatter by convention.
            continue
        if repo.config.ignored(rel, "frontmatter"):
            continue
        if has_unclosed_frontmatter(repo.text(rel)):
            violations.append(
                Violation(
                    "frontmatter",
                    rel,
                    "frontmatter block is opened but never closed; add the "
                    "closing `---`",
                )
            )
            # Every field check below reads a block that parsed to nothing, so
            # they would pile "missing title" onto the real cause.
            continue

        fm = repo.frontmatter(rel)
        required = required_fields(rel)
        missing = [k for k in required if k not in fm]
        if missing:
            violations.append(
                Violation(
                    "frontmatter",
                    rel,
                    f"frontmatter is missing required field(s): {', '.join(missing)}",
                )
            )
        # A required key present with an empty value satisfies nothing the field
        # exists for, so it is a violation rather than a pass.
        blank = [k for k in required if k in fm and not fm[k].strip()]
        if blank:
            violations.append(
                Violation(
                    "frontmatter",
                    rel,
                    f"frontmatter field(s) present but empty: {', '.join(blank)}",
                )
            )
        # The tables define these as ISO dates, and checking only that they are
        # populated accepted `created: yesterday`. `freshness` validates
        # `last-updated` too, but only for docs in this branch's diff — a doc
        # nobody touched keeps whatever it has, so the value is checked here as
        # well.
        is_template = repo.config.is_template(rel)
        for key in ("created", "last-updated"):
            value = fm.get(key, "").strip()
            if not is_template and value and not is_iso_date(value):
                violations.append(
                    Violation(
                        "frontmatter",
                        rel,
                        f"`{key}` is not a valid ISO YYYY-MM-DD date (got '{value}')",
                    )
                )

        status = fm.get("status", "").strip()
        if (
            "/design-docs/" in f"/{rel}"
            and status
            and status not in DESIGN_DOC_STATUSES
        ):
            violations.append(
                Violation(
                    "frontmatter",
                    rel,
                    f"design doc status must be one of "
                    f"{', '.join(DESIGN_DOC_STATUSES)} (got '{status}')",
                )
            )
        if "/generated/" in f"/{rel}" and fm.get("generated") not in (None, "true"):
            violations.append(
                Violation(
                    "frontmatter",
                    rel,
                    f"generated doc must declare `generated: true` "
                    f"(got '{fm.get('generated')}')",
                )
            )
    return violations


def check_superseded_by(repo: Repo) -> list[Violation]:
    """`superseded-by:` names where the live answer moved — it has to resolve."""
    violations: list[Violation] = []
    for rel in repo.docs:
        if not under_doc_root(repo, rel):
            continue
        if repo.config.ignored(rel, "map_paths"):
            continue
        fm = repo.frontmatter(rel)
        if "superseded-by" not in fm:
            # Omitting it is legitimate — a doc deprecated with no successor.
            continue
        target = fm["superseded-by"].strip()
        if not target:
            # Present but blank claims a successor exists and then names none, so
            # it routes the reader nowhere. Omit the field or give it a target.
            violations.append(
                Violation(
                    "map_paths",
                    rel,
                    "`superseded-by:` is present but empty; give it the "
                    "replacement doc's path or omit the field",
                )
            )
            continue
        if EXTERNAL_RE.match(target) or repo.is_cross_repo(target, rel):
            # A successor in another repo is the documented case for a doc
            # deprecated because the live answer moved out of this tree.
            continue
        if repo.resolve(target, rel, require_file=True) is None:
            violations.append(
                Violation(
                    "map_paths",
                    rel,
                    f"`superseded-by: {target}` does not resolve",
                )
            )
    return violations


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

ALL_CHECKS = (
    "freshness",
    "links",
    "anchors",
    "map_paths",
    "map_status_section",
    "map_cells",
    "frontmatter",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/lint_docs.py",
        description=(
            "Documentation linter — link/anchor resolution, Documentation Map "
            "rows, frontmatter, and `last-updated:` freshness. Runnable from "
            "anywhere inside the repo."
        ),
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="bump last-updated on offenders (applies to the freshness check only)",
    )
    parser.add_argument(
        "--base",
        default=DEFAULT_BASE,
        metavar="REF",
        help=f"base ref for the freshness merge-base (default: {DEFAULT_BASE})",
    )
    parser.add_argument(
        "--check",
        action="append",
        dest="checks",
        metavar="NAME",
        choices=ALL_CHECKS,
        help="run only this check; repeatable",
    )
    parser.add_argument(
        "--list-checks",
        action="store_true",
        help="list check names and whether config enables them, then exit",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help=(
            "config file (default: lint_docs.toml beside this script). Lets the "
            "test suite drive a fixture repo without inheriting this repo's config"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    config_path = (
        Path(args.config)
        if args.config
        else Path(__file__).resolve().parent / "lint_docs.toml"
    )
    if args.config and not config_path.exists():
        fail(f"config file not found: {config_path}")
    config = load_config(config_path, ALL_CHECKS)

    if args.list_checks:
        for name in ALL_CHECKS:
            state = "enabled" if name in config.enabled else "disabled"
            print(f"{name}: {state}")
        return 0

    enabled = config.enabled
    if args.checks:
        requested = frozenset(args.checks)
        # An explicit --check overrides config, so a disabled check can still be
        # exercised deliberately (which is how a new rule gets swept).
        enabled = requested
    if not enabled:
        print("lint_docs: no checks enabled; nothing to do.", file=sys.stderr)
        return 0

    repo = discover_repo(config)
    clock = Clock.build()

    violations: list[Violation] = []
    fixed: list[str] = []

    if "freshness" in enabled:
        fresh_violations, fixed = check_freshness(repo, clock, args.base, args.fix)
        violations.extend(fresh_violations)

    if enabled & {"links", "anchors"}:
        violations.extend(check_links_and_anchors(repo, enabled))

    if enabled & {"map_paths", "map_status_section", "map_cells"}:
        map_violations, mapped = check_maps(repo, enabled)
        violations.extend(map_violations)
        if "map_paths" in enabled:
            violations.extend(check_unmapped(repo, mapped))
            violations.extend(check_superseded_by(repo))

    if "frontmatter" in enabled:
        violations.extend(check_frontmatter(repo))

    if args.fix:
        if fixed:
            print(
                f"lint_docs: bumped last-updated on {len(fixed)} doc(s):",
                file=sys.stderr,
            )
            for rel in fixed:
                print(f"  {rel}", file=sys.stderr)
        else:
            print("lint_docs: no docs needed a last-updated bump.", file=sys.stderr)

    warnings = [v for v in violations if v.warning]
    errors = [v for v in violations if not v.warning]

    if warnings:
        print(f"lint_docs: {len(warnings)} warning(s):", file=sys.stderr)
        for v in sorted(warnings, key=lambda v: (v.path, v.line or 0)):
            print(v.render(), file=sys.stderr)

    if errors:
        print(f"lint_docs: {len(errors)} violation(s):", file=sys.stderr)
        for v in sorted(errors, key=lambda v: (v.path, v.line or 0)):
            print(v.render(), file=sys.stderr)
        if any(v.check == "freshness" for v in errors):
            print("Bump the date, or run scripts/lint_docs.py --fix.", file=sys.stderr)
        return 1

    if not args.fix:
        print("lint_docs: all documentation checks passed.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
