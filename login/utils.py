"""Shared input-validation helpers used by the views.

There is no forms.py in this project — validation lives inline in the views —
so this module holds the one piece shared by more than one view: checking that
an uploaded file really is an image before it is stored.
"""

from django.core.exceptions import ValidationError

from PIL import Image


def is_valid_image_upload(uploaded_file, max_bytes=5 * 1024 * 1024):
    """Return True only for real, decodable images within the size limit.

    Checks the declared content type, the file size, and finally asks Pillow
    to actually decode the header — a ``.txt`` renamed to ``.png`` (or a
    content-type forged by the client) is still rejected.

    The file position is restored so the caller can still save the upload.
    """
    if uploaded_file is None:
        return True

    content_type = getattr(uploaded_file, "content_type", "") or ""
    if not content_type.startswith("image/"):
        return False

    if getattr(uploaded_file, "size", 0) > max_bytes:
        return False

    try:
        uploaded_file.seek(0)
        Image.open(uploaded_file).verify()  # raises for non-image data
        uploaded_file.seek(0)
    except (ValidationError, OSError, ValueError):
        try:
            uploaded_file.seek(0)
        except (OSError, ValueError):
            pass
        return False
    return True
