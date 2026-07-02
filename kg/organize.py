"""LLM-proposed taxonomy for flat dumps (no folder structure to inherit).

Two phases:
  1. One call over all page titles -> category tree (<= max_top_categories).
  2. Batched calls assigning each page id to one taxonomy path.
Only ever applied to pages not already placed in the tree.
"""
from __future__ import annotations

from .util import slugify, truncate

_SYSTEM = (
    "You are an information architect organizing an internal company wiki. "
    "You respond with valid JSON only — no prose, no markdown fences."
)


def _taxonomy_paths(taxonomy: dict) -> list[str]:
    paths = []
    for cat in taxonomy.get("categories", []):
        cslug = slugify(str(cat.get("slug") or cat.get("name", "misc")))
        paths.append(cslug)
        for sub in cat.get("subcategories", []) or []:
            paths.append(f"{cslug}/{slugify(str(sub.get('slug') or sub.get('name', 'misc')))}")
    return paths


def propose_taxonomy(cfg, llm, pages) -> list[str]:
    listing = "\n".join(
        f"- {p.id} | {p.title} | {truncate(' '.join(p.body.split()), 120)}" for p in pages
    )
    sub_rule = (
        "Add subcategories (max 6 per category) only where at least 5 pages clearly warrant one."
        if cfg.organize.allow_subcategories
        else "Do not use subcategories."
    )
    prompt = f"""These are pages from a company Confluence space (id | title | opening words):

{listing}

Design a folder taxonomy to organize them.
Rules:
- At most {cfg.organize.max_top_categories} top-level categories. {sub_rule}
- kebab-case slugs; short human names; one-line descriptions.
- Prefer broadly useful categories over one-off buckets.

Return JSON only:
{{"categories": [{{"slug": "...", "name": "...", "description": "...",
  "subcategories": [{{"slug": "...", "name": "...", "description": "..."}}]}}]}}"""
    taxonomy = llm.complete_json(prompt, system=_SYSTEM, max_tokens=3000)
    paths = _taxonomy_paths(taxonomy)
    if not paths:
        raise RuntimeError("taxonomy proposal returned no categories")
    print(f"[organize] taxonomy: {', '.join(paths)}")
    return paths


def assign(cfg, llm, pages) -> dict[str, str]:
    paths = propose_taxonomy(cfg, llm, pages)
    path_set = set(paths)
    assignments: dict[str, str] = {}
    batch_size = cfg.organize.assign_batch_size

    for start in range(0, len(pages), batch_size):
        batch = pages[start : start + batch_size]
        listing = "\n".join(f"- {p.id} | {p.title}" for p in batch)
        prompt = f"""Valid category paths:
{chr(10).join('- ' + p for p in paths)}

Assign each page below to exactly one path from the list above (best topical fit).

Pages (id | title):
{listing}

Return JSON only: {{"assignments": {{"<page id>": "<path>", ...}}}}"""
        result = llm.complete_json(prompt, system=_SYSTEM, max_tokens=3000)
        for pid, path in (result.get("assignments") or {}).items():
            assignments[str(pid)] = path if path in path_set else "misc"
        print(f"[organize] assigned {min(start + batch_size, len(pages))}/{len(pages)}")

    for p in pages:  # anything the model skipped
        assignments.setdefault(p.id, "misc")
    return assignments
