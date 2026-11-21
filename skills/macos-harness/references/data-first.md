# Read app data before the GUI

Read files, settings and databases first; drive the GUI for the change; prove it in the data. `mac.data` only reads: never write an app's store, preferences or database directly.

```python
found = mac.data.find_files(kind="com.adobe.pdf", onlyin="~/Downloads")
info = mac.data.file_info(found["paths"][0])
info["where_froms"], info["downloaded"], info["authors"], info["content_type"]
```

- `find_files` uses Spotlight: `query` is `mdfind` syntax (`kMDItemWhereFroms == "*github*"c`), `content=`/`name=` for words, `kind` matches subtypes. Unindexed files are missed; raises `timeout` (10 s) instead of hanging.
- `run_shortcut(name, input=, output=, timeout=60)` runs headless; read a shortcut's actions before running one you did not write.
- `read_defaults(domain, key=None)` (`-g` global), `read_plist(path)`, `query_sqlite(path, sql, params)` (read-only; includes the WAL; `immutable=True` only on copies).
- Sandboxed apps' data under `~/Library/Containers/<bundle id>` and guarded folders raise `unsupported_op` without Full Disk Access: read from the window or ask for access.

## Verify a save

A banner or closed sheet is not a save:

```python
before = time.time()
mac.do.menu("File > Save", app="TextEdit")
mac.data.wait_file("~/Documents/note.txt", contains="total: 42", since=before)

mac.data.wait_value(lambda: mac.data.read_defaults("com.apple.finder", "ShowHardDrivesOnDesktop"), True)
```

`wait_file` raises `timeout` with `reason` (`missing`, `not_modified_since`, `content_missing`); `contains` matches stored bytes (rich text escapes). Read the value before the change too: one already matching proves nothing.
