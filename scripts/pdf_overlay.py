"""Guarded source changes for the reviewed Frappe Chromium PDF contract."""
import ast
import hashlib
from pathlib import Path


def replace_function(text, name, replacement):
    tree = ast.parse(text)
    matches = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
    if len(matches) != 1:
        raise ValueError('Unexpected PDF source function: '+name)
    node = matches[0]
    lines = text.splitlines(keepends=True)
    return ''.join(lines[:node.lineno-1])+replacement.rstrip()+'\n'+''.join(lines[node.end_lineno:])


def apply_pdf(root, resources):
    if root.name not in ('frappe', 'erpnext', 'print_designer'):
        return []
    changes = []
    def write(path, text):
        previous = path.read_bytes() if path.exists() else b''
        path.write_text(text)
        changes.append({'file': str(path.relative_to(root)), 'previous_sha256': hashlib.sha256(previous).hexdigest(),
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'action': 'sandboxed Chromium PDF migration'})
    if root.name == 'print_designer':
        path = root/'print_designer/pdf.py'
        text = path.read_text()
        old = 'print_designer/pdf_generator/framework fromats/pdf_header_footer_chrome.html'
        if old not in text:
            raise ValueError('Review changed Print Designer header hook')
        text = text.replace(old, 'templates/print_formats/chrome_pdf_header_footer.html')
        text = text.replace('"wkhtmltopdf"', '"chrome"')
        write(path, text)
        path = root/'print_designer/pdf_generator/pdf.py'
        if 'def before_request()' not in path.read_text():
            raise ValueError('Review changed Print Designer PDF entry points')
        write(path, '''import frappe

def before_request():
    if frappe.request.path in ('/api/method/frappe.utils.print_format.download_pdf', '/printview'):
        frappe.local.form_dict.pdf_generator = 'chrome'

def after_request():
    pass  # Each core renderer closes its own browser in a finally block.

def get_pdf(print_format, html, options, output, pdf_generator=None):
    from frappe.utils.pdf import get_chrome_pdf
    return get_chrome_pdf(print_format, html, options, output, pdf_generator='chrome')
''')
        return changes
    if root.name == 'erpnext':
        path = root/'erpnext/accounts/doctype/bank_statement_import_log/test_bank_statement_import_log.py'
        text = path.read_text()
        if 'import pdfkit' not in text or 'pdfkit.from_string(html, False)' not in text:
            raise ValueError('Review changed ERPNext bank statement PDF fixture')
        write(path, text.replace('import pdfkit', 'from frappe.utils.pdf import get_pdf')
              .replace('pdfkit.from_string(html, False)', 'get_pdf(html)'))
        return changes
    project = root/'pyproject.toml'
    text = project.read_text()
    if '"pdfkit~=1.0.0",' not in text:
        raise ValueError('Review changed upstream pdfkit dependency')
    write(project, text.replace('"pdfkit~=1.0.0",', ''))
    pdf = root/'frappe/utils/pdf.py'
    text = pdf.read_text()
    start, end = text.index('import pdfkit\n'), text.index('from packaging.version import Version')
    text = text[:start]+text[end:]
    start, end = text.index('class FrappePDFKit('), text.index('def pdf_header_html(')
    text = text[:start]+text[end:]
    text = replace_function(text, 'get_pdf', '''def get_pdf(html, options=None, output=None, smart_shrinking=False):
    """Render classic prints and reports through the sandboxed Chromium engine."""
    from pypdf import PdfReader
    options = dict(options or {})
    options['container-smart-shrinking'] = bool(smart_shrinking)
    # Output writers are encrypted by their caller after all documents are appended.
    if output is not None:
        options.pop('password', None)
    html = inline_private_images(scrub_urls(html))
    data = get_chrome_pdf(None, html, options, None, pdf_generator='chrome')
    if output is not None:
        output.append_pages_from_reader(PdfReader(io.BytesIO(data)))
        return output
    return data
''')
    text = replace_function(text, 'is_wkhtmltopdf_valid', '''def is_wkhtmltopdf_valid():
    """Legacy print UI availability probe for this image's default PDF engine."""
    return os.access('/usr/bin/chromium-headless-shell', os.X_OK)
''')
    text = replace_function(text, 'get_chrome_pdf', '''def get_chrome_pdf(print_format, html, options, output, pdf_generator=None):
    from pypdf import PdfReader
    from frappe.utils.pdf_generator.browser import Browser
    from frappe.utils.pdf_generator.chrome_pdf_generator import ChromePDFGenerator
    from frappe.utils.pdf_generator.pdf_merge import PDFTransformer
    if pdf_generator != 'chrome':
        return
    options = dict(options or {})
    if output is not None:
        options.pop('password', None)
    generator = ChromePDFGenerator()
    try:
        browser = Browser(generator, print_format, html, options)
        data = PDFTransformer(browser).transform_pdf()
        if output is not None:
            output.append_pages_from_reader(PdfReader(io.BytesIO(data)))
            return output
        return data
    finally:
        generator._close_browser()
''')
    write(pdf, text)
    template = root/'frappe/templates/print_formats/chrome_pdf_header_footer.html'
    text = template.read_text()
    if '\t\t\tbody {' not in text:
        raise ValueError('Review upstream Chromium header container styling')
    write(template, text.replace('\t\t\tbody {', '\t\t\thtml body, html body > .print-format {'))
    directory = root/'frappe/utils/pdf_generator'
    for name in ('chrome_pdf_generator.py', 'pdf_resources.py'):
        write(directory/name, (resources/name).read_text())
    browser = directory/'browser.py'
    text = browser.read_text()
    old = 'frappe.get_cached_value("Print Format", print_format, "print_designer")'
    if old not in text:
        raise ValueError('Review upstream Browser print format lookup')
    text = text.replace(old, '(bool(print_format) and '+old+')')
    text = text.replace('self.header_height = self.header_page.get_element_height()',
                        'self.header_height = self.header_page.get_element_height()\n\t\t\tif not self.is_print_designer:\n\t\t\t\toptions.setdefault("margin-top", "15mm")')
    text = text.replace('self.footer_height = self.footer_page.get_element_height()',
                        'self.footer_height = self.footer_page.get_element_height()\n\t\t\tif not self.is_print_designer:\n\t\t\t\toptions.setdefault("margin-bottom", "15mm")')
    marker = '\t\tupdated_options["paperWidth"] = convert_uom(options["page-width"], "px", "in", only_number=True)'
    if marker not in text:
        raise ValueError('Review upstream Chromium orientation handling')
    text = text.replace(marker, '''\t\t# Explicit @page dimensions take precedence over CDP's landscape flag.
\t\tif updated_options["landscape"]:
\t\t\tif options["page-width"] < options["page-height"]:
\t\t\t\toptions["page-width"], options["page-height"] = options["page-height"], options["page-width"]
\t\t\tupdated_options["landscape"] = False
'''+marker)
    marker = '\t\t# make copy of options to update them in header, body, footer.'
    if marker not in text:
        raise ValueError('Review upstream Chromium page scaling')
    text = text.replace(marker, '''\t\tif options.get("container-smart-shrinking"):
\t\t\tself.body_page.wait_for_set_content()
\t\t\tmetrics = self.body_page.evaluate("document.documentElement.scrollWidth")
\t\t\twidth = metrics.get("result", {}).get("value", 0)
\t\t\tavailable = (updated_options["paperWidth"] - updated_options["marginLeft"] - updated_options["marginRight"]) * 96
\t\t\tif width > available > 0:
\t\t\t\tupdated_options["scale"] = max(0.1, available / width)

'''+marker)
    text = replace_function(text, '_inject_page_no_script', '\tdef _inject_page_no_script(self, soup):\n\t\tpass\n')
    write(browser, text)
    page = directory/'page.py'
    text = page.read_text()
    text = replace_function(text, 'intercept_request_for_local_resources', '''\tdef intercept_request_for_local_resources(self, url_pattern="*"):
\t\tfrom frappe.utils.pdf_generator.pdf_resources import intercept
\t\tintercept(self)
''')
    text = text.replace('self.session.send("Fetch.enable", {"patterns": [{"urlPattern": url_pattern}]})',
                        'self.send("Fetch.enable", {"patterns": [{"urlPattern": url_pattern}]})')
    text = text.replace('self.session.send(\n\t\t\t\t"Fetch.fulfillRequest",', 'self.send(\n\t\t\t\t"Fetch.fulfillRequest",')
    text = replace_function(text, 'set_content', '''\tdef set_content(self, html, wait_for=None):
\t\tfrom frappe.utils.pdf_generator.pdf_resources import safe_html
\t\tself.intercept_request_for_local_resources()
\t\twait_start = self.wait_for_load(wait_for=wait_for or ["load", "DOMContentLoaded"])
\t\tself.send("Page.setDocumentContent", {"frameId": self._ensure_frame_id(), "html": safe_html(html)})
\t\t# Only the reviewed page-number helper executes; document scripts are blocked by CSP.
\t\tself.evaluate(frappe.read_file(frappe.get_app_path("frappe", "utils", "pdf_generator", "update_page_no.js")))
\t\tdone = False
\t\tdef wait_once():
\t\t\tnonlocal done
\t\t\tif not done:
\t\t\t\twait_start()
\t\t\t\tdone = True
\t\tself.wait_for_set_content = wait_once
''')
    write(page, text)
    connection = directory/'cdp_connection.py'
    text = connection.read_text().replace('await future\n', 'await asyncio.wait_for(future, timeout=30)\n')
    text = text.replace('max_size=None', 'max_size=16*1024*1024, open_timeout=10, close_timeout=3')
    text = text.replace('frappe.log_error(title="Timeout waiting for event", message=f"{frappe.get_traceback()}")',
                        'raise TimeoutError("Chromium event deadline exceeded")')
    write(connection, text)
    tests = root/'frappe/tests/test_pdf.py'
    text = tests.read_text()
    text = replace_function(text, 'test_smart_shrinking_is_opt_in', '''\tdef test_smart_shrinking_is_opt_in(self):
\t\tfrom unittest.mock import patch
\t\twith patch.object(pdfgen, "get_chrome_pdf", return_value=blank_pdf()) as render:
\t\t\tpdfgen.get_pdf(self.html)
\t\t\tself.assertFalse(render.call_args.args[2]["container-smart-shrinking"])
\t\t\tpdfgen.get_pdf(self.html, smart_shrinking=True)
\t\t\tself.assertTrue(render.call_args.args[2]["container-smart-shrinking"])
''')
    write(tests, text)
    return changes
