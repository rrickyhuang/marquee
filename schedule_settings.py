"""Task Scheduler settings shared by both scripts' --schedule commands.

`schtasks /create` can't set these, and its defaults (don't start on battery,
no catch-up after a missed run, no wake) silently drop runs on a laptop that
sleeps. Applying them after every (re)registration keeps `--schedule` from
undoing them.
"""
import subprocess


def apply_task_settings(task_names):
    """Make each task catch up after a missed run, wake the PC, and run on battery.

    Returns an error string if PowerShell failed, else None.
    """
    names = ", ".join("'" + n.replace("'", "''") + "'" for n in task_names)
    script = (
        f"foreach ($n in @({names})) {{ "
        "$t = Get-ScheduledTask -TaskName $n -ErrorAction Stop; "
        "$t.Settings.StartWhenAvailable = $true; "
        "$t.Settings.WakeToRun = $true; "
        "$t.Settings.DisallowStartIfOnBatteries = $false; "
        "$t.Settings.StopIfGoingOnBatteries = $false; "
        "Set-ScheduledTask -TaskName $n -Settings $t.Settings | Out-Null "
        "}"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True,
    )
    if result.returncode:
        return result.stderr.strip() or "PowerShell failed"
    return None
