"""Background images for the dashboard slideshow.

Images are uploaded from the phone control panel and stored in
``var/backgrounds``. Nothing about them is trusted: the file type is verified
from its magic bytes rather than its name or the browser's declared content
type, names are regenerated rather than sanitised, sizes are capped, and the
number of files is capped. Serving is by exact filename lookup against the
directory listing, so a crafted name cannot escape the directory.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .logging_setup import get_logger, log_event

log = get_logger("backgrounds")

#: Largest single image accepted.
MAX_IMAGE_BYTES = 12 * 1024 * 1024
#: Most images the slideshow will hold.
MAX_IMAGES = 60

#: ``magic bytes -> (extension, mime)``. Only these formats are accepted.
_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
    (b"GIF87a", ".gif", "image/gif"),
    (b"GIF89a", ".gif", "image/gif"),
)

_SAFE_NAME_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-z]+\.(jpg|png|gif|webp)$")


@dataclass
class BackgroundImage:
    name: str
    size_bytes: int
    modified: float

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "url": f"/media/backgrounds/{self.name}",
            "size_kb": round(self.size_bytes / 1024),
            "modified": self.modified,
        }


def detect_image_type(head: bytes) -> tuple[str, str] | None:
    """``(extension, mime)`` for a recognised image, else ``None``."""
    for signature, extension, mime in _SIGNATURES:
        if head.startswith(signature):
            return extension, mime
    # WebP: "RIFF" .... "WEBP"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


class BackgroundService:
    """Stores, lists and removes slideshow images."""

    def __init__(self) -> None:
        self.directory = paths.VAR_DIR / "backgrounds"
        self._ensure_directory()

    def _ensure_directory(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log_event(log, logging.ERROR, "backgrounds.directory_failed", error=str(exc))

    # -- listing ---------------------------------------------------------
    def list_images(self) -> list[BackgroundImage]:
        """Images currently available, oldest first."""
        out: list[BackgroundImage] = []
        try:
            entries = sorted(self.directory.iterdir(), key=lambda p: p.name)
        except OSError:
            return out
        for entry in entries:
            if not entry.is_file() or not _SAFE_NAME_RE.match(entry.name):
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            out.append(
                BackgroundImage(
                    name=entry.name, size_bytes=stat.st_size, modified=stat.st_mtime
                )
            )
        out.sort(key=lambda image: image.modified)
        return out

    def count(self) -> int:
        return len(self.list_images())

    def resolve(self, name: str) -> Path | None:
        """Path for a served image, or ``None`` if the name is not one of ours.

        Matching is against the real directory listing, so no amount of
        ``../`` or unicode trickery in ``name`` can reach another file.
        """
        if not name or not _SAFE_NAME_RE.match(name):
            return None
        for image in self.list_images():
            if image.name == name:
                candidate = self.directory / image.name
                try:
                    resolved = candidate.resolve(strict=True)
                    if resolved.parent == self.directory.resolve():
                        return resolved
                except OSError:
                    return None
        return None

    # -- writing ---------------------------------------------------------
    def save(self, stream, *, declared_name: str = "") -> tuple[BackgroundImage | None, str]:
        """Store an uploaded image.

        Returns ``(image, error)``. ``stream`` is any object with ``read``.
        """
        self._ensure_directory()
        if self.count() >= MAX_IMAGES:
            return None, f"There are already {MAX_IMAGES} images. Delete some first."

        head = stream.read(64)
        if not head:
            return None, "That file was empty."
        detected = detect_image_type(head)
        if detected is None:
            return None, "Only JPEG, PNG, GIF and WebP images can be used."
        extension, _mime = detected

        # Write to a temporary file first so a failed or oversized upload never
        # leaves a partial image in the slideshow.
        fd, tmp_name = tempfile.mkstemp(prefix=".upload-", dir=str(self.directory))
        total = len(head)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(head)
                while True:
                    chunk = stream.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES:
                        raise ValueError("too-large")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())

            final_name = f"{secrets.token_hex(4)}-{int(time.time()):x}{extension}"
            final_path = self.directory / final_name
            os.chmod(tmp_name, 0o644)
            os.replace(tmp_name, final_path)
        except ValueError:
            self._discard(tmp_name)
            return None, f"Images must be smaller than {MAX_IMAGE_BYTES // (1024 * 1024)} MB."
        except OSError as exc:
            self._discard(tmp_name)
            log_event(log, logging.ERROR, "backgrounds.save_failed", error=str(exc))
            return None, "The image could not be saved. Is the disk full?"

        log_event(
            log, logging.INFO, "backgrounds.image_added",
            name=final_name, size_kb=round(total / 1024),
            original=(declared_name or "")[:40],
        )
        return BackgroundImage(name=final_name, size_bytes=total, modified=time.time()), ""

    @staticmethod
    def _discard(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    def delete(self, name: str) -> bool:
        target = self.resolve(name)
        if target is None:
            return False
        try:
            target.unlink()
        except OSError as exc:
            log_event(log, logging.WARNING, "backgrounds.delete_failed", error=str(exc))
            return False
        log_event(log, logging.INFO, "backgrounds.image_removed", name=name)
        return True

    def delete_all(self) -> int:
        removed = 0
        for image in self.list_images():
            if self.delete(image.name):
                removed += 1
        return removed

    # -- for the UI ------------------------------------------------------
    def payload(self) -> dict[str, object]:
        images = self.list_images()
        return {
            "images": [image.to_dict() for image in images],
            "count": len(images),
            "max": MAX_IMAGES,
            "max_size_mb": MAX_IMAGE_BYTES // (1024 * 1024),
        }
