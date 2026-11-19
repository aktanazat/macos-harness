# Spare step benchmark

Measured on macOS, CPython 3.12.15, in this checkout's uv environment.
Exact command (from the repository root):

```sh
.venv/bin/python bench/spare_steps.py
```

Output:

```text
without spare: n=20, median=169.308 ms, mean=172.261 ms, served=0/20
with spare: n=20, median=90.594 ms, mean=88.724 ms, served=20/20
```

Each step calls the real, read-only `mac.list_apps()` and prints its PID and
application count. Each mode has three untimed warmups and twenty timed steps.
Timing includes command startup, IPC, listing apps, output, and process exit.
There is a 0.5-second decision interval outside each timed step to let the next
spare load. Broker startup is not included. The PID check confirms every
measured spare-mode step actually ran in a spare, not its fallback process.
Median wall time fell 46.5%; mean fell 48.5%. These are paced steps, not a
measurement of maximum back-to-back throughput.
