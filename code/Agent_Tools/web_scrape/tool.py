# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Grant Harris
"""web_scrape — fetch one URL -> clean main-content markdown (+ metadata), SSRF-guarded and token-capped.

Lean by design: an impersonating HTTP fetch (primp — no browser) + trafilatura extraction + an optional BM25
query filter that returns only the relevant chunks. Untrusted page text is labeled (OWASP LLM01). JS/browser
rendering is intentionally NOT in v1; if extraction comes back ~empty the tool salvages inline structured data
(JSON-LD / OpenGraph) and flags that the page likely needs JS."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # make _web_common importable
import _web_common as wc

_CACHE_TTL = 1800        # 30 min — survives the loop's per-iteration context wipes
_DEFAULT_MAX = 6000      # chars returned by default (the frontend's capToolOutput trims further to fit ctx)


def validate(args):
    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("provide a non-empty 'url'")
    wc.validate_url(url)                                     # reject bad scheme / SSRF target early (clean 400)


def run(args, ctx):
    url = (args.get("url") or "").strip()
    query = (args.get("query") or "").strip()
    try:
        max_chars = int(args.get("max_chars") or _DEFAULT_MAX)
    except (TypeError, ValueError):
        max_chars = _DEFAULT_MAX
    max_chars = max(500, min(max_chars, 50000))
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0

    page = wc.cache_get("scrape", url, _CACHE_TTL)           # cached page is query-independent; filter applied on read
    if page is None:
        try:
            safe = wc.validate_url(url)
        except ValueError as e:
            return {"error": "blocked url: %s" % e, "url": url}
        try:
            resp = wc.fetch(safe, max_bytes=ctx.MAX_BYTES)
        except ValueError as e:
            return {"error": "blocked url: %s" % e, "url": url}
        except OSError as e:
            return {"error": "fetch failed: %s" % (str(e)[:160]), "url": url}
        try:
            md, title = wc.extract_markdown(resp["html"], url=resp["final_url"])
        except ImportError as e:
            return {"error": str(e), "url": url}
        if len(md) < 64:                                     # ~empty -> likely JS-rendered; salvage inline structured data
            alt = wc.inline_structured(resp["html"])
            if alt:
                md = alt
                title = title or (md.splitlines()[0].lstrip("# ").strip() if md else "")
        page = {"final_url": resp["final_url"], "title": title, "status": resp["status"],
                "content_type": resp["content_type"], "full": md, "fetched_bytes": resp["bytes"]}
        wc.cache_set("scrape", url, page)

    full = page.get("full") or ""
    body = wc.bm25_filter(full, query, max_chars) if query else full
    window = body[offset:offset + max_chars]
    out = {"url": url, "final_url": page.get("final_url"), "title": page.get("title"),
           "status": page.get("status"), "content_type": page.get("content_type"),
           "content": (wc.UNTRUSTED_PREFIX + window) if window else "",
           "chars": len(window), "truncated": (offset + len(window)) < len(body)}
    if not full:
        out["note"] = "no main content extracted — the page may require JavaScript (v1 does not render JS)"
    elif not query and out["truncated"]:
        out["note"] = ("returned the page from the start and it was truncated — call web_scrape again with "
                       "query=<what you're looking for> to get only the relevant sections instead")
    return out
