"""HLS (m3u8) proxy helpers for the SpankBang API.

WHY THIS EXISTS
---------------
SpankBang's sb-cd.com links are signed AND bound to the IP that resolved
them (this server's, or the outbound proxy's). The MP4 quality links
already work from any device through app.py's /stream/<code> pipe. The
HLS quality links (m3u8_links) could not: a master.m3u8 is only the FIRST
hop — every variant playlist, key and media segment inside it is another
IP-bound CDN URL, so handing a player just the raw master URL (or even a
/stream/-style pipe of only that one file) breaks on the very next
request the player makes.

So HLS needs a real proxy: every URL inside a playlist is rewritten to
point back at this server, and each such URL is fetched by this server
(same IP that resolved the video) and piped back.

DESIGN
------
* Playlist / segment URLs are STATELESS: the upstream URL is encoded into
  the proxy path itself (urlsafe base64), so a video with thousands of
  segments never touches the in-memory registry. Only ONE registry entry
  exists per quality link (it carries the master URL and the outbound
  proxy to use) — the same entry shape the MP4 /stream/ links use.
* Rewritten URLs are ROOT-RELATIVE ("/hls/<code>/p/<token>.ts"), not
  absolute, so they never depend on PUBLIC_BASE_URL / X-Forwarded-* being
  right and can't accidentally produce http:// URLs behind an https
  front (mixed content).
* SSRF guard: this must never become an open proxy. Every hop (including
  each redirect) is checked against an allow-list of CDN host suffixes,
  and the route additionally requires a live registry code.
* The original file extension is kept as the tail of the proxy path
  (".ts", ".m3u8", ".mp4", ".m4s", ...). Some players/demuxers (ffmpeg's
  HLS demuxer among them) refuse segment URLs with no recognised
  extension.

This module has no FastAPI dependency on purpose, so it can be unit
tested on its own.
"""
import base64
import os
import re
from urllib.parse import urljoin, urlparse

import requests

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Hosts (and their subdomains) the proxy is allowed to fetch from.
# Overridable: HLS_ALLOWED_HOST_SUFFIXES="sb-cd.com,phprcdn.com,..."
_DEFAULT_SUFFIXES = "spankbang.com,spankbang.party,sb-cd.com"
ALLOWED_HOST_SUFFIXES = tuple(
    s.strip().lower().lstrip(".")
    for s in os.getenv("HLS_ALLOWED_HOST_SUFFIXES", _DEFAULT_SUFFIXES).split(",")
    if s.strip()
)

MAX_PLAYLIST_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 4

_URI_ATTR_RE = re.compile(r'URI="([^"]*)"')
_EXT_RE = re.compile(r"\.([A-Za-z0-9]{1,5})$")


def _parent_domain(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def allowed_host(url: str, entry: dict | None = None) -> bool:
    """True only for http(s) URLs on an allow-listed CDN host, or on the same
    parent domain as the URL that yt-dlp itself resolved for this entry."""
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower().rstrip(".")
        port = p.port
    except ValueError:
        return False
    if p.scheme not in ("http", "https") or not host:
        return False
    if port not in (None, 80, 443):
        return False
    if any(host == s or host.endswith("." + s) for s in ALLOWED_HOST_SUFFIXES):
        return True
    if entry and entry.get("url"):
        try:
            base = (urlparse(entry["url"]).hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        return bool(base) and _parent_domain(host) == _parent_domain(base)
    return False


def b64e(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def b64d(token: str) -> str:
    return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")


def ext_of(url: str) -> str:
    """".ts" / ".m3u8" / ".mp4" ... from the URL's PATH (never its query),
    or "" when there isn't a sane one."""
    m = _EXT_RE.search(urlparse(url).path)
    return "." + m.group(1).lower() if m else ""


def is_playlist_url(url: str) -> bool:
    return ".m3u8" in urlparse(url).path.lower()


def proxy_path(code: str, upstream_url: str) -> str:
    return f"/hls/{code}/p/{b64e(upstream_url)}{ext_of(upstream_url)}"


def token_to_url(token_with_ext: str) -> str:
    """Inverse of proxy_path()'s last path segment. base64url's alphabet
    has no '.', so everything before the first dot is the token."""
    return b64d(token_with_ext.split(".", 1)[0])


def rewrite_playlist(text: str, playlist_url: str, code: str) -> str:
    """Rewrite every URI in an HLS playlist to go through this proxy.

    Covers both places a URI can appear:
      * bare URI lines (variant playlists after #EXT-X-STREAM-INF, media
        segments after #EXTINF, ...)
      * URI="..." attributes on tags (#EXT-X-KEY, #EXT-X-MAP,
        #EXT-X-MEDIA, #EXT-X-I-FRAME-STREAM-INF, #EXT-X-SESSION-KEY, ...)

    Relative URIs are resolved against playlist_url (the URL the playlist
    was ACTUALLY served from, i.e. after any redirect). URIs with a
    non-http(s) scheme (data:, skd:, urn:, ...) aren't fetchable over
    HTTP and are left exactly as they are.
    """

    def proxify(uri: str) -> str:
        uri = uri.strip()
        if not uri:
            return uri
        scheme = urlparse(uri).scheme.lower()
        if scheme and scheme not in ("http", "https"):
            return uri
        return proxy_path(code, urljoin(playlist_url, uri))

    out = []
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            if "URI=" in line:
                line = _URI_ATTR_RE.sub(lambda m: f'URI="{proxify(m.group(1))}"', line)
            out.append(line)
        else:
            out.append(proxify(stripped))
    return "\n".join(out)


class HostNotAllowed(Exception):
    pass


def open_upstream(entry: dict, url: str, range_header: str = ""):
    """GET `url` (streaming) from this server's own IP / configured
    outbound proxy. Redirects are followed by hand so EVERY hop is
    re-checked against the host allow-list. Returns (response, final_url).
    """
    headers = {"User-Agent": _UA, "Referer": "https://www.spankbang.com/"}
    if range_header:
        headers["Range"] = range_header
    proxy = entry.get("proxy")
    proxies = {"http": proxy, "https": proxy} if proxy else None

    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not allowed_host(current, entry):
            raise HostNotAllowed(urlparse(current).hostname or "unknown host")
        resp = requests.get(
            current, headers=headers, stream=True, timeout=(10, 60),
            allow_redirects=False, proxies=proxies,
        )
        location = resp.headers.get("Location")
        if resp.status_code in (301, 302, 303, 307, 308) and location:
            resp.close()
            current = urljoin(current, location)
            continue
        return resp, current
    raise RuntimeError("too many redirects from upstream")


def read_capped(resp, cap: int = MAX_PLAYLIST_BYTES) -> bytes:
    """Read a (playlist) response body, refusing anything absurdly large."""
    buf = bytearray()
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        buf.extend(chunk)
        if len(buf) > cap:
            raise ValueError("playlist too large")
    return bytes(buf)


def looks_like_playlist(url: str, content_type: str) -> bool:
    return is_playlist_url(url) or "mpegurl" in (content_type or "").lower()
