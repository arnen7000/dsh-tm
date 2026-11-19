"""Exact slot process ownership and a detached, redacting Node supervisor."""

from dataclasses import dataclass
import ctypes
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

WINDOWS = sys.platform == "win32"
START_TIMEOUT = 20.0
TOKEN = re.compile(rb"(token=)[^\s&#\"'<>\x1b]+", re.IGNORECASE)
ENTRY_URL = re.compile(rb"dsh web:\s*(https?://(?:127\.0\.0\.1|localhost|\[::1\]):(\d+)(?=[/?#\s]|$)[^\s\x1b]*)")


def capture(command):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if WINDOWS else {}
    return subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                          timeout=5, **options).stdout


def normalize_port(port):
    if type(port) is int:
        value = port
    elif isinstance(port, str) and port.strip().isascii() and port.strip().isdigit():
        try:
            value = int(port)
        except ValueError:
            return None
    else:
        return None
    return value if 0 < value <= 65535 else None


def listener_ports(port=None):
    """Return {pid: {ports}} for TCP listeners."""
    if port is not None:
        port = normalize_port(port)
        if port is None:
            return {}
    result = {}
    try:
        if WINDOWS:
            for line in capture(["netstat", "-ano"]).splitlines():
                fields = line.split()
                if len(fields) != 5 or fields[0] != b"TCP" or fields[3] != b"LISTENING":
                    continue
                local_port = int(fields[1].rsplit(b":", 1)[1])
                if port is None or local_port == port:
                    result.setdefault(int(fields[4]), set()).add(local_port)
        else:
            tcp = "-iTCP" if port is None else "-iTCP:%d" % port
            pid = None
            for line in capture(["lsof", "-nP", tcp, "-sTCP:LISTEN", "-Fpn"]).splitlines():
                if line.startswith(b"p"):
                    pid = int(line[1:])
                elif pid and line.startswith(b"n"):
                    local_port = int(line.rsplit(b":", 1)[1])
                    result.setdefault(pid, set()).add(local_port)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        pass
    return result


@dataclass(frozen=True)
class Process:
    pid: int
    birth: str
    executable: str
    argv: tuple


if sys.platform == "darwin":
    _LIBC = ctypes.CDLL(None, use_errno=True)
    _LIBC.proc_pidinfo.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int,
    ]


def _mac_birth(pid):
    # PROC_PIDTBSDINFO (struct proc_bsdinfo). Its two trailing uint64 fields
    # are the process creation time as seconds and microseconds.
    data = ctypes.create_string_buffer(136)
    if _LIBC.proc_pidinfo(pid, 3, 0, data, len(data)) != len(data):
        return None
    status = struct.unpack_from("=I", data, 4)[0]
    if status == 5:  # SZOMB
        return None
    return "%d.%06d" % struct.unpack_from("=QQ", data, 120)


def _mac_process(pid):
    birth = _mac_birth(pid)
    if not birth:
        return None
    # CTL_KERN, KERN_PROCARGS2, pid. The kernel keeps the original argument
    # boundaries, including spaces, and places the argument count up front.
    mib = (ctypes.c_int * 3)(1, 49, pid)
    data = ctypes.create_string_buffer(256 * 1024)
    size = ctypes.c_size_t(len(data))
    if _LIBC.sysctl(mib, 3, data, ctypes.byref(size), None, 0) or size.value < 5:
        return None
    raw = data.raw[:size.value]
    argc = struct.unpack_from("=i", raw)[0]
    executable, separator, rest = raw[4:].partition(b"\0")
    if not separator or argc <= 0 or not executable:
        return None
    arguments = rest.lstrip(b"\0").split(b"\0", argc)
    if len(arguments) <= argc or birth != _mac_birth(pid):
        return None
    return Process(pid, birth, normal_path(os.fsdecode(executable)),
                   tuple(os.fsdecode(arg) for arg in arguments[:argc]))


def _linux_birth(pid):
    with open("/proc/%d/stat" % pid, encoding="utf-8", errors="replace") as stat:
        fields = stat.read().rsplit(")", 1)[1].split()
    return None if fields[0] == "Z" else fields[19]


def _linux_process(pid):
    birth = _linux_birth(pid)
    if not birth:
        return None
    executable = os.readlink("/proc/%d/exe" % pid)
    with open("/proc/%d/cmdline" % pid, "rb") as cmdline:
        data = cmdline.read()
        arguments = (data[:-1] if data.endswith(b"\0") else data).split(b"\0")
    if birth != _linux_birth(pid):
        return None
    return Process(pid, birth, executable, tuple(os.fsdecode(arg) for arg in arguments))


def _windows_argv(cmdline):
    parse = ctypes.windll.shell32.CommandLineToArgvW
    parse.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(ctypes.c_wchar_p)
    size = ctypes.c_int()
    args = parse(cmdline, ctypes.byref(size))
    if not args:
        return ()
    try:
        return tuple(args[:size.value])
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(args)


def _open_windows_process(pid, access=0x1000):
    api = ctypes.windll.kernel32
    api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    api.OpenProcess.restype = ctypes.c_void_p
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    return api, api.OpenProcess(access, False, pid)


def _windows_process(pid):
    api, handle = _open_windows_process(pid)
    if not handle:
        return None
    try:
        return _windows_native_process(api, handle, pid) or _windows_cim_process(pid)
    finally:
        api.CloseHandle(handle)


def _windows_native_process(api, handle, pid):
    from ctypes import wintypes

    class UnicodeString(ctypes.Structure):
        _fields_ = [("length", wintypes.USHORT), ("capacity", wintypes.USHORT),
                    ("buffer", ctypes.c_void_p)]

    timestamps = [wintypes.FILETIME() for _ in range(4)]
    api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    if not api.GetProcessTimes(handle, *(ctypes.byref(stamp) for stamp in timestamps)):
        return None
    creation = timestamps[0]
    birth = str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)
    executable = ctypes.create_unicode_buffer(32768)
    size = wintypes.DWORD(len(executable))
    api.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    if not api.QueryFullProcessImageNameW(handle, 0, executable, ctypes.byref(size)):
        return None

    # Available on current Windows. Fall back to Win32_Process if the process
    # class is unavailable or the system denies reading the command line.
    query = ctypes.windll.ntdll.NtQueryInformationProcess
    query.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p,
                      wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    query_size = wintypes.ULONG()
    process_command_line = 60
    query(handle, process_command_line, None, 0, ctypes.byref(query_size))
    if not ctypes.sizeof(UnicodeString) <= query_size.value <= 1024 * 1024:
        return None
    data = ctypes.create_string_buffer(query_size.value)
    if query(handle, process_command_line, data, len(data), ctypes.byref(query_size)) != 0:
        return None
    cmd = UnicodeString.from_buffer(data)
    start = ctypes.addressof(data)
    if (not cmd.buffer or cmd.length % ctypes.sizeof(ctypes.c_wchar)
            or not start <= cmd.buffer <= cmd.buffer + cmd.length <= start + len(data)):
        return None
    value = ctypes.wstring_at(cmd.buffer, cmd.length // ctypes.sizeof(ctypes.c_wchar))
    if value:
        return Process(pid, birth, executable.value, _windows_argv(value))
    return None


def _windows_cim_process(pid):
    script = (
        '[Console]::OutputEncoding = [Text.Encoding]::UTF8; '
        '$p=Get-CimInstance Win32_Process -Filter "ProcessId=%d" -ErrorAction Stop; '
        'if ($p -and $p.CreationDate) { @{birth=$p.CreationDate.ToUniversalTime().ToFileTimeUtc(); '
        'executable=$p.ExecutablePath; command=$p.CommandLine} | ConvertTo-Json -Compress }'
    ) % pid
    data = json.loads(capture(["powershell", "-NoProfile", "-NonInteractive",
                               "-Command", script]).decode("utf-8-sig"))
    if data["command"] and data["executable"]:
        return Process(pid, str(data["birth"]), data["executable"],
                       _windows_argv(data["command"]))
    return None


def process_info(pid):
    try:
        if WINDOWS:
            return _windows_process(pid)
        if sys.platform == "darwin":
            return _mac_process(pid)
        if sys.platform.startswith("linux"):
            return _linux_process(pid)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, KeyError):
        pass
    return None


def normal_path(path):
    return os.path.normcase(os.path.realpath(path))


def _entrypoint(process):
    if (not process or len(process.argv) < 3
            or os.path.basename(process.executable).lower() not in ("node", "node.exe")
            or process.argv[1].startswith("-") or process.argv[2] != "web"):
        return None
    entry = process.argv[1]
    # The current cwd can change after launch and cannot prove which relative
    # script Node loaded. Managed launchers always pass an absolute path.
    return normal_path(entry) if os.path.isabs(entry) else None


def process_matches(pid, entrypoint):
    return bool(entrypoint) and _entrypoint(process_info(pid)) == normal_path(entrypoint)


def _matching_listeners(entrypoint, port):
    port = normalize_port(port)
    if port is None:
        return {}, set()
    listeners = set(listener_ports(port))
    owned = {}
    if isinstance(entrypoint, (str, os.PathLike)) and entrypoint:
        expected = normal_path(entrypoint)
        for pid in listeners:
            process = process_info(pid)
            if _entrypoint(process) == expected:
                owned[pid] = process
    return owned, listeners


def slot_listeners(entrypoint, port):
    owned, listeners = _matching_listeners(entrypoint, port)
    return set(owned), listeners


def port_reachable(port):
    port = normalize_port(port)
    if port is None:
        return False
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as conn:
                conn.settimeout(0.15)
                if conn.connect_ex((host, port)) == 0:
                    return True
        except OSError:
            continue
    return False


def observe_slot(entrypoint, port):
    """Return (state, verified processes by PID) from one listener observation."""
    port = normalize_port(port)
    if port is None:
        return "idle", {}
    owned, listeners = _matching_listeners(entrypoint, port)
    if listeners:
        return ("owned" if owned.keys() == listeners else "conflict"), owned
    # A listener that the OS won't disclose is still a conflict.
    return ("conflict" if port_reachable(port) else "idle"), owned


def slot_state(entrypoint, port):
    return observe_slot(entrypoint, port)[0]


def _still_owned(process, entrypoint, port, tracked):
    if not tracked and port not in listener_ports(port).get(process.pid, ()):
        return False
    current = process_info(process.pid)
    return (current is not None and current.birth == process.birth
            and _entrypoint(current) == normal_path(entrypoint))


def _terminate_windows(process, entrypoint, port, tracked):
    # Hold a process handle across the fresh verification and the signal.
    # A reused numeric PID cannot redirect TerminateProcess to another process.
    api, handle = _open_windows_process(process.pid, access=0x0001)  # PROCESS_TERMINATE
    api.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    if not handle:
        return False
    try:
        return (_still_owned(process, entrypoint, port, tracked)
                and bool(api.TerminateProcess(handle, 1)))
    finally:
        api.CloseHandle(handle)


def _terminate_posix(process, entrypoint, port, tracked):
    pidfd = None
    try:
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            try:
                pidfd = os.pidfd_open(process.pid)
            except OSError:
                pass
        if not _still_owned(process, entrypoint, port, tracked):
            return False
        if pidfd is None:
            os.kill(process.pid, signal.SIGTERM)
        else:
            signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if sys.platform == "darwin":
                birth = _mac_birth(process.pid)
            else:
                try:
                    birth = _linux_birth(process.pid)
                except OSError:
                    birth = None
            if birth != process.birth:
                return True
            time.sleep(0.05)
        # During shutdown a verified process may close its listener before it
        # exits. Recheck the creation identity and exact entrypoint before escalation.
        if not _still_owned(process, entrypoint, port, True):
            return False
        if pidfd is None:
            os.kill(process.pid, signal.SIGKILL)
        else:
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return True
    except OSError:
        return False
    finally:
        if pidfd is not None:
            os.close(pidfd)


def stop_slot(entrypoint, port, tracked=None):
    """Stop verified listeners and, when provided, the verified child of this launch."""
    port = normalize_port(port)
    if not entrypoint or port is None:
        return 0
    owned, _ = _matching_listeners(entrypoint, port)
    child = None
    if (isinstance(tracked, SlotProcess) and tracked.entrypoint == normal_path(entrypoint)
            and tracked.port == port and tracked.poll() is None):
        tracked.started.wait(timeout=2)
        child = tracked.child
        if child and child.pid not in owned:
            current = process_info(child.pid)
            if current and current.birth == child.birth:
                owned[child.pid] = current
    terminate = _terminate_windows if WINDOWS else _terminate_posix
    return sum(terminate(process, entrypoint, port,
                         bool(child and process.pid == child.pid and process.birth == child.birth))
               for process in owned.values())


class SlotProcess:
    """A supervisor handle. Its authenticated URL stays in an anonymous pipe and memory."""

    def __init__(self, proc, entrypoint, port):
        self._proc = proc
        self.entrypoint = normal_path(entrypoint)
        self.port = port
        self.launched_at = time.monotonic()
        self.start_confirmed = False
        self.child = None
        self.url = None
        self.started = threading.Event()
        threading.Thread(target=self._read_events, daemon=True).start()

    @property
    def pid(self):
        return self._proc.pid

    @property
    def child_pid(self):
        return self.child.pid if self.child else None

    def poll(self):
        return self._proc.poll()

    def wait(self, timeout=None):
        return self._proc.wait(timeout=timeout)

    def _read_events(self):
        try:
            with self._proc.stdout as events:
                for line in events:
                    event = json.loads(line)
                    if "pid" in event:
                        self.child = Process(event["pid"], event["birth"], "", ())
                        self.started.set()
                    if "url" in event:
                        self.url = event["url"]
        finally:
            self.started.set()


def _default_args(port):
    return ["web", "--host", "127.0.0.1", "--port", str(port), "--no-open"]


def spawn_slot(node, entrypoint, port, *, env, cwd, output_path, extra_args=(), launcher=None,
               capture_url=True):
    """Run Node detached from the caller and redact its output before it reaches disk."""
    port = normalize_port(port)
    if port is None:
        raise ValueError("Invalid TCP port")
    entrypoint = os.path.abspath(entrypoint)
    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    args = list(launcher or [sys.executable, os.path.abspath(__file__)])
    args.extend(["--supervise", str(port), output_path, str(int(capture_url)),
                 os.fspath(node), entrypoint])
    args.extend(str(arg) for arg in (extra_args or _default_args(port)))
    if WINDOWS:
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    else:
        options = {"start_new_session": True}
    proc = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **options)
    return SlotProcess(proc, entrypoint, port)


def _supervise(port, output_path, capture_url, cmd):
    events = sys.stdout
    if events is None and WINDOWS:
        # Frozen windowed launchers can suppress Python's stdio objects even
        # when the parent provided a real anonymous pipe as stdout.
        import msvcrt
        stdout_handle = ctypes.windll.kernel32.GetStdHandle
        stdout_handle.argtypes = [ctypes.c_uint32]
        stdout_handle.restype = ctypes.c_void_p
        handle = stdout_handle(-11)  # STD_OUTPUT_HANDLE
        if handle and handle != ctypes.c_void_p(-1).value:
            events = os.fdopen(msvcrt.open_osfhandle(handle, os.O_WRONLY), "w", encoding="utf-8")
    captured = False

    def send(message):
        try:
            events.write(json.dumps(message) + "\n")
            events.flush()
            return True
        except (OSError, AttributeError):
            return False

    fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab", buffering=0) as log:
        try:
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if WINDOWS else {}
            child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, **options)
        except OSError as error:
            log.write(TOKEN.sub(rb"\1[redacted]", (str(error) + "\n").encode("utf-8", "replace")))
            return 1
        process = process_info(child.pid)
        if process and child.poll() is None:
            send({"pid": child.pid, "birth": process.birth})
        with child.stdout as output:
            for line in output:
                # readline keeps a token intact even when Node writes it in several chunks.
                log.write(TOKEN.sub(rb"\1[redacted]", line))
                if capture_url and not captured:
                    match = ENTRY_URL.search(line)
                    if match and int(match.group(2)) == port:
                        captured = send({"url": match.group(1).decode("utf-8", "replace")})
                        if not captured:
                            capture_url = False
        result = child.wait()
    return result if result >= 0 else 128 - result


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 6 or args[0] != "--supervise":
        return 2
    return _supervise(int(args[1]), args[2], args[3] == "1", args[4:])


if __name__ == "__main__":
    sys.exit(main())
