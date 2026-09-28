"""Windows can register .js as text/plain; the app must still serve it as JS."""

import mimetypes

from app.main import app  # noqa: F401  (importing applies the MIME pins)


def test_javascript_extensions_keep_a_js_mime_type():
    # Registry lookups on Windows land in the non-strict map; simulate such a
    # polluted machine and confirm the strict pins still win.
    mimetypes.add_type("text/plain", ".js", strict=False)
    mimetypes.add_type("text/plain", ".mjs", strict=False)
    assert mimetypes.guess_type("bundle.js")[0] == "text/javascript"
    assert mimetypes.guess_type("pdf.worker.mjs")[0] == "text/javascript"
