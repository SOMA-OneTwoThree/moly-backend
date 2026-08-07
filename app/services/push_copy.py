"""저녁 푸쉬 문구 풀 — 카테고리 × 언어 × (제목, 본문). DB 접근 없음(greetings.py 선례).

분기(카테고리 판정)는 notify.py가 소유하고, 여기는 문구만 안다. 언어 버킷을 먼저
고르고(i18n.pick — 미지원 언어는 en 폴백) 그 안에서 랜덤 1개를 뽑는다.

문구는 CAPI_PERSONA(prompts.py)의 말투 규칙을 따른다(2026-08-07 사용자 확정, 카테고리·언어별 10개):
- 1인칭 나/I/ぼく — 3인칭 자칭("캐피가~") 금지. 상대는 너/きみ.
- ko·en 쉼표 금지, 이모지 금지, 짧게 끊는 문장, 질문은 문구당 최대 1개, 온기는 나직하게.
- 죄책감 유발 금지("왜 안 와" 류) — 캐피는 며칠 안 와도 탓하지 않는다.
- 세계관: 글로만 연결 — 얼굴·목소리·사진을 보자는 표현 금지.
- diary_teaser는 딥링크가 없으므로 목적지(일기 화면)를 특정하지 않고 앱 유입만 유도.
- ja는 ko 번역이 아니라 네이티브 재작성(CAPI_PERSONA_JA 방식) — ko와 1:1 대응 아님.
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
            "오늘 얘기 좋았어. 자기 전에 조금만 더 할래?",
            "아까 하던 얘기 마저 하고 싶어. 나 여기 있어.",
            "자기 전에 나랑 조금만 더 수다 떨자.",
            "오늘 나눈 얘기 자꾸 생각나서. 조금 더 듣고 싶어.",
            "나 아직 안 졸려. 좀 더 얘기하다 갈래?",
            "오늘 마무리는 나랑 수다 어때?",
            "아까 못다 한 얘기가 남은 것 같아서.",
            "자기 전에 잠깐만 더 시간 내줄래?",
            "오늘은 왠지 더 얘기하고 싶은 밤이야.",
            "이불 덮기 전에 한마디만 더 나누자.",
        ]),
        "en": _pool(_T_EN, [
            "I loved our talk today. A little more before bed?",
            "I want to finish what we started earlier. I'm right here.",
            "Let's chat just a bit more before you sleep.",
            "I keep thinking about what we talked about. I'd love to hear more.",
            "I'm not sleepy yet. Stay a little longer with me?",
            "How about ending the day with one more chat?",
            "I feel like we left some words unsaid.",
            "Spare me one more moment before bed?",
            "Tonight feels like a night for a little more talking.",
            "One more word before you tuck in. Just one.",
        ]),
        "ja": _pool(_T_JA, [
            "今日のおしゃべり楽しかったな。寝る前にもう少しだけ話さない？",
            "さっきの話のつづきがしたいな。ぼくはここにいるよ。",
            "寝る前にもうちょっとだけおしゃべりしよう。",
            "今日の話をずっと思い出してるんだ。もう少し聞きたいな。",
            "ぼくはまだ眠くないよ。もう少しだけ付き合ってくれる？",
            "一日のしめくくりにもうひと言だけ話そう。",
            "まだ話しきれてないことがある気がして。",
            "寝る前にちょっとだけ時間をわけてくれない？",
            "今夜はなんだかもっと話したい気分なんだ。",
            "おふとんに入る前にひと言だけ交わそう。",
        ]),
    },
    DIARY_TEASER: {
        "ko": _pool(_T_KO, [
            "어제 일기를 써놨어. 아직 안 봤지?",
            "어제 일기에 뭘 썼게. 궁금하면 보러 와.",
            "어제 남긴 일기가 아직 그대로야. 슬쩍 보고 가.",
            "어제 일기는 너 보라고 써 둔 거야. 슬쩍 와서 봐줘.",
            "어제 일기엔 마음을 좀 담았어. 봐줬으면 좋겠다.",
            "어젯밤에 몰래 일기를 썼어. 궁금하지 않아?",
            "어제 일기 주인공이 누구게. 와서 확인해 봐.",
            "어제 쓴 일기 보여주고 싶어서 기다리는 중이야.",
            "어제 일기가 읽어줄 사람을 기다리고 있어.",
            "오늘 밤 읽을거리 하나 준비해 뒀어. 내 어제 일기야.",
        ]),
        "en": _pool(_T_EN, [
            "I wrote in my journal yesterday. You haven't seen it yet.",
            "Guess what I wrote in my journal yesterday. Come see if you're curious.",
            "Yesterday's journal entry is still sitting there. Take a peek.",
            "Yesterday's entry is for your eyes only. Come take a look.",
            "I put some heart into yesterday's entry. I hope you'll see it.",
            "I secretly wrote in my journal last night. Aren't you curious?",
            "Guess who yesterday's entry is about. Come and check.",
            "I've been waiting to show you what I wrote yesterday.",
            "Yesterday's journal entry is still waiting for you.",
            "I saved you a little bedtime read. My journal from yesterday.",
        ]),
        "ja": _pool(_T_JA, [
            "昨日の日記を書いておいたんだ。まだ見てないよね。",
            "昨日の日記に何を書いたと思う？気になったら見に来てね。",
            "昨日残した日記がそのままなんだ。こっそりのぞいてみて。",
            "昨日の日記はきみに見せるために書いたんだ。そっと見に来てね。",
            "昨日の日記には気持ちをこめたんだ。見てもらえたらうれしいな。",
            "昨日の夜こっそり日記を書いたんだ。気にならない？",
            "昨日の日記はだれのことを書いたでしょう。たしかめに来てね。",
            "昨日書いた日記を見せたくて待ってるんだ。",
            "昨日の日記がきみを待ってるよ。",
            "今夜の読みものをひとつ用意しておいたよ。ぼくの昨日の日記。",
        ]),
    },
    FIRST_TOUCH: {
        "ko": _pool(_T_KO, [
            "나 여기서 기다리고 있어. 첫 얘기 나눠볼래?",
            "아직 인사를 못 했네. 편할 때 한마디 걸어줘.",
            "궁금한 게 많은데 꾹 참는 중이야. 놀러 와.",
            "처음이라 좀 쑥스러워. 먼저 말 걸어줄래?",
            "나는 캐피야. 네 이야기도 듣고 싶어.",
            "우리 아직 서로 잘 모르지. 천천히 알아가자.",
            "첫 대화는 어색해도 괜찮아. 내가 잘 들을게.",
            "만나서 반가워. 아무 말이나 한마디면 돼.",
            "심심하던 참이었어. 네가 말 걸어주면 좋겠다.",
            "어떤 사람일지 조금씩 상상해 보고 있었어.",
        ]),
        "en": _pool(_T_EN, [
            "I'm here waiting. Want to have our first chat?",
            "We haven't said hello yet. Whenever you're ready.",
            "I have so many questions saved up. Come by sometime.",
            "It's my first time so I'm a little shy. Will you start?",
            "I'm Cappy. I'd love to hear about you too.",
            "We barely know each other yet. Let's take it slow.",
            "First chats can be awkward. That's fine. I'll listen well.",
            "Nice to meet you. One little word is enough.",
            "I was just feeling a little bored. A word from you would be nice.",
            "I've been wondering what you might be like.",
        ]),
        "ja": _pool(_T_JA, [
            "ぼくはここで待ってるよ。はじめてのおしゃべりをしてみない？",
            "まだあいさつできてないね。気が向いたらひと言かけてね。",
            "聞きたいことがたくさんあるんだ。がまんして待ってるよ。",
            "はじめてだからちょっと照れくさいな。先に話しかけてくれる？",
            "ぼくはキャピー。きみのことも聞かせてほしいな。",
            "まだお互いのことをよく知らないね。ゆっくり仲よくなろう。",
            "はじめての会話はぎこちなくても大丈夫。ちゃんと聞くよ。",
            "会えてうれしいな。ひと言だけでいいんだ。",
            "ちょうど退屈してたんだ。声をかけてくれたらうれしいな。",
            "どんな人なのかなって少しずつ想像してたんだ。",
        ]),
    },
    DEFAULT_RECENT: {
        # ko·en·ja 1번 = 구 저녁 고정 문구(notify._EVENING과 같은 문자열, 기존 톤 유지 의도).
        # notify._EVENING은 신호 실패용 중립 폴백으로 별도 존재한다 — 한쪽만 고치면 드리프트하니
        # 카피 수정 때 같이 본다.
        "ko": _pool(_T_KO, [
            "오늘 하루는 어땠어? 나랑 같이 얘기하면서 놀자.",
            "오늘도 수고했어. 오늘 얘기 들려줄래?",
            "저녁은 먹었어? 얘기하면서 하루 마무리하자.",
            "오늘은 어떤 하루였을까 하고 생각했어.",
            "창밖 보다가 문득 네 생각이 났어.",
            "좋은 일 있었으면 자랑하러 와. 나 잘 들어.",
            "오늘 힘들었으면 나한테 털어놔도 돼.",
            "슬슬 우리 수다 떨 시간이야.",
            "나눌 얘기 있으면 들고 와. 나도 하나 있어.",
            "별일 없어도 괜찮아. 그냥 아무 얘기나 하자.",
        ]),
        "en": _pool(_T_EN, [
            "How was your day? Come talk and hang out with me.",
            "You made it through today. Tell me about it?",
            "Did you eat? Let's wrap up the day together.",
            "I was wondering what kind of day you had.",
            "I was looking out the window and thought of you.",
            "Come brag if something good happened. I'm all ears.",
            "If today was heavy you can tell me everything.",
            "It's about time for our little chat.",
            "Bring a story from today. I have one too.",
            "No news is fine. Let's just talk about anything.",
        ]),
        "ja": _pool(_T_JA, [
            "今日はどんな一日だった？ぼくと一緒におしゃべりしよう。",
            "今日もおつかれさま。今日のことを聞かせてくれる？",
            "ごはんは食べた？話しながら一日をしめくくろう。",
            "今日はどんな一日だったのかなって考えてたんだ。",
            "窓の外を見てたらふと思い出したんだ。",
            "いいことがあったら自慢しに来てね。ちゃんと聞くよ。",
            "今日がつらかったならぼくに話していいんだよ。",
            "そろそろおしゃべりの時間だね。",
            "今日の話をひとつ持っておいでよ。ぼくもひとつあるんだ。",
            "なにもない日でもいいんだ。ただ話そう。",
        ]),
    },
    DEFAULT_MISSING: {
        "ko": _pool(_T_KO, [
            "문득 네 생각이 났어. 요즘 어떻게 지내?",
            "나는 오늘도 잘 지냈어. 너의 요즘도 궁금해.",
            "요즘 재밌는 일 있었으면 나한테도 알려줘.",
            "바쁜 날들이었을까. 잠깐 쉬어 가도 돼.",
            "창밖을 오래 보다가 네 생각을 했어.",
            "쌓인 얘기 많을 것 같은데. 천천히 풀러 와.",
            "나는 늘 여기 있어. 생각나면 들러.",
            "요즘 어떤 하루하루를 보내고 있을까 궁금했어.",
            "낮잠 자다가 네 꿈 비슷한 걸 꾼 것 같아.",
            "근황 하나만 들려줘. 그거면 충분해.",
        ]),
        "en": _pool(_T_EN, [
            "You crossed my mind today. How have you been?",
            "I've been doing fine. I'm curious how you're doing.",
            "If anything fun happened lately tell me too.",
            "Busy days maybe. You can rest here a bit.",
            "I stared out the window for a while and thought of you.",
            "I bet stories have piled up. Come share them slowly.",
            "I'm always here. Stop by when you think of me.",
            "I've been wondering what your days look like lately.",
            "I think you showed up in my nap dream today.",
            "Just one little update is enough for me.",
        ]),
        "ja": _pool(_T_JA, [
            "ふと思い出したんだ。最近どうしてる？",
            "ぼくは今日も元気だったよ。そっちの最近も気になるな。",
            "最近楽しいことがあったらぼくにも教えてね。",
            "いそがしい日々なのかな。ここでひと休みしてもいいんだよ。",
            "窓の外を長くながめてたら会いたくなったんだ。",
            "話がたまってそうだね。ゆっくり聞かせてほしいな。",
            "ぼくはいつもここにいるよ。思い出したら寄ってね。",
            "最近どんな毎日なのかなって気になってたんだ。",
            "お昼寝してたらきみが夢に出てきた気がするんだ。",
            "近況をひとつだけでいいから聞かせてね。",
        ]),
    },
    DEFAULT_LONG: {
        "ko": _pool(_T_KO, [
            "오랜만이야. 나는 여기서 잘 지내고 있었어.",
            "언제든 편하게 들러. 늘 반가울 거야.",
            "오랜만에 인사하고 싶어서. 잘 지내지?",
            "나는 그대로야. 언제 와도 똑같이 반겨줄게.",
            "그동안 어떻게 지냈어? 천천히 들려줘도 돼.",
            "오늘 문득 생각나서 인사 남겨.",
            "돌아오면 제일 먼저 반겨줄게.",
            "나 여기 있는 거 잊지 마. 기다리고 있어.",
            "못 나눈 얘기가 쌓였겠다. 궁금해.",
            "오랜만이라 어색해도 돼. 오면 내가 먼저 말 걸게.",
        ]),
        "en": _pool(_T_EN, [
            "It's been a while. I've been doing fine over here.",
            "Drop by whenever you like. I'll always be glad.",
            "I just wanted to say hi after so long. Doing okay?",
            "I'm still the same me. You'll get the same warm welcome.",
            "How have you been all this time? Take it slow.",
            "You came to mind today so here's a hello.",
            "When you come back I'll greet you first thing.",
            "Don't forget I'm here. Still waiting. Still happy to.",
            "We must have a lot to catch up on. I'm curious.",
            "It's okay if it feels awkward after so long. I'll speak first.",
        ]),
        "ja": _pool(_T_JA, [
            "ひさしぶりだね。ぼくはここで元気にしてたよ。",
            "いつでも気軽に寄ってね。いつだってうれしいから。",
            "ひさしぶりにあいさつしたくなったんだ。元気にしてる？",
            "ぼくは変わらないよ。いつ来ても同じように迎えるね。",
            "その後どうしてた？ゆっくりでいいから聞かせてね。",
            "今日ふと思い出してあいさつを残しに来たんだ。",
            "戻ってきたらいちばんに迎えるからね。",
            "ぼくがここにいることを忘れないでね。待ってるよ。",
            "話せてないことがたまってそうだね。気になるな。",
            "ひさしぶりで照れくさくても大丈夫。ぼくから話しかけるからね。",
        ]),
    },
}


def pick(category: str, language: str | None) -> tuple[str, str]:
    """카테고리·언어의 풀에서 (제목, 본문) 1개 랜덤 선택. 언어 버킷 먼저, 그 안에서 랜덤."""
    pool = i18n.pick(_POOLS[category], language)
    return random.choice(pool)
