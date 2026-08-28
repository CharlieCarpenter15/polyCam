"""Find the online-meeting link in a calendar event and name its provider.

Real calendar entries put the join link in inconsistent places: the ``URL``
property, somewhere in a long HTML ``DESCRIPTION``, or in ``LOCATION``. Outlook
also rewrites links through SafeLinks, and HTML descriptions arrive with
entities (``&amp;``) and surrounding markup. All of that is handled here, in one
small module with no dependencies, so it can be unit-tested without a calendar.

Adding a provider means adding one entry to :data:`PROVIDERS`.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit


@dataclass(frozen=True)
class Provider:
    """A meeting platform the appliance recognises."""

    id: str
    label: str
    #: Host suffixes that identify this provider.
    hosts: tuple[str, ...]
    #: Extra full-URL regexes (used where the host alone is ambiguous).
    patterns: tuple[str, ...] = ()
    #: False for providers we can display but not reliably drive.
    automatable: bool = True


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        id="teams",
        label="Microsoft Teams",
        hosts=("teams.microsoft.com", "teams.live.com", "teams.microsoft.us"),
    ),
    Provider(
        id="meet",
        label="Google Meet",
        hosts=("meet.google.com",),
    ),
    Provider(
        id="zoom",
        label="Zoom",
        hosts=("zoom.us", "zoomgov.com"),
        patterns=(r"https?://[^\s/]*\bzoom\.us/(?:j|s|w|my)/",),
    ),
    Provider(
        id="webex",
        label="Webex",
        hosts=("webex.com", "webex.com.cn"),
    ),
    Provider(
        id="whereby",
        label="Whereby",
        hosts=("whereby.com",),
        automatable=False,
    ),
    Provider(
        id="other",
        label="Online meeting",
        hosts=(),
        automatable=False,
    ),
)

PROVIDERS_BY_ID: dict[str, Provider] = {p.id: p for p in PROVIDERS}
OTHER = PROVIDERS_BY_ID["other"]

#: Hosts that mean "this is a meeting link" even without a known provider.
_GENERIC_MEETING_HINTS = (
    "meet.jit.si",
    "bluejeans.com",
    "gotomeet.me",
    "gotomeeting.com",
    "chime.aws",
    "ringcentral.com",
    "8x8.vc",
)

# Deliberately permissive: calendar bodies are messy. Trailing punctuation and
# markup are trimmed afterwards by _clean_url().
_URL_RE = re.compile(r"""(?i)\bhttps?://[^\s<>"'\]\)}]+""")

_TRAILING = ".,;:!?…\"'’”)»>]}|*_"

_SAFELINK_HOST_RE = re.compile(r"(?i)safelinks\.protection\.outlook\.(?:com|us|de)$")
_URLDEFENSE_HOST_RE = re.compile(r"(?i)urldefense\.(?:com|proofpoint\.com)$")


def _strip_markup(text: str) -> str:
    """Turn an HTML description into plain text without losing URLs."""
    if not text:
        return ""
    # Keep href targets: <a href="URL"> ... </a>
    text = re.sub(r'(?is)<a\b[^>]*href\s*=\s*"([^"]+)"[^>]*>', r" \1 ", text)
    text = re.sub(r"(?is)<a\b[^>]*href\s*=\s*'([^']+)'[^>]*>", r" \1 ", text)
    # A bare <https://…> is a URL in angle brackets, not an HTML tag.
    text = re.sub(r"<\s*(https?://[^>\s]+)\s*>", r" \1 ", text)
    text = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    # ICS escaping: a feed contains the literal two-character sequences \n, \,
    # and \; where a newline, comma or semicolon was meant.
    text = text.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";")
    return text


def unwrap_url(url: str) -> str:
    """Undo link-rewriting wrappers (Outlook SafeLinks, Proofpoint URLDefense).

    ``parse_qs`` already percent-decodes query values, so the extracted target
    must *not* be unquoted a second time — doing so would turn a Teams
    ``19%3ameeting_…`` identifier and its ``context={"Tid":…}`` parameter into
    raw characters and break the link.
    """
    for _ in range(4):  # wrappers are occasionally nested
        try:
            parts = urlsplit(url)
        except ValueError:
            return url
        host = (parts.hostname or "").lower()
        target = ""

        if _SAFELINK_HOST_RE.search(host):
            # ?url=<already-encoded target>
            target = (parse_qs(parts.query).get("url") or [""])[0]

        elif _URLDEFENSE_HOST_RE.search(host):
            path = parts.path or ""
            if "/v3/" in path or path.startswith("/v3"):
                # /v3/__<target>__;<tracking>
                match = re.search(r"__(https?://.+?)__", unquote(path))
                target = match.group(1) if match else ""
            else:
                # /v2/url?u=<target with - and _ substituted for % and />
                raw = (parse_qs(parts.query).get("u") or [""])[0]
                if raw:
                    target = unquote(raw.replace("-", "%").replace("_", "/"))

        if not target:
            return url
        if not target.lower().startswith(("http://", "https://")):
            return url
        url = target
    return url


def _clean_url(url: str) -> str:
    url = html.unescape(url).strip()
    url = url.strip("<>")
    while url and url[-1] in _TRAILING:
        # Do not eat a closing bracket that is part of the URL itself.
        url = url[:-1]
    return unwrap_url(url)


def find_urls(*texts: str) -> list[str]:
    """Every http(s) URL in the given texts, cleaned, de-duplicated, in order."""
    seen: set[str] = set()
    out: list[str] = []
    for text in texts:
        if not text:
            continue
        for match in _URL_RE.finditer(_strip_markup(str(text))):
            url = _clean_url(match.group(0))
            if len(url) < 12 or url in seen:
                continue
            seen.add(url)
            out.append(url)
    return out


def provider_for_url(url: str) -> Provider | None:
    """The provider a URL belongs to, or ``None`` if it is not a meeting link."""
    if not url:
        return None
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    for provider in PROVIDERS:
        for suffix in provider.hosts:
            if host == suffix or host.endswith("." + suffix):
                if provider.patterns and not any(
                    re.search(pattern, url) for pattern in provider.patterns
                ):
                    # e.g. zoom.us marketing pages are not meetings.
                    continue
                return provider
    if any(host == hint or host.endswith("." + hint) for hint in _GENERIC_MEETING_HINTS):
        return OTHER
    return None


def extract_meeting(
    *, url: str = "", description: str = "", location: str = "", extra: str = ""
) -> tuple[str, Provider] | tuple[None, None]:
    """Best join link for an event.

    Fields are searched in the order a calendar is most likely to be correct:
    the dedicated ``URL`` property, then ``LOCATION`` (Teams/Meet put the link
    there), then the description body. Automatable providers win over ones we
    can only display.
    """
    candidates: list[tuple[str, Provider]] = []
    for text in (url, location, description, extra):
        for found in find_urls(text):
            provider = provider_for_url(found)
            if provider is not None:
                candidates.append((found, provider))
        if candidates:
            # Prefer a link from the earliest field that yielded any.
            break

    if not candidates:
        return None, None

    candidates.sort(key=lambda pair: (not pair[1].automatable, pair[1].id == "other"))
    return candidates[0]


def provider_label(provider_id: str) -> str:
    provider = PROVIDERS_BY_ID.get(provider_id)
    return provider.label if provider else "Online meeting"
