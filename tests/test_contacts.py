import tempfile
import unittest
from xml.etree import ElementTree
from zipfile import ZipFile
from datetime import date, datetime
from pathlib import Path

from birthday_mailer.contacts import DataError, Member, members_from_rows, parse_birthday, read_csv, read_rows, validate_email, write_members


class ContactTests(unittest.TestCase):
    def test_roster_date_formats(self):
        values = ["2008.08.11", "2008.0811", "20080811", "2008-8-11", "2008/8/11", "2008年8月11日", "08-11", datetime(2008, 8, 11)]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(parse_birthday(value), (8, 11))

    def test_invalid_dates_and_solar_leap_year(self):
        for value in ["2007-02-29", "02-30", "13-01", "########", "unknown", "2008.811"]:
            with self.subTest(value=value), self.assertRaises(DataError):
                parse_birthday(value)
        self.assertEqual(parse_birthday("02-29"), (2, 29))
        self.assertEqual(parse_birthday("04-30", "lunar"), (4, 30))

    def test_recipient_list_and_header_injection_rejected(self):
        for value in ["a@example.edu,b@example.edu", "A <a@example.edu>", "a@example.edu\r\nBcc:x@example.edu", "a..b@example.edu", "a@localhost"]:
            with self.subTest(value=value), self.assertRaises(DataError):
                validate_email(value)

    def test_chinese_headers_drop_unneeded_columns(self):
        members, derived = members_from_rows([{"姓名": "示例", "邮箱": "demo@example.edu", "出生年月日": "2008.0811", "手机号": "not-retained", "QQ": "not-retained", "在职状态": "在职"}])
        self.assertEqual(members[0], Member("示例", "demo@example.edu", 8, 11))
        self.assertEqual(derived, 0)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "members.csv"
            write_members(target, members)
            text = target.read_text(encoding="utf-8-sig")
            self.assertNotIn("not-retained", text)
            self.assertNotIn("2008", text)
            self.assertEqual(members_from_rows(read_rows(target))[0], members)
        for header in ("在职", "在职状态", "在职情况"):
            with self.subTest(header=header):
                inactive, _ = members_from_rows([{"姓名": "示例", "学校邮箱": "demo@example.edu", "出生年月日": "11-05", header: "离职"}])
                self.assertFalse(inactive[0].enabled)

    def test_derived_student_emails_are_disabled(self):
        row = {"姓名": "示例", "学号": "U202600001", "出生年月日": "2008-11-05", "在职": "在职"}
        with self.assertRaises(DataError):
            members_from_rows([row])
        members, count = members_from_rows([row], email_from_student_id=True)
        self.assertEqual(count, 1)
        self.assertEqual(members[0].email, "U202600001@hust.edu.cn")
        self.assertFalse(members[0].enabled)

    def test_duplicate_address_rejected_without_leaking_address(self):
        rows = [{"name": "示例", "email": email, "birthday": "11-05"} for email in ["demo@example.edu", "DEMO@example.edu"]]
        with self.assertRaises(DataError) as error:
            members_from_rows(rows)
        self.assertNotIn("example.edu", str(error.exception))

    def test_invalid_csv_and_status_fail_closed(self):
        for text in ["name,name\na,b", "name,email,birthday\na,a@example.edu,11-05,extra"]:
            with self.assertRaises(DataError):
                read_csv(text)
        with self.assertRaises(DataError):
            members_from_rows([{"name":"示例", "email":"demo@example.edu", "birthday":"11-05", "enabled":"maybe"}])

    def test_formula_like_names_do_not_overwrite_existing_import(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "members.csv"
            target.write_text("original", encoding="utf-8")
            with self.assertRaises(DataError):
                write_members(target, [Member("=1+1", "demo@example.edu", 11, 5)])
            self.assertEqual(target.read_text(), "original")

    def test_xlsx_typed_dates_and_blank_rows(self):
        from openpyxl import Workbook
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "roster.xlsx"
            workbook = Workbook()
            page = workbook.active
            page.title = "资委会"
            page.append(["姓名", "学校邮箱", "出生年月日", "是否启用"])
            page.append(["示例", "demo@example.edu", datetime(2008, 11, 5), True])
            page.append([None, None, None, None])
            workbook.save(target)
            members, _ = members_from_rows(read_rows(target, "资委会"))
            self.assertEqual(members, [Member("示例", "demo@example.edu", 11, 5)])

    def test_tencent_empty_fill_keeps_dates_and_source_file(self):
        from openpyxl import Workbook
        import io
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "tencent.xlsx"
            workbook = Workbook()
            workbook.active.append(["姓名", "学校邮箱", "出生年月日"])
            workbook.active.append(["示例", "demo@example.edu", datetime(2008, 11, 5)])
            workbook.save(target)
            buffer = io.BytesIO()
            namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
            with ZipFile(target) as source, ZipFile(buffer, "w") as destination:
                for entry in source.infolist():
                    content = source.read(entry.filename)
                    if entry.filename == "xl/styles.xml":
                        styles = ElementTree.fromstring(content)
                        styles.find(f"{{{namespace}}}fills")[0].clear()
                        content = ElementTree.tostring(styles, encoding="utf-8")
                    destination.writestr(entry, content)
            original = buffer.getvalue()
            target.write_bytes(original)
            members, _ = members_from_rows(read_rows(target))
            self.assertEqual(members, [Member("示例", "demo@example.edu", 11, 5)])
            self.assertEqual(target.read_bytes(), original)


class CalendarTests(unittest.TestCase):
    def test_feb29_policy(self):
        member = Member("示例", "demo@example.edu", 2, 29)
        self.assertEqual(member.occurrence_on(date(2026, 2, 28)), "solar-2026")
        self.assertIsNone(member.occurrence_on(date(2026, 2, 28), "mar01"))
        self.assertEqual(member.occurrence_on(date(2026, 3, 1), "mar01"), "solar-2026")
        self.assertIsNone(member.occurrence_on(date(2026, 2, 28), "strict"))
        self.assertEqual(member.occurrence_on(date(2028, 2, 29)), "solar-2028")

    def test_disabled_members_are_skipped(self):
        self.assertIsNone(Member("示例", "demo@example.edu", 11, 5, enabled=False).occurrence_on(date(2026, 11, 5)))

    def test_lunar_birthdays_and_leap_month_policy(self):
        from lunardate import LunarDate
        member = Member("农历示例", "demo@example.edu", 1, 1, "lunar")
        self.assertEqual(member.occurrence_on(date(2026, 2, 17)), "lunar-2026")
        leap = Member("闰月示例", "leap@example.edu", 6, 1, "lunar", True)
        regular_day = LunarDate(2025, 6, 1).to_solar_date()
        leap_day = LunarDate(2025, 6, 1, True).to_solar_date()
        self.assertEqual(leap.occurrence_on(regular_day), "lunar-2025")
        self.assertIsNone(leap.occurrence_on(leap_day))
        self.assertEqual(leap.occurrence_on(leap_day, lunar_leap="strict"), "lunar-2025")


if __name__ == "__main__":
    unittest.main()
