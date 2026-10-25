"""Serve only permitted site files to Chromium; never send renderer traffic to the network."""
import base64
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlsplit

import frappe
from bs4 import BeautifulSoup


def safe_html(html):
    soup = BeautifulSoup(html, 'html5lib')
    for tag in soup.find_all(['script', 'iframe', 'object', 'embed', 'base']):
        tag.decompose()
    for tag in soup.find_all('meta'):
        if tag.get('http-equiv'):
            tag.decompose()
    for tag in soup.find_all('link'):
        if tag.get('rel') != ['stylesheet']:
            tag.decompose()
    for tag in soup.find_all(True):
        for name in list(tag.attrs):
            if name.lower().startswith('on'):
                del tag.attrs[name]
    policy = soup.new_tag('meta')
    policy['http-equiv'] = 'Content-Security-Policy'
    policy['content'] = "default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self' data:; script-src 'none'; frame-src 'none'; connect-src 'none'; base-uri 'none'; form-action 'none'"
    soup.head.insert(0, policy)
    return str(soup)


def local_resource(url):
    from frappe.utils.pdf import get_host_url
    from frappe.core.doctype.file.utils import find_file_by_url
    parsed, origin = urlsplit(url), urlsplit(get_host_url())
    if parsed.scheme not in ('http', 'https') or (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
        return None
    path = unquote(parsed.path).lstrip('/')
    if '\\' in path or '..' in Path(path).parts:
        return None
    if not path:
        return b'', 'text/html'
    bench = Path(frappe.utils.get_bench_path()).resolve()
    if path.startswith('assets/'):
        target = (bench/'sites'/path).resolve()
        roots = [(bench/'assets').resolve()]
        # Frappe asset symlinks point into each application's public directory.
        roots += [(bench/'apps'/app/app/'public').resolve() for app in frappe.get_installed_apps()]
    elif path.startswith('private/files/'):
        if frappe.session.user == 'Guest':
            return None
        file = find_file_by_url('/'+path)
        if not file or not file.has_permission('read'):
            return None
        roots = [Path(frappe.get_site_path('private/files')).resolve()]
        target = (roots[0]/path.removeprefix('private/files/')).resolve()
    else:
        roots = [Path(frappe.get_site_path('public')).resolve()]
        target = (roots[0]/path).resolve()
    allowed = {'.css', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.ico', '.woff', '.woff2', '.ttf', '.otf'}
    if target.suffix.lower() not in allowed or not any(target.is_relative_to(root) for root in roots):
        return None
    if not target.is_file() or target.stat().st_size > 10*1024*1024:
        return None
    return target.read_bytes(), mimetypes.guess_type(target.name)[0] or 'application/octet-stream'


def intercept(page):
    def paused(future, response):
        params = response.get('params', {})
        request_id = params.get('requestId')
        if not request_id:
            return
        try:
            resource = local_resource(params['request']['url'])
        except Exception:
            resource = None
        if resource is None:
            method, values = 'Fetch.failRequest', {'requestId': request_id, 'errorReason': 'AccessDenied'}
        else:
            content, mime = resource
            method, values = 'Fetch.fulfillRequest', {
                'requestId': request_id, 'responseCode': 200,
                'responseHeaders': [{'name': 'Content-Type', 'value': mime}],
                'body': base64.b64encode(content).decode()}
        page.send(method, values, return_future=True)
    page.session.start_listener('Fetch.requestPaused', paused, page.session_id, page.target_id, None)
    page.send('Fetch.enable', {'patterns': [{'urlPattern': '*'}]})
