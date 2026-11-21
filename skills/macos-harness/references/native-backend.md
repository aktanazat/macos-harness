# Native backend (optional)

`MACOS_HARNESS_BACKEND` or `MacOS(backend=...)`: `python` (default, fastest), `native` (raises if the agent is unreachable), `auto` (falls back only when the agent never answered `ping`).

- Native is process isolation, not speed. Each instance launches a private `macos-harness-agent` over an inherited socket pair.
- Agent binary: `MACOS_HARNESS_AGENT_BIN`, else the bundled one, else a local SwiftPM release build (needs Xcode CLT).
- `mac.close()` or `with MacOS(backend=...) as mac:` reaps it; a closed instance raises instead of relaunching.
- A stopped agent's call raises without resending; its elements raise `element.unknown`: query again.
- Only `list_apps`, `ax.query`/`query_all`, `ax.press`, `ax.get`/`get_attributes`/`set`/`perform` route to the agent; everything else stays local.
