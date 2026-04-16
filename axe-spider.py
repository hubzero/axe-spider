#!/usr/bin/env python3
"""
axe-spider - WCAG accessibility scanner using axe-core, Selenium, and Chromium.

Crawls a website and runs axe-core accessibility checks on each page,
producing HTML and JSON reports.

Usage:
    axe-spider.py [OPTIONS] START_URL

Examples:
    # Full crawl scan
    axe-spider.py https://example.com/
    axe-spider.py --max-pages 500 --llm https://example.com/

    # Quick single-page check after a fix
    axe-spider.py --page -q --summary-json https://example.com/fixed-page

    # Re-scan only pages that failed previously
    axe-spider.py --rescan previous.jsonl --diff previous.jsonl --llm

    # Check just contrast issues
    axe-spider.py --page --rule color-contrast https://example.com/page

    # Scan a specific list of URLs
    axe-spider.py --urls pages.txt --llm

Exit codes: 0 = no violations, 1 = violations found.
"""

import argparse
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.request
import urllib.error
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse, urlunparse
from urllib.robotparser import RobotFileParser

# Selenium is the only external dependency.  We catch ImportError here
# rather than letting Python's traceback confuse users who haven't installed it.
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.common.exceptions import (
        TimeoutException, WebDriverException,
    )
except ImportError:
    print("ERROR: selenium is not installed.", file=sys.stderr)
    print("  Install it with:  pip install selenium", file=sys.stderr)
    print("  (Python 3.7+ required for Selenium 4)", file=sys.stderr)
    sys.exit(2)

# All supporting files (axe.min.js, config) live alongside this script.
# This lets the tool work as a self-contained directory you can clone
# and run from anywhere without installation.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AXE_JS_PATH = os.path.join(SCRIPT_DIR, 'axe.min.js')
DEFAULT_CONFIG_PATH = os.path.join(SCRIPT_DIR, 'axe-spider.yaml')

# File extensions that are never HTML pages.  Using a frozenset gives O(1)
# lookup instead of scanning a list on every URL the crawler discovers.
SKIP_EXTENSIONS = frozenset((
    '.pdf', '.jpg', '.jpeg', '.png', '.gif', '.svg', '.ico',
    '.css', '.js', '.zip', '.tar', '.gz', '.mp4', '.mp3',
    '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.xml', '.json', '.rss', '.atom', '.woff', '.woff2',
    '.ttf', '.eot', '.bmp', '.webp', '.csv',
))

# axe-core version (read from the bundled file header on first use)
AXE_VERSION = None

# WCAG level presets.  Each level includes the tags for all lower levels
# (e.g. AA includes A rules too).  These map to the tag values that
# axe-core's runOnly option accepts.
WCAG_LEVELS = {
    'wcag2a': {
        'tags': ['wcag2a'],
        'label': 'WCAG 2.0 Level A',
    },
    'wcag2aa': {
        'tags': ['wcag2a', 'wcag2aa'],
        'label': 'WCAG 2.0 Level AA',
    },
    'wcag2aaa': {
        'tags': ['wcag2a', 'wcag2aa', 'wcag2aaa'],
        'label': 'WCAG 2.0 Level AAA',
    },
    'wcag21a': {
        'tags': ['wcag2a', 'wcag21a'],
        'label': 'WCAG 2.1 Level A',
    },
    'wcag21aa': {
        'tags': ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'],
        'label': 'WCAG 2.1 Level AA',
    },
    'wcag21aaa': {
        'tags': ['wcag2a', 'wcag2aa', 'wcag2aaa',
                 'wcag21a', 'wcag21aa', 'wcag21aaa'],
        'label': 'WCAG 2.1 Level AAA',
    },
    'wcag22a': {
        'tags': ['wcag2a', 'wcag21a', 'wcag22a'],
        'label': 'WCAG 2.2 Level A',
    },
    'wcag22aa': {
        'tags': ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'],
        'label': 'WCAG 2.2 Level AA',
    },
    'wcag22aaa': {
        'tags': ['wcag2a', 'wcag2aa', 'wcag2aaa',
                 'wcag21a', 'wcag21aa', 'wcag21aaa',
                 'wcag22aa', 'wcag22aaa'],
        'label': 'WCAG 2.2 Level AAA',
    },
}
DEFAULT_LEVEL = 'wcag21aa'


def _safe_int(val, default=0):
    """Convert to int, returning default on failure."""
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _count_nodes(result_list):
    """Count total nodes across a list of axe-core rule results."""
    total = 0
    for rule_result in result_list:
        total += len(rule_result.get('nodes', []))
    return total


# WCAG success criteria names (subset — covers all criteria axe-core tests)
WCAG_SC_NAMES = {
    '1.1.1': 'Non-text Content',
    '1.2.1': 'Audio-only and Video-only',
    '1.2.2': 'Captions (Prerecorded)',
    '1.2.3': 'Audio Description or Media Alternative',
    '1.2.5': 'Audio Description (Prerecorded)',
    '1.3.1': 'Info and Relationships',
    '1.3.2': 'Meaningful Sequence',
    '1.3.3': 'Sensory Characteristics',
    '1.3.4': 'Orientation',
    '1.3.5': 'Identify Input Purpose',
    '1.4.1': 'Use of Color',
    '1.4.2': 'Audio Control',
    '1.4.3': 'Contrast (Minimum)',
    '1.4.4': 'Resize Text',
    '1.4.5': 'Images of Text',
    '1.4.6': 'Contrast (Enhanced)',
    '1.4.10': 'Reflow',
    '1.4.11': 'Non-text Contrast',
    '1.4.12': 'Text Spacing',
    '1.4.13': 'Content on Hover or Focus',
    '2.1.1': 'Keyboard',
    '2.1.2': 'No Keyboard Trap',
    '2.1.4': 'Character Key Shortcuts',
    '2.2.1': 'Timing Adjustable',
    '2.2.2': 'Pause, Stop, Hide',
    '2.3.1': 'Three Flashes or Below Threshold',
    '2.4.1': 'Bypass Blocks',
    '2.4.2': 'Page Titled',
    '2.4.3': 'Focus Order',
    '2.4.4': 'Link Purpose (In Context)',
    '2.4.5': 'Multiple Ways',
    '2.4.6': 'Headings and Labels',
    '2.4.7': 'Focus Visible',
    '2.5.1': 'Pointer Gestures',
    '2.5.2': 'Pointer Cancellation',
    '2.5.3': 'Label in Name',
    '2.5.4': 'Motion Actuation',
    '3.1.1': 'Language of Page',
    '3.1.2': 'Language of Parts',
    '3.2.1': 'On Focus',
    '3.2.2': 'On Input',
    '3.3.1': 'Error Identification',
    '3.3.2': 'Labels or Instructions',
    '3.3.3': 'Error Suggestion',
    '3.3.4': 'Error Prevention (Legal, Financial, Data)',
    '4.1.1': 'Parsing',
    '4.1.2': 'Name, Role, Value',
    '4.1.3': 'Status Messages',
}


def _parse_wcag_sc(tags):
    """Extract WCAG success criteria numbers from axe-core tags.

    Tags like 'wcag111' -> '1.1.1', 'wcag143' -> '1.4.3',
    'wcag2a' / 'wcag21aa' (level tags) are ignored.
    """
    criteria = set()
    for tag in tags:
        # axe-core tags encode SC numbers as concatenated digits: 'wcag143' = SC 1.4.3.
        # Level tags like 'wcag2a' and 'wcag21aa' have fewer than 3 digits or contain
        # letters, so the \d+ group won't match them.  The third group is \d+ (not \d)
        # to handle two-digit sub-clauses like SC 1.4.10 → 'wcag1410'.
        m = re.match(r'^wcag(\d)(\d)(\d+)$', tag)
        if m:
            sc = '{}.{}.{}'.format(m.group(1), m.group(2), m.group(3))
            criteria.add(sc)
    return criteria


def load_allowlist(path):
    """Load an allowlist file that suppresses known-acceptable incompletes.

    Format (YAML):
        - rule: color-contrast
          reason: axe-core limitation on scroll-snap flex layouts
        - rule: color-contrast
          url: /groups/mmsc/usage
          reason: Google Charts SVG
        - rule: aria-allowed-attr
          target: "#main-nav"

    Returns a list of dicts with keys: rule, url (optional), target (optional).
    """
    if not path or not os.path.exists(path):
        return []
    entries = []
    try:
        import yaml
        with open(path) as f:
            data = yaml.safe_load(f) or []
        if isinstance(data, list):
            entries = data
    except ImportError:
        # PyYAML not installed — fall back to a minimal parser that handles
        # the subset of YAML we use: a flat list of key:value dicts.
        # Each "- key: value" starts a new dict entry.  This covers the
        # allowlist format but won't handle nested structures.
        with open(path) as f:
            current = {}
            for line in f:
                line = line.rstrip()
                stripped = line.lstrip()
                if not stripped or stripped.startswith('#'):
                    continue
                # "- " marks the start of a new list item
                if stripped.startswith('- '):
                    if current:
                        entries.append(current)
                    current = {}
                    stripped = stripped[2:]
                if ':' in stripped:
                    key, _, val = stripped.partition(':')
                    current[key.strip()] = val.strip()
            if current:
                entries.append(current)
    return entries


def _matches_allowlist(rule_id, url, nodes, allowlist):
    """Check if a result matches any allowlist entry.

    Returns True if the result should be suppressed.
    """
    for entry in allowlist:
        # Rule must match
        if entry.get('rule') != rule_id:
            continue

        # If entry has a URL filter, it must appear in the page URL
        entry_url = entry.get('url', '')
        if entry_url and entry_url not in url:
            continue

        # If entry has a target filter, at least one node must match
        entry_target = entry.get('target', '')
        if entry_target:
            target_found = False
            for node in nodes:
                if entry_target in str(node.get('target', '')):
                    target_found = True
                    break
            if not target_found:
                continue

        # All filters passed — this result is allowlisted
        return True

    return False


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
            # PyYAML not installed — fall back to a minimal parser that handles
            # the subset of YAML our config files use: scalar "key: value" pairs
            # and simple lists of strings ("- item" lines under a key with no value).
            # This won't handle nested dicts, multi-line values, or flow syntax.
            with open(path) as f:
                in_list = None  # tracks which key we're appending list items to
                for line in f:
                    line = line.rstrip()
                    stripped = line.lstrip()
                    if not stripped or stripped.startswith('#'):
                        continue
                    # "- item" under a list key → append to that key's list
                    if stripped.startswith('- ') and in_list:
                        config.setdefault(in_list, []).append(stripped[2:].strip())
                        continue
                    if ':' in stripped:
                        key, _, val = stripped.partition(':')
                        key = key.strip()
                        val = val.strip()
                        if val == '' or val == '[]':
                            # Key with no value → start of a list
                            in_list = key
                            config[key] = []
                        else:
                            # Simple scalar key: value
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
            m = re.search(r'axe v([\d.]+)', header)
            AXE_VERSION = m.group(1) if m else 'unknown'
        except Exception:
            AXE_VERSION = 'unknown'
    return AXE_VERSION


def load_axe_source():
    if not os.path.exists(AXE_JS_PATH):
        print("ERROR: axe-core not found at {}".format(AXE_JS_PATH), file=sys.stderr)
        print("Download it: curl -o axe.min.js https://cdn.jsdelivr.net/npm/axe-core/axe.min.js",
              file=sys.stderr)
        sys.exit(2)
    with open(AXE_JS_PATH, 'r') as f:
        return f.read()


class RateLimiter:
    """Thread-safe rate limiter that enforces a minimum delay between calls.

    Used to ensure that all worker threads together don't exceed the
    robots.txt Crawl-delay.  Each worker calls wait() before making a
    request, and it sleeps if needed to maintain the minimum interval.
    """

    def __init__(self, min_interval):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last_time = 0

    def wait(self):
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.time()
            elapsed = now - self._last_time
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_time = time.time()


# ---------------------------------------------------------------------------
# Browser abstraction — thin wrapper so crawl_and_scan doesn't care whether
# Selenium or Playwright is behind it.  Both expose the same 6 methods:
#   navigate(url), current_url, page_source, run_js(script),
#   run_js_async(script, args), quit()
# ---------------------------------------------------------------------------

class SeleniumBrowser:
    """Browser driver backed by Selenium + ChromeDriver."""

    def __init__(self, config):
        # Pre-flight checks — catch missing binaries before Selenium's cryptic errors
        chromium = config.get('chromium_path', '/usr/bin/chromium-browser')
        chromedriver = config.get('chromedriver_path', '/usr/bin/chromedriver')
        if not os.path.isfile(chromium):
            print("ERROR: Chromium not found at: {}".format(chromium), file=sys.stderr)
            print("  Install it or set chromium_path in axe-spider.yaml", file=sys.stderr)
            sys.exit(2)
        if not os.path.isfile(chromedriver):
            print("ERROR: ChromeDriver not found at: {}".format(chromedriver), file=sys.stderr)
            print("  Install it or set chromedriver_path in axe-spider.yaml", file=sys.stderr)
            sys.exit(2)

        opts = Options()
        opts.binary_location = chromium
        opts.add_argument('--headless')
        opts.add_argument('--no-sandbox')
        opts.add_argument('--disable-dev-shm-usage')
        opts.add_argument('--disable-gpu')
        opts.add_argument('--window-size=1280,1024')
        if config.get('ignore_certificate_errors') in (True, 'true', 'yes', '1'):
            opts.add_argument('--ignore-certificate-errors')

        # Block file downloads — crawler follows all links, don't fetch binaries
        prefs = {
            'download_restrictions': 3,
            'download.default_directory': '/dev/null',
            'download.prompt_for_download': False,
            'profile.default_content_setting_values.automatic_downloads': 2,
        }
        opts.add_experimental_option('prefs', prefs)

        # Selenium 4 uses Service(), Selenium 3 uses executable_path=
        try:
            try:
                from selenium.webdriver.chrome.service import Service
                self._driver = webdriver.Chrome(
                    service=Service(chromedriver), options=opts)
            except (ImportError, TypeError):
                self._driver = webdriver.Chrome(
                    executable_path=chromedriver, options=opts)
        except WebDriverException as e:
            print("ERROR: Could not start browser.", file=sys.stderr)
            msg = str(e).split('\n')[0][:200]
            print("  {}".format(msg), file=sys.stderr)
            print("  Chromium: {}  ChromeDriver: {}".format(
                chromium, chromedriver), file=sys.stderr)
            sys.exit(2)
        self._driver.set_page_load_timeout(30)
        self._driver.set_script_timeout(120)
        self._driver.implicitly_wait(5)

    def navigate(self, url):
        self._driver.get(url)

    @property
    def current_url(self):
        return self._driver.current_url

    @property
    def page_source(self):
        return self._driver.page_source

    def run_js(self, script):
        return self._driver.execute_script(script)

    def run_js_async(self, script, args):
        return self._driver.execute_async_script(script, args)

    def quit(self):
        self._driver.quit()


class PlaywrightBrowser:
    """Browser driver backed by Playwright (faster, no chromedriver needed).

    Install: pip install playwright && playwright install chromium
    """

    def __init__(self, config):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("ERROR: playwright is not installed.", file=sys.stderr)
            print("  Install it with:", file=sys.stderr)
            print("    pip install playwright", file=sys.stderr)
            print("    playwright install chromium", file=sys.stderr)
            sys.exit(2)

        self._pw = sync_playwright().start()

        # Build launch arguments similar to our Selenium config
        launch_args = [
            '--disable-dev-shm-usage',
            '--disable-gpu',
        ]
        ignore_certs = config.get('ignore_certificate_errors') in (
            True, 'true', 'yes', '1')

        # Playwright manages its own Chromium binary, but if a custom path
        # is set in config, use it instead.
        chromium_path = config.get('chromium_path')
        if chromium_path and os.path.isfile(chromium_path):
            self._browser = self._pw.chromium.launch(
                headless=True,
                executable_path=chromium_path,
                args=launch_args,
            )
        else:
            self._browser = self._pw.chromium.launch(
                headless=True,
                args=launch_args,
            )

        self._page = self._browser.new_page(
            viewport={'width': 1280, 'height': 1024},
            ignore_https_errors=ignore_certs,
        )
        self._page.set_default_timeout(30000)
        self._page.set_default_navigation_timeout(30000)

    def navigate(self, url):
        self._page.goto(url, wait_until='load')

    @property
    def current_url(self):
        return self._page.url

    @property
    def page_source(self):
        return self._page.content()

    def run_js(self, script):
        # Playwright's evaluate() auto-returns the last expression, so it
        # doesn't support bare "return" statements like Selenium does.
        # Strip leading "return " if present to make it compatible.
        script = script.strip()
        if script.startswith('return '):
            script = script[7:]
        # For large scripts (like axe-core injection, ~500KB), use
        # add_script_tag instead of evaluate — it's more reliable and
        # doesn't hit expression-length limits.
        if len(script) > 10000:
            self._page.add_script_tag(content=script)
            return None
        return self._page.evaluate(script)

    def run_js_async(self, script, args):
        """Run axe.run() via Playwright's native Promise support.

        Selenium uses a callback-based async pattern.  Playwright can
        await Promises directly, so we skip the callback wrapper and
        call axe.run() as a Promise.
        """
        return self._page.evaluate(
            """(opts) => {
                return axe.run(document, opts).catch(err => {
                    return {error: err.toString()};
                });
            }""",
            args
        )

    def quit(self):
        try:
            self._page.close()
        except Exception:
            pass
        try:
            self._browser.close()
        except Exception:
            pass
        try:
            self._pw.stop()
        except Exception:
            pass


def create_browser(config=None):
    """Create a browser instance based on the 'driver' config setting.

    Returns a SeleniumBrowser or PlaywrightBrowser — both expose the same
    interface so the rest of the code doesn't need to know which one it is.
    """
    config = config or {}
    driver_type = config.get('driver', 'selenium').lower()

    if driver_type == 'playwright':
        return PlaywrightBrowser(config)
    else:
        return SeleniumBrowser(config)


def normalize_url(url):
    """Normalize URL for deduplication: strip fragment, trailing slash on path."""
    parsed = urlparse(url)
    path = parsed.path.rstrip('/') or '/'
    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, ''))


def is_same_origin(url, base_url):
    return urlparse(url).netloc == urlparse(base_url).netloc


def load_robots_txt(base_url):
    """Fetch and parse the site's robots.txt.

    Returns a RobotFileParser that can check whether a URL is allowed.
    Returns None if robots.txt can't be fetched (we'll allow everything).
    """
    parsed = urlparse(base_url)
    robots_url = '{}://{}/robots.txt'.format(parsed.scheme, parsed.netloc)
    parser = RobotFileParser()
    parser.set_url(robots_url)
    try:
        parser.read()
        return parser
    except Exception:
        return None


def should_scan(url, base_url, include_paths, exclude_paths, exclude_regex=None,
                exclude_query=None, robots_parser=None):
    if not is_same_origin(url, base_url):
        return False
    parsed = urlparse(url)
    path = parsed.path

    # Skip non-HTML resources (O(1) lookup via frozenset)
    ext = os.path.splitext(path.lower())[1]
    if ext in SKIP_EXTENSIONS:
        return False

    if include_paths:
        if not any(path.startswith(p) for p in include_paths):
            return False

    if exclude_paths:
        if any(path.startswith(p) for p in exclude_paths):
            return False

    if exclude_regex:
        for pat in exclude_regex:
            if pat.search(path):
                return False

    # Skip query strings that produce non-HTML output
    query = parsed.query
    if 'action=pdf' in query:
        return False
    if exclude_query:
        for q in exclude_query:
            if q in query:
                return False

    # Respect robots.txt if a parser was provided (--ignore-robots disables this).
    # We check both the exact URL and with a trailing slash, because our URL
    # normalizer strips trailing slashes but robots.txt Disallow patterns
    # often include them (e.g. "Disallow: /tools/" blocks /tools/ but
    # technically not /tools without the slash).
    if robots_parser is not None:
        if not robots_parser.can_fetch('axe-spider', url):
            return False
        url_with_slash = url.rstrip('/') + '/'
        if not robots_parser.can_fetch('axe-spider', url_with_slash):
            return False

    return True


def http_status(url, timeout=10):
    """Return HTTP status code via a lightweight HEAD request.

    Falls back to GET if the server rejects HEAD (some do).
    Returns 0 on network error.  Follows redirects — the returned
    status reflects the final response, not intermediate 3xx hops.

    This is used as a pre-check before loading pages in Chromium.
    It's much cheaper than a full browser load and lets us identify
    error pages (4xx/5xx) without wasting Chromium resources.
    """
    try:
        req = urllib.request.Request(url, method='HEAD',
                                     headers={'User-Agent': 'axe-spider/1.0'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        # 4xx/5xx errors are still valid status codes we want to return
        return e.code
    except Exception:
        # HEAD failed (connection error, or server rejects HEAD) — try GET
        try:
            req = urllib.request.Request(url, method='GET',
                                         headers={'User-Agent': 'axe-spider/1.0'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            return 0  # network error — host unreachable, DNS failure, etc.


def extract_links(driver, base_url):
    """Extract all same-origin links from the current page."""
    try:
        links = driver.run_js(
            "return Array.from(document.querySelectorAll('a[href]'))"
            ".map(a => a.href)"
            ".filter(h => h.startsWith('http'))"
        )
        return [normalize_url(link) for link in links if link]
    except Exception:
        return []


def run_axe(driver, axe_source, tags=None, rules=None):
    """Inject axe-core into the current page and run accessibility analysis.

    We inject the full axe-core JS library into every page (rather than loading
    it from a URL) because the target site may have a Content-Security-Policy
    that blocks external scripts.
    """
    # Step 1: Inject the axe-core library into the page's JS context.
    try:
        driver.run_js(axe_source)
    except Exception as e:
        return {'error': 'axe-core injection failed: {}'.format(str(e)[:100])}

    # Step 2: Configure which rules/tags to run.
    run_opts = {}
    if rules:
        run_opts['runOnly'] = {'type': 'rule', 'values': rules}
    elif tags:
        run_opts['runOnly'] = {'type': 'tag', 'values': tags}

    # Step 3: Run axe.run() via the browser's async JS execution.
    # SeleniumBrowser uses execute_async_script with a callback pattern;
    # PlaywrightBrowser rewrites this into a native Promise await.
    try:
        results = driver.run_js_async(
            """
            var callback = arguments[arguments.length - 1];
            var opts = arguments[0];
            axe.run(document, opts).then(function(results) {
                callback(results);
            }).catch(function(err) {
                callback({error: err.toString()});
            });
            """,
            run_opts
        )
    except Exception as e:
        return {'error': 'axe.run() failed: {}'.format(str(e)[:100])}
    if results is None:
        return {'error': 'axe.run() returned null (page may have navigated away)'}
    return results


def crawl_and_scan(start_url, max_pages=50, tags=None, rules=None, level=None,
                   include_paths=None, exclude_paths=None, exclude_regex=None,
                   exclude_query=None, verbose=False, quiet=False, config=None,
                   json_path=None, html_path=None, save_every=25,
                   level_label=None, allowlist=None, seed_urls=None,
                   robots_parser=None):
    """Crawl the site starting from start_url and scan each page with axe-core.

    If json_path is provided, results are flushed to disk every `save_every`
    pages and on SIGTERM/SIGINT so partial runs preserve progress.
    """
    config = config or {}

    # Line-buffered stdout so progress prints live
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    if tags is None:
        level = level or DEFAULT_LEVEL
        level_info = WCAG_LEVELS.get(level)
        if level_info is None:
            print("ERROR: Unknown level '{}'. Valid levels: {}".format(
                level, ', '.join(sorted(WCAG_LEVELS.keys()))))
            sys.exit(1)
        tags = level_info['tags']
        level_label = level_label or level_info['label']
    else:
        level_label = level_label or 'custom'

    # Lower priority so the scan doesn't starve production services.
    # Chromium is CPU- and memory-hungry; on a shared web server we'd rather
    # the scan be slow than cause Apache/MySQL to be unresponsive.
    niceness = _safe_int(config.get('niceness', 10), 10)
    oom_score = _safe_int(config.get('oom_score_adj', 1000), 1000)
    if niceness:
        try:
            os.nice(niceness)  # higher = lower CPU priority (0-19)
        except (OSError, PermissionError):
            pass  # not fatal — just means we run at normal priority
    if oom_score:
        try:
            # Tell the Linux OOM killer to sacrifice this process first.
            # 1000 = highest possible score = killed before anything else.
            with open('/proc/self/oom_score_adj', 'w') as f:
                f.write(str(oom_score))
        except (IOError, PermissionError):
            pass  # not on Linux or no permission — harmless

    page_wait = _safe_int(config.get('page_wait', 1), 1)
    axe_source = load_axe_source()
    driver = create_browser(config)
    base_url = start_url

    visited = set()
    if seed_urls:
        queue = deque(normalize_url(u) for u in seed_urls)
        no_crawl = True  # Don't follow links when using a URL list
    else:
        queue = deque([normalize_url(start_url)])
        no_crawl = False
    page_count = 0

    # MEMORY STRATEGY: Stream results to a JSONL file (one JSON object per line)
    # instead of accumulating everything in a Python dict.  Without this, a
    # 5000-page scan would hold ~500MB+ of results in memory.  By writing each
    # page's results to disk immediately, memory usage stays constant regardless
    # of scan size.  The JSONL is later converted to the final JSON/HTML reports
    # by streaming through the file line-by-line.
    jsonl_path = (json_path + 'l') if json_path else None
    if jsonl_path:
        with open(jsonl_path, 'w'):
            pass  # truncate for fresh scan

    if not quiet:
        print("Starting axe-core {} accessibility scan...".format(get_axe_version()))
        print("  Start URL: {}".format(start_url))
        print("  Level: {} ({})".format(level_label, ', '.join(tags)))
        print("  Max pages: {}".format(max_pages))
        if page_wait > 1:
            print("  Page wait: {}s".format(page_wait))
        if json_path and save_every:
            print("  Incremental save every {} pages".format(save_every))
        print()

    def _write_page(url, page_data):
        """Append one page's results to the JSONL file."""
        if not jsonl_path:
            return
        try:
            with open(jsonl_path, 'a') as f:
                f.write(json.dumps({url: page_data}, default=str) + '\n')
        except (IOError, OSError) as e:
            print("  WARNING: failed to write results for {}: {}".format(
                url, e), file=sys.stderr)

    def _flush(reason=''):
        """Build final JSON + HTML from the JSONL stream on disk."""
        if not json_path or not jsonl_path:
            return
        try:
            # Convert JSONL → final JSON by reading each line and
            # writing it into a single JSON object.  We stream line-by-line
            # so memory stays constant regardless of scan size.
            tmp = json_path + '.tmp'
            with open(tmp, 'w') as out:
                out.write('{\n')
                first_entry = True
                for page_url, page_data in _iter_jsonl(jsonl_path):
                    if not first_entry:
                        out.write(',\n')
                    json_key = json.dumps(page_url)
                    json_val = json.dumps(page_data, default=str)
                    out.write('  {}: {}'.format(json_key, json_val))
                    first_entry = False
                out.write('\n}\n')
            os.replace(tmp, json_path)
            if html_path:
                try:
                    generate_html_report(jsonl_path, html_path, start_url,
                                         level_label or 'WCAG', allowlist=allowlist)
                except Exception as e:
                    print('  (html flush failed: {})'.format(str(e)[:80]))
            if reason:
                print('  [flushed {} pages ({})]'.format(page_count, reason))
        except Exception as e:
            print('  (flush failed: {})'.format(str(e)[:80]))

    # How many browser instances to run in parallel.  Each gets its own
    # Chromium process (~200-500MB), so don't set this too high on
    # memory-constrained servers.
    num_workers = _safe_int(config.get('workers', 1), 1)

    # Rate limiter shared across all workers to enforce robots.txt crawl delay.
    # This is separate from page_wait (which is per-worker JS settle time).
    # Only the robots.txt crawl_delay is a cross-worker rate limit — page_wait
    # is applied per-worker after each page load to let JavaScript settle.
    crawl_delay = 0
    if robots_parser is not None:
        delay = robots_parser.crawl_delay('axe-spider')
        if delay is not None:
            crawl_delay = int(delay)
    rate_limiter = RateLimiter(crawl_delay)

    # Thread-safe locks for shared state
    write_lock = threading.Lock()
    print_lock = threading.Lock()
    queue_lock = threading.Lock()

    def _scan_one_page(browser, url):
        """Scan a single page and return (url, page_data, new_links) or None."""
        status = http_status(url)

        # Enforce cross-worker rate limit (from robots.txt crawl-delay)
        rate_limiter.wait()

        try:
            browser.navigate(url)
            # Per-worker settle time for JS frameworks (MathJax, SPAs, etc.)
            time.sleep(page_wait)

            current = browser.current_url

            # Redirected off-origin — skip
            if not is_same_origin(current, base_url):
                return None

            # Same-origin redirect — use actual URL
            actual_url = normalize_url(current)
            with queue_lock:
                if actual_url != url:
                    if actual_url in visited:
                        return None
                    visited.add(actual_url)
                    url = actual_url

            # Skip empty pages
            page_html = (browser.page_source or '')
            if len(page_html) < 100:
                return None

            # Skip non-HTML responses
            page_start = (browser.run_js(
                "return document.documentElement.outerHTML.substring(0, 80);") or '')
            if page_start and '<html' not in page_start.lower():
                return None

            results = run_axe(browser, axe_source, tags, rules)
            if 'error' in results:
                with print_lock:
                    print("  ERROR on {}: {}".format(url, results['error']))
                return None

            violations = results.get('violations', [])
            incomplete = results.get('incomplete', [])
            passes = results.get('passes', [])

            page_data = {
                'url': url,
                'timestamp': datetime.now().isoformat(),
                'http_status': status if status != 0 else None,
                'violations': violations,
                'incomplete': incomplete,
                'passes': passes,
                'inapplicable': results.get('inapplicable', []),
            }

            # Discover new links (unless using a URL list or error page)
            new_links = []
            is_ok = (status == 0 or status < 400)
            if not no_crawl and is_ok:
                new_links = extract_links(browser, base_url)

            return (url, page_data, new_links)

        except Exception as e:
            with print_lock:
                print("  Error on {}: {}, skipping".format(url, str(e)[:100]))
            return None

    # SIGTERM/SIGINT handler: flush partial results and exit
    interrupted = False

    def _on_signal(signum, frame):
        nonlocal interrupted
        if interrupted:
            return
        interrupted = True
        print('\n!! Signal {} — flushing {} pages...'.format(signum, page_count))
        _flush(reason='signal {}'.format(signum))
        sys.exit(128 + signum)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # Create browser pool — one per worker
    browsers = [driver]
    for _ in range(num_workers - 1):
        browsers.append(create_browser(config))

    if not quiet and num_workers > 1:
        print("  Workers: {} (parallel)".format(num_workers))

    try:
        if num_workers <= 1:
            # --- Serial mode (original behavior, no thread overhead) ---
            while queue and page_count < max_pages and not interrupted:
                url = queue.popleft()
                if url in visited:
                    continue
                visited.add(url)

                if not should_scan(url, base_url, include_paths, exclude_paths,
                                   exclude_regex, exclude_query, robots_parser):
                    continue

                page_count += 1
                result = _scan_one_page(driver, url)

                if result is not None:
                    url, page_data, new_links = result
                    v_count = _count_nodes(page_data.get('violations', []))
                    i_count = _count_nodes(page_data.get('incomplete', []))

                    if not quiet:
                        page_width = len(str(max_pages))
                        status_parts = []
                        if v_count:
                            status_parts.append('{} violations'.format(v_count))
                        if i_count:
                            status_parts.append('{} incomplete'.format(i_count))
                        status_str = ', '.join(status_parts) if status_parts else 'clean'
                        print("[{}/{}] {} — {}".format(
                            str(page_count).rjust(page_width), max_pages,
                            url, status_str))
                        if verbose and (v_count or i_count):
                            print("  Violations: {} ({} nodes), Incomplete: {} ({} nodes)".format(
                                len(page_data['violations']), v_count,
                                len(page_data['incomplete']), i_count))

                    _write_page(url, page_data)

                    for link in new_links:
                        if link not in visited and link not in queue:
                            queue.append(link)
                else:
                    page_count -= 1

                if json_path and save_every and page_count % save_every == 0:
                    _flush()

        else:
            # --- Parallel mode: thread pool with shared queue ---
            with ThreadPoolExecutor(max_workers=num_workers) as pool:
                while (queue or pool._work_queue.qsize() > 0) and \
                        page_count < max_pages and not interrupted:
                    # Submit a batch of URLs to workers
                    futures = {}
                    while queue and len(futures) < num_workers and \
                            page_count + len(futures) < max_pages:
                        url = queue.popleft()
                        if url in visited:
                            continue
                        visited.add(url)
                        if not should_scan(url, base_url, include_paths,
                                           exclude_paths, exclude_regex,
                                           exclude_query, robots_parser):
                            continue
                        # Round-robin browser assignment
                        browser_idx = len(futures) % num_workers
                        future = pool.submit(
                            _scan_one_page, browsers[browser_idx], url)
                        futures[future] = url

                    if not futures:
                        break

                    # Collect results as they complete
                    for future in as_completed(futures):
                        page_count += 1
                        result = future.result()

                        if result is not None:
                            url, page_data, new_links = result
                            v_count = _count_nodes(
                                page_data.get('violations', []))
                            i_count = _count_nodes(
                                page_data.get('incomplete', []))

                            with print_lock:
                                if not quiet:
                                    pw = len(str(max_pages))
                                    parts = []
                                    if v_count:
                                        parts.append(
                                            '{} violations'.format(v_count))
                                    if i_count:
                                        parts.append(
                                            '{} incomplete'.format(i_count))
                                    ss = ', '.join(parts) if parts else 'clean'
                                    print("[{}/{}] {} — {}".format(
                                        str(page_count).rjust(pw),
                                        max_pages, url, ss))

                            with write_lock:
                                _write_page(url, page_data)

                            with queue_lock:
                                for link in new_links:
                                    if link not in visited and \
                                            link not in queue:
                                        queue.append(link)
                        else:
                            page_count -= 1

                    if json_path and save_every and \
                            page_count % save_every == 0:
                        _flush()

    finally:
        for browser in browsers:
            try:
                browser.quit()
            except Exception:
                pass
        _flush(reason='final')

    return page_count, jsonl_path


def _iter_jsonl(jsonl_path):
    """Iterate (url, data) pairs from a JSONL results file.

    Skips blank or corrupt lines (e.g. from a partial write after a crash).
    """
    with open(jsonl_path, 'r') as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                print("  WARNING: corrupt JSONL line {} in {}, skipping".format(
                    lineno, jsonl_path), file=sys.stderr)
                continue
            for url, data in obj.items():
                yield url, data


def generate_html_report(jsonl_path, output_path, start_url,
                         level_label='WCAG 2.1 Level AA', allowlist=None):
    """Generate an HTML report by streaming through JSONL results on disk.

    Memory usage is O(unique_rules) regardless of page count.
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    axe_ver = get_axe_version()
    allowlist = allowlist or []

    # --- Pass 1: aggregate stats (constant memory) ---
    total_pages = 0
    total_violations = 0
    total_violation_nodes = 0
    total_incomplete_nodes = 0
    total_suppressed = 0
    impact_counts = {'critical': 0, 'serious': 0, 'moderate': 0, 'minor': 0}
    rule_summary = {}
    incomplete_summary = {}
    wcag_criteria = {}

    def _track_wcag(tags, category, count=1):
        for sc in _parse_wcag_sc(tags):
            if sc not in wcag_criteria:
                wcag_criteria[sc] = {'violations': 0, 'incomplete': 0, 'passes': 0}
            wcag_criteria[sc][category] += count

    for url, data in _iter_jsonl(jsonl_path):
        total_pages += 1
        for v in data.get('violations', []):
            nodes = v.get('nodes', [])
            total_violations += 1
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
            _track_wcag(v.get('tags', []), 'violations', len(nodes))

        for v in data.get('incomplete', []):
            nodes = v.get('nodes', [])
            rule_id = v.get('id', 'unknown')
            if _matches_allowlist(rule_id, url, nodes, allowlist):
                total_suppressed += len(nodes)
                continue
            total_incomplete_nodes += len(nodes)
            if rule_id not in incomplete_summary:
                incomplete_summary[rule_id] = {
                    'help': v.get('help', ''),
                    'helpUrl': v.get('helpUrl', ''),
                    'impact': v.get('impact', 'unknown'),
                    'count': 0,
                    'pages': [],
                }
            incomplete_summary[rule_id]['count'] += len(nodes)
            incomplete_summary[rule_id]['pages'].append(url)
            _track_wcag(v.get('tags', []), 'incomplete', len(nodes))

        for v in data.get('passes', []):
            _track_wcag(v.get('tags', []), 'passes')

    sorted_rules = sorted(rule_summary.items(), key=lambda x: x[1]['count'], reverse=True)
    sorted_incomplete = sorted(incomplete_summary.items(), key=lambda x: x[1]['count'], reverse=True)

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
    html_parts.append('<p class="meta">Scope: HTML pages only. '
                      'Does not cover PDFs, videos, audio, PowerPoint, Word documents, '
                      'or other media files.</p>')

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
        '<div class="label">Needs Review</div></div>'.format(total_incomplete_nodes))
    if total_suppressed:
        html_parts.append(
            '<div class="summary-card"><div class="number" style="color:#888">{}</div>'
            '<div class="label">Suppressed (allowlist)</div></div>'.format(total_suppressed))
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

    # WCAG criteria summary
    if wcag_criteria:
        sorted_sc = sorted(wcag_criteria.items(), key=lambda x: x[0])
        html_parts.append('<h2>WCAG Success Criteria</h2>')
        html_parts.append('<table><tr><th>Criterion</th><th>Name</th>'
                          '<th style="color:#d32f2f">Violations</th>'
                          '<th style="color:#e65100">Incomplete</th>'
                          '<th style="color:#2e7d32">Passes</th>'
                          '<th>Status</th></tr>')
        for sc, counts in sorted_sc:
            name = WCAG_SC_NAMES.get(sc, '')
            v = counts['violations']
            i = counts['incomplete']
            p = counts['passes']
            if v > 0:
                status = '<span class="badge badge-critical">FAIL</span>'
            elif i > 0:
                status = '<span class="badge badge-serious">REVIEW</span>'
            else:
                status = '<span style="color:#2e7d32;font-weight:bold">PASS</span>'
            html_parts.append(
                '<tr><td>{sc}</td><td>{name}</td>'
                '<td>{v}</td><td>{i}</td><td>{p}</td>'
                '<td>{status}</td></tr>'.format(
                    sc=_esc(sc), name=_esc(name),
                    v=v or '', i=i or '', p=p or '',
                    status=status))
        html_parts.append('</table>')

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

    # Incomplete summary table
    if sorted_incomplete:
        html_parts.append('<h2>Incomplete Summary (Needs Manual Review)</h2>')
        html_parts.append('<p class="meta">axe-core could not automatically determine '
                          'pass/fail for these items — typically color-contrast on elements '
                          'with background images, gradients, or pseudo-elements.</p>')
        html_parts.append('<table><tr><th>Rule</th><th>Nodes</th>'
                          '<th>Pages</th><th>Description</th></tr>')
        for rule_id, info in sorted_incomplete:
            html_parts.append(
                '<tr><td><a href="{url}">{id}</a></td>'
                '<td>{count}</td><td>{pages}</td><td>{desc}</td></tr>'.format(
                    url=_esc(info['helpUrl']), id=_esc(rule_id),
                    count=info['count'], pages=len(set(info['pages'])),
                    desc=_esc(info['help'])))
        html_parts.append('</table>')

    # --- Pass 2: per-page details (stream from JSONL again) ---
    html_parts.append('<h2>Per-Page Details</h2>')
    clean_pages = []
    for url, data in _iter_jsonl(jsonl_path):
        violations = data.get('violations', [])
        incomplete = data.get('incomplete', [])
        # Filter out allowlisted incompletes
        shown_incomplete = []
        for v in incomplete:
            rule_id = v.get('id', '')
            nodes = v.get('nodes', [])
            if not _matches_allowlist(rule_id, url, nodes, allowlist):
                shown_incomplete.append(v)
        if not violations and not shown_incomplete:
            clean_pages.append(url)
            continue

        v_count = _count_nodes(violations)
        i_count = _count_nodes(shown_incomplete)
        html_parts.append('<div class="page-section">')
        html_parts.append('<h3><a href="{}" class="page-url">{}</a></h3>'.format(
            _esc(url), _esc(url)))
        html_parts.append('<p>{} violation(s), {} issue(s) &mdash; {} incomplete, {} node(s)</p>'.format(
            len(violations), v_count, len(shown_incomplete), i_count))

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
            html_parts.append(_render_nodes_html(v.get('nodes', []), limit=20))
            html_parts.append('</div>')

        if shown_incomplete:
            html_parts.append('<h4 style="margin-top:1em;color:#e65100;">Incomplete (needs manual review)</h4>')
            for v in shown_incomplete:
                html_parts.append('<div class="rule-card">')
                html_parts.append(
                    '<strong><a href="{}">{}</a></strong>'.format(
                        _esc(v.get('helpUrl', '')), _esc(v.get('id', ''))))
                html_parts.append('<p>{}</p>'.format(_esc(v.get('help', ''))))
                html_parts.append(_render_nodes_html(v.get('nodes', []), limit=10, snippet_max=300))
                html_parts.append('</div>')

        html_parts.append('</div>')

    if clean_pages:
        html_parts.append('<h2>Fully Clean Pages ({})'.format(len(clean_pages)))
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
            .replace('"', '&quot;')
            .replace("'", '&#39;'))


def _render_nodes_html(nodes, limit=20, snippet_max=500):
    """Render axe-core node details as HTML fragments."""
    parts = []
    parts.append('<details><summary>{} element(s)</summary>'.format(len(nodes)))
    for node in nodes[:limit]:
        parts.append('<div class="node-detail">')
        target = node.get('target', [])
        if target:
            parts.append('<p class="target">Selector: {}</p>'.format(
                _esc(', '.join(str(t) for t in target))))
        html_snippet = node.get('html', '')
        if html_snippet:
            if len(html_snippet) > snippet_max:
                html_snippet = html_snippet[:snippet_max] + '...'
            parts.append('<div class="html-snippet">{}</div>'.format(_esc(html_snippet)))
        messages = []
        for check in node.get('any', []) + node.get('all', []) + node.get('none', []):
            msg = check.get('message', '')
            if msg:
                messages.append(msg)
        if messages:
            parts.append('<ul>')
            for msg in messages[:5]:
                parts.append('<li>{}</li>'.format(_esc(msg)))
            parts.append('</ul>')
        parts.append('</div>')
    if len(nodes) > limit:
        parts.append('<p><em>... and {} more</em></p>'.format(len(nodes) - limit))
    parts.append('</details>')
    return '\n'.join(parts)


def generate_llm_report(jsonl_path, output_path, start_url,
                        level_label='WCAG 2.1 Level AA', allowlist=None,
                        config=None):
    """Generate a token-efficient markdown summary optimized for LLMs.

    Instead of dumping raw JSON (100K+ tokens for a large scan), this
    produces a compact report (~2-5K tokens) with:
    - Context and instructions for the LLM
    - Violations grouped by rule with deduplicated examples
    - Incompletes grouped by messageKey
    - Affected page lists (URLs only, no repeated node data)
    """
    allowlist = allowlist or []
    axe_ver = get_axe_version()

    # Aggregate: {rule_id -> {info, pages, example_nodes}}
    violations_by_rule = {}
    incompletes_by_key = {}
    total_pages = 0
    pages_with_violations = set()
    pages_with_incompletes = set()
    suppressed_count = 0

    for url, data in _iter_jsonl(jsonl_path):
        total_pages += 1
        path = urlparse(url).path

        for v in data.get('violations', []):
            rule_id = v.get('id', 'unknown')
            nodes = v.get('nodes', [])
            pages_with_violations.add(path)
            if rule_id not in violations_by_rule:
                violations_by_rule[rule_id] = {
                    'help': v.get('help', ''),
                    'helpUrl': v.get('helpUrl', ''),
                    'impact': v.get('impact', ''),
                    'tags': v.get('tags', []),
                    'count': 0,
                    'pages': [],
                    'examples': [],
                }
            info = violations_by_rule[rule_id]
            info['count'] += len(nodes)
            if path not in info['pages']:
                info['pages'].append(path)
            # Keep up to 3 unique example HTML snippets
            for node in nodes:
                snippet = node.get('html', '')[:200]
                if snippet and len(info['examples']) < 3 and snippet not in info['examples']:
                    info['examples'].append(snippet)

        for v in data.get('incomplete', []):
            nodes = v.get('nodes', [])
            rule_id = v.get('id', 'unknown')
            if _matches_allowlist(rule_id, url, nodes, allowlist):
                suppressed_count += len(nodes)
                continue
            pages_with_incompletes.add(path)
            for node in nodes:
                for check in node.get('any', []):
                    d = check.get('data', {})
                    mk = d.get('messageKey', '') if isinstance(d, dict) else ''
                    if mk not in incompletes_by_key:
                        incompletes_by_key[mk] = {'count': 0, 'pages': set(), 'examples': []}
                    info = incompletes_by_key[mk]
                    info['count'] += 1
                    info['pages'].add(path)
                    snippet = node.get('html', '')[:150]
                    if snippet and len(info['examples']) < 2 and snippet not in info['examples']:
                        info['examples'].append(snippet)

    # Build markdown
    lines = []
    lines.append('# axe-spider accessibility scan results\n')
    lines.append('Site: {}  '.format(start_url))
    lines.append('Level: {}  '.format(level_label))
    lines.append('axe-core: {}  '.format(axe_ver))
    lines.append('Pages scanned: {}  '.format(total_pages))
    lines.append('Scan date: {}\n'.format(datetime.now().strftime('%Y-%m-%d')))
    lines.append('**Scope**: HTML pages only.  This scan does not cover accessibility of '
                 'PDFs, videos, audio, PowerPoint, Word documents, or other media files.')

    # Instructions section — read from a file if configured, otherwise use defaults.
    # This lets each site customize the LLM prompt for their specific codebase
    # (e.g. "templates are in app/templates/cdm/", "use LESS not CSS", etc.)
    llm_instructions_path = config.get('llm_instructions') if config else None
    if llm_instructions_path and os.path.exists(llm_instructions_path):
        with open(llm_instructions_path) as f:
            lines.append(f.read().rstrip())
        lines.append('')
    else:
        lines.append('## Instructions\n')
        lines.append('This is a WCAG accessibility scan summary. When investigating:')
        lines.append('- Each violation needs a code fix — find the source that generates the flagged HTML')
        lines.append('- Incompletes are items axe-core could not auto-verify (usually contrast issues)')
        lines.append('- The "examples" show representative HTML — the same pattern repeats across listed pages')
        lines.append('- Focus on violations first (failures), then incompletes (may be false positives)\n')

    # Violations
    if violations_by_rule:
        lines.append('## Violations ({} issues on {} pages)\n'.format(
            sum(v['count'] for v in violations_by_rule.values()),
            len(pages_with_violations)))
        for rule_id, info in sorted(
                violations_by_rule.items(),
                key=lambda x: x[1]['count'], reverse=True):
            wcag_scs = ', '.join(sorted(_parse_wcag_sc(info['tags'])))
            lines.append('### {} ({}, {} issues)'.format(rule_id, info['impact'], info['count']))
            lines.append('{}'.format(info['help']))
            if wcag_scs:
                lines.append('WCAG: {}'.format(wcag_scs))
            lines.append('Pages: {}'.format(', '.join(info['pages'][:10])))
            if len(info['pages']) > 10:
                lines.append('  ... and {} more'.format(len(info['pages']) - 10))
            lines.append('Examples:')
            for ex in info['examples']:
                lines.append('```html\n{}\n```'.format(ex))
            lines.append('')
    else:
        lines.append('## Violations: NONE\n')

    # Incompletes
    if incompletes_by_key:
        total_inc = sum(v['count'] for v in incompletes_by_key.values())
        lines.append('## Incompletes ({} nodes on {} pages)\n'.format(
            total_inc, len(pages_with_incompletes)))
        for mk, info in sorted(
                incompletes_by_key.items(),
                key=lambda x: x[1]['count'], reverse=True):
            lines.append('### {} — {} nodes, {} pages'.format(
                mk or '(unknown)', info['count'], len(info['pages'])))
            pages_list = sorted(info['pages'])
            lines.append('Pages: {}'.format(', '.join(pages_list[:10])))
            if len(pages_list) > 10:
                lines.append('  ... and {} more'.format(len(pages_list) - 10))
            if info['examples']:
                lines.append('Example:')
                lines.append('```html\n{}\n```'.format(info['examples'][0]))
            lines.append('')
    else:
        lines.append('## Incompletes: NONE\n')

    if suppressed_count:
        lines.append('## Suppressed (allowlist): {} nodes\n'.format(suppressed_count))

    # Point to full reports for deeper investigation
    json_sibling = output_path.replace('.md', '.json')
    jsonl_sibling = output_path.replace('.md', '.jsonl')
    html_sibling = output_path.replace('.md', '.html')
    lines.append('## Detailed reports\n')
    lines.append('This is a summary.  For full per-page, per-node details:')
    lines.append('- JSON (full axe-core output): {}'.format(json_sibling))
    lines.append('- JSONL (streaming, for --diff/--rescan): {}'.format(jsonl_sibling))
    lines.append('- HTML (human-readable report): {}'.format(html_sibling))
    lines.append('- Run `axe-spider.py --help-audit` for the full audit workflow guide')
    lines.append('')

    report = '\n'.join(lines)
    with open(output_path, 'w') as f:
        f.write(report)
    return report


def diff_scans(old_jsonl, new_jsonl, allowlist=None):
    """Compare two scans and print what changed.

    Returns (fixed_count, new_count) violation nodes.
    """
    allowlist = allowlist or []

    def _violation_keys(jsonl_path):
        """Return {(url_path, rule_id): node_count} for violations."""
        keys = {}
        for url, data in _iter_jsonl(jsonl_path):
            path = urlparse(url).path
            for v in data.get('violations', []):
                key = (path, v.get('id', ''))
                keys[key] = keys.get(key, 0) + len(v.get('nodes', []))
        return keys

    old = _violation_keys(old_jsonl)
    new = _violation_keys(new_jsonl)

    fixed = {k: v for k, v in old.items() if k not in new}
    added = {k: v for k, v in new.items() if k not in old}
    remaining = {k: v for k, v in new.items() if k in old}

    if fixed:
        print("\n  FIXED ({} rule/page combos, {} nodes):".format(
            len(fixed), sum(fixed.values())))
        for (path, rule), count in sorted(fixed.items()):
            print("    - {} on {} ({} nodes)".format(rule, path, count))

    if added:
        print("\n  NEW ({} rule/page combos, {} nodes):".format(
            len(added), sum(added.values())))
        for (path, rule), count in sorted(added.items()):
            print("    + {} on {} ({} nodes)".format(rule, path, count))

    if remaining:
        print("\n  REMAINING ({} rule/page combos, {} nodes)".format(
            len(remaining), sum(remaining.values())))

    if not fixed and not added:
        print("\n  No changes in violations.")

    return sum(fixed.values()), sum(added.values())


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
    parser.add_argument('--ignore-robots', action='store_true',
                        help='Ignore robots.txt (by default, disallowed paths are skipped)')
    parser.add_argument('--output', default=None,
                        help='Output file basename (default: axe-spider-YYYY-MM-DD-HHMMSS)')
    parser.add_argument('--output-dir', default=None,
                        help='Output directory (default: from config or current directory)')
    parser.add_argument('--allowlist', default=None,
                        help='YAML file of known-acceptable incompletes to suppress')
    parser.add_argument('--workers', type=int, default=None,
                        help='Number of parallel browser instances (default: 1). '
                             'Each uses ~200-500MB RAM. Rate limits are shared.')
    parser.add_argument('--save-every', type=int, default=None,
                        help='Flush reports every N pages (default: 25). '
                             'Partial results survive if the scan is killed.')
    parser.add_argument('--diff', default=None, metavar='PREV.jsonl',
                        help='Compare against a previous scan JSONL and show what changed')
    parser.add_argument('--urls', default=None, metavar='FILE',
                        help='Scan URLs from a file (one per line) instead of crawling')
    parser.add_argument('--rescan', default=None, metavar='PREV.jsonl',
                        help='Re-scan only pages that had violations or incompletes in a previous scan')
    parser.add_argument('--rule', action='append', default=None,
                        help='Only run specific axe rules (repeatable, e.g. --rule color-contrast)')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--page', action='store_true',
                       help='Scan only the given URL (no crawling). Fast single-page verify.')
    group.add_argument('--crawl', action='store_true', default=True,
                       help='Crawl and discover pages from the starting URL (default).')
    parser.add_argument('--llm', action='store_true',
                        help='Generate a compact markdown summary optimized for LLM context')
    parser.add_argument('--summary-json', action='store_true',
                        help='Print a one-line JSON summary to stdout (machine-parseable)')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='Suppress per-page progress, only show final summary')
    parser.add_argument('--help-audit', action='store_true',
                        help='Print a guide for using this tool to perform a WCAG audit')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Show detailed rule/node counts for pages with issues')

    args = parser.parse_args()

    if args.help_audit:
        print("""
WCAG Accessibility Audit Guide
===============================

You are a WCAG accessibility auditor. Use axe-spider to scan websites for
WCAG 2.1 AA compliance violations and then fix them in the source code.

AUDIT WORKFLOW
--------------
1. SCAN: Run a full crawl to establish a baseline.
     axe-spider.py --max-pages 500 --llm https://example.com/
   Read the .md (LLM report) for a concise summary of issues.

2. PRIORITIZE: Fix violations first (WCAG failures), then incompletes.
   Violations are grouped by rule — fix the rule with the most instances
   first for maximum impact.

3. FIX: For each violation, find the template/CSS that generates the
   flagged HTML. Common fixes:
   - color-contrast: darken text or lighten background to reach 4.5:1
   - missing alt text: add descriptive alt attributes to images
   - missing labels: add <label> or aria-label to form controls
   - empty headings/links: add text content or aria-label
   - focus visible: add :focus outline styles

4. VERIFY: After each fix, re-check the specific page:
     axe-spider.py --page -q --summary-json https://example.com/fixed-page
   Check exit code: 0 = clean, 1 = still has violations.

5. REGRESSION CHECK: Re-scan previous failures to confirm fixes:
     axe-spider.py --rescan baseline.jsonl --diff baseline.jsonl --llm
   The diff shows what was fixed vs what's new vs what remains.

6. SUPPRESS KNOWN ISSUES: For axe-core limitations that aren't real
   accessibility problems (e.g. can't compute contrast on gradients),
   add entries to an allowlist.yaml:
     - rule: color-contrast
       url: /homepage
       reason: axe-core flex layout measurement limitation

UNDERSTANDING RESULTS
---------------------
- VIOLATIONS: Definite WCAG failures. Must be fixed.
- INCOMPLETE: axe-core couldn't auto-verify. May be real issues or
  false positives. Common causes: background gradients, images, pseudo-
  elements blocking contrast computation, elements outside viewport.
- PASSES: Rules that were checked and satisfied.

COMMON AXE-CORE INCOMPLETE TYPES (usually not real issues):
- bgOverlap/elmPartiallyObscured: flex/scroll layout measurement artifacts
- pseudoContent: CSS ::before/::after blocking contrast computation
- bgGradient/bgImage: background-image preventing contrast resolution
  Fix: set explicit background-color on text elements
- shortTextContent: single-character text (e.g. x delete buttons)
  Fix: move character to CSS ::after, leave element empty
- nonBmp: icon font glyphs axe can't evaluate
  Fix: move icon character to CSS ::after on aria-hidden elements

KEY FLAGS FOR LLM WORKFLOWS
----------------------------
--page              Scan one URL, no crawling (fast verify after a fix)
--rule NAME         Check only specific rules (fast, focused)
--summary-json      Machine-parseable one-line JSON output
--llm               Generate compact markdown report (~300 tokens vs 300K)
--diff PREV.jsonl   Show what changed since last scan
--rescan PREV.jsonl Only re-scan pages that previously had issues
--allowlist FILE    Suppress known-acceptable incompletes
-q                  Quiet — no per-page progress, just final summary
-v                  Verbose — add detailed rule/node counts for problem pages

OTHER NOTES
-----------
- robots.txt is respected by default.  Use --ignore-robots to scan
  disallowed paths, or set ignore_robots: true in your config.
- Reports are flushed every 25 pages (configurable with --save-every)
  and on SIGTERM/SIGINT, so partial results survive if the scan is killed.
- The scanner runs at low CPU priority (nice 10) and high OOM score
  (1000) by default so it won't starve production services on shared
  servers.  Both are configurable in axe-spider.yaml.
""")
        sys.exit(0)

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

    # Load URL list from file or previous scan
    seed_urls = None
    if args.rescan:
        if not os.path.exists(args.rescan):
            parser.error('Rescan file not found: {}'.format(args.rescan))
        seed_urls = []
        for prev_url, prev_data in _iter_jsonl(args.rescan):
            if prev_data.get('violations') or prev_data.get('incomplete'):
                seed_urls.append(prev_url)
        if not seed_urls:
            print("No failures in previous scan — nothing to rescan.")
            sys.exit(0)
        print("Rescanning {} pages with previous violations/incompletes".format(len(seed_urls)))
    if args.urls:
        if not os.path.exists(args.urls):
            parser.error('URL file not found: {}'.format(args.urls))
        with open(args.urls) as f:
            seed_urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        if not seed_urls:
            parser.error('No URLs found in {}'.format(args.urls))
        if not url:
            url = seed_urls[0]

    # Resolve max pages
    if args.page:
        max_pages = 1
    elif seed_urls:
        max_pages = len(seed_urls)
    else:
        max_pages = args.max_pages or _safe_int(config.get('max_pages', 50), 50)

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

    # Resolve exclude regex from config
    exclude_regex = None
    regex_list = config.get('exclude_regex', [])
    if regex_list and not args.no_default_excludes:
        exclude_regex = []
        for pattern in regex_list:
            try:
                exclude_regex.append(re.compile(pattern))
            except re.error as e:
                print("WARNING: invalid exclude_regex '{}': {}".format(pattern, e),
                      file=sys.stderr)

    # Resolve exclude query strings from config (e.g. action=overview)
    exclude_query = config.get('exclude_query') if not args.no_default_excludes else None

    # Resolve output
    save_every = args.save_every or _safe_int(config.get('save_every', 25), 25)

    # Workers: number of parallel browser instances
    if args.workers:
        config['workers'] = args.workers
    basename = args.output or 'axe-spider-{}'.format(datetime.now().strftime('%Y-%m-%d-%H%M%S'))
    output_dir = args.output_dir or config.get('output_dir', os.getcwd())
    os.makedirs(output_dir, exist_ok=True)

    # Load allowlist
    allowlist_path = args.allowlist or config.get('allowlist')
    allowlist = load_allowlist(allowlist_path) if allowlist_path else []
    if allowlist:
        print("Allowlist: {} entries from {}".format(len(allowlist), allowlist_path))

    # Load robots.txt unless told to ignore it.
    # By default we respect robots.txt — it's polite and often excludes
    # the same paths we'd want to skip anyway (admin, API, login, etc.).
    ignore_robots = args.ignore_robots or config.get('ignore_robots') in (
        True, 'true', 'yes', '1')
    robots_parser = None
    if not ignore_robots:
        robots_parser = load_robots_txt(url)
        if robots_parser and not args.quiet:
            print("Respecting robots.txt (use --ignore-robots to override)")

    html_path = os.path.join(output_dir, basename + '.html')
    json_path = os.path.join(output_dir, basename + '.json')

    scanned, jsonl_path = crawl_and_scan(
        url,
        max_pages=max_pages,
        tags=tags,
        rules=args.rule,
        level=args.level,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        exclude_regex=exclude_regex,
        exclude_query=exclude_query,
        verbose=args.verbose,
        quiet=args.quiet,
        config=config,
        json_path=json_path,
        html_path=html_path,
        save_every=save_every,
        level_label=level_label,
        allowlist=allowlist,
        seed_urls=seed_urls,
        robots_parser=robots_parser,
    )

    # Final reports already flushed by crawl_and_scan
    print("\nJSON report: {}".format(json_path))
    print("HTML report: {}".format(html_path))

    if args.llm and jsonl_path and os.path.exists(jsonl_path):
        llm_path = os.path.join(output_dir, basename + '.md')
        generate_llm_report(jsonl_path, llm_path, url,
                            level_label=level_label, allowlist=allowlist,
                            config=config)
        print("LLM report: {}".format(llm_path))

    # Summary (single pass through JSONL)
    total_violations = 0
    total_incomplete = 0
    violation_rules = set()
    if jsonl_path and os.path.exists(jsonl_path):
        for _, data in _iter_jsonl(jsonl_path):
            total_violations += _count_nodes(data.get('violations', []))
            total_incomplete += _count_nodes(data.get('incomplete', []))
            for v in data.get('violations', []):
                violation_rules.add(v.get('id', ''))

    print("\nScan complete: {} pages scanned".format(scanned))
    print("  Violations: {} node(s) failing WCAG rules".format(total_violations))
    print("  Incomplete: {} node(s) needing manual review".format(total_incomplete))

    if args.summary_json:
        summary = {
            'pages': scanned,
            'violations': total_violations,
            'incomplete': total_incomplete,
            'rules': sorted(violation_rules),
            'clean': total_violations == 0,
        }
        print(json.dumps(summary))

    # Diff against previous scan
    if args.diff and jsonl_path and os.path.exists(jsonl_path):
        if os.path.exists(args.diff):
            print("\nDiff vs {}:".format(args.diff))
            diff_scans(args.diff, jsonl_path, allowlist=allowlist)
        else:
            print("\nWARNING: diff file not found: {}".format(args.diff))

    # Exit code: 0 = clean, 1 = violations found
    if total_violations > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
