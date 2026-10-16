"""
outside_view.py — what a running child process really holds, read from outside it (Windows, test-only).

What:  Reads, through the operating system and the child's process handle,
       the child's environment block, current directory and command line
       (NtQueryInformationProcess for the address of its PEB, then
       ReadProcessMemory of the PEB and of its RTL_USER_PROCESS_PARAMETERS),
       whether it is still running (GetExitCodeProcess), and its committed
       memory now and at its peak (psapi GetProcessMemoryInfo: PagefileUsage
       and PeakPagefileUsage, the quantity a Job Object's per-process memory
       limit caps).
Why:   A probe that prints its own os.environ shows what that probe saw, not
       what the real worker holds. Windows keeps one environment block per
       process, and every SetEnvironmentVariable call, os.environ writes and
       a .env loader included, replaces it; reading it from the parent while
       the real worker runs shows the variables the worker actually has.
       The same handle gives the committed memory the worker's ceiling is
       measured against.
How:   Imported only on Windows, by tests/unit/services/test_pdf_text_extraction.py
       and tests/unit/api/test_evidence_upload_admission.py, with a
       subprocess.Popen's handle (int(process._handle)), which carries the
       full access CreateProcess grants. Standard library only. The file
       name does not match python_files, so pytest never collects it.
"""

from __future__ import annotations

import ctypes
from collections.abc import Callable
from typing import NamedTuple

# Fixed Windows definitions (winternl.h, winnt.h, minwinbase.h, psapi.h), not
# tunable values: the PROCESSINFOCLASS for PROCESS_BASIC_INFORMATION, the
# NTSTATUS of success, the exit code GetExitCodeProcess reports for a process
# still running, and the page size memory is mapped in on x86 and x64.
_PROCESS_BASIC_INFORMATION_CLASS = 0
_STATUS_SUCCESS = 0
_STILL_ACTIVE = 259
_PAGE_BYTES = 4096
# The PEB opens with four one-byte fields (InheritedAddressSpace,
# ReadImageFileExecOptions, BeingDebugged, BitField) before its pointers.
_PEB_LEADING_FLAG_BYTES = 4
# An environment block is UTF-16LE "NAME=value" strings, each ending in a NUL,
# and one more NUL after the last; so it ends at the first two NUL characters
# that start on a character boundary.
_WIDE_ENCODING = "utf-16-le"
_WIDE_CHARACTER_BYTES = 2
_BLOCK_TERMINATOR = b"\x00" * (2 * _WIDE_CHARACTER_BYTES)
_WIDE_NUL = "\0"
_NAME_VALUE_SEPARATOR = "="
# Far above any block this repository's tests start a process with; reading
# stops there rather than walk another process's memory without end.
_ENVIRONMENT_BLOCK_LIMIT_BYTES = 1024 * 1024

_WINDOWS_HANDLE = ctypes.c_void_p
_WINDOWS_BOOL = ctypes.c_int
_WINDOWS_DWORD = ctypes.c_uint32
_NTSTATUS = ctypes.c_long


class _ProcessBasicInformation(ctypes.Structure):
    """PROCESS_BASIC_INFORMATION, laid out as winternl.h defines it."""

    _fields_ = [
        ("ExitStatus", _NTSTATUS),
        ("PebBaseAddress", ctypes.c_void_p),
        ("AffinityMask", ctypes.c_size_t),
        ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", ctypes.c_size_t),
        ("InheritedFromUniqueProcessId", ctypes.c_size_t),
    ]


class _PebHead(ctypes.Structure):
    """The start of the PEB, up to its ProcessParameters pointer: four bytes of flags, then four pointers."""

    _fields_ = [
        ("Flags", ctypes.c_ubyte * _PEB_LEADING_FLAG_BYTES),
        ("Mutant", ctypes.c_void_p),
        ("ImageBaseAddress", ctypes.c_void_p),
        ("Ldr", ctypes.c_void_p),
        ("ProcessParameters", ctypes.c_void_p),
    ]


class _UnicodeString(ctypes.Structure):
    """UNICODE_STRING: a length in bytes, a capacity, and a pointer to UTF-16LE characters."""

    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_void_p),
    ]


class _CurrentDirectory(ctypes.Structure):
    """CURDIR: the current directory's path and an open handle to it."""

    _fields_ = [("DosPath", _UnicodeString), ("Handle", ctypes.c_void_p)]


class _ProcessParametersHead(ctypes.Structure):
    """The start of RTL_USER_PROCESS_PARAMETERS, up to its Environment pointer."""

    _fields_ = [
        ("MaximumLength", ctypes.c_uint32),
        ("Length", ctypes.c_uint32),
        ("Flags", ctypes.c_uint32),
        ("DebugFlags", ctypes.c_uint32),
        ("ConsoleHandle", ctypes.c_void_p),
        ("ConsoleFlags", ctypes.c_uint32),
        ("StandardInput", ctypes.c_void_p),
        ("StandardOutput", ctypes.c_void_p),
        ("StandardError", ctypes.c_void_p),
        ("CurrentDirectory", _CurrentDirectory),
        ("DllPath", _UnicodeString),
        ("ImagePathName", _UnicodeString),
        ("CommandLine", _UnicodeString),
        ("Environment", ctypes.c_void_p),
    ]


class _ProcessMemoryCounters(ctypes.Structure):
    """PROCESS_MEMORY_COUNTERS, laid out as psapi.h defines it."""

    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class ProcessView(NamedTuple):
    """One reading of a process: its environment (parsed and as the raw block), current directory and command line."""

    environment: dict[str, str]
    environment_block: str
    current_directory: str
    command_line: str


class CommittedMemory(NamedTuple):
    """A process's committed memory now and the most it has ever had committed, in bytes."""

    current: int
    peak: int


def read_process(handle: int) -> ProcessView:
    """Return the environment block, current directory and command line the process with this handle holds now.

    Raises OSError if Windows refuses a read, as it does once the process
    has exited and its memory is gone.
    """
    read = _memory_reader(handle)
    peb = _PebHead.from_buffer_copy(read(_peb_address(handle), ctypes.sizeof(_PebHead)))
    parameters = _ProcessParametersHead.from_buffer_copy(
        read(peb.ProcessParameters, ctypes.sizeof(_ProcessParametersHead))
    )
    block = _environment_block(read, parameters.Environment)
    return ProcessView(
        environment=parse_environment_block(block),
        environment_block=block,
        current_directory=_unicode_string(read, parameters.CurrentDirectory.DosPath),
        command_line=_unicode_string(read, parameters.CommandLine),
    )


def is_running(handle: int) -> bool:
    """Return whether the process with this handle has not yet exited."""
    kernel32 = _kernel32()
    exit_code = ctypes.c_uint32()
    if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
        raise ctypes.WinError(ctypes.get_last_error())
    return exit_code.value == _STILL_ACTIVE


def committed_memory(handle: int) -> CommittedMemory:
    """Return the process's committed memory now and at its peak; the handle stays valid after the process exits."""
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [_WINDOWS_HANDLE, ctypes.c_void_p, _WINDOWS_DWORD]
    psapi.GetProcessMemoryInfo.restype = _WINDOWS_BOOL
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return CommittedMemory(counters.PagefileUsage, counters.PeakPagefileUsage)


def parse_environment_block(block: str) -> dict[str, str]:
    """Return the variables of an environment block's text, its strings separated by NUL characters.

    A name may itself start with "=", as the hidden per-drive directory
    variables ("=C:") do, so the separator is the first "=" after the name's
    first character.
    """
    environment: dict[str, str] = {}
    for entry in block.split(_WIDE_NUL):
        if not entry:
            continue
        separator = entry.find(_NAME_VALUE_SEPARATOR, 1)
        environment[entry[:separator]] = entry[separator + 1:]
    return environment


def _kernel32():
    """Return kernel32 with ReadProcessMemory and GetExitCodeProcess declared, so no handle or address is truncated."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.ReadProcessMemory.argtypes = [
        _WINDOWS_HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)
    ]
    kernel32.ReadProcessMemory.restype = _WINDOWS_BOOL
    kernel32.GetExitCodeProcess.argtypes = [_WINDOWS_HANDLE, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.GetExitCodeProcess.restype = _WINDOWS_BOOL
    return kernel32


def _peb_address(handle: int) -> int:
    """Return the address of the process's PEB, from NtQueryInformationProcess."""
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtQueryInformationProcess.argtypes = [
        _WINDOWS_HANDLE, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)
    ]
    ntdll.NtQueryInformationProcess.restype = _NTSTATUS
    information = _ProcessBasicInformation()
    returned = ctypes.c_ulong()
    status = ntdll.NtQueryInformationProcess(
        handle,
        _PROCESS_BASIC_INFORMATION_CLASS,
        ctypes.byref(information),
        ctypes.sizeof(information),
        ctypes.byref(returned),
    )
    if status != _STATUS_SUCCESS:
        raise OSError(f"NtQueryInformationProcess failed with NTSTATUS {status:#x}")
    return information.PebBaseAddress


def _memory_reader(handle: int) -> Callable[[int, int], bytes]:
    """Return a function reading exactly size bytes at an address of the process, raising OSError on a short read."""
    kernel32 = _kernel32()

    def read(address: int, size: int) -> bytes:
        """Copy size bytes from the process's memory at address."""
        buffer = ctypes.create_string_buffer(size)
        copied = ctypes.c_size_t()
        if not kernel32.ReadProcessMemory(handle, address, buffer, size, ctypes.byref(copied)):
            raise ctypes.WinError(ctypes.get_last_error())
        if copied.value != size:
            raise OSError(f"read {copied.value} of {size} bytes")
        return buffer.raw

    return read


def _unicode_string(read: Callable[[int, int], bytes], string: _UnicodeString) -> str:
    """Return the characters a UNICODE_STRING in the process points at."""
    if not string.Length:
        return ""
    return read(string.Buffer, string.Length).decode(_WIDE_ENCODING)


def _environment_block(read: Callable[[int, int], bytes], address: int) -> str:
    """Return the environment block at address, read one page at a time so no read passes the page it ends on."""
    block = bytearray()
    cursor = address
    while len(block) < _ENVIRONMENT_BLOCK_LIMIT_BYTES:
        page_end = (cursor // _PAGE_BYTES + 1) * _PAGE_BYTES
        block += read(cursor, page_end - cursor)
        cursor = page_end
        end = _terminator_offset(block)
        if end is not None:
            return bytes(block[:end]).decode(_WIDE_ENCODING)
    raise OSError("no end of the environment block within the reading limit")


def _terminator_offset(block: bytearray) -> int | None:
    """Return where the block's closing pair of NUL characters starts, or None if it has not been read yet."""
    start = 0
    while True:
        found = block.find(_BLOCK_TERMINATOR, start)
        if found < 0:
            return None
        if found % _WIDE_CHARACTER_BYTES == 0:
            return found
        start = found + 1
