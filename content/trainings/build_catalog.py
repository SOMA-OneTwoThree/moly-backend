"""Build the approved bilingual translations and stage arrangement into the runtime catalog.

Run from the backend root: uv run python content/trainings/build_catalog.py [--check]
No network or LLM is used by this build. Answer keys/rubrics remain server-only.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGET = ROOT.parents[1] / 'app/resources/trainings/catalog.json'
LANGS = ('ko', 'en', 'ja')
source = json.loads((ROOT / 'ko-source.json').read_text())
translations = {}
for line in (ROOT / 'translations.tsv').read_text().splitlines():
    idx, en, ja = line.split('\t')
    assert int(idx) not in translations
    translations[int(idx)] = (en, ja)
assert set(translations) == set(range(len(source)))
copy = {ko: dict(zip(LANGS, (ko.replace('\n', ' '), *translations[i])))
        for i, ko in enumerate(source)}
rubrics = {}
for line in (ROOT / 'rubrics.tsv').read_text().splitlines():
    topic, number, criteria, ko, en, ja = line.split('\t')
    rubrics[topic, int(number)] = {
        'criteria': ('Positive reframing. ' if topic == 'positive' else 'Self-acceptance. ') + criteria,
        'example': dict(zip(LANGS, (ko, en, ja))),
    }

def loc(ko, en, ja):
    return dict(zip(LANGS, (ko, en, ja)))

metadata = {
    'positive': ('positive-circuit-v1', loc('긍정 회로', 'Positive Perspective', '前向きな視点'),
                 loc('아쉬운 상황에서도 좋은 면과 새로운 가능성을 찾아보는 30단계 연습이에요.',
                     'Thirty steps to notice good aspects and new possibilities, even when things disappoint you.',
                     '残念な出来事の中でも、よい面や新しい可能性を見つける三十のステップです。')),
    'acceptance': ('self-acceptance-v1', loc('자기수용', 'Self-Acceptance', '自己受容'),
                   loc('실수와 나의 가치를 구분하고, 나를 과하게 탓하지 않는 생각을 연습해요.',
                       'Practice separating mistakes from your worth and responding without excessive self-blame.',
                       'ミスと自分の価値を分け、自分を責めすぎない考え方を練習します。')),
}
choose_hint = loc('이 상황에서 미션에 맞는 생각을 다시 골라볼까요?',
                  'Which thought fits the mission in this situation?',
                  'この状況でミッションに合う考えを、もう一度選んでみましょう。')
erase_hint = loc('실수나 수습 행동보다, 나 자체를 깎아내리는 문장을 찾아보세요.',
                 'Look for a sentence attacking your worth, rather than acknowledging or fixing a mistake.',
                 'ミスや対応の文ではなく、自分の価値そのものをけなす文を探しましょう。')
arrangement = json.loads((ROOT / 'arrangement.json').read_text())['courses']
trainings = []
for order, (topic, rows) in enumerate(arrangement.items()):
    tid, title, summary = metadata[topic]
    stages = []
    for row in rows:
        s = row['stage']
        number = s['stage']
        write = s['stage_type'] == 'WRITE'
        diary = s['ui_type'] == 'DIARY'
        erase = any(e['data']['input_type'] == 'ERASE' for e in s['scenario'])
        lines = []
        for entry in s['scenario']:
            data = entry['data']
            slot = data['input_type'] in ('FILL', 'ERASE')
            # Erasable text is rendered from the options at its slot, never duplicated.
            lines.append({'speaker': 'me' if slot and not diary else 'narration',
                          'slot': slot,
                          'text': copy[data['text']] if data['text'] and not erase else None})
            if not slot and data['text']:
                lines[-1]['text'] = copy[data['text']]
        reward = {5: 15, 15: 40, 30: 100}.get(number, 0)
        stage = {'icon_key': s['icon_key'], 'number': number, 'answer_type': 'write' if write else 'pick',
                 'layout': 'diary' if diary else 'chat', 'pick_style': None if write else ('erase' if erase else 'choose'),
                 'review_point': bool(reward), 'reward_hay': reward,
                 'mission': copy[s['mission']], 'lines': lines, 'options': []}
        if write:
            stage['grading'] = rubrics[topic, number]
        else:
            correct = row['authoring_only']['correct_option_index']
            for index, option in enumerate(s['pick_texts']):
                stage['options'].append({'id': chr(97 + index), 'text': copy[option],
                                         'correct': index == correct,
                                         'hint': None if index == correct else (erase_hint if erase else choose_hint)})
            if s['pick_comment']:
                stage['explanation'] = copy[s['pick_comment']]
            # The edited birthday choices no longer match the original numbered explanation.
            if topic == 'acceptance' and number == 14:
                stage['explanation'] = loc('실수는 인정하고 사과하되, 그 실수로 내 인격 전체를 판단하지 않아도 돼요.',
                                           'Acknowledge the mistake and apologize without judging your whole character.',
                                           'ミスは認めて謝りつつ、それだけで人格全体を決めつけなくて大丈夫です。')
        stages.append(stage)
    trainings.append({'id': tid, 'order': order, 'icon_key': 'sun' if topic == 'positive' else 'red_heart',
                      'title': title, 'summary': summary, 'stages': stages})
# Existing progress remains readable by its old id; the Explore list shows only the new courses.
for legacy in json.loads((ROOT / 'legacy-catalog.json').read_text())['trainings']:
    legacy['archived'] = True
    trainings.append(legacy)
raw = json.dumps({'schema_version': 1, 'trainings': trainings}, ensure_ascii=False, indent=2) + '\n'
if '--check' in sys.argv:
    assert TARGET.read_text() == raw, 'catalog differs from its reviewed sources'
else:
    TARGET.write_text(raw)
print('Training catalog: two active courses, 60 stages; legacy history preserved.')
