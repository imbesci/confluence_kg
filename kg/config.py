"""Configuration: defaults overridable via config.yaml."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class LLMCfg:
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 1500
    max_workers: int = 8
    max_retries: int = 5


@dataclass
class OrganizeCfg:
    # auto: run LLM taxonomy only when the source dump is flat (no subfolders)
    enabled: str = "auto"  # auto | always | never
    max_top_categories: int = 12
    allow_subcategories: bool = True
    assign_batch_size: int = 80


@dataclass
class SummarizeCfg:
    page_max_words: int = 120
    section_max_words: int = 250
    root_max_words: int = 400
    max_input_chars: int = 24000  # per-page body cap fed to the LLM


@dataclass
class CrosslinkCfg:
    enabled: bool = True
    similarity_threshold: float = 0.60
    top_k_per_page: int = 8
    max_candidates: int = 300
    confirm_batch_size: int = 20


@dataclass
class IndexCfg:
    backend: str = "st"  # st (sentence-transformers) | dummy (offline smoke tests only)
    model: str = "sentence-transformers/all-MiniLM-L6-v2"
    chunk_max_chars: int = 3000


@dataclass
class SearchCfg:
    top_k: int = 8


@dataclass
class Cfg:
    source_dir: Path = Path("./confluence_dump")
    kg_dir: Path = Path("./knowledge")
    build_dir: Path = Path("./build")
    llm: LLMCfg = field(default_factory=LLMCfg)
    organize: OrganizeCfg = field(default_factory=OrganizeCfg)
    summarize: SummarizeCfg = field(default_factory=SummarizeCfg)
    crosslink: CrosslinkCfg = field(default_factory=CrosslinkCfg)
    index: IndexCfg = field(default_factory=IndexCfg)
    search: SearchCfg = field(default_factory=SearchCfg)
    dry_run: bool = False


def _apply(obj, data: dict):
    for key, value in (data or {}).items():
        if not hasattr(obj, key):
            print(f"[config] ignoring unknown key: {key}")
            continue
        current = getattr(obj, key)
        if isinstance(value, dict) and hasattr(current, "__dataclass_fields__"):
            _apply(current, value)
        elif isinstance(current, Path):
            setattr(obj, key, Path(value))
        else:
            setattr(obj, key, value)


def load(path: str | Path | None) -> Cfg:
    cfg = Cfg()
    if path:
        p = Path(path)
        if p.exists():
            _apply(cfg, yaml.safe_load(p.read_text(encoding="utf-8")) or {})
        elif str(path) != "config.yaml":
            raise FileNotFoundError(f"config file not found: {p}")
    return cfg
