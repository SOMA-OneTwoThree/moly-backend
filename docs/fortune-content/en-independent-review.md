# English independent-score reading review

2026-09-09 implementation draft. Asset: `app/resources/fortune/copy.v2.en.json`, version `fortune-copy.v3-independent.1`.

## Scope and meaning agreement

The Korean, Japanese, and English owners agreed on the same ID-level meaning selection before expanding the files. Each category retains its primary variant’s judgment and action, adds the same-band variant six positions ahead’s judgment and action, and finishes with the action twelve positions ahead. Positions wrap within the existing twenty variants. Overall retains its three primary reading observations and adds the six-ahead action and twelve-ahead caution. This broadens each paragraph without letting an overall score determine category advice.

Existing IDs and every band’s twenty variants remain. Category wire arrays retain two blocks (two sentences plus three); overall retains three blocks (one plus two plus two). Normal periods and spaces allow the client to render one paragraph.

The source themes are retained rather than replacing them with unrelated scenarios. English edits clarify ambiguous pronouns when an action appears without its original opening, replace literal or inflated expressions, and make higher-band suggestions less hesitant without guaranteeing money, affection, or health outcomes. Color names were reviewed and remain ordinary labels, including Gray for the compatibility key `coral`.

## Verification performed

- 200 overall and 800 category paragraphs, each with five sentences.
- Twenty variants in every existing band; all IDs and JSON structure preserved.
- No duplicate complete paragraphs, embedded newlines, or explicit guaranteed-outcome phrases in the automated screening.
- Overall paragraphs contain 45–69 English words; categories 39–71. Device line count is deliberately not promised.
- Targeted editorial review covered low, middle, and high readings in all four categories and opposite-score overall/category combinations. It also checked flagged stock constructions, missing noun antecedents, and broad all-day claims.
- Full automated report: ignored local `reports/fortune-redesign/en-independent-qa.json`.

## Limits requiring honest handoff

This is a meaning-preserving expansion with curated source observations, not 5,000 independently authored sentences. Category judgments occur in two paragraphs and category actions in three under the shared donor arrangement: 1,600 sentence types repeat, at most three times each; full paragraphs do not repeat. Overall flow sentence duplication is zero. Repetition of observations across nearby reading days should be evaluated alongside the selector; unique paragraph IDs alone do not establish editorial variety.

Automated counts and targeted editorial checks do not establish human/native-speaker approval or an AI-detector score. A complete independent line-by-line assessment of all 1,000 English units has not been performed. The five editorial dimensions therefore are not represented as having received independent passing scores. UI strings outside the owned English fortune resource were not changed by this task.

## Follow-up target-language pass

A further English-only pass addressed the three integration-sample findings: awkward references to “another taste,” retaining learning through “reading or speaking,” and an “experience or convenience” one would “use.” The same corrections were applied at every reused location. This pass also reviewed flagged constructions across all four axes and sixteen additional complete low/middle/high paragraphs, replacing literal noun phrases, unclear action references, and stiff expressions with ordinary English. More than eighty source expressions were edited across their reuse locations. The category and overall five-sentence counts and complete-paragraph uniqueness were checked again after the main copyedit pass. This remains targeted editorial review, not a claim of independent native-speaker review of every paragraph.

The Korean owner’s `overall.d10.v13` correction was also mirrored: its meaning is now schedule adjustment and necessary checks, not a prediction about physical energy. The associated action/caution donor appearances in `v07` and `v01` were updated with it.
