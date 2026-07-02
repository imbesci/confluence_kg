"""Ingest raw scraped files into the knowledge repo as frontmatter'd markdown.

Rules that make re-runs safe:
  * A page's `id` is minted once (from its source path) and then lives in
    frontmatter forever. Pages are located by id on re-runs.
  * If a page already exists anywhere in the tree, it is updated IN PLACE —
    the pipeline never moves files, so manual re-filing by humans is respected.
  * Unchanged content (same content_hash) is skipped entirely; changed content
    gets its stale `summary` cleared so the summarize step regenerates it.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import fm, organize
from .config import Cfg
from .util import slugify, pretty, sha

SOURCE_EXTS = {".txt", ".md", ".markdown", ".text"}
_URL_RE = re.compile(r"^\s*(?:url|source(?:_url)?|link)\s*[:=]\s*(\S+)\s*$", re.IGNORECASE)


@dataclass
class Page:
    id: str
    title: str
    body: str
    source_file: str
    mtime: str
    url: str | None = None
    target_rel: Path = field(default_factory=Path)  # relative dir inside kg_dir


def _derive_title_and_body(body: str, fallback_stem: str) -> tuple[str, str]:
    lines = body.splitlines()
    for i, raw in enumerate(lines[:5]):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            return line.lstrip("#").strip(), body  # keep heading in body
        if len(line) <= 90 and not _URL_RE.match(line):
            rest = "\n".join(lines[:i] + lines[i + 1 :]).strip()
            return line, rest  # plain title line: lift it out of the body
        break
    return pretty(fallback_stem), body


def _lift_url(body: str) -> tuple[str | None, str]:
    lines = body.splitlines()
    for i, raw in enumerate(lines[:8]):
        m = _URL_RE.match(raw)
        if m:
            return m.group(1), "\n".join(lines[:i] + lines[i + 1 :]).strip()
    return None, body


def load_source(cfg: Cfg) -> list[Page]:
    src = cfg.source_dir
    if not src.exists():
        raise SystemExit(f"source_dir does not exist: {src}")
    pages, seen_ids = [], set()
    for path in sorted(src.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SOURCE_EXTS:
            continue
        rel = path.relative_to(src)
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
        if not raw:
            continue
        url, body = _lift_url(raw)
        title, body = _derive_title_and_body(body, rel.stem)

        pid = "/".join(slugify(p) for p in rel.with_suffix("").parts)
        if pid in seen_ids:
            n = 2
            while f"{pid}-{n}" in seen_ids:
                n += 1
            pid = f"{pid}-{n}"
        seen_ids.add(pid)

        pages.append(
            Page(
                id=pid,
                title=title,
                body=body,
                source_file=str(rel),
                mtime=dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
                url=url,
                target_rel=Path(*[slugify(p) for p in rel.parent.parts]) if rel.parent != Path(".") else Path("."),
            )
        )
    return pages


def existing_pages(kg_dir: Path) -> dict[str, Path]:
    """Map frontmatter id -> file path for every page already in the tree."""
    out: dict[str, Path] = {}
    if not kg_dir.exists():
        return out
    for path in kg_dir.rglob("*.md"):
        if path.name in ("_summary.md", "_root.md"):
            continue
        meta, _ = fm.read(path)
        if meta.get("id"):
            out[str(meta["id"])] = path
    return out


def run(cfg: Cfg, llm=None) -> None:
    pages = load_source(cfg)
    if not pages:
        raise SystemExit(f"no source files found under {cfg.source_dir}")
    print(f"[ingest] {len(pages)} source pages found")

    existing = existing_pages(cfg.kg_dir)
    new_pages = [p for p in pages if p.id not in existing]

    # Optional LLM taxonomy for flat dumps: only ever applied to NEW pages.
    flat = all(p.target_rel == Path(".") for p in pages)
    mode = cfg.organize.enabled
    if new_pages and (mode == "always" or (mode == "auto" and flat)):
        if cfg.dry_run:
            print("[organize] dry-run: keeping source layout (no taxonomy calls)")
        else:
            assignments = organize.assign(cfg, llm, new_pages)
            for p in new_pages:
                p.target_rel = Path(assignments.get(p.id, "misc"))

    created = updated = unchanged = 0
    for p in pages:
        body_hash = sha(p.body)
        if p.id in existing:
            path = existing[p.id]  # in place — never move
            meta, _old_body = fm.read(path)
            if meta.get("content_hash") == body_hash:
                unchanged += 1
                continue
            meta.update(
                title=p.title, source_file=p.source_file,
                last_modified=p.mtime, content_hash=body_hash,
            )
            if p.url:
                meta["source_url"] = p.url
            meta.pop("summary", None)  # stale — regenerate downstream
            fm.write(path, meta, p.body)
            updated += 1
        else:
            path = cfg.kg_dir / p.target_rel / f"{slugify(Path(p.source_file).stem)}.md"
            meta = {
                "id": p.id, "title": p.title, "type": "page",
                "source_file": p.source_file, "last_modified": p.mtime,
                "content_hash": body_hash,
            }
            if p.url:
                meta["source_url"] = p.url
            fm.write(path, meta, p.body)
            created += 1

    print(f"[ingest] created={created} updated={updated} unchanged={unchanged}")
