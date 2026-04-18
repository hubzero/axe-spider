"""Login plugin for HubZero sites.

Called by axe-spider when auth.login_script is set in the config.

Required:
    async def login(context, config) -> bool
        Log in using the given BrowserContext. Return True on success.

Optional:
    async def is_logged_in(page) -> bool
        Called after each page load to verify the session is still
        active. Return False to trigger a re-login.

    exclude_paths -> list[str]
        Additional paths to exclude from scanning (e.g. /logout).
"""

import os

# Paths to exclude — /logout destroys the session and would cause
# a re-login loop.  The is_logged_in check handles unexpected session
# loss (server timeout, etc.) without needing to visit /logout.
exclude_paths = ['/logout']

# Session cookie name — tracked to detect forced logout
_session_cookie_name = None
_session_cookie_value = None


async def login(context, config):
    """Log in to a HubZero site.

    Args:
        context: Playwright BrowserContext (already has viewport set)
        config: dict from axe-spider.yaml

    Returns:
        True if login succeeded, False otherwise
    """
    global _session_cookie_name, _session_cookie_value

    auth = config.get('auth', {})
    cred_path = os.path.expanduser(auth.get('credentials_file', ''))
    if not cred_path or not os.path.isfile(cred_path):
        print("  No credentials file: {}".format(cred_path))
        return False

    # Read credentials — username:password or two lines
    with open(cred_path) as f:
        content = f.read().strip()
    if ':' in content.split('\n')[0]:
        username, password = content.split('\n')[0].split(':', 1)
    else:
        lines = content.split('\n')
        username, password = lines[0].strip(), lines[1].strip()
    username = username.strip()
    password = password.strip()

    base_url = config.get('url', '').rstrip('/')
    login_path = auth.get('login_url', '/login')
    login_url = base_url + login_path

    page = await context.new_page()
    try:
        await page.goto(login_url, wait_until='load')
        await page.wait_for_timeout(2000)

        # HubZero shows "Choose your sign in method" first —
        # click the local login link to reveal the form
        local_link = await page.query_selector('text=Sign in with your')
        if local_link:
            await local_link.click()
            await page.wait_for_timeout(1000)

        # Fill credentials — HubZero uses name="username" and name="passwd"
        username_field = await page.query_selector(
            'input[name="username"], input[name="user"], '
            'input[name="email"], input[type="email"]')
        password_field = await page.query_selector('input[type="password"]')

        if not username_field or not password_field:
            print("  Login failed: form fields not found")
            return False

        await username_field.click()
        await page.keyboard.type(username)
        await password_field.click()
        await page.keyboard.type(password)
        await page.keyboard.press('Enter')
        await page.wait_for_timeout(5000)

        success = '/login' not in page.url
        if success:
            print("  Authenticated as {}".format(username))
            # Remember the session cookie so is_logged_in can detect changes.
            # The HubZero session cookie is httpOnly and not a _ga cookie.
            cookies = await context.cookies()
            for c in cookies:
                if c.get('httpOnly') and not c['name'].startswith('_ga'):
                    _session_cookie_name = c['name']
                    _session_cookie_value = c['value']
                    break
        else:
            print("  Login failed: still on login page")
        return success

    finally:
        await page.close()


async def is_logged_in(page):
    """Check if the session is still active after a page load.

    Checks the session cookie value — if it changed from what we
    got at login, the server invalidated our session (e.g. /logout
    was visited, or the session timed out).

    Returns True if still authenticated, False if session was lost.
    """
    if not _session_cookie_name:
        return True  # no cookie tracking, assume OK

    cookies = await page.context.cookies()
    for c in cookies:
        if c['name'] == _session_cookie_name:
            if c['value'] == _session_cookie_value:
                return True  # same session cookie — still logged in
            else:
                return False  # cookie changed — session was reset
    # Session cookie missing entirely — logged out
    return False
