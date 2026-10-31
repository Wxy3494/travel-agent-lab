"""Independent weather lookup, with a documented adapter for a later provider."""
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from pydantic import Field, ValidationError, model_validator

from app.providers import AmapProvider, ProviderError
from app.schemas import DayWeather, Model


def itinerary_weather(city, on_date, rows, *, today=None, demo=False):
    """Match the actual itinerary date within today's seven-day forecast window."""
    today = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    result = DayWeather(city=city, date=on_date)
    if not today <= on_date <= today + timedelta(days=6):
        return result
    row = next((r for r in rows if str(r.get("date")) == str(on_date)), None)
    if not row or not row.get("dayweather"):
        return result

    def temperature(key):
        try:
            value = float(row.get(key + "_float", row.get(key)))
            return value if -100 <= value <= 80 else None
        except (ValueError, TypeError):
            return None

    result.condition = str(row["dayweather"])
    result.high_c, result.low_c = temperature("daytemp"), temperature("nighttemp")
    result.source = row.get("source") or ("离线天气示例" if demo else "天气服务")
    result.updated_at = str(row.get("updated_at") or "")
    result.is_demo = demo or bool(row.get("is_demo"))
    result.available = True
    return result


class ForecastDay(Model):
    date: date
    day_condition: str = Field(max_length=100)
    night_condition: str = Field(default="", max_length=100)
    high_c: float | None = Field(default=None, ge=-100, le=80)
    low_c: float | None = Field(default=None, ge=-100, le=80)
    wind: str = Field(default="", max_length=100)

    @model_validator(mode="after")
    def temperatures(self):
        if self.high_c is not None and self.low_c is not None and self.low_c > self.high_c:
            raise ValueError("最低温度不能高于最高温度")
        return self


class WeatherReport(Model):
    city: str = Field(min_length=1, max_length=60)
    source: str = Field(min_length=1, max_length=100)
    updated_at: str = Field(default="", max_length=100)
    forecasts: list[ForecastDay] = Field(default_factory=list, max_length=30)
    is_demo: bool = False
    message: str = Field(default="", max_length=500)


class WeatherService:
    def __init__(self, settings, client=None):
        self.settings, self.client = settings, client
        self.cache = {}

    async def lookup(self, city, on_date=None):
        city = city.strip()
        if not city or len(city) > 60:
            raise ProviderError("weather_city_invalid", "请填写要查询天气的城市")
        cached = self.cache.get(city)
        if cached and time.monotonic() - cached[0] < 300:
            report = cached[1].model_copy(deep=True)
        else:
            try:
                report = await self.fetch(city)
            except (ValidationError, ValueError, TypeError, KeyError):
                raise ProviderError("weather_data_invalid", "天气服务返回的信息不完整，请稍后再试") from None
            self.cache[city] = (time.monotonic(), report.model_copy(deep=True))
        if on_date and not any(f.date == on_date for f in report.forecasts):
            report.message = f"天气服务尚未提供{on_date}的预报，请临近出行时再查询；下方仅显示已返回日期的天气。"
        elif not report.forecasts:
            report.message = "暂时没有可用预报，请稍后再查询。"
        return report

    async def fetch(self, city):
        if self.settings.mode == "demo":
            today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
            return WeatherReport(city=city, source="离线天气示例", is_demo=True,
                updated_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                forecasts=[ForecastDay(date=today + timedelta(days=i), day_condition="晴（演示）",
                    high_c=26, low_c=20, wind="示例微风") for i in range(4)],
                message="这些天气仅为演示，不代表真实预报。")
        if self.settings.weather_provider == "custom":
            return await self.custom(city)
        if self.settings.weather_provider != "amap":
            raise ProviderError("weather_not_configured", "天气服务尚未配置，请选择高德或接入自定义天气服务")
        provider = AmapProvider(self.settings.amap_key, max_calls=3, client=self.client)
        try:
            geo = await provider.locate(city)
            data = await provider.get("/v3/weather/weatherInfo", {"city": geo["adcode"], "extensions": "all"})
            forecast = next(iter(data.get("forecasts", [])), {})

            def temperature(value):
                return float(value) if value not in (None, "", []) else None

            rows = [ForecastDay(date=row["date"], day_condition=row.get("dayweather") or "未提供",
                    night_condition=row.get("nightweather") or "",
                    high_c=temperature(row.get("daytemp_float", row.get("daytemp"))),
                    low_c=temperature(row.get("nighttemp_float", row.get("nighttemp"))),
                    wind=" ".join(str(row[k]) for k in ("daywind", "daypower") if row.get(k)))
                    for row in forecast.get("casts", [])]
            return WeatherReport(city=city, source="高德天气", updated_at=forecast.get("reporttime") or "",
                                 forecasts=rows)
        finally:
            await provider.close()

    async def custom(self, city):
        url = self.settings.weather_api_url
        parsed = urlparse(url)
        if (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})
                or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ProviderError("weather_not_configured", "请配置有效的天气接口地址，密钥应单独填写")
        headers = {"Authorization": "Bearer " + self.settings.weather_api_key} if self.settings.weather_api_key else {}
        client = self.client or httpx.AsyncClient(timeout=12)
        try:
            response = await client.get(url, params={"city": city}, headers=headers)
            response.raise_for_status()
            report = WeatherReport.model_validate(response.json())
            if report.city != city or report.is_demo:
                raise ValueError("Unexpected city or simulated data from live weather adapter")
            return report
        except httpx.HTTPError:
            raise ProviderError("weather_unavailable", "天气服务暂时无法连接，请稍后重试或检查接口配置") from None
        finally:
            if self.client is None:
                await client.aclose()
