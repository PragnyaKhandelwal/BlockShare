"""Small presentation helpers for templates (file-type icons and filters)."""
from django import template

register = template.Library()

# extension -> (icon kind, filter group)
_TYPES = {}
for exts, kind, group in [
    ("pdf", "pdf", "docs"),
    ("doc docx odt rtf", "doc", "docs"),
    ("txt md log", "txt", "docs"),
    ("xls xlsx csv ods tsv", "sheet", "docs"),
    ("ppt pptx odp key", "slide", "docs"),
    ("png jpg jpeg gif svg webp bmp heic ico tif tiff", "img", "images"),
    ("mp4 mov mkv avi webm mp3 wav flac ogg m4a aac", "media", "media"),
    ("zip rar 7z tar gz tgz bz2 xz", "zip", "archives"),
    ("py js ts jsx tsx html css json sol c h cpp java go rs sh yml yaml xml toml ipynb", "code", "code"),
]:
    for ext in exts.split():
        _TYPES[ext] = (kind, group)


def _ext(name):
    name = str(name or "")
    return name.rsplit(".", 1)[-1].lower() if "." in name.strip(".") else ""


@register.filter
def file_ext(name):
    """Short upper-case label for the icon tile, e.g. PDF, ZIP, FILE."""
    ext = _ext(name)
    return ext[:4].upper() if ext else "FILE"


@register.filter
def file_kind(name):
    return _TYPES.get(_ext(name), ("file", "other"))[0]


@register.filter
def file_group(name):
    return _TYPES.get(_ext(name), ("file", "other"))[1]
