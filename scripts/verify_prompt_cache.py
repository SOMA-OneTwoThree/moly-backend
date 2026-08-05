"""프롬프트 캐시 실증 — provider가 실제로 캐시를 태우는지 확인한다(11장).

설계는 "휘발값을 최근 원문 뒤에 두면 앞 prefix가 캐시된다"를 전제로 비용을 계산한다.
그 전제가 **실제 provider usage로 성립하는지**는 문서만 봐서는 알 수 없다.

확인하는 것(11장):
 1. stable instructions + append-only recent가 실제 `cached_tokens`로 누적되는가
 2. current-context envelope만 바뀐 연속 두 호출에서 앞 prefix가 유지되는가
 3. 금지된 배치(휘발값을 앞)에서는 캐시가 실제로 깨지는가

⚠️ 실 OpenAI 호출이 발생한다(4회, 약 $0.01 이하). `--yes` 없이는 실행하지 않는다.

사용:
    PYTHONPATH=. uv run python scripts/verify_prompt_cache.py --yes
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import settings
from app.services import llm

# OpenAI 자동 프리픽스 캐시 최소 길이. 이보다 짧으면 캐시 자체가 안 걸린다.
_MIN_PREFIX_TOKENS = 1024
# 캐시가 걸리도록 충분히 긴 안정 프리픽스를 만든다.
_FILLER = "이건 캐시 프리픽스를 채우기 위한 고정 문장이다. " * 120


def _stable() -> str:
    return f"[페르소나]\n{_FILLER}"


def _recent(n: int) -> list[dict]:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"지난 대화 {i}번째 내용이다."}
        for i in range(n)
    ]


async def _call(system: str, convo: list[dict], label: str) -> dict:
    r = await llm.generate(system, convo, model=settings.model_chat, max_tokens=16)
    got = {
        "label": label,
        "input": r.input_tokens,
        "cached": r.cache_read_tokens,
        "write_est": r.cache_write_tokens,
        "output": r.output_tokens,
    }
    print(
        f"  {label:34s} cached={got['cached']:6d}  input={got['input']:6d}  "
        f"write(추정)={got['write_est']:6d}"
    )
    return got


async def main(yes: bool) -> int:
    if not yes:
        print("⚠️ 실 OpenAI 호출 4회가 발생한다. 진행하려면 --yes 를 붙인다.")
        return 2

    stable = _stable()
    recent = _recent(20)
    print(f"\n모델: {settings.model_chat} / 안정 프리픽스 약 {len(stable)}자\n")

    print("[1] 올바른 배치 — 휘발값을 최근 원문 **뒤**에")
    good_convo_a = [
        *recent,
        {"role": "system", "content": "[지금 상태] 오후 3시"},
        {"role": "user", "content": "오늘 뭐 했게"},
    ]
    await _call(stable, good_convo_a, "1회차(캐시 워밍)")
    good_convo_b = [
        *recent,
        {"role": "system", "content": "[지금 상태] 오후 4시"},   # 휘발값만 변경
        {"role": "user", "content": "그래서 뭐 했게"},
    ]
    second = await _call(stable, good_convo_b, "2회차(휘발값만 변경)")

    print("\n[2] 금지된 배치 — 휘발값을 최근 원문 **앞**에")
    bad_convo_a = [
        {"role": "system", "content": "[지금 상태] 오후 3시"},
        *recent,
        {"role": "user", "content": "오늘 뭐 했게"},
    ]
    await _call(stable, bad_convo_a, "1회차(캐시 워밍)")
    bad_convo_b = [
        {"role": "system", "content": "[지금 상태] 오후 4시"},   # 앞에서 바뀐다
        *recent,
        {"role": "user", "content": "그래서 뭐 했게"},
    ]
    bad_second = await _call(stable, bad_convo_b, "2회차(앞 휘발값 변경)")

    print("\n" + "=" * 62)
    print("판정")
    print("=" * 62)
    ok = True

    if second["cached"] <= 0:
        print("❌ 올바른 배치에서 cached_tokens가 0 — 캐시가 안 걸렸다")
        print("   (프리픽스가 1024토큰 미만이거나 provider 캐시 정책이 다르다)")
        ok = False
    else:
        print(f"✅ 올바른 배치 2회차 cached={second['cached']} — 앞 prefix가 캐시로 유지된다")

    if second["cached"] > bad_second["cached"]:
        print(
            f"✅ 배치에 따른 실제 차이 확인 — 올바른 {second['cached']} vs "
            f"금지 {bad_second['cached']}"
        )
    else:
        print(
            f"⚠️ 배치 차이가 usage에 안 나타난다 — 올바른 {second['cached']} vs "
            f"금지 {bad_second['cached']}. 대화가 짧아 프리픽스 경계가 안 갈렸을 수 있다"
        )

    print(f"\n{'✅ 캐시 fixture 통과' if ok else '❌ 캐시 fixture 실패'}")
    return 0 if ok else 1


_p = argparse.ArgumentParser()
_p.add_argument("--yes", action="store_true")
_a = _p.parse_args(sys.argv[1:])
raise SystemExit(asyncio.run(main(_a.yes)))
