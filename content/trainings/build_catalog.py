"""Build the reviewed course manuscripts into the runtime catalog.

Run from the backend root: uv run python content/trainings/build_catalog.py [--check]
No network or LLM is used by this build. Answer keys/rubrics remain server-only.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGET = ROOT.parents[1] / 'app/resources/trainings/catalog.json'
COURSES = ('positive.json', 'acceptance.json', 'refusal.json')
trainings = [json.loads((ROOT / name).read_text()) for name in COURSES]
# Existing progress remains readable by its old id; the Explore list shows only the new courses.
for legacy in json.loads((ROOT / 'legacy-catalog.json').read_text())['trainings']:
    legacy['archived'] = True
    trainings.append(legacy)
raw = json.dumps({'schema_version': 1, 'trainings': trainings}, ensure_ascii=False, indent=2) + '\n'
if '--check' in sys.argv:
    assert TARGET.read_text() == raw, 'catalog differs from its reviewed sources'
else:
    TARGET.write_text(raw)
print('Training catalog: three active courses, 90 stages; legacy history preserved.')
