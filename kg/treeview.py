"""Print the knowledge tree with one-line summaries — an agent's map."""
from __future__ import annotations

from pathlib import Path

from . import fm
from .config import Cfg
from .util import first_sentence, pretty


def _line(indent: int, text: str) -> None:
    print("  " * indent + text)


def _walk(d: Path, kg_dir: Path, indent: int, show_pages: bool) -> None:
    for sub in sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith(".")):
        s = sub / "_summary.md"
        if s.exists():
            meta, body = fm.read(s)
            _line(indent, f"{sub.relative_to(kg_dir).as_posix()}/ — {first_sentence(body)}")
        else:
            _line(indent, f"{sub.relative_to(kg_dir).as_posix()}/")
        _walk(sub, kg_dir, indent + 1, show_pages)
    if show_pages:
        for f in sorted(d.glob("*.md")):
            if f.name in ("_summary.md", "_root.md"):
                continue
            meta, _ = fm.read(f)
            _line(indent, f"• {meta.get('title', pretty(f.stem))}  ({f.relative_to(kg_dir).as_posix()})")


def show(cfg: Cfg, show_pages: bool = True) -> None:
    root = cfg.kg_dir / "_root.md"
    if root.exists():
        _, body = fm.read(root)
        print(f"ROOT — {first_sentence(body, 240)}\n")
    _walk(cfg.kg_dir, cfg.kg_dir, 0, show_pages)
