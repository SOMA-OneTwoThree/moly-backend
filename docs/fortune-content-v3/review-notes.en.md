> 이번 재설계의 통합 정본: `app/resources/fortune/copy.v2.en.json`
> 실행 자산에서 생성한 전문과 최종 테스트 결과는 [기준 문서](../DAILY-FORTUNE.md)에 정리

# English overall fortune rewrite review

## Editorial standard

- Describe the feel of a whole day before suggesting an attitude toward it
- Keep each entry recognizable to a reader without assuming a job, partner, project, or specific event
- Match the Korean entry's outlook, situation, and advice at the same score and variant ID
- Use idiomatic contemporary US English for a daily fortune, without literal Korean syntax
- Replace abstract coaching language and workflow instructions with everyday experiences and emotional relevance
- Different score bands must change the day's interpretation, not merely add stronger adjectives
- Never guarantee success, threaten misfortune, invent facts about the reader, or prescribe financial or medical decisions
- End no field with a period; keep headlines concise, body lines complete, and action/caution labels short
- Check the 20 entries in each band for distinct situations and interpretations, not adjective substitutions

## Prior copy problems

The old entries were organized around task phases such as starting, coordinating, and finishing. Many entries required a reader to imagine a specific project, document, or unconfirmed requirement. Headlines described instructions rather than a day's fortune. Repeated modal phrasing made the reading feel tentative and procedural. This rewrite starts from the approved day-level Korean concepts rather than editing those old sentences.

## Completion

Complete: all 200 readings independently localized and self-reviewed; final results below

## Final authoring and review result

- Completed `en/d00.json` through `en/d90.json`: 10 score bands × 20 complete readings = 200 readings / 1,200 fields
- Each reading contains one headline, three interpretation lines, one action, and one caution
- Wrote from the newly authored Korean source for the same score/variant ID; did not reuse the previous eight-flow catalog
- Compared the meaning of each Korean bundle while localizing: overall outlook, recognizable situation, and suggested attitude remain aligned
- Synchronized the Korean d70 v03/v13 headline revisions and reviewed the later four Korean wording refinements; their meaning was already reflected in English
- Lowest bands allow delays, sensitivity, and self-protection without predicting disaster; middle bands emphasize everyday satisfaction and choice; upper bands allow more favorable response and opportunity without promising an event or outcome
- Removed literal metaphors that sounded unnatural in English during the final pass, including a choice having confidence settle around it and a wish itself gaining confidence
- Corrected d20 v19 action to `agreeing to a favor`, avoiding the opposite meaning of receiving a favor
- Final `python reports/fortune-reset/en/check.py`: 200 readings / 1,200 fields, 0 shape/punctuation/global exact duplicate/same-band same-field near-duplicate findings
- Near-duplicate check: SequenceMatcher ≥ 0.72, field length ≥ 30, interchangeable fields within each 20-reading band
- These are agent editorial checks, not a claim of external human native-speaker approval or device QA
- English source frozen after final checks; root informed before runtime assembly

## Independent server review

Reviewed `app/services/fortune_copy_selection.py`, `app/services/fortune_catalog.py`, relevant selector/catalog test changes, and the main document's selection and deployment contracts without changing server code

No correctness defect found in the reviewed routing/state changes

Verified by code inspection:

- The original 80 semantic routes remain validated against score and flow, then collapse into ten score-only editorial routes
- Changing only the calculation flow within a score band uses the same overall cursor
- All current cursor routes belong to a closed set of 10 overall + 40 category routes, bounding persisted state at 50
- v1 state is fully shape/date/position/selection-identity checked before discarded overall routes are removed
- Categories retain both the v1 permutation and their previous cursor; advancing applies only on a later local day
- Same/older local dates preserve position and the stored date high-water mark
- Malformed/unknown states remain errors; they are not silently reset
- Current state is deep-copied, so migration does not mutate the prior snapshot in place
- Existing valid snapshots are not rewritten simply because the copy version changed

Review notes sent to root:

1. A v1 writer rejects v2 state on subsequent regeneration, so a simple old-image rollback or mixed writers is unsafe at this version boundary. Root's section 7 now documents this and favors a v2-compatible fixed image after disabling the feature if needed
2. The API response example still contained the old copy version and old punctuated body at review time. Requested replacement with a current response or explicit legacy labeling before final documentation completion

The root executes final combined tests and the development-DB verification; this review does not claim an independent new test execution

## Final independent editorial follow-up

Root flagged two unnatural expressions in d90. Replaced v09 flow[1] with `You don't need to put on an act to be interesting`, preserving the appeal of the reader's ordinary self. Replaced v20 headline with `Look forward to what today might bring`, preserving a hopeful daily-fortune outlook without the awkward phrase `happy possibility`. Re-ran all English checks with zero findings and froze the final files again before assembly.


통합 완료: 새 원고를 서버 자산에 반영하고 전체 서버 2,171개·서비스/개발 DB 6개 테스트를 통과했다. 최신 API 예시와 배포 제한은 [기준 문서](../DAILY-FORTUNE.md)에 반영했다.
