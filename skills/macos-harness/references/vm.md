# Disposable VMs

Work that must not touch this Mac's desktop runs in a throwaway macOS virtual
machine. The VM has its own screen, pointer, keyboard focus, and clipboard, and
starts without a window on this Mac.

```bash
macos-harness vm create t1                 # clone the base, about a second
macos-harness vm start t1                  # boot; returns when the guest answers
macos-harness --vm t1 -c 'print(mac.launch("Calculator"))'
macos-harness --vm t1 -c 'print(mac.see("Calculator"))'
macos-harness vm snapshot t1 clean         # keep the VM, memory included, as "clean"
macos-harness vm reset t1 --to clean       # roll back to it; without --to, to the base
macos-harness vm suspend t1               # save its memory and stop
macos-harness vm start t1                  # resume where it was, about 3 s
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

## Suspend, snapshots, and resets

`suspend` saves a running guest's memory to disk and stops it, in about 2 s;
`start` then resumes it with its apps open in about 3 s instead of a 60 s
boot. `snapshot` of a running VM suspends it, copies it as an APFS clone in
under a second, and resumes it, so the snapshot holds the open apps too.
`reset --to LABEL` drops the VM's current state and puts the snapshot back,
running again in about 10 s when the VM was running. `reset` without `--to`
clones the base and boots it cold.

`stop` cuts the guest's power rather than shutting macOS down; the guest
flushes its disk first, but an app's unsaved state is lost. Stopping a
suspended VM discards its saved memory. `create --from` and `reset` to the
base refuse a base that is running, since its disk would copy mid-write.

## Running a checkout live

```bash
macos-harness vm start t1 --source ~/Developer/macos-harness
```

With `--source`, each later `--vm` call sends the guest that checkout's
package as it stands at the call, and the guest runs it in place of its
installed harness. A change on this Mac is live in the next call without
rebuilding the base; the guest keeps the last three copies and is sent a new
one only when a file changed. The source holds across `suspend`, `snapshot`
and `reset`; `vm start` without `--source` goes back to the guest's own
harness. The guest's server itself stays the installed version, and a base
too old to take a source makes `start --source` fail.

The guest keeps one process loaded ahead of time for the next `--vm` call, as
`macos-harness skill spare` describes, so a call takes about 0.25 s instead
of 0.5 s. A call that arrives while another one runs loads its own.

## The base

`vm create` clones `macos-harness-base` unless `--from` names another tart VM.
A base is a macOS guest that:

- logs in by itself at boot, so `start` needs no one at the screen;
- has this harness installed, at the same version as the host, with
  `macos-harness vm install-guest` run once, which starts `macos-harness vm serve`
  from Terminal at each login;
- has granted Terminal Accessibility and Screen Recording.

`vm serve` keeps the guest's display awake while it runs; a guest left
without input otherwise sleeps its display, locks its screen, and leaves
nothing to capture.

Shut the base down cleanly after setting it up; every clone starts from that
disk. A change to `vm serve` itself means updating the base; other harness
changes can run with `--source`.

To rebuild the base after such a change: build a wheel, start a clone of the
base, write the wheel into the guest with a `--vm` call and install it there
with `uv tool install --reinstall`, `vm stop` the clone, then `tart clone` it
over the base with `--overwrite`.

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
