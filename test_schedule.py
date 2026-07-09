"""Tests for register_scheduled_task() in both scripts."""
from unittest.mock import patch, MagicMock

import watchlist_checker
import recommendations


# ── watchlist_checker ─────────────────────────────────────────────────────────

def test_watchlist_sunday_default():
    fake_run = MagicMock(returncode=0, stderr="")
    cfg = {"watchlist": {"schedule_day": "sunday", "schedule_time": "20:00"}}
    with patch.dict(watchlist_checker.__dict__, {"_cfg": cfg}):
        with patch("subprocess.run", return_value=fake_run) as mock_run:
            watchlist_checker.register_scheduled_task()
            cmd = mock_run.call_args[0][0]
            assert "/sc weekly" in cmd
            assert "/d SUN" in cmd
            assert "/st 20:00" in cmd


def test_watchlist_wednesday_custom_time():
    fake_run = MagicMock(returncode=0, stderr="")
    cfg = {"watchlist": {"schedule_day": "wednesday", "schedule_time": "18:30"}}
    with patch.dict(watchlist_checker.__dict__, {"_cfg": cfg}):
        with patch("subprocess.run", return_value=fake_run) as mock_run:
            watchlist_checker.register_scheduled_task()
            cmd = mock_run.call_args[0][0]
            assert "/d WED" in cmd
            assert "/st 18:30" in cmd


def test_watchlist_defaults_when_keys_missing():
    fake_run = MagicMock(returncode=0, stderr="")
    cfg = {"watchlist": {}}
    with patch.dict(watchlist_checker.__dict__, {"_cfg": cfg}):
        with patch("subprocess.run", return_value=fake_run) as mock_run:
            watchlist_checker.register_scheduled_task()
            cmd = mock_run.call_args[0][0]
            assert "/d SUN" in cmd
            assert "/st 20:00" in cmd


# ── recommendations ───────────────────────────────────────────────────────────

def _recs_schtasks_calls(cfg):
    """Helper: run register_scheduled_task() and return all schtasks command strings."""
    fake_run = MagicMock(returncode=0, stderr="")
    with patch.dict(recommendations.__dict__, {"_cfg": cfg}):
        with patch("subprocess.run", return_value=fake_run) as mock_run:
            recommendations.register_scheduled_task()
            # First call is the PowerShell cleanup; remaining are schtasks calls
            return [
                call[0][0] for call in mock_run.call_args_list[1:]
            ]


def test_recommendations_first_of_month():
    cmds = _recs_schtasks_calls({"recommendations": {"schedule_days": 1, "schedule_time": "21:00"}})
    assert len(cmds) == 1
    assert "/d 1" in cmds[0]
    assert "/st 21:00" in cmds[0]
    assert "Letterboxd Recommendations\"" in cmds[0]  # no day suffix for single day


def test_recommendations_custom_day_and_time():
    cmds = _recs_schtasks_calls({"recommendations": {"schedule_days": 15, "schedule_time": "09:00"}})
    assert len(cmds) == 1
    assert "/d 15" in cmds[0]
    assert "/st 09:00" in cmds[0]


def test_recommendations_twice_a_month():
    cmds = _recs_schtasks_calls({"recommendations": {"schedule_days": [1, 15], "schedule_time": "21:00"}})
    assert len(cmds) == 2
    assert "/d 1" in cmds[0]
    assert "/d 15" in cmds[1]
    assert "(1)" in cmds[0]   # day suffix in task name
    assert "(15)" in cmds[1]


def test_recommendations_string_days():
    cmds = _recs_schtasks_calls({"recommendations": {"schedule_days": "1, 15", "schedule_time": "21:00"}})
    assert len(cmds) == 2
    assert "/d 1" in cmds[0]
    assert "/d 15" in cmds[1]


def test_recommendations_defaults_when_keys_missing():
    cmds = _recs_schtasks_calls({"recommendations": {}})
    assert len(cmds) == 1
    assert "/d 1" in cmds[0]
    assert "/st 21:00" in cmds[0]
