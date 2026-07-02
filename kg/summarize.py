"""Bottom-up recursive summarization.

Phase A: every page gets a `summary` in frontmatter (skipped if present).
Phase B: every directory gets `_summary.md`, deepest directories first, so
         parents summarize their children's summaries, not raw text.
Phase C: `_root.md` at the top of the tree.

Idempotency: `_summary.md` stores an `inputs_hash` over its children's
(id, hash, summary) tuples; if inputs are unchanged the LLM call is skipped.
Combined with ingest clearing `summary` only on changed pages, a re-run after
a small Confluence edit re-summarizes just the affected path to the root.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import fm
from .config import Cfg
from .util import pmap, pretty, sha, truncate, first_sentence

_SYSTEM = (
    "You write terse, factual reference summaries for an internal company "
    "knowledge base. Plain prose. No preamble, no headings, no bullet lists, "
    "never refer to 'this page' or 'this document' — just state the content."
)


def _hidden(path: Path, root: Path) -> bool:
    return any(part.startswith(".") for part in path.relative_to(root).parts)


def iter_pages(kg_dir: Path):
    for path in sorted(kg_dir.rglob("*.md")):
        if path.name in ("_summary.md", "_root.md") or _hidden(path, kg_dir):
            continue
        meta, body = fm.read(path)
        yield path, meta, body


def _summarize_page(cfg: Cfg, llm, item):
    path, meta, body = item
    if cfg.dry_run:
        meta["summary"] = f"[dry-run] {meta.get('title', path.stem)}: {truncate(' '.join(body.split()), 100)}"
    else:
        prompt = (
            f"TITLE: {meta.get('title', path.stem)}\n\n"
            f"CONTENT:\n{truncate(body, cfg.summarize.max_input_chars)}\n\n"
            f"Summarize in at most {cfg.summarize.page_max_words} words. Capture the page's "
            "purpose plus the concrete facts an engineer would need: named systems, teams, "
            "people, decisions, numbers, and procedures."
        )
        meta["summary"] = llm.complete(prompt, system=_SYSTEM)
    fm.write(path, meta, body)


def _dir_children(cfg: Cfg, d: Path):
    """(hash_inputs, prompt_lines, child_ids) for a directory's direct children."""
    inputs, lines, ids = [], [], []
    for sub in sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith(".")):
        s = sub / "_summary.md"
        if not s.exists():
            continue
        smeta, sbody = fm.read(s)
        inputs.append((smeta.get("id", sub.name), sha(sbody), ""))
        lines.append(f"- [subsection] {smeta.get('title', pretty(sub.name))}: {truncate(sbody, 700)}")
        ids.append(smeta.get("id", sub.name))
    for f in sorted(d.glob("*.md")):
        if f.name in ("_summary.md", "_root.md"):
            continue
        meta, _ = fm.read(f)
        summary = str(meta.get("summary", ""))
        inputs.append((meta.get("id", f.stem), meta.get("content_hash", ""), summary))
        lines.append(f"- [page] {meta.get('title', f.stem)}: {truncate(summary, 500)}")
        ids.append(meta.get("id", f.stem))
    return sha(json.dumps(sorted(inputs), ensure_ascii=False)), lines, ids


def _summarize_dir(cfg: Cfg, llm, d: Path, kg_dir: Path):
    inputs_hash, lines, ids = _dir_children(cfg, d)
    out = d / "_summary.md"
    if not lines:
        out.unlink(missing_ok=True)  # dir emptied (e.g. pages manually re-filed)
        return
    if out.exists():
        old_meta, _ = fm.read(out)
        if old_meta.get("inputs_hash") == inputs_hash:
            return  # nothing beneath changed
    rel = d.relative_to(kg_dir)
    title = pretty(d.name)
    if cfg.dry_run:
        body = f"[dry-run] Section '{title}' covering {len(ids)} items."
    else:
        prompt = (
            f"SECTION: {rel.as_posix()} ({title})\n\nCONTENTS:\n" + "\n".join(lines) +
            f"\n\nWrite an overview of this section in at most "
            f"{cfg.summarize.section_max_words} words: what it covers, its key "
            "subtopics, and the most important specifics (systems, owners, processes)."
        )
        body = llm.complete(prompt, system=_SYSTEM)
    fm.write(out, {
        "id": rel.as_posix(), "title": title, "type": "summary",
        "children": ids, "inputs_hash": inputs_hash,
    }, body)


def _summarize_root(cfg: Cfg, llm):
    inputs_hash, lines, ids = _dir_children(cfg, cfg.kg_dir)
    if not lines:
        return
    out = cfg.kg_dir / "_root.md"
    if out.exists():
        old_meta, _ = fm.read(out)
        if old_meta.get("inputs_hash") == inputs_hash:
            return
    if cfg.dry_run:
        body = f"[dry-run] Knowledge base root covering {len(ids)} top-level items."
    else:
        prompt = (
            "TOP-LEVEL CONTENTS OF A COMPANY KNOWLEDGE BASE:\n" + "\n".join(lines) +
            f"\n\nWrite the root overview in at most {cfg.summarize.root_max_words} "
            "words: the major areas this knowledge base covers and what a reader "
            "will find in each. This is the first thing an AI agent reads to orient itself."
        )
        body = llm.complete(prompt, system=_SYSTEM)
    fm.write(out, {"id": "_root", "title": "Knowledge Base Root", "type": "root",
                   "children": ids, "inputs_hash": inputs_hash}, body)


def run(cfg: Cfg, llm=None) -> None:
    pages = list(iter_pages(cfg.kg_dir))
    todo = [(p, m, b) for p, m, b in pages if not m.get("summary")]
    print(f"[summarize] pages: {len(todo)} to summarize, {len(pages) - len(todo)} cached")
    pmap(lambda item: _summarize_page(cfg, llm, item), todo, cfg.llm.max_workers, "page summaries")

    dirs = sorted(
        {p.parent for p, _, _ in pages if p.parent != cfg.kg_dir},
        key=lambda d: len(d.relative_to(cfg.kg_dir).parts),
        reverse=True,
    )
    # include intermediate dirs that only contain subdirs
    all_dirs = sorted(
        {d for d in cfg.kg_dir.rglob("*") if d.is_dir() and not _hidden(d, cfg.kg_dir)} | set(dirs),
        key=lambda d: len(d.relative_to(cfg.kg_dir).parts),
        reverse=True,
    )
    by_depth: dict[int, list[Path]] = {}
    for d in all_dirs:
        by_depth.setdefault(len(d.relative_to(cfg.kg_dir).parts), []).append(d)
    for depth in sorted(by_depth, reverse=True):
        pmap(lambda d: _summarize_dir(cfg, llm, d, cfg.kg_dir), by_depth[depth],
             cfg.llm.max_workers, f"section summaries (depth {depth})")

    for d in all_dirs:  # deepest-first, so nested empties collapse upward
        if d.exists() and not any(d.iterdir()):
            d.rmdir()

    _summarize_root(cfg, llm)
    print("[summarize] done", file=sys.stderr)
