"""Shared helpers used across the pipeline."""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import re
import sys


def slugify(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return (s[:max_len].rstrip("-")) or "page"


def pretty(name: str) -> str:
    """'oncall-escalation' -> 'Oncall Escalation'."""
    return re.sub(r"[-_]+", " ", name).strip().title()


def sha(text: str, n: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def truncate(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0] + " …[truncated]"


def first_sentence(text: str, max_chars: int = 160) -> str:
    text = " ".join(text.split())
    m = re.search(r"(?<=[.!?])\s", text)
    out = text[: m.start()] if m and m.start() < max_chars else text[:max_chars]
    return out.strip()


def json_from_text(text: str):
    """Extract a JSON object/array from an LLM reply (tolerates fences/preamble)."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    starts = [i for i in (t.find("{"), t.find("[")) if i != -1]
    if not starts:
        raise ValueError(f"no JSON found in: {t[:200]!r}")
    start = min(starts)
    end = max(t.rfind("}"), t.rfind("]"))
    if end <= start:
        raise ValueError(f"unbalanced JSON in: {t[:200]!r}")
    return json.loads(t[start : end + 1])


def pmap(fn, items, workers: int, desc: str = ""):
    """Ordered parallel map with lightweight progress output."""
    items = list(items)
    if not items:
        return []
    results = [None] * len(items)
    total, done = len(items), 0
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = {ex.submit(fn, item): i for i, item in enumerate(items)}
        for fut in cf.as_completed(futures):
            i = futures[fut]
            results[i] = fut.result()  # re-raises worker exceptions
            done += 1
            if desc and (done % 10 == 0 or done == total):
                print(f"  {desc}: {done}/{total}", file=sys.stderr)
    return results
