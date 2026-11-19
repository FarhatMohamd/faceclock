"""Worked-time computation: paid / unpaid breaks, allowances, schedule checks, anomalies."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from faceclock.db import query
from faceclock.services.punches import record_punch
from faceclock.services.timesheet import compute_timesheet, fmt_minutes

PARIS = ZoneInfo("Europe/Paris")
DAY = date(2026, 3, 10)


def at(hhmm, day=DAY):
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=PARIS)


@pytest.fixture
def punch(app, seed_data):
    def _punch(emp_key, kind, hhmm, break_code=None, day=DAY):
        emp = query("SELECT * FROM employees WHERE id = ?", (seed_data[emp_key],), one=True)
        return record_punch(emp, kind, site_id=seed_data["site_a"], method="ADMIN",
                            ts=at(hhmm, day), break_code=break_code, created_by="test")
    return _punch


def sheet(seed_data, key, start=DAY, end=DAY):
    emp = query("SELECT * FROM employees WHERE id = ?", (seed_data[key],), one=True)
    return compute_timesheet(emp, start, end)


def test_paid_smoking_break_counts_as_work(app, seed_data, punch):
    with app.test_request_context():
        punch("alice", "IN", "09:00")
        punch("alice", "BREAK_START", "10:30", "SMOKE")
        punch("alice", "BREAK_END", "10:38")
        punch("alice", "BREAK_START", "12:00", "LUNCH")
        punch("alice", "BREAK_END", "13:00")
        punch("alice", "OUT", "17:00")
        d = sheet(seed_data, "alice")[0]
    assert d.presence_min == 480
    assert d.paid_break_min == 8           # smoking: paid, within the 20 min allowance
    assert d.unpaid_break_min == 60        # lunch: unpaid
    assert d.worked_min == 420
    assert d.anomalies == []
    assert d.overtime_min == 0


def test_break_allowance_exceeded(app, seed_data, punch):
    with app.test_request_context():
        punch("alice", "IN", "09:00")
        punch("alice", "BREAK_START", "10:00", "SMOKE")
        punch("alice", "BREAK_END", "10:09")
        punch("alice", "BREAK_START", "11:00", "SMOKE")
        punch("alice", "BREAK_END", "11:09")
        punch("alice", "BREAK_START", "15:00", "SMOKE")
        punch("alice", "BREAK_END", "15:09")
        punch("alice", "OUT", "17:00")
        d = sheet(seed_data, "alice")[0]
    assert d.breaks["SMOKE"] == 27
    assert d.paid_break_min == 20 and d.unpaid_break_min == 7
    assert "BREAK_ALLOWANCE_EXCEEDED:SMOKE" in d.anomalies


def test_long_single_break_flagged(app, seed_data, punch):
    with app.test_request_context():
        punch("alice", "IN", "09:00")
        punch("alice", "BREAK_START", "10:00", "SMOKE")
        punch("alice", "BREAK_END", "10:15")       # max single smoke break = 10 min
        punch("alice", "OUT", "17:00")
        d = sheet(seed_data, "alice")[0]
    assert "LONG_BREAK:SMOKE" in d.anomalies


def test_late_and_early_leave(app, seed_data, punch):
    with app.test_request_context():
        punch("alice", "IN", "09:20")
        punch("alice", "OUT", "16:30")
        d = sheet(seed_data, "alice")[0]
    assert d.late_min == 20 and d.early_leave_min == 30
    assert {"LATE", "EARLY_LEAVE"} <= set(d.anomalies)


def test_grace_period(app, seed_data, punch):
    with app.test_request_context():
        punch("alice", "IN", "09:04")
        punch("alice", "OUT", "17:00")
        d = sheet(seed_data, "alice")[0]
    assert d.late_min == 0 and "LATE" not in d.anomalies


def test_overtime(app, seed_data, punch):
    with app.test_request_context():
        punch("alice", "IN", "08:00")
        punch("alice", "OUT", "18:00")
        d = sheet(seed_data, "alice")[0]
    assert d.worked_min == 600 and d.overtime_min == 180


def test_out_during_break_closes_break(app, seed_data, punch):
    with app.test_request_context():
        punch("alice", "IN", "09:00")
        punch("alice", "BREAK_START", "16:50", "COFFEE")
        res = punch("alice", "OUT", "17:00")
        d = sheet(seed_data, "alice")[0]
    assert res["auto_closed_break"]
    assert d.breaks["COFFEE"] == 10 and d.worked_min == 480


def test_missing_clock_out(app, seed_data, punch):
    with app.test_request_context():
        punch("alice", "IN", "09:00")
        punch("alice", "IN", "09:00", day=DAY + timedelta(days=1))   # forgot OUT yesterday
        punch("alice", "OUT", "17:00", day=DAY + timedelta(days=1))
        days = sheet(seed_data, "alice", DAY, DAY + timedelta(days=1))
    assert "MISSING_OUT" in days[0].anomalies and days[0].worked_min == 0
    assert days[1].worked_min == 480 and days[1].anomalies == []


def test_absence_on_workday(app, seed_data):
    with app.test_request_context():
        d = sheet(seed_data, "bob")[0]
    assert d.anomalies == ["ABSENT"]


def test_night_shift_attached_to_start_day(app, seed_data, punch):
    with app.test_request_context():
        punch("carl", "IN", "22:00")
        punch("carl", "OUT", "06:00", day=DAY + timedelta(days=1))
        days = sheet(seed_data, "carl", DAY, DAY + timedelta(days=1))
    assert days[0].presence_min == 480
    assert days[1].presence_min == 0


def test_fmt_minutes():
    assert fmt_minutes(0) == "0h00"
    assert fmt_minutes(485) == "8h05"
    assert fmt_minutes(-30) == "-0h30"
