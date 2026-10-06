"""Run the API and control worker. Domain workers have separate environments."""

import signal
import subprocess
import sys
import time

processes: list[subprocess.Popen] = []
stopping = False


def stop(*_):
    global stopping
    stopping = True


for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, stop)
code = 0
try:
    for args in (["serve"], ["control-worker"]):
        processes.append(
            subprocess.Popen(  # noqa: S603 -- fixed local commands
                [sys.executable, "-m", "copenhagen.cli", *args]
            )
        )
    while not stopping:
        for process in processes:
            if process.poll() is not None:
                code = process.returncode or 1
                stopping = True
                break
        time.sleep(0.2)
finally:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
sys.exit(code)
