"""Read a minimal roster without retaining phone numbers or birth years."""

from __future__ import annotations

import calendar as calendar_module
import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable


class DataError(ValueError):
    """Invalid input; messages deliberately exclude personal data."""


@dataclass(frozen=True)
class Member:
    name: str
    email: str
    month: int
    day: int
    calendar: str = "solar"
    is_leap_month: bool = False
    enabled: bool = True

    def occurrence_on(
        self, today: date, feb29: str = "feb28", lunar_leap: str = "regular"
    ) -> str | None:
        if not self.enabled:
            return None
        if self.calendar == "solar":
            target = (self.month, self.day)
            if target == (2, 29) and not calendar_module.isleap(today.year):
                target = {"feb28": (2, 28), "mar01": (3, 1), "strict": (2, 29)}[feb29]
            return f"solar-{today.year}" if (today.month, today.day) == target else None
        try:
            from lunardate import LunarDate
        except ImportError as exc:
            raise DataError('农历成员需要安装：pip install -e ".[lunar]"') from exc
        try:
            lunar = LunarDate.from_solar_date(today.year, today.month, today.day)
        except ValueError as exc:
            raise DataError("当前日期超出农历库支持范围") from exc
        leap = self.is_leap_month if lunar_leap == "strict" else False
        if (lunar.month, lunar.day, bool(lunar.is_leap_month)) == (self.month, self.day, leap):
            return f"lunar-{lunar.year}"
        return None


def validate_email(value: str) -> str:
    value = value.strip()
    # Exactly one ASCII mailbox; do not allow display names or recipient lists.
    pattern = r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}"
    if len(value) > 254 or not re.fullmatch(pattern, value):
        raise DataError("邮箱格式无效（应为一个完整地址）")
    local, domain = value.rsplit("@", 1)
    if len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in value:
        raise DataError("邮箱格式无效")
    return f"{local}@{domain.lower()}"


def parse_bool(value: object, *, default: bool = False) -> bool:
    if value is None or str(value).strip() == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "是", "启用", "在职", "在任"}:
        return True
    if text in {"0", "false", "no", "n", "否", "停用", "离职", "离任", "已退出", "退出"}:
        return False
    raise DataError("启用状态应为 true / false（或在职 / 离职）")


def parse_calendar(value: object) -> str:
    text = str(value or "solar").strip().lower()
    aliases = {"solar": "solar", "公历": "solar", "阳历": "solar", "lunar": "lunar", "农历": "lunar", "阴历": "lunar"}
    if text not in aliases:
        raise DataError("历法必须是 solar / lunar（公历 / 农历）")
    return aliases[text]


def parse_birthday(value: object, calendar: str = "solar") -> tuple[int, int]:
    if isinstance(value, (date, datetime)):
        parts = (value.month, value.day)
    else:
        text = str(value or "").strip()
        if re.fullmatch(r"\d{8}", text):
            text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
        # Tencent rosters sometimes contain 2008.0811 for August 11.
        compact = re.fullmatch(r"(\d{4})\.(\d{2})(\d{2})", text)
        if compact:
            text = "-".join(compact.groups())
        text = re.sub(r"[年./]", "-", text).replace("月", "-").replace("日", "")
        match = re.fullmatch(r"(?:(\d{4})-)?(\d{1,2})-(\d{1,2})(?: 00:00:00)?", text)
        if not match:
            raise DataError("生日格式无效；使用 MM-DD 或 YYYY-MM-DD")
        year, month, day = match.groups()
        parts = (int(month), int(day))
        if year and calendar == "solar":
            try:
                date(int(year), *parts)
            except ValueError as exc:
                raise DataError("公历出生日期不存在") from exc
    if calendar == "solar":
        try:
            date(2000, *parts)  # A leap year allows an annual 02-29 birthday.
        except ValueError as exc:
            raise DataError("公历生日不存在") from exc
    elif not (1 <= parts[0] <= 12 and 1 <= parts[1] <= 30):
        raise DataError("农历月份应为 1–12，日期应为 1–30")
    return parts


ALIASES = {
    "name": ("name", "姓名"),
    "email": ("email", "邮箱", "邮箱地址", "电子邮箱", "学校邮箱", "edu邮箱"),
    "birthday": ("birthday", "生日", "出生年月日", "出生日期"),
    "calendar": ("calendar", "历法", "生日历法", "生日类型"),
    "is_leap_month": ("is_leap_month", "闰月", "是否闰月"),
    "enabled": ("enabled", "启用", "是否启用", "在职状态", "在职"),
    "student_id": ("student_id", "学号"),
}
FIELDS = ("name", "email", "birthday", "calendar", "is_leap_month", "enabled")


def canonical_row(row: dict[str, object]) -> dict[str, object]:
    clean = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
    return {key: next((clean[alias] for alias in aliases if alias in clean), None) for key, aliases in ALIASES.items()}


def read_rows(path: Path, sheet: str | None = None) -> list[dict[str, object]]:
    if path.suffix.lower() == ".xlsx":
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise DataError('读取 Excel 需要安装：pip install -e ".[xlsx]"') from exc
        book = load_workbook(path, read_only=True, data_only=True)
        try:
            if sheet and sheet not in book.sheetnames:
                raise DataError("找不到指定工作表")
            page = book[sheet] if sheet else book.active
            rows = list(page.iter_rows(values_only=True))
            if not rows:
                raise DataError("通讯录为空")
            headers = [str(x).strip() if x is not None else "" for x in rows[0]]
            if len([x for x in headers if x]) != len(set(x for x in headers if x)):
                raise DataError("表头存在重名列")
            return [dict(zip(headers, row)) for row in rows[1:]]
        finally:
            book.close()
    if path.suffix.lower() not in {".csv", ".tsv"}:
        raise DataError("输入文件应为 .xlsx、.csv 或 .tsv")
    return read_csv(path.read_text(encoding="utf-8-sig"), delimiter="\t" if path.suffix.lower() == ".tsv" else ",")


def read_csv(text: str, delimiter: str = ",") -> list[dict[str, object]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), delimiter=delimiter)
    if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise DataError("CSV 表头为空或存在重名列")
    result = list(reader)
    if any(None in row for row in result):
        raise DataError("CSV 某行的列数超过表头列数")
    return result


def members_from_rows(
    rows: Iterable[dict[str, object]], *, email_from_student_id: bool = False
) -> tuple[list[Member], int]:
    members: list[Member] = []
    seen: set[str] = set()
    derived = 0
    for number, row in enumerate(rows, start=2):
        if not any(v is not None and str(v).strip() for v in row.values()):
            continue
        try:
            entry = canonical_row(row)
            name = str(entry["name"] or "").strip()
            if not name or any(ord(c) < 32 for c in name):
                raise DataError("姓名为空或含控制字符")
            email = str(entry["email"] or "").strip()
            inferred = False
            if not email and email_from_student_id:
                student_id = str(entry["student_id"] or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9]+", student_id):
                    raise DataError("学号为空或格式无效")
                email = student_id + "@hust.edu.cn"
                inferred = True
            email = validate_email(email)
            if email.casefold() in seen:
                raise DataError("邮箱重复，请合并后再运行")
            seen.add(email.casefold())
            kind = parse_calendar(entry["calendar"])
            month, day = parse_birthday(entry["birthday"], kind)
            is_leap = parse_bool(entry["is_leap_month"])
            if kind == "solar" and is_leap:
                raise DataError("公历生日不能设置农历闰月")
            enabled = parse_bool(entry["enabled"], default=True)
            members.append(Member(name, email, month, day, kind, is_leap, enabled and not inferred))
            derived += int(inferred)
        except DataError as exc:
            raise DataError(f"第 {number} 行：{exc}") from exc
    if not members:
        raise DataError("通讯录没有成员")
    return members, derived


def write_members(path: Path, members: Iterable[Member]) -> None:
    members = list(members)
    for member in members:
        if member.name.startswith(("=", "+", "-", "@", "\t")) or member.email.startswith(("=", "+", "-", "@")):
            raise DataError("姓名或邮箱以表格公式字符开头，请先人工核实")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for member in members:
            writer.writerow({"name": member.name, "email": member.email,
                             "birthday": f"{member.month:02d}-{member.day:02d}", "calendar": member.calendar,
                             "is_leap_month": str(member.is_leap_month).lower(), "enabled": str(member.enabled).lower()})
