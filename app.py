"""Standalone SpankBang-only resolver API."""
import logging
import os
import re
import secrets
import threading
import time
import traceback
from collections import defaultdict, deque

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

import config
import hls_proxy
import spankbang_resolver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("spankbang_api")

app = FastAPI(
    title="SpankBang Resolver API",
    description="Standalone SpankBang-only video resolver API.",
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# Stream proxy (ported from FBOT's keep_alive.py /stream/<code> proxy).
# SpankBang's sb-cd.com links are signed AND bound to the IP that resolved them
# (this server's, or the proxy's). Opening one from a phone/browser on another
# IP gives the CDN's "Page not found". So each MP4 link also gets a
# /stream/<code> URL that this server fetches itself (same IP) and pipes back,
# with Range pass-through so seeking works. Codes are random and expire.
# ---------------------------------------------------------------------------
STREAM_TTL = 3600
STREAM_MAX_ENTRIES = 2000
_stream_registry: dict = {}
_stream_lock = threading.Lock()
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _register_stream(cdn_url: str, name: str, proxy, kind: str = "mp4", headers: dict | None = None) -> str:
    """kind: "mp4" -> served by /stream/<code> (single-file pipe);
    "hls" -> served by /hls/<code>/master.m3u8 (rewriting playlist proxy).
    Each route only accepts its own kind, so a code for one can't be
    replayed against the other."""
    code = secrets.token_urlsafe(9)
    now = time.time()
    with _stream_lock:
        for k in [k for k, v in _stream_registry.items() if now - v["ts"] > STREAM_TTL]:
            del _stream_registry[k]
        while len(_stream_registry) >= STREAM_MAX_ENTRIES:
            del _stream_registry[min(_stream_registry, key=lambda k: _stream_registry[k]["ts"])]
        _stream_registry[code] = {"url": cdn_url, "name": name, "proxy": proxy, "ts": now, "kind": kind, "headers": headers or {}}
    return code


def _public_base(request: Request) -> str:
    explicit = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip().strip("/")
    if host:
        return f"https://{host}"
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{proto}://{request.headers.get('x-forwarded-host', request.headers.get('host', ''))}"


def _safe_name(title, label) -> str:
    base = re.sub(r"[^\w\-. ]+", "", f"{title or 'video'} {label or ''}").strip() or "video"
    return base[:120] + ".mp4"


def _open_upstream(entry: dict, range_header: str):
    headers = {"User-Agent": _UA, "Referer": "https://spankbang.com/"}
    headers.update({k: v for k, v in (entry.get("headers") or {}).items() if k.lower() not in ("range", "host")})
    if range_header:
        headers["Range"] = range_header
    proxies = {"http": entry["proxy"], "https": entry["proxy"]} if entry.get("proxy") else None
    return requests.get(
        entry["url"], headers=headers, stream=True, timeout=(10, 300),
        allow_redirects=True, proxies=proxies,
    )


_hits: dict[str, deque] = defaultdict(deque)
EXAMPLE_URL = "https://spankbang.com/abcde/video/example-title"


def _check_rate_limit(client_ip: str):
    now = time.time()
    window = _hits[client_ip]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= config.RATE_LIMIT_PER_MINUTE:
        raise HTTPException(status_code=429, detail="Rate limit exceeded, try again in a bit.")
    window.append(now)


@app.get("/", include_in_schema=False)
def root():
    return {
        "status": True,
        "creator": "SpankBang API",
        "message": "SpankBang Resolver API is online",
        "version": "1.0.0",
        "example": {
            "url": EXAMPLE_URL,
            "resolve": f"/api/spankbang?url={EXAMPLE_URL}",
        },
        "endpoints": {
            "resolve": "/api/spankbang?url=<SPANKBANG_VIDEO_URL>",
            "stream": "/stream/<code> (from each MP4 link's stream_url)",
            "hls": "/hls/<code>/master.m3u8 (from each m3u8 link's stream_url)",
            "health": "/health",
        },
    }


@app.get("/health")
def health():
    return {
        "status": True,
        "service": "spankbang-api",
        "provider": "SpankBang",
        # False => yt-dlp can't use curl_cffi here (unsupported curl_cffi version?)
        "impersonation": spankbang_resolver.impersonation_available(),
    }


@app.get("/api/spankbang")
async def spankbang(request: Request, url: str = Query(..., description="SpankBang video link")):
    # SpankBang slugs use "+" between words. If the caller didn't URL-encode the
    # link, "+" arrives here as a space, so restore it.
    url = url.strip().replace(" ", "+")
    if not spankbang_resolver.is_spankbang_link(url):
        return JSONResponse(
            status_code=400,
            content={"status": False, "error": "Only SpankBang links are supported here."},
        )
    try:
        data = await run_in_threadpool(spankbang_resolver.resolve_spankbang, url)
    except Exception as e:
        # BUG FIX: str(e) can be empty for some exception types (e.g. a
        # bare-raised exception with no message, or some urllib3/requests
        # errors whose __str__ returns ""), which used to produce a
        # useless "SpankBang resolve failed for ...: " log line with
        # nothing after the colon and an equally empty {"error": ""} in
        # the response -- confirmed from a real log showing exactly that.
        # Fall back to the exception's type name (and repr as a second
        # fallback) so there's always something to actually debug from.
        detail = str(e) or repr(e) or "unknown error"
        # BUG FIX: an AssertionError (or similar bare exception) has no
        # useful str()/repr() at all -- confirmed from a real log showing
        # exactly "[AssertionError] AssertionError()" and nothing else,
        # which says WHAT failed but not WHERE inside yt-dlp's extractor
        # it happened. The full traceback is what actually pinpoints that;
        # logging it here (server-side only, never in the API response)
        # costs nothing on the happy path and is the difference between
        # guessing and knowing the next time this kind of bare exception
        # comes up.
        logger.warning(
            "SpankBang resolve failed for %s: [%s] %s\n%s",
            url, type(e).__name__, detail, traceback.format_exc(),
        )
        return JSONResponse(status_code=502, content={"status": False, "error": f"{type(e).__name__}: {detail}"})
    proxy = data.pop("_proxy", None)
    base = _public_base(request)
    title = (data.get("videoDetails") or {}).get("title")
    # Drop non-video entries (cover_image, stream_sheet, thumbnail, images...)
    _bad_ext = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".vtt", ".json")
    _bad_names = {"cover_image", "stream_sheet", "thumbnail", "thumb", "preview"}
    data["links"] = [
        l for l in data.get("links", [])
        if (l.get("title") or "").strip().lower() not in _bad_names
        and not (l.get("url") or "").split("?")[0].lower().endswith(_bad_ext)
    ]
    for link in data.get("links", []):
        if link.get("url"):
            raw_url = link["url"]
            code = _register_stream(raw_url, _safe_name(title, link.get("title")), proxy, headers=link.pop("_headers", None))
            # Expose only our stream URL; never return the raw CDN URL.
            link["stream_url"] = f"{base}/stream/{code}"
            link.pop("url", None)
            # Keep the public quality label compact: 240p, 480p, 720p, etc.
            link["title"] = link.get("title", "").replace("Video ", "", 1)
    # Qualities that exist ONLY as HLS (e.g. 360p) used to be lost because the
    # whole m3u8_links list was dropped. Keep them, served through the /hls proxy.
    def _h(title):
        m = re.search(r"(\d{3,4})\s*p", title or "", re.I)
        return int(m.group(1)) if m else None

    have = {_h(l.get("title")) for l in data.get("links", [])} - {None}
    for m in data.get("m3u8_links") or []:
        h = m.get("height") or _h(m.get("title"))
        if not m.get("url") or not h or h in have:
            continue
        code = _register_stream(m["url"], _safe_name(title, f"{h}p"), proxy, kind="hls")
        data["links"].append({"title": f"{h}p", "format": "hls", "stream_url": f"{base}/hls/{code}/master.m3u8"})
        have.add(h)

    # Keep qualities in sequence: 240p, 480p, 720p, 1080p ... (lowest -> highest).
    # Labels without a number (e.g. "Video") go last. For highest-first, use reverse=True
    # on the numbered part.
    def _quality_key(link):
        m = re.search(r"(\d{3,4})\s*p", link.get("title") or "", re.I)
        return (0, int(m.group(1))) if m else (1, 0)

    data["links"].sort(key=_quality_key)
    # The resolver can return the same quality twice: once in `links` and
    # once in `m3u8_links`. The public API exposes one quality only, using
    # the progressive /stream links above. Keep the HLS proxy implementation
    # available, but do not duplicate qualities in the JSON response.
    data.pop("m3u8_links", None)
    return {"status": True, "data": data, "credit": "Ak"}


@app.api_route("/stream/{code}", methods=["GET", "HEAD"], include_in_schema=False)
async def stream(code: str, request: Request):
    with _stream_lock:
        entry = _stream_registry.get(code)
    if not entry or entry.get("kind", "mp4") != "mp4" or time.time() - entry["ts"] > STREAM_TTL:
        return JSONResponse(status_code=404, content={"status": False, "error": "Stream not found or expired."})
    try:
        upstream = await run_in_threadpool(_open_upstream, entry, request.headers.get("range", ""))
    except Exception as e:
        logger.warning("Stream fetch failed for %s: %s", code, e)
        return JSONResponse(status_code=502, content={"status": False, "error": f"Upstream fetch failed: {type(e).__name__}: {e}"})
    if upstream.status_code >= 400:
        code_up = upstream.status_code
        upstream.close()
        return JSONResponse(status_code=502, content={
            "status": False,
            "error": f"CDN returned HTTP {code_up} (link expired, or resolved from a different IP than this server now uses).",
        })
    headers = {
        "Content-Disposition": f'inline; filename="{entry["name"]}"',
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-cache",
    }
    for h in ("Content-Length", "Content-Range"):
        if upstream.headers.get(h):
            headers[h] = upstream.headers[h]
    media_type = upstream.headers.get("Content-Type", "video/mp4")
    if request.method == "HEAD":
        upstream.close()
        return StreamingResponse(iter(()), status_code=upstream.status_code, headers=headers, media_type=media_type)

    def body():
        try:
            for chunk in upstream.iter_content(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=headers, media_type=media_type)


# ---------------------------------------------------------------------------
# HLS proxy (m3u8 quality links). See hls_proxy.py for the design notes.
#   /hls/<code>/master.m3u8      the quality's master/media playlist, rewritten
#   /hls/<code>/p/<token>[.ext]  any URL found inside a playlist (variant
#                                playlist, key, init segment, media segment);
#                                <token> is the upstream URL, base64url-encoded
# Playlists come back rewritten (text); everything else is piped through
# with Range pass-through. CORS is open so web players (hls.js etc.) work.
# ---------------------------------------------------------------------------
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Range, Content-Type",
    "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
}
_HLS_MIME = "application/vnd.apple.mpegurl"


def _hls_error(status: int, message: str):
    return JSONResponse(status_code=status, content={"status": False, "error": message}, headers=_CORS_HEADERS)


def _get_hls_entry(code: str):
    with _stream_lock:
        entry = _stream_registry.get(code)
    if not entry or entry.get("kind") != "hls" or time.time() - entry["ts"] > STREAM_TTL:
        return None
    return entry


async def _hls_serve(entry: dict, code: str, upstream_url: str, request: Request):
    # Never forward Range for playlists -- a partial playlist is useless.
    range_header = "" if hls_proxy.is_playlist_url(upstream_url) else request.headers.get("range", "")
    try:
        upstream, final_url = await run_in_threadpool(hls_proxy.open_upstream, entry, upstream_url, range_header)
    except hls_proxy.HostNotAllowed as e:
        logger.warning("HLS proxy refused host %s (code %s)", e, code)
        return _hls_error(403, f"Host not allowed for the HLS proxy: {e}")
    except Exception as e:
        logger.warning("HLS upstream fetch failed for %s: %s: %s", code, type(e).__name__, e)
        return _hls_error(502, f"Upstream fetch failed: {type(e).__name__}: {e}")

    if upstream.status_code >= 400:
        status_up = upstream.status_code
        upstream.close()
        return _hls_error(
            502,
            f"CDN returned HTTP {status_up} (link expired, or resolved from a different IP than this server now uses).",
        )

    content_type = upstream.headers.get("Content-Type", "")

    if hls_proxy.looks_like_playlist(final_url, content_type):
        try:
            raw = await run_in_threadpool(hls_proxy.read_capped, upstream)
        except Exception as e:
            return _hls_error(502, f"Could not read playlist: {type(e).__name__}: {e}")
        finally:
            upstream.close()
        text = raw.decode("utf-8", errors="replace")
        if not text.lstrip("\ufeff \t\r\n").startswith("#EXTM3U"):
            return _hls_error(502, "Upstream did not return a valid HLS playlist.")
        headers = {"Cache-Control": "no-cache", **_CORS_HEADERS}
        if request.method == "HEAD":
            return Response(status_code=200, headers=headers, media_type=_HLS_MIME)
        body = hls_proxy.rewrite_playlist(text.lstrip("\ufeff"), final_url, code)
        return Response(content=body, media_type=_HLS_MIME, headers=headers)

    # Media segment / key / init section: pipe it through.
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache", **_CORS_HEADERS}
    if upstream.headers.get("Content-Range"):
        headers["Content-Range"] = upstream.headers["Content-Range"]
    # Content-Length only when the body isn't content-encoded (requests
    # transparently decodes gzip, which would make the length wrong).
    if upstream.headers.get("Content-Length") and not upstream.headers.get("Content-Encoding"):
        headers["Content-Length"] = upstream.headers["Content-Length"]
    media_type = content_type or "application/octet-stream"
    if request.method == "HEAD":
        status_up = upstream.status_code
        upstream.close()
        return StreamingResponse(iter(()), status_code=status_up, headers=headers, media_type=media_type)

    def body():
        try:
            for chunk in upstream.iter_content(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=headers, media_type=media_type)


@app.api_route("/hls/{code}/master.m3u8", methods=["GET", "HEAD"], include_in_schema=False)
async def hls_master(code: str, request: Request):
    entry = _get_hls_entry(code)
    if not entry:
        return _hls_error(404, "Stream not found or expired.")
    return await _hls_serve(entry, code, entry["url"], request)


@app.api_route("/hls/{code}/p/{token}", methods=["GET", "HEAD"], include_in_schema=False)
async def hls_part(code: str, token: str, request: Request):
    entry = _get_hls_entry(code)
    if not entry:
        return _hls_error(404, "Stream not found or expired.")
    try:
        upstream_url = hls_proxy.token_to_url(token)
    except Exception:
        return _hls_error(400, "Malformed HLS token.")
    return await _hls_serve(entry, code, upstream_url, request)


@app.options("/hls/{path:path}", include_in_schema=False)
async def hls_options(path: str):
    return Response(status_code=204, headers=_CORS_HEADERS)


@app.middleware("http")
async def rate_limit_mw(request, call_next):
    if request.url.path.startswith(("/stream/", "/hls/")):
        return await call_next(request)  # players issue many Range / segment requests
    client_ip = request.client.host if request.client else "unknown"
    try:
        _check_rate_limit(client_ip)
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"status": False, "error": e.detail})
    return await call_next(request)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=config.PORT, reload=False)
