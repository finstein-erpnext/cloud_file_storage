"""Create the File -> derived thumbnail object link.

Idempotent: `create_custom_fields` is a no-op when the field is already there, and
`after_install`/`after_migrate` create it too. The patch exists so a site that upgrades
between those hooks running still gets the column — without it, `make_thumbnail` has
nowhere to record which object serves a `/files/*_small.*` URL and every thumbnail 404s
after S3_ONLY.
"""

from cloud_file_storage.install import ensure_cloud_thumbnail_object_field


def execute():
	ensure_cloud_thumbnail_object_field()
