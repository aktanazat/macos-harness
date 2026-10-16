# Native backend (optional)

`mac.*` stays fully local unless you opt in. `MACOS_HARNESS_BACKEND` (`python`
default, `native`, `auto`) or `MacOS(backend=...)` picks the backend;
`python` never launches an agent, `native` raises immediately if the agent
is unreachable instead of falling back, and `auto` falls back to Python only
when the agent is unreachable before a real `ping` response comes back —
never after a protocol, semantic, permission, timeout, or mutating error.

Native is process isolation, not a speed mode — default `python` remains
the latency-recommended choice. Measured on an M4 Pro: a 50-query Finder
benchmark found a Python median of 1.75 ms against a native steady-state
median of 2.07 ms, plus ~240 ms of native cold-launch cost on first use.
Results are workload-specific; measure locally with `bench/ax_smoke.py`.

Each `MacOS(backend="native"/"auto")` instance that dispatches a routed call
launches its own private `macos-harness-agent` child process over an
inherited, validated UNIX-domain socket pair only that instance holds either
end of — no shared daemon, no well-known socket path, nothing for another
process to discover or connect to. The harness verifies the child's actual
PID from its first `ping` response. Executable resolution order: an
explicit `MACOS_HARNESS_AGENT_BIN` path (must exist and be executable, or
the launch fails immediately with no fallback), then the binary bundled
with the installed package, then a fresh local SwiftPM release build
(requires the Xcode Command Line Tools; rebuilt only when missing or stale,
and only at this tier). Call `mac.close()` when done, or use
`with MacOS(backend=...) as mac:` — both reap its private agent and pointer
helper and close their socket or pipe; `close()` is idempotent and also
cleans up the pointer on the `python` backend. A closed instance raises
`MacOSError` on further native-routed
calls instead of relaunching; construct a new `MacOS()` to use `native`/
`auto` again.

Only `list_apps`, `ax.query`/`ax.query_all`, `ax.press`, and the element
primitives `ax.get`/`ax.get_attributes`/`ax.set`/`ax.perform` can route to
the agent. Everything else — screenshots, keyboard/pointer input, the pointer
overlay, AppleScript, full app snapshots — always stays local. A native
`element_index` is interned into the same handle registry a local query
would use, so it behaves exactly like one: stale or reset indices still
raise instead of aliasing a different element.
