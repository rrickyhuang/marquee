"""Tests for register_scheduled_task() in both scripts."""
from unittest.mock import patch, MagicMock

import watchlist_checker
import recommendations


# ── watchlist_checker ─────────────────────────────────────────────────────────

def test_watchlist_interval_default():
    fake_run = MagicMock(returncode=0, stderr="")
    cfg = {"watchlist": {"schedule_interval_days": 3, "schedule_time": "20:00"}}
    with patch.dict(watchlist_checker.__dict__, {"_cfg": cfg}):
        with patch("subprocess.run", return_value=fake_run) as mock_run:
            watchlist_checker.register_scheduled_task()
            cmd = mock_run.call_args_list[0][0][0]
            assert "/sc daily" in cmd
            assert "/mo 3" in cmd
            assert "/st 20:00" in cmd


def test_watchlist_custom_interval_and_time():
    fake_run = MagicMock(returncode=0, stderr="")
    cfg = {"watchlist": {"schedule_interval_days": 5, "schedule_time": "18:30"}}
    with patch.dict(watchlist_checker.__dict__, {"_cfg": cfg}):
        with patch("subprocess.run", return_value=fake_run) as mock_run:
            watchlist_checker.register_scheduled_task()
            cmd = mock_run.call_args_list[0][0][0]
            assert "/mo 5" in cmd
            assert "/st 18:30" in cmd


def test_watchlist_defaults_when_keys_missing():
    fake_run = MagicMock(returncode=0, stderr="")
    cfg = {"watchlist": {}}
    with patch.dict(watchlist_checker.__dict__, {"_cfg": cfg}):
        with patch("subprocess.run", return_value=fake_run) as mock_run:
            watchlist_checker.register_scheduled_task()
            cmd = mock_run.call_args_list[0][0][0]
            assert "/mo 3" in cmd
            assert "/st 20:00" in cmd


# ── recommendations ───────────────────────────────────────────────────────────

def _recs_schtasks_calls(cfg):
    """Helper: run register_scheduled_task() and return all schtasks command strings."""
    fake_run = MagicMock(returncode=0, stderr="")
    with patch.dict(recommendations.__dict__, {"_cfg": cfg}):
        with patch("subprocess.run", return_value=fake_run) as mock_run:
            recommendations.register_scheduled_task()
            # schtasks calls are shell strings; PowerShell cleanup/settings calls are lists
            return [
                call[0][0] for call in mock_run.call_args_list
                if isinstance(call[0][0], str)
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


# ── task settings (catch-up / wake / battery) ─────────────────────────────────

def _powershell_calls(mock_run):
    return [c[0][0] for c in mock_run.call_args_list if isinstance(c[0][0], list)]


def test_watchlist_applies_task_settings():
    fake_run = MagicMock(returncode=0, stderr="")
    cfg = {"watchlist": {}}
    with patch.dict(watchlist_checker.__dict__, {"_cfg": cfg}):
        with patch("subprocess.run", return_value=fake_run) as mock_run:
            watchlist_checker.register_scheduled_task()
    script = _powershell_calls(mock_run)[-1][-1]
    assert "'WatchlistChecker'" in script
    assert "StartWhenAvailable = $true" in script
    assert "WakeToRun = $true" in script
    assert "DisallowStartIfOnBatteries = $false" in script
    assert "StopIfGoingOnBatteries = $false" in script


def test_recommendations_applies_task_settings_to_every_task():
    fake_run = MagicMock(returncode=0, stderr="")
    cfg = {"recommendations": {"schedule_days": [1, 15], "schedule_time": "21:00"}}
    with patch.dict(recommendations.__dict__, {"_cfg": cfg}):
        with patch("subprocess.run", return_value=fake_run) as mock_run:
            recommendations.register_scheduled_task()
    script = _powershell_calls(mock_run)[-1][-1]
    assert "'Letterboxd Recommendations (1)'" in script
    assert "'Letterboxd Recommendations (15)'" in script
    assert "StartWhenAvailable = $true" in script


def test_settings_skipped_when_schtasks_fails():
    fake_run = MagicMock(returncode=1, stderr="access denied")
    cfg = {"watchlist": {}}
    with patch.dict(watchlist_checker.__dict__, {"_cfg": cfg}):
        with patch("subprocess.run", return_value=fake_run) as mock_run:
            watchlist_checker.register_scheduled_task()
    assert mock_run.call_count == 1
