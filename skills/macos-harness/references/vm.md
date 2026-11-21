# Disposable VMs

A throwaway macOS guest with its own screen, focus and clipboard; no window on this Mac.

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
macos-harness vm put t1 ~/Applications/MyApp.app      # lands in the guest's /Applications
macos-harness vm put t1 ~/notes/sample.txt --to ~/Documents
macos-harness vm start t1 --source ~/Developer/macos-harness   # run this checkout in the guest
```

- `--vm NAME` works with `-c`, stdin, `run FILE`, `see`, `state`, `apps`, `doctor`. Files a program writes to its temp dir come back under `~/Library/Application Support/macos-harness/vms/NAME/files/`; only paths there reach the guest.
- `mac.activate(app)` is safe in the guest.
- `vm put` of a dev build re-signs it ad hoc and lists `dropped_entitlements`. Quit the app in the guest before putting a new build.
- To bring a file out: `shutil.copy(path, os.environ["TMPDIR"])` (64 MiB per call). No sound devices.
- `stop` cuts power (unsaved app state lost; discards suspended memory). `create --from` and base resets refuse a running base.
- `--source` sends the checkout each call; the guest server stays the installed version.

## The base

`macos-harness-base` (or `--from`) must auto-login, have this harness at the host's version with the native agent bundled, have run `macos-harness vm install-guest`, and have Terminal granted Accessibility and Screen Recording. Shut it down cleanly.

Rebuild: `scripts/build-universal-agent.sh` (after any test run), `uv build --wheel` (not bare `uv build`), check `zipinfo` lists `macos_harness/bin/macos-harness-agent` as `-rwxr-xr-x`, install it in a clone with `uv tool install --offline --reinstall-package macos-harness --python <version from uv-receipt.toml>`, check `MacOS(backend="native").list_apps()` there, `vm stop`, then `tart clone` over the base with `--overwrite`.

## Limits

- tart on `PATH` or `MACOS_HARNESS_TART`; calls run with `TART_NO_AUTO_PRUNE=1`.
- The guest listens on port 47821 to this Mac only, without a password; treat it as untrusted.
- `repl`, `spare`, `telemetry`, `vm` refuse `--vm`.
- Each create/reset takes a new network address held a day; a few hundred a day exhaust the pool and `start` times out.
