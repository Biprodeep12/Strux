"""Files and images attached to a chat message.

The UI sends, on any user message, `attachments: [{name, type, data}]` with `data` base64.
- images (png / jpg / webp / gif / bmp) go to the vision model; they are re-encoded as JPEG,
  at most 1280 px on the long side, so a 4K screenshot costs the 3B model seconds, not minutes
- documents are read as text and put into the message itself: plain text, code, markdown, json,
  csv, logs, html, PDF (pypdf) and Word .docx (read straight from its XML, no extra dependency)
Bounded: 12 000 characters per file, 24 000 per request, 3 images per request.
"""
from __future__ import annotations

import base64
import io
import logging
import re
import zipfile

log = logging.getLogger(__name__)

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
TEXT_EXT = {".txt", ".md", ".markdown", ".json", ".csv", ".tsv", ".log", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml",
            ".html", ".htm", ".css", ".js", ".jsx", ".ts", ".tsx", ".py", ".java", ".kt", ".go", ".rs", ".c", ".h", ".cpp",
            ".hpp", ".cs", ".rb", ".php", ".swift", ".sql", ".sh", ".ps1", ".bat", ".env", ".gradle", ".dart", ".vue", ".svelte"}
PER_FILE, TOTAL, MAX_IMAGES, MAX_BYTES = 12_000, 24_000, 3, 15 * 1024 * 1024


def _ext(name: str) -> str:
    m = re.search(r"(\.[A-Za-z0-9]+)$", name or "")
    return m.group(1).lower() if m else ""


def is_image(att: dict) -> bool:
    return (att.get("type") or "").startswith("image/") or _ext(att.get("name", "")) in IMAGE_EXT


def _raw(att: dict) -> bytes:
    data = att.get("data") or ""
    if "," in data[:100] and data.startswith("data:"):
        data = data.split(",", 1)[1]
    raw = base64.b64decode(data, validate=False)
    if len(raw) > MAX_BYTES:
        raise ValueError(f"{att.get('name')} is larger than 15 MB")
    return raw


def image_b64(att: dict, max_side: int = 1280) -> str:
    from PIL import Image
    im = Image.open(io.BytesIO(_raw(att)))
    im.seek(0)                                    # first frame of a GIF
    im = im.convert("RGB")
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def file_text(att: dict) -> str:
    """Readable text of a document attachment (raises ValueError if it can't be read)."""
    name, ext, mime = att.get("name", "file"), _ext(att.get("name", "")), att.get("type") or ""
    raw = _raw(att)
    if ext == ".pdf" or mime == "application/pdf":
        try:
            from pypdf import PdfReader
        except ImportError as e:
            raise ValueError("PDF support needs the pypdf package") from e
        reader = PdfReader(io.BytesIO(raw))
        pages = []
        for i, page in enumerate(reader.pages):
            pages.append(f"[page {i + 1}]\n" + (page.extract_text() or ""))
            if sum(len(p) for p in pages) > PER_FILE:
                break
        text = "\n".join(pages).strip()
        if not text.replace("[page", "").strip():
            raise ValueError(f"{name} has no text layer (a scanned PDF); attach it as an image instead")
        return text
    if ext == ".docx" or "wordprocessingml" in mime:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
        xml = re.sub(r"</w:p>", "\n", xml)
        xml = re.sub(r"<w:tab/>", "\t", xml)
        return re.sub(r"<[^>]+>", "", xml).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()
    if ext in TEXT_EXT or mime.startswith("text/") or mime in ("application/json", "application/xml", "application/x-yaml"):
        for enc in ("utf-8-sig", "utf-16", "cp1252"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
    if b"\x00" not in raw[:4096]:                  # unknown extension but clearly text
        return raw.decode("utf-8", "replace")
    raise ValueError(f"{name}: can't read this file type (attach text, code, PDF, .docx or an image)")


def expand(messages: list[dict]) -> tuple[list[dict], list[str], list[str], list[str]]:
    """-> (messages with document text inlined, image slots {msg, name, b64}, attachment names, problems).

    Documents stay inlined on every turn of the history (the UI resends them), so follow-up questions
    about a file still see it. Images are returned as slots: chat reads each into text (vision model,
    cached) and inlines that into its own message, so "now add these as TODOs" works on the next turn."""
    out, names, problems = [], [], []
    budget = TOTAL
    for m in messages:
        content = m.get("content") or ""
        for att in m.get("attachments") or []:
            if is_image(att):
                continue
            names.append(att.get("name", "file"))
            try:
                text = file_text(att)
            except Exception as e:                # noqa: BLE001 - every failure becomes a note to the user
                problems.append(str(e) if isinstance(e, ValueError) else f"{att.get('name')}: {e}")
                continue
            cut = min(PER_FILE, max(0, budget))
            if cut <= 0:
                problems.append(f"{att.get('name')}: skipped, too much text attached at once")
                continue
            body = text[:cut] + (f"\n[… truncated, {len(text) - cut} more characters]" if len(text) > cut else "")
            budget -= len(body)
            content += f"\n\n[Attached file: {att.get('name')}]\n```\n{body}\n```"
        out.append({"role": m["role"], "content": content})
    images: list[dict] = []                       # {"msg": index in messages, "name", "b64"}, newest last
    for idx, m in enumerate(messages):
        for a in [a for a in (m.get("attachments") or []) if is_image(a)]:
            names.append(a.get("name", "image"))
            try:
                images.append({"msg": idx, "name": a.get("name") or "image", "b64": image_b64(a)})
            except Exception as e:                # noqa: BLE001
                problems.append(f"{a.get('name')}: not a readable image ({e})")
    return out, images[-MAX_IMAGES:], names, problems


# ---- attachments that arrive outside the browser: files dropped on the bar, Ctrl+Alt+V ----------------
def _mime(name: str) -> str:
    import mimetypes
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def from_paths(paths: list[str], limit: int = 6) -> tuple[list[dict], list[str]]:
    """Files on disk -> UI attachments ({name, type, size, data, preview?}). Images are shrunk here already."""
    import os
    out, problems = [], []
    for path in [p for p in paths if p][:limit]:
        name = os.path.basename(path)
        try:
            if os.path.isdir(path):
                problems.append(f"{name}: folders can't be attached")
                continue
            size = os.path.getsize(path)
            if size > MAX_BYTES:
                problems.append(f"{name} is larger than 15 MB")
                continue
            with open(path, "rb") as f:
                raw = f.read()
            att = {"name": name, "type": _mime(name), "size": size, "data": base64.b64encode(raw).decode("ascii")}
            if is_image(att):
                small = image_b64(att)
                att.update(type="image/jpeg", data=small, preview="data:image/jpeg;base64," + small)
            out.append(att)
        except Exception as e:                     # noqa: BLE001
            problems.append(f"{name}: {e}")
    return out, problems


def from_clipboard() -> tuple[list[dict], list[str]]:
    """The Windows clipboard -> attachments: a copied / screenshotted image, or files copied in Explorer."""
    from PIL import Image, ImageGrab
    try:
        clip = ImageGrab.grabclipboard()
    except Exception as e:                         # noqa: BLE001 - clipboard busy / locked by another app
        return [], [f"clipboard unavailable: {e}"]
    if isinstance(clip, Image.Image):
        buf = io.BytesIO()
        clip.convert("RGB").save(buf, "PNG")
        att = {"name": "clipboard.png", "type": "image/png", "size": buf.tell(), "data": base64.b64encode(buf.getvalue()).decode("ascii")}
        small = image_b64(att)
        att.update(type="image/jpeg", data=small, preview="data:image/jpeg;base64," + small)
        return [att], []
    if isinstance(clip, list) and clip:
        return from_paths([str(p) for p in clip])
    return [], ["nothing to attach on the clipboard (copy an image, a screenshot or files first)"]
