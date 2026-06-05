#!/usr/bin/env python3
"""pulse — glanceable git repo dashboard.

Scan a directory, find every git repo, and show branch state, recent activity,
and last commit in one synthwave-colored screen.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "0.1.0"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SPARK = " ▁▂▃▄▅▆▇█"
DAYS = 14
IGNORED_DIRS = {
    "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    "target", ".next", ".nuxt", "vendor", ".cache", "site-packages",
    "Pods", "DerivedData", ".gradle", ".tox", ".terraform",
}


class Style:
    RESET = "\x1b[0m"
    BOLD = "\x1b[1m"
    DIM = "\x1b[2m"
    MAGENTA = "\x1b[38;5;200m"
    PINK = "\x1b[38;5;213m"
    CYAN = "\x1b[38;5;51m"
    YELLOW = "\x1b[38;5;227m"
    GREEN = "\x1b[38;5;120m"
    VIOLET = "\x1b[38;5;141m"
    GREY = "\x1b[38;5;245m"
    DARK = "\x1b[38;5;239m"

    @classmethod
    def disable(cls) -> None:
        for name in list(vars(cls)):
            if name.isupper():
                setattr(cls, name, "")


@dataclass
class Repo:
    path: Path
    name: str
    branch: str = "?"
    staged: int = 0
    unstaged: int = 0
    untracked: int = 0
    ahead: int = 0
    behind: int = 0
    last_commit_at: int = 0
    last_commit_subject: str = ""
    activity: list[int] = field(default_factory=list)

    @property
    def dirty(self) -> bool:
        return self.staged + self.unstaged + self.untracked > 0


def run_git(args: list[str], cwd: Path, timeout: float = 4.0) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            timeout=timeout, check=False,
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return ""


def find_repos(root: Path, max_depth: int = 6) -> list[Path]:
    root = root.resolve()
    found: list[Path] = []

    def walk(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = list(d.iterdir())
        except (PermissionError, OSError):
            return
        if any(e.name == ".git" for e in entries):
            found.append(d)
            return
        for e in entries:
            if e.is_symlink() or not e.is_dir():
                continue
            if e.name in IGNORED_DIRS or e.name.startswith("."):
                continue
            walk(e, depth + 1)

    walk(root, 0)
    return found


def probe(path: Path) -> Repo:
    repo = Repo(path=path, name=path.name)

    head = run_git(["rev-parse", "--abbrev-ref", "HEAD"], path).strip()
    repo.branch = head or "HEAD"

    status = run_git(["status", "--porcelain=v1", "-b"], path)
    lines = status.splitlines()
    if lines and lines[0].startswith("##") and "[" in lines[0] and "]" in lines[0]:
        inner = lines[0][lines[0].index("[") + 1:lines[0].rindex("]")]
        for part in inner.split(","):
            part = part.strip()
            if part.startswith("ahead "):
                try:
                    repo.ahead = int(part[6:])
                except ValueError:
                    pass
            elif part.startswith("behind "):
                try:
                    repo.behind = int(part[7:])
                except ValueError:
                    pass
    for line in lines[1:]:
        if len(line) < 2:
            continue
        x, y = line[0], line[1]
        if x == "?" and y == "?":
            repo.untracked += 1
        else:
            if x not in (" ", "?"):
                repo.staged += 1
            if y not in (" ", "?"):
                repo.unstaged += 1

    log = run_git(["log", "-1", "--format=%ct%n%s"], path).strip()
    if log:
        parts = log.split("\n", 1)
        try:
            repo.last_commit_at = int(parts[0])
        except (ValueError, IndexError):
            pass
        if len(parts) > 1:
            repo.last_commit_subject = parts[1].strip()

    activity = run_git(
        ["log", f"--since={DAYS} days ago", "--format=%ct"], path,
    )
    buckets = [0] * DAYS
    now = int(time.time())
    for ln in activity.splitlines():
        try:
            ts = int(ln)
        except ValueError:
            continue
        idx = DAYS - 1 - ((now - ts) // 86400)
        if 0 <= idx < DAYS:
            buckets[idx] += 1
    repo.activity = buckets

    return repo


def fmt_age(ts: int) -> str:
    if not ts:
        return "—"
    d = int(time.time()) - ts
    if d < 0:
        return "now"
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    if d < 86400 * 30:
        return f"{d // 86400}d ago"
    if d < 86400 * 365:
        return f"{d // (86400 * 30)}mo ago"
    return f"{d // (86400 * 365)}y ago"


def truncate(s: str, n: int) -> str:
    if n <= 0:
        return ""
    return s if len(s) <= n else s[: n - 1] + "…"


def len_visible(s: str) -> int:
    return len(ANSI_RE.sub("", s))


def pad_visible(s: str, n: int) -> str:
    return s + " " * max(0, n - len_visible(s))


def sparkline(buckets: list[int]) -> str:
    if not buckets:
        return ""
    m = max(buckets)
    if m == 0:
        return Style.DARK + ("·" * len(buckets)) + Style.RESET
    chars: list[str] = []
    for b in buckets:
        if b == 0:
            chars.append(Style.DARK + "·" + Style.RESET)
        else:
            ch = SPARK[min(8, int(round((b / m) * 8)))]
            chars.append(Style.CYAN + ch + Style.RESET)
    return "".join(chars)


def render(repos: list[Repo], width: int) -> str:
    out: list[str] = []
    n = len(repos)
    dirty = sum(1 for r in repos if r.dirty)
    ahead = sum(1 for r in repos if r.ahead > 0)
    behind = sum(1 for r in repos if r.behind > 0)
    box_w = max(60, min(width, 120))

    title = "P U L S E"
    sub = f"{n} repos · {dirty} dirty · {ahead} ↑ · {behind} ↓"

    out.append(Style.MAGENTA + "╔" + "═" * (box_w - 2) + "╗" + Style.RESET)
    out.append(
        Style.MAGENTA + "║" + Style.RESET
        + Style.BOLD + Style.PINK + f"{title:^{box_w-2}}" + Style.RESET
        + Style.MAGENTA + "║" + Style.RESET
    )
    out.append(
        Style.MAGENTA + "║" + Style.RESET
        + Style.CYAN + f"{sub:^{box_w-2}}" + Style.RESET
        + Style.MAGENTA + "║" + Style.RESET
    )
    out.append(Style.MAGENTA + "╚" + "═" * (box_w - 2) + "╝" + Style.RESET)
    out.append("")

    if not repos:
        out.append(f"  {Style.GREY}no git repos found{Style.RESET}")
        return "\n".join(out)

    repos = sorted(repos, key=lambda r: -r.last_commit_at)

    name_w = min(20, max(8, max(len(r.name) for r in repos)))
    branch_w = min(16, max(6, max(len(r.branch) for r in repos)))
    spark_w = DAYS
    state_w = 14
    age_w = 9
    prefix = 4
    seps = 1 + 2 + 2 + 2 + 2
    fixed = prefix + name_w + branch_w + spark_w + state_w + age_w + seps
    msg_w = max(0, width - fixed - 3)

    for r in repos:
        if r.dirty:
            marker = Style.YELLOW + "●" + Style.RESET
        elif r.ahead or r.behind:
            marker = Style.MAGENTA + "●" + Style.RESET
        else:
            marker = Style.DARK + "○" + Style.RESET

        name = Style.PINK + truncate(r.name, name_w).ljust(name_w) + Style.RESET
        branch = Style.GREY + truncate(r.branch, branch_w).ljust(branch_w) + Style.RESET
        spark = sparkline(r.activity)
        age = Style.DARK + fmt_age(r.last_commit_at).ljust(age_w) + Style.RESET

        bits: list[str] = []
        if r.staged:
            bits.append(Style.CYAN + f"+{r.staged}" + Style.RESET)
        if r.unstaged:
            bits.append(Style.YELLOW + f"~{r.unstaged}" + Style.RESET)
        if r.untracked:
            bits.append(Style.VIOLET + f"?{r.untracked}" + Style.RESET)
        if r.ahead:
            bits.append(Style.CYAN + f"↑{r.ahead}" + Style.RESET)
        if r.behind:
            bits.append(Style.MAGENTA + f"↓{r.behind}" + Style.RESET)
        state_raw = " ".join(bits) if bits else Style.GREEN + "clean" + Style.RESET
        state = pad_visible(state_raw, state_w)

        line = f"  {marker} {name} {branch}  {spark}  {state}  {age}"
        if msg_w >= 10 and r.last_commit_subject:
            msg = Style.DIM + '"' + truncate(r.last_commit_subject, msg_w) + '"' + Style.RESET
            line += f"  {msg}"
        out.append(line)

    return "\n".join(out)


def collect(
    root: Path, depth: int, workers: int, dirty_only: bool, limit: int | None,
) -> list[Repo]:
    paths = find_repos(root, max_depth=depth)
    if not paths:
        return []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        repos = list(pool.map(probe, paths))
    if dirty_only:
        repos = [r for r in repos if r.dirty]
    repos.sort(key=lambda r: -r.last_commit_at)
    if limit is not None:
        repos = repos[:limit]
    return repos


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pulse",
        description="Glanceable git repo dashboard for your terminal.",
    )
    parser.add_argument("root", nargs="?", default=".",
                        help="Directory to scan (default: current).")
    parser.add_argument("--depth", type=int, default=6,
                        help="Max scan depth (default: 6).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Show only N most recent.")
    parser.add_argument("--dirty", action="store_true",
                        help="Show only repos with uncommitted work.")
    parser.add_argument("--watch", type=float, nargs="?", const=5.0, default=None,
                        metavar="SEC",
                        help="Refresh every N seconds (default 5).")
    parser.add_argument("--no-color", action="store_true",
                        help="Plain output, no ANSI codes.")
    parser.add_argument("--workers", type=int, default=8,
                        help="Concurrent git probes (default: 8).")
    parser.add_argument("--version", action="version", version=f"pulse {__version__}")
    args = parser.parse_args(argv)

    if args.no_color or not sys.stdout.isatty():
        Style.disable()

    root = Path(args.root).expanduser()
    if not root.exists():
        print(f"pulse: {root} does not exist", file=sys.stderr)
        return 2

    def show_once() -> None:
        width = shutil.get_terminal_size((100, 20)).columns
        repos = collect(root, args.depth, args.workers, args.dirty, args.limit)
        print(render(repos, width))

    if args.watch is not None:
        try:
            while True:
                sys.stdout.write("\x1b[2J\x1b[H")
                show_once()
                print(f"\n  {Style.DARK}↻ every {args.watch:g}s · ctrl-c to quit{Style.RESET}")
                time.sleep(args.watch)
        except KeyboardInterrupt:
            sys.stdout.write("\n")
            return 0
    show_once()
    return 0


if __name__ == "__main__":
    sys.exit(main())
