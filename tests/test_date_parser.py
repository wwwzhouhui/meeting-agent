from datetime import date

from src.agents.calendar_agent.date_parser import extract_due_raw, parse_due, parse_relative_date

BASE = date(2026, 10, 23)  # Friday


def test_tomorrow():
    assert parse_relative_date("明天", BASE) == date(2026, 10, 24)


def test_next_week_tuesday():
    # next calendar week: Mon 2026-10-26, Tue 2026-10-27
    assert parse_relative_date("下周二", BASE) == date(2026, 10, 27)


def test_next_week_wednesday_before():
    assert parse_relative_date("下周三之前", BASE) == date(2026, 10, 28)


def test_explicit_date():
    assert parse_relative_date("11月8日", BASE) == date(2026, 11, 8)
    assert parse_relative_date("11月1日", BASE) == date(2026, 11, 1)


def test_days_later_and_month_end():
    assert parse_relative_date("3天后", BASE) == date(2026, 10, 26)
    assert parse_relative_date("月底", BASE) == date(2026, 10, 31)


def test_parse_due_pair():
    due, raw = parse_due("李四，下周三之前完成证书续期", BASE)
    assert raw == "下周三之前"
    assert due == "2026-10-28"
    assert extract_due_raw("11月1日前完成报告") == "11月1日前"
    assert parse_due("没有时间信息", BASE) == (None, None)
