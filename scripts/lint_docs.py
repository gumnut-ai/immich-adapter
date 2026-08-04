#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["markdown-it-py>=3,<5"]
#
# [tool.uv]
# # The same supply-chain cooldown the test project carries: without it `uv run`
# # resolves fresh on every CI run, skipping the cooldown and executing a version
# # the suite never saw. `lint_docs.py.lock` pins it; CI runs `--locked`.
# exclude-newer = "14 days"
# ///
"""scripts/lint_docs.py

Documentation linter. Enforces the machine-checkable half of this repo's
documentation conventions; its `AGENTS.md` names where they live and which checks
run here. Deliberately no path to that doc: this file is byte-identical across
repos, so any repo-specific path in it is wrong in at least one of them.

Checks (each independently switchable in `lint_docs.toml`, so a new rule can
land dark and be enabled once its sweep is done):

  freshness           `last-updated:` is recent on an edited doc or a doc added
                      on this branch
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

Markdown is parsed by `markdown-it-py`, declared in the PEP 723 block above and
resolved by `uv run` — so this stays one file with no project to install, while
the "is this real content or an example of markup?" question is answered by a
CommonMark implementation instead of by hand. That question was the linter's
entire bug surface: four rounds of review each found a real spec deviation and
uncovered the next one behind it. The rules below are ours; the parsing is not.

Repo layout differences live in `lint_docs.toml`, which keeps this file identical
across the Gumnut repos that use it.

Usage (runnable from anywhere inside the repo):
  uv run scripts/lint_docs.py                 # check; non-zero exit on violations
  uv run scripts/lint_docs.py --fix           # bump last-updated on offenders
  uv run scripts/lint_docs.py --base origin/main
  uv run scripts/lint_docs.py --check freshness --check links
  uv run scripts/lint_docs.py --list-checks
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
from datetime import date, timedelta
from pathlib import Path
from typing import NoReturn
from urllib.parse import unquote as percent_decode

try:
    from markdown_it import MarkdownIt
    from markdown_it.rules_inline import autolink, image, link
except ModuleNotFoundError:  # pragma: no cover - exercised by running without uv
    # Inlined rather than routed through `fail()`, which is not defined yet at
    # import time. Same output shape, so the message reads like every other one.
    print(
        "lint_docs: markdown-it-py is not installed. Run this as "
        "`uv run scripts/lint_docs.py` — the dependency is declared in the "
        "script's own PEP 723 header, so uv resolves it with no project to "
        "install.",
        file=sys.stderr,
    )
    raise SystemExit(1) from None

DEFAULT_BASE = "origin/main"
DEFAULT_FRESHNESS_WINDOW_DAYS = 7
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
    freshness_window_days: int = DEFAULT_FRESHNESS_WINDOW_DAYS
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
CONFIG_LIMIT_KEYS = frozenset({"consult_cell_chars", "freshness_window_days"})
CONFIG_IGNORE_KEYS = frozenset({"path", "checks", "reason"})


def config_str_list(path: Path, raw: dict, key: str, default: tuple[str, ...]) -> tuple:
    """Read a list-of-strings setting, rejecting anything else.

    A bare string is the trap: `strip_prefixes = "repo-root "` is iterable, so it
    silently became ten one-character prefixes and mangled every citation, with no
    error. Quoting a single value instead of bracketing it reads natural in TOML.
    """
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, str):
        # Named separately from the general type error: this is the likely mistake,
        # and the fix is to add brackets rather than to change the value.
        fail(
            f"{path}: `{key}` must be a list of strings, not a bare string — "
            f'a single value still needs brackets: {key} = ["{value}"]'
        )
    if not isinstance(value, list):
        fail(f"{path}: `{key}` must be a list of strings, got {type(value).__name__}")
    bad = [item for item in value if not isinstance(item, str)]
    if bad:
        fail(f"{path}: `{key}` must contain only strings; got {bad[0]!r}")
    return tuple(value)


def config_positive_int(path: Path, raw: dict, key: str, default: int) -> int:
    """Read an integer limit, rejecting anything else.

    A mistyped value otherwise raised a bare `ValueError` out of `int()` — a
    traceback reads as the tool being broken rather than the config being wrong.
    `bool` is rejected explicitly, being an `int` subclass, so `= true` would mean 1.
    """
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        fail(
            f"{path}: `{key}` must be an integer, got {type(value).__name__} "
            f"({value!r})"
        )
    if value <= 0:
        fail(f"{path}: `{key}` must be greater than 0, got {value}")
    return value


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
        strip_prefixes=config_str_list(path, raw, "strip_prefixes", ("repo-root ",)),
        self_prefixes=config_str_list(path, raw, "self_prefixes", ()),
        cross_repo_prefixes=config_str_list(
            path, raw, "cross_repo_prefixes", ("gumnut-ai/",)
        ),
        template_paths=config_str_list(path, raw, "template_paths", ()),
        enabled=enabled,
        consult_cell_chars=config_positive_int(path, limits, "consult_cell_chars", 250),
        freshness_window_days=config_positive_int(
            path,
            limits,
            "freshness_window_days",
            DEFAULT_FRESHNESS_WINDOW_DAYS,
        ),
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

# `js-default` is CommonMark plus GFM tables and strikethrough, matching upstream
# markdown-it's default — tables are needed for the Documentation Map checks.
#
# `html=True` is load-bearing and **not** the default: with it off, `<!--` is not
# treated as HTML at all, so a commented-out heading or link stays live and every
# comment-related check silently inverts.
MD = MarkdownIt("js-default", {"html": True})

# Inline tokens carry no source position, which is the one thing a linter needs
# most — a violation has to name a line. Rather than reconstruct it by counting
# `](` delimiters in the block source (which needs a separate rule for images,
# autolinks, escaped delimiters, and reference links, each wrong in its own way),
# ask the parser: an inline rule runs with `state.pos` at the construct's start,
# so wrapping the three rules that emit link-ish tokens records it directly.
SRC_POS = "src_pos"


def stamp_source_position(rule):
    """Wrap an inline rule so the tokens it emits carry their source offset."""

    def wrapped(state, silent):
        start = state.pos
        first_new = len(state.tokens)
        matched = rule(state, silent)
        if matched and not silent:
            for token in state.tokens[first_new:]:
                token.meta.setdefault(SRC_POS, start)
        return matched

    return wrapped


for _rule_name, _rule in (("link", link), ("image", image), ("autolink", autolink)):
    MD.inline.ruler.at(_rule_name, stamp_source_position(_rule))

HTML_ANCHOR_TAG_RE = re.compile(r"<a\s[^>]*>", re.IGNORECASE | re.DOTALL)
# `(?<![\w-])`, not `\b`: a hyphen is a non-word character, so `\b` matched the tail
# of `data-id` / `aria-name` and recorded their values as fragment anchors — making a
# link to a nonexistent fragment pass. Worse, in `<a data-id="x" id="t">` the first
# match was `x`, so the *real* anchor was missed too.
HTML_ANCHOR_ATTR_RE = re.compile(
    r"(?<![\w-])(?:id|name)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
)
# Exact match, so a heading that merely *starts* with the phrase is not read as a
# map. Prose sections about the convention do (`Documentation Maps`,
# `Documentation Map Sections`), and their non-map tables would be parsed as rows.
# See is_map_heading.
MAP_HEADING_RE = re.compile(r"^Documentation Map$")


def blank_frontmatter(text: str) -> str:
    """Replace a leading frontmatter block with blank lines, keeping the line count.

    Frontmatter is YAML, not Markdown, and must not reach the parser: its closing
    `---` makes the block a **setext heading** (a phantom anchor on 215 of 295
    docs here), and a `#` line inside it is a comment, not an H1.

    Blanking rather than dropping keeps token line numbers equal to file line
    numbers. An unterminated block is left alone — `check_frontmatter` reports
    that, and blanking to EOF would hide the doc from every other check.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return text
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return "\n".join([""] * (i + 1) + lines[i + 1 :])
    return text


def parse_markdown(text: str):
    """Token stream for a document, with frontmatter neutralized."""
    return MD.parse(blank_frontmatter(text))


def heading_text(inline_token) -> str:
    """The visible text of a heading, as GitHub would slug it.

    Built from the children, not the raw source, so markup disappears for free:
    emphasis and links are separate tokens and a code span yields its content
    without backticks. `html_inline` and `image` contribute nothing, matching
    GitHub, which slugs an element's text content — alt text is an attribute.
    A heading that is entirely raw HTML therefore yields "", which
    `collect_anchors` drops.
    """
    parts: list[str] = []
    for child in inline_token.children or []:
        if child.type in ("text", "code_inline"):
            parts.append(child.content)
        elif child.type == "softbreak":
            # Headings take no hard break, but a wrapped table cell reuses this.
            parts.append(" ")
    return "".join(parts)


def split_html_comments(chunk: str) -> tuple[str, bool]:
    """(chunk without its comment spans, whether one is left open).

    A comment is itself an HTML block, so `<!-- <a id="x"></a> -->` arrives as one
    `html_block` containing the tag; scanning it raw made a commented-out anchor
    real. An unterminated `<!--` is dropped to the end of the chunk, since per
    CommonMark it runs to the end of the document.

    Both answers need a full scan, not a prefix test: the opener need not start
    the block (`<div>` then `<!-- draft` is one block beginning with `<div>`).
    """
    out: list[str] = []
    i = 0
    while True:
        start = chunk.find("<!--", i)
        if start == -1:
            out.append(chunk[i:])
            return "".join(out), False
        out.append(chunk[i:start])
        end = chunk.find("-->", start + 4)
        if end == -1:
            return "".join(out), True
        i = end + 3


def iter_html_chunks(tokens):
    """Every rendered raw-HTML run, block-level and inline, comment spans removed."""
    for token in tokens:
        if token.type == "html_block":
            yield split_html_comments(token.content)[0]
        elif token.type == "inline":
            for child in token.children or []:
                if child.type == "html_inline":
                    yield split_html_comments(child.content)[0]


def has_unterminated_comment(tokens) -> bool:
    """Whether a block-level `<!--` opens a comment that never closes.

    Such a block runs to the end of the document, so everything after it renders
    as nothing and silently leaves every check — hence reporting the marker.

    Block form only: an unterminated `<!--` mid-prose is an incomplete *inline*
    candidate that renders literally and hides nothing.
    """
    return any(
        token.type == "html_block" and split_html_comments(token.content)[1]
        for token in tokens
    )


def slugify_heading(heading: str) -> str:
    """GitHub's heading-anchor slug, given a heading's *rendered text*.

    Takes `heading_text` output, where markup is already gone, and must not undo
    markup a second time: link syntax reaching here is *literal*, and stripping it
    turned `# \\[foo\\]\\(bar\\)` into `foo` where GitHub renders `foobar`.

    Two details produce confident false positives if missed, both found by running
    over the real tree:

    * whitespace runs do NOT collapse — `## Migration & Rollout Plan` becomes
      `migration--rollout-plan`, the `&` dropped and both spaces kept.
    * `_` survives, being a word character (`asset_metadata.raw_width`).
    """
    s = heading.strip().lower()
    # This also covers any stray `*`, backtick, or `~` left as literal text.
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    return s.strip().replace(" ", "-")


def anchors_in(text: str) -> set[str]:
    """Anchors for a document's source. Convenience over parse + collect."""
    return collect_anchors(parse_markdown(text))


def collect_anchors(tokens) -> set[str]:
    """Every fragment a `#...` link in this file could target.

    Headings come from the token stream, so a `#` inside a fenced block, a code
    span, or a comment is not one — that distinction was the previous scanner's
    entire job, and the reason it needed two parallel renderings of the same text.
    """
    anchors: set[str] = set()

    # Explicit anchors, from raw-HTML tokens only, so an `<a id="fake">` shown as
    # a code example or inside a comment defines nothing.
    for chunk in iter_html_chunks(tokens):
        for tag in HTML_ANCHOR_TAG_RE.findall(chunk):
            # Every id/name in the tag: `<a name="old" id="new">` defines both.
            for attr in HTML_ANCHOR_ATTR_RE.finditer(tag):
                anchors.add(attr.group(1))

    # GitHub disambiguates a repeated slug with -1, -2, ..., and the suffix must land
    # on an id nothing else holds. Counting per base collides with a *literal* heading
    # of the suffixed name: `Foo`, `Foo`, `Foo-1` are `foo`, `foo-1`, `foo-1-1`, but
    # per-base counting emits `foo-1` twice. Slugs are ids — dedupe against the set.
    used: set[str] = set()
    for i, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        base = slugify_heading(heading_text(tokens[i + 1]))
        if not base:
            # No sluggable text (`# <Title>`) means no anchor on GitHub either, and
            # adding "" would also consume a dedup slot.
            continue
        slug, n = base, 0
        while slug in used:
            n += 1
            slug = f"{base}-{n}"
        used.add(slug)
        anchors.add(slug)
    return anchors


def frontmatter_end(lines: list[str]) -> int | None:
    """Index of the frontmatter block's closing `---`, or None if there is none.

    The one place the delimiter rule lives, so "what counts as frontmatter" cannot
    drift between the parser and the checks that rewrite dates inside it.
    """
    if not lines or lines[0].strip() != "---":
        return None
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return i
    return None


def parse_frontmatter(text: str) -> dict[str, str | list[str]]:
    """Parse the leading YAML frontmatter block.

    Only the subset the conventions use: `key: value`, plus block and flow
    sequences. A body mention of a key (e.g. a doc documenting the convention) is
    not frontmatter, which is why parsing stops at the closing delimiter.

    A sequence value stays a `list`, so its **shape** survives. Every field the
    conventions define holds a single value, so `check_frontmatter` rejects a list
    for any of them — inferring that from a *value* check instead only covered the
    fields that happen to have one, letting a list-valued `title` pass, and
    flattening a one-item sequence to a string made it indistinguishable from a
    scalar. A key the conventions do not define is free to be a list.

    An **unterminated** block yields no fields: no renderer reads a truncated doc
    as having frontmatter, and `has_unclosed_frontmatter` reports the missing
    delimiter as its own violation rather than a pile of missing fields.
    """
    lines = text.split("\n")
    end = frontmatter_end(lines)
    if end is None:
        return {}
    fields: dict[str, str | list[str]] = {}
    last_key: str | None = None
    for line in lines[1:end]:
        m = re.match(r"^\s*([A-Za-z0-9_-]+)\s*:(.*)$", line)
        if m:
            key = last_key = m.group(1)
            raw = m.group(2).strip()
            # A flow sequence is the same shape as a block one. Tested on the raw
            # value, since a *quoted* `"[x]"` is a string that looks like a list.
            if raw.startswith("[") and raw.endswith("]"):
                inner = raw[1:-1].strip()
                fields[key] = (
                    [unquote(v) for v in inner.split(",") if v.strip()] if inner else []
                )
            else:
                fields[key] = unquote(m.group(2))
            continue
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item and last_key is not None:
            # A block-sequence item continues the preceding key. Without this a
            # list-valued field read as the empty string, failing a doc on a shape
            # YAML allows.
            existing = fields.get(last_key)
            if not isinstance(existing, list):
                existing = []
                fields[last_key] = existing
            existing.append(unquote(item.group(1)))
    return fields


def field_text(value: str | list[str]) -> str:
    """A frontmatter value rendered for a violation message."""
    return value if isinstance(value, str) else "[" + ", ".join(value) + "]"


def has_unclosed_frontmatter(text: str) -> bool:
    """Whether a doc opens a frontmatter block and never closes it."""
    lines = text.split("\n")
    return bool(lines) and lines[0].strip() == "---" and frontmatter_end(lines) is None


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
    _frontmatter: dict[str, dict[str, str | list[str]]] = field(default_factory=dict)
    _tokens: dict[str, list] = field(default_factory=dict)
    tracked: frozenset[str] = frozenset()

    def text(self, rel: str) -> str:
        if rel not in self._text:
            try:
                self._text[rel] = (self.root / rel).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                self._text[rel] = ""
        return self._text[rel]

    def anchors(self, rel: str) -> set[str]:
        if rel not in self._anchors:
            self._anchors[rel] = collect_anchors(self.tokens(rel))
        return self._anchors[rel]

    def tokens(self, rel: str) -> list:
        """The document's token stream, parsed once per run.

        Links, anchors, and map rows all walk the same tokens, and each used to
        re-parse the file — 391 parses for 180 documents. Caching them took this
        repo's run from 3.1s to 1.0s.

        The cost is retention: the checks are sequential repo-wide passes, so the
        saving comes precisely from holding every document's tokens across them,
        at ~430KB each (peak RSS 48MB to 126MB here). That is linear in doc count.
        If a repo ever grows large enough for that to matter, the fix is to invert
        the loops — one pass over documents running every check — not to make this
        cache cleverer.
        """
        if rel not in self._tokens:
            self._tokens[rel] = parse_markdown(self.text(rel))
        return self._tokens[rel]

    def frontmatter(self, rel: str) -> dict[str, str | list[str]]:
        if rel not in self._frontmatter:
            self._frontmatter[rel] = parse_frontmatter(self.text(rel))
        return self._frontmatter[rel]

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
            try:
                candidate = (self.root / base / cite).resolve()
            except (ValueError, OSError):
                # A path the filesystem cannot even be asked about is unresolvable,
                # not a crash. `[x](foo%00bar.md)` decodes to an embedded NUL and
                # `resolve()` raises `ValueError`, which took down the whole run
                # with a traceback instead of reporting one broken link.
                return None

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
        self._frontmatter.pop(rel, None)
        self._tokens.pop(rel, None)


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
        tracked=frozenset(tracked),
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
    earliest_fresh_date: date
    latest_fresh_date: date
    freshness_window_days: int

    @classmethod
    def build(cls, freshness_window_days: int) -> Clock:
        # Tests pin the clock so a fixture and this process cannot straddle a
        # date boundary. Normal invocations use the system clock.
        now = int(os.environ.get("LINT_DOCS_NOW_EPOCH") or time.time())
        today = os.environ.get("LINT_DOCS_TODAY") or time.strftime(
            "%Y-%m-%d", time.localtime(now)
        )
        earliest_current_date = date.fromisoformat(
            time.strftime("%Y-%m-%d", time.gmtime(now - TZ_WINDOW_EARLIEST_SECONDS))
        )
        return cls(
            today=today,
            earliest_fresh_date=earliest_current_date
            - timedelta(days=freshness_window_days),
            latest_fresh_date=date.fromisoformat(
                time.strftime("%Y-%m-%d", time.gmtime(now + TZ_WINDOW_LATEST_SECONDS))
            ),
            freshness_window_days=freshness_window_days,
        )

    def is_fresh(self, value: str) -> bool:
        parsed = date.fromisoformat(value)
        return self.earliest_fresh_date <= parsed <= self.latest_fresh_date


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
    # takes the added-on-this-branch path below and must carry a recent date.
    untracked = {rel for rel in repo.docs if rel not in repo.tracked}
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

        head_val = field_text(repo.frontmatter(rel).get("last-updated", ""))
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
        elif not clock.is_fresh(head_val):
            # The body changed (or the doc is new), so the date must be within
            # the tolerance window. Keying on "differs from the base value"
            # instead would accept any change at all, including a bump backwards
            # to another stale date.
            what = "body changed" if exists_at_base else "doc was added on this branch"
            parsed = date.fromisoformat(head_val)
            if parsed > clock.latest_fresh_date:
                reason = "post-dated beyond the timezone window"
            else:
                reason = (
                    f"older than the {clock.freshness_window_days}-day freshness window"
                )
            message = (
                f"{what} but last-updated is {head_val}, {reason}; "
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


def scan_links(tokens) -> tuple[list[tuple[int, str]], bool]:
    """One walk, two answers: (links worth resolving, unterminated comment found)."""
    return list(_iter_links_in(tokens)), has_unterminated_comment(tokens)


def _iter_links_in(tokens):
    """Every link destination in the token stream, with its line number.

    The label is discarded; comparing it to the target is a separate check. Only
    `link_open` counts — `image` is a distinct token type, so `![](...)` needs no
    lookbehind, and a link inside a code span, fence, or comment emits no token.

    The line comes from the block's own `map` plus the newlines before the token's
    recorded source offset (see `stamp_source_position`), so it is the parser's
    answer rather than a reconstruction.
    """
    for token in tokens:
        if token.type != "inline" or not token.map:
            continue
        block_line = token.map[0] + 1
        for child in token.children or []:
            # Not into an image's children. Those are its *alt text*, and
            # markdown-it still tokenizes link syntax there — `![alt [x][r]](i.png)`
            # carries a `link_open` — but the render is `<img alt="alt x">` with no
            # hyperlink at all, so resolving that destination is a false positive.
            if child.type != "link_open":
                continue
            offset = child.meta.get(SRC_POS)
            line = block_line
            if offset is not None:
                line += token.content.count("\n", 0, offset)
            yield line, (child.attrGet("href") or "").strip()


def check_links_and_anchors(repo: Repo, enabled: frozenset[str]) -> list[Violation]:
    violations: list[Violation] = []
    for rel in repo.docs:
        links, unterminated_comment = scan_links(repo.tokens(rel))
        if (
            "links" in enabled
            and not repo.config.ignored(rel, "links")
            and unterminated_comment
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
        for lineno, target in links:
            if EXTERNAL_RE.match(target):
                continue
            # Decoded only now that the target is known repo-local: decoding
            # external URLs rewrote six `#:~:text=` links this never resolves.
            # And split *before* decoding — a filename containing `#` must be
            # encoded (`foo%23bar.md`), so decoding first reintroduces the
            # delimiter and the partition resolves `foo` against a `#bar.md`
            # fragment, rejecting a valid link. markdown-it also normalizes
            # `<b c.md>` to `b%20c.md`, which is why decoding happens at all.
            raw_path, _, raw_frag = target.partition("#")
            path_part, frag = percent_decode(raw_path), percent_decode(raw_frag)
            target = f"{path_part}#{frag}" if raw_frag else path_part

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


def raw_cell_count(line: str) -> int:
    r"""How many cells a table row's source line actually declares.

    The parser **normalizes column counts away** — a short row is padded, a long
    one truncated — so both would pass. The long case is the one that matters: an
    unescaped `|` splits a Consult-when cell, and the truncated remainder would
    satisfy the cell-length check while the real content went unchecked.

    A `\|` is a literal pipe, not a delimiter, even inside a code span. Whether
    the *trailing* pipe is escaped is decided by the **parity** of the backslash
    run before it: a final cell ending in a literal backslash (`… | c\\|`) has an
    even run, so that pipe is the delimiter and a `\|` suffix test rejects a valid
    row as having four cells.
    """
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        run = len(body) - 1 - len(body[:-1].rstrip("\\"))
        if run % 2 == 0:
            body = body[:-1]
    cells, escaped = 1, False
    for ch in body:
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == "|":
            cells += 1
    return cells


def parse_map_rows(repo: Repo, map_rel: str) -> list[MapRow]:
    """Rows of every Documentation Map table in a file.

    A map's heading level is not fixed and both nestings are correct — one file
    puts `## Documentation Map` above `###` sections, another `#` above `##` — so
    a map is found by heading *text* and its sections by "any deeper heading",
    never an absolute level. Keying on level reported 81 docs falsely unmapped.

    Header and separator rows need no handling: the parser puts the header in
    `thead` and consumes `|---|` as structure, so neither reaches the body loop.
    """
    text = repo.text(map_rel)
    source_lines = text.split("\n")
    rows: list[MapRow] = []
    in_map = False
    map_level = 0
    section = ""
    in_thead = False
    in_top_table = False
    row_line: int | None = None
    cells: list[str] = []

    tokens = repo.tokens(map_rel)
    for i, token in enumerate(tokens):
        # `token.level` gates out containers throughout: a map *quoted* as an
        # example emits ordinary heading and row tokens, and its rows would then be
        # resolved as real citations. A map is a navigational structure at a
        # document's top level, never inside a blockquote or list item.
        if token.type == "heading_open" and token.level == 0:
            level, heading = int(token.tag[1:]), heading_text(tokens[i + 1])
            if is_map_heading(heading):
                in_map, map_level, section = True, level, ""
            elif in_map and level <= map_level:
                in_map = False
            elif in_map:
                section = heading
        elif token.type == "table_open":
            in_top_table = token.level == 0
        elif token.type == "table_close":
            in_top_table = False
        elif token.type == "thead_open":
            in_thead = True
        elif token.type == "thead_close":
            in_thead = False
        elif token.type == "tr_open" and in_map and in_top_table and not in_thead:
            row_line = (token.map[0] + 1) if token.map else None
            cells = []
        elif token.type == "td_open" and row_line is not None:
            nxt = tokens[i + 1]
            cells.append(nxt.content.strip() if nxt.type == "inline" else "")
        elif token.type == "tr_close" and row_line is not None:
            declared = raw_cell_count(source_lines[row_line - 1])
            if declared != 3:
                # Reported, not skipped. Discarding the row silently means its cited
                # path is never resolved, so a stray unescaped `|` turns a broken row
                # into a passing one — and outside `design-docs/` there is no
                # unmapped-doc backstop to catch the document another way.
                rows.append(MapRow(map_rel, row_line, section, "", "", "", declared))
            else:
                topic, doc_cell, consult = (cells + ["", "", ""])[:3]
                rows.append(
                    MapRow(map_rel, row_line, section, topic, doc_cell, consult)
                )
            row_line = None
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

    # Every doc, not a pre-filtered list: `parse_map_rows` already yields nothing
    # for a file with no map, and a separate detection pass meant a second parse of
    # every document plus a second copy of the "what is a map heading" rule.
    #
    # A map section may legitimately hold no table — a repo whose real map lives
    # elsewhere keeps the heading plus a pointer to it — so zero rows is not an error.
    for map_rel in repo.docs:
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
    status = field_text(repo.frontmatter(resolved).get("status", "")).strip()
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

# Every field the conventions define holds a single value, so a YAML sequence is
# malformed wherever one of these appears. Checked by shape rather than by value:
# leaning on the value validators caught only the fields that happen to have one
# (the dates, `status`) and let a sequence-valued `title` pass as merely non-empty.
SCALAR_FIELDS = frozenset(
    {"title", "status", "created", "last-updated", "generated", "superseded-by"}
)


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

    def report(rel: str, message: str) -> None:
        violations.append(Violation("frontmatter", rel, message))

    for rel in repo.docs:
        if not under_doc_root(repo, rel):
            # `AGENTS.md` / `README.md` carry no frontmatter by convention.
            continue
        if repo.config.ignored(rel, "frontmatter"):
            continue
        if has_unclosed_frontmatter(repo.text(rel)):
            report(
                rel,
                "frontmatter block is opened but never closed; add the closing `---`",
            )
            # Every field check below reads a block that parsed to nothing, so
            # they would pile "missing title" onto the real cause.
            continue

        fm = repo.frontmatter(rel)
        required = required_fields(rel)

        missing = [k for k in required if k not in fm]
        if missing:
            report(
                rel, f"frontmatter is missing required field(s): {', '.join(missing)}"
            )

        # A required key present with an empty value satisfies nothing the field
        # exists for, so it is a violation rather than a pass.
        blank = [k for k in required if k in fm and not field_text(fm[k]).strip()]
        if blank:
            report(rel, f"frontmatter field(s) present but empty: {', '.join(blank)}")

        # Shape, not value: a sequence is malformed for any field the conventions
        # define, whether or not that field has a value validator to trip over.
        sequences = sorted(
            k for k, v in fm.items() if isinstance(v, list) and k in SCALAR_FIELDS
        )
        if sequences:
            report(
                rel,
                "frontmatter field(s) must hold a single value, not a list: "
                f"{', '.join(sequences)}",
            )

        # The tables define these as ISO dates, and checking only that they are
        # populated accepted `created: yesterday`. `freshness` validates
        # `last-updated` too, but only for docs in this branch's diff — a doc
        # nobody touched keeps whatever it has, so the value is checked here as well.
        if not repo.config.is_template(rel):
            for key in ("created", "last-updated"):
                value = field_text(fm.get(key, "")).strip()
                if value and not is_iso_date(value):
                    report(
                        rel,
                        f"`{key}` is not a valid ISO YYYY-MM-DD date (got '{value}')",
                    )

        status = field_text(fm.get("status", "")).strip()
        if (
            "/design-docs/" in f"/{rel}"
            and status
            and status not in DESIGN_DOC_STATUSES
        ):
            report(
                rel,
                f"design doc status must be one of "
                f"{', '.join(DESIGN_DOC_STATUSES)} (got '{status}')",
            )

        # Absent is fine — the required-field check above owns that case.
        if "/generated/" in f"/{rel}" and "generated" in fm:
            generated = field_text(fm["generated"])
            if generated != "true":
                report(
                    rel,
                    f"generated doc must declare `generated: true` (got '{generated}')",
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
        target = field_text(fm["superseded-by"]).strip()
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
        prog="uv run scripts/lint_docs.py",
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
    clock = Clock.build(config.freshness_window_days)

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
            print(
                "Bump the date, or run `uv run scripts/lint_docs.py --fix`.",
                file=sys.stderr,
            )
        return 1

    if not args.fix:
        print("lint_docs: all documentation checks passed.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
