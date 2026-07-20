"""닉네임 플레이스홀더 — 라운드트립·조사8종·리터럴통과·멱등·경계·폴백."""
import pytest

from app.services import naming


# --- render: 조사 8종 ---
@pytest.mark.parametrize(
    "token, batchim_name, batchim_expect, open_name, open_expect",
    [
        ("{name}", "승민", "승민", "지호", "지호"),
        ("{name:voc}", "승민", "승민아", "지호", "지호야"),
        ("{name:subj}", "승민", "승민이", "지호", "지호가"),
        ("{name:cop}", "승민", "승민이야", "지호", "지호야"),
        ("{name:wa}", "승민", "승민과", "지호", "지호와"),
        ("{name:ira}", "승민", "승민이라고", "지호", "지호라고"),
        ("{name:top}", "승민", "승민은", "지호", "지호는"),
        ("{name:obj}", "승민", "승민을", "지호", "지호를"),
        ("{name:ro}", "승민", "승민으로", "지호", "지호로"),
    ],
)
def test_render_all_josa(token, batchim_name, batchim_expect, open_name, open_expect):
    assert naming.render(token, batchim_name) == batchim_expect
    assert naming.render(token, open_name) == open_expect


def test_render_ro_riul_batchim_uses_ro_not_euro():
    # ㄹ받침은 '으로'가 아니라 '로'(서울로). 종성 예외 커버.
    assert naming.render("{name:ro}", "서울") == "서울로"


# --- C1: 리터럴(토큰 없는 옛 텍스트) 그대로 통과 ---
@pytest.mark.parametrize(
    "literal",
    [
        "승민아 뭐해?",                       # 옛 리터럴 이름 — 오치환 없이 통과
        "오늘 승민이 바빴대.",
        "그냥 평범한 문장이다.",
        "중괄호 없는 {글자} 비슷한 것",        # {name} 아닌 중괄호는 무시
        "",
    ],
)
def test_render_passes_literal_text_through(literal):
    assert naming.render(literal, "지호") == literal


def test_render_none_text():
    assert naming.render(None, "지호") is None


# --- 라운드트립 항등: render(to_placeholder(x)) == x ---
@pytest.mark.parametrize("name", ["승민", "지호", "서울", "훈", "Jo", "지혜"])
@pytest.mark.parametrize(
    "sentence",
    [
        "{voc} 뭐해?",
        "오늘 {subj} 좀 피곤해 보였어.",
        "이름은 {cop}.",
        "{wa} 같이 놀았어.",
        "누가 {ira} 불렀어.",
        "{top} 어제 갔어.",
        "{obj} 봤어.",
        "{ro} 갔어.",
        "그냥 이름 없는 문장.",
        "{voc}, 밥은 먹었어? {subj} 걱정돼.",
    ],
)
def test_roundtrip_identity(name, sentence):
    # sentence 템플릿을 현재 이름의 실제 표면형으로 채운 '자연 텍스트'가 원본.
    original = sentence.format(
        voc=naming.render("{name:voc}", name),
        subj=naming.render("{name:subj}", name),
        cop=naming.render("{name:cop}", name),
        wa=naming.render("{name:wa}", name),
        ira=naming.render("{name:ira}", name),
        top=naming.render("{name:top}", name),
        obj=naming.render("{name:obj}", name),
        ro=naming.render("{name:ro}", name),
    )
    placeholdered = naming.to_placeholder(original, name)
    assert naming.render(placeholdered, name) == original


def test_roundtrip_survives_rename():
    # 저장은 placeholder, 개명 후 render는 새 이름 — 드리프트 없음.
    stored = naming.to_placeholder("승민아 뭐해? 오늘 승민이 바빴어.", "승민")
    assert "승민" not in stored
    assert naming.render(stored, "지호") == "지호야 뭐해? 오늘 지호가 바빴어."


# --- 멱등 ---
def test_to_placeholder_idempotent():
    once = naming.to_placeholder("승민아 안녕", "승민")
    assert naming.to_placeholder(once, "승민") == once  # 이미 토큰 → skip


def test_to_placeholder_none_nickname_noop():
    assert naming.to_placeholder("아무 텍스트", None) == "아무 텍스트"


def test_to_placeholder_empty():
    assert naming.to_placeholder("", "승민") == ""


# --- 경계 / 과치환 반례 ---
def test_no_oversubstitution_name_as_substring():
    # '민'이 이름이어도 '국민'·'승민'의 부분으로 잡히면 안 된다(앞 한글 경계).
    assert naming.to_placeholder("국민과 승민이 만났다.", "민") == "국민과 승민이 만났다."


def test_single_syllable_bare_not_matched():
    # 1음절 '수' — bare 단독 매칭 금지. '수요일'이 오염되면 안 된다.
    assert naming.to_placeholder("이번 주 수요일에 봐.", "수") == "이번 주 수요일에 봐."


def test_single_syllable_josa_still_matched():
    # 1음절도 조사 부착형은 잡는다(경계 확실).
    out = naming.to_placeholder("훈아 이리 와.", "훈")
    assert out == "{name:voc} 이리 와."
    assert naming.render(out, "훈") == "훈아 이리 와."


def test_trailing_hangul_blocks_match():
    # 승민'아빠'처럼 조사 뒤에 한글이 붙으면 미매칭(개명 시 오염 방지).
    assert naming.to_placeholder("승민아빠가 왔어.", "승민") == "승민아빠가 왔어."


def test_non_hangul_name_roundtrip():
    out = naming.to_placeholder("Jo와 놀았어.", "Jo")
    assert out == "{name:wa} 놀았어."
    assert naming.render(out, "Jo") == "Jo와 놀았어."


def test_bare_before_punctuation_matched():
    out = naming.to_placeholder("주인공은 승민. 끝.", "승민")
    assert out == "주인공은 {name}. 끝."
