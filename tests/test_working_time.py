from datetime import datetime, timedelta, timezone

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
