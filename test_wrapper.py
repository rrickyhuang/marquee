"""Tests for run_watchlist_checker.sh (the cron wrapper): logging, exit status
and healthcheck pings, using a fake interpreter and a fake curl."""
import os
import shutil
import stat
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash"
)


def _exe(path, body):
    with open(path, "w") as f:
        f.write("#!/usr/bin/env bash\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


@pytest.fixture
def sandbox(tmp_path):
    """A copy of the wrapper in its own folder, with fake python and curl."""
    shutil.copy(os.path.join(HERE, "run_watchlist_checker.sh"), tmp_path)
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    # Records "<url>" and the request body for each ping.
    _exe(fakebin / "curl", 'echo "${@: -1}" >> "$PINGS"\ncat >> "$PINGS"\necho >> "$PINGS"\n')
    return tmp_path


def run(sandbox, python_body, env_extra=None, dotenv=None):
    _exe(sandbox / "fakepython", python_body)
    if dotenv is not None:
        (sandbox / ".env").write_text(dotenv)
    env = {
        "PATH": f"{sandbox / 'fakebin'}:{os.environ['PATH']}",
        "MARQUEE_PYTHON": str(sandbox / "fakepython"),
        "PINGS": str(sandbox / "pings.txt"),
        "HOME": str(sandbox),
        **(env_extra or {}),
    }
    proc = subprocess.run(["bash", str(sandbox / "run_watchlist_checker.sh")],
                          env=env, capture_output=True, text=True)
    pings = (sandbox / "pings.txt").read_text() if (sandbox / "pings.txt").exists() else ""
    log = (sandbox / "logs" / "watchlist.log").read_text()
    return proc, log, pings


def test_success_is_logged_and_pinged(sandbox):
    proc, log, pings = run(sandbox, 'echo "all good"\nexit 0\n',
                           {"HEALTHCHECKS_URL": "https://hc.example/abc"})
    assert proc.returncode == 0
    assert "run start" in log and "all good" in log and "run end, exit 0" in log
    lines = pings.splitlines()
    assert lines[0] == "https://hc.example/abc/start"
    assert "https://hc.example/abc/0" in lines
    assert "all good" in pings  # run output is the body of the final ping


def test_failure_exits_nonzero_and_pings_failure_with_output(sandbox):
    proc, log, pings = run(sandbox, 'echo "✗ Email failed: boom"\nexit 1\n',
                           {"HEALTHCHECKS_URL": "https://hc.example/abc"})
    assert proc.returncode == 1
    assert "✗ Email failed: boom" in log and "exit 1" in log
    assert "https://hc.example/abc/1" in pings.splitlines()
    assert "Email failed: boom" in pings


def test_traceback_on_stderr_is_captured(sandbox):
    proc, log, _ = run(sandbox, 'echo "Traceback: oops" >&2\nexit 1\n')
    assert proc.returncode == 1
    assert "Traceback: oops" in log


def test_missing_interpreter_is_reported_as_failure(sandbox):
    proc, log, pings = run(sandbox, "exit 0\n",
                           {"HEALTHCHECKS_URL": "https://hc.example/abc",
                            "MARQUEE_PYTHON": str(sandbox / "does-not-exist")})
    assert proc.returncode != 0
    assert "run end, exit" in log
    assert "https://hc.example/abc/0" not in pings.splitlines()


def test_no_url_means_log_only(sandbox):
    proc, log, pings = run(sandbox, 'echo hi\n')
    assert proc.returncode == 0 and "hi" in log
    assert pings == ""


def test_url_read_from_dotenv_despite_spaces_in_other_values(sandbox):
    dotenv = "GMAIL_APP_PASSWORD=abcd efgh ijkl mnop\nHEALTHCHECKS_URL=https://hc.example/xyz/\n"
    proc, _, pings = run(sandbox, "exit 0\n", dotenv=dotenv)
    assert proc.returncode == 0
    assert "https://hc.example/xyz/start" in pings.splitlines()  # trailing slash stripped


def test_log_appends_across_runs(sandbox):
    run(sandbox, 'echo first\n')
    _, log, _ = run(sandbox, 'echo second\n')
    assert "first" in log and "second" in log
    assert log.count("run start") == 2
