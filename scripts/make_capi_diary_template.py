"""캐피 날짜/주간 원고 CSV 템플릿. 기존 파일은 --force 없이 덮어쓰지 않는다.

날짜: --start 2026-09-07 --days 30 (기본 db/capi_diaries.csv)
주간: --weekly --week-start 2026-09-07 --count 3 (기본 db/capi_diaries_weekly.csv)
content를 채운 행만 seed_capi_diaries.py에 입력한다.
"""
import argparse
import csv
import os
from datetime import date, timedelta

WEATHERS = ("sunny", "cloudy", "rainy", "windy")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weekly", action="store_true", help="주간 원고 템플릿 생성")
    ap.add_argument("--week-start", help="월요일 YYYY-MM-DD (--weekly 필수)")
    ap.add_argument("--count", type=int, help="주간 원고 편수 (--weekly 필수)")
    ap.add_argument("--start", help="시작일 YYYY-MM-DD(기본 오늘)")
    ap.add_argument("--days", type=int, help="생성할 일수(기본 30)")
    ap.add_argument("--weather", default="sunny", choices=WEATHERS)
    ap.add_argument("--out", help="출력 CSV 경로")
    ap.add_argument("--force", action="store_true", help="기존 파일 덮어쓰기 허용")
    args = ap.parse_args()
    if args.weekly:
        if args.start is not None or args.days is not None:
            ap.error("--weekly는 --start/--days와 함께 사용할 수 없습니다.")
        if args.week_start is None or args.count is None or args.count <= 0:
            ap.error("--weekly에는 --week-start와 양의 --count가 필요합니다.")
        raw_start, count = args.week_start, args.count
        out = args.out or "db/capi_diaries_weekly.csv"
    else:
        if args.week_start is not None or args.count is not None:
            ap.error("--week-start/--count는 --weekly와 함께 사용하세요.")
        raw_start, count = args.start or date.today().isoformat(), args.days or 30
        if args.days is not None and args.days <= 0:
            ap.error("--days는 양수여야 합니다.")
        out = args.out or "db/capi_diaries.csv"
    try:
        start = date.fromisoformat(raw_start)
        if start.isoformat() != raw_start:
            raise ValueError
    except ValueError:
        ap.error("날짜 형식은 YYYY-MM-DD여야 합니다.")
    if args.weekly and start.weekday() != 0:
        ap.error("--week-start는 월요일이어야 합니다.")
    if os.path.exists(out) and not args.force:
        raise SystemExit(f"이미 존재: {out} — 덮어쓰려면 --force (작성한 내용 유실 주의)")
    with open(out, "w" if args.force else "x", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if args.weekly:
            w.writerow(["week_start_date", "sequence_no", "weather", "content"])
            for i in range(count):
                w.writerow([start.isoformat(), i + 1, args.weather, ""])
        else:
            w.writerow(["diary_date", "weather", "content"])
            for i in range(count):
                w.writerow([(start + timedelta(days=i)).isoformat(), args.weather, ""])
    print(f"생성 완료: {out} — {count}행. content를 채워서 seed 스크립트로 반영하세요.")


if __name__ == "__main__":
    main()
