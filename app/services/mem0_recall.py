"""v2 기억 회상 — 벡터 검색 결과를 registry 상태로 거른다(9.4절·10장).

**벡터 검색만으로는 안 된다.** mem0는 candidate-add-only라 과거와 현재의 상반된 기억이 벡터
저장소에 함께 남는다. provider 삭제는 늦어질 수 있고 실패할 수도 있다. 그래서 "지금 유효한
기억이 무엇인가"의 판정은 registry가 갖고, 검색 결과는 **반드시 그 판정을 통과해야** 한다.

거르지 않으면 사용자가 "이제 회사 안 다녀"라고 말한 뒤에도 캐피가 예전 직장을 계속 꺼낸다.

`ambiguous`는 버리지 않는다. consolidation이 어느 쪽이 현재인지 판정하지 못한 상태이고,
그건 "모른다"이지 "없다"가 아니다. 양쪽을 발생 시각과 함께 넘겨 캐피가 단정하지 않게 한다.

**이 모듈은 예외를 올리지 않는다.** 회상은 대화의 보조지 전제가 아니다. provider가 죽었다고
대화가 죽으면 안 되므로 실패는 빈 목록이다.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from app.services import i18n
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger("moly")

# 검색에 통과시키는 semantic 상태. 이 목록이 이 모듈의 전부다.
VISIBLE_STATUSES = ("active", "ambiguous")

# provider에서 넉넉히 받아 registry로 거른 뒤 상위 N개를 쓴다. 거르고 나서 모자라면
# 회상이 비니, 필터로 빠지는 몫을 감안해 더 많이 받는다.
_PROVIDER_FETCH = 40
DEFAULT_LIMIT = 8

# 거친 상한. 이것만으로는 **부족하다** — 아래 RELEVANCE_MARGIN 설명 참고.
MAX_DISTANCE = 0.90

# 가장 가까운 기억으로부터 이만큼 안쪽만 남긴다.
#
# **절대 임계값은 원리적으로 안 된다.** 짧거나 내용 없는 입력은 임베딩 공간 중심 근처에
# 놓여 모든 것과 적당히 가깝다. dev 실측:
#     'ㅇㅇ'                 최소 거리 0.668   ← 의미 없는 입력이 더 '가깝다'
#     '여자친구랑 요즘 어때?'  최소 거리 0.726
# 절대값으로 자르면 'ㅇㅇ'이 기억을 더 많이 끌어온다. 그래서 **그 질의 안에서의 상대
# 거리**로 자른다. 질의가 흐릿하면 결과가 뭉쳐 있어 적게 남고, 뾰족하면 가까운 것만 남는다.
#
# ⚠️ **이 값은 검증되지 않았다.** dev 기억 12건(한 대화에서 나온)으로 고른 값이라 일반화할
#    근거가 없다. margin 0.08에서 '오늘 루틴 했어?'→루틴 3건, '여자친구랑 요즘 어때?'→
#    여자친구 관련 3건으로 맞았지만, **'안녕' 같은 내용 없는 발화에도 여전히 기억이 딸려온다**
#    (2건). 벡터 유사도만으로는 "이 발화에 회상할 대상이 있는가"를 구분할 수 없다.
#    이건 임계값 조정으로 풀 문제가 아니며, golden set(회상 정답 200건)으로 재보고 정해야 한다.
RELEVANCE_MARGIN = 0.08

# margin과 무관하게 최소 이만큼은 남긴다.
#
# **margin만 쓰면 같은 주제 기억이 여럿일 때 정작 찾는 게 밀려난다.** dev 실측:
#     "여자친구 이름 기억나?" → 여자친구 관련 3건이 앞을 채우고
#                              "내 여자친구 민승이다"는 margin 밖으로 잘림
#     → 캐피가 "이름은 아직 들은 적이 없어"라고 답함
#
# ⚠️ margin을 느슨하게 해도 인사에 기억이 딸려오지 않는다 — 그 방어는 `needs_recall`이
#    따로 한다. margin은 회상하기로 정한 뒤 **긴 꼬리를 자르는 용도**일 뿐이다.
MIN_KEEP = 5


# 되짚는 발화의 표지. 이게 있으면 짧아도 회상한다("민승이?" 4글자).
_LOOKBACK = re.compile(
    r"[?？]|뭐|무슨|언제|어디|누구|얼마|어떻|어땠|했지|있었|였지|이었|"
    r"기억|어제|지난|전에|예전|아까|그때|저번"
)
# 이 길이 이하에서 표지가 없으면 되짚을 대상이 없다고 본다.
SHORT_TURN_CHARS = 6


def needs_recall(query: str) -> bool:
    """이 발화에 회상할 대상이 있는가.

    **벡터 거리로는 판정할 수 없다.** 짧거나 내용 없는 입력은 임베딩 공간 중심 근처에 놓여
    모든 것과 적당히 가깝다. dev 실측:

        '안녕'(2자)              최소 거리 0.697
        'ㅇㅇ'(2자)              최소 거리 0.667   ← 의미 없는 입력이 더 '가깝다'
        '내 루틴 뭐있었지?'(10자)   최소 거리 0.623

    거리로 자르면 'ㅇㅇ'이 기억을 더 많이 끌어온다. 실제로 '안녕' 한마디에 '사이가 안 좋다'를
    포함한 기억 8건이 프롬프트에 들어갔다(감사 지적). 그래서 **발화 자체**를 본다 —
    되짚는 표지가 있거나 충분히 길면 회상하고, 짧은 인사·호응은 넘긴다.
    """
    text = (query or "").strip()
    if not text:
        return False
    if _LOOKBACK.search(text):
        return True
    return len(text) > SHORT_TURN_CHARS


@dataclass(frozen=True, slots=True)
class Recalled:
    text: str
    status: str                      # active | ambiguous
    distance: float                  # cosine 거리 — **낮을수록 관련 있다**
    occurred_at: datetime | None     # 사건 시각(있으면). ambiguous 판단 근거로 보여준다
    conflict_group_id: uuid.UUID | None

    @property
    def uncertain(self) -> bool:
        return self.status == "ambiguous"


# 벡터 id → registry. 상태 필터와 user 재검증을 **DB에서** 한 번에 한다.
# provider payload의 user_id는 adapter가 이미 봤지만, registry로 한 번 더 본다 —
# 벡터 저장소가 오염돼도 남의 기억이 프롬프트에 실리면 안 된다.
_FILTER = text("""
SELECT r.provider_memory_id::text AS pid,
       r.semantic_status,
       COALESCE(r.event_started_at, r.created_at) AS occurred_at,
       r.conflict_group_id
FROM mem0_memory_registry r
WHERE r.user_id = :user_id
  AND r.collection_version = :collection_version
  AND r.semantic_status = ANY(:statuses)
  AND r.provider_memory_id::text = ANY(:ids)
""")


async def recall(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    query: str,
    adapter,
    embed_query,
    collection_version: str = "v2",
    limit: int = DEFAULT_LIMIT,
    timeout: float = 3.0,
) -> list[Recalled]:
    """질의와 관련된 **현재 유효한** 기억. 실패하면 빈 목록이다.

    adapter·embed_query를 주입받는다 — 워커와 챗이 같은 코드를 쓰되 각자의 클라이언트를
    들고 있고, 테스트가 provider 없이 돌 수 있어야 한다.
    """
    if not needs_recall(query):
        # 인사·호응에 기억을 끌어오면 캐피가 뜬금없이 옛 얘기를 꺼낸다. 임베딩 호출도 아낀다.
        return []
    try:
        # 임베딩에도 **같은 예산**을 건다. 안쪽이 60초를 쓰면 경계의 wait_for가 걸리기 전에
        # 이미 마감을 넘긴다.
        vector = await embed_query(query, timeout=timeout)
        hits = await adapter.search(
            vector, user_id=str(user_id), limit=_PROVIDER_FETCH, timeout=timeout
        )
    except Exception as e:  # noqa: BLE001  회상 실패가 대화를 막지 않는다
        _log.warning("v2 회상 실패(빈 목록으로 진행) — user=%s: %r", user_id, e)
        return []
    if not hits:
        return []

    by_id = {h.id: h for h in hits}
    try:
        rows = (await session.execute(_FILTER, {
            "user_id": user_id,
            "collection_version": collection_version,
            "statuses": list(VISIBLE_STATUSES),
            "ids": list(by_id),
        })).all()
    except Exception as e:  # noqa: BLE001
        _log.warning("v2 회상 registry 조회 실패 — user=%s: %r", user_id, e)
        return []

    out: list[Recalled] = []
    for pid, status, occurred_at, group in rows:
        hit = by_id.get(pid)
        if hit is None:
            continue
        body = (hit.payload or {}).get("text")
        if not body:
            # 본문은 provider payload가 갖는다. 없으면 보여줄 게 없다.
            continue
        out.append(Recalled(
            text=str(body), status=str(status), distance=float(hit.distance or 0.0),
            occurred_at=occurred_at, conflict_group_id=group,
        ))

    # 거리 **오름차순** — 가까운 것이 먼저다. 같은 거리면 최근 사건을 앞에 둔다.
    out.sort(key=lambda r: (r.distance, -(r.occurred_at.timestamp() if r.occurred_at else 0)))
    if not out:
        return []
    # 상대 컷: 이 질의에서 가장 가까운 것 기준으로 자른다(절대값이 안 되는 이유는 위 주석).
    cut = min(out[0].distance + RELEVANCE_MARGIN, MAX_DISTANCE)
    kept = [r for r in out if r.distance <= cut]
    if len(kept) < MIN_KEEP:
        # 꼬리를 너무 많이 잘랐다. 절대 상한 안쪽에서 최소한은 채운다.
        kept = [r for r in out if r.distance <= MAX_DISTANCE][:MIN_KEEP]
    return kept[:limit]


# 어긋나는 기억을 보여줄 때의 안내. **언어별로 있어야 한다** — 영어 사용자에게 한국어
# 지시문이 가면 모델이 그 언어로 답할 수 있다(감사 지적: en 요청에 [기억] 한국어 헤더).
_UNCERTAIN: dict[str, str] = {
    "ko": "(아래는 서로 어긋나는 기억이야. 어느 쪽이 지금인지 단정하지 말고, "
          "궁금하면 자연스럽게 물어봐.)",
    "en": "(These memories conflict. Do not assert which one is current; "
          "ask naturally if you want to know.)",
    "ja": "(以下は食い違う記憶。どちらが今かは断定せず、気になるなら自然に聞いて。)",
}


def render_block(items: list[Recalled], *, language: str = "ko") -> str:
    """프롬프트에 넣을 서버 소유 블록. 항목이 없으면 빈 문자열이다.

    ambiguous는 **단정하지 않는 문장**으로 렌더한다. 캐피가 "너 회사 그만뒀잖아"라고
    말해버리면 틀렸을 때 사용자가 정정해야 하고, 그건 기억이 아니라 사고다.
    """
    if not items:
        return ""
    sure = [i for i in items if not i.uncertain]
    unsure = [i for i in items if i.uncertain]

    lines: list[str] = []
    if sure:
        lines += [f"- {i.text}" for i in sure]
    if unsure:
        lines.append(_UNCERTAIN[i18n.resolve(language)])
        for i in unsure:
            when = i.occurred_at.strftime("%Y-%m-%d") if i.occurred_at else "시점 모름"
            lines.append(f"- ({when}) {i.text}")

    lang = i18n.resolve(language)
    header = {"ja": "[記憶]", "en": "[memory]"}.get(lang, "[기억]")
    return f"{header}\n" + "\n".join(lines)
