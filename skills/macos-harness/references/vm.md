# Disposable VMs

Work that must not touch this Mac's desktop runs in a throwaway macOS virtual
machine. The VM has its own screen, pointer, keyboard focus, and clipboard, and
starts without a window on this Mac.

```bash
macos-harness vm create t1                 # clone the base, about a second
macos-harness vm start t1                  # boot; returns when the guest answers
macos-harness --vm t1 -c 'print(mac.launch("Calculator"))'
macos-harness --vm t1 -c 'print(mac.see("Calculator"))'
macos-harness vm snapshot t1 clean         # keep the disk as "clean"
macos-harness vm reset t1 --to clean       # roll back to it; without --to, to the base
macos-harness vm delete t1                 # the VM, its snapshots, its files
```

`--vm NAME` runs `-c`, a program on stdin, `run FILE`, `see`, `state`, `apps`,
and `doctor` inside the guest with the same verbs and output. Pictures and other
files a program writes to its temporary directory come back under
`~/Library/Application Support/macos-harness/vms/NAME/files/`, and the paths it
prints point there. A path you pass in under that folder reaches the guest as
its own copy's path; other host paths do not exist in the guest.

Focus in the guest is the guest's own, so `mac.activate(app)` there is safe and
keeps a first click from being dropped by a background window.

`vm list` shows the VMs `vm create` made, their state, and their snapshots.
`snapshot` and `reset` power a running VM off for the copy and boot it again.
`stop` and those copies cut the guest's power rather than shutting macOS down;
the guest flushes its disk first, but an app's unsaved state is lost.
`create --from` and `reset` refuse a source VM that is running, since its disk
would copy mid-write; stop it first.

## The base

`vm create` clones `macos-harness-base` unless `--from` names another tart VM.
A base is a macOS guest that:

- logs in by itself at boot, so `start` needs no one at the screen;
- has this harness installed, at the same version as the host, with
  `macos-harness vm install-guest` run once, which starts `macos-harness vm serve`
  from Terminal at each login;
- has granted Terminal Accessibility and Screen Recording.

Shut the base down cleanly after setting it up; every clone starts from that
disk. A harness update means updating the base.

## Requirements and limits

- tart on `PATH`, or its path in `MACOS_HARNESS_TART`. Every tart call runs with
  `TART_NO_AUTO_PRUNE=1`; without it, a clone on a full disk deletes cached
  restore images.
- Each running guest takes the memory and CPUs its base was given; clones share
  the base's disk blocks until they change them.
- The guest answers only its gateway, which is this Mac, on port 47821, without
  a password. Treat the guest as untrusted: files it sends back cannot land
  outside its folder.
- `repl`, `spare`, `telemetry`, and `vm` run only on this Mac and refuse `--vm`.
- Each `create` and `reset` boots the guest with a new network address, and
  macOS holds each one for a day. A few hundred in one day use up the shared
  network's addresses, and `start` then times out.
