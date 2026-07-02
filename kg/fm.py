"""Read/write markdown files with YAML frontmatter."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def parse(text: str) -> tuple[dict, str]:
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}, text
    if not isinstance(meta, dict):
        meta = {}
    return meta, text[m.end() :]


def render(meta: dict, body: str) -> str:
    fm = yaml.safe_dump(
        meta, sort_keys=False, allow_unicode=True, width=1000, default_flow_style=False
    ).strip()
    return f"---\n{fm}\n---\n\n{body.strip()}\n"


def read(path: Path) -> tuple[dict, str]:
    return parse(path.read_text(encoding="utf-8"))


def write(path: Path, meta: dict, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(meta, body), encoding="utf-8")
