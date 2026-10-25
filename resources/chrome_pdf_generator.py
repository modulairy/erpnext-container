"""Per-request Chromium with its namespace sandbox enabled and bounded lifetime."""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time

import frappe


class ChromePDFGenerator:
    def __init__(self):
        self._browsers = []
        self._devtools_url = None
        self._chromium_process = None
        self._timer = None
        self._work = None
        if os.getuid() == 0:
            raise RuntimeError('PDF rendering requires a non-root user')
        if frappe.conf.get('chromium_websocket_url'):
            raise RuntimeError('External Chromium endpoints are not permitted')
        if frappe.conf.developer_mode and frappe.form_dict.get('pdf_debug'):
            raise RuntimeError('Interactive PDF debugging is disabled in the container')
        root = Path('/tmp/frappe-pdf')
        root.mkdir(mode=0o700, exist_ok=True)
        if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
            raise RuntimeError('Unsafe PDF temporary directory')
        self._work = tempfile.TemporaryDirectory(prefix='job-', dir=root)
        work = Path(self._work.name)
        (work/'cache').mkdir(mode=0o700)
        self._log = (work/'browser.log').open('w+')
        try:
            self._chromium_process = subprocess.Popen([
                '/usr/bin/chromium-headless-shell', '--headless', '--disable-gpu',
                '--disable-dev-shm-usage', '--no-first-run', '--disable-background-networking',
                '--disable-component-update', '--disable-extensions', '--no-startup-window',
                '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
                '--user-data-dir='+str(work/'profile')],
                stdin=subprocess.DEVNULL, stdout=self._log, stderr=self._log,
                start_new_session=True, env=dict(os.environ, XDG_CACHE_HOME=str(work/'cache')))
            self._timer = threading.Timer(90, self._terminate)
            self._timer.daemon = True
            self._timer.start()
            active = work/'profile/DevToolsActivePort'
            deadline = time.monotonic()+15
            while time.monotonic() < deadline:
                if self._chromium_process.poll() is not None:
                    raise RuntimeError('Sandboxed Chromium failed to start; check the documented seccomp and user namespace requirements')
                if active.exists():
                    lines = active.read_text().splitlines()
                    if len(lines) == 2 and lines[0].isdigit() and lines[1].startswith('/devtools/browser/'):
                        self._devtools_url = 'ws://127.0.0.1:'+lines[0]+lines[1]
                        break
                time.sleep(0.05)
            if not self._devtools_url:
                raise TimeoutError('Chromium startup exceeded 15 seconds')
        except BaseException:
            self._close_browser()
            raise

    def add_browser(self, browser):
        self._browsers.append(browser)

    def remove_browser(self, browser):
        if browser in self._browsers:
            self._browsers.remove(browser)

    def _set_devtools_url(self):
        if not self._devtools_url:
            raise RuntimeError('Chromium is not running')

    def _terminate(self):
        process = self._chromium_process
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)
            # The parent can exit while profile-writing children are still alive.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _close_browser(self):
        if self._timer:
            self._timer.cancel()
        try:
            self._terminate()
        finally:
            if getattr(self, '_log', None):
                self._log.close()
            if self._work:
                for attempt in range(10):
                    try:
                        self._work.cleanup()
                        break
                    except OSError:
                        if attempt == 9:
                            raise
                        time.sleep(0.1)
            self._chromium_process = None
            self._devtools_url = None
