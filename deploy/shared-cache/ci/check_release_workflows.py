#!/usr/bin/env python3
"""Fail CI if a release workflow, or a workflow that runs with secrets on untrusted code,
can reach the shared build cache (docs/tailscale-shared-cache.md, sections 3.4 and 3.5).

Meant to be copied into ostrium-labs/loams and run in its CI on every pull request:

    python3 scripts/check_release_workflows.py            # checks ./.github/workflows
    python3 scripts/check_release_workflows.py --root ../loams --release-glob 'deploy-*'
    python3 scripts/check_release_workflows.py --self-test

A workflow "uses the cache" if, outside full-line comments, its text mentions one of
MARKERS (case-insensitive), or if a local action or reusable workflow it calls with
`uses: ./...` does, followed recursively. Two rules:

1. A release workflow must not use the cache. A workflow file is a release workflow if
   its file name matches one of RELEASE_GLOBS or a --release-glob.
2. A workflow triggered by pull_request_target or workflow_run must not use the cache,
   because those triggers can run code from a fork with the repository's secrets.

Each violation is printed as a GitHub annotation, `::error file=<path>::<message>`, and
the exit status is 1. A local `uses: ./...` that cannot be read is also an error, since
the check cannot see what it does. Remote reusable workflows and actions
(`owner/repo/...@ref`) are not followed.

Standard library only, so it runs on any runner with Python 3.9 or newer.
"""

from __future__ import annotations

import argparse
import fnmatch
import posixpath
import re
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

# Text that shows a workflow joins the tailnet or turns sccache on.
MARKERS = ("SCCACHE_", "RUSTC_WRAPPER", "sccache", "tailscale/github-action", "TS_OAUTH")

# File names of release, package, sign, promote and publish workflows.
RELEASE_GLOBS = ("release*", "publish*", "*-release*", "*-sign*", "*promote*")

# Triggers that can run fork code with the repository's secrets.
RISKY_TRIGGERS = ("pull_request_target", "workflow_run")

WORKFLOWS_DIR = ".github/workflows"

Reader = Callable[[str], Optional[str]]

_ON_KEY = re.compile(r"""^(?:on|"on"|'on')\s*:(.*)$""")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_SEQ_ITEM = re.compile(r"^\s*-\s*(.*)$")
_LOCAL_USES = re.compile(r"""^\s*(?:-\s*)?uses\s*:\s*['"]?(\./[^'"\s#]*)""", re.MULTILINE)


def strip_comment_lines(text: str) -> str:
    """Drop lines whose first non-blank character is '#'.

    Only whole-line comments are dropped. A trailing '#' may sit inside a quoted string or
    a shell command, so it is kept: the check may then flag a mention in a trailing
    comment, which is the safe direction to be wrong in.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def first_marker(text: str) -> Optional[str]:
    lowered = strip_comment_lines(text).lower()
    for marker in MARKERS:
        if marker.lower() in lowered:
            return marker
    return None


def _strip_trailing_comment(line: str) -> str:
    """Drop ` # ...` outside quotes; YAML starts a comment only at whitespace."""
    quote: Optional[str] = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def _balanced(text: str) -> bool:
    """Whether every [] and {} in `text` is closed outside quotes and comments."""
    depth = 0
    quote: Optional[str] = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                if quote == "'" and i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth < 0:
                return False
        elif ch == "#" and (i == 0 or text[i - 1].isspace()):
            newline = text.find("\n", i)
            if newline == -1:
                break
            i = newline
        i += 1
    return depth == 0 and quote is None


def _split_key(part: str) -> Optional[tuple[str, str]]:
    """An event name and the text after its `:` from one flow item or block entry."""
    part = part.strip()
    if not part:
        return None
    if part[0] in "\"'":
        quote = part[0]
        i = 1
        while i < len(part):
            if part[i] == quote:
                if quote == "'" and i + 1 < len(part) and part[i + 1] == "'":
                    i += 2
                    continue
                break
            i += 1
        else:
            return None
        name = part[1:i]
        rest = part[i + 1:].strip()
        if rest.startswith(":"):
            return name, rest[1:].strip()
        return (name, "") if not rest else None
    match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*(?::(.*))?$", part, re.S)
    if not match:
        return None
    return match.group(1), (match.group(2) or "").strip()


def _event(part: str) -> Optional[str]:
    key = _split_key(part)
    if key is None:
        return None
    return key[0] if _IDENT.match(key[0]) else None


def _split_flow(text: str) -> Optional[list[str]]:
    """The top-level comma-separated items of a flow sequence or mapping."""
    text = text.strip()
    if len(text) < 2 or text[0] not in "[{" or text[-1] != ("]" if text[0] == "[" else "}"):
        return None
    items: list[str] = []
    start = 1
    depth = 0
    quote: Optional[str] = None
    i = 1
    while i < len(text) - 1:
        ch = text[i]
        if quote:
            if ch == quote:
                if quote == "'" and i + 1 < len(text) - 1 and text[i + 1] == "'":
                    i += 2
                    continue
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append(text[start:i])
            start = i + 1
        i += 1
    items.append(text[start:-1])
    return items


def _flow_events(text: str) -> Optional[set[str]]:
    items = _split_flow(text)
    if items is None:
        return None
    found: set[str] = set()
    for item in items:
        if not item.strip():
            continue
        name = _event(item)
        if name is None:
            return None
        found.add(name)
    return found


def triggers(text: str) -> Optional[set[str]]:
    """The event names under the top-level `on:` key, or None when the key is absent or its
    value cannot be read safely. Both block and flow YAML are read, including keys in quotes
    and flow lists that span lines; anything else returns None, which `check` treats as
    unreadable rather than as an empty event set."""
    lines = strip_comment_lines(text).splitlines()
    for i, line in enumerate(lines):
        match = _ON_KEY.match(line)
        if not match:
            continue
        inline = _strip_trailing_comment(match.group(1)).strip()
        if inline:
            if inline[0] in "[{":
                block = inline
                j = i + 1
                while not _balanced(block):
                    if j >= len(lines):
                        return None
                    nxt = _strip_trailing_comment(lines[j]).strip()
                    if nxt and not lines[j][0].isspace():
                        return None
                    block += "\n" + nxt
                    j += 1
                return _flow_events(block)
            name = _event(inline)
            return {name} if name is not None else None
        found: set[str] = set()
        indent: Optional[int] = None
        style: Optional[str] = None
        j = i + 1
        while j < len(lines):
            raw = lines[j]
            if not raw.strip():
                j += 1
                continue
            if not raw[0].isspace():
                break  # the next top-level key
            depth = len(raw) - len(raw.lstrip())
            if indent is None:
                indent = depth
            if depth < indent:
                return None
            stripped = _strip_trailing_comment(raw).rstrip()
            if depth > indent:
                j += 1
                continue
            entry = stripped.strip()
            if style is None:
                if entry.startswith("-"):
                    style = "seq"
                elif entry[:1] in ("[", "{"):
                    style = "flow"
                else:
                    style = "map"
            if style == "flow":
                block = entry
                k = j + 1
                while not _balanced(block):
                    if k >= len(lines):
                        return None
                    nxt = _strip_trailing_comment(lines[k]).strip()
                    if nxt and not lines[k][0].isspace():
                        return None
                    block += "\n" + nxt
                    k += 1
                events = _flow_events(block)
                if events is None:
                    return None
                found |= events
                j = k
                continue
            if style == "seq":
                item = _SEQ_ITEM.match(stripped)
                if not item:
                    return None
                name = _event(item.group(1))
            else:
                name = _event(entry)
            if name is None:
                return None
            found.add(name)
            j += 1
        return found or None
    return None


def local_uses(text: str) -> list[str]:
    """Targets of `uses: ./...` lines, as repository-relative paths."""
    refs = []
    for raw in _LOCAL_USES.findall(strip_comment_lines(text)):
        path = posixpath.normpath(raw.split("@", 1)[0])
        refs.append("" if path == "." else path)
    return refs


def resolve(ref: str, read: Reader) -> Optional[tuple[str, str]]:
    """The file a local `uses:` points at: a workflow file, or a directory's action.yml."""
    candidates = [ref] if ref.endswith((".yml", ".yaml")) else [
        posixpath.join(ref, "action.yml"),
        posixpath.join(ref, "action.yaml"),
    ]
    for candidate in candidates:
        text = read(candidate)
        if text is not None:
            return candidate, text
    return None


def cache_use(path: str, text: str, read: Reader, seen: Optional[set[str]] = None) -> tuple[Optional[str], list[str]]:
    """How `path` reaches the cache, if it does, and any local references it cannot read.

    Returns (reason, unreadable). reason is None when neither the file nor anything it
    calls locally mentions a marker.
    """
    seen = set() if seen is None else seen
    seen.add(path)
    marker = first_marker(text)
    if marker:
        return f"mentions {marker}", []
    unreadable: list[str] = []
    for ref in local_uses(text):
        target = resolve(ref, read)
        if target is None:
            unreadable.append(ref or ".")
            continue
        target_path, target_text = target
        if target_path in seen:
            continue
        reason, missing = cache_use(target_path, target_text, read, seen)
        unreadable.extend(missing)
        if reason:
            return f"uses ./{target_path}, which {reason}", unreadable
    return None, unreadable


def is_release(path: str, release_globs: Iterable[str]) -> bool:
    name = posixpath.basename(path).lower()
    return any(fnmatch.fnmatchcase(name, glob.lower()) for glob in release_globs)


def check(workflow_paths: Iterable[str], read: Reader, release_globs: Iterable[str]) -> list[tuple[str, str]]:
    """All violations, as (repository-relative path, message) pairs."""
    release_globs = tuple(release_globs)
    problems: list[tuple[str, str]] = []
    for path in sorted(workflow_paths):
        text = read(path)
        if text is None:
            problems.append((path, "cannot be read"))
            continue
        reason, unreadable = cache_use(path, text, read)
        release = is_release(path, release_globs)
        on = triggers(text)
        risky = sorted(set(RISKY_TRIGGERS) & on) if on is not None else []
        if reason and release:
            problems.append((path, f"is a release workflow and {reason}. Release, package, "
                                   "sign, promote and publish workflows must not join the "
                                   "tailnet or use the shared cache (plan section 3.5)."))
        if reason and risky:
            problems.append((path, f"is triggered by {', '.join(risky)} and {reason}. A "
                                   "workflow that can run fork code with secrets must not "
                                   "use the shared cache (plan section 3.4)."))
        if reason and on is None and not release:
            problems.append((path, f"{reason}, but its triggers cannot be read safely (no "
                                   "readable top-level 'on:' block), so the check cannot clear it."))
        if (release or risky) and unreadable:
            for ref in unreadable:
                problems.append((path, f"uses {ref}, which cannot be read, so the check "
                                       "cannot see whether it uses the shared cache."))
    return problems


def workflow_files(root: Path) -> list[str]:
    directory = root / WORKFLOWS_DIR
    files = [p for p in directory.iterdir() if p.is_file() and p.suffix in (".yml", ".yaml")]
    return [p.relative_to(root).as_posix() for p in files]


def disk_reader(root: Path) -> Reader:
    resolved_root = root.resolve()

    def read(relative: str) -> Optional[str]:
        target = (resolved_root / relative).resolve()
        if resolved_root not in target.parents and target != resolved_root:
            return None  # a `uses: ./../x` outside the repository
        try:
            return target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    return read


def report(problems: list[tuple[str, str]]) -> None:
    for path, message in problems:
        # Annotation values cannot contain raw newlines; the messages have none.
        print(f"::error file={path}::{path} {message}")


# --- self-test ---------------------------------------------------------------------------

_CACHE_JOB = """\
name: Test
on:
  push:
    branches: [dev]
  pull_request:          # not pull_request_target
jobs:
  test:
    runs-on: ubuntu-24.04
    steps:
      - uses: tailscale/github-action@d1b6cd204f8dceda5b3eaad7f1f767be390056cd # v4.2.0
        with:
          oauth-secret: ${{ secrets.TS_OAUTH_SECRET }}
      - run: echo "RUSTC_WRAPPER=sccache" >> "$GITHUB_ENV"
"""

_PLAIN_RELEASE = """\
name: Release
on:
  push:
    tags: ['v*']
jobs:
  build:
    runs-on: ubuntu-24.04
    steps:
      - run: cargo build --release --locked
"""


def _workflow(on_block: str, step: str) -> str:
    return f"name: W\n{on_block}\njobs:\n  j:\n    runs-on: ubuntu-24.04\n    steps:\n      - {step}\n"


def self_test() -> int:
    wf = WORKFLOWS_DIR
    cases: list[tuple[str, dict[str, str], tuple[str, ...], set[str]]] = [
        # (description, files, extra release globs, workflow paths expected to fail)
        ("cache in a test workflow, plain release workflow",
         {f"{wf}/ci.yml": _CACHE_JOB, f"{wf}/release-github.yml": _PLAIN_RELEASE}, (), set()),
        ("release workflow joins the tailnet",
         {f"{wf}/release-images.yml": _PLAIN_RELEASE + "      - uses: tailscale/github-action@abc\n"},
         (), {f"{wf}/release-images.yml"}),
        ("*-sign* workflow sets RUSTC_WRAPPER, any case",
         {f"{wf}/desktop-sign.yml": _workflow("on: workflow_call", "run: export rustc_wrapper=sccache")},
         (), {f"{wf}/desktop-sign.yml"}),
        ("*-release* and *promote* and publish* names, .yaml extension",
         {f"{wf}/sdk-release.yaml": _workflow("on: push", "run: sccache --show-stats"),
          f"{wf}/auto-promote.yml": _workflow("on: push", "env: {SCCACHE_BUCKET: rust-cache}"),
          f"{wf}/publish-crates.yml": _workflow("on: push", "run: echo $TS_OAUTH_CLIENT_ID")},
         (), {f"{wf}/sdk-release.yaml", f"{wf}/auto-promote.yml", f"{wf}/publish-crates.yml"}),
        ("a mention in a whole-line comment does not count",
         {f"{wf}/release-github.yml": "# This workflow never uses sccache or TS_OAUTH_*.\n" + _PLAIN_RELEASE},
         (), set()),
        ("a release workflow calling a reusable workflow that uses the cache",
         {f"{wf}/desktop-electron-release.yml": _workflow("on: push", "uses: ./.github/workflows/build.yml"),
          f"{wf}/build.yml": _workflow("on: workflow_call", "run: sccache --start-server")},
         (), {f"{wf}/desktop-electron-release.yml"}),
        ("a release workflow using a local composite action that uses the cache",
         {f"{wf}/release-package.yml": _workflow("on: push", "uses: ./.github/actions/setup"),
          ".github/actions/setup/action.yml": "runs:\n  using: composite\n  steps:\n"
                                              "    - run: echo RUSTC_WRAPPER=sccache\n      shell: bash\n"},
         (), {f"{wf}/release-package.yml"}),
        ("a release workflow using a clean local action, and a reference cycle",
         {f"{wf}/release-a.yml": _workflow("on: push", "uses: ./.github/workflows/b.yml"),
          f"{wf}/b.yml": _workflow("on: workflow_call", "uses: ./.github/workflows/release-a.yml")},
         (), set()),
        ("a release workflow whose local action cannot be read",
         {f"{wf}/release-x.yml": _workflow("on: push", "uses: ./.github/actions/missing")},
         (), {f"{wf}/release-x.yml"}),
        ("pull_request_target as a scalar",
         {f"{wf}/ci.yml": _workflow("on: pull_request_target", "uses: tailscale/github-action@abc")},
         (), {f"{wf}/ci.yml"}),
        ("workflow_run in a flow list",
         {f"{wf}/ci.yml": _workflow("on: [push, workflow_run]", "run: sccache --show-stats")},
         (), {f"{wf}/ci.yml"}),
        ("workflow_run in a flow list split across lines",
         {f"{wf}/ci.yml": _workflow("on: [push,\n  workflow_run]", "run: sccache --show-stats")},
         (), {f"{wf}/ci.yml"}),
        ("pull_request_target with a quoted key and an empty mapping",
         {f"{wf}/ci.yml": _workflow('on:\n  "pull_request_target": {}', "run: sccache --show-stats")},
         (), {f"{wf}/ci.yml"}),
        ("pull_request_target in a block, quoted on key",
         {f"{wf}/ci.yml": _workflow('"on":\n  pull_request_target:\n    types: [opened]', "run: sccache -s")},
         (), {f"{wf}/ci.yml"}),
        ("workflow_run trigger calling a reusable workflow that uses the cache",
         {f"{wf}/after.yml": _workflow("on:\n  workflow_run:\n    workflows: [CI]", "uses: ./.github/workflows/build.yml"),
          f"{wf}/build.yml": _workflow("on: workflow_call", "run: sccache --start-server")},
         (), {f"{wf}/after.yml"}),
        ("workflow_run trigger without the cache is fine",
         {f"{wf}/release-notes.yml": _workflow("on:\n  workflow_run:\n    workflows: [CI]", "run: echo notes")},
         (), set()),
        ("github.event.workflow_run in an expression is not a trigger",
         {f"{wf}/ci.yml": _CACHE_JOB + "      - run: echo ${{ github.event.workflow_run.id }}\n"},
         (), set()),
        ("--release-glob adds a name pattern",
         {f"{wf}/console-deploy.yml": _workflow("on: push", "run: sccache -s")},
         ("console-deploy*",), {f"{wf}/console-deploy.yml"}),
        ("without that glob the same file passes",
         {f"{wf}/console-deploy.yml": _workflow("on: push", "run: sccache -s")},
         (), set()),
    ]
    failures = 0
    for description, files, extra_globs, expected in cases:
        workflows = [p for p in files if p.startswith(WORKFLOWS_DIR + "/")]
        problems = check(workflows, files.get, RELEASE_GLOBS + extra_globs)
        flagged = {path for path, _ in problems}
        if flagged != expected:
            failures += 1
            print(f"FAIL: {description}: expected {sorted(expected)}, got {sorted(flagged)}")
            for path, message in problems:
                print(f"      {path}: {message}")
    # Unit checks of the helpers.
    unit = [
        (triggers("on: push\n"), {"push"}),
        (triggers("on:\n  - push\n  - pull_request\n"), {"push", "pull_request"}),
        (triggers("on:\n  push:\n    branches: [dev]\n  workflow_dispatch:\njobs: {}\n"),
         {"push", "workflow_dispatch"}),
        (triggers("on: push # pull_request_target is not used\n"), {"push"}),
        (triggers('on:\n  "pull_request_target": {}\n'), {"pull_request_target"}),
        (triggers("on: [push,\n  workflow_run]\n"), {"push", "workflow_run"}),
        (triggers("on: [push,\n  workflow_run\n"), None),
        (triggers("name: no triggers\n"), None),
        (local_uses("      - uses: ./.github/actions/x\n    uses: './.github/workflows/y.yml'\n"),
         [".github/actions/x", ".github/workflows/y.yml"]),
        (is_release(".github/workflows/Release-Images.YML", RELEASE_GLOBS), True),
        (is_release(".github/workflows/design.yml", RELEASE_GLOBS), False),
        (first_marker("  # sccache\n  run: true\n"), None),
    ]
    for got, want in unit:
        if got != want:
            failures += 1
            print(f"FAIL: expected {want!r}, got {got!r}")
    total = len(cases) + len(unit)
    if failures:
        print(f"self-test: {failures} of {total} checks failed")
        return 1
    print(f"self-test: all {total} checks passed")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=".", help="repository root (default: .)")
    parser.add_argument("--release-glob", action="append", default=[], metavar="GLOB",
                        help="another file-name pattern for release workflows; repeatable")
    parser.add_argument("--self-test", action="store_true", help="run the built-in tests and exit")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    root = Path(args.root)
    if not (root / WORKFLOWS_DIR).is_dir():
        print(f"::error::{root / WORKFLOWS_DIR} is not a directory", file=sys.stderr)
        return 2
    paths = workflow_files(root)
    globs = RELEASE_GLOBS + tuple(args.release_glob)
    problems = check(paths, disk_reader(root), globs)
    report(problems)
    releases = [p for p in sorted(paths) if is_release(p, globs)]
    print(f"checked {len(paths)} workflows, {len(releases)} of them release workflows: "
          f"{', '.join(posixpath.basename(p) for p in releases) or 'none'}")
    if problems:
        print(f"{len(problems)} problem(s) found")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
