#!/usr/bin/env python3
"""Package a skill directory into a distributable `.skill` archive.

    python scripts/package_skill.py skill [--output dist]

A `.skill` file is a zip whose single top-level directory is the skill's name.
That name comes from the `name:` field in SKILL.md rather than from the folder
on disk, so the source directory can be called anything (here: `skill/`) while
the archive still unpacks as `delegate-local/`.

Archives are built deterministically — fixed timestamps, sorted entries — so an
unchanged skill produces a byte-identical file and doesn't churn releases.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import stat
import sys
import zipfile
from pathlib import Path

EXCLUDE_DIRS = {"__pycache__", "node_modules", ".git"}
EXCLUDE_GLOBS = {"*.pyc", "*.skill"}
EXCLUDE_FILES = {".DS_Store"}
ROOT_EXCLUDE_DIRS = {"evals"}

ALLOWED_KEYS = {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}
NAME_RE = re.compile(r"^[a-z0-9-]+$")
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)

# Fixed DOS timestamp for reproducible archives (zip epoch is 1980).
FIXED_DATE = (1980, 1, 1, 0, 0, 0)


class SkillError(Exception):
    """Validation failure — reported to the user, never a traceback."""


class Nested:
    """Marker for a top-level key whose value is a nested block (e.g. metadata).

    Validation only type-checks the scalar fields, so nested values just need to
    be distinguishable from strings rather than fully parsed.
    """

    __slots__ = ()


KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):[ \t]*(.*)$")
BLOCK_INDICATORS = {"|", ">", "|-", ">-", "|+", ">+"}


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_frontmatter(block: str) -> dict:
    """Parse the top level of a SKILL.md frontmatter mapping.

    Deliberately not a general YAML parser — it handles exactly what skill
    frontmatter uses (scalar keys, folded/literal block scalars, plain scalars
    continued across indented lines, and opaque nested blocks). Keeping it
    dependency-free means this script runs identically in CI and on a machine
    where pip is locked down by PEP 668.
    """
    data: dict = {}
    lines = block.split("\n")
    i = 0

    def consume_indented() -> list[str]:
        collected = []
        nonlocal i
        while i < len(lines) and (not lines[i].strip() or lines[i][:1].isspace()):
            collected.append(lines[i])
            i += 1
        return collected

    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#") or line[:1].isspace():
            i += 1
            continue

        match = KEY_RE.match(line)
        if not match:
            i += 1
            continue

        key, rest = match.group(1), match.group(2).strip()
        i += 1

        if rest in BLOCK_INDICATORS:
            body = [ln.strip() for ln in consume_indented()]
            joiner = "\n" if rest.startswith("|") else " "
            data[key] = joiner.join(body).strip()
        elif rest == "":
            body = consume_indented()
            data[key] = Nested() if any(ln.strip() for ln in body) else ""
        else:
            # A plain scalar may continue across indented lines, folded with spaces.
            continuation = [ln.strip() for ln in consume_indented() if ln.strip()]
            data[key] = " ".join([_unquote(rest), *continuation]).strip()

    return data


def load_frontmatter(skill_md: Path) -> dict:
    content = skill_md.read_text(encoding="utf-8")
    if not content.startswith("---"):
        raise SkillError("SKILL.md has no YAML frontmatter")
    match = FRONTMATTER_RE.match(content)
    if not match:
        raise SkillError("SKILL.md frontmatter is not terminated by a closing ---")
    data = parse_frontmatter(match.group(1))
    if not data:
        raise SkillError("frontmatter is empty")
    return data


def validate(skill_dir: Path) -> dict:
    """Enforce the published SKILL.md spec. Returns the parsed frontmatter."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise SkillError(f"SKILL.md not found in {skill_dir}")

    fm = load_frontmatter(skill_md)

    unexpected = set(fm) - ALLOWED_KEYS
    if unexpected:
        raise SkillError(
            f"unexpected frontmatter key(s): {', '.join(sorted(unexpected))}. "
            f"Allowed: {', '.join(sorted(ALLOWED_KEYS))}"
        )

    for field in ("name", "description"):
        if field not in fm:
            raise SkillError(f"missing '{field}' in frontmatter")
        if not isinstance(fm[field], str):
            raise SkillError(f"'{field}' must be a string, got {type(fm[field]).__name__}")

    name = fm["name"].strip()
    if not NAME_RE.match(name):
        raise SkillError(f"name '{name}' must be kebab-case (lowercase, digits, hyphens)")
    if name.startswith("-") or name.endswith("-") or "--" in name:
        raise SkillError(f"name '{name}' cannot start/end with a hyphen or contain '--'")
    if len(name) > 64:
        raise SkillError(f"name is {len(name)} characters; the maximum is 64")

    description = fm["description"].strip()
    if "<" in description or ">" in description:
        raise SkillError("description cannot contain angle brackets (< or >)")
    if len(description) > 1024:
        raise SkillError(f"description is {len(description)} characters; the maximum is 1024")

    compatibility = fm.get("compatibility", "")
    if compatibility and len(compatibility) > 500:
        raise SkillError(f"compatibility is {len(compatibility)} characters; the maximum is 500")

    return {"name": name, "description": description}


def should_exclude(rel: Path) -> bool:
    parts = rel.parts
    if any(part in EXCLUDE_DIRS for part in parts):
        return True
    if parts and parts[0] in ROOT_EXCLUDE_DIRS:
        return True
    if rel.name in EXCLUDE_FILES:
        return True
    return any(fnmatch.fnmatch(rel.name, pat) for pat in EXCLUDE_GLOBS)


def collect_files(skill_dir: Path) -> list[Path]:
    files = [
        path
        for path in skill_dir.rglob("*")
        if path.is_file() and not should_exclude(path.relative_to(skill_dir))
    ]
    # Sorted so archive order is stable regardless of filesystem iteration order.
    return sorted(files, key=lambda p: p.relative_to(skill_dir).as_posix())


def package(skill_dir: Path, output_dir: Path) -> Path:
    meta = validate(skill_dir)
    name = meta["name"]

    files = collect_files(skill_dir)
    if not files:
        raise SkillError(f"no packageable files found in {skill_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{name}.skill"

    warnings: list[str] = []
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            rel = path.relative_to(skill_dir)
            arcname = Path(name) / rel
            mode = path.stat().st_mode

            # Scripts that lose their executable bit are a silent failure later,
            # so surface it at package time rather than at first use.
            if rel.parts and rel.parts[0] == "scripts" and not mode & stat.S_IXUSR:
                warnings.append(f"{rel.as_posix()} is not executable")

            info = zipfile.ZipInfo(arcname.as_posix(), date_time=FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (mode & 0xFFFF) << 16
            zf.writestr(info, path.read_bytes())
            print(f"  added  {arcname.as_posix()}")

    for warning in warnings:
        print(f"  WARN   {warning}", file=sys.stderr)

    size = target.stat().st_size
    print(f"\npackaged {len(files)} files -> {target} ({size:,} bytes)")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Package a skill directory into a .skill file")
    parser.add_argument("skill_dir", nargs="?", default="skill", help="skill source directory")
    parser.add_argument("--output", default="dist", help="output directory (default: dist)")
    parser.add_argument("--check", action="store_true", help="validate only, do not write an archive")
    args = parser.parse_args()

    skill_dir = Path(args.skill_dir).resolve()
    if not skill_dir.is_dir():
        print(f"error: not a directory: {skill_dir}", file=sys.stderr)
        return 2

    try:
        if args.check:
            meta = validate(skill_dir)
            print(f"valid: {meta['name']} ({len(meta['description'])} char description)")
        else:
            package(skill_dir, Path(args.output).resolve())
    except SkillError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
