"""spankbang_resolver.py — resolves a SpankBang video URL to every playable
format yt-dlp can find, in the same response envelope as the sibling resolvers
(links / m3u8_links / audio_links / videoDetails).

yt-dlp already ships a SpankBang extractor, so this file only handles:
host validation, impersonation (curl_cffi), optional cookies/proxies,
a short-lived cache, and shaping the output.

Env vars (all optional):
  SPANKBANG_COOKIES        path to a Netscape cookies file
  SPANKBANG_PROXY          single proxy URL
  SPANKBANG_PROXIES        comma-separated proxy list (tried in order on block)
  SPANKBANG_ALWAYS_PROXY   true -> skip the direct attempt
"""
import logging
import os
import re
import socket
import threading
import time
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget

logger = logging.getLogger("spankbang_api")

try:
    _CHROME_TARGET = ImpersonateTarget.from_str("chrome")
except Exception:
    _CHROME_TARGET = None
_impersonate_ok = None


def impersonation_available() -> bool:
    global _impersonate_ok
    if _impersonate_ok is not None:
        return _impersonate_ok
    if _CHROME_TARGET is None:
        _impersonate_ok = False
    else:
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
                check = getattr(ydl, "_impersonate_target_available", None)
                _impersonate_ok = True if check is None else bool(check(_CHROME_TARGET))
        except Exception:
            _impersonate_ok = False
    logger.info("SpankBang impersonation available: %s", _impersonate_ok)
    return _impersonate_ok


def _format_duration(seconds):
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return None
    if total < 0:
        return None
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# spankbang.com, its locale subdomains (www., de., fr. ...) and spankbang.party
_HOST_RE = re.compile(r"^(?:[\w-]+\.)*spankbang\.(?:com|party)$", re.IGNORECASE)


def is_spankbang_link(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    return bool(_HOST_RE.match(host))


_BLOCK_MARKERS = (
    "not available from your location", "not available in your country",
    "geo restriction", "geo-restricted", "403", "cloudflare", "just a moment",
)


def _is_block_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(m in text for m in _BLOCK_MARKERS)


def _proxy_port_open(proxy_url: str, timeout: float = 2.0) -> bool:
    try:
        rest = proxy_url.split("://", 1)[1]
        host, port_str = rest.split("@")[-1].rsplit(":", 1)
        port = int(port_str.split("/")[0])
    except Exception:
        return True
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _dedupe_by_height(formats):
    best, order, heightless, seen = {}, [], [], set()
    for f in formats:
        h = f.get("height")
        if not h:
            if f.get("url") in seen:
                continue
            seen.add(f.get("url"))
            heightless.append(f)
            continue
        cur = best.get(h)
        if cur is None:
            order.append(h)
            best[h] = f
        elif (f.get("width") or 0) > (cur.get("width") or 0):
            best[h] = f
    return [best[h] for h in order], heightless


_INFO_CACHE = {}
_INFO_CACHE_LOCK = threading.Lock()


def resolve_spankbang(url: str) -> dict:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "format": "all",
        "socket_timeout": 20,
        "retries": 3,
        "extractor_retries": 2,
        "cachedir": False,
        "ignoreconfig": True,
    }
    if impersonation_available():
        ydl_opts["impersonate"] = _CHROME_TARGET

    cookiefile = os.getenv("SPANKBANG_COOKIES", "").strip()
    if cookiefile and os.path.isfile(cookiefile):
        ydl_opts["cookiefile"] = cookiefile

    now = time.time()
    info, used_proxy = None, None
    with _INFO_CACHE_LOCK:
        cached = _INFO_CACHE.get(url)
        if cached and now - cached["ts"] < 60:
            info, used_proxy = cached["info"], cached.get("proxy")

    proxies = [p.strip() for p in os.getenv("SPANKBANG_PROXIES", "").split(",") if p.strip()]
    single = os.getenv("SPANKBANG_PROXY", "").strip()
    if single and single not in proxies:
        proxies.append(single)
    always_proxy = os.getenv("SPANKBANG_ALWAYS_PROXY", "").strip().lower() in ("1", "true", "yes")

    def _extract(opts):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            text = str(e).lower()
            if opts.get("impersonate") and ("impersonate" in text or "curl_cffi" in text or "curl-cffi" in text):
                logger.warning("impersonate unavailable, retrying without it: %s", e)
                fb = dict(opts)
                fb.pop("impersonate", None)
                with yt_dlp.YoutubeDL(fb) as ydl:
                    return ydl.extract_info(url, download=False)
            raise

    if info is None:
        attempts = [] if (always_proxy and proxies) else [None]
        attempts.extend(proxies)
        last_exc = None
        for i, proxy in enumerate(attempts):
            if proxy and proxy.startswith("socks5") and not _proxy_port_open(proxy):
                last_exc = RuntimeError(f"proxy {proxy} is not accepting connections")
                continue
            opts = dict(ydl_opts)
            if proxy:
                opts["proxy"] = proxy
            try:
                logger.info("Resolving SpankBang URL: %s (attempt %d/%d, %s)", url, i + 1, len(attempts),
                            f"proxy={proxy}" if proxy else "direct")
                info = _extract(opts)
                used_proxy, last_exc = proxy, None
                break
            except Exception as e:
                last_exc = e
                if not _is_block_error(e):
                    break
        if last_exc is not None:
            if _is_block_error(last_exc) and not proxies:
                raise RuntimeError(
                    f"{last_exc} (SpankBang is blocking this server — try SPANKBANG_COOKIES "
                    "from a logged-in browser or set SPANKBANG_PROXY/SPANKBANG_PROXIES)"
                )
            raise last_exc
        with _INFO_CACHE_LOCK:
            _INFO_CACHE[url] = {"info": info, "ts": time.time(), "proxy": used_proxy}
            if len(_INFO_CACHE) > 100:
                _INFO_CACHE.pop(min(_INFO_CACHE, key=lambda k: _INFO_CACHE[k]["ts"]), None)

    progressive, hls, audio_links = [], [], []
    for f in info.get("formats") or []:
        f_url = f.get("url")
        if not f_url:
            continue
        vcodec, acodec = f.get("vcodec"), f.get("acodec")
        manifest = f.get("manifest_url") or ""
        proto = (f.get("protocol") or "").lower()
        is_hls = proto.startswith("m3u8") or ".m3u8" in f_url.lower() or ".m3u8" in manifest.lower()
        if vcodec in (None, "none") and acodec not in (None, "none"):
            audio_links.append({"title": f.get("format_note") or f.get("format_id") or "Audio", "url": f_url})
            continue
        if is_hls:
            f = dict(f)
            f["_m3u8_url"] = manifest or f_url
        (hls if is_hls else progressive).append(f)

    links = []
    best, heightless = _dedupe_by_height(progressive)
    for f in best:
        links.append({"title": f"Video {f['height']}p", "url": f["url"], "_headers": f.get("http_headers") or {}})
    for f in heightless:
        links.append({"title": f.get("format_note") or f.get("format_id") or "Video", "url": f["url"], "_headers": f.get("http_headers") or {}})

    m3u8_links = []
    best, heightless = _dedupe_by_height(hls)
    for f in best:
        m3u8_links.append({"title": f"Video {f['height']}p (HLS)", "url": f.get("_m3u8_url") or f["url"]})
    for f in heightless:
        label = f.get("format_note") or f.get("format_id") or "Video"
        m3u8_links.append({"title": f"{label} (HLS)", "url": f.get("_m3u8_url") or f["url"]})

    thumbs, seen = [], set()
    for t in [info.get("thumbnail")] + [x.get("url") for x in (info.get("thumbnails") or [])]:
        if t and t not in seen:
            thumbs.append({"url": t})
            seen.add(t)

    return {
        "videoDetails": {
            "title": info.get("title"),
            "duration": _format_duration(info.get("duration")),
            "lengthSeconds": info.get("duration"),
            "thumbnails": thumbs,
        },
        "m3u8_links": m3u8_links,
        "audio_links": audio_links,
        "links": links,
        "_proxy": used_proxy,
    }
