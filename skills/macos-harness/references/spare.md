# A spare for the next step

Each step starts Python and loads the harness before it runs. A shell can keep
one process loaded ahead of time, and the next step from that shell runs in it.
Start it once per shell session. In omp's shell:

```bash
coproc SPARE { macos-harness spare; }; export MACOS_HARNESS_SPARE="${SPARE[0]},${SPARE[1]}"
```

In zsh:

```zsh
coproc macos-harness spare; exec 3<&p 4>&p; export MACOS_HARNESS_SPARE=3,4
```

## What runs in it

A program on stdin, and `macos-harness run` with stdin from `/dev/null`, run in
the spare with the step's own output, exit status, working directory, and
environment. A spare serves one step and exits, and the next one loads while
that step runs, so nothing carries over from one step to the next.

A step runs in its own process, as it would without a spare, when its output is
a terminal, it uses other Python flags or load-time settings, another step from
the same shell is running, no spare is ready yet, or the installed code changed
since the spare loaded. After an update, the first step runs alone and the
spare reloads.

## What differs

- A traceback from a program that fails starts with the spare's frames instead
  of the `macos-harness` command's; the frames of the program itself are the
  same.
- A process the step leaves running keeps running, but what it prints after
  the step ends is dropped instead of reaching the shell.
- Only stdin, stdout, and stderr reach the program; other files the shell has
  open do not.
- The program runs in a process group of its own, not the step's.

## Lifetime

The spare and the process that keeps it exit with the shell, even when the
shell is killed. A step running in a spare when the shell exits, such as one
started with `nohup` or left in the background, runs to its end first, as its
own process would, and then they exit. A spare exits with the step it serves.
They listen nowhere: only commands the shell starts hold the pipes.
