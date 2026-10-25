"""Real Frappe invoice/report exports and Chromium isolation regressions."""
import io
import json
import os
from pathlib import Path
import shutil


def verify_pdf(session, base):
    import frappe
    from pypdf import PdfReader, PdfWriter
    from frappe.utils.pdf import get_pdf
    from frappe.utils.pdf_generator.chrome_pdf_generator import ChromePDFGenerator
    from frappe.utils.pdf_generator.cdp_connection import CDPSocketClient

    folder = Path('/pdf-results')
    folder.mkdir(exist_ok=True)
    assert shutil.which('wkhtmltopdf') is None, 'Legacy PDF binary remains'
    import importlib.util
    assert importlib.util.find_spec('pdfkit') is None, 'Legacy PDF wrapper remains'
    # Verify renderer namespaces and seccomp, not just the absence of a flag.
    generator = ChromePDFGenerator()
    client = CDPSocketClient(generator._devtools_url)
    try:
        client.connect()
        target, error = client.send('Target.createTarget', {'url': 'about:blank'})
        assert not error
        processes, error = client.send('SystemInfo.getProcessInfo')
        assert not error
        renderers = [p for p in processes['processInfo'] if p['type'] == 'renderer']
        assert renderers, processes
        import psutil
        sandbox = []
        for renderer in renderers:
            pid = int(renderer['id'])
            status = dict(line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
            command = psutil.Process(pid).cmdline()
            assert '--no-sandbox' not in command and '--disable-seccomp-filter-sandbox' not in command
            assert int(status['CapEff'].strip(), 16) == 0
            assert status['NoNewPrivs'].strip() == '1'
            assert status['Seccomp'].strip() == '2'
            assert len(status['NSpid'].split()) >= 2, status['NSpid']
            sandbox.append({k: status[k].strip() for k in ('CapEff', 'NoNewPrivs', 'Seccomp', 'NSpid')})
        (folder/'sandbox.json').write_text(json.dumps(sandbox, indent=2))
    finally:
        client.disconnect()
        generator._close_browser()
    print('PASS: Chromium renderer PID namespace, seccomp, no-new-privileges and zero capabilities', flush=True)

    company = 'Container PDF Company'
    if not frappe.db.exists('Company', company):
        from erpnext.setup.setup_wizard.operations.install_fixtures import install
        install('United States')
        frappe.get_doc({'doctype': 'Company', 'company_name': company, 'abbr': 'CPDF',
                        'default_currency': 'USD', 'country': 'United States',
                        'create_chart_of_accounts_based_on': 'Standard Template',
                        'chart_of_accounts': 'Standard'}).insert()
    customer = 'Container PDF Customer'
    if not frappe.db.exists('Customer', customer):
        frappe.get_doc({'doctype': 'Customer', 'customer_name': customer,
                        'customer_type': 'Company', 'customer_group': 'All Customer Groups',
                        'territory': 'All Territories'}).insert()
    income = frappe.db.get_value('Account', {'company': company, 'root_type': 'Income', 'is_group': 0}, 'name')
    receivable = frappe.db.get_value('Account', {'company': company, 'account_type': 'Receivable', 'is_group': 0}, 'name')
    cost_center = frappe.db.get_value('Cost Center', {'company': company, 'is_group': 0}, 'name')
    invoice = frappe.get_doc({'doctype': 'Sales Invoice', 'company': company, 'customer': customer,
        'posting_date': frappe.utils.today(), 'due_date': frappe.utils.today(), 'currency': 'USD',
        'conversion_rate': 1, 'debit_to': receivable, 'items': [
            {'item_name': 'Sandbox PDF item', 'description': 'İstanbul Şişli invoice test',
             'qty': 2, 'rate': 12.5, 'uom': 'Nos', 'income_account': income, 'cost_center': cost_center}
        ]}).insert()
    frappe.db.commit()
    response = session.post(base+'/api/method/frappe.utils.print_format.download_pdf',
        data={'doctype': 'Sales Invoice', 'name': invoice.name, 'format': 'Standard', 'no_letterhead': 1}, timeout=100)
    response.raise_for_status()
    assert response.content.startswith(b'%PDF-'), response.text[:500]
    (folder/'invoice.pdf').write_bytes(response.content)
    reader = PdfReader(io.BytesIO(response.content))
    text = '\n'.join(page.extract_text() for page in reader.pages)
    assert invoice.name in text and 'Sandbox PDF item' in text, text
    print('PASS: saved ERPNext Sales Invoice exported through authenticated Frappe PDF API', flush=True)

    records = frappe.get_all('Sales Invoice', filters={'name': invoice.name}, fields=['name', 'grand_total'])
    html = frappe.render_template('<html><body><h1>Invoice Report</h1>{% for r in rows %}<p>{{ r.name }} {{ r.grand_total }}</p>{% endfor %}</body></html>', {'rows': records})
    response = session.post(base+'/api/method/frappe.utils.print_format.report_to_pdf',
                            data={'html': html, 'orientation': 'Landscape'}, timeout=100)
    response.raise_for_status()
    assert response.content.startswith(b'%PDF-'), response.text[:500]
    (folder/'report.pdf').write_bytes(response.content)
    reader = PdfReader(io.BytesIO(response.content))
    assert 'Invoice Report' in reader.pages[0].extract_text() and invoice.name in reader.pages[0].extract_text()
    assert reader.pages[0].mediabox.width > reader.pages[0].mediabox.height
    print('PASS: authenticated Frappe report PDF endpoint with database invoice data and landscape output', flush=True)

    html = '<html><body><h1>İstanbul Şişli</h1><div style="break-before:page">Second page</div></body></html>'
    pdf = get_pdf(html, options={'password': 'test-only-password'})
    reader = PdfReader(io.BytesIO(pdf))
    assert reader.is_encrypted and reader.decrypt('test-only-password')
    assert len(reader.pages) == 2 and 'İstanbul' in reader.pages[0].extract_text()
    writer = PdfWriter()
    assert get_pdf(html, output=writer) is writer and len(writer.pages) == 2
    html = '''<html><head><style>body {font-family: sans-serif}</style></head><body>
        <div id="header-html">Container letterhead</div>
        <div id="footer-html">Page <span class="page"></span> of <span class="topage"></span></div>
        <p>First body</p><div style="break-before:page">Second body</div></body></html>'''
    pdf = get_pdf(html)
    (folder/'letterhead.pdf').write_bytes(pdf)
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == 2
    for number, page in enumerate(reader.pages, 1):
        text = page.extract_text()
        assert 'Container letterhead' in text and f'Page {number} of 2' in text, text
    wide = '<html><body><div style="width:1800px">Wide report</div></body></html>'
    assert get_pdf(wide, smart_shrinking=True).startswith(b'%PDF-')
    css = Path(frappe.get_site_path('public/files/pdf-test.css'))
    css.write_text('p::after { content: " LOCAL_ASSET_OK"; }')
    try:
        pdf = get_pdf('<html><head><link rel="stylesheet" href="/files/pdf-test.css"></head><body><p>Asset test</p></body></html>')
        assert 'LOCAL_ASSET_OK' in PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
    finally:
        css.unlink()
    # Untrusted HTML cannot execute JS, load local files or fetch network endpoints.
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    requests = []
    class Canary(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'CANARY')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('0.0.0.0', 0), Canary)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        malicious = f'''<html><body><p>Safe content</p>
        <script>document.body.innerHTML='EXECUTED'</script>
        <img src="http://127.0.0.1:{server.server_port}/probe">
        <iframe src="file:///etc/passwd"></iframe>
        <meta name="pdfkit-enable-local-file-access" content="true"></body></html>'''
        reader = PdfReader(io.BytesIO(get_pdf(malicious)))
        text = '\n'.join(p.extract_text() for p in reader.pages)
        assert 'Safe content' in text and 'EXECUTED' not in text and 'root:' not in text
        assert not requests, requests
    finally:
        server.shutdown()
        server.server_close()
    assert not list(Path('/tmp/frappe-pdf').glob('job-*')), 'PDF job directories leaked'
    print('PASS: PDF encryption, append, Turkish text, pagination, script/file/network denial and cleanup', flush=True)


if __name__ == '__main__':
    import frappe
    import requests
    os.chdir('sites')
    frappe.init(site='test.local', sites_path=os.getcwd())
    frappe.connect()
    frappe.set_user('Administrator')
    from frappe.core.doctype.user.user import generate_keys
    keys = generate_keys('Administrator')
    frappe.db.commit()
    session = requests.Session()
    session.headers.update({'Host': 'test.local', 'Authorization': f"token {keys['api_key']}:{keys['api_secret']}"})
    try:
        verify_pdf(session, 'http://frontend:8080')
    finally:
        frappe.destroy()
