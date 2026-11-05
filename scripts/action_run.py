"""Workflow entry point: inputs stay in the environment, never in shell code."""

import os
import sys

from birthday_mailer.cli import main


def run() -> int:
    mode = os.environ.get("RUN_MODE", "dry-run")
    if mode == "test":
        return main(["test-email", "--send"])
    if mode not in {"dry-run", "send"}:
        print("未知运行模式。", file=sys.stderr)
        return 1
    has_roster = bool(os.environ.get("MEMBERS_CSV", "").strip())
    if mode == "send":
        if os.environ.get("BIRTHDAY_ENABLED") != "true":
            print("请先完成测试，再将仓库变量 BIRTHDAY_ENABLED 设为 true。", file=sys.stderr)
            return 1
        if not has_roster:
            print("未配置 MEMBERS_CSV；正式发送已停止。", file=sys.stderr)
            return 1
        return main(["run", "--send", "--git-state"])
    arguments = ["run"]
    if not has_roster:
        print("未配置真实名单，本次仅使用虚构示例名单演练。")
        arguments += ["--members", "examples/members.csv"]
    return main(arguments)


if __name__ == "__main__":
    raise SystemExit(run())
