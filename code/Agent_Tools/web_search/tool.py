"""web_search — ranked web results (title, url, snippet) via the self-hosted ddgs metasearch library (no API key).

Returns only URLs + snippets; it never fetches page bodies (that's web_scrape's job — the two tools stay decoupled,
sharing only the url string). ddgs aggregates ~10 engines with backend='auto' fallback, so one blocked engine
doesn't sink the query. Results are deduped by URL, normalized, capped, and TTL-cached."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # make _web_common importable
import _web_common as wc

_CACHE_TTL = 1800        # 30 min — cheap reuse across the loop's per-iteration context wipes

# Engine order for ddgs. We do NOT use its default "auto" (which shuffles engines and pins wikipedia + grokipedia
# FIRST for text). Instead we DEFER to general web engines and DROP grokipedia entirely (low-quality source);
# wikipedia is kept last, only as a fallback. ddgs queries these in THIS order, in parallel, stopping once
# max_results is collected — and silently skips any name a given ddgs build doesn't have (logged, non-fatal).
_BACKENDS = "google,duckduckgo,brave,startpage,mojeek,yahoo,yandex,wikipedia"


def validate(args):
    if not (args.get("query") or "").strip():
        raise ValueError("provide a non-empty 'query'")
    if args.get("timelimit") not in (None, "", "d", "w", "m", "y"):
        raise ValueError("timelimit must be one of d / w / m / y")


def run(args, ctx):
    query = (args.get("query") or "").strip()
    try:
        n = int(args.get("max_results") or 5)
    except (TypeError, ValueError):
        n = 5
    n = max(1, min(n, 15))                                   # small by default; hard cap so output stays token-cheap
    timelimit = args.get("timelimit") or None

    ck = "%s|%d|%s" % (query, n, timelimit or "")
    cached = wc.cache_get("search", ck, _CACHE_TTL)
    if cached is not None:
        return cached

    try:
        from ddgs import DDGS
    except ImportError:
        return {"error": "web_search needs 'ddgs' — install it (see Agent_Tools/requirements.txt)"}
    try:
        raw = list(DDGS().text(query, max_results=n, timelimit=timelimit, backend=_BACKENDS))   # general engines first;
    except Exception as e:                                   # rate-limit/transient is common on one IP -> fail soft
        return {"error": "search unavailable: %s" % (str(e)[:160]), "query": query}

    seen, results = set(), []
    for r in raw:
        u = r.get("href") or r.get("url") or ""
        if not u or u in seen:                              # dedupe by URL (SearXNG-style)
            continue
        seen.add(u)
        results.append({"title": (r.get("title") or "")[:200], "url": u,
                        "snippet": (r.get("body") or r.get("snippet") or "")[:300]})
    out = {"query": query, "results": results, "count": len(results)}
    wc.cache_set("search", ck, out)
    return out
