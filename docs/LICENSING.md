# Licensing guide

ds4Xtend is licensed under the **GNU General Public License v3.0 or later**
(`GPL-3.0-or-later`). The full text is in [LICENSE](../LICENSE); the user-facing
summary and credits are in the [README](../README.md#license).

Copyright (C) 2026 Grant Harris.

## Why GPLv3

ds4Xtend adapts its visual theme (palette, radii, acrylic/glass surfaces, the "X"
logo treatment) from [UIXtend](https://github.com/gstroudharris/UIXtend), which is
GPLv3. A work that builds on GPLv3 material is itself distributed under GPLv3, so
the whole project is GPLv3-or-later. This is a deliberate, copyleft choice: anyone
who receives the software can read, modify, and redistribute it under the same terms.

## Per-file SPDX headers

Every source file starts with a two-line SPDX header, placed **after** any shebang
or `<!DOCTYPE>` so those keep working. SPDX is a concise, machine-readable notice;
the authoritative terms live in `LICENSE`.

| File type | Header |
|---|---|
| `.py`, `.sh`, `ds4Xtend` | `# SPDX-License-Identifier: GPL-3.0-or-later`<br>`# Copyright (C) 2026 Grant Harris` |
| `.js` | `// SPDX-License-Identifier: GPL-3.0-or-later`<br>`// Copyright (C) 2026 Grant Harris` |
| `.css` | `/* SPDX-License-Identifier: GPL-3.0-or-later */`<br>`/* Copyright (C) 2026 Grant Harris */` |
| `.html` | `<!-- SPDX-License-Identifier: GPL-3.0-or-later -->`<br>`<!-- Copyright (C) 2026 Grant Harris -->` |

Examples:

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Grant Harris
"""Module docstring stays the first statement — comments before it are fine."""
```

```html
<!DOCTYPE html>
<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- Copyright (C) 2026 Grant Harris -->
<html lang="en">
```

**Exceptions:** `.json` files (e.g. each tool's `spec.json`) can't carry comments,
so they have no header — they're covered by the project license like everything
else. Markdown docs likewise carry no SPDX line.

### Adding a new file

Add the matching header above. New Agent-mode tools should follow
[`code/Agent_Tools/TOOL_TEMPLATE.md`](../code/Agent_Tools/TOOL_TEMPLATE.md), whose
`tool.py` template already includes the header.

### Verifying coverage

This lists any source file that's missing the header (should print nothing):

```bash
git ls-files \
  | grep -vE '\.venv/' \
  | grep -E '\.(js|py|css|html|sh)$|^ds4Xtend$' \
  | while read -r f; do head -5 "$f" | grep -q 'SPDX-License-Identifier' || echo "MISSING: $f"; done
```

## Third-party components

| Component | License | How it's used | Compatibility |
|---|---|---|---|
| [UIXtend](https://github.com/gstroudharris/UIXtend) theme | GPL-3.0 | Visual design adapted into `styles.css` | Source of the GPLv3 obligation |
| [ddgs](https://github.com/deedy5/ddgs) | MIT | Keyless web search (`web_search` tool) | Permissive → GPLv3-compatible |
| [trafilatura](https://github.com/adbar/trafilatura) | Apache-2.0 | HTML→text extraction (`web_scrape` tool) | Apache-2.0 → GPLv3-compatible |
| [ds4-server](https://github.com/antirez/ds4) (DeepSeek V4 Flash) | (its own) | Inference backend, reached over HTTP | Separate process — not linked or bundled, no license entanglement |

`ddgs` and `trafilatura` are **runtime dependencies**: they're `pip install`ed into a
local, git-ignored venv (`code/Agent_Tools/.venv/`, from `requirements.txt`) and are
neither vendored into this repo nor redistributed with it, so their permissive
licenses raise no conflict. ds4-server runs as its own process and ds4Xtend only
talks to it over HTTP — there is no source dependency in either direction.

### Transitive dependencies

The full tree pulled in by `ddgs` and `trafilatura` was audited and is GPLv3-compatible:
`lxml` (BSD), `courlan` / `htmldate` / `fake-useragent` (Apache-2.0), `certifi` (MPL-2.0),
`primp` / `socksio` / `charset-normalizer` / `h11` / `anyio` (MIT), `httpx` / `httpcore` (BSD),
`dateparser` (BSD), `regex` (Apache-2.0 AND CNRI-Python), and the rest are permissive.

One package needs a license **choice**: **`tld`** (transitive via `trafilatura → courlan → tld`)
is tri-licensed `MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later`. The `OR` is the licensee's choice,
so ds4Xtend takes it under the **LGPL-2.1-or-later** arm — which is GPLv3-compatible — not the
`GPL-2.0-only` arm. (It's a runtime dependency this repo never redistributes, so nothing about it
attaches to the repo's own license either way.)

**Scope of the audit.** Only `ddgs`, `trafilatura`, and their transitive deps are project
dependencies. The venv is built with `--system-site-packages`, so enumerating *every* package the
venv can import also surfaces the host's Ubuntu Python packages (e.g. `pycairo`, `ufw`, `repoman`) —
those belong to the operating system, not to ds4Xtend, and are outside this project's licensing
surface. To reproduce the audit, inspect only the distributions physically installed under
`code/Agent_Tools/.venv/lib/python3.12/site-packages/*.dist-info` (their `License-Expression` /
`License ::` classifier fields).

## Reusing this code

You may copy, modify, and redistribute ds4Xtend under GPLv3-or-later. If you
distribute a modified version, keep it under GPLv3-or-later, preserve the copyright
and license notices, and make the corresponding source available. See [LICENSE](../LICENSE)
for the binding terms.
