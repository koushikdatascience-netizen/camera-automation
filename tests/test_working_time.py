from datetime import date, datetime, timedelta, timezone

from cloud_portal.working_time import calculate_working_time


BASE = datetime(2026, 10, 8, 3, 30, tzinfo=timezone.utc)


def event(minutes, kind):
    return {"occurred_at": BASE + timedelta(minutes=minutes), "activity_type": kind}


def test_nine_hour_shift_with_break():
    events = [event(0,"CHECK_IN"), event(180,"BREAK_START"),
              event(210,"BREAK_END"), event(570,"CHECK_OUT")]
    result = calculate_working_time(events)
    assert result["grossMinutes"] == 570
    assert result["breakMinutes"] == 30
    assert result["netMinutes"] == 540
    assert result["shortfallMinutes"] == 0
    assert result["overtimeMinutes"] == 0


def test_open_shift_not_invented_as_complete():
    result = calculate_working_time([event(0,"CHECK_IN")])
    assert result["hasOpenSession"] is True
    assert result["grossMinutes"] == 0
    assert result["calculationStatus"] == "PROVISIONAL"


def test_duplicate_check_in_does_not_double_count():
    result = calculate_working_time([event(0,"CHECK_IN"),event(10,"CHECK_IN"),
                                     event(60,"CHECK_OUT")])
    assert result["grossMinutes"] == 60


def test_daily_summary_clips_overnight_shift_to_local_day():
    # Asia/Kolkata local day begins at 18:30 UTC on the previous date.
    start = datetime(2026, 10, 7, 17, 30, tzinfo=timezone.utc)
    end = datetime(2026, 10, 8, 4, 30, tzinfo=timezone.utc)
    events = [{"occurred_at": start, "activity_type": "CHECK_IN"},
              {"occurred_at": end, "activity_type": "CHECK_OUT"}]
    result = calculate_working_time(events, day=date(2026, 10, 8), timezone_name="Asia/Kolkata")
    assert result["grossMinutes"] == 600
    assert result["netMinutes"] == 600


def test_daily_summary_counts_open_shift_through_now():
    start = datetime(2026, 10, 8, 4, 0, tzinfo=timezone.utc)
    now = datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc)
    result = calculate_working_time([{"occurred_at": start, "activity_type": "CHECK_IN"}],
                                    day=date(2026, 10, 8), timezone_name="Asia/Kolkata", as_of=now)
    assert result["grossMinutes"] == 60
    assert result["hasOpenSession"] is True
    assert result["calculationStatus"] == "PROVISIONAL"
