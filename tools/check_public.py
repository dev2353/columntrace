"""Pre-publish check: nothing private in the files that would be pushed.

Usage: python tools/check_public.py [project_root]

Built-in checks look for things that should never be in a public copy of this project: real
workspace or endpoint ids, e-mail addresses, personal home folders, access tokens and private
output files. Words specific to your own organization (company, workspace, server or schema
names) go in a git-ignored file, `.publiccheck.local`, one regular expression per line, so the
check can find them without this script naming them. Exit code 1 means something was found.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "dist", "data", "out", ".pytest_cache", "__pycache__", ".venv", ".ruff_cache", ".claude"}
SKIP_FILES = {"package-lock.json"}
SELF = {"tools/check_public.py", "tests/test_check_public.py"}   # the patterns and their deliberate test leaks
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".json", ".md", ".toml", ".yml", ".yaml", ".css", ".html", ".txt",
                 ".sql", ".tmdl", ".pbism", ".platform", ".schedules", ".cfg", ".ini", ".example", ""}
FORBIDDEN_NAMES = [re.compile(p) for p in (r"\.private\.json$", r"(^|/)\.env(\.\w+)?\.local$", r"(^|/)\.publiccheck\.local$")]

SYNTHETIC_GUID = re.compile(r"^(0{8}|f{8})-0000-0000-0000-[0-9a-f]{12}$", re.I)
# well-known public ids that are fine to publish
PUBLIC_GUIDS = {"499b84ac-1321-427f-aa17-267ca6975798"}   # the Azure DevOps API resource (token audience)
STORAGE_DOMAIN = re.compile(r"@onelake\.dfs\.fabric\.microsoft\.com|\.dfs\.core\.windows\.net", re.I)
CHECKS: list[tuple[str, re.Pattern[str]]] = [
    ("e-mail address", re.compile(r"\b[A-Za-z0-9._%+-]+@(?!example\.(?:com|org)\b|users\.noreply\.github\.com\b|anthropic\.com\b)"
                                  r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("GUID (workspace, item or tenant id?)", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)),
    ("Fabric SQL endpoint host", re.compile(r"\b[a-z0-9-]{12,}\.datawarehouse\.fabric\.microsoft\.com\b", re.I)),
    ("OneLake path with a real workspace", re.compile(r"abfss://(?!ws@|workspace@|<|\{)[^@\s/]+@onelake\.dfs\.fabric\.microsoft\.com", re.I)),
    ("Azure DevOps organization", re.compile(r"dev\.azure\.com/(?!<)[A-Za-z0-9-]+", re.I)),
    ("personal home folder", re.compile(r"(?:[A-Za-z]:[\\/]+Users[\\/]+|/Users/|/home/)(?!<|runner\b|USER\b|you\b)[A-Za-z0-9._-]+", re.I)),
    ("access token", re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}|AccountKey=|SharedAccessSignature=|\bghp_[A-Za-z0-9]{20,}")),
]


def published_files(root: Path) -> list[Path]:
    """Files git would publish, or (outside a repository) every file not in an ignored folder."""
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root,
                             capture_output=True, check=True)
        files = [root / p for p in out.stdout.decode("utf-8").split("\0") if p]
        if files:
            return [f for f in files if f.is_file()]
    except (OSError, subprocess.CalledProcessError):
        pass
    # not a repository yet: walk the tree and honor the simple patterns of the .gitignore files
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        ignored = _ignore_patterns(root, here)
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not _ignored(here / d, root, ignored, True)]
        found.extend(here / f for f in filenames if not _ignored(here / f, root, ignored, False))
    return found


def _ignore_patterns(root: Path, here: Path) -> list[tuple[Path, str]]:
    """(folder of the .gitignore, pattern) for every .gitignore from the root down to `here`."""
    out = []
    chain = [p for p in [here, *here.parents] if p == root or root in p.parents]
    for d in reversed(chain):
        gi = d / ".gitignore"
        if gi.is_file():
            out += [(d, ln.strip()) for ln in gi.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith(("#", "!"))]
    return out


def _ignored(path: Path, root: Path, patterns: list[tuple[Path, str]], is_dir: bool) -> bool:
    from fnmatch import fnmatch
    for base, pat in patterns:
        if pat.endswith("/") and not is_dir:
            continue
        p = pat.rstrip("/")
        rel = path.relative_to(base).as_posix() if base in path.parents else path.name
        if fnmatch(path.name, p) or fnmatch(rel, p.lstrip("/")):
            return True
    return False


def local_patterns(root: Path) -> list[re.Pattern[str]]:
    f = root / ".publiccheck.local"
    if not f.is_file():
        return []
    lines = [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()]
    return [re.compile(ln, re.I) for ln in lines if ln and not ln.startswith("#")]


def mask(s: str) -> str:
    return s if len(s) <= 6 else f"{s[:3]}…{s[-2:]}"


def scan(root: Path, extra: list[re.Pattern[str]] | None = None) -> list[str]:
    extra = extra if extra is not None else local_patterns(root)
    problems: list[str] = []
    for path in sorted(published_files(root)):
        rel = path.relative_to(root).as_posix()
        if any(p.search(rel) for p in FORBIDDEN_NAMES):
            problems.append(f"{rel}: private file would be published")
            continue
        if path.name in SKIP_FILES or path.suffix.lower() not in TEXT_SUFFIXES or rel in SELF:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for label, rx in CHECKS:
                for m in rx.finditer(line):
                    if label.startswith("GUID") and (SYNTHETIC_GUID.match(m.group(0)) or m.group(0).lower() in PUBLIC_GUIDS):
                        continue
                    if label == "e-mail address" and STORAGE_DOMAIN.search(line, m.start()):
                        continue
                    problems.append(f"{rel}:{n}: {label}: {mask(m.group(0))}")
            for rx in extra:
                m = rx.search(line)
                if m:
                    problems.append(f"{rel}:{n}: matches a local pattern: {mask(m.group(0))}")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    root = Path(args[0] if args else Path(__file__).resolve().parents[1]).resolve()
    problems = scan(root)
    for p in problems:
        print(p)
    n_local = len(local_patterns(root))
    print(f"{len(problems)} finding(s); built-in checks plus {n_local} local pattern(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
