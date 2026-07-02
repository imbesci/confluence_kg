#!/usr/bin/env python3
"""Scrape Confluence space(s) into the dump format `python -m kg ingest` expects.

Output (default ./confluence_dump):
    <ancestor-title>/<ancestor-title>/<page-title>-<pageid>.md
        URL: <link back to the page>
        # <Page Title>
        <body converted to markdown>
    * Directory nesting mirrors the Confluence page hierarchy (the space
      homepage is treated as the invisible root, not a folder).
    * File mtimes are set to each page's last-edited time, so `last_modified`
      in the knowledge tree reflects real Confluence edit dates.
    * The -<pageid> suffix keeps ids stable across page renames and makes
      duplicate sibling titles safe.

Supports both deployment flavors:
    Cloud (…atlassian.net)  -> REST v2, cursor pagination, email + API token
    Server / Data Center    -> REST v1 (/rest/api/content), PAT or basic auth

Auth via environment:
    CONFLUENCE_BASE_URL   e.g. https://acme.atlassian.net  or  https://confluence.acme.com
    CONFLUENCE_EMAIL + CONFLUENCE_API_TOKEN   (Cloud; token from id.atlassian.com)
    CONFLUENCE_PAT                            (Server/DC personal access token)

Usage:
    python scrape_confluence.py --space ENG --space PLAT
    python scrape_confluence.py --space ENG --out ./confluence_dump --clean

Notes:
    * Pages nested under new-style Folders/whiteboards attach to their nearest
      *page* ancestor (or the dump root) since only pages are fetched.
    * Deletions: with --clean the dump mirrors Confluence, but `kg ingest`
      never deletes — remove stale .md files from knowledge/ by hand (git
      makes that visible).
API refs: https://developer.atlassian.com/cloud/confluence/rest/v2/intro/
          https://developer.atlassian.com/server/confluence/rest/
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

import requests

try:
    import html2text
except ImportError:
    sys.exit("missing dependency: pip install html2text requests")


# --------------------------------------------------------------------- model
@dataclass
class RawPage:
    id: str
    title: str
    html: str
    web_url: str
    when: str | None = None            # ISO last-edited timestamp
    parent_id: str | None = None       # Cloud v2
    ancestors: list[tuple[str, str]] = field(default_factory=list)  # v1: [(id, title)] root-first


# -------------------------------------------------------------------- client
class Client:
    def __init__(self, base_url: str, timeout: int = 30, max_retries: int = 5):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json"

        email, token = os.environ.get("CONFLUENCE_EMAIL"), os.environ.get("CONFLUENCE_API_TOKEN")
        pat = os.environ.get("CONFLUENCE_PAT")
        if email and token:
            self.session.auth = (email, token)
        elif pat:
            self.session.headers["Authorization"] = f"Bearer {pat}"
        else:
            print("[warn] no credentials in env (CONFLUENCE_EMAIL+CONFLUENCE_API_TOKEN "
                  "or CONFLUENCE_PAT) — anonymous access only works on public spaces",
                  file=sys.stderr)

    def get(self, url: str, params: dict | None = None) -> dict:
        if not url.startswith("http"):
            url = self.base_url + url
        for attempt in range(self.max_retries):
            resp = self.session.get(url, params=params, timeout=self.timeout)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429 or resp.status_code >= 500:
                delay = float(resp.headers.get("Retry-After") or min(60, 2 ** attempt))
                print(f"[http] {resp.status_code} on {url} — retrying in {delay:.0f}s", file=sys.stderr)
                time.sleep(delay)
                continue
            if resp.status_code in (401, 403):
                sys.exit(f"auth failed ({resp.status_code}) for {url} — check credentials/permissions")
            resp.raise_for_status()
        raise RuntimeError(f"gave up on {url} after {self.max_retries} retries")


# ------------------------------------------------------------- Cloud (v2 API)
def fetch_cloud(client: Client, space_key: str, max_pages: int | None) -> tuple[list[RawPage], str | None]:
    wiki = client.base_url + "/wiki"
    spaces = client.get(wiki + "/api/v2/spaces", params={"keys": space_key}).get("results", [])
    if not spaces:
        sys.exit(f"space not found or not visible: {space_key}")
    space_id, homepage_id = str(spaces[0]["id"]), str(spaces[0].get("homepageId") or "")

    pages: list[RawPage] = []
    url, params = wiki + "/api/v2/pages", {
        "space-id": space_id, "status": "current",
        "body-format": "export_view", "limit": 100,
    }
    while url:
        data = client.get(url, params=params)
        params = None  # cursor URLs carry their own query string
        for p in data.get("results", []):
            body = (p.get("body") or {}).get("export_view") or {}
            webui = (p.get("_links") or {}).get("webui") or f"/pages/{p['id']}"
            pages.append(RawPage(
                id=str(p["id"]), title=p.get("title") or f"page-{p['id']}",
                html=body.get("value") or "",
                web_url=wiki + webui,
                when=(p.get("version") or {}).get("createdAt"),
                parent_id=str(p["parentId"]) if p.get("parentId") else None,
            ))
            if max_pages and len(pages) >= max_pages:
                return pages, homepage_id
        nxt = (data.get("_links") or {}).get("next")
        url = None if not nxt else (nxt if nxt.startswith("http") else urljoin(client.base_url + "/", nxt.lstrip("/")))
        print(f"  fetched {len(pages)} pages…", file=sys.stderr)
    return pages, homepage_id


# --------------------------------------------------- Server / Data Center (v1)
def fetch_server(client: Client, space_key: str, max_pages: int | None) -> tuple[list[RawPage], str | None]:
    space = client.get(f"/rest/api/space/{space_key}", params={"expand": "homepage"})
    homepage_id = str((space.get("homepage") or {}).get("id") or "")

    pages: list[RawPage] = []
    start, limit = 0, 50
    while True:
        data = client.get("/rest/api/content", params={
            "spaceKey": space_key, "type": "page", "status": "current",
            "expand": "body.export_view,ancestors,version", "start": start, "limit": limit,
        })
        results = data.get("results", [])
        link_base = (data.get("_links") or {}).get("base") or client.base_url
        for p in results:
            body = (p.get("body") or {}).get("export_view") or {}
            webui = (p.get("_links") or {}).get("webui") or f"/pages/viewpage.action?pageId={p['id']}"
            pages.append(RawPage(
                id=str(p["id"]), title=p.get("title") or f"page-{p['id']}",
                html=body.get("value") or "",
                web_url=link_base.rstrip("/") + webui,
                when=(p.get("version") or {}).get("when"),
                ancestors=[(str(a["id"]), a.get("title", "")) for a in p.get("ancestors") or []],
            ))
            if max_pages and len(pages) >= max_pages:
                return pages, homepage_id
        print(f"  fetched {len(pages)} pages…", file=sys.stderr)
        if len(results) < limit:
            return pages, homepage_id
        start += limit


# ----------------------------------------------------------- paths & writing
def slugify(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)[:max_len].rstrip("-") or "page"


def build_paths(pages: list[RawPage], homepage_id: str | None) -> dict[str, Path]:
    """page id -> relative output path; homepage acts as invisible root."""
    by_id = {p.id: p for p in pages}

    def chain(p: RawPage) -> list[str]:
        if p.ancestors:  # v1: server gave us the ordered chain directly
            return [slugify(t) for aid, t in p.ancestors if aid != homepage_id and t]
        names, cur, seen = [], p.parent_id, {p.id}
        while cur and cur not in seen and cur != homepage_id:
            seen.add(cur)
            parent = by_id.get(cur)
            if parent is None:  # parent is a Folder/whiteboard or unfetched — stop here
                break
            names.append(slugify(parent.title))
            cur = parent.parent_id
        return list(reversed(names))

    return {p.id: Path(*chain(p)) / f"{slugify(p.title)}-{p.id}.md" for p in pages}


def to_markdown(html: str) -> str:
    h = html2text.HTML2Text()
    h.body_width = 0
    h.ignore_images = True
    h.unicode_snob = True
    md = h.handle(html or "").replace("\u00a0", " ")
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def write_page(out_dir: Path, rel: Path, page: RawPage) -> None:
    path = out_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    content = f"URL: {page.web_url}\n\n# {page.title}\n\n{to_markdown(page.html)}\n"
    path.write_text(content, encoding="utf-8")
    if page.when:
        try:
            ts = dt.datetime.fromisoformat(page.when.replace("Z", "+00:00")).timestamp()
            os.utime(path, (ts, ts))
        except ValueError:
            pass


# ---------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default=os.environ.get("CONFLUENCE_BASE_URL"),
                    help="e.g. https://acme.atlassian.net (or set CONFLUENCE_BASE_URL)")
    ap.add_argument("--space", action="append", required=True,
                    help="space key; repeat for multiple spaces")
    ap.add_argument("--out", default="./confluence_dump", type=Path)
    ap.add_argument("--mode", choices=["auto", "cloud", "server"], default="auto")
    ap.add_argument("--clean", action="store_true",
                    help="wipe the output dir first so the dump mirrors Confluence")
    ap.add_argument("--max-pages", type=int, default=None, help="cap per space (for testing)")
    args = ap.parse_args()

    if not args.base_url:
        sys.exit("--base-url or CONFLUENCE_BASE_URL is required")
    mode = args.mode
    if mode == "auto":
        mode = "cloud" if "atlassian.net" in args.base_url else "server"
    client = Client(args.base_url)

    if args.clean and args.out.exists():
        shutil.rmtree(args.out)
    args.out.mkdir(parents=True, exist_ok=True)

    total = 0
    for key in args.space:
        print(f"[scrape] space {key} via {mode} API")
        fetch = fetch_cloud if mode == "cloud" else fetch_server
        pages, homepage_id = fetch(client, key, args.max_pages)
        by_id = {p.id: p for p in pages}
        root = args.out if len(args.space) == 1 else args.out / slugify(key)
        for page_id, rel in build_paths(pages, homepage_id).items():
            write_page(root, rel, by_id[page_id])
        print(f"[scrape] wrote {len(pages)} pages under {root}")
        total += len(pages)
    print(f"[scrape] done — {total} pages. Next: python -m kg build")


if __name__ == "__main__":
    main()
