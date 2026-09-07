"""Serving — how bytes reach a browser once they live in the bucket.

Two paths, both proven against the real frappe request chain in
`tests/test_serving_spike.py` before any of this was written:

* `private.py` — the cloud-aware `download_private_file`, installed by `runtime_patches`
  as a module attribute on `frappe.utils.response` so `app.py`'s call-time lookup reaches
  it, after `validate_auth()` has resolved token clients;
* `public.py` — `PublicFileRenderer`, a `page_renderer` that only ever sees requests nginx
  (or the dev statics middleware) could not satisfy from local disk.

Both resolve the object through the A1 chain across URL-sharing rows (A35), both consult
`Cloud File URL Alias` before giving up (A11), and neither ever stores a signed URL.
"""
