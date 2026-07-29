"""Postgres 오류 판별 — asyncpg가 SQLAlchemy IntegrityError로 래핑한 UNIQUE 위반 구분(SOMA-372).

충돌 가능 INSERT를 savepoint(begin_nested)로 감싸고, 잡은 IntegrityError가 '예상한' 특정
UNIQUE 제약 위반인지(멱등 처리) 아니면 다른 오류인지(재던짐 → inbox pending·실오류) 구분한다.
광범위 except로 모든 IntegrityError를 삼키면 NULL/FK 위반 같은 실버그가 은폐되므로 제약명으로 좁힌다.
"""
from __future__ import annotations

from sqlalchemy.exc import IntegrityError

_UNIQUE_VIOLATION = "23505"  # PostgreSQL unique_violation SQLSTATE


def unique_violation(exc: IntegrityError, *constraints: str) -> bool:
    """IntegrityError가 SQLSTATE 23505(UNIQUE 위반)이고 제약명이 constraints 중 하나면 True.

    constraints 미지정이면 23505이기만 하면 True. asyncpg 예외는 exc.orig에 래핑되며
    .sqlstate·.constraint_name 속성을 노출한다(psycopg 대비 pgcode/diag도 방어적으로 확인).
    """
    orig = getattr(exc, "orig", None)
    if orig is None:
        return False
    # ⚠️ SQLAlchemy asyncpg 어댑터: wrapper(exc.orig)엔 sqlstate/pgcode만 복사되고 실제
    # constraint_name은 원 asyncpg 예외(UniqueViolationError)인 __cause__에만 있다(실측 확인).
    # → sqlstate가 wrapper에 있어도 constraint_name은 반드시 __cause__까지 봐야 한다.
    cause = getattr(orig, "__cause__", None)
    sqlstate = (
        getattr(orig, "sqlstate", None)
        or getattr(orig, "pgcode", None)
        or (getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None) if cause else None)
    )
    if sqlstate != _UNIQUE_VIOLATION:
        return False
    if not constraints:
        return True
    cname = getattr(orig, "constraint_name", None)
    if cname is None and cause is not None:  # asyncpg: 이름은 __cause__에
        cname = getattr(cause, "constraint_name", None)
    if cname is None:  # psycopg diag 폴백
        diag = getattr(orig, "diag", None) or (getattr(cause, "diag", None) if cause else None)
        cname = getattr(diag, "constraint_name", None)
    return cname in constraints
