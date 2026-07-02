# confluence-kg — a git-native hierarchical knowledge graph

Turns a folder of scraped Confluence text files into a **parent-child knowledge
tree stored as a git repo**, where an LLM does all the semantic work: page
summaries, bottom-up section rollups, a root overview, and confirmed
cross-links between branches. Agents consume it with `ls`/`cat`, a tree view,
and collapsed-tree vector search.

```
knowledge/                      <- commit this; it IS the graph
├── _root.md                    <- LLM overview of the whole space
├── edges.yaml                  <- confirmed cross-branch RELATED_TO edges
├── engineering/
│   ├── _summary.md             <- LLM rollup of everything below
│   ├── deploy-process.md       <- page: frontmatter (id, source_url, summary,
│   └── oncall-escalation.md       related, content_hash) + original body
└── security/
    └── ...
build/                          <- vector index; disposable, gitignored
```

Tree edges are the directory structure. Cross-links live in each page's
`related:` frontmatter and in `edges.yaml`. Every node traces back to its
Confluence page via `source_url`.

## Quickstart

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...          # https://docs.claude.com/en/api/overview

# point config.yaml at your scrape, or try the included sample first:
#   source_dir: ./example_dump
python -m kg build                            # ingest -> summarize -> crosslink -> index

git init && git add knowledge/ && git commit -m "initial knowledge graph"
```

Dry run first if you want to see the tree take shape without spending tokens
(placeholder text instead of LLM calls): `python -m kg build --dry-run`
(set `index.backend: dummy` too if you haven't installed sentence-transformers yet).

For ~500 pages expect roughly 600–800 LLM calls on the first build; with the
default `claude-sonnet-4-6` that's typically a few dollars, with
`claude-haiku-4-5-20251001` well under one. Embeddings are local and free
(sentence-transformers, CPU is fine).

## How it works

1. **ingest** — reads `.txt`/`.md` files, lifts `URL:` lines and titles into
   YAML frontmatter, mirrors your dump's folder structure into `knowledge/`.
   If the dump is *flat*, an LLM proposes a taxonomy from the page titles and
   files each page into it (`organize:` in config).
2. **summarize** — every page gets a frontmatter `summary`; then every
   directory gets `_summary.md`, deepest first, so parents summarize their
   children's summaries; then `_root.md`.
3. **crosslink** — embedding similarity nominates candidate page pairs across
   different branches; the LLM confirms which are genuinely related; confirmed
   edges are written to both pages' `related:` lists and `edges.yaml`.
4. **index** — embeds every node (root, section summaries, page summaries,
   heading-level chunks) into `build/` for collapsed-tree search.

Each step is also a standalone command: `python -m kg ingest|summarize|crosslink|index`.

## Incremental re-runs (this is the point of the design)

Everything is content-hashed, so `python -m kg build` is cheap after the first
run: unchanged pages are skipped, and only pages whose Confluence content
changed get re-summarized — along with just their ancestor `_summary.md`
chain up to the root (`inputs_hash` gates each rollup). A one-page edit costs
a handful of LLM calls, and the git diff shows exactly which knowledge changed.

Two rules worth knowing:
- **Manual moves are respected.** Pages are found by frontmatter `id`, never
  by path. Re-file a page by hand and rebuilds update it in place; emptied
  directories and their stale summaries are cleaned up automatically.
- **Changed content clears its stale summary** (regenerated next run) but
  keeps `related:` edges; re-run `crosslink` occasionally to refresh those.

`scripts/rebuild.sh` shows the refresh-as-PR workflow: re-scrape, rebuild,
commit to a branch, open a PR — a human reviews the diff of what the LLM
changed before it merges.

## How agents use it

- **Navigate:** start at `knowledge/_root.md`, descend via `_summary.md`
  files, `cat` pages. Any filesystem-capable agent (e.g. Claude Code pointed
  at the repo) needs zero custom tooling. `python -m kg tree` prints the whole
  map with one-line summaries.
- **Search:** `python -m kg search "how do rollbacks work" --json` returns
  scored nodes across *all* levels — broad questions hit section summaries,
  specific ones hit chunks. Filters: `--kind root|section|page|chunk`,
  `--prefix engineering/`.
- **Cite:** every result carries a repo path, and the page's `source_url`
  frontmatter links back to Confluence.

A simple agent loop that works well: search first, then read the full page
file for any promising hit, then follow its `related:` ids.

## Configuration

All knobs live in `config.yaml` with comments: paths, model, parallelism,
taxonomy behavior for flat dumps, summary word budgets, cross-link
threshold/caps, embedding model, chunk size. `organize.enabled: auto` only
invokes the taxonomy step when your dump has no folder structure to inherit.

## Swapping pieces

- **LLM provider:** everything goes through `kg/llm.py::LLM.complete()` —
  reimplement that one method for another provider.
- **Graph database later:** the repo stays the source of truth; a loader that
  walks it (directories → `CHILD_OF`, `related:` → cross-edges) can populate
  Neo4j or similar as a disposable query index whenever you want one.

## Troubleshooting

- `ANTHROPIC_API_KEY is not set` — export it, or use `--dry-run`.
- First `st` embedding run downloads the model from Hugging Face (~90 MB);
  offline machines can smoke-test with `index.backend: dummy` (test-only —
  its vectors are meaningless for real search).
- `index was built with backend=...` — config changed since the last build;
  re-run `python -m kg index`.
