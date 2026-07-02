"""Thin Anthropic Messages API wrapper with retry/backoff.

Swapping providers means reimplementing LLM.complete() — nothing else in the
pipeline touches the API. See https://docs.claude.com/en/api/overview
"""
from __future__ import annotations

import os
import random
import sys
import time

from .config import LLMCfg
from .util import json_from_text


class LLM:
    def __init__(self, cfg: LLMCfg):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.exit(
                "ANTHROPIC_API_KEY is not set. Export it, or use --dry-run to "
                "exercise the pipeline without API calls."
            )
        import anthropic  # imported lazily so --dry-run works without the package

        self._anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.cfg = cfg

    def complete(self, prompt: str, system: str | None = None, max_tokens: int | None = None) -> str:
        a = self._anthropic
        kwargs = dict(
            model=self.cfg.model,
            max_tokens=max_tokens or self.cfg.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        if system:
            kwargs["system"] = system

        last_err = None
        for attempt in range(self.cfg.max_retries):
            try:
                msg = self.client.messages.create(**kwargs)
                return "".join(b.text for b in msg.content if b.type == "text").strip()
            except (
                a.RateLimitError,
                a.APIConnectionError,
                a.APITimeoutError,
                a.InternalServerError,
            ) as e:
                last_err = e
            except a.APIStatusError as e:
                if e.status_code in (408, 409, 429, 529) or e.status_code >= 500:
                    last_err = e
                else:
                    raise
            delay = min(60, 2**attempt) + random.uniform(0, 1)
            print(f"  [llm] transient error ({type(last_err).__name__}), retrying in {delay:.1f}s", file=sys.stderr)
            time.sleep(delay)
        raise RuntimeError(f"LLM call failed after {self.cfg.max_retries} retries: {last_err}")

    def complete_json(self, prompt: str, system: str | None = None, max_tokens: int | None = None):
        """Call the model expecting JSON; one corrective retry on parse failure."""
        text = self.complete(prompt, system=system, max_tokens=max_tokens)
        try:
            return json_from_text(text)
        except ValueError:
            retry_prompt = (
                prompt
                + "\n\nYour previous reply was not valid JSON. Respond with ONLY the "
                "JSON object/array — no prose, no markdown fences."
            )
            return json_from_text(self.complete(retry_prompt, system=system, max_tokens=max_tokens))
