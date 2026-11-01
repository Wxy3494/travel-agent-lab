"""Photos tied to a provider's exact POI; no generic or generated substitutes."""
import base64
import html
from io import BytesIO
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx
from PIL import Image

MAX_PHOTO_BYTES = 2_500_000


def photo_url(value):
    if not isinstance(value, str):
        return None
    try:
        p = urlsplit(value)
        host = (p.hostname or "").lower()
        if (p.scheme not in {"http", "https"} or p.username or p.password or p.port not in {None, 80, 443}
                or not host.endswith((".autonavi.com", ".amap.com"))
                or p.fragment or {k.lower() for k in parse_qs(p.query)} & {"key", "token", "api_key", "access_token"}):
            return None
        return urlunsplit(("https", host, p.path, p.query, ""))
    except ValueError:
        return None


def poi_photos(point):
    rows = point.get("photos", [])
    if not isinstance(rows, list):
        return []
    return list(dict.fromkeys(url for row in rows if isinstance(row, dict) and (url := photo_url(row.get("url")))))[:4]


def fetch_photo(url, client=None):
    """Bound downloads; failed/missing pictures do not break the itinerary."""
    url = photo_url(url)
    if not url:
        return None
    own = client is None
    client = client or httpx.Client(timeout=4, follow_redirects=False)
    try:
        for _ in range(3):
            with client.stream("GET", url) as response:
                if response.is_redirect:
                    url = photo_url(response.headers.get("location"))
                    if not url:
                        return None
                    continue
                response.raise_for_status()
                if response.headers.get("content-type", "").split(";", 1)[0] not in {"image/jpeg", "image/png", "image/webp"}:
                    return None
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_PHOTO_BYTES:
                        return None
                with Image.open(BytesIO(data)) as picture:
                    if picture.format not in {"JPEG", "PNG", "WEBP"} or picture.width * picture.height > 12_000_000:
                        return None
                    mime = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}[picture.format]
                    picture.verify()
                return mime, bytes(data)
    except (httpx.HTTPError, OSError, ValueError, Image.DecompressionBombError):
        return None
    finally:
        if own:
            client.close()
    return None


def gallery_html(name, pictures):
    images = "".join(f'<img src="data:{mime};base64,{base64.b64encode(data).decode()}" alt="{html.escape(name, quote=True)}实景" loading="lazy">'
                     for mime, data in pictures[:2])
    return f'<div class="place-gallery">{images}</div>' if images else ""
