import os
import subprocess
import sys

import pytest

from datagen.platform_metrics import ProcessPeakRSS, run_monitored


def test_sampler_can_measure_current_process():
    sampler = ProcessPeakRSS(os.getpid()).start()
    value = sampler.stop()
    assert value >= 0


def test_run_monitored_captures_output_and_exit_code():
    proc, peak = run_monitored(
        [sys.executable, "-c", "print('phase9-metrics-ok')"],
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "phase9-metrics-ok"
    assert peak >= 0


def test_run_monitored_preserves_nonzero_exit_code():
    proc, _ = run_monitored([sys.executable, "-c", "raise SystemExit(7)"])
    assert proc.returncode == 7
