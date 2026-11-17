"""Bounded, verified previews; never serve uploaded HTML or SVG inline."""
from pathlib import PurePath

from . import services
from storage.controller import StorageError

MAX_PREVIEW_BYTES = 20 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".log", ".py", ".js", ".css", ".yaml", ".yml"}


def load_preview(user, file, number):
    version, stream, _ = services.open_download(user, file, number)
    if version.size > MAX_PREVIEW_BYTES:
        stream.close()
        return version, None, None, "Preview is limited to 20 MiB. Download this file to open it."
    data = bytearray()
    try:
        for chunk in stream:
            if len(data) + len(chunk) > MAX_PREVIEW_BYTES:
                raise services.ServiceError("File exceeds the preview limit.")
            data.extend(chunk)
    except StorageError as exc:
        raise services.ServiceError(str(exc)) from exc
    finally:
        stream.close()
    data = bytes(data)
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return version, "image", "image/png", data
    if data.startswith(b"\xff\xd8\xff"):
        return version, "image", "image/jpeg", data
    if data.startswith((b"GIF87a", b"GIF89a")):
        return version, "image", "image/gif", data
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return version, "image", "image/webp", data
    if data.startswith(b"%PDF-"):
        return version, "pdf", "application/pdf", data
    if PurePath(file.name).suffix.lower() in TEXT_EXTENSIONS:
        if len(data) > MAX_TEXT_BYTES:
            return version, None, None, "Text preview is limited to 1 MiB. Download this file to open it."
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return version, None, None, "This text file is not UTF-8. Download it to open it."
        if "\x00" not in text:
            return version, "text", "text/plain", text
    return version, None, None, "Preview is unavailable for this format. Download the file to open it."
