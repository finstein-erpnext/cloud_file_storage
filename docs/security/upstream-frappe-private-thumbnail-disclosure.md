# Upstream security report — frappe: private file thumbnails are served to unauthenticated callers

**Status:** ready to file. Prepared by the Cloud File Storage P3 gate; not a Cloud File
Storage vulnerability. Recorded in `docs/DECISIONS.md` (2026-08-15, backlog escalation).

**Project:** frappe/frappe
**Affects:** version-15 as verified here (`v15.16.0` … `v15.93.0`); the write path is
unchanged across that range, so almost certainly older and newer branches too.
**Severity:** unauthenticated disclosure of private file content, no authentication and no
user interaction required. Content is limited to the *thumbnail rendering* of a private
image, not the original bytes.
**Reporter's note:** the second half of the defect (§2) is provable from frappe's source
alone and does not depend on any deployment assumption. That is deliberate — it makes the
report actionable even for a maintainer who disputes the nginx premise in §1.

---

## Summary

`File.make_thumbnail` writes every thumbnail into the **public** site tree, including the
thumbnails of **private** files, at a path that bench's own nginx configuration serves
directly off disk. A `GET /private/files/<name>_small.<ext>` therefore returns a rendering
of a private file to an unauthenticated caller, without any frappe process running, without
a permission check and without an access-log entry.

The same inconsistency has a second, deployment-independent consequence: `delete_file` looks
for that thumbnail somewhere else, so it is orphaned on disk when the File is deleted.

## 1. Disclosure

**Where core writes it** — `frappe/core/doctype/file/file.py:463`:

```python
thumbnail_url = f"{filename}_{suffix}.{extn}"
path = os.path.abspath(frappe.get_site_path("public", thumbnail_url.lstrip("/")))
```

For a private file, `file_url` is `/private/files/<name>.<ext>`, so `thumbnail_url` is
`/private/files/<name>_small.<ext>` and the destination is:

```
sites/<site>/public/private/files/<name>_small.<ext>
```

`get_local_image` (`frappe/core/doctype/file/utils.py:86-94`) accepts `/private/...` URLs, so
this branch is genuinely reached for private files — it is not dead code.

**Where nginx serves it from** — bench's template,
`bench/config/templates/nginx.conf` (line 91 in the version checked):

```nginx
location / {
    ...
    try_files /{{ site_name }}/public/$uri @webserver;
}
```

A request for `/private/files/<name>_small.<ext>` matches `location /`, and `try_files`
resolves it to `sites/<site>/public/private/files/<name>_small.<ext>`, which exists. nginx
returns it and never reaches `@webserver`, so `download_private_file` — the function that
holds the permission gate — is never called.

The template's other relevant block does **not** cover this:

```nginx
location ~ ^/protected/(.*) {
    internal;
    try_files /{{ site_name }}/$1 =404;
}
```

`internal` means it is reachable only via `X-Accel-Redirect`, which is how
`send_private_file` serves an *authorised* download. It does not guard `/private/...`
request URIs.

**Observed.** On a test bench, `sites/<site>/public/private/files/serve-thumb-local_small.png`
was present on disk after a private image was thumbnailed through core's code path.

## 2. Orphaned on delete (independent of §1)

`delete_file` resolves the same URL to a different location —
`frappe/utils/file_manager.py:317-322`:

```python
parts = os.path.split(path.strip("/"))
if parts[0] == "files":
    path = frappe.utils.get_site_path("public", "files", parts[-1])
else:
    path = frappe.utils.get_site_path("private", "files", parts[-1])
```

For `/private/files/<name>_small.<ext>`, `parts[0]` is `private/files`, so the delete looks
in `private/files/` — where `make_thumbnail` never wrote it. Deleting the File therefore
leaves the thumbnail on disk, in the public tree, indefinitely. This is provable from
frappe's source alone: **write** and **delete** disagree about where a private thumbnail
lives, and the write side is the one in the public tree.

## 3. Reproduction

1. Any frappe v15 site behind the standard bench nginx configuration.
2. Attach a private image (`is_private = 1`) to a document, so `make_thumbnail` runs.
3. Note `thumbnail_url` on the File row: `/private/files/<name>_small.<ext>`.
4. Confirm the file is in the public tree:
   `ls sites/<site>/public/private/files/`.
5. From an unauthenticated client (no cookies, no token):
   `curl -i https://<site>/private/files/<name>_small.<ext>` → **200** with the image.
6. Delete the File row; the file in step 4 is still there.

## 4. Suggested fix

Make `make_thumbnail` write through the same convention `delete_file` reads back:

```python
# frappe/core/doctype/file/file.py, in make_thumbnail
if thumbnail_url.startswith("/private/"):
    path = os.path.abspath(frappe.get_files_path(os.path.basename(thumbnail_url), is_private=1))
else:
    path = os.path.abspath(frappe.get_site_path("public", thumbnail_url.lstrip("/")))
```

That is one behaviour change with two effects: the thumbnail leaves the public tree, and
`delete_file` starts finding it. `download_private_file` already serves
`sites/<site>/private/files/...` through `send_private_file`, so an authorised request keeps
working; an unauthorised one now gets the same `Forbidden` the original file gets.

Two matters for the maintainers to decide, which is why this is a report and not a PR:

* **Existing sites.** A migration would have to move files already sitting in
  `public/private/files/`. Sites that never fix this keep the exposure.
* **Whether private files should be thumbnailed at all**, given that
  `download_private_file` has no path that serves a thumbnail URL (there is no File row
  whose `file_url` is the thumbnail's URL), so the fix leaves private thumbnails
  unreachable in stock frappe rather than merely unexposed.

A hardening measure operators can apply immediately, independent of any code change, is an
explicit nginx guard above `location /`:

```nginx
location ^~ /private/ { internal; }
```

## 5. What Cloud File Storage does about it

Reported here for completeness, and because it shows the fix works.

* Files this app manages are already fixed: a private thumbnail is written to
  `private/files/`, a privacy flip moves an existing one out of the public tree (moved,
  never deleted), and the private serving path serves it behind core's own permission gate.
* Files it does **not** manage are left alone deliberately: relocating an operator's files
  is not an app's call, and one of this app's modes is contractually byte-identical to core.
* Instead it **detects and warns**: `cloud_file_storage.health.detect_public_private_residue`
  reports every file under `sites/<site>/public/private/` on `bench migrate` and in the
  settings health panel, with remediation, and moves nothing.
* Operator-facing writeup: `docs/runbooks/deployment.md`, section
  "SECURITY — private files served off disk from the PUBLIC site tree".

## 6. Disclosure handling

To be filed through frappe's security process (`security@frappe.io` / the project's
published policy) rather than as a public issue, because §1 is remotely exploitable against
deployed sites with no authentication. The suggested patch and the operator-side nginx guard
above may be shared publicly once maintainers have had a chance to respond.
