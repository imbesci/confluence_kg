"""CLI: python -m kg <command>."""
from __future__ import annotations

import argparse

from . import config, crosslink, indexer, ingest, summarize, treeview


def _llm_for(cfg):
    if cfg.dry_run:
        return None
    from .llm import LLM
    return LLM(cfg.llm)


def main() -> None:
    p = argparse.ArgumentParser(
        prog="kg",
        description="Build a hierarchical, git-native knowledge graph from a Confluence dump.",
    )
    p.add_argument("--config", default="config.yaml", help="path to config.yaml")
    p.add_argument("--dry-run", action="store_true",
                   help="run the full pipeline with placeholder text instead of LLM calls")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ingest", help="scraped files -> markdown tree (+ taxonomy for flat dumps)")
    sub.add_parser("summarize", help="page summaries, then section rollups, then _root.md")
    sub.add_parser("crosslink", help="propose + confirm RELATED_TO edges across branches")
    sub.add_parser("index", help="build the vector index (build artifact, gitignored)")
    sub.add_parser("build", help="ingest -> summarize -> crosslink -> index")

    s = sub.add_parser("search", help="collapsed-tree vector search")
    s.add_argument("query")
    s.add_argument("--top-k", type=int, default=None)
    s.add_argument("--kind", choices=["root", "section", "page", "chunk"], default=None)
    s.add_argument("--prefix", default=None, help="restrict to paths under this prefix")
    s.add_argument("--json", action="store_true", help="machine-readable output for agents")

    t = sub.add_parser("tree", help="print the tree with one-line summaries")
    t.add_argument("--no-pages", action="store_true", help="sections only")

    args = p.parse_args()
    cfg = config.load(args.config)
    cfg.dry_run = args.dry_run

    if args.cmd == "ingest":
        ingest.run(cfg, _llm_for(cfg))
    elif args.cmd == "summarize":
        summarize.run(cfg, _llm_for(cfg))
    elif args.cmd == "crosslink":
        if cfg.crosslink.enabled:
            crosslink.run(cfg, _llm_for(cfg))
    elif args.cmd == "index":
        indexer.build(cfg)
    elif args.cmd == "build":
        llm = _llm_for(cfg)
        ingest.run(cfg, llm)
        summarize.run(cfg, llm)
        if cfg.crosslink.enabled:
            crosslink.run(cfg, llm)
        indexer.build(cfg)
        print("\n[build] complete — commit the knowledge/ directory to git")
    elif args.cmd == "search":
        indexer.search(cfg, args.query, top_k=args.top_k, kind=args.kind,
                       prefix=args.prefix, as_json=args.json)
    elif args.cmd == "tree":
        treeview.show(cfg, show_pages=not args.no_pages)


if __name__ == "__main__":
    main()
