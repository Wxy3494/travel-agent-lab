from pathlib import Path

import httpx

from app.photos import MAX_PHOTO_BYTES, fetch_photo, gallery_html, photo_url, poi_photos
from app.providers import AmapProvider

ROOT = Path(__file__).resolve().parents[1]


def test_photo_urls_are_provider_hosted_https_and_credential_free():
    assert photo_url("http://store.is.autonavi.com/showpic/one") == "https://store.is.autonavi.com/showpic/one"
    for url in ("http://127.0.0.1/private", "file:///secret", "javascript:alert(1)", "https://evil.com/a.jpg",
                "https://store.is.autonavi.com/a?key=secret", "https://user:secret@store.is.autonavi.com/a"):
        assert photo_url(url) is None
    assert poi_photos({"photos": [{"url": "http://store.is.autonavi.com/showpic/one"}, {"url": "javascript:bad"}]}) == ["https://store.is.autonavi.com/showpic/one"]


def test_real_image_bytes_verify_broken_oversize_and_redirect_fail_gracefully():
    image = (ROOT / "app/assets/coastal-evening.jpg").read_bytes()
    seen = []
    def respond(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=image, headers={"Content-Type": "image/jpeg"})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = fetch_photo("https://store.is.autonavi.com/photo/a", client)
    assert result[0] == "image/jpeg" and result[1] == image
    html = gallery_html('酒店"<script>', [result])
    assert 'data:image/jpeg;base64,' in html and '<script>' not in html
    for response in (httpx.Response(200, text="<html>blocked</html>", headers={"Content-Type": "image/jpeg"}),
                     httpx.Response(200, content=b"x" * (MAX_PHOTO_BYTES + 1), headers={"Content-Type": "image/jpeg"}),
                     httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})):
        with httpx.Client(transport=httpx.MockTransport(lambda r, response=response: response)) as client:
            assert fetch_photo("https://store.is.autonavi.com/photo/b", client) is None


async def test_provider_keeps_photos_and_complete_hotel_address(constraints):
    photo = "https://store.is.autonavi.com/showpic/real-point"
    point = {"id": "photo-poi", "name": "具体分店", "location": "113.1,23.1", "adcode": "440106",
             "typecode": "100000", "address": "中山路88号2楼", "photos": [{"url": photo}]}
    def respond(request):
        body = {"geocodes": [{"location": "113.1,23.1", "adcode": "440100", "citycode": "020"}]} if "geocode" in request.url.path else {"pois": [point]}
        return httpx.Response(200, json={"status": "1", **body})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = AmapProvider("test", client=client)
        choices = await provider.search("广州", "具体分店", constraints, "hotel")
        saved = await provider.by_id("广州", "photo-poi", constraints, "hotel")
    assert choices[0].photos == [photo] and saved.address == "中山路88号2楼" and saved.photos == [photo]


async def test_autonomous_hotel_and_photos_survive_storage_and_drive_actual_routes(service, constraints):
    from app.providers import FixtureProvider
    from app.schemas import TripRequest

    class PhotoProvider(FixtureProvider):
        async def city(self, city, constraints):
            facts = await super().city(city, constraints)
            for point in facts.places + facts.hotels:
                point.photos = [f"https://store.is.autonavi.com/showpic/{point.id}"]
            return facts

    service.provider_factory = PhotoProvider
    trip = await service.create(TripRequest(constraints=constraints, autonomous=True))
    saved = service.repository.get(trip.id)
    first = saved.days[0]
    assert first.suggested_stay and first.suggested_stay.category == "hotel"
    first_travel = next(e for e in first.events if e.kind == "travel")
    assert first_travel.start_location == first.suggested_stay.location
    for event in first.events:
        if event.kind == "visit":
            assert event.photos == [f"https://store.is.autonavi.com/showpic/{event.place_id}"]
    assert first.suggested_stay.photos == [f"https://store.is.autonavi.com/showpic/{first.suggested_stay.id}"]
