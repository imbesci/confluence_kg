"""Non-tree edges: RELATED_TO links between pages in different branches.

Embeddings nominate candidate pairs (cheap), the LLM confirms them (accurate).
Same-directory siblings are excluded — they're already related via their parent.
Confirmed edges land in both pages' frontmatter (`related:`) and in edges.yaml.
"""
from __future__ import annotations

import numpy as np
import yaml

from . import fm
from .config import Cfg
from .indexer import embed_texts
from .summarize import iter_pages
from .util import truncate

_SYSTEM = (
    "You maintain cross-references in a company wiki. You respond with valid "
    "JSON only — no prose, no markdown fences."
)


def _candidates(cfg: Cfg, pages, embs: np.ndarray):
    sims = embs @ embs.T
    best: dict[tuple[int, int], float] = {}
    k = cfg.crosslink.top_k_per_page
    for i in range(len(pages)):
        for j in np.argsort(-sims[i])[1 : k + 1]:
            j = int(j)
            if sims[i, j] < cfg.crosslink.similarity_threshold:
                break
            if pages[i][0].parent == pages[j][0].parent:
                continue  # siblings: already linked through their parent node
            key = (min(i, j), max(i, j))
            best[key] = max(best.get(key, 0.0), float(sims[i, j]))
    ranked = sorted(best.items(), key=lambda kv: -kv[1])
    return ranked[: cfg.crosslink.max_candidates]


def _confirm(cfg: Cfg, llm, pages, ranked):
    edges = []
    bs = cfg.crosslink.confirm_batch_size
    for start in range(0, len(ranked), bs):
        batch = ranked[start : start + bs]
        if cfg.dry_run:
            edges += [(i, j, s, "dry-run link") for (i, j), s in batch]
            continue
        blocks = []
        for n, ((i, j), _s) in enumerate(batch, 1):
            (_, ma, _), (_, mb, _) = pages[i], pages[j]
            blocks.append(
                f"PAIR {n}:\nA: {ma['title']} — {truncate(str(ma.get('summary','')), 300)}\n"
                f"B: {mb['title']} — {truncate(str(mb.get('summary','')), 300)}"
            )
        prompt = (
            "For each pair of wiki pages, decide whether a reader of one would "
            "genuinely benefit from a cross-link to the other (same system, "
            "dependent process, complementary reference). Be selective — "
            "superficial topical overlap is not enough.\n\n"
            + "\n\n".join(blocks)
            + '\n\nReturn JSON only: [{"pair": 1, "related": true, "label": "<=8 words"}, ...] '
            "with one entry per pair."
        )
        result = llm.complete_json(prompt, system=_SYSTEM, max_tokens=2000)
        for entry in result if isinstance(result, list) else []:
            try:
                n = int(entry.get("pair"))
                if entry.get("related") and 1 <= n <= len(batch):
                    (i, j), s = batch[n - 1]
                    edges.append((i, j, s, str(entry.get("label", ""))))
            except (TypeError, ValueError):
                continue
        print(f"[crosslink] confirmed {min(start + bs, len(ranked))}/{len(ranked)} candidates")
    return edges


def run(cfg: Cfg, llm=None) -> None:
    pages = [(p, m, b) for p, m, b in iter_pages(cfg.kg_dir)]
    if len(pages) < 2:
        print("[crosslink] fewer than 2 pages — skipping")
        return
    texts = [f"{m.get('title','')}. {m.get('summary') or truncate(b, 800)}" for _, m, b in pages]
    embs = embed_texts(cfg, texts)
    ranked = _candidates(cfg, pages, embs)
    print(f"[crosslink] {len(ranked)} candidate pairs above threshold {cfg.crosslink.similarity_threshold}")
    edges = _confirm(cfg, llm, pages, ranked)

    for i, j, _s, _label in edges:
        for a, b in ((i, j), (j, i)):
            meta = pages[a][1]
            rel = set(map(str, meta.get("related") or []))
            rel.add(str(pages[b][1]["id"]))
            meta["related"] = sorted(rel)
    for path, meta, body in pages:
        fm.write(path, meta, body)

    edge_dump = [
        {"source": str(pages[i][1]["id"]), "target": str(pages[j][1]["id"]),
         "similarity": round(s, 3), "label": label}
        for i, j, s, label in sorted(edges, key=lambda e: -e[2])
    ]
    (cfg.kg_dir / "edges.yaml").write_text(
        yaml.safe_dump(edge_dump, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    print(f"[crosslink] wrote {len(edges)} edges to {cfg.kg_dir / 'edges.yaml'}")
