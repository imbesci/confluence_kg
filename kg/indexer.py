"""Vector index over the whole tree ("collapsed tree" retrieval).

Indexed node kinds:
  root / section : LLM-written summaries (match broad questions)
  page           : per-page frontmatter summaries (mid-level)
  chunk          : heading-split body sections (match specific questions)

The index is a disposable build artifact (build/ is gitignored): index.npz
holds normalized float32 embeddings, index.json holds node metadata.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import numpy as np

from . import fm
from .config import Cfg
from .util import slugify, truncate

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


# ---------------------------------------------------------------- embeddings
def embed_texts(cfg: Cfg, texts: list[str]) -> np.ndarray:
    backend = cfg.index.backend
    if backend == "st":
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            sys.exit("sentence-transformers not installed: pip install sentence-transformers")
        model = _get_st_model(cfg.index.model)
        embs = model.encode(texts, normalize_embeddings=True, batch_size=64,
                            show_progress_bar=len(texts) > 200)
        return np.asarray(embs, dtype=np.float32)
    if backend == "dummy":  # deterministic offline vectors — smoke tests ONLY
        out = np.empty((len(texts), 384), dtype=np.float32)
        for i, t in enumerate(texts):
            seed = int.from_bytes(hashlib.sha256(t.encode()).digest()[:4], "big")
            v = np.random.RandomState(seed).standard_normal(384)
            out[i] = (v / np.linalg.norm(v)).astype(np.float32)
        return out
    sys.exit(f"unknown embedding backend: {backend!r} (expected 'st' or 'dummy')")


_ST_CACHE: dict[str, object] = {}


def _get_st_model(name: str):
    if name not in _ST_CACHE:
        from sentence_transformers import SentenceTransformer
        _ST_CACHE[name] = SentenceTransformer(name)
    return _ST_CACHE[name]


# ------------------------------------------------------------------ chunking
def split_sections(body: str, max_chars: int) -> list[tuple[str, str]]:
    """-> [(heading, text)]; heading-aware, long sections split on paragraphs."""
    matches = list(_HEADING_RE.finditer(body))
    sections: list[tuple[str, str]] = []
    if not matches:
        sections.append(("", body))
    else:
        if body[: matches[0].start()].strip():
            sections.append(("", body[: matches[0].start()]))
        for k, m in enumerate(matches):
            end = matches[k + 1].start() if k + 1 < len(matches) else len(body)
            sections.append((m.group(2).strip(), body[m.end() : end]))

    out: list[tuple[str, str]] = []
    for heading, text in sections:
        text = text.strip()
        if not text and not heading:
            continue
        if len(text) <= max_chars:
            out.append((heading, text))
            continue
        buf = ""
        for para in re.split(r"\n\s*\n", text):
            if buf and len(buf) + len(para) > max_chars:
                out.append((heading, buf.strip()))
                buf = para
            else:
                buf = f"{buf}\n\n{para}" if buf else para
        if buf.strip():
            out.append((heading, buf.strip()))
    return out


# --------------------------------------------------------------------- build
def build(cfg: Cfg) -> None:
    entries, texts = [], []

    def add(node_id, path, kind, title, heading, text):
        entries.append({
            "id": node_id, "path": str(path.relative_to(cfg.kg_dir)), "kind": kind,
            "title": title, "heading": heading, "preview": truncate(" ".join(text.split()), 220),
        })
        texts.append(text)

    for path in sorted(cfg.kg_dir.rglob("*.md")):
        if any(part.startswith(".") for part in path.relative_to(cfg.kg_dir).parts):
            continue
        meta, body = fm.read(path)
        title = str(meta.get("title", path.stem))
        if path.name == "_root.md":
            add("_root", path, "root", title, "", body)
        elif path.name == "_summary.md":
            add(str(meta.get("id", path.parent.name)), path, "section", title, "", body)
        else:
            pid = str(meta.get("id", path.stem))
            if meta.get("summary"):
                add(pid, path, "page", title, "", f"{title}. {meta['summary']}")
            for n, (heading, text) in enumerate(split_sections(body, cfg.index.chunk_max_chars)):
                cid = f"{pid}#{slugify(heading) if heading else 'body'}-{n}"
                add(cid, path, "chunk", title, heading, f"{title} — {heading}\n{text}" if heading else text)

    if not entries:
        raise SystemExit(f"nothing to index under {cfg.kg_dir} — run ingest/summarize first")

    print(f"[index] embedding {len(entries)} nodes with backend={cfg.index.backend}")
    embs = embed_texts(cfg, texts)
    cfg.build_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cfg.build_dir / "index.npz", embeddings=embs)
    (cfg.build_dir / "index.json").write_text(
        json.dumps({"backend": cfg.index.backend, "model": cfg.index.model,
                    "entries": entries}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"[index] wrote {cfg.build_dir / 'index.npz'} and index.json")


# -------------------------------------------------------------------- search
def search(cfg: Cfg, query: str, top_k: int | None = None,
           kind: str | None = None, prefix: str | None = None, as_json: bool = False) -> None:
    meta_path, npz_path = cfg.build_dir / "index.json", cfg.build_dir / "index.npz"
    if not meta_path.exists() or not npz_path.exists():
        sys.exit("index not built — run: python -m kg index")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta["backend"] != cfg.index.backend or meta["model"] != cfg.index.model:
        sys.exit(
            f"index was built with backend={meta['backend']} model={meta['model']} "
            f"but config says backend={cfg.index.backend} model={cfg.index.model} — "
            "rebuild with: python -m kg index"
        )
    embs = np.load(npz_path)["embeddings"]
    entries = meta["entries"]

    q = embed_texts(cfg, [query])[0]
    scores = embs @ q
    mask = np.ones(len(entries), dtype=bool)
    if kind:
        mask &= np.array([e["kind"] == kind for e in entries])
    if prefix:
        mask &= np.array([e["path"].startswith(prefix) for e in entries])
    scores = np.where(mask, scores, -np.inf)

    k = top_k or cfg.search.top_k
    order = [int(i) for i in np.argsort(-scores)[:k] if np.isfinite(scores[i])]
    results = [{**entries[i], "score": round(float(scores[i]), 4)} for i in order]

    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
        return
    if not results:
        print("no results")
        return
    for r in results:
        loc = r["path"] + (f" § {r['heading']}" if r["heading"] else "")
        print(f"{r['score']:.3f}  [{r['kind']:7s}] {r['title']}\n        {loc}\n        {r['preview']}\n")
