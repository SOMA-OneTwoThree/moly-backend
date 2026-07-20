from types import SimpleNamespace

import pytest

from scripts import evaluate_memory_recall as evaluator


class _FakeEmbeddings:
    def __init__(self, vector_for):
        self.vector_for = vector_for
        self.calls = []

    def create(self, *, model, input):
        self.calls.append({"model": model, "input": input})
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=index, embedding=self.vector_for(text))
                for index, text in enumerate(input)
            ]
        )


def _benchmark_fixture():
    target = evaluator.MemoryRecord("target", "유저는 비 오는 날 수제비를 먹는다.", "2026-01-01")
    distractors = tuple(
        evaluator.MemoryRecord(
            f"d{index:02d}",
            f"유저는 오늘 사무실 화분 {index}번에 물을 줬다.",
            f"2026-02-{index:02d}",
        )
        for index in range(1, 21)
    )
    cases = (
        evaluator.RecallCase(
            "match",
            "older_than_20",
            "비 오는 날 내가 찾는 음식이 뭐였지?",
            ("target",),
        ),
        evaluator.RecallCase(
            "none",
            "no_match",
            "내 여권 번호를 기억해?",
            (),
        ),
    )
    return evaluator.Fixture((target, *distractors), cases)


def test_committed_fixture_has_korean_category_coverage():
    fixture = evaluator.load_fixture()

    assert len(fixture.cases) >= 40
    assert evaluator.REQUIRED_CATEGORIES == {case.category for case in fixture.cases}
    assert sum(not case.expected_memory_ids for case in fixture.cases) == 20


def test_run_evaluation_batches_embeddings_and_compares_legacy_latest20():
    fixture = _benchmark_fixture()

    def vector_for(text):
        if text in {fixture.memories[0].text, fixture.cases[0].query}:
            return [1.0, 0.0]
        if text == fixture.cases[1].query:
            return [-1.0, 0.0]
        return [0.0, 1.0]

    embeddings = _FakeEmbeddings(vector_for)
    client = SimpleNamespace(embeddings=embeddings)

    metrics = evaluator.run_evaluation(
        client,
        fixture,
        model="text-embedding-3-small",
        threshold=0.5,
    )

    assert len(embeddings.calls) == 1
    assert embeddings.calls[0]["model"] == "text-embedding-3-small"
    assert metrics.semantic_recall_at_4 == 1.0
    assert metrics.legacy_recall_at_20 == 0.0
    assert metrics.improvement == 1.0
    assert metrics.no_match_false_selection_rate == 0.0


def test_compute_metrics_counts_multi_memory_recall_and_no_match_false_selection():
    cases = (
        evaluator.RecallCase("multi", "multi_session", "두 사실을 찾아줘.", ("a", "b")),
        evaluator.RecallCase("none", "no_match", "없는 사실을 찾아줘.", ()),
    )

    metrics = evaluator.compute_metrics(
        cases,
        {"multi": ("a",), "none": ("unrelated",)},
        ("a", "b"),
    )

    assert metrics.semantic_recall_at_4 == 0.5
    assert metrics.legacy_recall_at_20 == 1.0
    assert metrics.improvement == -0.5
    assert metrics.no_match_false_selection_rate == 1.0


@pytest.mark.parametrize(
    ("metrics", "expected"),
    [
        (evaluator.Metrics(0.90, 0.60, 0.30, 0.05, 40, 20), 0),
        (evaluator.Metrics(0.89, 0.00, 0.89, 0.00, 40, 20), 1),
        (evaluator.Metrics(1.00, 0.71, 0.29, 0.00, 40, 20), 1),
        (evaluator.Metrics(1.00, 0.00, 1.00, 0.06, 40, 20), 1),
    ],
)
def test_exit_code_enforces_acceptance_thresholds(metrics, expected):
    assert evaluator.exit_code_for(metrics) == expected


def test_main_exits_clearly_without_openai_key(monkeypatch, capsys):
    monkeypatch.setattr(evaluator.settings, "openai_api_key", "")

    assert evaluator.main([]) == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().err


def test_main_returns_nonzero_when_mocked_embeddings_miss_gate(monkeypatch, capsys):
    fixture = _benchmark_fixture()

    def vector_for(text):
        if text in {fixture.memories[0].text, *(case.query for case in fixture.cases)}:
            return [1.0, 0.0]
        return [0.0, 1.0]

    client = SimpleNamespace(embeddings=_FakeEmbeddings(vector_for))
    monkeypatch.setattr(evaluator.settings, "openai_api_key", "test-key")
    monkeypatch.setattr(evaluator, "load_fixture", lambda _path: fixture)

    assert evaluator.main([], client_factory=lambda **_kwargs: client) == 1
    assert "FAIL: no-match false selection" in capsys.readouterr().out
