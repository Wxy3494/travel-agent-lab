"""Walking leg endpoints and branch identity must survive concise presentation."""
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.hotel_identity import address_key, confirm_hotels
from app.navigation import route_lines, walking_link
from app.providers import AmapProvider
from app.schemas import Place, TravelInfo, TripRequest
from app.travel_references import parse_references


def test_adjacent_walks_merge_but_navigation_uses_their_actual_endpoints():
    value = {"route_steps": [
        {"kind": "walk", "distance": 1549, "start_location": "113,23", "end_location": "113.01,23.01"},
        {"kind": "walk", "distance": 145, "start_location": "113.01,23.01", "end_location": "113.02,23.02"},
        {"kind": "transit", "text": "高铁G1"},
        {"kind": "walk", "distance": 100, "start_location": "114,24", "end_location": "114.01,24.01"}]}
    rows = route_lines(value)
    assert [r["text"] for r in rows] == ["步行共 1694 米", "高铁G1", "步行 100 米"]
    params = parse_qs(urlsplit(rows[0]["links"][0]).query)
    assert params["mode"] == ["walk"] and params["callnative"] == ["1"]
    assert params["from"][0].startswith("113.000000,23.000000,")
    assert params["to"][0].startswith("113.020000,23.020000,")
    value["route_steps"][1]["start_location"] = "113.015,23.015"
    assert len(route_lines(value)[0]["links"]) == 2  # Two disconnected legs cannot become a single walk.


def test_legacy_transit_walks_merge_without_fabricated_cross_city_walk_navigation():
    rows = route_lines({"route_segments": ["地铁1号线", "步行 1549 米", "步行 145 米", "高铁G1"],
        "start_location": "113,23", "end_location": "116,39", "note": "公交参考费用"})
    assert rows[1] == {"text": "步行共 1694 米", "links": []}
    assert walking_link("NaN,23", "113,23") is None
    assert walking_link("113,23&key=secret", "114,24") is None
    assert walking_link("113,23", "113,23") is None


async def test_provider_preserves_walk_before_bus_and_navigation_coordinates():
    data = {"status": "1", "route": {"transits": [{"distance": "2000", "cost": {"duration": "600", "transit_fee": "3"},
        "segments": [{"walking": {"distance": "145", "origin": "113.1,23.1", "destination": "113.11,23.11"},
            "bus": {"buslines": [{"name": "地铁1号线", "departure_stop": {"name": "A站"}, "arrival_stop": {"name": "B站"}}]}},
            {"walking": {"distance": "80", "steps": [{"polyline": "113.2,23.2;113.21,23.21"}]}}]}]}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))) as client:
        result = await AmapProvider("test-never-output", client=client).route("113,23", "114,24", "020", "020", "public")
    assert [s["kind"] for s in result["steps"]] == ["walk", "transit", "walk"]
    rows = route_lines({"route_steps": result["steps"]})
    assert "113.100000,23.100000" in parse_qs(urlsplit(rows[0]["links"][0]).query)["from"][0]
    assert "113.210000,23.210000" in parse_qs(urlsplit(rows[2]["links"][0]).query)["to"][0]


async def test_route_steps_survive_storage_and_export_without_raw_source_lists(service, constraints):
    from app.providers import FixtureProvider
    from app.render import render_markdown
    class WalkFixture(FixtureProvider):
        async def route(self, *args):
            result = await super().route(*args)
            result["steps"] = [{"kind": "walk", "distance": 145, "start_location": "113,23", "end_location": "113.01,23.01"},
                {"kind": "walk", "distance": 80, "start_location": "113.01,23.01", "end_location": "113.02,23.02"}]
            return result
    service.provider_factory = WalkFixture
    result = await service.create(TripRequest(constraints=constraints))
    saved = service.repository.get(result.id)
    assert any(e.route_steps for d in saved.days for e in d.events)
    saved.evidence[0].description = "RAW_SOURCE_SENTINEL"
    text = render_markdown(saved)
    assert "步行共 225 米" in text and "mode=walk" in text
    assert "RAW_SOURCE_SENTINEL" not in text and "## 来源与数据类型" not in text


@pytest.mark.parametrize("variant", ["unique", "intro", "two_pois", "wrong_poi", "floor", "missing_address"])
async def test_different_branch_name_needs_complete_address_and_unique_amap_poi(variant):
    target = {"place_id": "same-store", "name": "南宁初见酒店(北站店)", "city": "南宁", "kind": "hotel",
              "dates": ["2026-11-07"], "address": "灵源路82号2楼", "location": "108.1,23.1"}
    supplier = "南宁初见酒店（康和城店）"
    address = "南宁市武鸣区灵源路82号3楼" if variant == "floor" else "南宁市武鸣区灵源路82号2楼"
    content = f"### {supplier}\n酒店地址：{address}\n经济双床房 88元/晚。"
    if variant == "missing_address":
        content = f"### {supplier}\n经济双床房 88元/晚。"
    elif variant == "intro":
        content = f"已查询**{supplier}**。\n酒店地址：{address}\n## 经济双床房\n88元/晚。"
    report = TravelInfo(status="success", content=content, references=parse_references(content, [target]))
    seen = []
    async def search(city, query, c, category):
        seen.append(query)
        ids = ["same-store", "another"] if variant == "two_pois" else ["another"] if variant == "wrong_poi" else ["same-store"]
        return [Place(id=pid, name=target["name"], city=city, address=target["address"], location=target["location"],
            category="hotel", evidence_id="amap") for pid in ids]
    provider = SimpleNamespace(search=search, record=lambda *args: "identity-evidence")
    await confirm_hotels(report, [target], provider, None)
    assert bool(report.references) is (variant in {"unique", "intro"})
    if variant in {"unique", "intro"}:
        assert report.references[0].identity_confirmed and report.references[0].place_id == "same-store"
        assert "88" in report.references[0].content
    if variant in {"floor", "missing_address"}:
        assert seen == []


def test_address_matching_keeps_house_number_floor_and_room():
    assert address_key("南宁市武鸣区灵源路82号2楼") == address_key("灵源路82号2楼")
    assert address_key("灵源路82号2楼") != address_key("灵源路82号3楼")
    assert address_key("康和城商业广场") is None
