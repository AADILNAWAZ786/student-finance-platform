"""Cross-platform peak memory measurement for a child process, with no extra dependency."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes


class ProcessPeakRSS:
    """Measure a child process peak working set on Windows, or child peak RSS via resource."""

    def __init__(self, pid: int, interval: float = 0.20):
        self.pid = int(pid)
        self.interval = interval
        self.peak_mb = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._handle = None
        self._psapi = None
        self._kernel32 = None

    def start(self) -> "ProcessPeakRSS":
        if os.name == "nt":
            self._start_windows()
            self._sample_windows()
            self._thread = threading.Thread(target=self._poll, daemon=True)
            self._thread.start()
        return self

    def _start_windows(self) -> None:
        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        self._counter_type = PROCESS_MEMORY_COUNTERS
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._psapi = ctypes.WinDLL("Psapi.dll", use_last_error=True)
        self._kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
            wintypes.DWORD,
        ]
        self._psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
        self._handle = self._kernel32.OpenProcess(0x0400 | 0x0010, False, self.pid)
        if not self._handle:
            raise OSError(ctypes.get_last_error(), "OpenProcess failed for memory measurement")

    def _sample_windows(self) -> None:
        if not self._handle:
            return
        counters = self._counter_type()
        counters.cb = ctypes.sizeof(self._counter_type)
        ok = self._psapi.GetProcessMemoryInfo(
            self._handle, ctypes.byref(counters), counters.cb
        )
        if ok:
            self.peak_mb = max(self.peak_mb, counters.PeakWorkingSetSize / (1024 * 1024))

    def _poll(self) -> None:
        while not self._stop.wait(self.interval):
            self._sample_windows()

    def stop(self) -> float:
        if os.name == "nt":
            self._sample_windows()
            self._stop.set()
            if self._thread:
                self._thread.join(timeout=2)
            self._sample_windows()
            if self._handle:
                self._kernel32.CloseHandle(self._handle)
                self._handle = None
        else:
            import resource

            raw = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
            # Linux/BSD report KiB; macOS reports bytes.
            self.peak_mb = raw / (1024 * 1024 if sys.platform == "darwin" else 1024)
        return round(self.peak_mb, 1)


def run_monitored(argv, *, cwd=None, env=None):
    """Run a child command and return (CompletedProcess, peak_rss_mb)."""
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    monitor = ProcessPeakRSS(proc.pid).start()
    stdout, stderr = proc.communicate()
    peak = monitor.stop()
    result = subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
    return result, peak
