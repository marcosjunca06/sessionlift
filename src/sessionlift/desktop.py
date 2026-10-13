"""Everything that depends on the laptop's operating system: processes, terminal windows, the console
keyboard, shortcuts and logon tasks.

Built and used on Windows 11 with Windows Terminal. The window features (typing /resume or Ctrl+D into
a Claude Code window, reading its screen, opening a tab) exist only there; `WINDOWS_UI` says whether
they are available, and the rest of the package asks it before relying on them. Process checks and
starting a detached helper have POSIX versions, which have not been run on macOS or Linux yet.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

WINDOWS_UI = os.name == "nt"


class Unsupported(SystemExit):
    """A feature that does not exist on this platform yet."""


def python_exe() -> str:
    """python.exe beside this interpreter. Helpers started from pythonw (the sessions page, the
    keeper) need a console interpreter: one of them attaches to other windows' consoles."""
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")
    return str(console) if exe.name.lower() == "pythonw.exe" and console.exists() else sys.executable


def pythonw_exe() -> str:
    exe = Path(sys.executable).with_name("pythonw.exe")
    return str(exe) if exe.exists() else sys.executable


def spawn_detached(argv: list, cwd: Path) -> None:
    """Start argv outside this process tree, so it outlives the Claude Code session that ran it."""
    cwd = Path(cwd)
    cwd.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        subprocess.Popen(argv, cwd=str(cwd), start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    # WMI creates the process as a child of its own service, outside any job object Claude Code puts
    # its shells in, with a hidden console. Such a process gets the user's default environment, not
    # the caller's: pass settings as arguments, never as environment variables.
    cmdline = subprocess.list2cmdline(argv)
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        wmi = win32com.client.GetObject(r"winmgmts:root\cimv2")
        startup = wmi.Get("Win32_ProcessStartup").SpawnInstance_()
        startup.ShowWindow = 0
        process = wmi.Get("Win32_Process")
        params = process.Methods_("Create").InParameters.SpawnInstance_()
        params.Properties_.Item("CommandLine").Value = cmdline
        params.Properties_.Item("CurrentDirectory").Value = str(cwd)
        params.Properties_.Item("ProcessStartupInformation").Value = startup
        if process.ExecMethod_("Create", params).Properties_.Item("ReturnValue").Value == 0:
            return
    except Exception:  # noqa: BLE001 - no pywin32, or WMI refused: the PowerShell route below
        pass
    q = lambda s: "'" + s.replace("'", "''") + "'"  # noqa: E731
    ps = ("$si = New-CimInstance -ClassName Win32_ProcessStartup -Property @{ShowWindow=[uint16]0} -ClientOnly; "
          f"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
          f"@{{CommandLine={q(cmdline)}; CurrentDirectory={q(str(cwd))}; ProcessStartupInformation=$si}}; "
          "if ($r.ReturnValue -ne 0) { Write-Error \"Win32_Process.Create returned $($r.ReturnValue)\"; exit 1 }")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode:
        raise SystemExit(f"could not start a background helper: {r.stderr.strip()[:300]}")


# ---------------------------------------------------------------- processes

def wait_for_pid(pid: int, timeout_s: float) -> bool:
    """True once process `pid` has exited (or never existed), False if it still runs after timeout_s."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return True
        try:
            return k32.WaitForSingleObject(handle, int(timeout_s * 1000)) == 0
        finally:
            k32.CloseHandle(handle)
    deadline = time.time() + timeout_s
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(min(2.0, max(0.05, deadline - time.time())))


def process_started(pid: int):
    """When process pid started, as a Windows FILETIME (0 where that cannot be read, and always on
    POSIX); None if it is not running."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass
        return 0
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = k32.OpenProcess(0x1000 | 0x00100000, False, pid)  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
    if not handle:
        return None
    try:
        if k32.WaitForSingleObject(handle, 0) == 0:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return 0
    finally:
        k32.CloseHandle(handle)


def process_alive(pid: int, created=None) -> bool:
    """pid is running and, when `created` (its start as a FILETIME) is given, is that process rather
    than a later one given the same number (Windows reuses them, after a restart most of all)."""
    start = process_started(pid)
    if start is None:
        return False
    try:
        created = int(created) if created else 0
    except (TypeError, ValueError):  # a start time in some other form: the pid alone decides
        created = 0
    return not created or not start or abs(start - created) < 20_000_000  # within 2 s


def pid_stamp() -> str:
    """What a pid or lock file holds: this process and when it started."""
    return f"{os.getpid()} {process_started(os.getpid()) or 0}"


def stamp_alive(text: str) -> bool:
    """The process a pid_stamp() names is still that one, running (a bare pid is accepted too)."""
    parts = (text or "").split()
    if not parts or not parts[0].isdigit():
        return False
    created = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return process_alive(int(parts[0]), created or None)


def kill(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False,
                       stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        with contextlib.suppress(OSError):
            os.kill(pid, 15)


# ---------------------------------------------------------------- Windows consoles

@contextlib.contextmanager
def attached_console(pid: int, device: str):
    """This process attached to the console of process pid, yielding (kernel32, a handle to its
    CONIN$ or CONOUT$). Windows only."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.AttachConsole.argtypes = (wintypes.DWORD,)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.FreeConsole()
    if not k32.AttachConsole(pid):
        raise OSError(f"cannot attach to the console of pid {pid}")
    try:
        h = k32.CreateFileW(device, 0xC0000000, 3, None, 3, 0, None)  # read/write, shared, existing
        if not h or h == ctypes.c_void_p(-1).value:
            raise OSError(f"cannot open {device} of pid {pid}")
        try:
            yield k32, h
        finally:
            k32.CloseHandle(h)
    finally:
        k32.FreeConsole()


def console_keys(pid: int, chunks: list, pause: float = 0.6) -> bool:
    """Type chunks into the console of process pid as if at its keyboard, pausing between them.
    "\\x1b" is Esc, "\\r" Enter, "\\x04" Ctrl+D and "\\b" Backspace. Enter goes as its own chunk, or the
    prompt takes it for a pasted newline. False where it cannot (not Windows, no console)."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    class KeyEvent(ctypes.Structure):  # KEY_EVENT_RECORD
        _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                    ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                    ("UnicodeChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]

    class InputRecord(ctypes.Structure):  # INPUT_RECORD, its union reduced to the key event
        _fields_ = [("EventType", wintypes.WORD), ("KeyEvent", KeyEvent)]

    u32 = ctypes.WinDLL("user32")
    u32.VkKeyScanW.argtypes = (wintypes.WCHAR,)
    u32.VkKeyScanW.restype = ctypes.c_short

    def records(chars: str):
        recs = (InputRecord * (2 * len(chars)))()
        for i, ch in enumerate(chars):
            for j, down in enumerate((True, False)):
                r = recs[2 * i + j]
                r.EventType = 1  # KEY_EVENT
                r.KeyEvent.bKeyDown = down
                r.KeyEvent.wRepeatCount = 1
                r.KeyEvent.wVirtualKeyCode = {"\r": 0x0D, "\x1b": 0x1B, "\x04": 0x44, "\b": 0x08}.get(ch) or \
                    u32.VkKeyScanW(ch) & 0xFF
                r.KeyEvent.UnicodeChar = ch
                if ch == "\x04":  # Ctrl+D
                    r.KeyEvent.dwControlKeyState = 0x0008  # LEFT_CTRL_PRESSED
        return recs

    try:
        with attached_console(pid, "CONIN$") as (k32, h):
            k32.WriteConsoleInputW.argtypes = (wintypes.HANDLE, ctypes.POINTER(InputRecord), wintypes.DWORD,
                                               ctypes.POINTER(wintypes.DWORD))
            ok = True
            for i, chunk in enumerate(chunks):
                if i:
                    time.sleep(pause)
                recs, n = records(chunk), wintypes.DWORD()
                ok = bool(k32.WriteConsoleInputW(h, recs, len(recs), ctypes.byref(n))) and n.value == len(recs) and ok
            return ok
    except OSError:
        return False


def console_text(pid: int) -> str:
    """The text on screen in the console of process pid, or "" if it cannot be read (not Windows)."""
    if os.name != "nt":
        return ""
    import ctypes
    from ctypes import wintypes

    class Coord(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class Rect(ctypes.Structure):
        _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short), ("Right", ctypes.c_short),
                    ("Bottom", ctypes.c_short)]

    class Info(ctypes.Structure):  # CONSOLE_SCREEN_BUFFER_INFO
        _fields_ = [("dwSize", Coord), ("dwCursorPosition", Coord), ("wAttributes", wintypes.WORD),
                    ("srWindow", Rect), ("dwMaximumWindowSize", Coord)]

    try:
        with attached_console(pid, "CONOUT$") as (k32, h):
            k32.GetConsoleScreenBufferInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(Info))
            k32.ReadConsoleOutputCharacterW.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, Coord,
                                                        ctypes.POINTER(wintypes.DWORD))
            info = Info()
            if not k32.GetConsoleScreenBufferInfo(h, ctypes.byref(info)):
                return ""
            width, lines = info.dwSize.X, []
            for y in range(info.srWindow.Top, info.srWindow.Bottom + 1):
                buf, n = ctypes.create_unicode_buffer(width + 1), wintypes.DWORD()
                k32.ReadConsoleOutputCharacterW(h, buf, width, Coord(0, y), ctypes.byref(n))
                lines.append(buf.value[:n.value].rstrip())
            return "\n".join(lines)
    except OSError:
        return ""


# ---------------------------------------------------------------- windows, shortcuts, tasks

def open_terminal(argv: list, cwd: str) -> None:
    """Run argv in a new terminal tab that stays open after it ends: Windows Terminal when installed,
    else a console window."""
    if os.name != "nt":
        raise Unsupported("opening a terminal tab is not supported on this platform yet; run: "
                          + " ".join(argv))
    wt = shutil.which("wt")
    if wt:
        subprocess.Popen([wt, "-w", "0", "new-tab", "-d", cwd, "cmd", "/k", *argv], cwd=cwd)
    else:
        subprocess.Popen(["cmd", "/c", "start", "", "/D", cwd, "cmd", "/k", *argv], cwd=cwd,
                         creationflags=subprocess.CREATE_NO_WINDOW)


def focus_window(title: str) -> bool:
    """Bring an open Tk window with this title to the front. Windows only."""
    if os.name != "nt":
        return False
    import ctypes
    u32 = ctypes.windll.user32
    hwnd = u32.FindWindowW("TkTopLevel", title)
    if not hwnd:
        return False
    u32.ShowWindow(hwnd, 9)  # SW_RESTORE
    u32.SetForegroundWindow(hwnd)
    return True


def _powershell(script: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, env=dict(os.environ, **env),
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def make_shortcut(name: str, target: str, arguments: str, workdir: str, description: str, icon: str = "") -> Path:
    """A desktop shortcut (.lnk). Windows only."""
    if os.name != "nt":
        raise Unsupported("desktop shortcuts are Windows-only")
    script = ("$d = [Environment]::GetFolderPath('Desktop'); $p = Join-Path $d ($env:SL_NAME + '.lnk'); "
              "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($p); $s.TargetPath = $env:SL_TARGET; "
              "$s.Arguments = $env:SL_ARGS; $s.WorkingDirectory = $env:SL_DIR; $s.Description = $env:SL_DESC; "
              "if ($env:SL_ICON) { $s.IconLocation = $env:SL_ICON + ',0' }; $s.Save(); Write-Output $p")
    r = _powershell(script, SL_NAME=name, SL_TARGET=target, SL_ARGS=arguments, SL_DIR=workdir, SL_DESC=description,
                    SL_ICON=icon)
    if r.returncode:
        raise SystemExit(f"could not make the shortcut: {r.stderr.strip()[:300]}")
    return Path(r.stdout.strip())


def install_logon_task(name: str, exe: str, arguments: str, workdir: str, description: str) -> None:
    """A Task Scheduler task that starts at every logon of this user. Windows only."""
    if os.name != "nt":
        raise Unsupported("starting at logon is set up only on Windows so far; run `sessionlift keeper` yourself")
    script = ("$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
              "-DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan); "
              "$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; "
              "Register-ScheduledTask -Force -TaskName $env:SL_TASK -Settings $s -Trigger $t "
              "-Description $env:SL_DESC "
              "-Action (New-ScheduledTaskAction -Execute $env:SL_EXE -Argument $env:SL_ARGS "
              "-WorkingDirectory $env:SL_DIR) | Out-Null")
    r = _powershell(script, SL_TASK=name, SL_EXE=exe, SL_ARGS=arguments, SL_DIR=workdir, SL_DESC=description)
    if r.returncode:
        raise SystemExit(f"could not register the logon task: {r.stderr.strip()[:300]}")


def uninstall_logon_task(name: str) -> None:
    if os.name == "nt":
        subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], capture_output=True, check=False,
                       stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)


def shut_down() -> None:
    if os.name != "nt":
        raise Unsupported("shutting down from the page is Windows-only")
    subprocess.Popen(["shutdown", "/s", "/t", "0"], creationflags=subprocess.CREATE_NO_WINDOW)
