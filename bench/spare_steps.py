"""Read-only per-step benchmark: .venv/bin/python bench/spare_steps.py."""

import os
import statistics
import subprocess
import sys
import time


def main() -> None:
    command = [sys.executable, "-m", "macos_harness.cli"]
    env = {key: value for key, value in os.environ.items() if key != "MACOS_HARNESS_SPARE"}
    env["DO_NOT_TRACK"] = "1"
    program = b"import os\nprint(os.getpid(), len(mac.list_apps()))\n"
    requests_end, requests = os.pipe()
    replies, replies_end = os.pipe()
    broker = subprocess.Popen(command + ["spare"], stdin=requests_end, stdout=replies_end, env=env)
    os.close(requests_end)
    os.close(replies_end)
    try:
        for active in (False, True):
            step_env = dict(env)
            if active:
                step_env["MACOS_HARNESS_SPARE"] = f"{replies},{requests}"
            times = []
            served = 0
            for index in range(23):
                # Model decision time, outside the timed step, so the next spare can load.
                time.sleep(0.5)
                start = time.perf_counter()
                step = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=step_env,
                                        pass_fds=(replies, requests) if active else ())
                out, err = step.communicate(program, timeout=30)
                elapsed = time.perf_counter() - start
                if step.returncode:
                    raise RuntimeError(err.decode())
                if index >= 3:
                    times.append(elapsed)
                    served += int(int(out.split()[0]) != step.pid)
            print(f"{'with' if active else 'without'} spare: n={len(times)}, "
                  f"median={statistics.median(times)*1000:.3f} ms, "
                  f"mean={statistics.mean(times)*1000:.3f} ms, served={served}/20", flush=True)
    finally:
        os.close(requests)
        os.close(replies)
        broker.wait(timeout=10)


if __name__ == "__main__":
    main()
