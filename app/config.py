import math
import os
from dataclasses import dataclass

from dotenv import load_dotenv

DEFAULT_PLANNING_TIMEOUT = 300
MAX_PLANNING_TIMEOUT = 600
CLIENT_TIMEOUT_MARGIN = 30


def planning_timeout(value=None):
    value = float(os.getenv("TRAVEL_TIMEOUT_SECONDS", DEFAULT_PLANNING_TIMEOUT) if value is None else value)
    if not math.isfinite(value) or not 0 < value <= MAX_PLANNING_TIMEOUT:
        raise ValueError("Timeout must be finite and between 0 and 600 seconds")
    return value


def client_read_timeout(backend_timeout=None):
    # Backend timeout includes planning; leave time for cleanup and response delivery.
    return planning_timeout(backend_timeout) + CLIENT_TIMEOUT_MARGIN


@dataclass(frozen=True)
class Settings:
    mode: str = "demo"
    db_path: str = "data/runtime/trips.sqlite3"
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    model_key: str = ""
    amap_key: str = ""
    timeout: float = DEFAULT_PLANNING_TIMEOUT
    max_repairs: int = 2
    max_tool_calls: int = 300
    max_model_calls: int = 30
    input_price: float | None = None
    output_price: float | None = None
    weather_provider: str = "amap"
    weather_api_url: str = ""
    weather_api_key: str = ""

    def __post_init__(self):
        if self.mode not in {"demo", "live"}:
            raise ValueError("TRAVEL_MODE must be demo or live")
        planning_timeout(self.timeout)
        if not 0 <= self.max_repairs <= 2 or not 1 <= self.max_tool_calls <= 500:
            raise ValueError("Invalid execution limits")
        if not 1 <= self.max_model_calls <= 50:
            raise ValueError("Invalid model request limit")
        if any(p is not None and (not math.isfinite(p) or p < 0) for p in (self.input_price, self.output_price)):
            raise ValueError("Unit prices must be finite and nonnegative")

    @classmethod
    def from_env(cls):
        load_dotenv()
        def price(name):
            value = os.getenv(name, "").strip()
            return float(value) if value else None
        result = cls(
            mode=os.getenv("TRAVEL_MODE", "demo"), db_path=os.getenv("TRAVEL_DB", cls.db_path),
            model=os.getenv("MODEL_NAME", cls.model), base_url=os.getenv("MODEL_BASE_URL", cls.base_url),
            model_key=os.getenv("DEEPSEEK_API_KEY", ""), amap_key=os.getenv("AMAP_API_KEY", ""),
            timeout=planning_timeout(),
            max_repairs=int(os.getenv("TRAVEL_MAX_REPAIRS", "2")),
            max_tool_calls=int(os.getenv("TRAVEL_MAX_TOOL_CALLS", "300")),
            max_model_calls=int(os.getenv("TRAVEL_MAX_MODEL_CALLS", "30")),
            input_price=price("MODEL_INPUT_PRICE_PER_MILLION"),
            output_price=price("MODEL_OUTPUT_PRICE_PER_MILLION"),
            weather_provider=os.getenv("WEATHER_PROVIDER", "amap"),
            weather_api_url=os.getenv("WEATHER_API_URL", ""),
            weather_api_key=os.getenv("WEATHER_API_KEY", ""),
        )
        if result.mode not in {"demo", "live"}:
            raise ValueError("TRAVEL_MODE must be demo or live")
        if not 0 <= result.max_repairs <= 2 or not 1 <= result.max_tool_calls <= 500:
            raise ValueError("Invalid execution limits")
        return result
