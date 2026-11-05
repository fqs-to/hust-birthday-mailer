from __future__ import annotations

import argparse
import os
import re
import smtplib
import sys
from datetime import date, datetime
from email.utils import make_msgid
from pathlib import Path

from . import git_state
from .contacts import DataError, Member, members_from_rows, read_csv, read_rows, validate_email, write_members
from .ledger import Ledger, ledger_lock
from .mail import BEIJING, SMTPConfig, SMTPTransport, build_message, render_greeting
from .service import deliver, due_members


def load_env(path: Path = Path(".env")) -> None:
    """A deliberately small KEY=value reader. Process environment takes precedence."""
    if not path.exists():
        return
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.strip().replace("_", "").isalnum():
            raise DataError(f".env 第 {number} 行格式无效")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def load_members(path: Path | None) -> list[Member]:
    if path is not None:
        rows = read_rows(path)
    elif os.environ.get("MEMBERS_CSV"):
        rows = read_csv(os.environ["MEMBERS_CSV"])
    else:
        rows = read_rows(Path("data/members.csv"))
    return members_from_rows(rows)[0]


def parser() -> argparse.ArgumentParser:
    app = argparse.ArgumentParser(description="资委会生日祝福工具 / Birthday Mailer")
    commands = app.add_subparsers(dest="command", required=True)
    imp = commands.add_parser("import", help="导入 Excel/CSV；不保留手机号、QQ 和出生年份")
    imp.add_argument("source", type=Path)
    imp.add_argument("--sheet")
    imp.add_argument("--output", type=Path, default=Path("data/members.csv"))
    imp.add_argument("--email-from-student-id", action="store_true", help="生成候选学校邮箱，默认停用，核实后再启用")
    imp.add_argument("--overwrite", action="store_true", help="明确允许覆盖已有导入文件")
    check = commands.add_parser("validate", help="校验名单（不显示姓名、地址）")
    check.add_argument("--members", type=Path)
    preview = commands.add_parser("preview", help="生成一封本地 HTML 预览，不发信")
    preview.add_argument("--name", default="示例同学")
    preview.add_argument("--output", type=Path, default=Path("previews/birthday.html"))
    run = commands.add_parser("run", help="检查今日生日；默认演练，加 --send 才会发信")
    run.add_argument("--members", type=Path)
    run.add_argument("--date", type=date.fromisoformat, help="仅供演练，不能与 --send 同时使用")
    run.add_argument("--send", action="store_true")
    run.add_argument("--ledger", type=Path, default=Path("data/deliveries.json"))
    run.add_argument("--git-state", action="store_true", help="将记录在 Git 状态分支上持久保存，供 Actions 使用")
    run.add_argument("--feb29", choices=("feb28", "mar01", "strict"), default="feb28")
    run.add_argument("--lunar-leap", choices=("regular", "strict"), default="regular")
    test = commands.add_parser("test-email", help="只给发件人自己发一封测试邮件，不使用名单")
    test.add_argument("--name", default="测试同学")
    test.add_argument("--send", action="store_true")
    test.add_argument("--git-state", action="store_true", help="先验证云端发送记录的持久保存")
    resolve = commands.add_parser("resolve", help="人工核实后处理一条未确认的发送记录")
    resolve.add_argument("--email", required=True)
    resolve.add_argument("--occurrence", required=True, help="例如 solar-2026 或 lunar-2026")
    resolve.add_argument("--action", choices=("mark-sent", "allow-retry"), required=True)
    resolve.add_argument("--ledger", type=Path, default=Path("data/deliveries.json"))
    resolve.add_argument("--git-state", action="store_true")
    return app


def run_command(args: argparse.Namespace) -> int:
    if args.send and args.date:
        raise DataError("--date 仅可用于演练；正式发送始终使用北京时间今天")
    today = args.date or datetime.now(BEIJING).date()
    members = load_members(args.members)
    due = due_members(members, today, args.feb29, args.lunar_leap)
    print(f"北京时间日期：{today}；成员：{len(members)}；今日生日：{len(due)}")
    if not args.send:
        print("演练完成，发送邮件：0。使用 preview 查看祝福内容。")
        return 0
    if not due:
        print("今天没有待发送的生日祝福。")
        return 0
    path, callback = state_path(args)
    with ledger_lock(path):
        ledger = Ledger(path, os.environ.get("STATE_SECRET", ""), callback)
        pending = [(member, cycle) for member, cycle in due if not ledger.contains(member.email, cycle)]
        blocked = sum(ledger.status(member.email, cycle) in {"pending", "uncertain"} for member, cycle in due)
        print(f"已有记录跳过：{len(due) - len(pending)}；其中需人工核实：{blocked}")
        if blocked:
            print("请核对邮箱投递情况后使用 resolve 处理；程序不会自动重发这些记录。")
        if not pending:
            return 1 if blocked else 0
        config = SMTPConfig.from_env()
        with SMTPTransport(config) as transport:
            sent = deliver(pending, ledger, config, os.environ.get("ORGANIZATION", "计算机学院资委会"), transport.send)
        print(f"SMTP 已接受：{sent} 封。最终是否入箱请以收件箱或退信为准。")
        return 1 if blocked else 0


def state_path(args: argparse.Namespace):
    if args.git_state:
        root = Path.cwd().resolve()
        directory = root / ".state"
        git_state.prepare(root, directory)
        return directory / "deliveries.json", lambda: git_state.sync(directory, root)
    return args.ledger, None


def main(argv: list[str] | None = None) -> int:
    # Windows pipes can default to an ASCII/Western code page, even though
    # console output and roster files are Unicode. Keep CLI output UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    try:
        load_env()
        args = parser().parse_args(argv)
        organization = os.environ.get("ORGANIZATION", "计算机学院资委会")
        if any(ord(c) < 32 for c in organization):
            raise DataError("组织名称含控制字符")
        if args.command == "import":
            if args.output.exists() and not args.overwrite:
                raise DataError("输出文件已存在；如确认覆盖，请添加 --overwrite")
            rows = read_rows(args.source, args.sheet)
            members, derived = members_from_rows(rows, email_from_student_id=args.email_from_student_id)
            write_members(args.output, members)
            print(f"已导入 {len(members)} 位成员；生成的 {derived} 个候选地址均已停用，核实后将 enabled 改为 true。")
            print("输出只含姓名、邮箱、生日月日、历法及启用状态。")
        elif args.command == "validate":
            members = load_members(args.members)
            # Check lunar support and date range before reporting success.
            due_members(members, datetime.now(BEIJING).date())
            print(f"名单校验通过：{len(members)} 位成员，{sum(m.enabled for m in members)} 位启用。")
        elif args.command == "preview":
            name = args.name.strip()
            if not name or any(ord(c) < 32 for c in name):
                raise DataError("预览姓名无效")
            _, _, markup = render_greeting(Member(name, "demo@example.edu", 1, 1), organization)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(markup, encoding="utf-8")
            print(f"HTML 预览已保存：{args.output}")
        elif args.command == "run":
            return run_command(args)
        elif args.command == "test-email":
            if not args.send:
                print("测试演练完成，发送邮件：0。添加 --send 后只会发送到 SMTP_FROM（默认 SMTP_USERNAME）本人。")
                return 0
            config = SMTPConfig.from_env()
            if args.git_state:
                path, callback = state_path(args)
                with ledger_lock(path):
                    Ledger(path, os.environ.get("STATE_SECRET", ""), callback).save()
                print("云端发送记录已完成读写验证。")
            member = Member(args.name, config.sender, 1, 1)
            message = build_message(member, config, organization, make_msgid(domain=config.sender.rsplit("@", 1)[1]), test=True)
            with SMTPTransport(config) as transport:
                transport.send(message)
            print("SMTP 已接受一封发给发件人自己的测试邮件，请检查收件箱。")
        elif args.command == "resolve":
            email = validate_email(args.email)
            if not re.fullmatch(r"(?:solar|lunar)-\d{4}", args.occurrence):
                raise DataError("生日周期格式应为 solar-YYYY 或 lunar-YYYY")
            path, callback = state_path(args)
            if not path.exists():
                raise DataError("发送记录不存在")
            with ledger_lock(path):
                Ledger(path, os.environ.get("STATE_SECRET", ""), callback).resolve(email, args.occurrence, args.action)
            print("记录已按指定方式处理。")
        return 0
    except DataError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except UnicodeDecodeError:
        print("错误：文本文件需要使用 UTF-8 编码。", file=sys.stderr)
        return 1
    except smtplib.SMTPAuthenticationError as exc:
        print(f"错误：SMTP 认证失败（状态码 {exc.smtp_code}）；请检查邮箱账号、客户端服务及客户端专用密码。", file=sys.stderr)
        return 1
    except (OSError, smtplib.SMTPException) as exc:
        print(f"错误：{type(exc).__name__}。请检查网络、文件和 SMTP 设置；已有投递尝试请先核实，不要直接重发。", file=sys.stderr)
        return 1
