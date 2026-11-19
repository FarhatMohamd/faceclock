"""
Timesheet engine
================

Turns raw punches into daily worked time, per employee:

* presence time      = OUT - IN (several shifts per day are supported)
* break accounting   = per break type; a *paid* break (e.g. smoking, coffee)
                       counts as working time up to its daily allowance, any
                       excess is deducted; an *unpaid* break (lunch) is deducted
* schedule checks    = late arrival, early departure, overtime, absence
* anomalies          = forgotten clock-out, too long break, orphan punch,
                       punches awaiting review

Night shifts are attached to the day on which they *started*.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import current_app

from ..db import from_iso, query, to_iso, utcnow


@dataclass
class DaySummary:
    day: date
    presence_min: int = 0
    worked_min: int = 0
    paid_break_min: int = 0
    unpaid_break_min: int = 0
    breaks: dict = field(default_factory=dict)        # code -> minutes
    first_in: str | None = None                        # local HH:MM
    last_out: str | None = None
    late_min: int = 0
    early_leave_min: int = 0
    overtime_min: int = 0
    expected_min: int = 0
    pin_punches: int = 0
    anomalies: list = field(default_factory=list)

    def as_dict(self):
        d = self.__dict__.copy()
        d["day"] = self.day.isoformat()
        return d


def _minutes(seconds: float) -> int:
    return int(round(seconds / 60.0))


def _employee_tz(employee):
    if employee["home_site_id"]:
        site = query("SELECT timezone FROM sites WHERE id = ?", (employee["home_site_id"],), one=True)
        if site and site["timezone"]:
            return ZoneInfo(site["timezone"])
    return ZoneInfo("Europe/Paris")


def _schedule(employee):
    if not employee["schedule_id"]:
        return None
    return query("SELECT * FROM schedules WHERE id = ?", (employee["schedule_id"],), one=True)


def _is_workday(schedule, day: date) -> bool:
    if schedule is None:
        return False
    return str(day.isoweekday()) in schedule["workdays"].split(",")


def _sched_bounds(schedule, day: date, tz):
    sh, sm = map(int, schedule["start_time"].split(":"))
    eh, em = map(int, schedule["end_time"].split(":"))
    start = datetime.combine(day, time(sh, sm), tz)
    end = datetime.combine(day, time(eh, em), tz)
    if end <= start:                       # night shift
        end += timedelta(days=1)
    return start, end


# ---------------------------------------------------------------------------
# Shift reconstruction
# ---------------------------------------------------------------------------
def _build_shifts(punches, max_shift):
    """Group chronological punches into shifts starting with an IN."""
    shifts, orphans, current = [], [], None
    for p in punches:
        ts = from_iso(p["ts"])
        kind = p["punch_type"]
        if current and ts - current["start"] > max_shift and kind != "IN":
            current["missing_out"] = True
            shifts.append(current)
            current = None
        if kind == "IN":
            if current:                        # IN while a shift is open => forgotten OUT
                current["missing_out"] = True
                shifts.append(current)
            current = {"start": ts, "end": None, "events": [p], "missing_out": False}
        elif current is None:
            orphans.append(p)
        else:
            current["events"].append(p)
            if kind == "OUT":
                current["end"] = ts
                shifts.append(current)
                current = None
    if current:
        current["missing_out"] = True
        shifts.append(current)
    return shifts, orphans


def _shift_breaks(shift):
    """Return a list of (break_code, start, end) for a shift."""
    breaks, open_break = [], None
    for p in shift["events"]:
        ts = from_iso(p["ts"])
        if p["punch_type"] == "BREAK_START":
            open_break = (p["break_code"] or "COFFEE", ts)
        elif p["punch_type"] in ("BREAK_END", "OUT") and open_break:
            breaks.append((open_break[0], open_break[1], ts))
            open_break = None
    return breaks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def compute_timesheet(employee, start_day: date, end_day: date):
    """Return a list of DaySummary (one per day, inclusive range)."""
    cfg = current_app.config
    tz = _employee_tz(employee)
    schedule = _schedule(employee)
    max_shift = timedelta(hours=cfg["MAX_SHIFT_HOURS"])
    break_types = {r["code"]: r for r in query("SELECT * FROM break_types")}

    lo = datetime.combine(start_day, time.min, tz) - timedelta(hours=1)
    hi = datetime.combine(end_day + timedelta(days=1), time.min, tz) + max_shift
    punches = query(
        "SELECT p.*, bt.code AS break_code FROM punches p "
        "LEFT JOIN break_types bt ON bt.id = p.break_type_id "
        "WHERE p.employee_id = ? AND p.status != 'REJECTED' AND p.ts >= ? AND p.ts < ? "
        "ORDER BY p.ts, p.id",
        (employee["id"], to_iso(lo), to_iso(hi)))

    shifts, orphans = _build_shifts(punches, max_shift)
    by_day = defaultdict(list)
    for s in shifts:
        by_day[s["start"].astimezone(tz).date()].append(s)

    today_local = utcnow().astimezone(tz).date()
    results = []
    day = start_day
    while day <= end_day:
        summary = DaySummary(day=day)
        day_shifts = by_day.get(day, [])
        break_totals = defaultdict(float)

        for s in day_shifts:
            if s["missing_out"]:
                summary.anomalies.append("MISSING_OUT")
            else:
                summary.presence_min += _minutes((s["end"] - s["start"]).total_seconds())
            for code, b_start, b_end in _shift_breaks(s):
                if s["missing_out"]:
                    continue                     # open shift: no reliable total
                duration = (b_end - b_start).total_seconds()
                break_totals[code] += duration
                bt = break_types.get(code)
                if bt and bt["max_duration_minutes"] and duration > bt["max_duration_minutes"] * 60:
                    summary.anomalies.append(f"LONG_BREAK:{code}")
            for p in s["events"]:
                if p["method"] in ("PIN", "OFFLINE"):
                    summary.pin_punches += 1
                if p["status"] == "PENDING_REVIEW":
                    summary.anomalies.append("PENDING_REVIEW")

        # Paid / unpaid break split with daily allowance
        for code, seconds in break_totals.items():
            minutes = _minutes(seconds)
            summary.breaks[code] = minutes
            bt = break_types.get(code)
            if bt is None or not bt["paid"]:
                summary.unpaid_break_min += minutes
                continue
            allowance = bt["daily_allowance_minutes"]
            if allowance and minutes > allowance:
                summary.paid_break_min += allowance
                summary.unpaid_break_min += minutes - allowance
                summary.anomalies.append(f"BREAK_ALLOWANCE_EXCEEDED:{code}")
            else:
                summary.paid_break_min += minutes

        summary.worked_min = max(0, summary.presence_min - summary.unpaid_break_min)

        closed = [s for s in day_shifts if not s["missing_out"]]
        if day_shifts:
            summary.first_in = day_shifts[0]["start"].astimezone(tz).strftime("%H:%M")
        if closed:
            summary.last_out = closed[-1]["end"].astimezone(tz).strftime("%H:%M")

        # Schedule comparison
        if _is_workday(schedule, day):
            summary.expected_min = schedule["expected_minutes"]
            s_start, s_end = _sched_bounds(schedule, day, tz)
            grace = timedelta(minutes=schedule["grace_minutes"])
            if day_shifts:
                first = day_shifts[0]["start"]
                if first > s_start + grace:
                    summary.late_min = _minutes((first - s_start).total_seconds())
                    summary.anomalies.append("LATE")
                if closed and closed[-1]["end"] < s_end - grace:
                    summary.early_leave_min = _minutes((s_end - closed[-1]["end"]).total_seconds())
                    summary.anomalies.append("EARLY_LEAVE")
            elif day < today_local:
                summary.anomalies.append("ABSENT")
            summary.overtime_min = max(0, summary.worked_min - summary.expected_min)
        else:
            summary.overtime_min = summary.worked_min if schedule else 0

        results.append(summary)
        day += timedelta(days=1)

    for p in orphans:
        d = from_iso(p["ts"]).astimezone(tz).date()
        for s in results:
            if s.day == d:
                s.anomalies.append(f"ORPHAN_{p['punch_type']}")

    for s in results:                              # de-duplicate, keep order
        s.anomalies = list(dict.fromkeys(s.anomalies))
    return results


def period_totals(days):
    keys = ("presence_min", "worked_min", "paid_break_min", "unpaid_break_min",
            "late_min", "early_leave_min", "overtime_min", "expected_min", "pin_punches")
    totals = {k: sum(getattr(d, k) for d in days) for k in keys}
    totals["anomalies"] = sum(len(d.anomalies) for d in days)
    return totals


def fmt_minutes(minutes) -> str:
    minutes = int(minutes or 0)
    sign = "-" if minutes < 0 else ""
    minutes = abs(minutes)
    return f"{sign}{minutes // 60}h{minutes % 60:02d}"


def presence_now(site_id=None, department_ids=None):
    """Who is currently on site (for the live board and evacuation roll-call)."""
    cfg = current_app.config
    cutoff = to_iso(utcnow() - timedelta(hours=cfg["MAX_SHIFT_HOURS"]))
    rows = query(
        """
        SELECT e.id, e.employee_code, e.first_name, e.last_name, e.department_id,
               d.name AS department, p.punch_type, p.ts, p.site_id, s.name AS site,
               bt.label AS break_label
        FROM employees e
        JOIN punches p ON p.id = (
            SELECT p2.id FROM punches p2
            WHERE p2.employee_id = e.id AND p2.status != 'REJECTED'
            ORDER BY p2.ts DESC, p2.id DESC LIMIT 1)
        LEFT JOIN departments d ON d.id = e.department_id
        LEFT JOIN sites s ON s.id = p.site_id
        LEFT JOIN break_types bt ON bt.id = p.break_type_id
        WHERE e.active = 1 AND p.punch_type != 'OUT' AND p.ts >= ?
        ORDER BY s.name, d.name, e.last_name
        """, (cutoff,))
    out = []
    for r in rows:
        if site_id is not None and r["site_id"] != site_id:
            continue
        if department_ids is not None and r["department_id"] not in department_ids:
            continue
        out.append(dict(r, on_break=r["punch_type"] == "BREAK_START"))
    return out


def utc_range_for_local_days(start_day, end_day, tz_name="Europe/Paris"):
    tz = ZoneInfo(tz_name)
    lo = datetime.combine(start_day, time.min, tz).astimezone(timezone.utc)
    hi = datetime.combine(end_day + timedelta(days=1), time.min, tz).astimezone(timezone.utc)
    return to_iso(lo), to_iso(hi)
