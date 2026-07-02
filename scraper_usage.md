export CONFLUENCE_BASE_URL=https://acme.atlassian.net
export CONFLUENCE_EMAIL=you@acme.com CONFLUENCE_API_TOKEN=...   # Cloud
# or CONFLUENCE_PAT=... for Server/Data Center
python scrape_confluence.py --space ENG --out ./confluence_dump --clean
python -m kg build