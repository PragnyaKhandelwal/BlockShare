"""Verified previews with one-chunk sniffing and bounded streaming."""
from pathlib import PurePath

from . import services
from storage.controller import StorageError

MAX_PREVIEW_BYTES = 20 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".log", ".py", ".js", ".css", ".yaml", ".yml"}
UNAVAILABLE = "Preview is unavailable for this format. Download the file to open it."


def _media_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image", "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image", "image/webp"
    if data.startswith(b"%PDF-"):
        return "pdf", "application/pdf"
    return None, None


def _open(user, file, number):
    # A one-chunk window prevents the type probe from prefetching the whole file.
    return services.open_download(user, file, number, log_transfer=False, window=1)


def load_preview(user, file, number):
    """Page view: fetch only the first media chunk; buffer only small text."""
    version, stream, _ = _open(user, file, number)
    try:
        if version.size > MAX_PREVIEW_BYTES:
            return version, None, None, "Preview is limited to 20 MiB. Download this file to open it."
        first = next(stream, b"")
        kind, mime = _media_type(first)
        if kind:
            return version, kind, mime, None
        if PurePath(file.name).suffix.lower() not in TEXT_EXTENSIONS:
            return version, None, None, UNAVAILABLE
        if version.size > MAX_TEXT_BYTES or len(first) > MAX_TEXT_BYTES:
            return version, None, None, "Text preview is limited to 1 MiB. Download this file to open it."
        data = bytearray(first)
        for chunk in stream:
            if len(data) + len(chunk) > MAX_TEXT_BYTES:
                return version, None, None, "Text preview is limited to 1 MiB. Download this file to open it."
            data.extend(chunk)
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return version, None, None, "This text file is not UTF-8. Download it to open it."
        if "\x00" not in text:
            return version, "text", "text/plain", text
        return version, None, None, UNAVAILABLE
    except StorageError as exc:
        raise services.ServiceError(str(exc)) from exc
    finally:
        stream.close()


def open_preview_content(user, file, number):
    """Validate the first chunk and return a closeable, bounded media iterator."""
    version, source, _ = _open(user, file, number)
    try:
        if version.size > MAX_PREVIEW_BYTES:
            source.close()
            return version, None, None
        first = next(source, b"")
        _, mime = _media_type(first)
        if not mime:
            source.close()
            return version, None, None
    except (StorageError, services.ServiceError) as exc:
        source.close()
        raise services.ServiceError(str(exc)) from exc

    def stream():
        sent = 0
        try:
            chunk = first
            while True:
                sent += len(chunk)
                if sent > MAX_PREVIEW_BYTES:
                    raise services.ServiceError("File exceeds the preview limit.")
                yield chunk
                chunk = next(source, None)
                if chunk is None:
                    break
        finally:
            source.close()

    # HttpResponse registers close() even if the browser disconnects before
    # consuming the first byte. Close the already-started source in that case too.
    class Content:
        def __init__(self):
            self.iterator = stream()

        def __iter__(self):
            return self.iterator

        def close(self):
            self.iterator.close()
            source.close()

    return version, mime, Content()
