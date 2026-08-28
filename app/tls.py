"""The self-signed certificate that browser screen sharing needs.

Browsers only hand a page the screen (``getDisplayMedia``) in a *secure
context*, and a private address like ``192.168.1.42`` does not count as one no
matter how private the network is. So the sender page has to be served over
HTTPS, and a room appliance on somebody's LAN cannot obtain a publicly trusted
certificate for an address like that from anyone.

The honest answer is a locally generated certificate and one click through the
browser's warning the first time a laptop is used in the room. Everything here
exists to make that one click as small as possible:

* the certificate names the room's own addresses, so the warning is the single
  familiar "not issued by a known authority" one rather than a name mismatch
  as well;
* it is regenerated when the Pi's address changes, so the warning does not
  quietly become a second, different warning after a DHCP lease moves;
* it is renewed before it expires, so a room does not stop working one morning
  two years from now.

``openssl`` does the work: it is present on every Raspberry Pi OS image, and it
keeps the appliance's Python dependencies as they were.
"""

from __future__ import annotations

import ipaddress
import logging

from . import paths
from .logging_setup import get_logger, log_event
from .system_service import run, which

log = get_logger("cast")

# Resolved late, like the controller token in web_security.py: reading VAR_DIR
# at import time would bake in whichever directory happened to be configured
# when this module was first imported, which the tests move around.


def cert_file():
    """The certificate the sharing page is served with."""
    return paths.VAR_DIR / "cast-cert.pem"


def key_file():
    return paths.VAR_DIR / "cast-key.pem"


def names_file():
    """The names last asked for, so a change of address can be noticed."""
    return paths.VAR_DIR / "cast-cert.names"


#: Two years and change: the longest span every browser accepts without
#: complaint, which keeps the number of times people re-approve the room to a
#: minimum.
VALID_DAYS = 800

#: Renew this far ahead of expiry, so it never runs out mid-meeting.
RENEW_BEFORE_SECONDS = 30 * 24 * 3600


def certificate_present() -> bool:
    """True when there is a certificate and key to serve HTTPS with."""
    try:
        return cert_file().is_file() and key_file().is_file()
    except OSError:
        return False


def _fingerprint(names: list[str]) -> str:
    return "\n".join(names)


def _recorded_names() -> str:
    try:
        return names_file().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _san_argument(names: list[str]) -> str:
    parts: list[str] = []
    for name in names:
        try:
            ipaddress.ip_address(name)
        except ValueError:
            parts.append(f"DNS:{name}")
        else:
            parts.append(f"IP:{name}")
    return ",".join(parts)


def _expiring_soon() -> bool:
    """True when the certificate is close enough to expiry to be replaced."""
    result = run(
        ["openssl", "x509", "-checkend", str(RENEW_BEFORE_SECONDS), "-noout",
         "-in", str(cert_file())],
        timeout=15,
    )
    # `-checkend` exits non-zero when the certificate *will* expire inside the
    # window. A missing openssl also lands here, and regenerating would fail
    # anyway, so treat only a clear "no" as a reason to replace it.
    if result.code == 127:
        return False
    return not result.ok


def needs_regeneration(names: list[str]) -> str:
    """Why the certificate should be (re)generated, or "" if it is fine."""
    if not certificate_present():
        return "there is no certificate yet"
    if names and _recorded_names() != _fingerprint(names):
        return "the room's addresses changed"
    if _expiring_soon():
        return "the certificate is about to expire"
    return ""


def ensure_certificate(names: list[str], *, common_name: str = "") -> tuple[str, str] | None:
    """Return ``(cert_path, key_path)``, generating them if needed.

    Returns ``None`` when HTTPS cannot be offered, having logged why. The
    caller's job is then to say so on the dashboard rather than to fail.
    """
    names = _clean(names)
    reason = needs_regeneration(names)
    if not reason:
        return (str(cert_file()), str(key_file()))

    if not which("openssl"):
        log_event(
            log, logging.ERROR, "cast.openssl_missing",
            hint="install openssl to enable screen sharing from a PC: sudo apt install openssl",
        )
        return None

    if not _generate(names, common_name=common_name, reason=reason):
        # A previously working certificate is better than none, even if the
        # addresses have moved on: the warning changes, the feature still works.
        return (str(cert_file()), str(key_file())) if certificate_present() else None
    return (str(cert_file()), str(key_file()))


def _clean(names: list[str]) -> list[str]:
    """Deduplicate and drop anything that cannot go in a certificate."""
    seen: list[str] = []
    for raw in names:
        name = str(raw or "").strip().lower()
        if not name or name in seen:
            continue
        if any(character in name for character in " ,/\\\"'"):
            continue
        seen.append(name)
    return seen


def _generate(names: list[str], *, common_name: str, reason: str) -> bool:
    try:
        paths.VAR_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log_event(log, logging.ERROR, "cast.certificate_directory_unwritable", error=str(exc))
        return False

    subject = f"/CN={(common_name or 'Meeting room screen sharing')[:60]}"
    base = [
        "openssl", "req", "-x509", "-nodes",
        "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
        "-sha256", "-days", str(VALID_DAYS),
        "-keyout", str(key_file()), "-out", str(cert_file()),
        "-subj", subject,
    ]
    extensions = [
        "-addext", "basicConstraints=critical,CA:FALSE",
        "-addext", "keyUsage=critical,digitalSignature,keyEncipherment",
        "-addext", "extendedKeyUsage=serverAuth",
    ]
    san = _san_argument(names)
    if san:
        extensions += ["-addext", f"subjectAltName={san}"]

    result = run(base + extensions, timeout=60)
    if not result.ok:
        # -addext arrived in OpenSSL 1.1.1, and an elliptic key needs a build
        # with EC enabled. Neither is worth failing over: a certificate with a
        # plain subject still lets the browser share a screen after the same
        # click, so fall back rather than leave the room without the feature.
        log_event(log, logging.WARNING, "cast.certificate_fallback",
                  error=result.output[:200])
        result = run(
            ["openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048", "-sha256",
             "-days", str(VALID_DAYS), "-keyout", str(key_file()),
             "-out", str(cert_file()), "-subj", subject],
            timeout=120,
        )
        names = []  # the fallback certificate carries no names at all

    if not result.ok:
        log_event(log, logging.ERROR, "cast.certificate_failed", error=result.output[:200])
        return False

    try:
        key_file().chmod(0o600)
    except OSError:
        pass
    try:
        # Written whether or not the names made it into the certificate. If the
        # fallback was used they did not, and regenerating on the next start
        # would not put them there either — it would just throw away every
        # laptop's saved exception, once per boot, forever.
        names_file().write_text(_fingerprint(names), encoding="utf-8")
    except OSError:
        pass

    log_event(log, logging.INFO, "cast.certificate_generated",
              reason=reason, names=",".join(names) or "none", days=VALID_DAYS)
    return True


def certificate_summary() -> dict[str, object]:
    """Human-readable certificate facts, for the diagnostics page."""
    if not certificate_present():
        return {"present": False, "names": [], "expires": ""}

    expires = ""
    result = run(["openssl", "x509", "-enddate", "-noout", "-in", str(cert_file())], timeout=15)
    if result.ok:
        expires = result.stdout.strip().split("=", 1)[-1].strip()

    return {
        "present": True,
        # Read out of the certificate rather than the note beside it, so this
        # cannot claim an address the certificate does not actually cover.
        "names": _certificate_names(),
        "expires": expires,
        "path": str(cert_file()),
    }


def _certificate_names() -> list[str]:
    """The addresses the certificate on disk really covers."""
    result = run(
        ["openssl", "x509", "-noout", "-ext", "subjectAltName", "-in", str(cert_file())],
        timeout=15,
    )
    if not result.ok:
        return []
    names: list[str] = []
    for entry in result.stdout.replace("\n", ",").split(","):
        entry = entry.strip()
        # Entries read "DNS:room.local" or "IP Address:192.168.1.9".
        if ":" not in entry:
            continue
        kind, _, value = entry.partition(":")
        if kind.strip() not in ("DNS", "IP Address", "IP"):
            continue
        value = value.strip()
        if value and value not in names:
            names.append(value)
    return names
