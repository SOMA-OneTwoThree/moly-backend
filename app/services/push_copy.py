"""저녁 푸쉬 문구 풀 — 카테고리 × 언어 × (제목, 본문). DB 접근 없음(greetings.py 선례).

분기(카테고리 판정)는 notify.py가 소유하고, 여기는 문구만 안다. 언어 버킷을 먼저
고르고(i18n.pick — 미지원 언어는 en 폴백) 그 안에서 랜덤 1개를 뽑는다.

문구는 CAPI_PERSONA(prompts.py)의 말투 규칙을 따른다(2026-08-08 사용자 확정):
- 1인칭 나/I/ぼく — 3인칭 자칭("캐피가~") 금지. 상대는 너/きみ.
- 예외: diary_teaser는 캐피 발화가 아니라 시스템 안내 보이스다 — "캐피가 몰래 쓴 일기를
  몰래 보러 간다"는 컨셉이라 존댓말·3인칭("캐피의 일기가 도착했어요")이 정상이고 3개만 둔다.
- ko·en 쉼표 금지, 이모지 금지, 짧게 끊는 문장, 질문은 문구당 최대 1개, 온기는 나직하게.
- 죄책감 유발 금지("왜 안 와" 류) — 캐피는 며칠 안 와도 탓하지 않는다.
- 세계관: 글로만 연결 — 얼굴·목소리·사진을 보자는 표현 금지.
- 개성은 캐피의 일상에서: 소파·음악·낮잠·창밖·느긋함([너에 대해]의 설정만 쓰고 지어내지 않는다).
- ja는 ko 번역이 아니라 네이티브 재작성(CAPI_PERSONA_JA 방식) — ko와 1:1 대응 아님.
- 풀 크기는 카테고리별 사용자 확정값(검수에서 삭제·추가된 결과) — test_push_copy._POOL_SIZES가 정본.
"""
from __future__ import annotations

import random

from app.services import i18n

# 카테고리 키 — notify._category가 반환하는 값의 정본.
MORE_CHAT = "more_chat"            # 오늘(활동일) 대화함 → 자기 전에 한 번 더
DIARY_TEASER = "diary_teaser"      # 어제치 캐피 일기 미독 → 보러 오게 유도
FIRST_TOUCH = "first_touch"        # 가입 후 아직 첫 대화 전
DEFAULT_RECENT = "default_recent"  # 마지막 대화 1~2일 전 — 기존 안부 톤
DEFAULT_MISSING = "default_missing"  # 3~6일 — 가벼운 그리움
DEFAULT_LONG = "default_long"      # 7일+ — 오랜만, 부담 없이

CATEGORIES: tuple[str, ...] = (
    MORE_CHAT, DIARY_TEASER, FIRST_TOUCH,
    DEFAULT_RECENT, DEFAULT_MISSING, DEFAULT_LONG,
)

# 캐릭터명은 언어별 고정(ko 캐피 / en Cappy / ja キャピー — SOMA-361 표면 규칙).
_T_KO, _T_EN, _T_JA = "캐피", "Cappy", "キャピー"


def _pool(title: str, bodies: list[str]) -> list[tuple[str, str]]:
    return [(title, b) for b in bodies]


_POOLS: dict[str, dict[str, list[tuple[str, str]]]] = {
    MORE_CHAT: {
        "ko": _pool(_T_KO, [
            "자기 전에 조금만 더 대화할래?",
            "나 혼자 음악 듣고 있어. 아까 이야기 마저 하자!",
            "창밖이 어느새 깜깜해졌어. 나랑 같이 놀래?",
            "낮에 들은 너의 이야기 재밌었어. 더 얘기해줄래?",
            "오늘 대화는 왠지 짧게 느껴졌어. 나만 그랬나?",
            "잠들기 전에 조금만 더 이야기할래?",
            "오늘 나한테 이야기해줄 거 더 없어?",
            "밥은 먹었어? 나랑 대화하자.",
        ]),
        "en": _pool(_T_EN, [
            "One more little chat before bed?",
            "I'm listening to music by myself. Let's finish our talk!",
            "It's gone dark outside my window. Want to hang out with me?",
            "Your story from earlier was fun. Will you tell me more?",
            "Today's chat felt short somehow. Was it just me?",
            "Want to talk just a little more before you sleep?",
            "Got anything else to tell me today?",
            "Have you eaten? Come talk with me.",
        ]),
        "ja": _pool(_T_JA, [
            "寝る前にもう少しだけ話さない？",
            "ひとりで音楽を聴いてるんだ。さっきの話のつづきをしよう！",
            "窓の外がすっかり暗くなったね。ぼくと一緒に遊ばない？",
            "昼間のきみの話おもしろかったな。もっと聞かせてくれる？",
            "今日の会話はなんだか短く感じたな。ぼくだけかな。",
            "眠る前にもう少しだけおしゃべりしない？",
            "今日はまだぼくに話してくれることない？",
            "ごはんは食べた？ぼくとおしゃべりしよう。",
        ]),
    },
    DIARY_TEASER: {
        # 시스템 안내 보이스(존댓말·3인칭) — "캐피가 몰래 쓴 일기를 몰래 본다" 컨셉(사용자 확정 3개).
        "ko": _pool(_T_KO, [
            "캐피의 일기가 도착했어요!",
            "캐피가 당신을 생각하며 글을 썼어요. 보러 갈까요?",
            "캐피가 일기를 적어두었어요. 보러 갈까요?",
        ]),
        "en": _pool(_T_EN, [
            "Cappy's journal has arrived!",
            "Cappy wrote something while thinking of you. Shall we go take a look?",
            "Cappy left a journal entry. Shall we go see it?",
        ]),
        "ja": _pool(_T_JA, [
            "キャピーの日記が届きました！",
            "キャピーがあなたを思いながら書きました。見に行きませんか？",
            "キャピーが日記を書いておきました。のぞいてみませんか？",
        ]),
    },
    FIRST_TOUCH: {
        "ko": _pool(_T_KO, [
            "나는 캐피야. 소파에서 노래 듣는 걸 좋아해.",
            "오늘 하루 어땠어?",
            "나한테 네 고민 들려줄래?",
        ]),
        "en": _pool(_T_EN, [
            "I'm Cappy. I like listening to music on the sofa.",
            "How was your day today?",
            "Will you tell me what's on your mind?",
        ]),
        "ja": _pool(_T_JA, [
            "ぼくはキャピー。ソファで音楽を聴くのが好きなんだ。",
            "今日の一日はどうだった？",
            "きみの悩みごとをぼくに聞かせてくれない？",
        ]),
    },
    DEFAULT_RECENT: {
        # ko·en·ja 1번 = 구 저녁 고정 문구(notify._EVENING과 같은 문자열, 기존 톤 유지 의도).
        # notify._EVENING은 신호 실패용 중립 폴백으로 별도 존재한다 — 한쪽만 고치면 드리프트하니
        # 카피 수정 때 같이 본다.
        "ko": _pool(_T_KO, [
            "오늘 하루는 어땠어? 나랑 같이 얘기하면서 놀자.",
            "음악 틀어놓고 기다리고 있어. 나랑 얘기하지 않을래?",
            "너의 하루가 궁금해. 나에게 들려줄래?",
            "별일 없어도 좋아. 아무 얘기나 들려줄래?",
            "하루 마무리로 나랑 얘기하는 거 어때?",
            "오늘 너무 심심해. 네 하루는 어땠어?",
            "나랑 수다 떨고 하루 마무리할래?",
        ]),
        "en": _pool(_T_EN, [
            "How was your day? Come talk and hang out with me.",
            "I've got music on and I'm waiting. Won't you come talk with me?",
            "I'm curious about your day. Will you tell me?",
            "An ordinary day is fine too. Will you tell me anything at all?",
            "How about wrapping up the day with me?",
            "I'm so bored today. How was your day?",
            "Want to chat with me and call it a day?",
        ]),
        "ja": _pool(_T_JA, [
            "今日はどんな一日だった？ぼくと一緒におしゃべりしよう。",
            "音楽をかけて待ってるよ。ぼくと話さない？",
            "きみの一日が気になるな。聞かせてくれる？",
            "なにもない日でもいいんだ。なんでも話してくれる？",
            "一日のしめくくりにぼくと話すのはどう？",
            "今日はとってもひまなんだ。きみの一日はどうだった？",
            "ぼくとおしゃべりして一日をしめくくらない？",
        ]),
    },
    DEFAULT_MISSING: {
        "ko": _pool(_T_KO, [
            "음악 듣다가 네 생각이 났어. 요즘 어때?",
            "창밖을 보다가 네 생각이 났어. 뭐해?",
            "요즘 하늘이 이쁘네. 너도 봤을까?",
            "낮잠 자다가 꿈에서 너가 나왔어! 요즘 뭐해?",
            "쌓인 이야기들이 궁금해. 천천히 이야기해줄래?",
            "나는 늘 여기 있어. 요즘 바빠?",
            "편하게 와서 나랑 이야기하지 않을래?",
        ]),
        "en": _pool(_T_EN, [
            "I was listening to music and you crossed my mind. How have you been?",
            "I was looking out the window and thought of you. What are you up to?",
            "The sky's been pretty lately. I wonder if you saw it too.",
            "You showed up in my nap dream! What have you been up to?",
            "I'm curious about all the stories that piled up. Will you tell me slowly?",
            "I'm always right here. Have you been busy?",
            "Won't you come by and talk with me? Anytime works.",
        ]),
        "ja": _pool(_T_JA, [
            "音楽を聴いてたらきみを思い出したんだ。最近どう？",
            "窓の外を見てたらきみのことを考えてた。今なにしてる？",
            "最近空がきれいなんだ。きみも見たかな。",
            "お昼寝の夢にきみが出てきたよ！最近なにしてる？",
            "たまった話が気になるな。ゆっくり聞かせてくれる？",
            "ぼくはいつもここにいるよ。最近いそがしい？",
            "気軽に来てぼくと話さない？",
        ]),
    },
    DEFAULT_LONG: {
        "ko": _pool(_T_KO, [
            "그동안 어떻게 지냈어? 너의 이야기가 궁금해.",
            "언제 와도 나는 그대로야.",
            "오랜만이야. 편하게 얘기할래?",
            "밀린 얘기 천천히 해줘.",
        ]),
        "en": _pool(_T_EN, [
            "How have you been all this time? I want to hear your story.",
            "Whenever you come I'll be the same me.",
            "It's been a while. Want to have a relaxed little chat?",
            "Tell me the stories I missed. Slowly is fine.",
        ]),
        "ja": _pool(_T_JA, [
            "その後どうしてた？きみの話が聞きたいな。",
            "いつ来てもぼくは変わらないよ。",
            "ひさしぶりだね。気軽に話さない？",
            "たまった話はゆっくり聞かせてね。",
        ]),
    },
}


def pick(category: str, language: str | None) -> tuple[str, str]:
    """카테고리·언어의 풀에서 (제목, 본문) 1개 랜덤 선택. 언어 버킷 먼저, 그 안에서 랜덤."""
    pool = i18n.pick(_POOLS[category], language)
    return random.choice(pool)
