"""One server clock and local calendar day shared by banners and routines."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from app.core.errors import AppError
from app.core.time_utils import is_valid_iana_timezone, safe_zone


def validate_app_timezone(value: str | None) -> str | None:
    if value is not None and (not 1 <= len(value) <= 64 or not is_valid_iana_timezone(value)):
        raise AppError("APP_TIMEZONE_INVALID", 422, "시간대 형식이 올바르지 않습니다.")
    return value


@dataclass(frozen=True)
class AppDay:
    local_date: date
    served_at: datetime
    ends_at: datetime

    @classmethod
    def at(cls, now: datetime, timezone_name: str) -> "AppDay":
        if now.tzinfo is None:
            raise ValueError("server clock must be timezone aware")
        zone = safe_zone(timezone_name)
        local_date = now.astimezone(zone).date()
        end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=zone)
        return cls(local_date, now.astimezone(timezone.utc), end.astimezone(timezone.utc))

    def headers(self) -> dict[str, str]:
        return {
            "X-App-Local-Date": self.local_date.isoformat(),
            "X-App-Served-At": self.served_at.isoformat().replace("+00:00", "Z"),
            "X-App-Day-Ends-At": self.ends_at.isoformat().replace("+00:00", "Z"),
        }
