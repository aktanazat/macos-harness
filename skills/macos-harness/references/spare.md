# A spare for the next step

A shell keeps one harness process loaded so the next step skips startup. bash/omp:

```bash
coproc SPARE { macos-harness spare; }; export MACOS_HARNESS_SPARE="${SPARE[0]},${SPARE[1]}"
```

zsh:

```zsh
coproc macos-harness spare; exec 3<&p 4>&p; export MACOS_HARNESS_SPARE=3,4
```

- Stdin programs and `run` with stdin from `/dev/null` use it; each spare serves one step, so nothing carries over.
- A step runs on its own when output is a terminal, flags differ, another step holds the spare, none is ready, or the code changed.
- Only stdin/stdout/stderr reach the program; output from processes it leaves running is dropped.
- `macos-harness doctor` reports `active`, `not running` or `busy`. The spare exits with the shell.
