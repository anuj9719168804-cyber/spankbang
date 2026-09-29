# SpankBang Resolver API

Standalone FastAPI + yt-dlp service that resolves a SpankBang video URL into
playable quality links, served through this server's own `/stream` and `/hls`
proxy (CDN links are often bound to the resolving IP).

## Endpoints
- `GET /`              info
- `GET /health`        health + impersonation status
- `GET /api/spankbang?url=<SPANKBANG_VIDEO_URL>`   resolve
- `GET /stream/<code>` MP4 pipe (Range supported)
- `GET /hls/<code>/master.m3u8` HLS rewriting proxy

```bash
curl --get 'http://localhost:8000/api/spankbang' \
  --data-urlencode 'url=https://spankbang.com/abcde/video/example-title'
```

## Run
```bash
pip install -r requirements.txt
python app.py        # http://127.0.0.1:8000/docs
```
Docker: `docker build -t spankbang-api . && docker run -p 8000:8000 spankbang-api`

## Env vars
```env
PORT=8000
RATE_LIMIT_PER_MINUTE=30
PUBLIC_BASE_URL=            # optional, public https base of your deployment
SPANKBANG_COOKIES=          # optional Netscape cookies file path
SPANKBANG_PROXY=            # optional single proxy
SPANKBANG_PROXIES=          # optional comma-separated proxies
SPANKBANG_ALWAYS_PROXY=false
HLS_ALLOWED_HOST_SUFFIXES=spankbang.com,spankbang.party,sb-cd.com
```

## Notes
- SpankBang sits behind Cloudflare; if you get 403 / "just a moment" errors from
  a datacenter IP, use cookies or a residential proxy, and keep `yt-dlp` updated.
- Failures return HTTP 502 with the real yt-dlp error.
