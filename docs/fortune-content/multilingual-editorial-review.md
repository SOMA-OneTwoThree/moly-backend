# 영어·일본어 전체 현지화 및 검수

2026-09-09 · [운세 기준 문서](../DAILY-FORTUNE.md) · PR·원격 push·서버 배포 전

## 범위와 기준

승인된 한국어 원고를 기준으로 영어·일본어 각 1,000문단을 현지화했다. 언어마다 총평 200묶음(제목·5문장 풀이·해볼 것·조심할 것), 애정·금전·일/학업·활력 각 200문단이다. 같은 분야·점수대·ID의 판단·조건·제안은 같고 표현은 각 언어의 자연스러운 문장으로 다시 작성했다.

- 첫 문장에서 해당 운세를 해석하고 구체적인 제안을 이어간다. 막연한 위로, 추상어의 어색한 결합, 한국어 어순을 옮긴 직역을 수정한다.
- 영어는 일상적인 미국 영어와 직접적인 제안, 일본어는 일관된 です・ます 문체를 사용한다. 마침표를 쓰고 문장마다 강제 개행하지 않는다.
- 낮은 점수의 어려움·예방, 중간 점수의 일상적 만족, 높은 점수의 기회·적극성을 한국어와 동일하게 유지한다. 한 분야의 점수가 다른 분야나 총평의 판정을 바꾸지 않는다.
- 상대의 마음·수입·건강 결과를 보장하지 않으며 원문에 없는 관계·직업·장소·인원 조건을 추가하지 않는다.
- 전체 문단을 작성한 뒤 기존 flow 3/text 2 구간에 나눈다. 모든 본문은 5문장이지만 화면의 고정 5줄을 보장하지 않는다.

한국어 파일 SHA-256은 `977a6afa4a4288ab8340b125de371c6f5894b83ad023b74fb765046bdc741212`로 이전 커밋과 바이트까지 같다. 행운색 12개와 HEX, 점수·선택 로직, API 스키마, 모바일 ARB는 변경하지 않았다. 새 카탈로그와 영어·일본어 자산은 `fortune-copy.v3-editorial.1`, 한국어 자산은 `fortune-copy.v3-ko-editorial.1`이다.

## 작성과 판단 분리

| 원고 | 작성 | 독립 판단 | 최종 범위 |
| --- | --- | --- | --- |
| 영어 총평·애정·금전·일/학업 | sample_writer | sample_judge | 800/800 |
| 영어 활력 | root | sample_judge | 200/200 |
| 일본어 총평·금전·일/학업 50~100점 | fortune_ja_rewrite | root | 500/500 |
| 일본어 활력·일/학업 0~49점 | sample_judge | root | 300/300 |
| 일본어 애정 | root | sample_writer | 200/200 |

각 판단자는 대상 언어만 먼저 읽고 한국어 전체와 대조했다. 수정 요청에는 실제 구절과 이유를 남겼으며, 수정 후 해당 문단 전체(총평은 제목·행동·주의까지)를 다시 읽었다. 다섯 기준은 자연스러움, 즉시 이해, 운세 해석, 점수·확신 강도, 분야·조건 보존이다. 모두 내부 편집 기준 최소 4/5로 판정했다. 이 판정은 모델의 편집 검수이며 원어민 전문가 인증이 아니다. 자동 검사는 문장 수·누락·중복·버전·해시를 확인하고 자연스러움 판단을 대신하지 않는다.

## 결과와 호환성

| 항목 | 영어 | 일본어 |
| --- | --- | --- |
| 본문 / 문장 | 1,000 / 5,000 | 1,000 / 5,000 |
| 전량 독립 대조·수정본 재독 | 완료 | 완료 |
| 완성 문단 중복 | 0 | 0 |
| 본문 길이(공백 포함) | 234~441자, 중앙값 324자 | 113~180자, 중앙값 141자 |
| 미해결 편집 지적 | 0 | 0 |

일부 일상 소재와 일반적인 조언은 반복될 수 있다. 완성 문단의 중복을 제거했고, 모든 문장의 소재가 서로 완전히 다르다고 주장하지 않는다. 초기 참고 길이에 억지로 맞추기보다 의미와 읽기 쉬운 문장을 우선했다.

DB 마이그레이션·데이터 보충·v3 cursor 초기화는 필요 없다. 기존 당일 snapshot은 이전 버전의 언어별 원고와 공개 권한 그대로 반환하며 새로 생성되는 결과부터 새 원고를 저장한다. 이 편집은 기존 개발 DB를 이용한 통합 검증 대상이며 실행 중인 개발 API나 운영 서버에 배포한 상태가 아니다.

### 실행 검증

| 검증 | 결과 |
| --- | --- |
| `uv run pytest -q` | 2,211 통과, 환경 의존 114 skip, 기존 Starlette deprecation 경고 1개 |
| `MOLY_FORTUNE_TEST_ENV=dev uv run pytest -q tests/integration/test_fortune_variants.py` | 기존 개발 DB에서 8 통과, fixture의 임시 사용자·데이터 정리 완료 |
| 변경 Python 4파일 Ruff | 통과 |
| `uv run python scripts/build_fortune_copy_docs.py --check` | 전체 전문 14문서와 실행 자산 일치 |
| `git diff --check` | 통과 |
| 검수본 → 실행 자산 대조 | 영어·일본어 각 50경로/1,000문단 전부 일치, 검수 후 미확인 원고 없음 |
| 한국어·행운색 보존 | 한국어 파일은 HEAD와 바이트 일치, 두 해외 언어 색 이름·HEX 동일 |
| 이전 해외 언어 완성 문단 재사용 | 영어·일본어 모두 0 |

기존 schema 3/4와 이전 독립 점수·한국어 단독 편집 copy 버전의 당일 결과 보존을 회귀 테스트했다. 스키마·권한·동시 열람 및 선택 cursor의 기존 테스트도 통과했다. 로컬 Docker나 새 DB, 개인 AWS 계정을 사용하지 않았다. 실행 중인 앱에서 새 원고의 실제 화면 폭·글자 배율은 이번 서버 편집 검증에 포함하지 않았다.

### 최종 자산

| 파일 | SHA-256 |
| --- | --- |
| `copy.v2.en.json` | `8432c85864f07e253ebd1ebc014314aa0ff6e56ef7cc6663e822faf84d5fcee1` |
| `copy.v2.ja.json` | `e47c80050df6e9cb14a85b7bef8d3cd47d9f45bf410a8cbeb82082b628bd3cc4` |

## 수정 근거

아래 인용은 교정 전 표현이다. 현재 전문은 [총평·분야 대조표](../DAILY-FORTUNE.md#부록-a-종합-운세-전문--10점수-구간--20묶음--3언어)와 서버 JSON에서 확인한다. 모든 항목은 수정 뒤 전체 문단 재독을 마쳤다. 동일 문단의 복수 표현 지적은 같은 ID로 표시한다.

### 영어

| 경로·ID | 교정 전 구절 | 이유 |
| --- | --- | --- |
| `overall.d00.general/v07` | erase a disappointment | Abstract verb/noun combination makes an ordinary feeling sound mechanically removable. |
| `overall.d00.general/v08` | Unspoken discomfort could come out all at once today. | Discomfort coming out is unnatural; the Korean means suppressed frustrations erupt emotionally. |
| `overall.d00.general/v09` | read anything you'll be responsible for yourself | Final yourself initially attaches to responsible for, requiring reparsing to understand personal checking. |
| `overall.d00.general/v10` | commit all your future free time | Broadens later plans into all future free time, an exaggerated scope absent in Korean. |
| `overall.d00.general/v12` | an unmanageable condition / an uncomfortable condition | Literal Korean condition produces stiff English; these are terms or demands the person cannot manage or accept. |
| `category.energy.d00.general/v02` | Enthusiasm could carry you further than your energy lasts today. | The carry further/energy lasts comparison combines distance and duration awkwardly. |
| `category.energy.d00.general/v11` | try not to push your return home later | Literal delayed-return phrasing sounds mechanical. |
| `overall.d10.general/v01` | Everything you need to do could seem to arrive at once | The Korean says existing tasks look overwhelming, not that they arrive simultaneously. |
| `overall.d10.general/v07` | leave you feeling short | Feeling short is incomplete here and may suggest money or stature rather than insufficient accomplishment. |
| `overall.d10.general/v13` | make small things feel heavy | Literal emotional weight obscures the simple Korean meaning of small tasks becoming difficult. |
| `overall.d10.general/v16` | Finishing a little preparation | Unnatural quantifier and completion phrasing. |
| `overall.d10.general/v19` | Trying to feel different immediately may lead you to an unusual choice. | Unusual is not equivalent to normally unwanted; feel different is vague. |
| `overall.d20.general/v03` | when an important schedule is involved | An important schedule is not an idiomatic way to refer to important plans. |
| `overall.d20.general/v11` | an inconvenient term | Literal term/condition collocation is stiff and makes the drawback unclear. |
| `overall.d20.general/v19` | Everyone doesn't have to like the same thing. | Negation scope can mean nobody has to like it instead of tastes differing. |
| `category.energy.d10.general/v14` | Time spent physically comfortable is enough. | Adverb/adjective construction is awkward and clinical for plain prose. |
| `category.energy.d20.general/v17` | Set a small limit on what you'll finish before you begin. | A small limit on finishing sounds like task management jargon rather than ordinary advice. |
| `overall.d30.general/v03` | a judgment of all of you | The phrase sounds like a judgment of a group, not of the reader as a whole person. |
| `overall.d30.general/v10` | Take other people's reactions as a reference | Literal reference phrasing is stiff for ordinary personal reactions. |
| `overall.d30.general/v12` | Letting go of one small insistence | Countable insistence sounds translated and obscures a simple change in method. |
| `overall.d40.general/v03` | Send anything that needs a reply | Unclear whether the reader should answer received messages or send a message and wait; KO means the latter. |
| `overall.d40.general/v14` | when there's a clear reason to need or enjoy it | Reasons to need or enjoy something is strained English. |
| `overall.d40.general/v19` | Enjoying something against your taste could feel difficult today. | Against your taste is literal and unidiomatic. |
| `overall.d40.general/v20` | Let the thought that you must do more rest for the evening. | Telling a thought to rest personifies an abstract noun awkwardly. |
| `category.energy.d30.general/v05` | Give your time at home some room | Time given room is vague and less natural than the simple instruction to allow rest. |
| `category.energy.d30.general/v14` | After a portion of exercise or tidying | A portion of tidying is an unnatural collocation. |
| `overall.d50.general/v09` | give the experiment enough importance to change your bigger plans | An overly complex explanation of a simple idea. |
| `overall.d50.general/v14` | Every concern doesn't need solving today. | Negation scope is avoidably ambiguous. |
| `overall.d50.general/v17` | Take just one useful adjustment from anything unpleasant. | Taking an adjustment from an experience is unnatural. |
| `overall.d60.general/v03` | Starting the conversation alone | Alone may be read as without another person instead of the act itself. |
| `overall.d60.general/v16` | Ask a light question | Light question is an awkward literal expression in this context. |
| `overall.d60.general/v19` | Enjoy the time you've made easier for yourself. | Time itself is not made easier; KO means enjoying the resulting freedom. |
| `overall.d60.general/v20` | Establish one small guide that fits your own life. | An abstract guide without an object sounds incomplete and translated. |
| `overall.d70.general/v14` | Give your favorite one enough time to enjoy it. | The implied subject of enjoy becomes the favorite object, making the sentence awkward. |
| `overall.d70.general/v16` | Don't give up alone because you expect a refusal. | Alone literally translates 혼자 and obscures giving up before asking. |
| `overall.d80.general/v06` | Finishing the decision frees the time spent worrying for something enjoyable. | Finishing a decision and freeing time already spent both make the sentence harder to parse. |
| `overall.d80.general/v09` | A chance encounter with information could prove useful today. | Encounter with information sounds unnecessarily formal and translated. |
| `overall.d80.general/v11` | An effort you've tolerated out of habit may be avoidable. | An effort tolerated is not a natural collocation for everyday inconvenience. |
| `overall.d80.general/v14` | Give an interest you already love a little more depth. | Giving an interest depth sounds like editorial instruction rather than ordinary advice. |
| `overall.d90.general/v04` | Give the new experience enough time to enjoy it. | Object incorrectly becomes the implied enjoy subject. |
| `overall.d90.general/v05` | Don't leave an interest at saying you'll do it someday. | Leave an interest at saying is unnatural. |
| `overall.d90.general/v16` | Enjoyment after a decision could feel especially strong today. | Abstract nominal construction sounds translated. |
| `overall.d90.general/v19` | Take a chance to experience it firsthand when available. | Take a chance often means risk; KO offers taking an available opportunity. |
| `category.energy.d50.general/v13` | Do enough that you'd like to return tomorrow | Enough suggests a minimum, while KO sets an upper limit so the reader still wants to return. |
| `category.energy.d60.general/v02` | Look for a short way to try it | Short way normally refers to a route, not the duration of a trial. |
| `category.energy.d70.general/v04` | Do enough that you can still laugh and chat afterward. | The amount should be an upper limit, not a minimum to accomplish. |
| `category.energy.d70.general/v06` | could set a pleasant start to the day | Set a start is an unnatural collocation. |
| `category.energy.d80.general/v05` | The excitement of being active with a group could last well today. | Last well is not idiomatic for a mood. |
| `category.energy.d90.general/v05` | With meals and breaks remembered, there's room to enjoy it fully. | Passive compression sounds unnatural rather than conversational. |
| `category.energy.d90.general/v12` | Energy from the morning could have you moving more easily than usual today. | Energy from the morning is a literal, unnatural time expression. |
| `category.energy.d90.general/v16` | Walking and moving may feel light enough for a longer outing today. | Walking and moving feel light is a translated collocation. |
| `category.love.d00.general/v17` | Interrupting even an explanation that sounds defensive could lose a chance to clear things up. | The interruption cannot lose a chance itself; subject and verb are awkwardly paired. |
| `category.love.d10.general/v09` | Less private interpretation can make the conversation less demanding. | Private interpretation is literal and formal for dwelling on hidden meanings. |
| `category.love.d10.general/v18` | Don't make today's disappointment stand for all their behavior. | Disappointment standing for behavior obscures the straightforward warning. |
| `category.love.d20.general/v11` | Don't judge a relationship by someone / asking fewer people the same concern | By someone omits their opinion; asking a concern is ungrammatical. |
| `category.love.d20.general/v16` | The wish to move a relationship forward could run ahead of you today. | A wish running ahead of its holder is an opaque metaphor. |
| `category.love.d30.general/v05` | dismiss their question with saying / messages keep missing each other's meaning | With saying and messages missing meaning both use awkward constructions. |
| `category.love.d30.general/v10` | could sharpen the conversation | Sharpen a conversation suggests focus or wit, not an increasingly harsh exchange. |
| `category.love.d30.general/v12` | Too little expression could make your gratitude unclear today. | Expression without a concrete object sounds translated. |
| `category.love.d40.general/v06` | Small attention can feel more natural than carefully crafted words. | Small attention is not an idiomatic expression for a little interest. |
| `category.love.d40.general/v15` | Let a nonurgent message rest while you spend your own time. | Messages resting and spending your own time both sound literal. |
| `category.love.d50.general/v02` | Familiar time with someone close should suit today. | Familiar time is an unnatural compressed noun phrase. |
| `category.love.d50.general/v17` | An amount that doesn't strain either of you suits today. | An amount has no clear object and sounds mechanical. |
| `category.love.d60.general/v18` | A relationship usually occupied with solving problems may find an easier subject. | A relationship cannot be occupied with problem-solving or find a subject; plain human subjects needed. |
| `category.love.d70.general/v04` | A hurt feeling with someone close can be easier to resolve today. | A hurt feeling with someone is unnatural. |
| `category.love.d70.general/v18` | Mention a story or experience you comfortably enjoyed. | Comfortably enjoyed is a literal and unnecessary adverb combination. |
| `category.love.d80.general/v06` | make an awkward connection feel much easier | An awkward connection sounds like a technical connection, not people feeling awkward. |
| `category.love.d80.general/v20` | Offer the same listening / Honesty and trust have a good place | Both are abstract literal combinations. |
| `category.love.d90.general/v10` | Trusting each other with support could feel right today. | Trusting with support is not idiomatic. |
| `category.love.d90.general/v15` | Hear the inconvenience you caused | You hear an explanation, not inconvenience. |
| `category.love.d90.general/v16` | The day is worth more than silently waiting for signs while hiding your feelings. | The day being worth more than an action is a strained comparison. |
| `category.money.d00.general/v03` | Stop the decision | Decisions are postponed or held off, not stopped. |
| `category.money.d00.general/v19` | A cheaper long-term payment | Literal payment phrase obscures paying upfront for longer access. |
| `category.money.d00.general/v20` | rather than losing a place or price | Not idiomatic for fear of missing a booking or price offer. |
| `category.money.d10.general/v16` | Reserving essentials | Essentials are not money; the sentence needs explicit setting aside funds. |
| `category.money.d30.general/v06` | No single large payment doesn't necessarily mean plenty of budget remains. | Fragment-like noun phrase and double negative make a simple idea hard to read. |
| `category.money.d40.general/v17` | Every purchase doesn't require the same standard. | Ambiguous negation scope. |
| `category.money.d40.general/v20` | Write from actual records | Missing object makes the instruction unnatural. |
| `category.money.d50.general/v12` | Your planned living budget should be comfortable to use today. | A living budget comfortable to use is translated collocation. |
| `category.money.d50.general/v19` | The explanation may reveal a concern that wasn't necessary. | Revealing an unnecessary concern is awkward and indirect. |
| `category.money.d60.general/v09` | Compare a service you regularly use with how much you actually use it. | Compares a service with usage instead of its plan or price. |
| `category.money.d60.general/v15` | Don't split more than you need just to qualify for a discount. | Splitting lacks an object and may be read as splitting a bill, not buying excess quantity. |
| `category.money.d90.general/v14` | Claiming the benefits you need could help you find a satisfying price today. | Benefits you need and satisfying price sound literal instead of ordinary shopping language. |
| `category.work.d00.general/v07` | If much of the explanation is difficult | KO specifically checks whether the reader can explain the content themselves, not whether an outside explanation is difficult. |
| `category.work.d00.general/v17` | rather than stepping forward | Vague translated metaphor obscures postponing speaking before ready. |
| `category.work.d30.general/v19` | trying to satisfy every opinion | Opinions are considered or followed, not satisfied. |
| `category.work.d40.general/v15` | Completing a small preparation | Countable preparation sounds unnatural here. |
| `category.work.d40.general/v16` | Take only the reference you need from someone else's finished work | Take a reference sounds like extracting a citation, not learning from an example. |
| `category.work.d50.general/v14` | A small idea is suitable for a light trial today. | Suitable for a light trial sounds technical and translated. |
| `category.work.d60.general/v05` | Communicating one or two points clearly should make the discussion sufficient. | Unnatural collocation makes a simple source meaning sound translated. |
| `category.work.d60.general/v12` | Organizing frequently used material could bring an immediate convenience today. | Unnatural collocation makes a simple source meaning sound translated. |
| `category.work.d70.general/v05` | Explaining something to another person could give your ability a chance to show today. | Literal abstract phrasing obscures a straightforward action or benefit. |
| `category.work.d70.general/v19` | You can use this experience as confidence for the next challenge. | Literal abstract phrasing obscures a straightforward action or benefit. |
| `category.work.d80.general/v01` | This is a good day to show the ability you've prepared. | Literal noun/verb combination is not idiomatic; preserve the source action using ordinary English. |
| `category.work.d80.general/v05` | Applying what you've learned to an actual problem can confirm your confidence. | Literal noun/verb combination is not idiomatic; preserve the source action using ordinary English. |
| `category.work.d80.general/v17` | It's also a reasonable time to show prepared material and hear the next feedback. | Literal noun/verb combination is not idiomatic; preserve the source action using ordinary English. |
| `category.work.d80.general/v19` | If there's something you've wanted to do, set the preparation and first date. | Literal noun/verb combination is not idiomatic; preserve the source action using ordinary English. |
| `category.work.d90.general/v02` | This is a strong day to demonstrate the ability you've prepared. | Overliteral noun/verb structure makes the message hard to read. |
| `category.work.d90.general/v05` | You can calmly explain answers to opposing questions and find a better approach. | Overliteral noun/verb structure makes the message hard to read. |
| `category.work.d90.general/v11` | This is a good day to actively take an opportunity for your ability to be recognized. | Overliteral noun/verb structure makes the message hard to read. |

### 일본어

| 경로·ID | 교정 전 구절 | 이유 |
| --- | --- | --- |
| `category.energy.d00.general/v10` | 移動の多い予定では、疲れやすい日 | awkward topic-predicate |
| `category.energy.d00.general/v11` | 予定をきれいに終える | literal collocation |
| `category.energy.d10.general/v02` | 最初のやる気ほど、長く | ill-formed comparison |
| `category.energy.d10.general/v10` | 約束を楽しむ | meeting translated as promise |
| `category.energy.d10.general/v13` | 遅くまで続く約束 | unnatural promise duration |
| `category.energy.d10.general/v18` | 短い約束 | short meeting not short promise |
| `category.energy.d20.general/v03` | 約束が終わった後 | meeting vs promise collocation |
| `category.energy.d20.general/v11` | 約束が長引き | meeting duration |
| `category.energy.d20.general/v14` | 慌ただしい予定が疲れる | subject-predicate mismatch |
| `category.energy.d20.general/v16` | 音や画面を同時につける | incompatible shared verb |
| `category.energy.d30.general/v01` | 残っている用事を増やす | additional pending tasks, not increasing remaining tasks |
| `category.energy.d30.general/v08` | 音や連絡が疲れる | subject predicate mismatch |
| `category.energy.d40.general/v04` | 始めずにいてみる | unnatural negative try |
| `category.energy.d40.general/v07` | 大きな運動 | literal collocation |
| `category.energy.d40.general/v08` | 趣味を手に取る | incompatible verb |
| `category.energy.d40.general/v14` | 準備をかけず | wrong collocation |
| `category.energy.d40.general/v19` | 変えないでみる | unnatural negative try |
| `category.energy.d50.general/v01` | 残りの用事を増やさず | additional pending tasks meaning |
| `category.energy.d50.general/v16` | 近くから出かける | wrong direction |
| `category.energy.d60.general/v04` | 窮屈な気分を減らす | literal abstraction |
| `category.energy.d60.general/v09` | 荷物が多いときや疲れるまで | ill-formed coordination |
| `category.energy.d60.general/v19` | 帰宅後 | unsupported return-home specificity |
| `category.energy.d70.general/v07` | 帰宅後 | unsupported return-home specificity |
| `category.energy.d90.general/v15` | 二人に合うペース | unsupported two-person restriction |
| `category.money.d00.general/v07` | 軽く感じる追加購入 | literal collocation |
| `category.money.d00.general/v08` | 役割を果たしていない | abstract role instead of share |
| `category.money.d00.general/v10` | 逃す割引 | literal collocation |
| `category.money.d00.general/v20` | 席や価格を逃す | incompatible coordination |
| `category.money.d10.general/v10` | お金の使い方まで面倒に | literal abstraction |
| `category.money.d30.general/v02` | 料金にある変更 | unnatural collocation |
| `category.money.d30.general/v10` | 同じかをそろえる | question predicate mismatch |
| `category.money.d30.general/v14` | 人の価格 | literal collocation |
| `category.money.d40.general/v05` | 買い物で、似た選択肢の間で | repeated particle |
| `category.money.d40.general/v10` | 確かめると気楽な日 | unnatural predicate |
| `category.money.d50.general/v02` | 普段決めた金額 | unnatural collocation |
| `category.money.d50.general/v05` | 決めると気楽な日 | unnatural predicate |
| `category.money.d50.general/v12` | 支払いを残す | money not payment retained |
| `category.money.d60.general/v10` | 人が選んだ価格 / よく選んだ | literal collocation |
| `category.money.d60.general/v13` | 減らしても惜しくない | regret mistranslated |
| `category.money.d60.general/v18` | 金額にも、よい選択 | unnatural collocation |
| `category.money.d70.general/v13` | 次の支払いに参考に | wrong particle |
| `category.money.d70.general/v15` | 支出を確保 | reserve money not expense |
| `category.money.d80.general/v07` | 安くて便利 | AND differs from KO OR |
| `category.money.d80.general/v19` | 利用する費用 | incompatible verb |
| `category.money.d80.general/v03` | 受け取る費用や確認する精算 | unnatural noun complements found in assembled result review |
| `category.money.d90.general/v01` | お金の選択を進める | literal abstraction |
| `category.money.d90.general/v03` | 通り過ぎた特典 | literal collocation |
| `category.money.d90.general/v05` | ほうへ決めて | wrong particle |
| `category.work.d00.general/v12` | 役割をそろえる | clarify division not align roles |
| `category.work.d00.general/v19` | 理解できない部分を通り過ぎ | literal collocation |
| `category.work.d10.general/v07` | 上手に話す重圧 | unnatural collocation |
| `category.work.d10.general/v15` | 決定を待てない | cannot delay own decision, not wait for decision |
| `category.work.d30.general/v07` | 違って覚えていることがありそうな日 | cumbersome literal predicate |
| `category.work.d40.general/v04` | 相手が違って理解した部分 | unnatural adverb complement |
| `category.work.d40.general/v11` | 聞く人が初めて接するつもりで | wrong subject for intention |
| `category.work.d50.general/v02` | 形を飾りすぎて | abstract formatting instruction |
| `category.work.d50.general/v06` | 気楽な日 | unnatural predicate |
| `category.work.d60.general/v09` | それぞれ得意な部分 | comfortable parts not necessarily strong suit |
| `category.work.d60.general/v13` | 初めから慣れないと構えず | unnatural collocation |
| `category.work.d70.general/v01` | 軽く見る | can mean underestimate |
| `category.work.d70.general/v05` | 詳しい内容 | well-known not detailed |
| `category.work.d70.general/v19` | 詳しい部分 | well-known not detailed; author follow-up nomination |
| `category.work.d80.general/v01` | 詳しい部分 | well-known not detailed |
| `category.work.d80.general/v05` | 自信を確かめる | literal confidence predicate |
| `category.work.d90.general/v05` | 反対の質問 | unnatural collocation |
| `overall.d00.general/v10` | 場に合わせて大きなことを言わないでください | vague literal wording for making exaggerated promises |
| `overall.d00.general/v11` | 今必要なことを、ほかと分けて書きましょう | unspecified comparison; write down own needs directly |
| `overall.d10.general/v07` | 終えられる約束 | unnatural collocation |
| `overall.d10.general/v13` | ひとりで解決したことに | tense/aspect unnatural in advice |
| `overall.d10.general/v18` | 次の用事を窮屈に | unnatural predicate-object |
| `overall.d20.general/v17` | 場に合わせた選択を、惜しく | regret mistranslated |
| `overall.d20.general/v19` | 耳ざわりのよい答え | wrong collocation |
| `overall.d20.general/v20` | 大事な部分だけ残して次に回す | wrong part postponed |
| `overall.d30.general/v10` | 自分の選択が惜しく | regret mistranslated |
| `overall.d30.general/v15` | 大切な時間を過ぎない | unnatural predicate |
| `overall.d40.general/v11` | 一つ拾えば / また動き始める時間 | abstract literal advice |
| `overall.d40.general/v12` | 守れる言葉 | unnatural collocation |
| `overall.d50.general/v05` | 全員の満足を背負う | abstract literal collocation |
| `overall.d50.general/v20` | よくしているのに満足が少ない | unnatural collocation |
| `overall.d60.general/v12` | 少し読みたかった / 聴きたかったものを手に取る | modifier scope/verb compatibility |
| `overall.d60.general/v10` | 選択を一つ決める | redundant selection predicate; author follow-up nomination |
| `overall.d70.general/v12` | 取り入れたい機会 | unnatural collocation |
| `overall.d80.general/v06` | 選択を決める | redundant nominalization |
| `overall.d80.general/v13` | 悩みに使える | unnatural collocation |
| `overall.d80.general/v16` | 満足がはっきり伝わってくる | self feeling not message transmission |
| `overall.d90.general/v01` | 合う楽しさや方法 | unnatural collocation |
| `overall.d90.general/v02` | 選択を決める | redundant nominalization |
| `overall.d90.general/v04` | 場所を体験 | incompatible verb |
| `overall.d90.general/v16` | よく選んだという満足 | awkward reading of well chosen |
| `overall.d90.general/v17` | よい考えを機会に | predicate-object mismatch |
| `category.love.d00.general/v04` | 今日は人と比べる言葉が、いつも以上に傷つけてしまいやすい日です。 | 傷つけてしまいやすい is cumbersome and lacks a clear affected person. |
| `category.love.d10.general/v02` | よい関係を思ってしたお願いも、今日は相手に負担と感じられそうです。 | よい関係を思ってした is unnatural and changes good intentions into an invented relationship motive. |
| `category.love.d10.general/v04` | 今日の心残りで、普段の優しさまでなかったことにしないようにしましょう。 | 心残り implies unfinished regrets rather than feeling disappointed by consideration; で is awkward causal linkage. |
| `category.love.d10.general/v09` | 一人で解釈する時間を減らすと、話す負担も軽くなりそうです。 | Literal interpretation wording is less natural than specifying guessing another person's meaning. |
| `category.love.d10.general/v18` | 今日の落胆を、相手の態度すべてに結びつけないでください。 | Abstract disappointment connected to all behavior sounds translated. |
| `category.love.d20.general/v02` | よいと思う気遣いがお互いに違い、今日は心残りが生まれやすい日です。 | 心残り means lingering regrets and is unnatural for unmet expectations of consideration. |
| `category.love.d20.general/v07` | 希望は初めから伝えると、後の心残りを減らせます。 | Same semantic issue: dissatisfaction rather than unfinished regret. |
| `category.love.d20.general/v16` | 相手が考えているなら、待つ時間をあげましょう。 | 待つ時間 makes the other person wait, while intended advice is giving them time to think. |
| `category.love.d30.general/v17` | 相手に考える時間を渡すことも、会話の一部です。 | 時間を渡す is translated collocation. |
| `category.love.d40.general/v01` | 先に話しかけるまで、今日は少しためらうかもしれません。 | 先に...まで gives awkward until phrasing instead of hesitating to initiate. |
| `category.love.d40.general/v16` | 一度でぴったり合わなくても、お互いに知れたことが役立ちます。 | お互いに知れたこと is missing what is learned. |
| `category.love.d50.general/v11` | 話し上手であることより、今日は気楽な態度が似合う日です。 | Attitude suiting a day is literal and awkward; preserve relaxed demeanor over eloquence. |
| `category.love.d50.general/v16` | 気持ちを飾らずに伝えるには、今日は無理のない日です。 | 無理のない日 is an unnatural day predicate. |
| `category.love.d60.general/v04` | 次にどうするか決めれば、心残りを長く引きずらずに済みそうです。 | Unmet expectations, not unresolved regrets. |
| `category.love.d60.general/v19` | 手の込んだ言葉より、今日はいつも変わらない態度がよく伝わります。 | An attitude being well communicated is literal; consistent behavior communicates care. |
| `category.love.d70.general/v04` | 親しい人との不満を解くには、今日はよい日です。 | 不満を解く is not a natural collocation. |
| `category.love.d70.general/v17` | 返事を急かさず聞く時間が、関係によい働きをしてくれます。 | Abstract relationship mechanism phrasing instead of plain fortune language. |
| `category.love.d70.general/v18` | 気軽に楽しんだ話や経験を紹介しましょう。 | 気軽に楽しんだ話 is a literal and unclear modifier. |
| `category.love.d70.general/v18` | 大げさな冗談より、お互いに反応する話題を続ければ大丈夫です。 | お互いに反応する modifies topic awkwardly. |
| `category.love.d80.general/v07` | 過去の正しさをすべて問い直すより、また気楽に過ごす方法を探してください。 | Past correctness is an unnatural substitute for who was right or wrong. |
| `category.love.d80.general/v17` | ただし、相手につらかった思い出を冗談にして持ち出さないでください。 | 相手に requires とって for a memory painful to someone. |
| `category.love.d90.general/v05` | 長く残った不満を解いて、今日はまた気楽に過ごす機会がありそうです。 | 不満を解く unnatural collocation. |
| `category.love.d90.general/v06` | よく見せるために、すべて相づちを合わせる必要はありません。 | 相づちを合わせる is a malformed collocation. |
| `category.love.d90.general/v08` | 初めての出会いでも、今日はうれしい親しさを感じられるかもしれません。 | うれしい親しさ is literal; welcoming familiarity should be natural. |
| `category.love.d90.general/v09` | してみたかった会い方や活動があれば、提案しましょう。 | 会い方 is unnatural in this context, describing how to meet. |
| `category.love.d90.general/v10` | 真剣に受け止めてもらえる反応を、期待できそうです。 | Reaction modified by being taken seriously is unnecessarily indirect. |
| `category.love.d90.general/v12` | 間が空いた心残りより、これから話せることに目を向けてください。 | 間が空いた心残り is not natural Japanese. |

## 결과 조합 읽기 검수

# EN / JA complete-fortune combination reading review

These are editorial test scores, not generated user predictions. Each set uses the same variant ID in both languages. This is an additional integration reading review, including some copy written by the reviewer, not a new independent review of the entire catalog. No product copy is modified.

## A — EN — v03

overall: 95, love: 25, money: 82, work: 58, energy: 14

### overall — 95 — overall.d90.general/v03

An idea you suggest could lead to a good opportunity today.

Stating a thought specifically may reveal how to put it into practice. Explain what you want and when you're available. Someone else's ideas could improve the plan beyond your first version. Follow up on the interesting parts and decide what comes next. Take the initiative instead of waiting for an invitation.

Do: Make a specific suggestion about what you'd like to do.
Pause: Don't wait until every part of the plan is perfect.

### love — 25 — category.love.d20.general/v03

A casual conversation could become more serious than expected today. Stop treating it lightly if an important concern comes up. Hear why the person was upset before defending yourself. Your own explanation can wait until you've listened. Understanding what each of you meant matters more than finishing quickly.

### money — 82 — category.money.d80.general/v03

This is a good day to resolve a money matter you've been putting off. If you're owed expenses or need to check a shared bill, send the details first. A clear explanation can reduce unnecessary back-and-forth. Look for any refund or reimbursement items you've missed, too. Ask clearly for what you're owed instead of simply waiting today.

### work — 58 — category.work.d50.general/v03

You should be able to divide solo and shared work reasonably today. Handle what you can finish alone first, and ask only where another opinion is needed. Not every step requires a joint decision. Briefly organize anything that needs handing over to someone else. Clear roles should let you continue without much pressure today.

### energy — 14 — category.energy.d10.general/v03

Spending a long time with a group could tire you out easily today. If you have plans, you don't have to stay until everyone else leaves. Taking a quiet break or heading home early are both fine options. Once you're home, try some quiet time instead of keeping the conversation going by phone. You needn't avoid company, but a shorter visit may suit you better.

## A — JA — v03

overall: 95, love: 25, money: 82, work: 58, energy: 14

### overall — 95 — overall.d90.general/v03

自分から出した提案に、よい機会を見つけられそうです。

考えているだけだったことも、具体的に話すと実行する方法が見えやすい日です。希望する内容と、使える時間をはっきり伝えましょう。人のアイデアが加われば、初めよりよい計画になるかもしれません。気になる部分はすぐに調べ、次の手順を決めれば大丈夫です。待つより、こちらから提案することを優先しましょう。

Do: してみたいことを、具体的に提案しましょう。
Pause: 完璧な計画ができるまで、待たないでください。

### love — 25 — category.love.d20.general/v03

軽く始めた会話が、今日は思ったより真剣な話になりそうです。冗談では済まない内容になったら、まず笑うのをやめましょう。相手がつらかった理由を聞く前に、弁解するのは避けてください。自分の考えは、十分に聞いてから伝えても遅くありません。今日は早く終えるより、お互いにどんな意味だったか確かめるほうがよさそうです。

### money — 82 — category.money.d80.general/v03

後回しのお金の問題を、整理するのによい日です。受け取る予定のお金や、確認が必要な精算があれば、先に内訳を送りましょう。説明が明確なら、余計なやり取りを減らせます。抜けていた払い戻しや請求項目も、一緒に見る価値がありそうです。待つより、受け取る分を正確に伝えて請求しましょう。

### work — 58 — category.work.d50.general/v03

一人ですることと共同の作業を、ほどよく分けられそうです。一人で終えられる部分を先に済ませ、意見の必要なところだけ尋ねましょう。すべての過程を一緒に決める必要はありません。ただし、引き継ぐ内容は簡単にまとめるのがおすすめです。役割が明確なほど、気負わずに進められます。

### energy — 14 — category.energy.d10.general/v03

大勢の人と長く過ごすと、今日は疲れやすくなりそうです。約束があっても、最後まで一緒にいなければと考える必要はありません。途中で一人になって休んだり、先に帰ったりしても大丈夫です。帰宅後は連絡を続けるより、静かに過ごしてみてください。人付き合いを避ける必要はありませんが、会う時間は短めがよさそうです。

## B — EN — v11

overall: 15, love: 94, money: 55, work: 85, energy: 72

### overall — 15 — overall.d10.general/v11

A small flaw could make you want to start over today.

The part you dislike may seem larger than everything else. Changing what's already fine could simply create more to do. Identify one specific problem first. Someone familiar with the situation can help if you can't separate it out. Fixing just what's necessary may save you from starting again.

Do: Identify only the parts that need changing.
Pause: Don't overturn everything because of one disappointment.

### love — 94 — category.love.d90.general/v11

A question about the relationship could be worth discussing directly today. Start calmly with your feelings if you've been hesitating and guessing alone. The explanation may make it easier to consider where to go from here. Turn a shared wish into a specific plan if one emerges. Be honest while giving the other person time to answer freely.

### money — 55 — category.money.d50.general/v11

A calm price comparison can lead to a reasonable choice today. Include delivery or usage fees even for similar products. If the difference is small, convenient use when you need it matters too. A tiny discount doesn't deserve unlimited time. Consider both cost and convenience.

### work — 85 — category.work.d80.general/v11

Another refinement could make the strengths of finished work more visible today. Change the order or explanation if an important point is buried. Even a simple example can make it easier to understand. Showing it to someone may bring an opportunity for your preparation to be recognized. Don't try to accommodate every small difference in taste.

### energy — 72 — category.energy.d70.general/v11

A balance of activity and rest could make for a satisfying day. Start by giving time to an exercise or hobby you want to do. After a short rest, you may feel ready to try something else. That doesn't mean every free slot needs another plan. Give the activity you enjoy most enough time today.

## B — JA — v11

overall: 15, love: 94, money: 55, work: 85, energy: 72

### overall — 15 — overall.d10.general/v11

少しずれただけで、全部やり直したくなりそうです。

気に入らない部分が、いつもより大きく見える日です。まだ問題のないところまで変えると、することが増えてしまいそうです。まず何が困るのかを一つに絞りましょう。ひとりで分けにくければ、状況を知る人の意見を聞いても大丈夫です。必要な部分だけ直せば、最初から始め直さなくて済みます。

Do: 変える必要のあるところだけ選びましょう。
Pause: 一つ気に入らないからと、すべて覆さないでください。

### love — 94 — category.love.d90.general/v11

関係について気になっていたことを、今日は直接話しやすい日です。一人で想像して迷っていたら、落ち着いて自分の気持ちから伝えましょう。説明を聞けば、これからどう付き合うか考えやすくなりそうです。同じ希望が見つかったら、具体的な約束へ進めても構いません。素直に話しながら、相手も気楽に答えられる時間を残してください。

### money — 55 — category.money.d50.general/v11

落ち着いて価格を比べると、無理なく選べる日です。似た商品でも、送料や利用料を含む金額を見ましょう。大きな差がなければ、必要なときに気楽に使えるほうがおすすめです。小さな割引のために、多くの時間をかける必要はありません。金額と便利さを、一緒に考えましょう。

### work — 85 — category.work.d80.general/v11

仕上げたものを整え直すと、長所がさらに伝わりやすい日です。大切な部分が埋もれていたら、順番や説明を変えましょう。簡単な例を入れるだけでも、理解してもらいやすくなります。人に見せると、準備の努力を認めてもらう機会がありそうです。小さな好みの違いまで、全部直そうとしないでください。

### energy — 72 — category.energy.d70.general/v11

動く時間と休む時間をほどよく分けると、充実した一日になりそうです。まず、やりたい運動や趣味に時間を使ってみましょう。少し休めば、別のことを楽しむ余裕も出てくるかもしれません。だからといって、空き時間のたびに予定を足す必要はありません。今日は特に楽しかった活動に、十分な時間を取りましょう。

## C — EN — v18

overall: 55, love: 65, money: 15, work: 75, energy: 95

### overall — 55 — overall.d50.general/v18

A little give and take should be enough today.

Small differences in taste can be worked out without much discomfort. Suggest what you'd like. Listen to what matters to the other person while explaining your own needs. Compromising once doesn't require you to agree to everything afterward. Finding what works for this occasion is enough.

Do: Ask what matters to each of you when deciding together.
Pause: Don't turn a small compromise into a lasting obligation.

### love — 65 — category.love.d60.general/v18

Sharing everyday news with someone close could lift your mood today. Tell them one thing you liked even if nothing special happened. Listen to their small pleasures too. If you usually talk only about problems, you may find something easier to chat about today. End even a short exchange on a pleasant note.

### money — 15 — category.money.d10.general/v18

Different refund or return terms could disappoint you today. Compare the current terms with what you were originally told. Have receipts or request records ready to make the conversation easier. Identify the difference instead of arguing from frustration. Following the available steps in order can help you find a resolution.

### work — 75 — category.work.d70.general/v18

You could find a solution together even with someone who disagrees today. Hearing the reasons behind an opposing view should reveal something useful. State your own thinking clearly with supporting reasons too. If you don't need one single method, you can use the suitable parts of each approach separately. A discussion of your differences can help resolve the problem.

### energy — 95 — category.energy.d90.general/v18

Taking the initiative could bring more chances to find enjoyable activities today. Check what's happening at a place or group you've been interested in. Joining in may offer a different kind of pleasure from watching. If you find something you like, learn a little more. There's no need to commit to doing it for a long time from the start.

## C — JA — v18

overall: 55, love: 65, money: 15, work: 75, energy: 95

### overall — 55 — overall.d50.general/v18

ほどよく譲り合い、無理なく過ごせる日です。

小さな好みの違いがあっても、大きな不満なく調整できそうです。希望があれば、気軽に提案しましょう。相手が大切にしたい点を聞き、こちらに必要なことも伝えれば大丈夫です。一度譲っても、次からすべて合わせる必要はありません。その都度、気楽にできる方法を見つければ十分です。

Do: 一緒に決めるときは、お互いに大切な点を尋ねましょう。
Pause: 小さな譲歩を、これからの義務にしないでください。

### love — 65 — category.love.d60.general/v18

親しい人と日常を話すことで、今日は気分がよくなりそうです。特別な出来事がなくても、今日よかったことを一つ伝えましょう。相手の小さな楽しみにも、耳を傾ければ十分です。解決すべき問題ばかり話していた間柄でも、気楽な話題ができるかもしれません。今日は短い連絡でも、楽しい気持ちで終えましょう。

### money — 15 — category.money.d10.general/v18

返金や返却の条件が予想と違い、がっかりするかもしれません。初めの案内と今の条件を、一緒に確かめましょう。必要なレシートや申込記録を用意すると、話をしやすくなります。感情的に責めるより、どの項目が違うかを示すのがおすすめです。できる手続きから順に進めれば、整理する方法が見つかりそうです。

### work — 75 — category.work.d70.general/v18

意見の違う人とも、解決策を一緒に探すのによい日です。反対の意見でも、理由を聞くと参考にできる点を見つけやすくなります。こちらの考えも、根拠を添えて明確に話しましょう。一つの方法にまとめなくてよければ、それぞれ合う部分を分けて使うのもおすすめです。違いを確かめる会話が、今日は解決に役立ちます。

### energy — 95 — category.energy.d90.general/v18

今日は自分から動くほど、楽しい活動に出会う機会がありそうです。気になっていた場所や集まりの予定を、調べてみましょう。参加すれば、眺めるときとは違う楽しさを感じられそうです。気に入った活動があれば、もう少し学んでもよいでしょう。最初から長く続けると、約束する必要はありません。


## Reading judgments

All six complete sets were read as continuous results, including headlines, all five-sentence paragraphs, and the overall action/caution pair. This is a contextual integration check, not an additional independent catalog certification.

- A EN: PASS. Overall initiative does not promise romance or physical stamina; cautious love and low energy remain distinct. The money section asks about an amount owed, while the overall section suggests an optional plan. Those are related conversational actions but different recommendations.
- A JA: PASS after the wording correction below and a full-paragraph reread. The high overall result does not erase the caution in love or the need for a shorter social visit in energy.
- B EN: PASS. The low overall result is specifically sensitivity to an imperfection, while work predicts better refinement and love favors a direct conversation. These can coexist without asserting that all areas are poor. Ordinary money and favorable energy retain their own intensity.
- B JA: PASS. Meaning and polarity match the English/Korean interpretation. Both overall and work mention revisions, but one warns against unnecessary restarting and the other recommends exposing strengths; they are compatible rather than contradictory.
- C EN: PASS. Moderate overall compromise and favorable work discussion share a broad negotiation theme but apply to different choices and task methods. Low money describes refund/return disappointment while high energy invites activity; neither reverses the other.
- C JA: PASS. The same distinctions hold. No added assumption of employment, partnership, financial gain, or health diagnosis is present.

### Wording finding

- Locale/route/variant: JA `category.money.d80.general/v03`, set A.
- Quote: `受け取る費用や確認する精算があれば、先に内訳を送りましょう。`
- Reason: `受け取る費用` and `確認する精算` read as compressed literal noun phrases. The Korean means money due to receive or a settlement requiring confirmation.
- Suggested sentence: `受け取る予定のお金や、確認が必要な精算があれば、先に内訳を送りましょう。`
- Severity: wording revision; no score/meaning reversal. Product copy not edited by this reviewer. Author applied the suggestion. The corrected entire five-sentence paragraph was reread and passed; the six complete sets above were regenerated from current drafts. Finding resolved.

No cross-category blanket verdict, contradictory overall-category polarity, or locale meaning reversal was found in these six sets. Some related advice occurs naturally across fields; none of the tested sets repeats an identical recommendation or adds it merely as filler.

Final contextual reading verdict: PASS — all six assembled results.
