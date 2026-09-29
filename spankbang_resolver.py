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
import logging
import os
import re
import socket
import tempfile
import threading
import time
from urllib.parse import urlparse

import requests
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
                    "from a logged-in browser, FLARESOLVERR_URL, or SPANKBANG_PROXY/SPANKBANG_PROXIES)"
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
