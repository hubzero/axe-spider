# axe-spider

WCAG accessibility scanner that crawls a website using Selenium/Chromium
and runs [axe-core](https://github.com/dequelabs/axe-core) checks on
each page.  Produces HTML, JSON, and LLM-optimized markdown reports.

## Quick start

```bash
# Scan with defaults from config
axe-spider.py

# Scan a specific URL
axe-spider.py https://example.com/

# Scan 500 pages with LLM-friendly output
axe-spider.py --max-pages 500 --llm https://example.com/

# Quick single-page check after a fix
axe-spider.py --page -q --summary-json https://example.com/fixed-page
```

## Setup

Requires Python 3.6+, Selenium, and Chromium/Chrome + ChromeDriver.

```bash
pip install selenium
```

Copy `axe-spider.yaml.example` to `axe-spider.yaml` and edit for your
site.  The config file is gitignored so each deployment keeps its own
settings without merge conflicts.

## Configuration

All settings in `axe-spider.yaml` can be overridden on the command line.

| Setting | CLI flag | Default | Description |
|---|---|---|---|
| `url` | positional arg | — | Starting URL to crawl |
| `level` | `--level` | `wcag21aa` | WCAG conformance level |
| `max_pages` | `--max-pages` | 50 | Maximum pages to scan |
| `page_wait` | — | 1 | Seconds to wait after page load for JS to settle |
| `save_every` | `--save-every` | 25 | Flush reports every N pages |
| `output_dir` | `--output-dir` | cwd | Report output directory |
| `exclude_paths` | `--exclude-path` | — | URL path prefixes to skip |
| `exclude_regex` | — | — | Regex patterns to skip (e.g. auth-protected routes) |
| `exclude_query` | — | — | Query substrings to skip (e.g. `action=overview`) |
| `include_paths` | `--include-path` | — | Only scan URLs under these prefixes |
| `strip_query_params` | — | — | Query parameters to strip for URL deduplication |
| `niceness` | — | 10 | OS nice level (0–19, higher = lower CPU priority) |
| `oom_score_adj` | — | 1000 | Linux OOM killer score (1000 = killed first) |
| `allowlist` | `--allowlist` | — | YAML file of known-acceptable incompletes |
| `ignore_robots` | `--ignore-robots` | false | Ignore robots.txt restrictions |
| `ignore_certificate_errors` | — | false | Accept self-signed TLS certs |
| `driver` | `--driver` | `selenium` | Browser driver: `selenium` or `playwright` |
| `workers` | `--workers` | 1 | Parallel browser instances |
| `restart_every` | — | 500 | Restart browser every N pages (prevents memory leaks) |
| `chromium_path` | — | `/usr/bin/chromium-browser` | Path to Chrome/Chromium |
| `chromedriver_path` | — | `/usr/bin/chromedriver` | Path to ChromeDriver (selenium only) |

## Output files

Each scan produces:

| File | Description |
|---|---|
| `*.json` | Full axe-core results for every page (violations, incomplete, passes) |
| `*.html` | Human-readable report with summary cards, WCAG criteria table, per-page details |
| `*.jsonl` | Streaming results (one JSON object per line) — used for `--diff` and `--rescan` |
| `*.state.json` | Crawl state (queue + visited URLs) — used for `--resume` to continue later |
| `*.md` | LLM-optimized markdown summary (only with `--llm`) — ~300 tokens vs ~300K for JSON |

## Key flags

### Scanning modes
| Flag | Description |
|---|---|
| `--crawl` | Crawl and discover pages from the starting URL (default) |
| `--page` | Scan only the given URL, no crawling — fast single-page verify |
| `--urls FILE` | Scan a specific list of URLs from a file (one per line) |
| `--rescan PREV.jsonl` | Re-scan only pages that had issues in a previous scan |
| `--resume STATE.json` | Resume a previous crawl from its saved state file |

### Performance
| Flag | Description |
|---|---|
| `--driver TYPE` | `selenium` (default) or `playwright` (~2x faster, manages own Chromium) |
| `--workers N` | Parallel browser instances (default: 1). Playwright uses async pages in one process; Selenium uses separate processes (~300MB each) |

### Filtering
| Flag | Description |
|---|---|
| `--rule RULE` | Only run specific axe rules (repeatable, e.g. `--rule color-contrast`) |
| `--include-path PREFIX` | Only scan URLs starting with this prefix (repeatable) |
| `--exclude-path PREFIX` | Skip URLs starting with this prefix (repeatable) |
| `--allowlist FILE` | Suppress known-acceptable incompletes from reports |
| `--ignore-robots` | Ignore robots.txt (by default, disallowed paths are skipped) |
| `--no-default-excludes` | Ignore exclude_paths from config file |

### Output control
| Flag | Description |
|---|---|
| `--llm` | Generate compact markdown report optimized for LLM context windows |
| `--summary-json` | Print one-line JSON summary to stdout (machine-parseable) |
| `--diff PREV.jsonl` | Compare against a previous scan — show fixed/new/remaining |
| `-v` / `--verbose` | Show detailed rule/node counts for pages with issues |
| `-q` / `--quiet` | Suppress per-page progress, show only final summary |

### Maintenance
| Flag | Description |
|---|---|
| `--cleanup` | Kill orphaned chromium/chromedriver processes from previous runs |
| `--help` | Show all options |
| `--help-audit` | Print a WCAG audit workflow guide (useful for LLM assistants) |

## Output levels

The default output shows one compact line per page with timing:

```
[1/500] https://example.com/ — 3 violations, 14 incomplete (4.2s)
[2/500] https://example.com/about — clean (3.8s)
```

With `--workers`, worker IDs are shown:

```
[1/500] W1 https://example.com/ — 14 incomplete (4.3s)
[2/500] W2 https://example.com/members — clean (4.4s)
[3/500] W3 https://example.com/about — clean (4.1s)
```

With `-v`, pages with issues also get a detailed breakdown.
With `-q`, only the final summary is shown.

The final summary shows throughput:

```
Scan complete: 500 pages in 312.4s (0.6s/page)
```

## Workflow: scan → fix → verify

```bash
# 1. Full baseline scan
axe-spider.py --max-pages 500 --llm https://example.com/

# 2. Read the .md report, fix issues in source code

# 3. Verify the fix on the specific page
axe-spider.py --page -q --summary-json https://example.com/fixed-page
# Exit code 0 = clean, 1 = still has violations

# 4. Re-scan only pages that failed before, compare against baseline
axe-spider.py --rescan baseline.jsonl --diff baseline.jsonl --llm

# 5. Large site? Scan in chunks and resume
axe-spider.py --max-pages 10000 https://example.com/
axe-spider.py --max-pages 10000 --resume reports/scan.state.json https://example.com/

# 6. Suppress known axe-core limitations in an allowlist
echo '- rule: color-contrast
  url: /homepage
  reason: axe-core flex layout measurement limitation' >> allowlist.yaml
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | No violations found |
| 1 | Violations found |
| 2 | Setup error (missing selenium, chromium, chromedriver, or axe.min.js) |

## License

MIT — see [LICENSE](LICENSE).  Bundled axe-core is MPL-2.0.
