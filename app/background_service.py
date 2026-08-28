"""Background images and videos for the dashboard slideshow.

Uploaded from the phone control panel and stored in ``var/backgrounds``.
Nothing about them is trusted: the file type is verified from its magic bytes
rather than its name or the browser's declared content type, names are
regenerated rather than sanitised, sizes are capped, and the number of files is
capped. Serving is by exact filename lookup against the directory listing, so a
crafted name cannot escape the directory.

A video is a slide like any other, with one difference the dashboard cares
about: it is not cut off when the slideshow's timer expires. It plays to its
end and *then* the slideshow moves on, which is the only way a clip makes sense
on a wall. That decision belongs to the player, so everything here does is
label each file ``image`` or ``video`` and let the dashboard act on it.

Magic bytes tell us the container, not the codec. An MP4 holding HEVC is a
valid MP4 that Chromium on a Pi cannot play, so the dashboard treats a video
that fails to start as a slide to skip rather than something to get stuck on.
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
#: Largest single video accepted. Generous, because a wall clip is usually a
#: minute of 1080p, but not unlimited: this is a Raspberry Pi's SD card.
MAX_VIDEO_BYTES = 200 * 1024 * 1024
#: Most files the slideshow will hold, images and videos together.
MAX_IMAGES = 60

#: ``magic bytes -> (extension, mime)``. Only these formats are accepted.
_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
    (b"GIF87a", ".gif", "image/gif"),
    (b"GIF89a", ".gif", "image/gif"),
)

#: MP4 and friends declare themselves with a brand at bytes 8-12, after the
#: ``ftyp`` box type. Only the brands Chromium actually plays are listed.
_MP4_BRANDS = (
    b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42",
    b"avc1", b"M4V ", b"dash", b"mmp4",
)
#: QuickTime — an iPhone recording. Often H.264, sometimes HEVC; the dashboard
#: skips the ones the browser cannot decode.
_MOV_BRANDS = (b"qt  ",)

_SAFE_NAME_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-z]+\.(jpg|png|gif|webp|mp4|webm|mov)$"
)

IMAGE = "image"
VIDEO = "video"


@dataclass
class BackgroundMedia:
    """One slide: an image, or a video that plays to its end."""

    name: str
    size_bytes: int
    modified: float
    kind: str = IMAGE
    mime: str = ""

    @property
    def is_video(self) -> bool:
        return self.kind == VIDEO

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "url": f"/media/backgrounds/{self.name}",
            "size_kb": round(self.size_bytes / 1024),
            "modified": self.modified,
            "kind": self.kind,
            "mime": self.mime or _mime_for(self.name),
        }


#: The old name, kept so nothing that imported it breaks.
BackgroundImage = BackgroundMedia

_MIME_BY_EXTENSION = {
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}


def _mime_for(name: str) -> str:
    return _MIME_BY_EXTENSION.get(Path(name).suffix.lower(), "application/octet-stream")


def kind_for(name: str) -> str:
    return VIDEO if Path(name).suffix.lower() in (".mp4", ".webm", ".mov") else IMAGE


def detect_media_type(head: bytes) -> tuple[str, str, str] | None:
    """``(extension, mime, kind)`` for a recognised file, else ``None``.

    ``head`` must be at least 16 bytes for the video containers to be
    identifiable; the caller reads 64.
    """
    for signature, extension, mime in _SIGNATURES:
        if head.startswith(signature):
            return extension, mime, IMAGE
    # WebP: "RIFF" .... "WEBP"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp", "image/webp", IMAGE
    # Matroska/WebM: the EBML header.
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return ".webm", "video/webm", VIDEO
    # ISO base media (MP4, M4V, MOV): a size field, then "ftyp", then a brand.
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _MOV_BRANDS:
            return ".mov", "video/quicktime", VIDEO
        if brand in _MP4_BRANDS:
            return ".mp4", "video/mp4", VIDEO
    return None


def detect_image_type(head: bytes) -> tuple[str, str] | None:
    """``(extension, mime)`` for a recognised *image*, else ``None``."""
    detected = detect_media_type(head)
    if detected is None or detected[2] != IMAGE:
        return None
    return detected[0], detected[1]


class BackgroundService:
    """Stores, lists and removes slideshow images and videos."""

    def __init__(self) -> None:
        self.directory = paths.VAR_DIR / "backgrounds"
        self._ensure_directory()

    def _ensure_directory(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log_event(log, logging.ERROR, "backgrounds.directory_failed", error=str(exc))

    # -- listing ---------------------------------------------------------
    def list_media(self) -> list[BackgroundMedia]:
        """Every slide — images and videos — oldest first."""
        out: list[BackgroundMedia] = []
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
                BackgroundMedia(
                    name=entry.name,
                    size_bytes=stat.st_size,
                    modified=stat.st_mtime,
                    kind=kind_for(entry.name),
                    mime=_mime_for(entry.name),
                )
            )
        out.sort(key=lambda item: item.modified)
        return out

    def list_images(self) -> list[BackgroundMedia]:
        """Only the still images.

        Kept separate because a still can be painted as a CSS background and a
        video cannot; anything that draws wallpaper directly wants this list.
        """
        return [item for item in self.list_media() if not item.is_video]

    def list_videos(self) -> list[BackgroundMedia]:
        return [item for item in self.list_media() if item.is_video]

    def count(self) -> int:
        return len(self.list_media())

    def resolve(self, name: str) -> Path | None:
        """Path for a served image, or ``None`` if the name is not one of ours.

        Matching is against the real directory listing, so no amount of
        ``../`` or unicode trickery in ``name`` can reach another file.
        """
        if not name or not _SAFE_NAME_RE.match(name):
            return None
        for item in self.list_media():
            if item.name == name:
                candidate = self.directory / item.name
                try:
                    resolved = candidate.resolve(strict=True)
                    if resolved.parent == self.directory.resolve():
                        return resolved
                except OSError:
                    return None
        return None

    # -- writing ---------------------------------------------------------
    def save(self, stream, *, declared_name: str = "") -> tuple[BackgroundMedia | None, str]:
        """Store an uploaded image or video.

        Returns ``(media, error)``. ``stream`` is any object with ``read``.
        """
        self._ensure_directory()
        if self.count() >= MAX_IMAGES:
            return None, f"There are already {MAX_IMAGES} files. Delete some first."

        head = stream.read(64)
        if not head:
            return None, "That file was empty."
        detected = detect_media_type(head)
        if detected is None:
            return None, (
                "Only JPEG, PNG, GIF and WebP images, or MP4, WebM and MOV "
                "videos, can be used."
            )
        extension, mime, kind = detected
        limit = MAX_VIDEO_BYTES if kind == VIDEO else MAX_IMAGE_BYTES

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
                    if total > limit:
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
            noun = "Videos" if kind == VIDEO else "Images"
            return None, f"{noun} must be smaller than {limit // (1024 * 1024)} MB."
        except OSError as exc:
            self._discard(tmp_name)
            log_event(log, logging.ERROR, "backgrounds.save_failed", error=str(exc))
            return None, "The file could not be saved. Is the disk full?"

        log_event(
            log, logging.INFO,
            "backgrounds.video_added" if kind == VIDEO else "backgrounds.image_added",
            name=final_name, size_kb=round(total / 1024),
            original=(declared_name or "")[:40],
        )
        return (
            BackgroundMedia(
                name=final_name,
                size_bytes=total,
                modified=time.time(),
                kind=kind,
                mime=mime,
            ),
            "",
        )

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
        for item in self.list_media():
            if self.delete(item.name):
                removed += 1
        return removed

    # -- for the UI ------------------------------------------------------
    def payload(self) -> dict[str, object]:
        media = self.list_media()
        return {
            # "images" is every slide, videos included: the control panel lists
            # and deletes them all together. The dashboard wants them split, and
            # asks for that separately.
            "images": [item.to_dict() for item in media],
            "media": [item.to_dict() for item in media],
            "count": len(media),
            "videos": sum(1 for item in media if item.is_video),
            "max": MAX_IMAGES,
            "max_size_mb": MAX_IMAGE_BYTES // (1024 * 1024),
            "max_video_mb": MAX_VIDEO_BYTES // (1024 * 1024),
        }
