#!/usr/bin/env python3
"""
axe-spider - WCAG accessibility scanner using axe-core, Selenium, and Chromium.

Crawls a website and runs axe-core accessibility checks on each page,
producing HTML and JSON reports.

Usage:
    python3 axe-spider.py [OPTIONS] START_URL

Examples:
    python3 axe-spider.py https://example.com/
    python3 axe-spider.py --level wcag21aa --max-pages 100 https://example.com/
    python3 axe-spider.py --config mysite.yaml https://example.com/
    python3 axe-spider.py --include-path /docs --exclude-path /admin https://example.com/
"""

import argparse
import json
import os
import sys
import time
from collections import OrderedDict
from datetime import datetime
from urllib.parse import urlparse, urlunparse

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import (
    TimeoutException, WebDriverException,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AXE_JS_PATH = os.path.join(SCRIPT_DIR, 'axe.min.js')
DEFAULT_CONFIG_PATH = os.path.join(SCRIPT_DIR, 'axe-spider.yaml')

# axe-core version (read from the bundled file header on first use)
AXE_VERSION = None

# WCAG level presets: maps a level name to the axe-core tags to run
WCAG_LEVELS = {
    'wcag2a':    {'tags': ['wcag2a'],                                                        'label': 'WCAG 2.0 Level A'},
    'wcag2aa':   {'tags': ['wcag2a', 'wcag2aa'],                                             'label': 'WCAG 2.0 Level AA'},
    'wcag2aaa':  {'tags': ['wcag2a', 'wcag2aa', 'wcag2aaa'],                                 'label': 'WCAG 2.0 Level AAA'},
    'wcag21a':   {'tags': ['wcag2a', 'wcag21a'],                                             'label': 'WCAG 2.1 Level A'},
    'wcag21aa':  {'tags': ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'],                      'label': 'WCAG 2.1 Level AA'},
    'wcag21aaa': {'tags': ['wcag2a', 'wcag2aa', 'wcag2aaa', 'wcag21a', 'wcag21aa', 'wcag21aaa'],
                                                                                              'label': 'WCAG 2.1 Level AAA'},
    'wcag22a':   {'tags': ['wcag2a', 'wcag21a', 'wcag22a'],                                  'label': 'WCAG 2.2 Level A'},
    'wcag22aa':  {'tags': ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'],          'label': 'WCAG 2.2 Level AA'},
    'wcag22aaa': {'tags': ['wcag2a', 'wcag2aa', 'wcag2aaa', 'wcag21a', 'wcag21aa', 'wcag21aaa',
                           'wcag22aa', 'wcag22aaa'],                                          'label': 'WCAG 2.2 Level AAA'},
}
DEFAULT_LEVEL = 'wcag21aa'


def load_config(config_path=None):
    """Load site configuration from YAML file.

    Returns a dict with config values.  Missing keys get sensible defaults.
    """
    config = {}
    path = config_path or DEFAULT_CONFIG_PATH
    if os.path.exists(path):
        try:
            import yaml
            with open(path) as f:
                config = yaml.safe_load(f) or {}
        except ImportError:
            # PyYAML not installed — fall back to simple key: value parsing
            with open(path) as f:
                in_list = None
                for line in f:
                    line = line.rstrip()
                    stripped = line.lstrip()
                    if not stripped or stripped.startswith('#'):
                        continue
                    if stripped.startswith('- ') and in_list:
                        config.setdefault(in_list, []).append(stripped[2:].strip())
                        continue
                    if ':' in stripped:
                        key, _, val = stripped.partition(':')
                        key = key.strip()
                        val = val.strip()
                        if val == '' or val == '[]':
                            in_list = key
                            config[key] = []
                        else:
                            in_list = None
                            config[key] = val
                    else:
                        in_list = None
    return config


def get_axe_version():
    """Read axe-core version from the bundled JS file header."""
    global AXE_VERSION
    if AXE_VERSION is None:
        try:
            with open(AXE_JS_PATH, 'r') as f:
                header = f.read(200)
            # Header looks like: /*! axe v4.11.3
            import re
            m = re.search(r'axe v([\d.]+)', header)
            AXE_VERSION = m.group(1) if m else 'unknown'
        except Exception:
            AXE_VERSION = 'unknown'
    return AXE_VERSION


def load_axe_source():
    with open(AXE_JS_PATH, 'r') as f:
        return f.read()


def create_driver(config=None):
    config = config or {}
    opts = Options()
    opts.binary_location = config.get('chromium_path', '/usr/bin/chromium-browser')
    opts.add_argument('--headless')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--disable-gpu')
    opts.add_argument('--window-size=1280,1024')
    opts.add_argument('--ignore-certificate-errors')

    # Block file downloads — we only need rendered HTML
    prefs = {
        'download_restrictions': 3,
        'download.default_directory': '/dev/null',
        'download.prompt_for_download': False,
        'profile.default_content_setting_values.automatic_downloads': 2,
    }
    opts.add_experimental_option('prefs', prefs)

    chromedriver = config.get('chromedriver_path', '/usr/bin/chromedriver')
    driver = webdriver.Chrome(executable_path=chromedriver, options=opts)
    driver.set_page_load_timeout(30)
    driver.implicitly_wait(5)
    return driver


def normalize_url(url):
    """Normalize URL for deduplication: strip fragment, trailing slash on path."""
    parsed = urlparse(url)
    path = parsed.path.rstrip('/') or '/'
    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, ''))


def is_same_origin(url, base_url):
    return urlparse(url).netloc == urlparse(base_url).netloc


def should_scan(url, base_url, include_paths, exclude_paths):
    if not is_same_origin(url, base_url):
        return False
    parsed = urlparse(url)
    path = parsed.path

    # Skip non-HTML resources
    skip_exts = (
        '.pdf', '.jpg', '.jpeg', '.png', '.gif', '.svg', '.ico',
        '.css', '.js', '.zip', '.tar', '.gz', '.mp4', '.mp3',
        '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
        '.xml', '.json', '.rss', '.atom', '.woff', '.woff2',
        '.ttf', '.eot', '.bmp', '.webp', '.csv',
    )
    if any(path.lower().endswith(ext) for ext in skip_exts):
        return False

    if include_paths:
        if not any(path.startswith(p) for p in include_paths):
            return False

    if exclude_paths:
        if any(path.startswith(p) for p in exclude_paths):
            return False

    return True


def extract_links(driver, base_url):
    """Extract all same-origin links from the current page."""
    try:
        links = driver.execute_script(
            "return Array.from(document.querySelectorAll('a[href]'))"
            ".map(a => a.href)"
            ".filter(h => h.startsWith('http'))"
        )
        return [normalize_url(l) for l in links if l]
    except Exception:
        return []


def run_axe(driver, axe_source, tags=None):
    """Inject axe-core and run analysis on the current page."""
    driver.execute_script(axe_source)

    run_opts = {}
    if tags:
        run_opts['runOnly'] = {'type': 'tag', 'values': tags}

    script = """
    var callback = arguments[arguments.length - 1];
    var opts = arguments[0];
    axe.run(document, opts).then(function(results) {
        callback(results);
    }).catch(function(err) {
        callback({error: err.toString()});
    });
    """
    driver.set_script_timeout(60)
    results = driver.execute_async_script(script, run_opts)
    return results


def crawl_and_scan(start_url, max_pages=50, tags=None, level=None,
                   include_paths=None, exclude_paths=None, verbose=False,
                   config=None):
    """Crawl the site starting from start_url and scan each page with axe-core."""
    config = config or {}

    if tags is None:
        level = level or DEFAULT_LEVEL
        level_info = WCAG_LEVELS.get(level)
        if level_info is None:
            print("ERROR: Unknown level '{}'. Valid levels: {}".format(
                level, ', '.join(sorted(WCAG_LEVELS.keys()))))
            sys.exit(1)
        tags = level_info['tags']
        level_label = level_info['label']
    else:
        level_label = 'custom'

    page_wait = int(config.get('page_wait', 1))
    axe_source = load_axe_source()
    driver = create_driver(config)
    base_url = start_url

    visited = set()
    queue = [normalize_url(start_url)]
    all_results = OrderedDict()
    page_count = 0

    print("Starting axe-core {} accessibility scan...".format(get_axe_version()))
    print("  Start URL: {}".format(start_url))
    print("  Level: {} ({})".format(level_label, ', '.join(tags)))
    print("  Max pages: {}".format(max_pages))
    if page_wait > 1:
        print("  Page wait: {}s".format(page_wait))
    print()

    try:
        while queue and page_count < max_pages:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)

            if not should_scan(url, base_url, include_paths, exclude_paths):
                continue

            page_count += 1
            print("[{}/{}] Scanning: {}".format(page_count, max_pages, url))

            try:
                driver.get(url)
                time.sleep(page_wait)

                if not is_same_origin(driver.current_url, start_url):
                    print("  SKIP: redirected off-origin to {}".format(driver.current_url))
                    continue

                results = run_axe(driver, axe_source, tags)

                if 'error' in results:
                    print("  ERROR: {}".format(results['error']))
                    continue

                violations = results.get('violations', [])
                incomplete = results.get('incomplete', [])
                passes = results.get('passes', [])

                v_count = sum(len(v.get('nodes', [])) for v in violations)
                print("  Violations: {} ({} issues), Incomplete: {}, Passes: {}".format(
                    len(violations), v_count, len(incomplete), len(passes)
                ))

                all_results[url] = {
                    'url': url,
                    'timestamp': datetime.now().isoformat(),
                    'violations': violations,
                    'incomplete': incomplete,
                    'passes': passes,
                    'inapplicable': results.get('inapplicable', []),
                }

                new_links = extract_links(driver, base_url)
                for link in new_links:
                    if link not in visited and link not in queue:
                        queue.append(link)

            except TimeoutException:
                print("  TIMEOUT loading page, skipping")
            except WebDriverException as e:
                print("  WebDriver error: {}, skipping".format(str(e)[:100]))
            except Exception as e:
                print("  Error: {}, skipping".format(str(e)[:100]))

    finally:
        driver.quit()

    return all_results


def generate_html_report(all_results, output_path, start_url, level_label='WCAG 2.1 Level AA'):
    """Generate an HTML report from scan results."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    axe_ver = get_axe_version()

    total_pages = len(all_results)
    total_violations = 0
    total_violation_nodes = 0
    total_incomplete = 0
    impact_counts = {'critical': 0, 'serious': 0, 'moderate': 0, 'minor': 0}
    rule_summary = {}

    for url, data in all_results.items():
        for v in data.get('violations', []):
            total_violations += 1
            nodes = v.get('nodes', [])
            total_violation_nodes += len(nodes)
            impact = v.get('impact', 'unknown')
            if impact in impact_counts:
                impact_counts[impact] += len(nodes)
            rule_id = v.get('id', 'unknown')
            if rule_id not in rule_summary:
                rule_summary[rule_id] = {
                    'description': v.get('description', ''),
                    'help': v.get('help', ''),
                    'helpUrl': v.get('helpUrl', ''),
                    'impact': impact,
                    'tags': v.get('tags', []),
                    'count': 0,
                    'pages': [],
                }
            rule_summary[rule_id]['count'] += len(nodes)
            rule_summary[rule_id]['pages'].append(url)

        total_incomplete += len(data.get('incomplete', []))

    sorted_rules = sorted(rule_summary.items(), key=lambda x: x[1]['count'], reverse=True)

    impact_colors = {
        'critical': '#d32f2f',
        'serious': '#e65100',
        'moderate': '#f9a825',
        'minor': '#1565c0',
    }

    html_parts = []
    html_parts.append("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Axe Accessibility Scan Report</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         line-height: 1.6; color: #333; max-width: 1200px; margin: 0 auto; padding: 20px; }
  h1 { color: #1a237e; margin-bottom: 5px; }
  h2 { color: #283593; margin: 30px 0 15px; border-bottom: 2px solid #e8eaf6; padding-bottom: 5px; }
  h3 { color: #3949ab; margin: 20px 0 10px; }
  .meta { color: #666; margin-bottom: 20px; }
  .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                   gap: 15px; margin: 20px 0; }
  .summary-card { background: #f5f5f5; border-radius: 8px; padding: 20px; text-align: center; }
  .summary-card .number { font-size: 2em; font-weight: bold; }
  .summary-card .label { color: #666; font-size: 0.9em; }
  .impact-critical { border-left: 4px solid #d32f2f; }
  .impact-serious { border-left: 4px solid #e65100; }
  .impact-moderate { border-left: 4px solid #f9a825; }
  .impact-minor { border-left: 4px solid #1565c0; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; color: white;
           font-size: 0.8em; font-weight: bold; margin-right: 5px; }
  .badge-critical { background: #d32f2f; }
  .badge-serious { background: #e65100; }
  .badge-moderate { background: #f9a825; color: #333; }
  .badge-minor { background: #1565c0; }
  table { width: 100%; border-collapse: collapse; margin: 10px 0; }
  th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid #e0e0e0; }
  th { background: #e8eaf6; font-weight: 600; }
  tr:hover { background: #f5f5f5; }
  .rule-card { background: #fafafa; border: 1px solid #e0e0e0; border-radius: 8px;
               padding: 15px; margin: 10px 0; }
  .tag { display: inline-block; background: #e8eaf6; color: #3949ab; padding: 1px 6px;
         border-radius: 3px; font-size: 0.75em; margin: 2px; }
  details { margin: 5px 0; }
  summary { cursor: pointer; font-weight: 500; padding: 5px 0; }
  .node-detail { background: #fff; border: 1px solid #e0e0e0; border-radius: 4px;
                 padding: 10px; margin: 5px 0; }
  .html-snippet { background: #263238; color: #aed581; padding: 8px 12px; border-radius: 4px;
                  font-family: 'Fira Code', monospace; font-size: 0.85em; overflow-x: auto;
                  white-space: pre-wrap; word-break: break-all; }
  .target { color: #666; font-family: monospace; font-size: 0.85em; }
  a { color: #1565c0; }
  .page-section { margin: 25px 0; padding: 15px; border: 1px solid #e0e0e0; border-radius: 8px; }
  .page-url { font-size: 0.9em; color: #1565c0; word-break: break-all; }
  .wcag-ref { font-size: 0.8em; color: #666; }
</style>
</head>
<body>
""")

    html_parts.append('<h1>Axe Accessibility Scan Report</h1>')
    html_parts.append('<p class="meta">Scanned: {} | {} | Generated: {} | axe-core {}</p>'.format(
        _esc(start_url), _esc(level_label), now, axe_ver))

    html_parts.append('<div class="summary-grid">')
    html_parts.append(
        '<div class="summary-card"><div class="number">{}</div>'
        '<div class="label">Pages Scanned</div></div>'.format(total_pages))
    html_parts.append(
        '<div class="summary-card"><div class="number" style="color:#d32f2f">{}</div>'
        '<div class="label">Total Issues</div></div>'.format(total_violation_nodes))
    html_parts.append(
        '<div class="summary-card"><div class="number">{}</div>'
        '<div class="label">Unique Rules</div></div>'.format(len(rule_summary)))
    html_parts.append(
        '<div class="summary-card"><div class="number">{}</div>'
        '<div class="label">Needs Review</div></div>'.format(total_incomplete))
    html_parts.append('</div>')

    html_parts.append('<h2>Impact Breakdown</h2>')
    html_parts.append('<div class="summary-grid">')
    for impact in ('critical', 'serious', 'moderate', 'minor'):
        cnt = impact_counts[impact]
        html_parts.append(
            '<div class="summary-card impact-{imp}">'
            '<div class="number" style="color:{color}">{cnt}</div>'
            '<div class="label">{imp_cap}</div></div>'.format(
                imp=impact, color=impact_colors[impact],
                cnt=cnt, imp_cap=impact.capitalize()))
    html_parts.append('</div>')

    if sorted_rules:
        html_parts.append('<h2>Violation Summary by Rule</h2>')
        html_parts.append('<table><tr><th>Rule</th><th>Impact</th><th>Issues</th>'
                          '<th>Pages</th><th>Description</th></tr>')
        for rule_id, info in sorted_rules:
            impact = info['impact']
            html_parts.append(
                '<tr><td><a href="{url}">{id}</a></td>'
                '<td><span class="badge badge-{imp}">{imp_cap}</span></td>'
                '<td>{count}</td><td>{pages}</td><td>{desc}</td></tr>'.format(
                    url=_esc(info['helpUrl']), id=_esc(rule_id),
                    imp=impact, imp_cap=impact.capitalize(),
                    count=info['count'], pages=len(set(info['pages'])),
                    desc=_esc(info['help'])))
        html_parts.append('</table>')

    html_parts.append('<h2>Per-Page Details</h2>')
    for url, data in all_results.items():
        violations = data.get('violations', [])
        if not violations:
            continue

        v_count = sum(len(v.get('nodes', [])) for v in violations)
        html_parts.append('<div class="page-section">')
        html_parts.append('<h3><a href="{}" class="page-url">{}</a></h3>'.format(
            _esc(url), _esc(url)))
        html_parts.append('<p>{} violation(s), {} issue(s)</p>'.format(
            len(violations), v_count))

        for v in violations:
            impact = v.get('impact', 'unknown')
            html_parts.append('<div class="rule-card impact-{}">'.format(impact))
            html_parts.append(
                '<strong><span class="badge badge-{}">{}</span> '
                '<a href="{}">{}</a></strong>'.format(
                    impact, impact.capitalize(),
                    _esc(v.get('helpUrl', '')), _esc(v.get('id', ''))))
            html_parts.append('<p>{}</p>'.format(_esc(v.get('help', ''))))

            tags = v.get('tags', [])
            wcag_tags = [t for t in tags if t.startswith('wcag')]
            if wcag_tags:
                html_parts.append('<p class="wcag-ref">WCAG: {}</p>'.format(
                    ' '.join('<span class="tag">{}</span>'.format(_esc(t)) for t in wcag_tags)))

            nodes = v.get('nodes', [])
            html_parts.append('<details><summary>{} element(s) affected</summary>'.format(
                len(nodes)))
            for node in nodes[:20]:
                html_parts.append('<div class="node-detail">')
                target = node.get('target', [])
                if target:
                    html_parts.append('<p class="target">Selector: {}</p>'.format(
                        _esc(', '.join(str(t) for t in target))))
                html_snippet = node.get('html', '')
                if html_snippet:
                    if len(html_snippet) > 500:
                        html_snippet = html_snippet[:500] + '...'
                    html_parts.append(
                        '<div class="html-snippet">{}</div>'.format(_esc(html_snippet)))
                any_of = node.get('any', [])
                all_of = node.get('all', [])
                none_of = node.get('none', [])
                messages = []
                for check in any_of + all_of + none_of:
                    msg = check.get('message', '')
                    if msg:
                        messages.append(msg)
                if messages:
                    html_parts.append('<ul>')
                    for msg in messages[:5]:
                        html_parts.append('<li>{}</li>'.format(_esc(msg)))
                    html_parts.append('</ul>')
                html_parts.append('</div>')

            if len(nodes) > 20:
                html_parts.append('<p><em>... and {} more elements</em></p>'.format(
                    len(nodes) - 20))
            html_parts.append('</details>')
            html_parts.append('</div>')

        html_parts.append('</div>')

    clean_pages = [url for url, data in all_results.items()
                   if not data.get('violations')]
    if clean_pages:
        html_parts.append('<h2>Pages with No Violations ({})'.format(len(clean_pages)))
        html_parts.append('</h2><ul>')
        for url in clean_pages:
            html_parts.append('<li><a href="{}">{}</a></li>'.format(_esc(url), _esc(url)))
        html_parts.append('</ul>')

    html_parts.append('</body></html>')

    with open(output_path, 'w') as f:
        f.write('\n'.join(html_parts))


def _esc(text):
    """Escape HTML special characters."""
    return (str(text)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))


def main():
    parser = argparse.ArgumentParser(
        description='Scan a website for WCAG accessibility violations using axe-core.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('url', nargs='?', default=None,
                        help='Starting URL to scan')
    parser.add_argument('--config', default=None,
                        help='Path to YAML config file (default: axe-spider.yaml alongside script)')
    parser.add_argument('--level', default=None,
                        choices=sorted(WCAG_LEVELS.keys()),
                        help='WCAG conformance level (default: wcag21aa)')
    parser.add_argument('--max-pages', type=int, default=None,
                        help='Maximum pages to scan (default: 50)')
    parser.add_argument('--tags', default=None,
                        help='Comma-separated axe-core tags (overrides --level)')
    parser.add_argument('--include-path', action='append', default=None,
                        help='Only scan URLs starting with this prefix (repeatable)')
    parser.add_argument('--exclude-path', action='append', default=None,
                        help='Skip URLs starting with this prefix (repeatable, adds to config)')
    parser.add_argument('--no-default-excludes', action='store_true',
                        help='Ignore exclude_paths from config file')
    parser.add_argument('--output', default=None,
                        help='Output file basename (default: axe-spider-YYYY-MM-DD-HHMMSS)')
    parser.add_argument('--output-dir', default=None,
                        help='Output directory (default: from config or current directory)')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Verbose output')

    args = parser.parse_args()

    # Load config
    config = load_config(args.config)

    # Resolve URL: command line > config > error
    url = args.url or config.get('url')
    if not url:
        parser.error('No URL specified. Provide a URL argument or set "url" in config.')
    if not url.startswith('http'):
        url = 'https://' + url

    # Resolve tags/level
    tags = None
    if args.tags:
        tags = [t.strip() for t in args.tags.split(',')]
    level = args.level or config.get('level', DEFAULT_LEVEL)
    level_info = WCAG_LEVELS.get(level, {})
    level_label = level_info.get('label', 'Custom') if not args.tags else 'Custom tags'

    # Resolve max pages
    max_pages = args.max_pages or int(config.get('max_pages', 50))

    # Resolve exclude paths: config defaults + CLI additions
    exclude_paths = []
    if not args.no_default_excludes:
        exclude_paths.extend(config.get('exclude_paths', []))
    if args.exclude_path:
        for p in args.exclude_path:
            if p not in exclude_paths:
                exclude_paths.append(p)

    # Resolve include paths: CLI only (config can set defaults)
    include_paths = args.include_path or config.get('include_paths')

    # Resolve output
    basename = args.output or 'axe-spider-{}'.format(datetime.now().strftime('%Y-%m-%d-%H%M%S'))
    output_dir = args.output_dir or config.get('output_dir', os.getcwd())
    os.makedirs(output_dir, exist_ok=True)

    html_path = os.path.join(output_dir, basename + '.html')
    json_path = os.path.join(output_dir, basename + '.json')

    results = crawl_and_scan(
        url,
        max_pages=max_pages,
        tags=tags,
        level=args.level,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        verbose=args.verbose,
        config=config,
    )

    # Save JSON
    with open(json_path, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print("\nJSON report: {}".format(json_path))

    # Save HTML
    generate_html_report(results, html_path, url, level_label=level_label)
    print("HTML report: {}".format(html_path))

    # Summary
    total_issues = sum(
        sum(len(v.get('nodes', [])) for v in data.get('violations', []))
        for data in results.values()
    )
    print("\nScan complete: {} pages scanned, {} total issues found.".format(
        len(results), total_issues))


if __name__ == '__main__':
    main()
