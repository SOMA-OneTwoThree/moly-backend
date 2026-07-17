"""캐피 날짜별 자기일기 CSV 템플릿 생성기.

오늘부터 N일치 행을 만든다 — diary_date·weather(기본 sunny)는 채워두고 content만 빈칸.
content에 일기를 써넣은 뒤 scripts/seed_capi_diaries.py로 DB에 반영한다.

사용:
  python scripts/make_capi_diary_template.py                 # 오늘부터 30일 → db/capi_diaries.csv
  python scripts/make_capi_diary_template.py --start 2026-07-17 --days 30 --out db/capi_diaries.csv
  python scripts/make_capi_diary_template.py --force         # 기존 파일 덮어쓰기(내용 날아감 주의)
"""
import argparse
import csv
import os
from datetime import date, datetime, timedelta

WEATHERS = ("sunny", "cloudy", "rainy", "windy")  # 참고용 — 기본은 sunny


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=date.today().isoformat(), help="시작일 YYYY-MM-DD(기본 오늘)")
    ap.add_argument("--days", type=int, default=30, help="생성할 일수(기본 30)")
    ap.add_argument("--weather", default="sunny", choices=WEATHERS, help="기본 날씨(기본 sunny)")
    ap.add_argument("--out", default="db/capi_diaries.csv", help="출력 CSV 경로")
    ap.add_argument("--force", action="store_true", help="기존 파일 덮어쓰기 허용")
    args = ap.parse_args()

    if os.path.exists(args.out) and not args.force:
        raise SystemExit(f"이미 존재: {args.out} — 덮어쓰려면 --force (작성한 내용 유실 주의)")

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["diary_date", "weather", "content"])
        for i in range(args.days):
            w.writerow([(start + timedelta(days=i)).isoformat(), args.weather, ""])

    print(f"생성 완료: {args.out} — {args.start}부터 {args.days}일치. content만 채워서 seed 스크립트로 반영하세요.")


if __name__ == "__main__":
    main()
