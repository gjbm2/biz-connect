---
name: sheet-io
description: Read and write Google Sheets via the service account. Use when the user wants to read spreadsheet data reliably (exact ranges, CSV/JSON), or write/append/clear cells, or create a sheet — anything the read-only Google Drive connector cannot do. The target sheet must be shared with the service-account email.
allowed-tools: Bash(bizconnect *), Bash(python *), Bash(python3 *), Bash(py *), Read
---

# Google Sheets read/write

The read-only Drive connector can read a sheet interactively but cannot write. This
tool writes (and reads reliably) via the central service account.

```bash
bizconnect sheet whoami                 # service-account email to share with
bizconnect sheet check  <sheet-url>      # access pre-flight; lists tabs
bizconnect sheet read   <sheet-url> --tab "Sheet1" --range A1:F --format csv
bizconnect sheet write  <sheet-url> --range A1 --csv data.csv --input user
bizconnect sheet append <sheet-url> --csv more.csv
bizconnect sheet clear  <sheet-url> --range A2:Z
```

If `bizconnect` isn't found (the plugin was installed in this session, or you're in a
clone), run the launcher with the same arguments:
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" <service> <verb> ...` (`python` or `py`
on Windows).

First step is almost always `sheet whoami` to get the service-account email, then ask
the user to **share the target sheet** with it (Editor to write, Viewer to read). If a
call returns `NO ACCESS [403/404]`, sharing hasn't taken effect yet.

`--input user` parses formulas/dates/numbers as if typed; default `raw` stores values
verbatim. Reads return unformatted values (numbers as numbers).
