"""Running one program in a Windows AppContainer: the containment under `ibasho tool`.

An AppContainer is the sandbox Windows gives Store apps and browsers' renderers. A process in one runs as the same
user, but with a second identity, the container's SID, and every file and network check has to pass for both: the
user's own rights open nothing by themselves. So the program reaches what has been granted to its container (here:
the tools folder read/write, the runtime read/execute) plus what Windows opens to every app sandbox (system files,
installed programs' folders, and read-only parts of the registry, the user's own part included). It runs at low
integrity, so it can't write anything labelled above that. Windows' firewall keeps it off the network unless it's
given the internetClient capability, and off this machine's loopback (the Ibasho daemon, other local servers) always.

Around it:
- a desktop of its own, so it can't see the screen, read keys or reach other programs' windows;
- a job object: a time limit (the caller's), a memory cap, a share of the CPU, a cap on processes, no clipboard or
  other desktop-wide state, and everything in it killed when the run ends, so nothing outlives a run.

Windows needs an app profile for the container (a registry entry under HKCU and a folder under
%LOCALAPPDATA%\\Packages); create_profile makes it and takes the container's own access off that folder, so the
tools folder stays the one place a tool can write. delete_profile removes both.
"""

from __future__ import annotations

import ctypes as C
import hashlib
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

try:
    from ctypes import wintypes as W
except (ImportError, ValueError):   # not everywhere: stand-ins keep this importable, and supported() says why not
    class W:
        DWORD, WORD, UINT, BOOL = C.c_uint32, C.c_uint16, C.c_uint, C.c_int
        HANDLE, LPWSTR, LPCWSTR = C.c_void_p, C.c_wchar_p, C.c_wchar_p


class SandboxError(Exception):
    pass


INTERNET_CLIENT = "S-1-15-3-1"   # the internetClient capability


@dataclass
class Result:
    code: int | None            # the program's exit code; None if it was stopped
    output: bytes               # stdout and stderr together, as they came, up to the cap
    stopped: str = ""           # why it was stopped: "" (it exited by itself), "timeout", "output"
    seconds: float = 0.0


def supported() -> str | None:
    """None if AppContainers work here, else why not."""
    if sys.platform != "win32":
        return "tools run in a Windows AppContainer, so they need Windows 10 or later (this is " + sys.platform + ")"
    if sys.getwindowsversion().build < 10240:
        return "tools need Windows 10 or later (AppContainer support)"
    return None


def container_name(home: Path) -> str:
    """One container per install, named after the install's folder."""
    return "Ibasho.Tools." + hashlib.sha256(str(Path(home).resolve()).lower().encode()).hexdigest()[:16]


if sys.platform == "win32":
    _k32 = C.WinDLL("kernel32", use_last_error=True)
    _adv = C.WinDLL("advapi32", use_last_error=True)
    _uenv = C.WinDLL("userenv", use_last_error=True)
    _u32 = C.WinDLL("user32", use_last_error=True)

    _uenv.DeriveAppContainerSidFromAppContainerName.argtypes = [W.LPCWSTR, C.POINTER(C.c_void_p)]
    _uenv.DeriveAppContainerSidFromAppContainerName.restype = C.c_long
    _uenv.CreateAppContainerProfile.argtypes = [W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, C.c_void_p, W.DWORD,
                                                C.POINTER(C.c_void_p)]
    _uenv.CreateAppContainerProfile.restype = C.c_long
    _uenv.DeleteAppContainerProfile.argtypes = [W.LPCWSTR]
    _uenv.DeleteAppContainerProfile.restype = C.c_long
    _uenv.GetAppContainerFolderPath.argtypes = [W.LPCWSTR, C.POINTER(W.LPWSTR)]
    _uenv.GetAppContainerFolderPath.restype = C.c_long
    _adv.ConvertSidToStringSidW.argtypes = [C.c_void_p, C.POINTER(W.LPWSTR)]
    _adv.ConvertStringSidToSidW.argtypes = [W.LPCWSTR, C.POINTER(C.c_void_p)]
    _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p),
                                                                          C.c_void_p]
    _adv.FreeSid.argtypes = [C.c_void_p]
    _adv.OpenProcessToken.argtypes = [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)]
    _adv.GetTokenInformation.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
    _k32.LocalFree.argtypes = [C.c_void_p]
    _k32.LocalFree.restype = C.c_void_p
    _k32.GetCurrentProcess.restype = W.HANDLE
    _ole = C.WinDLL("ole32")
    _ole.CoTaskMemFree.argtypes = [C.c_void_p]

    _k32.InitializeProcThreadAttributeList.argtypes = [C.c_void_p, W.DWORD, W.DWORD, C.POINTER(C.c_size_t)]
    _k32.UpdateProcThreadAttribute.argtypes = [C.c_void_p, W.DWORD, C.c_size_t, C.c_void_p, C.c_size_t,
                                               C.c_void_p, C.c_void_p]
    _k32.DeleteProcThreadAttributeList.argtypes = [C.c_void_p]
    _k32.CreateProcessW.argtypes = [W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p, W.BOOL, W.DWORD, C.c_void_p,
                                    W.LPCWSTR, C.c_void_p, C.c_void_p]
    _k32.CreatePipe.argtypes = [C.POINTER(W.HANDLE), C.POINTER(W.HANDLE), C.c_void_p, W.DWORD]
    _k32.SetHandleInformation.argtypes = [W.HANDLE, W.DWORD, W.DWORD]
    _k32.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
    _k32.CreateFileW.restype = W.HANDLE
    _k32.CloseHandle.argtypes = [W.HANDLE]
    _k32.CreateJobObjectW.argtypes = [C.c_void_p, W.LPCWSTR]
    _k32.CreateJobObjectW.restype = W.HANDLE
    _k32.SetInformationJobObject.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [W.HANDLE, W.HANDLE]
    _k32.TerminateJobObject.argtypes = [W.HANDLE, W.UINT]
    _k32.ResumeThread.argtypes = [W.HANDLE]
    _k32.ResumeThread.restype = W.DWORD
    _k32.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
    _k32.WaitForSingleObject.restype = W.DWORD
    _k32.GetExitCodeProcess.argtypes = [W.HANDLE, C.POINTER(W.DWORD)]
    _k32.TerminateProcess.argtypes = [W.HANDLE, W.UINT]
    _k32.SetErrorMode.argtypes = [W.UINT]
    _k32.SetErrorMode.restype = W.UINT
    _u32.CreateDesktopW.argtypes = [W.LPCWSTR, W.LPCWSTR, C.c_void_p, W.DWORD, W.DWORD, C.c_void_p]
    _u32.CreateDesktopW.restype = W.HANDLE
    _u32.CloseDesktop.argtypes = [W.HANDLE]


class _SidAndAttributes(C.Structure):
    _fields_ = [("Sid", C.c_void_p), ("Attributes", W.DWORD)]


class _SecurityCapabilities(C.Structure):
    _fields_ = [("AppContainerSid", C.c_void_p), ("Capabilities", C.POINTER(_SidAndAttributes)),
                ("CapabilityCount", W.DWORD), ("Reserved", W.DWORD)]


class _StartupInfo(C.Structure):
    _fields_ = [("cb", W.DWORD), ("lpReserved", W.LPWSTR), ("lpDesktop", W.LPWSTR), ("lpTitle", W.LPWSTR),
                ("dwX", W.DWORD), ("dwY", W.DWORD), ("dwXSize", W.DWORD), ("dwYSize", W.DWORD),
                ("dwXCountChars", W.DWORD), ("dwYCountChars", W.DWORD), ("dwFillAttribute", W.DWORD),
                ("dwFlags", W.DWORD), ("wShowWindow", W.WORD), ("cbReserved2", W.WORD),
                ("lpReserved2", C.c_void_p), ("hStdInput", W.HANDLE), ("hStdOutput", W.HANDLE),
                ("hStdError", W.HANDLE)]


class _StartupInfoEx(C.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", C.c_void_p)]


class _ProcessInformation(C.Structure):
    _fields_ = [("hProcess", W.HANDLE), ("hThread", W.HANDLE), ("dwProcessId", W.DWORD), ("dwThreadId", W.DWORD)]


class _SecurityAttributes(C.Structure):
    _fields_ = [("nLength", W.DWORD), ("lpSecurityDescriptor", C.c_void_p), ("bInheritHandle", W.BOOL)]


class _BasicLimits(C.Structure):
    _fields_ = [("PerProcessUserTimeLimit", C.c_int64), ("PerJobUserTimeLimit", C.c_int64),
                ("LimitFlags", W.DWORD), ("MinimumWorkingSetSize", C.c_size_t),
                ("MaximumWorkingSetSize", C.c_size_t), ("ActiveProcessLimit", W.DWORD),
                ("Affinity", C.c_size_t), ("PriorityClass", W.DWORD), ("SchedulingClass", W.DWORD)]


class _IoCounters(C.Structure):
    _fields_ = [(n, C.c_uint64) for n in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]


class _ExtendedLimits(C.Structure):
    _fields_ = [("Basic", _BasicLimits), ("Io", _IoCounters), ("ProcessMemoryLimit", C.c_size_t),
                ("JobMemoryLimit", C.c_size_t), ("PeakProcessMemoryUsed", C.c_size_t),
                ("PeakJobMemoryUsed", C.c_size_t)]


class _CpuRate(C.Structure):
    _fields_ = [("ControlFlags", W.DWORD), ("CpuRate", W.DWORD)]


SE_GROUP_ENABLED = 0x4
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x20002
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x20009
EXTENDED_STARTUPINFO_PRESENT = 0x80000
CREATE_SUSPENDED = 0x4
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x400
BELOW_NORMAL_PRIORITY_CLASS = 0x4000
STARTF_USESTDHANDLES = 0x100
HANDLE_FLAG_INHERIT = 0x1
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x8
JOB_OBJECT_LIMIT_PRIORITY_CLASS = 0x20
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_EXTENDED_LIMITS = 9
JOB_UI_RESTRICTIONS = 4
JOB_CPU_RATE = 15
JOB_UI_ALL = 0xFF          # other processes' windows, clipboard both ways, atoms, desktops, display, logoff, params
CPU_RATE_ENABLE_HARD_CAP = 0x1 | 0x4
WAIT_TIMEOUT = 0x102
INVALID_HANDLE = W.HANDLE(-1).value
GENERIC_ALL = 0x10000000
HRESULT_ALREADY_EXISTS = 0x800700B7
HRESULT_NOT_FOUND = 0x80070002
SEM_NO_DIALOGS = 0x0001 | 0x0002 | 0x8000   # no critical-error, crash or file-open-error boxes


def _check(ok, what: str):
    if not ok:
        raise SandboxError(f"{what} failed: {C.FormatError(C.get_last_error()).strip()}")


def _sid_string(p) -> str:
    s = W.LPWSTR()
    _check(_adv.ConvertSidToStringSidW(p, C.byref(s)), "ConvertSidToStringSid")
    try:
        return s.value
    finally:
        _k32.LocalFree(s)


def _sid(text: str) -> C.c_void_p:
    p = C.c_void_p()
    _check(_adv.ConvertStringSidToSidW(text, C.byref(p)), "ConvertStringSidToSid")
    return p


def container_sid(name: str) -> str:
    """The SID Windows derives from a container's name (S-1-15-2-...)."""
    if (why := supported()):
        raise SandboxError(why)
    p = C.c_void_p()
    hr = _uenv.DeriveAppContainerSidFromAppContainerName(name, C.byref(p))
    if hr != 0:
        raise SandboxError(f"deriving the container's id failed (0x{hr & 0xFFFFFFFF:08x})")
    try:
        return _sid_string(p)
    finally:
        _adv.FreeSid(p)


def profile_folder(name: str) -> Path | None:
    """The container's profile folder (%LOCALAPPDATA%\\Packages\\NAME), if its profile exists."""
    s = W.LPWSTR()
    if _uenv.GetAppContainerFolderPath(container_sid(name), C.byref(s)) != 0:
        return None
    try:
        return Path(s.value).parent      # the call gives ...\NAME\AC
    finally:
        _ole.CoTaskMemFree(s)


def create_profile(name: str) -> str:
    """Make (or keep) the container's app profile, which CreateProcess needs, and take the container's access off
    the profile's folder. Returns the container's SID."""
    if (why := supported()):
        raise SandboxError(why)
    p = C.c_void_p()
    hr = _uenv.CreateAppContainerProfile(name, "Ibasho tools", "Tools an agent wrote for itself, run by Ibasho", None,
                                         0, C.byref(p)) & 0xFFFFFFFF
    if hr == 0:
        _adv.FreeSid(p)
    elif hr != HRESULT_ALREADY_EXISTS:
        raise SandboxError(f"creating the tools' app container failed (0x{hr:08x})")
    sid = container_sid(name)
    folder = profile_folder(name)
    if folder and folder.is_dir():
        _icacls(str(folder), "/remove:g", f"*{sid}", "/T", "/C", "/Q")
    return sid


def delete_profile(name: str) -> bool:
    """Remove the container's app profile (registry entry and folder). False if there was none."""
    if (why := supported()):
        raise SandboxError(why)
    hr = _uenv.DeleteAppContainerProfile(name) & 0xFFFFFFFF
    if hr in (HRESULT_NOT_FOUND, 0x80070490):   # not found / element not found
        return False
    if hr != 0:
        raise SandboxError(f"removing the tools' app container failed (0x{hr:08x})")
    return True


def _icacls(*args: str):
    r = subprocess.run(["icacls", *args], capture_output=True, text=True, errors="replace",
                       creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise SandboxError(f"icacls {' '.join(args[:2])}: {(r.stdout + r.stderr).strip()[:300]}")


def grant(folder: Path, sid: str, write: bool):
    """Let the container into a folder and everything in it: read/execute, or modify with the folder labelled low
    integrity (an AppContainer runs at low integrity and can't write anything labelled higher)."""
    _icacls(str(folder), "/grant", f"*{sid}:(OI)(CI)({'M' if write else 'RX'})", "/Q")
    if write:
        _icacls(str(folder), "/setintegritylevel", "(OI)(CI)low", "/Q")


def refresh(folder: Path, sid: str):
    """grant(write=True), and everything inside back to inheriting from the folder. Files written there get the
    folder's access by themselves; a file moved in from elsewhere keeps the access it had, which this repairs.
    (icacls doesn't follow junctions, and a container can't make one pointing anywhere it couldn't write anyway.)"""
    grant(folder, sid, write=True)
    reset_inside(folder)


def reset_inside(folder: Path) -> str | None:
    """Everything in a folder back to inheriting its access from the folder (dropping entries of its own). A repair,
    so it goes on past files it can't fix (one deleted mid-way by a tool running at the same time, say) and returns
    what icacls said about them, or None."""
    if not any(folder.iterdir()):
        return None
    try:
        _icacls(str(folder / "*"), "/reset", "/T", "/C", "/Q")
    except SandboxError as e:
        return str(e)
    return None


def revoke(folder: Path, sid: str):
    """Take the container's entry off a folder and what inherits it. A low label stays: it only lowers who may
    write, and the person's own programs run above it."""
    _icacls(str(folder), "/remove:g", f"*{sid}", "/T", "/C", "/Q")


def _user_sid() -> str:
    tok = W.HANDLE()
    _check(_adv.OpenProcessToken(_k32.GetCurrentProcess(), 0x8, C.byref(tok)), "OpenProcessToken")   # TOKEN_QUERY
    try:
        need = W.DWORD()
        _adv.GetTokenInformation(tok, 1, None, 0, C.byref(need))                 # TokenUser
        buf = C.create_string_buffer(need.value)
        _check(_adv.GetTokenInformation(tok, 1, buf, need, C.byref(need)), "GetTokenInformation")
        return _sid_string(C.c_void_p.from_buffer(buf).value)
    finally:
        _k32.CloseHandle(tok)


def _desktop(sid: str) -> tuple[int, str]:
    """A fresh desktop on this window station that only the person and the container may use: what runs there
    can't see the screen, read keys, or reach other programs' windows."""
    name = f"ibasho-tool-{os.getpid()}-{threading.get_ident() & 0xFFFF:x}-{time.monotonic_ns() & 0xFFFFFF:x}"
    sddl = f"D:P(A;;GA;;;{_user_sid()})(A;;GA;;;SY)(A;;GA;;;{sid})S:(ML;;NW;;;LW)"
    sd = C.c_void_p()
    _check(_adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, C.byref(sd), None),
           "ConvertStringSecurityDescriptorToSecurityDescriptor")
    try:
        sa = _SecurityAttributes(C.sizeof(_SecurityAttributes), sd, False)
        h = _u32.CreateDesktopW(name, None, None, 0, GENERIC_ALL, C.byref(sa))
        _check(h, "CreateDesktop")
        return h, "WinSta0\\" + name
    finally:
        _k32.LocalFree(sd)


def _env_block(env: dict[str, str]) -> C.Array:
    text = "".join(f"{k}={v}\0" for k, v in sorted(env.items(), key=lambda kv: kv[0].upper())) + "\0"
    return C.create_unicode_buffer(text, len(text))


def run(argv: list[str], *, sid: str, cwd: Path, env: dict[str, str], network: bool = False, timeout: float = 60,
        max_output: int = 100_000, memory_mb: int = 1024, cpu_percent: int = 50, max_processes: int = 16,
        on_output=None) -> Result:
    """Run argv[0] (a full path) in the container `sid` on a desktop of its own, cwd pinned, with exactly `env` as
    its environment (Windows rewrites LOCALAPPDATA, TEMP and TMP to the container's profile, and refuses to start a
    container without LOCALAPPDATA). stdin is empty; stdout and stderr come back together, capped at max_output
    bytes (the run is stopped past it), and pass through on_output(bytes) as they come."""
    if (why := supported()):
        raise SandboxError(why)
    cap_sids = [INTERNET_CLIENT] if network else []
    sids = [_sid(sid)] + [_sid(c) for c in cap_sids]
    caps = (_SidAndAttributes * max(1, len(cap_sids)))()
    for i, p in enumerate(sids[1:]):
        caps[i].Sid, caps[i].Attributes = p, SE_GROUP_ENABLED
    sc = _SecurityCapabilities(sids[0], caps if cap_sids else None, len(cap_sids), 0)

    sa = _SecurityAttributes(C.sizeof(_SecurityAttributes), None, True)
    rd, wr = W.HANDLE(), W.HANDLE()
    nul = desk = job = proc = thread = None
    size = C.c_size_t()
    _k32.InitializeProcThreadAttributeList(None, 2, 0, C.byref(size))
    attrs = C.create_string_buffer(size.value)
    attrs_ready = False
    try:
        _check(_k32.CreatePipe(C.byref(rd), C.byref(wr), C.byref(sa), 1 << 16), "CreatePipe")
        _k32.SetHandleInformation(rd, HANDLE_FLAG_INHERIT, 0)
        nul = _k32.CreateFileW("NUL", 0x80000000, 3, C.byref(sa), 3, 0, None)  # GENERIC_READ, share r/w, OPEN_EXISTING
        if nul == INVALID_HANDLE:
            nul = None
            raise SandboxError("opening NUL for the tool's stdin failed")
        desk, desk_name = _desktop(sid)

        _check(_k32.InitializeProcThreadAttributeList(attrs, 2, 0, C.byref(size)), "InitializeProcThreadAttributeList")
        attrs_ready = True
        _check(_k32.UpdateProcThreadAttribute(attrs, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, C.byref(sc),
                                              C.sizeof(sc), None, None), "UpdateProcThreadAttribute(capabilities)")
        inherit = (W.HANDLE * 2)(wr, nul)       # only these two handles cross into the tool
        _check(_k32.UpdateProcThreadAttribute(attrs, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, inherit, C.sizeof(inherit),
                                              None, None), "UpdateProcThreadAttribute(handles)")
        si = _StartupInfoEx()
        si.StartupInfo.cb = C.sizeof(_StartupInfoEx)
        si.StartupInfo.lpDesktop = desk_name
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        si.StartupInfo.hStdInput, si.StartupInfo.hStdOutput, si.StartupInfo.hStdError = nul, wr, wr
        si.lpAttributeList = C.cast(attrs, C.c_void_p)

        job = _k32.CreateJobObjectW(None, None)
        _check(job, "CreateJobObject")
        lim = _ExtendedLimits()
        lim.Basic.LimitFlags = (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
                                | JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_PRIORITY_CLASS
                                | (JOB_OBJECT_LIMIT_JOB_MEMORY if memory_mb else 0))
        lim.Basic.ActiveProcessLimit = max(1, max_processes)
        lim.Basic.PriorityClass = BELOW_NORMAL_PRIORITY_CLASS
        lim.JobMemoryLimit = memory_mb * 1024 * 1024
        _check(_k32.SetInformationJobObject(job, JOB_EXTENDED_LIMITS, C.byref(lim), C.sizeof(lim)), "job limits")
        ui = W.DWORD(JOB_UI_ALL)
        _check(_k32.SetInformationJobObject(job, JOB_UI_RESTRICTIONS, C.byref(ui), C.sizeof(ui)), "job UI limits")
        if 0 < cpu_percent < 100:
            rate = _CpuRate(CPU_RATE_ENABLE_HARD_CAP, int(cpu_percent * 100))
            _check(_k32.SetInformationJobObject(job, JOB_CPU_RATE, C.byref(rate), C.sizeof(rate)), "job CPU cap")

        pi = _ProcessInformation()
        cmd = C.create_unicode_buffer(subprocess.list2cmdline(argv))
        block = _env_block(env)
        flags = (EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT
                 | BELOW_NORMAL_PRIORITY_CLASS)
        _k32.SetErrorMode(SEM_NO_DIALOGS)     # inherited: a missing DLL or a crash ends the tool without a dialog
        if not _k32.CreateProcessW(argv[0], cmd, None, None, True, flags, block, str(cwd), C.byref(si), C.byref(pi)):
            err = C.get_last_error()
            if err == 2 and Path(argv[0]).is_file():
                raise SandboxError("Windows can't find the tools' app container: `ibasho tool setup` makes it")
            raise SandboxError(f"starting the tool failed: {C.FormatError(err).strip()}")
        proc, thread = pi.hProcess, pi.hThread
        if not _k32.AssignProcessToJobObject(job, proc):
            err = C.get_last_error()
            _k32.TerminateProcess(proc, 1)
            raise SandboxError(f"putting the tool in its job failed: {C.FormatError(err).strip()}")
        _k32.ResumeThread(thread)
        _k32.CloseHandle(wr)
        wr = None

        out, reason = bytearray(), [""]
        done = threading.Event()

        def pump(handle: int):
            import msvcrt
            f = os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY), "rb", buffering=0)
            try:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    room = max_output - len(out)
                    out.extend(chunk[:room])
                    if on_output and room > 0:
                        on_output(chunk[:room])
                    if len(chunk) > room:
                        reason[0] = reason[0] or "output"
                        _k32.TerminateJobObject(job, 1)
                        break
            except OSError:
                pass
            finally:
                f.close()
                done.set()

        t0 = time.monotonic()
        reader = threading.Thread(target=pump, args=(rd.value,), daemon=True)
        rd = None                         # the reader's file object owns it now
        reader.start()
        if _k32.WaitForSingleObject(proc, max(1, int(timeout * 1000))) == WAIT_TIMEOUT:
            reason[0] = reason[0] or "timeout"
        # the main process is gone (or out of time): end anything it left running, which also closes the pipe
        _k32.TerminateJobObject(job, 1)
        _k32.WaitForSingleObject(proc, 5000)
        done.wait(10)
        code = W.DWORD()
        _k32.GetExitCodeProcess(proc, C.byref(code))
        return Result(None if reason[0] else int(code.value), bytes(out), reason[0], time.monotonic() - t0)
    finally:
        for h in (wr, rd, nul, thread, proc, job):
            if h:
                _k32.CloseHandle(h)
        if desk:
            _u32.CloseDesktop(desk)
        if attrs_ready:
            _k32.DeleteProcThreadAttributeList(attrs)
        for p in sids:
            _k32.LocalFree(p)
