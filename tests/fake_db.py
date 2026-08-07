"""테스트용 인메모리 세션 — SELECT의 WHERE/ORDER BY/LIMIT을 **실제로 평가**한다.

이 프로젝트의 단위테스트는 DB를 띄우지 않고 `FakeSession`이 미리 정한 행을 그대로 돌려준다.
그 방식으로는 W6의 핵심 요구인 **cross-user negative**(타 유저 데이터가 0건인지)를 검증할 수 없다 —
쿼리에서 `user_id` 조건을 통째로 빼도 테스트가 그대로 통과하기 때문이다.

그래서 여기서는 행을 테이블별로 담아두고, 실행된 `Select`의 WHERE 절을 파이썬으로 해석해 필터링한다.
지원 범위는 W6 도구가 실제로 쓰는 연산자뿐이고(=, !=, <, <=, >, >=, IS, IS NOT, IN, AND, OR),
모르는 구조를 만나면 조용히 통과시키지 않고 **예외를 던진다**. 조용한 통과는 필터가 사라진 것을
못 보게 만든다.

NULL 비교는 SQL 3값 논리를 따른다(`NULL <= now` → 제외). `published_at IS NOT NULL` 없이 짠 쿼리도
여기서 같은 결과가 나오도록 하기 위한 것이다.
"""
from __future__ import annotations

from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import (
    BinaryExpression,
    BindParameter,
    BooleanClauseList,
    ColumnClause,
    Grouping,
    Null,
    UnaryExpression,
)


class UnsupportedClause(AssertionError):
    """가짜 세션이 해석하지 못한 절 — 테스트가 사실을 못 보게 되므로 통과시키지 않는다."""


def _value(row: object, expr: object):
    if isinstance(expr, BindParameter):
        return expr.value
    if isinstance(expr, Null) or expr is None:
        return None
    if isinstance(expr, Grouping):
        return _value(row, expr.element)
    if isinstance(expr, ColumnClause):
        return getattr(row, expr.key)
    raise UnsupportedClause(f"해석할 수 없는 표현식: {expr!r}")


_ORDERED = {
    operators.lt: lambda a, b: a < b,
    operators.le: lambda a, b: a <= b,
    operators.gt: lambda a, b: a > b,
    operators.ge: lambda a, b: a >= b,
}


def matches(row: object, clause: object) -> bool:
    """행이 WHERE 절을 만족하는지. 절이 없으면(=필터 없음) True."""
    if clause is None:
        return True
    if isinstance(clause, Grouping):
        return matches(row, clause.element)
    if isinstance(clause, BooleanClauseList):
        parts = [matches(row, c) for c in clause.clauses]
        if clause.operator is operators.and_:
            return all(parts)
        if clause.operator is operators.or_:
            return any(parts)
        raise UnsupportedClause(f"지원하지 않는 논리연산자: {clause.operator!r}")
    if isinstance(clause, BinaryExpression):
        op = clause.operator
        left = _value(row, clause.left)
        right = _value(row, clause.right)
        if op is operators.eq:
            return left == right
        if op is operators.ne:
            return left != right
        if op is operators.is_:
            return left is None if right is None else left == right
        if op is operators.is_not:
            return left is not None if right is None else left != right
        if op in (operators.in_op, operators.not_in_op):
            items = list(right or [])
            hit = left in items
            return hit if op is operators.in_op else not hit
        if op in _ORDERED:
            if left is None or right is None:
                return False  # SQL 3값 논리: NULL 비교는 UNKNOWN → 행 제외
            return _ORDERED[op](left, right)
        raise UnsupportedClause(f"지원하지 않는 연산자: {op!r}")
    raise UnsupportedClause(f"해석할 수 없는 절: {clause!r}")


def _sorted(rows: list, clauses) -> list:
    """ORDER BY 적용. 여러 키는 뒤에서부터 안정정렬을 겹쳐 SQL과 같은 우선순위를 만든다."""
    out = list(rows)
    for clause in reversed(list(clauses or ())):
        desc = False
        expr = clause
        if isinstance(expr, UnaryExpression):
            desc = expr.modifier is operators.desc_op
            expr = expr.element
        if not isinstance(expr, ColumnClause):
            raise UnsupportedClause(f"해석할 수 없는 ORDER BY: {clause!r}")
        key = expr.key
        # NULL은 파이썬에서 비교 불가라 뒤로 몬다(Postgres 기본 NULLS LAST와 같은 방향).
        out.sort(key=lambda r, k=key: (getattr(r, k) is None, getattr(r, k) or 0), reverse=desc)
    return out


class _Scalars:
    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)

    def first(self):
        return self._items[0] if self._items else None


class _Result:
    def __init__(self, items):
        self._items = list(items)

    def scalars(self):
        return _Scalars(self._items)


class FakeDbSession:
    """`{모델클래스: [행, ...]}`을 받아 SELECT를 실제로 평가하는 읽기 전용 세션.

    행은 SimpleNamespace여도 되고 ORM 인스턴스여도 된다(속성 이름만 맞으면 된다).
    """

    def __init__(self, tables: dict | None = None):
        self.tables = {k: list(v) for k, v in (tables or {}).items()}
        self.statements: list = []
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params=None):
        self.statements.append(stmt)
        desc = stmt.column_descriptions
        if not desc:  # pragma: no cover - 도구는 항상 엔티티/컬럼을 고른다
            raise UnsupportedClause("column_descriptions가 비었다")
        entity = desc[0]["entity"]
        rows = [r for r in self.tables.get(entity, []) if matches(r, stmt.whereclause)]
        rows = _sorted(rows, stmt._order_by_clauses)
        limit = stmt._limit
        if limit is not None:
            rows = rows[:limit]
        if desc[0]["expr"] is entity:
            return _Result(rows)
        name = desc[0]["name"]
        return _Result([getattr(r, name) for r in rows])

    async def rollback(self):
        self.rolled_back = True

    async def commit(self):  # pragma: no cover - 도구는 커밋하지 않는다
        raise AssertionError("도구는 read-only다 — commit하면 안 된다")
