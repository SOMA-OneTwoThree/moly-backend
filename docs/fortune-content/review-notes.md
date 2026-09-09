# 운세 언어별 검수 기록

기준일: 2026-09-08 · 이전 버전 검수 이력 · [운세 기준 문서](../DAILY-FORTUNE.md)

이 문서는 당시 한국어·영어·일본어 집필 및 교차 검수 이력을 보존한다. 현행 문체(마침표·5문장 단락)의 전량 검수는 [한국어](ko-full-editorial-review.md)와 [영어·일본어](multilingual-editorial-review.md) 기록을 따른다. 아래 이전의 마침표 생략·짧은 문장 규칙은 최신 기준이 아니다

## 한국어

기준일: 2026-09-08 · 카탈로그: `fortune-copy.v2-field-readings.1`

정본은 `app/resources/fortune/copy.v2.json`이며 전체 문구와 최종 테스트 결과는 [기준 문서](../DAILY-FORTUNE.md)에 정리한다

### 분야 전면 재작성

애정·금전·일학업·활력 각각 10점수 구간 × 20묶음, 합계 800묶음 / 1,600문장을 새로 작성했다
한국어 담당이 애정·일학업을, 총괄이 금전·활력을 집필하고 한국어 담당이 금전·활력을 독립 검수했다

- 첫 문장은 자기 분야의 해석, 둘째는 그 해석에 맞는 제안·기회
- 분야에서 총평의 `~는 날이야`를 기본 어미로 재사용하지 않음
- `~겠어`, `흐름이야`, `가능성이 보여`, `기운이 모여` 및 문장 끝 마침표 없음
- 연인·직업·재학 상태를 전제하거나 상대의 호감·수익·건강 결과를 보장하지 않음
- 낮은 점수는 부담을 덜 제안, 중간은 일상의 만족, 높은 점수는 참여와 표현의 기회를 강조
- 점수가 다른 같은 ID에 고정 소재를 강요하지 않고 같은 분야·점수·ID의 세 언어 뜻을 맞춤
- 분야 첫 문장이 조언만 담고 있던 애정 원고와 `너다운 빛`이라는 추상 표현을 수정
- 금전·활력 독립 검수에서 `구매를 고를 눈`, `장바구니를 마쳐봐`, `산책을 함께 제안` 등 8필드를 자연스럽고 뜻이 분명하게 교열
- 전체 두 문장 묶음 중복 없음, 짧은 자연어를 억지 동의어로 바꾸지 않음

### 총평 세부 교열

승인된 총평 구조 200묶음을 모두 다시 읽고 39묶음의 41필드를 제한 교열했다
제목·풀이·해볼 것·조심할 것 구조와 점수·ID의 원래 의미는 유지한다

`좋은 흐름`, `기운이 모이는` 같은 추상 표현을 상황·계획·기회·느낌으로 풀었다
총평이 일상적인 대화·노력·쉼을 다루는 것은 허용하되 분야별 점수나 결과를 단정하는 말로 확대하지 않았다
영어·일본어 담당에게 변경된 의미를 대조하도록 전달했으며 자연스러운 기존 번역을 한국어 어미에 맞춰 기계적으로 바꾸지는 않았다

### 독립 서버 검토

카탈로그·관련 테스트·문서 생성기·편집 명세 변경을 읽고 진행을 막는 결함을 발견하지 못했다

- 문구 버전만 올리고 선택 상태·DB 구조를 유지함
- 같은 두 문장이 모두 복제된 묶음과 묶음 안의 동일 두 문장을 거부함
- 다른 해석과 함께 쓰이는 짧은 제안은 재사용할 수 있음
- 당일 snapshot 보존과 다음 날 기존 v2 순서의 진행을 개발 DB 테스트에서 확인하도록 구성함
- 문서 생성기는 폐기된 고정 주제 대신 실제 서버 문구를 표시함

영어 저·중·고점 36묶음도 별도로 대조했다
답장 속도로 상대 마음을 판단하지 말라는 뜻이 약해진 한 문장과 직역투 표현을 영어 담당에게 전달해 교열했다

이 기록은 에이전트 집필·자기검수·교차검토 결과이며 외부 사람 원어민 검수나 실제 기기 검증을 뜻하지 않는다

## English

2026-09-08 · `fortune-copy.v2-field-readings.1`

Canonical asset: `app/resources/fortune/copy.v2.en.json` · [Complete copy and final validation](../DAILY-FORTUNE.md)

### Editorial standard
- Two complete sentences per bundle, with no terminal punctuation
- Sentence 1 interprets today's luck in that specific field; sentence 2 gives a relevant suggestion, reassurance, or way to enjoy favorable luck
- American English written for a daily fortune card, rather than literal Korean syntax
- No mystical filler about flows, gathered energy, or visible possibilities
- Keep the Korean route and variant's situation, degree of favorability, and advice aligned
- No assumed partner, job, student status, medical diagnosis, financial return, or other person's definite feelings
- No mechanical score adjectives or copy-and-paste high-score intensifiers
- Vary second-sentence openings naturally; do not make every suggestion a prohibition

### Final review and freeze
- All 40 English field routes completed: 800 bundles / 1,600 sentences
- All final Korean edits synchronized, including love-d80 v12's change from an instruction to a prediction and money-d70 v10's check before adding extras
- 244 field expressions received a separate naturalness pass after drafting, followed by peer-review fixes
- Removed literal constructions such as Today suits, comfortable rest, pleasing plan, actual form, usage criteria, and repeated room-to forecasts
- Kept genuinely natural repeated short words where useful rather than forcing novelty
- Korean peer reviewer checked 36 sample bundles against English and identified one material meaning issue: love-d00 v01 formerly implied that a later judgment by response speed would be acceptable; now explicitly says not to judge feelings by reply speed
- Peer suggestions on notification muting, self-confidence repetition, spending language, and rest wording incorporated or addressed with equally direct phrasing
- Existing 200 English overall bundles reread in full; 62 fields refined for directness or synchronized with the Korean overall review, preserving their situations and advice
- Final checks: 40 files, 800 bundles, 1,600 sentences; no missing IDs, terminal punctuation, exact sentence duplicates, exact bundle duplicates, or same-route/same-field SequenceMatcher candidates at 0.72 or above
- Combined English overall and field expressions: 2,800 with no exact duplicates
- These are automated checks plus agent editorial review, not a claim of external human native-speaker approval

The 0.72 similarity scan was an editorial aid, not a runtime rejection rule. Natural short suggestions may recur when the complete interpretation and advice remain distinct.

## 日本語

2026-09-08 · `fortune-copy.v2-field-readings.1`

Canonical asset: `app/resources/fortune/copy.v2.ja.json` · [Complete copy and final validation](../DAILY-FORTUNE.md)

### Delivery

- Four fields × ten score bands × twenty bundles: 800 complete bundles / 1,600 sentences
- Same score bucket and v01–v20 ID preserve the final Korean judgement, situation, and suggestion
- All 200 overall bundles reviewed, with targeted edits to 58 fields in 50 bundles after reading all 1,200 expressions

### Editorial decisions

Each field starts with a direct judgement about closeness, everyday spending, learning/achievement, or experienced energy, then gives a fitting invitation or suggestion

- Love does not presume a partner or promise reciprocation
- Money discusses usefulness, budget, and satisfaction without predicting income or investment returns
- Work/study covers understanding, concentration, effort, and results without requiring employment or enrollment
- Energy describes everyday activity/rest without diagnosing or promising medical outcomes
- Low scores describe a manageable difficulty; higher scores describe a credible opportunity, never unlimited money, attraction, or stamina
- Sentence-final full stops `.` and `。` removed throughout
- No category judgement ends in the overall-style `〜日`; natural `〜そう`, `〜かも`, `〜よ`, and `〜とき` remain according to meaning
- `流れ` as an abstract fortune explanation eliminated in the overall text; the only substring in all 2,800 expressions is literal music playback `一曲流れる間`
- Tangible purchased objects use `物`; abstract alternatives, services, and nominalized clauses use `もの` where natural

### Cross-language and independent feedback

- Read and matched all final Korean category manuscripts while composing new Japanese sentences directly
- Synchronized Korean love-d80 v12 / love-d90 v05 review changes
- Synchronized final money-d70 v10 advice: check that necessities are included before adding extras
- Reviewed all 41 Korean overall edit locations; retained an already direct Japanese equivalent when it already matched, otherwise localized the final Korean meaning
- Incorporated root's independent Japanese feedback on notification wording, `合う答え`, prepared opportunities, applying experience, activity/settlement phrasing
- Existing Japanese overall copy was additionally reviewed end to end, including headline, all three explanatory sentences, do, and pause

### Checks

- 2,800 overall-plus-category expressions globally unique: 0 exact duplicates
- Same route / same field SequenceMatcher threshold >= 0.72, minimum 12 characters: 0 near-duplicate pairs
- 40 category routes × 20 IDs × 2 nonempty Japanese sentences present
- No Korean text, terminal full stops, or category `〜日` endings

This is agent editorial and source alignment review, not a claim of human native-speaker certification or device QA

The 0.72 scan was an editorial aid, not a runtime ban on shared everyday words. Natural short suggestions may recur if the complete bundle remains distinct.
