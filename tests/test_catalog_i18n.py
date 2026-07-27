"""DB 카탈로그 다국어 렌더(SOMA-346) — name_i18n → 유저 언어, NULL·부분키·빈값 폴백."""
from types import SimpleNamespace

from app.services import i18n, routine


def test_localized_name_language_and_fallback():
    loc = {"ko": "집", "en": "Home", "ja": "おうち"}
    assert i18n.localized_name(loc, "ja", "집") == "おうち"
    assert i18n.localized_name(loc, "en-US", "집") == "Home"   # BCP47 정규화
    assert i18n.localized_name(loc, "ko", "집") == "집"
    assert i18n.localized_name(loc, "zh", "집") == "Home"       # 미지원 → en
    # 부분 키: ja 없고 en 있으면 en (한국어로 떨어지지 않음)
    assert i18n.localized_name({"ko": "집", "en": "Home"}, "ja", "집") == "Home"
    # NULL / 빈 dict / 빈 문자열 → 원문 default(빈 이름·min_length 위반 방지)
    assert i18n.localized_name(None, "ja", "집") == "집"
    assert i18n.localized_name({}, "en", "집") == "집"
    assert i18n.localized_name({"ko": "집", "en": ""}, "en", "집") == "집"


def _routine(name, name_i18n):
    return SimpleNamespace(
        id="r1", name=name, name_i18n=name_i18n,
        days_of_week=[1, 2, 3], reminder_enabled=False, reminder_time=None,
    )


def test_routine_dto_renders_localized_name():
    r = _routine("이불 정리하기", {"ko": "이불 정리하기", "en": "Make the bed", "ja": "布団を整える"})
    assert routine._dto(r, False, "ja")["name"] == "布団を整える"
    assert routine._dto(r, False, "en")["name"] == "Make the bed"
    assert routine._dto(r, False, "ko")["name"] == "이불 정리하기"
    # 유저 커스텀(name_i18n=NULL) → 유저가 입력한 원문(언어 무관)
    custom = _routine("아침 스트레칭", None)
    assert routine._dto(custom, False, "ja")["name"] == "아침 스트레칭"


async def test_update_routine_clears_name_i18n_on_rename(monkeypatch):
    """유저가 기본 루틴 이름을 바꾸면 기본 다국어(name_i18n)는 무효화된다(SOMA-346 D1)."""
    r = _routine("이불 정리하기", {"ko": "이불 정리하기", "en": "Make the bed", "ja": "布団を整える"})

    class FakeSession:
        async def commit(self):
            pass

    async def _load_owned(session, uid, rid):
        return r

    monkeypatch.setattr(routine, "_load_owned", _load_owned)
    monkeypatch.setattr(routine, "_uid", lambda u: u)
    req = SimpleNamespace(
        name="새 이름", reminder_enabled=None, model_fields_set=set(), days_of_week=None
    )
    await routine.update_routine(FakeSession(), "u1", "r1", req)
    assert r.name == "새 이름" and r.name_i18n is None
