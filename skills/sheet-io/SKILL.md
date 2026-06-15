---
name: sheet-io
description: Read and write Google Sheets via the service account. Use when the user wants to read spreadsheet data reliably (exact ranges, CSV/JSON), or write/append/clear cells, or create a sheet — anything the read-only Google Drive connector cannot do. The target sheet must be shared with the service-account email.
allowed-tools: Bash(python *), Read
---

# Google Sheets read/write

The read-only Drive connector can read a sheet interactively but cannot write. This
tool writes (and reads reliably) via the central service account.

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" sheet whoami                 # service-account email to share with
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" sheet check  <sheet-url>      # access pre-flight; lists tabs
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" sheet read   <sheet-url> --tab "Sheet1" --range A1:F --format csv
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" sheet write  <sheet-url> --range A1 --csv data.csv --input user
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" sheet append <sheet-url> --csv more.csv
python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py" sheet clear  <sheet-url> --range A2:Z
```

First step is almost always `sheet whoami` to get the service-account email, then ask
the user to **share the target sheet** with it (Editor to write, Viewer to read). If a
call returns `NO ACCESS [403/404]`, sharing hasn't taken effect yet.

`--input user` parses formulas/dates/numbers as if typed; default `raw` stores values
verbatim. Reads return unformatted values (numbers as numbers).
