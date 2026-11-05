"""Optional durable GitHub state branch. No names or addresses are stored."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .contacts import DataError

BRANCH = "birthday-state"


def git(cwd: Path, *args: str, allowed: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(cwd), *args], text=True, capture_output=True, timeout=60)
    if result.returncode not in allowed:
        # Do not expose remote URLs, auth headers or arbitrary git diagnostics.
        raise DataError(f"发送记录同步失败（git {args[0]}），已停止以避免重发")
    return result


def prepare(root: Path, state_dir: Path) -> None:
    if state_dir.exists():
        raise DataError("Git 状态目录已存在；请使用新的运行环境，或在确认无任务运行后移走该目录")
    result = git(root, "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{BRANCH}", allowed=(0, 2))
    exists = result.returncode == 0
    if exists:
        git(root, "fetch", "--depth=1", "origin", f"refs/heads/{BRANCH}")
        git(root, "worktree", "add", "--detach", str(state_dir), "FETCH_HEAD")
        if not (state_dir / "deliveries.json").is_file():
            raise DataError("远程状态分支缺少发送记录，已停止")
    else:
        git(root, "worktree", "add", "--detach", str(state_dir), "HEAD")
        git(state_dir, "switch", "--orphan", BRANCH)
    author = os.environ.get("STATE_COMMIT_NAME") or os.environ.get("GITHUB_REPOSITORY_OWNER") or "zxw"
    email = os.environ.get("STATE_COMMIT_EMAIL")
    if not email:
        raise DataError("Git 状态同步需要配置 STATE_COMMIT_EMAIL")
    git(root, "config", "user.name", author)
    git(root, "config", "user.email", email)


def sync(state_dir: Path) -> None:
    git(state_dir, "add", "--", "deliveries.json")
    changed = git(state_dir, "diff", "--cached", "--quiet", allowed=(0, 1))
    if changed.returncode == 1:
        git(state_dir, "commit", "-m", "Record birthday delivery state")
        git(state_dir, "push", "origin", f"HEAD:refs/heads/{BRANCH}")
