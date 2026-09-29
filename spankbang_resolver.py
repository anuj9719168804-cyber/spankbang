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
import base64
import html as html_lib
import json
import logging
import os
import re
import socket
import struct
import tempfile
import threading
import time
from http.cookiejar import MozillaCookieJar
from urllib.parse import unquote, urljoin, urlparse

import requests
import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget
from yt_dlp.utils import parse_duration, parse_resolution

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
    "geo restriction", "geo-restricted", "cloudflare", "just a moment", "forbidden",
)
_BLOCK_STATUS_RE = re.compile(r"http error (?:403|429|503)\b")


def _is_block_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return bool(_BLOCK_STATUS_RE.search(text)) or any(m in text for m in _BLOCK_MARKERS)


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


# ---------------------------------------------------------------------------
# Cookie-less block workarounds
#   1. try several browser TLS fingerprints (chrome/safari/edge/firefox)
#   2. try host variants (spankbang.com / www.spankbang.com)
#   3. optional: FLARESOLVERR_URL -> fetch clearance cookies automatically
# ---------------------------------------------------------------------------
_TARGET_NAMES = ("chrome", "safari", "edge", "firefox")
_targets_cache = None


def _impersonation_targets() -> list:
    global _targets_cache
    if _targets_cache is not None:
        return _targets_cache
    found = []
    if impersonation_available():
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
                check = getattr(ydl, "_impersonate_target_available", None)
                for name in _TARGET_NAMES:
                    try:
                        t = ImpersonateTarget.from_str(name)
                    except Exception:
                        continue
                    try:
                        if check is None or check(t):
                            found.append(t)
                    except Exception:
                        continue
        except Exception:
            pass
    _targets_cache = found
    logger.info("SpankBang impersonation targets: %s", [str(t) for t in found] or "none")
    return found


def _is_impersonate_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "impersonat" in text or "curl_cffi" in text or "curl-cffi" in text


def _mirror_urls(url: str) -> list:
    p = urlparse(url)
    seen, out = {(p.netloc or "").lower()}, []
    for host in ("spankbang.com", "www.spankbang.com"):
        if host not in seen:
            seen.add(host)
            out.append(p._replace(netloc=host).geturl())
    return out


def _flaresolverr_cookiefile(url: str):
    """Ask a FlareSolverr instance to pass the Cloudflare check and hand back
    cookies + user agent. Returns (cookiefile_path, user_agent) or (None, None)."""
    base = os.getenv("FLARESOLVERR_URL", "").strip().rstrip("/")
    if not base:
        return None, None
    try:
        r = requests.post(
            base + "/v1",
            json={"cmd": "request.get", "url": url, "maxTimeout": 60000},
            timeout=90,
        )
        sol = (r.json() or {}).get("solution") or {}
        cookies = sol.get("cookies") or []
        if not cookies:
            return None, None
        fd, path = tempfile.mkstemp(prefix="sb_cookies_", suffix=".txt")
        with os.fdopen(fd, "w") as fh:
            fh.write("# Netscape HTTP Cookie File\n")
            for c in cookies:
                dom = c.get("domain", "")
                exp = int(c.get("expires") or 0)
                fh.write("\t".join([
                    dom, "TRUE" if dom.startswith(".") else "FALSE", c.get("path", "/"),
                    "TRUE" if c.get("secure") else "FALSE", str(max(exp, 0)),
                    c["name"], c["value"],
                ]) + "\n")
        return path, sol.get("userAgent")
    except Exception as e:
        logger.warning("FlareSolverr failed: %s", e)
        return None, None


# ---------------------------------------------------------------------------
# FlareSolverr direct path (no cookies needed from the operator)
# A real Chrome (FlareSolverr) loads the page, so we parse the stream URLs
# from the returned HTML ourselves instead of re-requesting with yt-dlp (whose
# TLS/UA would not match the Cloudflare clearance).
# ---------------------------------------------------------------------------
def _fs_call(base: str, payload: dict, timeout: int = 90) -> dict:
    r = requests.post(base + "/v1", json=payload, timeout=timeout)
    data = r.json() or {}
    if data.get("status") not in (None, "ok"):
        raise RuntimeError(f"FlareSolverr: {data.get('message')}")
    return data


def _fs_pre_json(html_text: str):
    m = re.search(r"<pre[^>]*>(.*?)</pre>", html_text, re.S)
    raw = html_lib.unescape(m.group(1) if m else html_text).strip()
    return json.loads(raw)


def _parse_sb_page(page: str, url: str):
    """Build a yt-dlp-shaped info dict from a SpankBang page's HTML."""
    if re.search(r"<[^>]+\b(?:id|class)=[\"']video_removed", page):
        raise RuntimeError("Video is not available (removed)")
    formats = []

    def add(fid, furl):
        if isinstance(furl, list):
            furl = furl[0] if furl else None
        if not furl or not str(furl).startswith("http"):
            return
        res = parse_resolution(fid) or {}
        if fid.startswith("m3u8") or ".m3u8" in furl.lower():
            formats.append({"url": furl, "manifest_url": furl, "format_id": fid,
                            "protocol": "m3u8_native", "ext": "mp4", **res})
        elif ".mpd" in furl.lower() or fid.startswith("mpd"):
            return  # DASH not supported by the proxy layer
        elif not (res.get("height") or res.get("width") or furl.split("?")[0].lower().endswith(".mp4")):
            return  # skip cover_image / stream_sheet / thumbnail (jpg etc.)
        else:
            formats.append({"url": furl, "format_id": fid, "protocol": "https",
                            "ext": "mp4", "vcodec": "unknown", "acodec": "unknown", **res})

    for m in re.finditer(r"stream_url_(?P<id>[^\s=]+)\s*=\s*([\"'])(?P<url>(?:(?!\2).)+)\2", page):
        add(m.group("id"), m.group("url"))

    def rx(pattern, default=None):
        m = re.search(pattern, page, re.S)
        return html_lib.unescape(m.group(1)).strip() if m else default

    meta = _meta_from_page(page)
    title, thumb, duration = meta["title"], meta["thumbnail"], meta["duration"]
    stream_key = rx(r"data-streamkey\s*=\s*[\"']([^\"']+)[\"']")
    return {"title": title, "thumbnail": thumb, "duration": duration,
            "formats": formats, "_stream_key": stream_key}


# ---------------------------------------------------------------------------
# Missing-metadata fix (duration / lengthSeconds / title / thumbnail == null)
#
# yt-dlp's SpankBang extractor only reads the duration from the page's
# <meta property="og:video:duration">. When SpankBang drops or renames that
# tag, yt-dlp returns duration=None and the API answered
# {"duration": null, "lengthSeconds": null}.
#
# _fill_missing() now runs after extraction and fills every gap, cheapest
# source first:
#   1. the video page HTML (several patterns: og:video:duration, JSON-LD,
#      itemprop, on-page length badge, ...)
#   2. the HLS playlist  -> sum of the #EXTINF segment durations
#   3. the MP4 header    -> 'mvhd' atom, read with a few tiny Range requests
# ---------------------------------------------------------------------------
_ISO_DUR_RE = re.compile(
    r"^P(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?$", re.I
)


def _to_seconds(val):
    """'754' / 754 / 'PT12M34S' / '12:34' / '1:02:03' / '12 min' -> int seconds."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val) if val > 0 else None
    val = str(val).strip()
    if not val:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", val):
        n = int(float(val))
        return n if n > 0 else None
    m = _ISO_DUR_RE.match(val)
    if m and any(m.groupdict().values()):
        g = {k: float(v) if v else 0.0 for k, v in m.groupdict().items()}
        n = int(g["d"] * 86400 + g["h"] * 3600 + g["m"] * 60 + g["s"])
        return n if n > 0 else None
    if re.fullmatch(r"\d{1,3}(?::\d{1,2}){1,2}", val):
        parts = [int(x) for x in val.split(":")]
        n = 0
        for x in parts:
            n = n * 60 + x
        return n if n > 0 else None
    try:
        n = parse_duration(val)
        return int(n) if n and n > 0 else None
    except Exception:
        return None


_DURATION_PATTERNS = (
    r"[\"']duration[\"']\s*:\s*[\"'](P[^\"']+)[\"']",                                  # JSON-LD ISO 8601
    r"<meta[^>]+(?:property|name)=[\"'](?:og:)?video:duration[\"'][^>]*content=[\"'](\d+)[\"']",
    r"<meta[^>]+content=[\"'](\d+)[\"'][^>]*(?:property|name)=[\"'](?:og:)?video:duration[\"']",
    r"itemprop=[\"']duration[\"'][^>]*content=[\"']([^\"']+)[\"']",
    r"content=[\"']([^\"']+)[\"'][^>]*itemprop=[\"']duration[\"']",
    r"<span[^>]+\bclass=[\"'][^\"']*\b(?:i-length|length|duration|video-length)\b[^\"']*[\"'][^>]*>\s*([\d:]+(?:\s*(?:min|m|h|s)\w*)?)\s*<",
    r"<div[^>]+\bclass=[\"']right_side[^>]+>\s*<span>([^<]+)",
    r"[\"']lengthSeconds[\"']\s*:\s*[\"']?(\d+)",
    r"[\"']duration[\"']\s*:\s*[\"']?(\d{2,6})[\"']?\s*[,}]",                          # "duration": 754
    r"\bvideo_duration\s*=\s*[\"']?(\d+)",
    r"data-duration=[\"'](\d+)[\"']",
)


def _duration_from_page(page: str):
    for pat in _DURATION_PATTERNS:
        for m in re.finditer(pat, page, re.S | re.I):
            d = _to_seconds(html_lib.unescape(m.group(1)))
            if d:
                return d
    return None


def _clean_title(title):
    if not title:
        return title
    # Remove unwanted surrounding/embedded double quotes and slash characters.
    return title.replace('"', '').replace('\\', '').replace('/', '').strip()

def _meta_from_page(page: str) -> dict:
    def rx(pattern):
        m = re.search(pattern, page, re.S)
        return html_lib.unescape(m.group(1)).strip() if m else None

    title = (
        rx(r"<h1[^>]+\btitle=[\"']([^\"']+)[\"']")
        or rx(r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)")
        or rx(r"<meta property=\"og:title\" content=\"([^\"]+)")
        or rx(r"<title>([^<]+)</title>")
    )
    thumb = (
        rx(r"<meta[^>]+property=[\"']og:image[\"'][^>]+content=[\"']([^\"']+)")
        or rx(r"<meta property=\"og:image\" content=\"([^\"]+)")
    )
    return {"title": _clean_title(title), "thumbnail": thumb, "duration": _duration_from_page(page)}


def _title_from_url(url: str):
    """Last-resort title from the URL slug: /abc12/video/some+cool+title -> 'Some Cool Title'."""
    try:
        parts = [p for p in urlparse(url).path.split("/") if p]
        slug = parts[parts.index("video") + 1] if "video" in parts else parts[-1]
        slug = unquote(slug).replace("+", " ").replace("-", " ").replace("_", " ").strip()
        return slug.title() or None
    except Exception:
        return None


def _load_cookie_dict(path):
    if not path or not os.path.isfile(path):
        return {}
    try:
        jar = MozillaCookieJar()
        jar.load(path, ignore_discard=True, ignore_expires=True)
        return {c.name: c.value for c in jar}
    except Exception:
        return {}


def _fetch_page_html(url: str, opts: dict):
    """GET the video page with a browser TLS fingerprint (same cookies/proxy
    yt-dlp used). Returns HTML text or None."""
    proxy = opts.get("proxy")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    cookies = _load_cookie_dict(opts.get("cookiefile"))
    hdrs = {"Referer": "https://spankbang.com/", "Accept-Language": "en-US,en;q=0.9"}

    def usable(status, text):
        return status == 200 and text and "just a moment" not in text[:3000].lower()

    try:
        from curl_cffi import requests as cr
        for target in ("chrome", "safari", "edge"):
            try:
                r = cr.get(url, impersonate=target, timeout=20, proxies=proxies, cookies=cookies or None,
                           headers=hdrs, allow_redirects=True)
                if usable(r.status_code, r.text):
                    return r.text
            except Exception as e:
                logger.info("page fetch (%s) failed: %s", target, str(e)[:120])
    except Exception:
        pass
    try:
        r = requests.get(url, headers={"User-Agent": _UA_FALLBACK, **hdrs}, cookies=cookies or None,
                         proxies=proxies, timeout=20)
        if usable(r.status_code, r.text):
            return r.text
    except Exception as e:
        logger.info("page fetch (requests) failed: %s", str(e)[:120])
    return None


_UA_FALLBACK = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _cdn_headers(fmt: dict) -> dict:
    h = {"User-Agent": _UA_FALLBACK, "Referer": "https://spankbang.com/"}
    h.update({k: v for k, v in (fmt.get("http_headers") or {}).items() if k.lower() not in ("range", "host")})
    return h


def _hls_duration(m3u8_url: str, headers: dict, proxy):
    """Total playtime of an HLS VOD = sum of its #EXTINF segment durations."""
    proxies = {"http": proxy, "https": proxy} if proxy else None

    def get(u):
        r = requests.get(u, headers=headers, proxies=proxies, timeout=15)
        r.raise_for_status()
        text = r.text.lstrip("\ufeff \t\r\n")
        if not text.startswith("#EXTM3U"):
            raise ValueError("not an HLS playlist")
        return text

    cur = m3u8_url
    text = get(cur)
    for _ in range(2):  # master -> media playlist
        if "#EXT-X-STREAM-INF" not in text:
            break
        lines = text.splitlines()
        nxt = None
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                for cand in lines[i + 1:]:
                    cand = cand.strip()
                    if cand and not cand.startswith("#"):
                        nxt = cand
                        break
                break
        if not nxt:
            return None
        cur = urljoin(cur, nxt)
        text = get(cur)
    total = sum(float(x) for x in re.findall(r"#EXTINF:\s*([\d.]+)", text))
    return int(round(total)) or None


def _mvhd_seconds(moov_body: bytes):
    off = 0
    while off + 8 <= len(moov_body):
        size, typ = struct.unpack(">I4s", moov_body[off:off + 8])
        if typ == b"mvhd":
            b = moov_body[off + 8:]
            if not b:
                return None
            if b[0] == 1 and len(b) >= 32:
                timescale, dur = struct.unpack(">IQ", b[20:32])
            elif len(b) >= 20:
                timescale, dur = struct.unpack(">II", b[12:20])
            else:
                return None
            return int(round(dur / timescale)) if timescale and dur else None
        if size < 8:
            return None
        off += size
    return None


def _mp4_duration_from(fetch):
    """fetch(start, length) -> bytes. Walks top-level MP4 boxes until 'moov',
    then reads the movie duration from 'mvhd'. Works whether moov is at the
    start (faststart) or after a huge mdat, using only tiny reads."""
    pos = 0
    for _ in range(16):
        hdr = fetch(pos, 16)
        if len(hdr) < 8:
            return None
        size, typ = struct.unpack(">I4s", hdr[:8])
        hlen = 8
        if size == 1:
            if len(hdr) < 16:
                return None
            size, hlen = struct.unpack(">Q", hdr[8:16])[0], 16
        elif size == 0:
            return None
        if size < hlen:
            return None
        if typ == b"moov":
            return _mvhd_seconds(fetch(pos + hlen, min(size - hlen, 8192)))
        pos += size
    return None


def _mp4_duration(url: str, headers: dict, proxy):
    proxies = {"http": proxy, "https": proxy} if proxy else None

    def fetch(start, length):
        h = dict(headers, Range=f"bytes={start}-{start + length - 1}")
        r = requests.get(url, headers=h, proxies=proxies, stream=True, timeout=(10, 20))
        try:
            if r.status_code == 200 and start > 0:
                raise ValueError("server ignores Range")
            if r.status_code >= 400:
                raise ValueError(f"HTTP {r.status_code}")
            return r.raw.read(length, decode_content=True)
        finally:
            r.close()

    return _mp4_duration_from(fetch)


def _fill_missing(info: dict, url: str, opts: dict) -> dict:
    """Fill duration / title / thumbnail when yt-dlp left them empty. Never raises."""
    try:
        page = info.pop("_page", None)
        need_dur = not _to_seconds(info.get("duration"))
        need_title = not info.get("title")
        need_thumb = not (info.get("thumbnail") or info.get("thumbnails"))
        if not (need_dur or need_title or need_thumb):
            return info

        if page is None:
            page = _fetch_page_html(url, opts)
        if page:
            meta = _meta_from_page(page)
            if need_dur and meta["duration"]:
                info["duration"], need_dur = meta["duration"], False
            if need_title and meta["title"]:
                info["title"], need_title = meta["title"], False
            if need_thumb and meta["thumbnail"]:
                info["thumbnail"], need_thumb = meta["thumbnail"], False

        if need_dur:
            proxy = opts.get("proxy")
            fmts = [f for f in (info.get("formats") or []) if f.get("url")]
            hls = [f for f in fmts if (f.get("protocol") or "").startswith("m3u8") or ".m3u8" in f["url"].lower()]
            mp4 = [f for f in fmts if f not in hls]
            for f in hls[:2]:
                try:
                    d = _hls_duration(f.get("manifest_url") or f["url"], _cdn_headers(f), proxy)
                    if d:
                        info["duration"], need_dur = d, False
                        break
                except Exception as e:
                    logger.info("HLS duration fallback failed: %s", str(e)[:120])
            if need_dur:
                for f in sorted(mp4, key=lambda x: x.get("height") or 10**6)[:2]:  # smallest file first
                    try:
                        d = _mp4_duration(f["url"], _cdn_headers(f), proxy)
                        if d:
                            info["duration"], need_dur = d, False
                            break
                    except Exception as e:
                        logger.info("MP4 duration fallback failed: %s", str(e)[:120])

        if need_title:
            info["title"] = _title_from_url(url) or "video"
        if need_dur:
            logger.warning("SpankBang: could not determine duration for %s", url)
    except Exception as e:
        logger.warning("metadata fill failed: %s", e)
    finally:
        info.pop("_page", None)
    return info



def _flaresolverr_info(url: str):
    """Returns a yt-dlp-shaped info dict via FlareSolverr, or None if not configured."""
    base = os.getenv("FLARESOLVERR_URL", "").strip().rstrip("/")
    if not base:
        return None
    sid = "sb_" + str(int(time.time() * 1000))
    proxy = os.getenv("SPANKBANG_PROXY", "").strip()
    extra = {"proxy": {"url": proxy}} if proxy else {}
    try:
        _fs_call(base, {"cmd": "sessions.create", "session": sid, **extra}, 30)
        host = urlparse(url).netloc
        sol = _fs_call(base, {
            "cmd": "request.get", "url": url, "session": sid, "maxTimeout": 60000,
            "cookies": [{"name": "country", "value": "US", "domain": ".spankbang.com", "path": "/"}],
        }).get("solution") or {}
        page = sol.get("response") or ""
        if not page or "just a moment" in page.lower()[:3000]:
            raise RuntimeError("FlareSolverr could not pass the Cloudflare challenge")
        info = _parse_sb_page(page, url)
        info["_page"] = page
        if not info["formats"] and info.get("_stream_key"):
            api = f"https://{host}/api/videos/stream"
            sol2 = _fs_call(base, {
                "cmd": "request.post", "url": api, "session": sid, "maxTimeout": 60000,
                "postData": "id=%s&data=0" % info["_stream_key"],
            }).get("solution") or {}
            stream = _fs_pre_json(sol2.get("response") or "{}")
            fmts = []
            for fid, furl in (stream or {}).items():
                tmp = _parse_sb_page(f"stream_url_{fid}='{furl[0] if isinstance(furl, list) and furl else furl}'", url)
                fmts.extend(tmp["formats"])
            info["formats"] = fmts
        if not info["formats"]:
            raise RuntimeError("FlareSolverr page had no stream URLs")
        return info
    finally:
        try:
            _fs_call(base, {"cmd": "sessions.destroy", "session": sid}, 15)
        except Exception:
            pass


_WARM_CACHE = {"path": None, "ts": 0.0}
_WARM_TTL = 600  # reuse self-generated anonymous cookies for 10 minutes


def _write_netscape(cookies, prefix="sb_warm_cookies_") -> str:
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=".txt")
    with os.fdopen(fd, "w") as fh:
        fh.write("# Netscape HTTP Cookie File\n")
        for c in cookies:
            dom = c.domain or ".spankbang.com"
            fh.write("\t".join([
                dom, "TRUE" if dom.startswith(".") else "FALSE", c.path or "/",
                "TRUE" if c.secure else "FALSE", str(int(c.expires or 0)),
                c.name, c.value or "",
            ]) + "\n")
    return path


def _warmup_cookiefile(url: str, proxy=None):
    """Cookie-free for the operator: behave like a first-time browser visit.
    Loads the home page (and the video page) through curl_cffi with a real
    browser TLS fingerprint in ONE session, keeps whatever anonymous cookies
    the site/Cloudflare hands out, and returns them as a temp cookie file for
    yt-dlp. No login and no manual export needed. Won't help if Cloudflare
    serves an interactive JS challenge (then use FlareSolverr/proxy)."""
    now = time.time()
    if _WARM_CACHE["path"] and now - _WARM_CACHE["ts"] < _WARM_TTL and os.path.isfile(_WARM_CACHE["path"]):
        return _WARM_CACHE["path"]
    try:
        from curl_cffi import requests as cr
    except Exception:
        return None
    p = urlparse(url)
    home = f"{p.scheme or 'https'}://{p.netloc}/"
    kw = {"proxies": {"http": proxy, "https": proxy}} if proxy else {}
    for target in ("chrome", "safari", "edge"):
        try:
            sess = cr.Session(impersonate=target, timeout=20)
            r1 = sess.get(home, allow_redirects=True, **kw)
            time.sleep(0.8)
            r2 = sess.get(url, headers={"Referer": home}, allow_redirects=True, **kw)
            jar = list(sess.cookies.jar)
            logger.info("warm-up %s: home=%s video=%s cookies=%d", target, r1.status_code, r2.status_code, len(jar))
            if jar and (r1.status_code < 400 or r2.status_code < 400):
                path = _write_netscape(jar)
                old = _WARM_CACHE["path"]
                _WARM_CACHE.update(path=path, ts=time.time())
                if old and old != path:
                    try:
                        os.remove(old)
                    except OSError:
                        pass
                return path
        except Exception as e:
            logger.info("warm-up %s failed: %s", target, str(e)[:120])
    return None


def _copy_file(src: str) -> str:
    fd, dst = tempfile.mkstemp(prefix="sb_use_", suffix=".txt")
    with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
        out.write(inp.read())
    _TMP_TO_CLEAN.append(dst)
    return dst


_TMP_TO_CLEAN: list = []


def _cleanup_tmp():
    while _TMP_TO_CLEAN:
        try:
            os.remove(_TMP_TO_CLEAN.pop())
        except OSError:
            pass


def _extract_with_fallbacks(opts: dict, url: str) -> dict:
    def run(o, u):
        with yt_dlp.YoutubeDL(o) as ydl:
            return ydl.extract_info(u, download=False)

    # Cookie-free path: if a FlareSolverr sidecar is configured, use it first.
    try:
        fs_info = _flaresolverr_info(url)
        if fs_info:
            return fs_info
    except Exception as e:
        logger.info("FlareSolverr direct path failed: %s", str(e)[:200])

    base = dict(opts)
    base.pop("impersonate", None)
    attempts = [dict(base, impersonate=t) for t in _impersonation_targets()]
    attempts.append(base)  # plain request as last resort

    last = None
    # The block is intermittent (Cloudflare scoring), so give it two rounds.
    for rnd in range(2):
        if rnd:
            time.sleep(2.0)
        for o in attempts:
            try:
                return run(o, url)
            except Exception as e:
                last = e
                if not (_is_block_error(e) or _is_impersonate_error(e)):
                    raise
                logger.info("round %d attempt failed (%s): %s", rnd + 1,
                            o.get("impersonate") or "no-impersonate", str(e)[:120])

    warm_path = _warmup_cookiefile(url, opts.get("proxy"))
    if warm_path and not opts.get("cookiefile"):
        for o in attempts[:2]:
            try:
                return run(dict(o, cookiefile=_copy_file(warm_path)), url)
            except Exception as e:
                last = e

    for m in _mirror_urls(url):
        try:
            return run(attempts[0], m)
        except Exception as e:
            last = e

    cookie_path, ua = _flaresolverr_cookiefile(url)
    if cookie_path:
        try:
            o = dict(base, cookiefile=cookie_path)
            if ua:
                o["http_headers"] = {"User-Agent": ua}
            return run(o, url)
        except Exception as e:
            last = e
        finally:
            try:
                os.remove(cookie_path)
            except OSError:
                pass
    raise last


def _cookie_temp_copy():
    """Return path to a private temp copy of the configured cookies, or None.
    Sources: SPANKBANG_COOKIES (file path), SPANKBANG_COOKIES_B64 (base64 of the
    Netscape file), SPANKBANG_COOKIES_TEXT (raw Netscape text). A copy is used
    because yt-dlp rewrites the cookie file on exit, which fails on read-only
    mounts (e.g. platform "secret files")."""
    content = None
    path = os.getenv("SPANKBANG_COOKIES", "").strip()
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError as e:
            logger.warning("cannot read SPANKBANG_COOKIES: %s", e)
    if content is None:
        b64 = os.getenv("SPANKBANG_COOKIES_B64", "").strip()
        if b64:
            try:
                content = base64.b64decode(b64).decode("utf-8", errors="replace")
            except Exception as e:
                logger.warning("bad SPANKBANG_COOKIES_B64: %s", e)
    if content is None:
        content = os.getenv("SPANKBANG_COOKIES_TEXT", "").replace("\\n", "\n") or None
    if not content:
        return None
    if not content.lstrip().startswith("# "):
        content = "# Netscape HTTP Cookie File\n" + content
    fd, tmp = tempfile.mkstemp(prefix="sb_user_cookies_", suffix=".txt")
    with os.fdopen(fd, "w") as fh:
        fh.write(content)
    return tmp


_INFO_CACHE = {}
_INFO_CACHE_LOCK = threading.Lock()


def _resolve_core(url: str, user_cookie_tmp) -> dict:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "format": "all",
        "socket_timeout": 20,
        "retries": 1,
        "extractor_retries": 1,
        "cachedir": False,
        "ignoreconfig": True,
    }
    if impersonation_available():
        ydl_opts["impersonate"] = _CHROME_TARGET

    if user_cookie_tmp:
        ydl_opts["cookiefile"] = user_cookie_tmp

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
                info = _extract_with_fallbacks(opts, url)
                info = _fill_missing(info, url, opts)
                used_proxy, last_exc = proxy, None
                break
            except Exception as e:
                last_exc = e
                if not _is_block_error(e):
                    break
        if last_exc is not None:
            if _is_block_error(last_exc) and not proxies:
                raise RuntimeError(
                    f"{last_exc} (SpankBang/Cloudflare is blocking this server's IP. Cookie-free fix: run "
                    "FlareSolverr (docker compose up) and set FLARESOLVERR_URL; if the IP itself is "
                    "blocked, also set SPANKBANG_PROXY to a residential proxy)"
                )
            raise last_exc
        with _INFO_CACHE_LOCK:
            _INFO_CACHE[url] = {"info": info, "ts": time.time(), "proxy": used_proxy}
            if len(_INFO_CACHE) > 100:
                _INFO_CACHE.pop(min(_INFO_CACHE, key=lambda k: _INFO_CACHE[k]["ts"]), None)

    # Diagnostics: shows exactly which qualities the site offered for this video.
    logger.info("SpankBang formats for %s: %s", url, [
        (f.get("format_id"), f.get("height"), (f.get("protocol") or "")[:6]) for f in info.get("formats") or []
    ])

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
            # Per-quality variant playlist when the height is known, so each
            # HLS quality points at ITS OWN stream (not the shared master).
            f["_m3u8_url"] = f_url if f.get("height") else (manifest or f_url)
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
        m3u8_links.append({"title": f"Video {f['height']}p (HLS)", "height": f["height"],
                           "url": f.get("_m3u8_url") or f["url"]})
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
            "duration": _format_duration(_to_seconds(info.get("duration"))),
            "lengthSeconds": _to_seconds(info.get("duration")),
            "thumbnails": thumbs,
        },
        "m3u8_links": m3u8_links,
        "audio_links": audio_links,
        "links": links,
        "_proxy": used_proxy,
    }


def resolve_spankbang(url: str) -> dict:
    tmp = _cookie_temp_copy()
    try:
        return _resolve_core(url, tmp)
    finally:
        _cleanup_tmp()
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
