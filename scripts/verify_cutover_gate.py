"""기존 운영 명령 호환용 진입점. 현재 구조·복구 지표 검사는 preflight_cutover가 소유한다.

완료된 일회성 전환의 사용자 수·6시간 soak·빈 값 기준을 현재 서비스 승인 조건으로 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.envfile import split_env_arg  # noqa: E402
from scripts.preflight_cutover import main  # noqa: E402

if __name__ == '__main__':
    env, rest = split_env_arg(sys.argv[1:])
    argparse.ArgumentParser(description=__doc__).parse_args(rest)
    raise SystemExit(asyncio.run(main(env)))
