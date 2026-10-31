"""원고 규칙 검증, 생성 호출 방식, 시드 파일, 보충 잡, 변경 SQL."""
import json
import re
import uuid
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.core.db import Base
from app.models.daily_practice import DailyPracticeAction
from app.services import daily_practice_content as content
from app.services import llm, usage_ledger
from scripts import daily_practice_seed as seed_script
from worker import daily_practice_jobs as worker_jobs

ROOT = Path(__file__).resolve().parents[1]
GOOD = {
    "slots": ["morning", "day"],
    "icon": "droplet",
    "title": {"ko": "물 한 컵 마시기", "en": "Drink a glass of water", "ja": "お水を一杯飲む"},
    "speech": {
        "ko": "물 한 컵 마시고 올래?",
        "en": "How about a glass of water?",
        "ja": "お水、一杯飲んでこない?",
    },
}


def with_change(path: str, value):
    raw = deepcopy(GOOD)
    target = raw
    keys = path.split(".")
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    return raw


def test_a_good_action_passes():
    assert content.problems(GOOD) == []


@pytest.mark.parametrize(("path", "value", "problem"), [
    ("slots", [], "slots"),
    ("slots", ["noon"], "slots"),
    ("slots", ["day", "day"], "slots"),
    ("icon", "pill", "icon"),
    ("icon", "not_a_key", "icon"),
    ("title.ko", "아주아주아주아주아주아주 긴 제목으로 물 마시기", "title_ko_length"),
    ("title.en", "Drink a very very very big glass of water", "title_en_length"),
    ("title.ja", "お水を一杯ゆっくりゆっくりゆっくり飲んでみる", "title_ja_length"),
    ("speech.ko", "물 한 컵 마시고 오는 건 어때? 천천히 마시고 나면 몸이 좀 가벼워질지도 몰라.", "speech_ko_length"),
    ("title.ko", "물·차 마시기", "title_ko_mark"),
    ("title.ja", "お水・お茶を飲む", "title_ja_mark"),
    ("speech.ko", "물 한 컵 마시자!", "speech_ko_mark"),
    ("speech.en", "Drink some water!", "speech_en_mark"),
    ("speech.ja", "お水を飲もう！", "speech_ja_mark"),
    ("title.ko", "물 한 컵 마셔", "title_ko_ending"),
    ("title.en", "drink a glass of water", "title_en_capital"),
    ("speech.ko", "물 한 컵 마셔 보세요.", "speech_ko_formal"),
    ("speech.ko", "물 드세요?", "speech_ko_formal"),
    ("speech.ja", "お水を飲みましょう", "speech_ja_formal"),
    ("speech.ko", "물 마셔. 지금 마셔. 얼른 마셔.", "speech_ko_sentences"),
    ("title.en", "Drink water.", "title_en_punctuation"),
    ("title.ko", "커피 한 잔 마시기", "title_ko_banned"),
    ("title.ko", "술 한 잔 마시기", "title_ko_banned"),
    ("title.ko", "약 챙겨 먹기", "title_ko_banned"),
    ("title.ko", "체중 재 보기", "title_ko_banned"),
    ("title.ko", "강아지 쓰다듬기", "title_ko_banned"),
    ("title.ko", "엄마에게 전화하기", "title_ko_banned"),
    ("title.ko", "회사 책상 닦기", "title_ko_banned"),
    ("speech.ko", "마음을 토닥여 볼까?", "speech_ko_banned"),
    ("title.en", "Call your mom", "title_en_banned"),
    ("title.en", "Skip coffee today", "title_en_banned"),
    ("title.en", "Pet your cat", "title_en_banned"),
    ("speech.en", "Going to work?", "speech_en_banned"),
    ("title.ja", "コーヒーを飲む", "title_ja_banned"),
    ("title.ja", "犬をなでる", "title_ja_banned"),
    ("title.ko", "Drink water 기", "title_ko_script"),
    ("title.en", "물 마시기", "title_en_script"),
    ("title.ja", "물 마시기", "title_ja_script"),
    ("speech.ko", "물 한 컵 😀", "speech_ko_script"),
    ("title.ko", "아침 물 한 컵 마시기", "title_ko_time_word"),
    ("title.ko", "잠들기 전 물 마시기", "title_ko_time_word"),
    ("speech.en", "Before bed, have some water?", "speech_en_time_word"),
    ("title", "물 마시기", "title"),
    ("speech", None, "speech"),
])
def test_rule_violations_are_found(path, value, problem):
    assert problem in content.problems(with_change(path, value))


@pytest.mark.parametrize("title", ["책상 정돈하기", "약속 하나 적기", "대화 한 줄 쓰기"])
def test_ordinary_words_are_not_caught_by_the_banned_list(title):
    assert "title_ko_banned" not in content.problems(with_change("title.ko", title))


def test_a_word_that_names_a_time_is_fine_in_a_matching_slot():
    raw = with_change("title.ko", "아침에 물 마시기")
    raw["slots"] = ["morning"]
    assert content.problems(raw) == []


def test_the_contract_limits_are_enforced_exactly():
    assert content.TITLE_MAX == {"ko": 18, "en": 32, "ja": 18}
    assert content.SPEECH_MAX["ko"] == 40
    assert content.problems(with_change("title.ko", "가" * 17 + "기")) == []
    assert "title_ko_length" in content.problems(with_change("title.ko", "가" * 18 + "기"))


def test_not_an_object_is_rejected():
    assert content.problems("x") == ["not_an_object"]


@pytest.mark.parametrize(("title", "existing", "expected"), [
    ("물 한 컵 마시기", ["물 한컵 마시기"], True),
    ("물 한 컵 마시기", ["물 한 컵 마시기 "], True),
    ("창문 열어 두기", ["창문 잠깐 열어 두기"], True),
    ("물 한 컵 마시기", ["책상 위 하나 치우기"], False),
    ("물 한 컵 마시기", [], False),
])
def test_duplicates_are_detected(title, existing, expected):
    assert content.is_duplicate(title, existing) is expected


@pytest.mark.parametrize("text", ["", "no json", "[1, 2]", "{broken", '{"actions": "x"}'])
def test_broken_output_parses_to_nothing(text):
    assert content.parse(text) == []


def test_output_inside_markdown_still_parses():
    assert content.parse('```json\n{"actions": [1]}\n```') == [1]


def test_icon_allow_list_has_routine_icon_keys_and_no_excluded_motifs():
    icons = content.allowed_icons()
    assert len(icons) == len(set(icons)) and len(icons) >= 50
    assert all(re.fullmatch(r"[a-z0-9_]{1,64}", icon) for icon in icons)
    assert not {"pill", "hospital", "dog", "cat_face", "money_bag", "briefcase", "school"} & set(icons)


def test_seed_time_words_are_case_insensitive():
    assert "speech_en_time_word" in content.problems(with_change("speech.en", "Before bed, some water?"))


def test_every_category_has_a_brief_and_the_prompt_lists_rules_and_icons():
    assert set(content.CATEGORY_BRIEF) == set(content.CATEGORIES)
    prompt = content.system_prompt("connect", "night")
    assert "no close friends" in prompt and "droplet" in prompt and "night" in prompt
    assert "18" in prompt and "40" in prompt


def llm_result(text):
    return SimpleNamespace(text=text, input_tokens=10, output_tokens=20)


def payload(*items):
    return json.dumps({"actions": list(items)}, ensure_ascii=False)


async def test_generate_uses_the_setting_the_ledger_and_no_user(monkeypatch):
    call = AsyncMock(return_value=llm_result(payload(GOOD)))
    monkeypatch.setattr(llm, "generate", call)
    batch = await content.generate(
        "rest", 1, existing_titles=[], purpose="daily_practice_seed", max_tokens=100, timeout=9
    )
    kwargs = call.call_args.kwargs
    assert kwargs["model"] == settings.model_daily_practice == "gpt-6-luna"
    assert kwargs["reasoning_effort"] in {"none", "low"}
    assert kwargs["max_tokens"] == 100 and kwargs["timeout"] == 9
    ledger = kwargs["ledger"]
    assert ledger.lane == usage_ledger.LANE_BACKGROUND and ledger.user_id is None
    assert ledger.prompt_version == content.PROMPT_VERSION
    assert [a.title["ko"] for a in batch.accepted] == ["물 한 컵 마시기"]
    assert batch.accepted[0].category == "rest"
    assert (batch.input_tokens, batch.output_tokens) == (10, 20)


async def test_generate_filters_invalid_and_duplicate_actions(monkeypatch):
    bad = with_change("icon", "pill")
    bad["title"]["ko"] = "다른 제목 쓰기"
    text = payload(GOOD, GOOD, bad, "not an object")
    monkeypatch.setattr(llm, "generate", AsyncMock(return_value=llm_result(text)))
    batch = await content.generate(
        "rest", 4, existing_titles=[], purpose="p", max_tokens=100, timeout=9
    )
    assert len(batch.accepted) == 1
    assert [reasons for _, reasons in batch.rejected] == [["duplicate"], ["icon"], ["not_an_object"]]


async def test_generate_rejects_titles_that_exist_already(monkeypatch):
    monkeypatch.setattr(llm, "generate", AsyncMock(return_value=llm_result(payload(GOOD))))
    batch = await content.generate(
        "rest", 1, existing_titles=["물 한 컵 마시기"], purpose="p", max_tokens=100, timeout=9
    )
    assert batch.accepted == []


# ── 시드 파일과 SQL ───────────────────────────────────────────────

def seed_row(**overrides):
    return {
        "category": "rest", "slots": ["night"], "icon": "bed",
        "title": {"ko": "이불 펴기", "en": "Make the bed", "ja": "布団を敷く"},
        "speech": {"ko": "이불 펼까?", "en": "Don't you want to make the bed?", "ja": "布団、敷く?"},
        "source": "seed", "prompt_version": "v", "model": "gpt-6-luna",
    } | overrides


def test_rendered_sql_is_idempotent_and_escapes_quotes():
    sql = seed_script.render_sql([seed_row()])
    assert "ON CONFLICT (title_ko) DO NOTHING;" in sql
    assert "'Don''t you want to make the bed?'" in sql
    assert "ARRAY['night']::text[]" in sql
    assert sql.index("rest") < sql.index("ON CONFLICT")


def test_rendered_sql_keeps_a_stable_order():
    first = seed_row(title={"ko": "나중 쓰기", "en": "B", "ja": "B"})
    second = seed_row(category="body", title={"ko": "먼저 쓰기", "en": "A", "ja": "A"})
    assert seed_script.render_sql([first, second]) == seed_script.render_sql([second, first])
    rendered = seed_script.render_sql([first, second])
    assert rendered.index("'rest'") < rendered.index("'body'")


def test_sql_columns_exist_on_the_model():
    assert set(seed_script._COLUMNS) <= set(DailyPracticeAction.__table__.columns.keys())


def checked_in_seed():
    if not seed_script.SEED.exists():
        pytest.skip("시드 원고가 아직 없다: scripts/daily_practice_seed.py generate")
    return json.loads(seed_script.SEED.read_text(encoding="utf-8"))


def test_checked_in_seed_passes_validation_and_has_no_duplicates():
    rows = checked_in_seed()
    seen: list[str] = []
    for row in rows:
        assert row["category"] in content.CATEGORIES
        assert row["source"] == "seed"
        assert content.problems(row) == [], row["title"]["ko"]
        assert not content.is_duplicate(row["title"]["ko"], seen), row["title"]["ko"]
        seen.append(row["title"]["ko"])


def test_checked_in_seed_sql_is_current():
    rows = checked_in_seed()
    assert seed_script.SQL.read_text(encoding="utf-8") == seed_script.render_sql(rows)


# ── 보충 잡 ───────────────────────────────────────────────────────

def job(**payload_values):
    return SimpleNamespace(id=uuid.uuid4(), payload={"category": "rest", "slot": "night"} | payload_values)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _WorkerSession:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        return _Rows(self.rows)


@pytest.fixture
def worker_session(monkeypatch):
    session = _WorkerSession([("물 한 컵 마시기", "rest"), ("책상 닦기", "order")])
    monkeypatch.setattr(worker_jobs, "get_sessionmaker", lambda: lambda: session)
    return session


async def test_top_up_caps_actions_per_run_and_records_provenance(monkeypatch, worker_session):
    many = [with_change("title.ko", f"{'가나다라마바사아자차'[i]}{'카타파하'[i % 4]}보기") for i in range(8)]
    for i, raw in enumerate(many):
        raw["title"]["en"] = f"Action {i}"
    generate = AsyncMock(return_value=SimpleNamespace(
        accepted=[content.to_action(raw, "rest") for raw in many], rejected=[]
    ))
    monkeypatch.setattr(content, "generate", generate)
    result = await worker_jobs.handle_top_up(job())

    kwargs = generate.call_args.kwargs
    assert generate.call_args.args == ("rest", worker_jobs.MAX_ACTIONS_PER_RUN)
    assert kwargs["slot"] == "night" and kwargs["existing_titles"] == ["물 한 컵 마시기", "책상 닦기"]
    assert kwargs["avoid_in_prompt"] == ["물 한 컵 마시기"]
    assert result.result_detail["added"] == worker_jobs.MAX_ACTIONS_PER_RUN == 5

    target = _WorkerSession()
    await result.apply_domain(target)
    params = target.statements[0].compile().params
    assert sum(1 for key in params if key.startswith("title_ko")) == 5
    assert {params[key] for key in params if key.startswith("source")} == {"top_up"}
    assert {params[key] for key in params if key.startswith("model")} == {"gpt-6-luna"}


async def test_top_up_failure_is_quiet(monkeypatch, worker_session):
    monkeypatch.setattr(content, "generate", AsyncMock(side_effect=RuntimeError("provider down")))
    result = await worker_jobs.handle_top_up(job())
    assert result.result_code == "generation_failed" and result.apply_domain is None


async def test_top_up_with_nothing_valid_writes_nothing(monkeypatch, worker_session):
    monkeypatch.setattr(content, "generate", AsyncMock(return_value=SimpleNamespace(accepted=[], rejected=[("x", ["icon"])])))
    result = await worker_jobs.handle_top_up(job())
    assert result.apply_domain is None and result.result_detail["rejected"] == 1


@pytest.mark.parametrize("values", [{"category": "nope"}, {"slot": "noon"}, {"category": None}])
async def test_top_up_ignores_unknown_payloads(monkeypatch, values):
    generate = AsyncMock()
    monkeypatch.setattr(content, "generate", generate)
    result = await worker_jobs.handle_top_up(job(**values))
    assert result.result_code == "invalid_payload"
    generate.assert_not_called()


# ── 변경 SQL ──────────────────────────────────────────────────────

def test_change_sql_matches_schema_sql_verbatim():
    change = (ROOT / "db/changes/daily_practice.sql").read_text(encoding="utf-8")
    body = change[change.index("CREATE TABLE public.daily_practice_actions"):].strip()
    assert body in (ROOT / "db/schema.sql").read_text(encoding="utf-8")


def test_offer_rows_cascade_from_the_profile():
    table = Base.metadata.tables["daily_practice_offers"]
    profile_fk = next(iter(table.c.user_id.foreign_keys))
    assert profile_fk.target_fullname == "profiles.id" and profile_fk.ondelete == "CASCADE"
