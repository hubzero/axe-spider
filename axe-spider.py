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
import atexit
import json
import os
import re
import signal
import subprocess
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

# Required dependencies.  Catch ImportError here rather than letting
# Python's traceback confuse users who haven't installed them.
try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is not installed.", file=sys.stderr)
    print("  Install it with:  pip install pyyaml", file=sys.stderr)
    sys.exit(2)

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
    with open(path) as f:
        data = yaml.safe_load(f) or []
    if isinstance(data, list):
        entries = data
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
        with open(path) as f:
            config = yaml.safe_load(f) or {}
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
    """Read the bundled axe-core JS library into a string for injection."""
    if not os.path.exists(AXE_JS_PATH):
        print("ERROR: axe-core not found at {}".format(AXE_JS_PATH), file=sys.stderr)
        print("Download it: curl -o axe.min.js https://cdn.jsdelivr.net/npm/axe-core/axe.min.js",
              file=sys.stderr)
        sys.exit(2)
    with open(AXE_JS_PATH, 'r') as f:
        return f.read()


# Track all browser processes we launch so we can kill them on exit.
# This prevents orphaned chromium/chromedriver processes when the script
# crashes, is killed, or exits abnormally.
_browser_pids = set()
_browser_pids_lock = threading.Lock()


def _register_browser_pid(pid):
    """Register a browser process PID for cleanup on exit."""
    with _browser_pids_lock:
        _browser_pids.add(pid)


def _cleanup_browsers():
    """Kill any browser processes we launched.  Called via atexit."""
    with _browser_pids_lock:
        for pid in _browser_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        _browser_pids.clear()

    # Also kill any chromedriver/chromium processes that are children of
    # this process (catches anything missed by PID tracking).
    my_pid = os.getpid()
    try:
        result = subprocess.run(
            ['ps', '-eo', 'pid,ppid,comm'],
            capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                try:
                    child_pid = int(parts[0])
                    parent_pid = int(parts[1])
                    comm = parts[2]
                    if parent_pid == my_pid and (
                            'chrome' in comm):
                        os.kill(child_pid, signal.SIGKILL)
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
    except Exception:
        pass


atexit.register(_cleanup_browsers)


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
        """Block (synchronous) until the rate limit allows the next request."""
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.time()
            elapsed = now - self._last_time
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_time = time.time()

    def wait_time(self):
        """Return seconds to sleep (for async callers that need asyncio.sleep)."""
        if self.min_interval <= 0:
            return 0
        with self._lock:
            now = time.time()
            elapsed = now - self._last_time
            delay = max(0, self.min_interval - elapsed)
            self._last_time = now + delay
            return delay


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
        # Track the chromedriver PID for cleanup on exit
        try:
            pid = self._driver.service.process.pid
            _register_browser_pid(pid)
        except Exception:
            pass

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

    Used for serial scanning (workers=1).  For parallel scanning (workers>1),
    crawl_and_scan uses Playwright's async API directly — it doesn't go
    through this class.

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
        ignore_certs = config.get('ignore_certificate_errors') in (
            True, 'true', 'yes', '1')
        launch_args = ['--disable-dev-shm-usage', '--disable-gpu']

        # Playwright manages its own Chromium, but use a custom path if set
        chromium_path = config.get('chromium_path')
        if chromium_path and os.path.isfile(chromium_path):
            self._browser = self._pw.chromium.launch(
                headless=True, executable_path=chromium_path,
                args=launch_args)
        else:
            self._browser = self._pw.chromium.launch(
                headless=True, args=launch_args)

        try:
            _register_browser_pid(self._browser.process.pid)
        except Exception:
            pass

        self._page = self._browser.new_page(
            viewport={'width': 1280, 'height': 1024},
            ignore_https_errors=ignore_certs,
        )
        self._page.set_default_timeout(30000)
        self._page.set_default_navigation_timeout(30000)

    def navigate(self, url):
        """Load a URL in the browser."""
        self._page.goto(url, wait_until='load')

    @property
    def current_url(self):
        """Return the current page URL (after any redirects)."""
        return self._page.url

    @property
    def page_source(self):
        """Return the page's HTML content."""
        return self._page.content()

    def run_js(self, script):
        """Execute JavaScript synchronously and return the result.

        Handles two Playwright quirks:
        - Strips 'return ' prefix (Playwright auto-returns expressions)
        - Uses add_script_tag for large scripts (axe-core is ~500KB)
        """
        script = script.strip()
        if script.startswith('return '):
            script = script[7:]
        if len(script) > 10000:
            self._page.add_script_tag(content=script)
            return None
        return self._page.evaluate(script)

    def run_js_async(self, script, args):
        """Run axe.run() via Playwright's native Promise support.

        Selenium uses a callback pattern for async JS.  Playwright can
        await Promises directly, so we call axe.run() as a Promise.
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
        """Close the page, browser, and Playwright process."""
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




def load_cookies(config):
    """Load cookies from the cookies file.

    Returns a list of cookie dicts, or an empty list if not configured
    or file doesn't exist.
    """
    auth = config.get('auth', {})
    if not auth:
        return []
    cookies_file = auth.get('cookies_file', '')
    if not cookies_file:
        return []
    cookies_file = os.path.expanduser(cookies_file)
    if not os.path.isfile(cookies_file):
        return []
    try:
        with open(cookies_file) as f:
            cookies = json.load(f)
        return cookies if isinstance(cookies, list) else []
    except Exception:
        return []


def inject_cookies_selenium(driver, cookies, base_url):
    """Inject saved cookies into a Selenium browser session."""
    if not cookies:
        return
    # Navigate to the domain first so cookies can be set
    driver.navigate(base_url)
    for cookie in cookies:
        # Selenium needs specific format — strip Playwright-specific keys
        c = {}
        for key in ('name', 'value', 'domain', 'path', 'secure', 'httpOnly'):
            if key in cookie:
                c[key] = cookie[key]
        if 'expires' in cookie:
            c['expiry'] = int(cookie['expires']) if cookie['expires'] else None
        if 'sameSite' in cookie:
            val = cookie['sameSite']
            if val in ('Strict', 'Lax', 'None'):
                c['sameSite'] = val
        try:
            driver._driver.add_cookie(c)
        except Exception:
            pass


def inject_cookies_playwright(context, cookies):
    """Inject saved cookies into a Playwright browser context (async)."""
    if not cookies:
        return []
    # Playwright cookies need 'name', 'value', 'url' or 'domain'+'path'
    clean = []
    for c in cookies:
        entry = {
            'name': c.get('name', ''),
            'value': c.get('value', ''),
            'domain': c.get('domain', ''),
            'path': c.get('path', '/'),
        }
        if 'secure' in c:
            entry['secure'] = c['secure']
        if 'httpOnly' in c:
            entry['httpOnly'] = c['httpOnly']
        if 'expires' in c and c['expires']:
            entry['expires'] = float(c['expires'])
        if 'sameSite' in c and c['sameSite'] in ('Strict', 'Lax', 'None'):
            entry['sameSite'] = c['sameSite']
        clean.append(entry)
    return clean


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


# Parameters to strip from URLs during normalization.  These are common
# pagination, sorting, and redirect params that produce the same page
# template with different data.  Stripping them deduplicates the crawl
# frontier so we don't scan /resources?sort=date AND /resources?sort=title.
_strip_params = set()        # global params to strip from all URLs
_strip_params_by_path = []   # list of (pattern, param_set) for path-conditional stripping


_strip_path_rules_compiled = []  # compiled (regex, param_set) pairs


def normalize_url(url):
    """Normalize URL for deduplication.

    Strips fragment, trailing slash, and any query parameters listed in
    _strip_params (global) or matching a path-conditional rule from
    _strip_params_by_path.  Configured via strip_query_params in YAML.
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip('/') or '/'

    # Build the set of params to strip for this URL
    query = parsed.query
    if query and (_strip_params or _strip_path_rules_compiled):
        from urllib.parse import parse_qs, urlencode
        strip = set(_strip_params)
        for regex, param_set in _strip_path_rules_compiled:
            if regex.search(path):
                strip.update(param_set)
        if strip:
            params = parse_qs(query, keep_blank_values=True)
            filtered = {k: v for k, v in params.items() if k not in strip}
            query = urlencode(filtered, doseq=True) if filtered else ''

    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, query, ''))


def is_same_origin(url, base_url):
    """Check whether two URLs share the same scheme+host+port."""
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
                robots_parser=None):
    """Decide whether a URL should be scanned based on all filter rules.

    Checks (in order): same-origin, file extension, include/exclude paths,
    exclude regex, query string filters, and robots.txt.
    """
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


_http_cookie_header = ''  # set by crawl_and_scan when auth cookies loaded


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Handler that stops urllib from following redirects."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_no_redirect_opener = urllib.request.build_opener(_NoRedirect)


def http_status(url, timeout=10):
    """Return (status_code, content_type) via a lightweight HEAD request.

    Falls back to GET if the server rejects HEAD (some do).
    Returns (0, '') on network error.  Does NOT follow redirects —
    redirects return the 3xx status so the browser can handle them
    with its own session cookies.

    This is used as a pre-check before loading pages in Chromium.
    It's much cheaper than a full browser load and lets us identify
    error pages (4xx/5xx) and non-HTML responses (application/json)
    without wasting Chromium resources.

    If auth cookies are loaded, they are sent with the request so
    authenticated pages return 200 instead of 302→login.
    """
    def _ct(r):
        """Extract base content-type (without charset)."""
        ct = r.headers.get('Content-Type', '')
        return ct.split(';')[0].strip().lower()

    headers = {'User-Agent': 'axe-spider/1.0'}
    if _http_cookie_header:
        headers['Cookie'] = _http_cookie_header

    try:
        req = urllib.request.Request(url, method='HEAD', headers=headers)
        with _no_redirect_opener.open(req, timeout=timeout) as r:
            return (r.status, _ct(r))
    except urllib.error.HTTPError as e:
        return (e.code, _ct(e))
    except Exception:
        # HEAD failed (connection error, or server rejects HEAD) — try GET
        try:
            req = urllib.request.Request(url, method='GET', headers=headers)
            with _no_redirect_opener.open(req, timeout=timeout) as r:
                return (r.status, _ct(r))
        except urllib.error.HTTPError as e:
            return (e.code, _ct(e))
        except Exception:
            return (0, '')


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
                   verbose=False, quiet=False, config=None,
                   json_path=None, html_path=None, save_every=25,
                   level_label=None, allowlist=None, seed_urls=None,
                   robots_parser=None, resume_state=None):
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
    num_workers = _safe_int(config.get('workers', 1), 1)
    driver_type = config.get('driver', 'selenium').lower()

    # Set up auth cookie header for http_status() pre-checks.
    # Without this, authenticated pages return 302→login to the HEAD
    # request and get skipped before the browser ever loads them.
    global _http_cookie_header
    _auth_cookies = load_cookies(config)
    if _auth_cookies:
        _http_cookie_header = '; '.join(
            '{}={}'.format(c['name'], c['value']) for c in _auth_cookies)

    # For Playwright with multiple workers, we skip creating the sync browser
    # entirely and go straight to async batch mode (sync and async Playwright
    # can't coexist in the same process).
    use_playwright_async = (driver_type == 'playwright' and num_workers > 1)
    if use_playwright_async:
        driver = None  # no sync browser — async sliding window creates its own
    else:
        driver = create_browser(config)
        # Inject auth cookies if configured
        auth_cookies = load_cookies(config)
        if auth_cookies:
            inject_cookies_selenium(driver, auth_cookies, start_url)
            if not quiet:
                print("  Loaded {} auth cookies".format(len(auth_cookies)))
    base_url = start_url

    # Initialize crawl state — either from a saved state file (--resume)
    # or from scratch.
    # URLs that cause session logout — discovered during recovery mode,
    # persisted in state files.
    _logout_urls = set()

    if resume_state:
        queue = deque(resume_state['queue'])
        visited = set(resume_state['visited'])
        _logout_urls = set(resume_state.get('logout_urls', []))
        no_crawl = False
        if not quiet:
            print("  Resuming: {} queued, {} already visited".format(
                len(queue), len(visited)))
            if _logout_urls:
                print("  {} banned logout URLs".format(len(_logout_urls)))
    elif seed_urls:
        visited = set()
        queue = deque(normalize_url(u) for u in seed_urls)
        no_crawl = True  # Don't follow links when using a URL list
    else:
        visited = set()
        queue = deque([normalize_url(start_url)])
        no_crawl = False
    page_count = 0
    scan_start_time = time.time()
    total_page_time = 0  # accumulated per-page scan times

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
            print('  (flush failed: {})'.format(e))

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

    def _vskip(url, reason):
        """Print a skip notice in verbose mode."""
        if verbose and not quiet:
            with print_lock:
                print("  skip: {} — {}".format(url, reason))

    # Content types that indicate an HTML page worth scanning.
    _HTML_TYPES = {'text/html', 'application/xhtml+xml', ''}

    def _scan_one_page(browser, url):
        """Scan a single page and return (url, page_data, new_links, elapsed) or None."""
        page_timer = time.time()
        status, content_type = http_status(url)

        # Skip error pages (4xx/5xx) — server/Apache error pages,
        # not application templates we can fix.
        if status >= 400:
            _vskip(url, "HTTP {}".format(status))
            return None

        # Skip non-HTML responses (JSON APIs, PDFs, etc.)
        if content_type and content_type not in _HTML_TYPES:
            _vskip(url, "not HTML ({})".format(content_type))
            return None

        # Enforce cross-worker rate limit (from robots.txt crawl-delay)
        rate_limiter.wait()

        try:
            browser.navigate(url)
            # Per-worker settle time for JS frameworks (MathJax, SPAs, etc.)
            time.sleep(page_wait)

            current = browser.current_url

            # Redirected off-origin — skip
            if not is_same_origin(current, base_url):
                _vskip(url, "redirect off-origin → {}".format(current))
                return None

            # Same-origin redirect — use actual URL
            actual_url = normalize_url(current)
            with queue_lock:
                if actual_url != url:
                    if actual_url in visited:
                        _vskip(url, "redirect → {} (already visited)".format(
                            actual_url))
                        return None
                    visited.add(actual_url)
                    if verbose and not quiet:
                        with print_lock:
                            print("  redirect: {} → {}".format(
                                url, actual_url))
                    url = actual_url

            # Skip empty pages
            page_html = (browser.page_source or '')
            if len(page_html) < 100:
                _vskip(url, "empty response ({} bytes)".format(
                    len(page_html)))
                return None

            # Skip non-HTML responses — check both document.contentType
            # (set by Chromium from the response headers) and the DOM.
            # The HEAD pre-check may miss this if the server returns
            # different headers for HEAD vs GET.
            doc_ct = (browser.run_js(
                "return document.contentType;") or '').lower()
            if doc_ct and doc_ct not in _HTML_TYPES:
                _vskip(url, "not HTML ({})".format(doc_ct))
                return None
            page_start = (browser.run_js(
                "return document.documentElement.outerHTML.substring(0, 80);") or '')
            if page_start and '<html' not in page_start.lower():
                _vskip(url, "not HTML")
                return None

            results = run_axe(browser, axe_source, tags, rules)
            if 'error' in results:
                with print_lock:
                    print("  ERROR on {}: {}".format(url, results['error']))
                return None

            # Log non-200 status in verbose mode (page is still scanned)
            if verbose and not quiet and status and status != 200:
                with print_lock:
                    print("  notice: {} — HTTP {}".format(url, status))

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

            elapsed = time.time() - page_timer
            return (url, page_data, new_links, elapsed)

        except Exception as e:
            with print_lock:
                print("  Error on {}: {}, skipping".format(url, str(e)[:100]))
            return None

    # SIGTERM/SIGINT handler: flush partial results and save state.
    interrupted = False

    def _save_state(reason=''):
        """Save crawl state (queue + visited) for --resume.

        Uses write-to-temp + verify + atomic rename to avoid
        corrupting the state file on crash or disk-full.
        """
        if not json_path or no_crawl or not queue:
            return
        state_path = json_path.replace('.json', '.state.json')
        tmp_path = state_path + '.tmp'
        old_path = state_path + '.old'
        try:
            state = {
                'queue': list(queue),
                'visited': sorted(visited),
                'start_url': start_url,
                'pages_scanned': page_count,
                'logout_urls': sorted(_logout_urls),
            }

            # Write to temp file
            with open(tmp_path, 'w') as f:
                json.dump(state, f)

            # Verify: re-read and check key counts match
            with open(tmp_path) as f:
                check = json.load(f)
            if (len(check.get('queue', [])) != len(state['queue'])
                    or len(check.get('visited', [])) != len(state['visited'])):
                raise ValueError(
                    'verification failed: queue {}/{}, visited {}/{}'.format(
                        len(check.get('queue', [])), len(state['queue']),
                        len(check.get('visited', [])), len(state['visited'])))

            # Rotate: current → .old, temp → current
            if os.path.exists(state_path):
                os.replace(state_path, old_path)
            os.replace(tmp_path, state_path)

            # Remove old only after new is safely in place
            try:
                os.unlink(old_path)
            except OSError:
                pass

            if not quiet:
                print("  Crawl state saved: {} ({} queued, {} visited)".format(
                    state_path, len(queue), len(visited)))
        except Exception as e:
            print("  (state save failed: {})".format(e))
            # Clean up temp on failure, leave current intact
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # Signal handler sets the interrupted flag.  Each scan mode checks this
    # flag and breaks out of its loop.  We don't call sys.exit() here because
    # asyncio.run() swallows SystemExit and leaves browsers orphaned.
    def _on_signal(signum, frame):
        nonlocal interrupted
        if interrupted:
            return
        interrupted = True
        print('\n!! Signal {} — flushing {} pages...'.format(signum, page_count))
        _flush(reason='signal {}'.format(signum))
        _save_state()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # SIGUSR1: save state on demand without stopping the scan.
    def _on_usr1(signum, frame):
        print('\n  [SIGUSR1 — saving state snapshot]')
        _flush(reason='snapshot')
        _save_state()
    try:
        signal.signal(signal.SIGUSR1, _on_usr1)
    except (AttributeError, OSError):
        pass  # SIGUSR1 not available on Windows

    # Create browser pool.
    # Selenium: one chromedriver + Chromium per worker (~300MB each).
    # Playwright async mode: handled separately in the crawl loop below.
    is_playwright = (driver_type == 'playwright')
    browsers = [driver] if driver else []
    if not is_playwright and num_workers > 1:
        for _ in range(num_workers - 1):
            browsers.append(create_browser(config))

    # Periodically restart the browser to prevent memory leaks.
    # Chromium accumulates garbage (DOM nodes, JS heaps, image caches) over
    # hundreds of pages, causing slowdowns and occasional 60s+ hangs.
    restart_every = _safe_int(config.get('restart_every', 500), 500)

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
                                   exclude_regex, robots_parser):
                    continue

                page_count += 1
                result = _scan_one_page(driver, url)

                if result is not None:
                    url, page_data, new_links, elapsed = result
                    total_page_time += elapsed
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
                        print("[{}/{}] {} — {} ({:.1f}s)".format(
                            str(page_count).rjust(page_width), max_pages,
                            url, status_str, elapsed))
                        if verbose:
                            print(
                                "  V: {} ({} nodes), I: {} ({} nodes),"
                                " Queue: {}".format(
                                    len(page_data['violations']),
                                    v_count,
                                    len(page_data['incomplete']),
                                    i_count, len(queue)))

                    _write_page(url, page_data)

                    for link in new_links:
                        if link not in visited and link not in queue:
                            queue.append(link)
                else:
                    page_count -= 1

                if json_path and save_every and page_count % save_every == 0:
                    _flush()

                # Restart browser periodically to prevent memory leaks
                if (restart_every and page_count > 0
                        and page_count % restart_every == 0):
                    if not quiet:
                        print("  [restarting browser after {} pages]".format(
                            page_count))
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = create_browser(config)
                    # Re-inject auth cookies after restart
                    auth_cookies = load_cookies(config)
                    if auth_cookies:
                        inject_cookies_selenium(
                            driver, auth_cookies, start_url)

        elif is_playwright and num_workers > 1:
            # --- Playwright parallel: async sliding window ---
            # Instead of batching (which returns all at once), we maintain
            # a sliding window of N concurrent async tasks.  As each task
            # finishes, we print its result immediately and start the next.
            import asyncio

            run_opts = {}
            if rules:
                run_opts['runOnly'] = {'type': 'rule', 'values': rules}
            elif tags:
                run_opts['runOnly'] = {'type': 'tag', 'values': tags}

            async def _pw_sliding_window():
                nonlocal page_count, total_page_time
                from playwright.async_api import async_playwright
                async with async_playwright() as pw:
                    launch_args = [
                        '--disable-dev-shm-usage', '--disable-gpu']
                    chromium_path = config.get('chromium_path')
                    ignore_certs = config.get(
                        'ignore_certificate_errors') in (
                        True, 'true', 'yes', '1')
                    if chromium_path and os.path.isfile(chromium_path):
                        browser = await pw.chromium.launch(
                            headless=True,
                            executable_path=chromium_path,
                            args=launch_args)
                    else:
                        browser = await pw.chromium.launch(
                            headless=True, args=launch_args)

                    try:
                        _register_browser_pid(browser.process.pid)
                    except Exception:
                        pass

                    # Authenticate if a login_script plugin is configured.
                    # The plugin is a Python file with an async login(context, config)
                    # function that drives the browser to log in.
                    auth = config.get('auth', {})
                    login_script = auth.get('login_script', '')
                    context = None
                    _login_plugin = None
                    _recovery_mode = asyncio.Event()
                    _recovery_done = asyncio.Event()
                    _suspect_urls = []
                    if login_script:
                        script_path = os.path.expanduser(login_script)
                        if not os.path.isabs(script_path):
                            script_path = os.path.join(SCRIPT_DIR, script_path)
                        if os.path.isfile(script_path):
                            import importlib.util
                            spec = importlib.util.spec_from_file_location(
                                'login_plugin', script_path)
                            _login_plugin = importlib.util.module_from_spec(spec)
                            spec.loader.exec_module(_login_plugin)

                            context = await browser.new_context(
                                viewport={'width': 1280, 'height': 1024},
                                ignore_https_errors=ignore_certs)
                            try:
                                success = await _login_plugin.login(
                                    context, config)
                            except Exception as e:
                                if not quiet:
                                    print("  Login error: {}".format(e))
                                success = False
                            if not success:
                                if not quiet:
                                    print("  Login failed — scanning as anonymous")
                                await context.close()
                                context = None
                        else:
                            if not quiet:
                                print("  Login script not found: {}".format(
                                    script_path))

                    async def _scan(url, worker_id=0,
                                    skip_rate_limit=False):
                        """Scan one URL — mirrors _scan_one_page."""
                        # If in recovery mode, wait for recovery to finish
                        if _recovery_mode.is_set():
                            await _recovery_done.wait()

                        page_t0 = time.time()
                        loop = asyncio.get_event_loop()
                        status, content_type = await loop.run_in_executor(
                            None, http_status, url)
                        if status >= 400:
                            _vskip(url, "HTTP {}".format(status))
                            return None
                        if (content_type
                                and content_type not in _HTML_TYPES):
                            _vskip(url, "not HTML ({})".format(
                                content_type))
                            return None
                        if context:
                            page = await context.new_page()
                        else:
                            page = await browser.new_page(
                                viewport={'width': 1280, 'height': 1024},
                                ignore_https_errors=ignore_certs)
                        try:
                            # Rate limit: async sleep so we don't block
                            # the event loop.  Skipped on initial
                            # staggered starts (stagger already spaces
                            # the requests correctly).
                            if not skip_rate_limit:
                                delay = rate_limiter.wait_time()
                                if delay > 0:
                                    await asyncio.sleep(delay)
                            await page.goto(url, wait_until='load')
                            await page.wait_for_timeout(
                                page_wait * 1000)

                            # Check if session is still active.
                            # If lost, add this URL to suspects and
                            # trigger recovery mode.
                            if (_login_plugin
                                    and hasattr(_login_plugin, 'is_logged_in')
                                    and not await _login_plugin.is_logged_in(page)):
                                _suspect_urls.append(url)
                                visited.discard(url)
                                if not _recovery_mode.is_set():
                                    _recovery_mode.set()
                                    _recovery_done.clear()
                                return None

                            current = page.url
                            if not is_same_origin(current, base_url):
                                _vskip(url, "redirect off-origin → {}".format(
                                    current))
                                return None
                            actual = normalize_url(current)
                            if actual != url:
                                if actual in visited:
                                    _vskip(url, "redirect → {} (already visited)".format(
                                        actual))
                                    return None
                                visited.add(actual)
                                if verbose and not quiet:
                                    print("  redirect: {} → {}".format(
                                        url, actual))

                            content = await page.content()
                            if len(content or '') < 100:
                                _vskip(url, "empty response ({} bytes)".format(
                                    len(content or '')))
                                return None

                            doc_ct = (await page.evaluate(
                                "document.contentType") or '').lower()
                            if doc_ct and doc_ct not in _HTML_TYPES:
                                _vskip(url, "not HTML ({})".format(
                                    doc_ct))
                                return None
                            page_start = await page.evaluate(
                                "document.documentElement.outerHTML"
                                ".substring(0, 80)")
                            if page_start and '<html' not in (
                                    page_start or '').lower():
                                _vskip(url, "not HTML")
                                return None

                            await page.add_script_tag(
                                content=axe_source)
                            results = await page.evaluate(
                                """(opts) => {
                                    return axe.run(document, opts)
                                        .catch(e => (
                                            {error: e.toString()}));
                                }""", run_opts)
                            if not results or 'error' in results:
                                err = (results or {}).get(
                                    'error', 'unknown error')
                                _vskip(url, "axe error: {}".format(err))
                                return None

                            # Log non-200 status (page is still scanned)
                            if (verbose and not quiet
                                    and status and status != 200):
                                print("  notice: {} — HTTP {}".format(
                                    url, status))

                            new_links = []
                            is_ok = (status == 0 or status < 400)
                            if not no_crawl and is_ok:
                                links = await page.evaluate(
                                    "Array.from(document"
                                    ".querySelectorAll('a[href]'))"
                                    ".map(a=>a.href)"
                                    ".filter(h=>"
                                    "h.startsWith('http'))")
                                new_links = [
                                    normalize_url(lnk)
                                    for lnk in (links or []) if lnk]

                            elapsed = time.time() - page_t0
                            return (actual, {
                                'url': actual,
                                'timestamp':
                                    datetime.now().isoformat(),
                                'http_status': (
                                    status if status != 0 else None),
                                'violations':
                                    results.get('violations', []),
                                'incomplete':
                                    results.get('incomplete', []),
                                'passes':
                                    results.get('passes', []),
                                'inapplicable':
                                    results.get('inapplicable', []),
                            }, new_links, worker_id, elapsed)
                        except Exception:
                            return None
                        finally:
                            try:
                                await page.close()
                            except Exception:
                                pass

                    # Merge plugin exclude_paths into the scan filter
                    _all_exclude = list(exclude_paths or [])
                    if (_login_plugin
                            and hasattr(_login_plugin, 'exclude_paths')):
                        _all_exclude.extend(_login_plugin.exclude_paths)

                    def _next_url():
                        """Pull the next scannable URL from the queue."""
                        while queue:
                            url = queue.popleft()
                            if url in visited or url in _logout_urls:
                                continue
                            visited.add(url)
                            if should_scan(
                                    url, base_url, include_paths,
                                    _all_exclude, exclude_regex,
                                    robots_parser):
                                return url
                        return None

                    # Fill initial window with staggered starts.
                    # Worker IDs start at 1 for display.
                    pending = {}
                    next_worker_id = 1
                    # Stagger initial starts by the crawl delay (not
                    # page_wait) so each worker's first request is
                    # spaced at the rate limit interval.
                    stagger = max(crawl_delay, 1) if crawl_delay else (
                        page_wait / max(num_workers, 1))

                    def _make_staggered(u, delay, w):
                        """Factory to avoid closure capture bug."""

                        async def _task():
                            if delay > 0:
                                await asyncio.sleep(delay)
                            return await _scan(
                                u, worker_id=w, skip_rate_limit=True)
                        return _task

                    for i in range(num_workers):
                        url = _next_url()
                        if url is None or page_count >= max_pages:
                            break
                        wid = next_worker_id
                        next_worker_id += 1
                        task = asyncio.create_task(
                            _make_staggered(url, i * stagger, wid)())
                        pending[task] = url

                    # Track which worker IDs are in use.
                    # task_workers maps task -> worker_id
                    task_workers = {}
                    active_wids = set()
                    for i, task in enumerate(pending.keys()):
                        wid = i + 1
                        task_workers[task] = wid
                        active_wids.add(wid)

                    def _free_wid():
                        """Return the lowest available worker ID."""
                        for w in range(1, num_workers + 1):
                            if w not in active_wids:
                                return w
                        return num_workers  # shouldn't happen

                    # Sliding window: as each finishes, print result,
                    # feed discovered links, fill empty worker slots.
                    while pending and not interrupted:
                        done, _ = await asyncio.wait(
                            pending.keys(),
                            return_when=asyncio.FIRST_COMPLETED)

                        # Collect freed worker IDs from completed tasks
                        freed_wids = []
                        for task in done:
                            del pending[task]
                            wid = task_workers.pop(task, 0)
                            active_wids.discard(wid)
                            freed_wids.append(wid)

                            page_count += 1
                            result = None
                            try:
                                result = task.result()
                            except Exception:
                                pass

                            if result is not None:
                                url, page_data, new_links, _, elapsed = result
                                total_page_time += elapsed
                                v_count = _count_nodes(
                                    page_data.get('violations', []))
                                i_count = _count_nodes(
                                    page_data.get('incomplete', []))
                                if not quiet:
                                    pw_w = len(str(max_pages))
                                    parts = []
                                    if v_count:
                                        parts.append(
                                            '{} violations'.format(
                                                v_count))
                                    if i_count:
                                        parts.append(
                                            '{} incomplete'.format(
                                                i_count))
                                    ss = (', '.join(parts)
                                          if parts else 'clean')
                                    print("[{}/{}] W{} {} — {} ({:.1f}s)".format(
                                        str(page_count).rjust(pw_w),
                                        max_pages, wid, url, ss,
                                        elapsed))
                                    if verbose:
                                        print(
                                            "  V: {} ({} nodes), I: {} ({} nodes),"
                                            " Queue: {}".format(
                                                len(page_data.get('violations', [])),
                                                v_count,
                                                len(page_data.get('incomplete', [])),
                                                i_count, len(queue)))
                                _write_page(url, page_data)

                                for link in new_links:
                                    if (link not in visited
                                            and link not in queue):
                                        queue.append(link)
                            else:
                                page_count -= 1

                        # Recovery mode: drain all workers, re-login,
                        # test suspect URLs serially, then resume.
                        if _recovery_mode.is_set() and _login_plugin and context:
                            # Drain remaining in-flight tasks
                            while pending:
                                d2, _ = await asyncio.wait(
                                    pending.keys(),
                                    return_when=asyncio.FIRST_COMPLETED)
                                for t2 in d2:
                                    del pending[t2]
                                    task_workers.pop(t2, None)
                                    try:
                                        r2 = t2.result()
                                    except Exception:
                                        r2 = None
                                    if r2 is not None:
                                        page_count += 1
                                        u2, pd2, nl2, _, el2 = r2
                                        total_page_time += el2
                                        _write_page(u2, pd2)
                                        for lnk in nl2:
                                            if (lnk not in visited
                                                    and lnk not in queue):
                                                queue.append(lnk)
                            active_wids.clear()

                            if not quiet:
                                print("  [recovery: {} suspect URLs, re-logging in]".format(
                                    len(_suspect_urls)))

                            # Re-login
                            try:
                                await _login_plugin.login(context, config)
                            except Exception:
                                pass

                            # Test each suspect URL serially
                            safe_urls = []
                            for surl in list(_suspect_urls):
                                p = await context.new_page()
                                try:
                                    await p.goto(surl, wait_until='load')
                                    await p.wait_for_timeout(2000)
                                    still_ok = await _login_plugin.is_logged_in(p)
                                    if still_ok:
                                        safe_urls.append(surl)
                                    else:
                                        # This URL caused logout
                                        _logout_urls.add(surl)
                                        if not quiet:
                                            print("  [banned: {}]".format(surl))
                                        # Re-login for next test
                                        await p.close()
                                        try:
                                            await _login_plugin.login(
                                                context, config)
                                        except Exception:
                                            pass
                                        continue
                                except Exception:
                                    safe_urls.append(surl)
                                finally:
                                    try:
                                        await p.close()
                                    except Exception:
                                        pass

                            # Requeue safe URLs
                            for surl in safe_urls:
                                if surl not in visited:
                                    queue.appendleft(surl)
                            _suspect_urls.clear()

                            _recovery_mode.clear()
                            _recovery_done.set()

                            if not quiet:
                                print("  [recovery done: {} banned, {} requeued]".format(
                                    len(_logout_urls), len(safe_urls)))

                        # Fill empty slots with freed worker IDs first,
                        # then allocate new ones if needed.
                        while (len(pending) < num_workers
                               and page_count + len(pending) < max_pages):
                            next_url = _next_url()
                            if next_url is None:
                                break
                            if freed_wids:
                                wid = freed_wids.pop(0)
                            else:
                                wid = _free_wid()
                            active_wids.add(wid)
                            t = asyncio.create_task(
                                _scan(next_url, worker_id=wid))
                            pending[t] = next_url
                            task_workers[t] = wid

                        if (json_path and save_every
                                and page_count % save_every == 0):
                            _flush()

                        # Restart browser periodically to prevent memory leaks.
                        # Wait for all in-flight pages to finish first.
                        if (restart_every and page_count > 0
                                and page_count % restart_every == 0
                                and page_count < max_pages):
                            # Drain remaining in-flight tasks
                            while pending:
                                done2, _ = await asyncio.wait(
                                    pending.keys(),
                                    return_when=asyncio.FIRST_COMPLETED)
                                for t2 in done2:
                                    del pending[t2]
                                    task_workers.pop(t2, None)
                                    page_count += 1
                                    try:
                                        r2 = t2.result()
                                    except Exception:
                                        r2 = None
                                    if r2 is not None:
                                        u2, pd2, nl2, _, el2 = r2
                                        total_page_time += el2
                                        _write_page(u2, pd2)
                                        for lnk in nl2:
                                            if (lnk not in visited
                                                    and lnk not in queue):
                                                queue.append(lnk)
                                    else:
                                        page_count -= 1
                            # Restart
                            if not quiet:
                                print("  [restarting browser after"
                                      " {} pages]".format(page_count))
                            try:
                                await browser.close()
                            except Exception:
                                pass
                            launch_kw = {
                                'headless': True, 'args': launch_args}
                            if (chromium_path
                                    and os.path.isfile(chromium_path)):
                                launch_kw['executable_path'] = (
                                    chromium_path)
                            browser = await pw.chromium.launch(
                                **launch_kw)
                            try:
                                _register_browser_pid(
                                    browser.process.pid)
                            except Exception:
                                pass
                            active_wids.clear()

                            # Re-authenticate after browser restart
                            if _login_plugin:
                                context = await browser.new_context(
                                    viewport={'width': 1280,
                                              'height': 1024},
                                    ignore_https_errors=ignore_certs)
                                try:
                                    success = await _login_plugin.login(
                                        context, config)
                                    if success:
                                        # Update cookie header for
                                        # HTTP pre-checks
                                        cookies = (
                                            await context.cookies())
                                        if cookies:
                                            global _http_cookie_header
                                            _http_cookie_header = (
                                                '; '.join(
                                                    '{}={}'.format(
                                                        c['name'],
                                                        c['value'])
                                                    for c in cookies))
                                    elif not quiet:
                                        print("  [re-login failed"
                                              " after restart]")
                                except Exception as e:
                                    if not quiet:
                                        print("  [re-login error: {}]"
                                              .format(e))

                            # Refill the sliding window after restart
                            for i in range(num_workers):
                                if page_count + len(pending) >= max_pages:
                                    break
                                next_url = _next_url()
                                if next_url is None:
                                    break
                                wid = i + 1
                                active_wids.add(wid)
                                t = asyncio.create_task(
                                    _scan(next_url, worker_id=wid))
                                pending[t] = next_url
                                task_workers[t] = wid

                    # Cancel any in-flight tasks (e.g. after ^C) so
                    # Python doesn't dump "Task exception was never
                    # retrieved" tracebacks at shutdown.
                    for task in list(pending.keys()):
                        task.cancel()
                    for task in list(pending.keys()):
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass

                    try:
                        await browser.close()
                    except Exception:
                        pass

            try:
                asyncio.run(_pw_sliding_window())
            except (KeyboardInterrupt, SystemExit):
                _cleanup_browsers()
            except Exception as e:
                print("  Playwright error: {}".format(e), file=sys.stderr)
                _cleanup_browsers()

        else:
            # --- Selenium parallel: thread pool with separate browsers ---
            with ThreadPoolExecutor(max_workers=num_workers) as pool:
                while queue and page_count < max_pages and not interrupted:
                    futures = {}
                    while queue and len(futures) < num_workers and \
                            page_count + len(futures) < max_pages:
                        url = queue.popleft()
                        if url in visited:
                            continue
                        visited.add(url)
                        if not should_scan(url, base_url, include_paths,
                                           exclude_paths, exclude_regex,
                                           robots_parser=robots_parser):
                            continue
                        browser_idx = len(futures) % num_workers
                        # Stagger starts so results stream evenly
                        stagger = page_wait / max(num_workers, 1)
                        if len(futures) > 0:
                            time.sleep(stagger)
                        future = pool.submit(
                            _scan_one_page, browsers[browser_idx], url)
                        futures[future] = url

                    if not futures:
                        break

                    for future in as_completed(futures):
                        page_count += 1
                        result = future.result()

                        if result is not None:
                            url, page_data, new_links, elapsed = result
                            total_page_time += elapsed
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
                                    print("[{}/{}] {} — {} ({:.1f}s)".format(
                                        str(page_count).rjust(pw),
                                        max_pages, url, ss, elapsed))
                                    if verbose:
                                        print(
                                            "  V: {} ({} nodes), I: {} ({} nodes),"
                                            " Queue: {}".format(
                                                len(page_data.get('violations', [])),
                                                v_count,
                                                len(page_data.get('incomplete', [])),
                                                i_count, len(queue)))

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

                    # Restart all browsers periodically to prevent
                    # memory leaks.  Safe here because all futures from
                    # the current batch have completed.
                    if (restart_every and page_count > 0
                            and page_count % restart_every == 0
                            and page_count < max_pages):
                        if not quiet:
                            print("  [restarting {} browsers after"
                                  " {} pages]".format(
                                      len(browsers), page_count))
                        for i, b in enumerate(browsers):
                            try:
                                b.quit()
                            except Exception:
                                pass
                            browsers[i] = create_browser(config)

    finally:
        for browser in browsers:
            try:
                browser.quit()
            except Exception:
                pass
        _flush(reason='final')
        _save_state()

    wall_time = time.time() - scan_start_time
    return page_count, jsonl_path, wall_time, total_page_time


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
    parser.add_argument('--name', '--output', default=None, dest='output',
                        help='Job name used as the basename for all output files '
                             '(default: axe-spider-YYYY-MM-DD-HHMMSS)')
    parser.add_argument('--output-dir', default=None,
                        help='Output directory (default: from config or current directory)')
    parser.add_argument('--allowlist', default=None,
                        help='YAML file of known-acceptable incompletes to suppress')
    parser.add_argument('--driver', default=None, choices=['selenium', 'playwright'],
                        help='Browser driver: selenium (default) or playwright (~2x faster)')
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
    parser.add_argument('--resume', default=None, metavar='STATE.json',
                        help='Resume a previous crawl from its saved state file')
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
    parser.add_argument('--cleanup', action='store_true',
                        help='Kill orphaned chromium/chromedriver processes from previous runs and exit')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Show detailed rule/node counts for pages with issues')

    args = parser.parse_args()

    config = load_config(args.config)

    if args.cleanup:
        # Kill any orphaned chromium/chromedriver processes owned by this user
        killed = 0
        try:
            result = subprocess.run(
                ['ps', '-u', str(os.getuid()), '-o', 'pid,comm'],
                capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        pid = int(parts[0])
                        comm = parts[1]
                        if pid != os.getpid() and (
                                'chrome' in comm
                                or 'chrome' in comm):
                            os.kill(pid, signal.SIGKILL)
                            killed += 1
                    except (ValueError, ProcessLookupError, PermissionError):
                        pass
        except Exception:
            pass
        print("Killed {} orphaned browser process(es).".format(killed))
        sys.exit(0)

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

    # Query parameters to strip from URLs during normalization.
    # This deduplicates sort/filter/pagination variants of the same page.
    # Entries can be plain strings (global) or dicts with path + params
    # for path-conditional stripping.
    global _strip_params, _strip_path_rules_compiled
    strip_list = config.get('strip_query_params', [])
    if isinstance(strip_list, str):
        strip_list = [s.strip() for s in strip_list.split(',')]
    _strip_params = set()
    _strip_path_rules_compiled = []
    for entry in strip_list:
        if isinstance(entry, str):
            _strip_params.add(entry)
        elif isinstance(entry, dict) and 'path' in entry:
            params = entry.get('querystring', entry.get('params', []))
            if isinstance(params, str):
                params = [p.strip() for p in params.split(',')]
            try:
                _strip_path_rules_compiled.append(
                    (re.compile(entry['path']), set(params)))
            except re.error as e:
                print("WARNING: invalid strip_query_params path "
                      "regex '{}': {}".format(entry['path'], e),
                      file=sys.stderr)

    # Resolve output
    save_every = args.save_every or _safe_int(config.get('save_every', 25), 25)

    # Workers: number of parallel browser instances
    # CLI overrides for config
    if args.driver:
        config['driver'] = args.driver
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

    # Load saved crawl state for --resume
    resume_state = None
    if args.resume:
        try:
            with open(args.resume) as f:
                resume_state = json.load(f)
            if not args.quiet:
                print("Resuming from: {}".format(args.resume))
        except Exception as e:
            print("ERROR: cannot load state file: {}".format(e),
                  file=sys.stderr)
            sys.exit(2)

    scanned, jsonl_path, wall_time, total_page_time = crawl_and_scan(
        url,
        max_pages=max_pages,
        tags=tags,
        rules=args.rule,
        level=args.level,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        exclude_regex=exclude_regex,
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
        resume_state=resume_state,
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

    throughput = (wall_time / scanned) if scanned else 0
    print("\nScan complete: {} pages in {:.1f}s ({:.1f}s/page)".format(
        scanned, wall_time, throughput))
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
