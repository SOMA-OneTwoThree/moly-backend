# Japanese independent-score fortune copy review

2026-09-10 · `fortune-copy.v3-independent.1`

## Scope and shared meaning

Japanese asset: `app/resources/fortune/copy.v2.ja.json`. All 200 overall bundles and 800 category paragraphs are updated; route IDs, 20 variants per band, color keys and hexadecimal values remain compatible.

The Korean, Japanese and English editors agreed on the same meaning map. Each category keeps its original variant's principal observation/action, adds the observation/action of the variant six places later within the same axis and band, and uses the action twelve places later as a final recommendation. Overall paragraphs retain the primary three-part reading and add the corresponding supplemental action and caution. Wraparound stays inside the same band. This is an authoring map, not runtime paragraph assembly.

Existing ideas are deliberately reused. These are not 5,000 independently invented Japanese sentences, and isolated sentences may recur in other variants. The resulting 1,000 complete paragraphs are distinct. Supplemental themes are not allowed to cross score bands or axes.

## Japanese editing

- Five sentences, Japanese `。` and `、`, no forced newline or intersentence ASCII spaces. Categories retain two text blocks (two sentences plus three); overall retains three blocks (two plus two plus one).
- Plain approachable register. Removed repeated sentence-final `よ` / `ね`; retained direct advice without turning every line into `してみて`.
- Replaced abstract or translated phrases such as `豊かな満足`, `心に余白`, `自分の心に尋ねる`, `一日を明るく彩る`, `力を添える` with concrete language.
- Expanded `かも` into complete sentences and corrected noun fragments. Kept uncertainty about actual responses/outcomes rather than guaranteeing affection, earnings or medical outcomes.
- Corrected overall statements about the whole day's luck so overall is a choice/priority reading, not an implied promise that every category improves.
- Color names reviewed: gray remains `グレー` under the compatibility key `coral`; normal Japanese names retained for the other colors.

## Verification performed

Full-corpus automated checks cover all 1,000 paragraphs:

| Check | Result |
| --- | --- |
| Overall / category counts | 200 / 800 |
| Variant and route IDs | preserved |
| Sentence count | exactly five in all paragraphs |
| Block count | overall three, category two |
| Duplicate complete paragraphs | zero |
| Duplicate sentences inside a paragraph | zero |
| Newlines / double spaces | zero |
| Known malformed endings and dangling conditional patterns | zero |
| Paragraph length | overall 113–150 chars, category 106–150 chars at the final measurement |

Readability/content review explicitly read `v01`, `v10`, and `v20` across every category route (120 complete paragraphs), and the same variants across every overall band (30 complete paragraphs). These cover all score bands and all four domains. Phrase-level corrections were then applied throughout the corpus, followed by rerunning all structural checks. This is not a claim that every final paragraph received a separate human or native-speaker review.

The most frequent exact six-character ending is `かもしれない` (111 of 1,000 overall sentences; 262 of 4,000 category sentences in the final audit). The remaining uses describe uncertain reactions/events and are not a generic suffix applied to every recommendation. Top eight-character opening occurs no more than twice in either section. Sentence-level reuse is intentionally visible in the authoring map rather than misreported as unique copy.

Generated local review evidence: `reports/fortune-redesign/ja-editorial-qa.json`. No DB writes, deployment, commits or PR creation were performed by this editorial task. Actual small-screen/large-font wrapping is a client verification concern; five sentences cannot guarantee exactly five physical lines.

## Integration follow-up: repeated advice endings

The integrated reading exposed three occurrences of `みよう` in one paragraph. A full-corpus scan found 82 category paragraphs with three `みよう` endings and 50 with three `みて` endings. All 132 were revised: the first request remains, later sentences use appropriate dictionary-form recommendations instead of repeating the same invitation ending. The changed final sentences were read through, and verb-conjugation errors from the editing pass were corrected before validation. In particular, `love.d60/v16` now asks for the other's recommendation and then accepts different preferences without repeating `聞いてみよう`.

The scan also exposed an unnatural body-condition expression in `energy.d50`; it now reads `体の調子に合わせて`. With its shared occurrences, the follow-up touched 133 paragraphs. After this pass, none of the 1,000 paragraphs contains three identical advice endings from `みよう`, `みて`, `おこう`, `といい`, or `おすすめ`. Five-sentence structure, no internal duplicate sentences, and no forced newlines were revalidated. Local before/after evidence: `reports/fortune-redesign/ja-rhythm-review.json`. The new checks supplement the initial sample review; they are not a claim of human review of the entire corpus.
