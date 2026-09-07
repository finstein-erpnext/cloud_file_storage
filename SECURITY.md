# Security Policy

`cloud_file_storage` holds object-store credentials, serves private attachments behind Frappe's
permission gate, and issues presigned URLs. A defect in any of those is a disclosure of customer
documents, so security reports are handled ahead of everything else in the queue.

---

## Supported versions

| Version line | Frappe | Status | Receives security fixes |
|---|---|---|---|
| `16.x` (`version-16`) | v16 | Current | Yes |
| `15.x` (`version-15`) | v15 | Maintained | Yes |
| `0.2.x` and earlier | v13–v15 | End of life | **No** — published as `frappe_s3_attachment` by ALYF GmbH; report those to that project |

A line is supported for as long as the Frappe major it targets is supported upstream.
`docs/BRANCHING.md` §5 carries the support policy; `docs/supported-versions.md` carries the
version windows, the CI matrix, and the reasoning behind the floors.

Fixes land on the newest supported line first and are backported downwards. Only the tip of a
supported line receives fixes — "we are on 15.4.0" is not a supported configuration for the
purpose of a patch; upgrade within the line.

---

## Do not open a public issue

**Never report a suspected vulnerability through the GitHub issue tracker, a pull request, a
discussion, or any other public channel.** The issue tracker is world-readable, and a
proof-of-concept posted there is an exploit handed to every site running this app before a fix
exists.

## How to report

Email **`<SECURITY_CONTACT>`**.

Include as much of this as you have:

| | |
|---|---|
| **What** | The vulnerability class and the impact — what an attacker gets |
| **Where** | File and line, or the endpoint / DocType / bench command |
| **Version** | App version (`cloud_file_storage/__init__.py` → `__version__`, or `bench version`), frappe version, and the branch if you are on a checkout |
| **Configuration** | Operation mode, whether the bucket is public-fronted, whether the default credential chain or static keys are in use |
| **Reproduction** | Minimal steps. A failing test is ideal |
| **Mitigation** | Any workaround you found |

Redact before you send: **no real credentials, no presigned URLs, no bucket names or ARNs you
consider sensitive, no `site_config.json`, no customer data.** A presigned URL in an email is a
live bearer token for the object it points at. Placeholders are fine — we can reconstruct the
shape from them.

If you need encryption and no key is published alongside this file, say so in a first email
containing no details and one will be arranged.

## What to expect

| Stage | Target |
|---|---|
| Acknowledgement that the report arrived | 3 business days |
| Triage: reproduced or not, severity, whether it is in scope | 10 business days |
| Fix on the supported lines | Severity-dependent; critical issues take priority over all other work |
| Public disclosure | Coordinated with you, normally when the fix ships, and no later than 90 days after the report |

You will be kept updated at each stage, including when the answer is "this is working as
intended" — with the reasoning, so you can push back. If you disagree with a severity
assessment, say so; it is a judgement call and judgement calls are worth arguing.

Credit is given in the release notes unless you ask not to be named. There is no bug bounty.

---

## Scope

**In scope** — anything in this repository:

- Permission bypass on file serving, download, or the presigned-URL endpoints
- Private attachment bytes reachable without the permission gate
- Credential exposure — in logs, error messages, Desk forms, the API, or backups
- Presigned URLs with wrong scope, wrong TTL, or an unpinned `Content-Disposition`
- Path traversal or key-injection in object keys or file paths
- Unsafe deletion: anything that removes bytes without the verify-then-delete sequence
- Privilege escalation through this app's DocTypes, whitelisted methods, hooks, background jobs,
  or bench commands
- Bucket policy, IAM, or lifecycle configuration this app generates that grants more than it should

**Out of scope** — report these to the project that owns them:

- Frappe Framework itself → https://frappe.io/security
- ERPNext → https://erpnext.com/security
- Your S3 provider's own service
- Findings that require an already-compromised System Manager account, or shell access to the
  bench user. Both are trusted for destructive actions by design
- Missing hardening on a site that is misconfigured against the documented deployment
  procedure (`docs/runbooks/deployment.md`)
- Automated scanner output with no demonstrated impact

**Already known, and not a vulnerability in this app:** frappe writes the thumbnails of private
files into the public site tree, where bench's own nginx configuration serves them without a
permission check. This app *detects* the condition; it does not cause it. The full write-up,
including the deployment-independent half of the defect, is
`docs/security/upstream-frappe-private-thumbnail-disclosure.md`. It belongs to frappe/frappe.

---

## Threat model

The app's threat model — assets, actors, trust boundaries, and STRIDE per surface, with each
control naming the file that implements it and the test that fails if it stops holding — is
`docs/security/threat-model.md`. Its last section lists what the model deliberately does **not**
cover. Read it before reporting: it may already name what you found, either as a control you can
test against or as an accepted limitation.

---

## For operators

If you run this app and believe a site has been exposed:

1. Rotate the object-store credentials first (**Cloud Storage Settings**, or the IAM role).
   A presigned URL already issued stays valid until its TTL expires, unless the key that
   signed it is deleted rather than merely replaced — so delete the old key, do not just add
   a new one.
2. `docs/runbooks/` covers deployment, migration, and backup/restore procedures.
3. The `Cloud Storage Audit Log` DocType is the access trail. Preserve it before doing anything
   that might trim it.
