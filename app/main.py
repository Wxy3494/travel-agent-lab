from datetime import date

from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError

from app.config import Settings
from app.history import compare
from app.schemas import CloneRequest, Constraints, RevisionRequest, TripRequest, TripResult
from app.service import TravelService
from app.storage import Repository, VersionConflict
from app.weather import WeatherReport


def create_app(settings=None, service=None):
    settings = settings or Settings.from_env()
    service = service or TravelService(settings, Repository(settings.db_path))
    api = FastAPI(title="Travel Agent Lab", version="0.1.0", description="市内与跨城规划；所有金额为人民币全团估算")
    api.state.service = service

    @api.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Default FastAPI errors echo rejected input. Never echo potential credentials.
        return JSONResponse(status_code=422, content={"detail": "输入字段或约束无效，请检查填写内容",
                            "fields": [list(e["loc"]) for e in exc.errors()]})

    @api.get("/health")
    def health():
        return {"status": "ok", "mode": settings.mode,
                "request_timeout_seconds": settings.timeout,
                "live_keys_configured": bool(settings.model_key and settings.amap_key),
                "meituan_configured": bool(settings.meituan_enabled and settings.meituan_token),
                "auth": "local-only demo; do not expose publicly"}

    @api.post("/trips", response_model=TripResult)
    async def create(request: TripRequest):
        return await service.create(request)

    @api.get("/trips")
    def list_trips(query: str = Query(default="", max_length=100), limit: int = Query(default=50, ge=1, le=100),
                   offset: int = Query(default=0, ge=0)):
        return service.repository.list(query, limit, offset)

    @api.get("/trips/history")
    def trip_history(query: str = Query(default="", max_length=100), limit: int = Query(default=20, ge=1, le=100),
                     offset: int = Query(default=0, ge=0)):
        return {"items": service.repository.list(query, limit, offset), "total": service.repository.count(query),
                "offset": offset, "limit": limit}

    @api.get("/weather", response_model=WeatherReport)
    async def weather(city: str = Query(min_length=1, max_length=60), on_date: date | None = None):
        from app.names import CITY_ALIASES
        from app.providers import ProviderError
        try:
            return await service.weather_service.lookup(CITY_ALIASES.get(city.strip(), city.strip()), on_date)
        except ProviderError as exc:
            raise HTTPException(422, str(exc)) from None

    @api.get("/places/search")
    async def search_places(city: str = Query(max_length=60), query: str = Query(min_length=1, max_length=80),
                            category: str = Query(default="attraction", pattern="^(attraction|hotel|station)$")):
        from app.names import CITY_ALIASES
        from app.providers import ProviderError
        if not city.strip() or service.settings.mode == "live" and not service.settings.amap_key:
            raise HTTPException(422, "请填写城市并配置对应模式的地图服务")
        provider = service.provider_factory()
        try:
            city = CITY_ALIASES.get(city.strip(), city.strip())
            c = Constraints(origin=city, cities=[city])
            places = await provider.search(city, query.strip(), c, category)
            return {"options": [p.model_dump(mode="json") for p in places],
                    "evidence": [e.model_dump(mode="json") for e in provider.evidence.values()]}
        except ProviderError as exc:
            raise HTTPException(422, str(exc)) from None
        finally:
            await provider.close()

    @api.get("/trips/{trip_id}/versions")
    def versions(trip_id: str):
        values = service.repository.versions(trip_id)
        if not values:
            raise HTTPException(404, "行程不存在")
        return values

    @api.get("/trips/{trip_id}/compare")
    def difference(trip_id: str, from_version: int = Query(ge=1), to_version: int = Query(ge=1)):
        before, after = service.repository.get(trip_id, from_version), service.repository.get(trip_id, to_version)
        if not before or not after:
            raise HTTPException(404, "行程或版本不存在")
        return compare(before, after)

    @api.post("/trips/{trip_id}/clone", response_model=TripResult)
    async def clone(trip_id: str, request: CloneRequest):
        old = service.repository.get(trip_id, request.version)
        if not old:
            raise HTTPException(404, "行程或版本不存在")
        return await service.create(TripRequest(constraints=old.constraints, strategy=old.strategy))

    @api.get("/trips/{trip_id}", response_model=TripResult)
    def get(trip_id: str, version: int | None = Query(default=None, ge=1)):
        result = service.repository.get(trip_id, version)
        if not result:
            raise HTTPException(404, "行程或版本不存在")
        return result

    @api.post("/trips/{trip_id}/revise", response_model=TripResult)
    async def revise(trip_id: str, request: RevisionRequest):
        try:
            result = await service.revise(trip_id, request)
        except VersionConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValidationError:
            raise HTTPException(422, "修改字段或约束无效，请检查日期、人数、预算和字段名") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if result is None:
            raise HTTPException(404, "行程不存在")
        return result

    @api.post("/trips/{trip_id}/verify-visits", response_model=TripResult)
    async def verify_visits(trip_id: str, request: RevisionRequest):
        if request.changes or request.instruction or request.adjustment_id or request.target_day:
            raise HTTPException(422, "重新核验只需提交当前版本号")
        try:
            result = await service.refresh_visits(trip_id, request.base_version)
        except VersionConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if result is None:
            raise HTTPException(404, "行程不存在")
        return result

    @api.get("/trips/{trip_id}/export", response_class=PlainTextResponse)
    def export(trip_id: str, version: int | None = Query(default=None, ge=1)):
        result = service.repository.get(trip_id, version)
        if not result:
            raise HTTPException(404, "行程或版本不存在")
        from app.render import render_markdown
        return PlainTextResponse(render_markdown(result), media_type="text/markdown; charset=utf-8")

    return api


app = create_app()
