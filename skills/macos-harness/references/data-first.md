# Read app data before the GUI

An app's files, settings and databases often answer faster and more
truly than its window. Read them first; drive the GUI for the change,
then prove the change in the data. `mac.data` only reads: never write an
app's store, preferences or database directly, since the running app
holds its own copy and overwrites or corrupts yours.

## Find files and their history

```python
found = mac.data.find_files(kind="com.adobe.pdf", onlyin="~/Downloads")
info = mac.data.file_info(found["paths"][0])
info["where_froms"], info["downloaded"], info["authors"], info["content_type"]
```

`find_files` asks the Spotlight index `mdfind` reads, in this process:
about 8 ms against 45 ms for `mdfind`. `query` is `mdfind`'s attribute
syntax (`kMDItemWhereFroms == "*github*"c`); bare words go in `content=`
(a word in the text) or `name=` (part of the name). `kind` is a content
type and matches its subtypes: `public.image`, `public.audio`. Every
argument given must match. It returns `paths` (at most `limit`) and
`total`. A file Spotlight excludes or has not indexed yet is not found;
`file_info` still reads it, with `indexed` False and file-system fields
only. Dates are UTC ISO text; `where_froms` holds the page and file URL a
browser saved it from.
When Spotlight does not answer, as on a fresh VM still indexing, it
raises `timeout` after `timeout` seconds (10 by default) where `mdfind`
would hang.

## Shortcuts

`mac.data.shortcuts()` lists `{"name", "id"}`. `mac.data.run_shortcut(name,
input=path, output=path, timeout=60)` runs one as `shortcuts run` does,
with no window: input files go in as its input, and its result lands in
`output` or comes back as `stdout`. The result's `output` is None when
the shortcut wrote nothing there. A nonzero exit raises with
`exit_status` and `stderr` in `details`; a run past `timeout` is stopped
and raises `timeout`. A shortcut can still act on apps and send things:
read its actions before running one you did not write.

## Settings, plists and databases

- `mac.data.read_defaults(domain, key=None)`: a preference domain through
  `defaults export`, as the app reads it; a missing key is None. `-g` is
  the global domain.
- `mac.data.read_plist(path)`: any XML or binary property list.
- `mac.data.query_sqlite(path, sql, params)`: rows as dicts, from a
  connection opened `mode=ro` with `query_only`, so a write raises. A
  store the app has open is read with its write-ahead log, so recent
  writes count. `immutable=True` skips locks and the log; use it only on
  a copy or a store nothing is writing, since a live store read that way
  is missing what the log holds.

A sandboxed app (TextEdit, Notes, most App Store apps) keeps its
settings and stores in `~/Library/Containers/<bundle id>`, and some
system apps keep theirs in guarded folders such as `~/Library/Shortcuts`.
macOS hides these from a process not granted access to other apps' data
or Full Disk Access, and posts "Data Access Blocked". `read_defaults`,
`read_plist` and `query_sqlite` raise `unsupported_op` there instead of
reading as unset; read that value from the app's window, or ask the user
to grant the access. Finder, Dock and the global domain read fine.

## Verify a save against the data

A save banner, a closed sheet or a cleared dot in the title bar is not a
save. Take the time before the save, then wait on the file or value:

```python
before = time.time()
mac.do.menu("File > Save", app="TextEdit")
mac.data.wait_file("~/Documents/note.txt", contains="total: 42", since=before)

mac.data.wait_value(lambda: mac.data.read_defaults("com.apple.finder", "ShowHardDrivesOnDesktop"), True)
```

`wait_file` returns once the file exists, was modified at or after
`since`, and holds `contains`; it raises `timeout` with `reason`
(`missing`, `not_modified_since`, `content_missing`). `contains` matches
the stored bytes, so rich text formats escape some characters. `wait_value`
calls a reader until it returns the expected value and raises `timeout`
with the last value read. Read the value before the change too: one that
already matched proves nothing.
