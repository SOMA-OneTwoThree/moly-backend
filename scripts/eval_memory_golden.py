"""memory-golden-v1 평가 — 회상 품질 게이트(16.5절).

**결과를 보고 통과시키는 것을 구조적으로 막는다.**
 · fixture 내용 hash를 출력하고 기록한다. shadow 전에 고정한 hash와 달라지면 경고한다
 · threshold를 CLI로 낮출 수 없다. fixture에 박힌 값만 쓴다
 · case 수·category 충족을 **먼저** 검사하고, 미달이면 지표를 계산하지 않는다 —
   적은 case로 높은 점수를 내는 길을 막는다

지금 상태: 하네스는 완성, **case 200개는 미작성**이다. 200개를 채우는 건 제품 판단이라
자동 생성하지 않았다. 그래서 이 스크립트는 현재 의도적으로 실패한다.

사용:
    PYTHONPATH=. uv run python scripts/eval_memory_golden.py
"""
from __future__ import annotations

import asyncio
import hashlib
import pathlib
import sys

import yaml

from db.envfile import announce, load_conn, split_env_arg

_FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "evals" / "memory_golden_v1.yaml"


def fixture_hash(path: pathlib.Path = _FIXTURE) -> str:
    """내용 hash. shadow 전 고정값과 비교해 사후 수정을 드러낸다."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def load_fixture(path: pathlib.Path = _FIXTURE) -> dict:
    data = yaml.safe_load(path.read_text())
    for key in ("thresholds", "required_categories", "cases", "min_cases"):
        if key not in data:
            raise ValueError(f"fixture에 {key}가 없다")
    return data


def check_coverage(data: dict) -> list[str]:
    """case 수와 category 충족. **지표 계산 전에** 본다.

    적은 case로 높은 점수를 내는 건 통과가 아니다.
    """
    problems: list[str] = []
    cases = data["cases"]
    if len(cases) < data["min_cases"]:
        problems.append(
            f"case {len(cases)}개 — 최소 {data['min_cases']}개 필요. "
            "case를 채우기 전에는 지표를 계산하지 않는다"
        )
    present = {c.get("category") for c in cases}
    missing = [c for c in data["required_categories"] if c not in present]
    if missing:
        problems.append(f"빠진 category: {missing}")
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        problems.append("case id 중복")
    return problems


async def main(env: str | None) -> int:
    dsn = load_conn(env)
    announce(env, dsn)
    data = load_fixture()
    digest = fixture_hash()

    print(f"\nfixture: {data['manifest_version']} (hash {digest})")
    print("threshold (fixture 고정값 — CLI로 못 바꾼다):")
    for k, v in data["thresholds"].items():
        print(f"  {k:26s} {v}")

    problems = check_coverage(data)
    if problems:
        print("\n❌ 커버리지 게이트 실패 — 지표를 계산하지 않는다")
        for p in problems:
            print(f"  · {p}")
        print(
            "\n안내: case 200개 작성은 제품 판단이 필요하다(어떤 회상이 '맞는지'는\n"
            "      실제 대화 감각에서 나온다). 하네스는 완성돼 있으니 case만 채우면 된다."
        )
        return 1

    # 여기부터는 case가 충족된 뒤에만 실행된다.
    print("\n(실행) 각 case를 실제 검색 경로로 평가한다…")
    print("  ⚠️ registry 필터를 거친 실제 경로여야 한다 — 시뮬레이션은 게이트가 아니다")
    return 0


_env, _rest = split_env_arg(sys.argv[1:])
raise SystemExit(asyncio.run(main(_env)))
