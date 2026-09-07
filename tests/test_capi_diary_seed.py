"""원고 CSV 검증·불변 append·파일 단위 rollback 계약. 외부 DB를 사용하지 않는다."""
import csv
from datetime import date
from unittest.mock import AsyncMock

import pytest

from scripts import make_capi_diary_template as template
from scripts import seed_capi_diaries as seed

WEEK = date(2026, 9, 7)


def write_csv(tmp_path, rows, fields=None):
    path = tmp_path / "diaries.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(fields or ["week_start_date", "sequence_no", "weather", "content"])
        writer.writerows(rows)
    return str(path)


def test_legacy_shape_and_blank_skip(tmp_path):
    path = write_csv(tmp_path, [["", "", ""], ["2026-09-07", "", " 원고 "]],
                     ["diary_date", "weather", "content"])
    assert seed.load_rows(path) == [("원고", "sunny", WEEK)]


def test_weekly_unicode_multiline(tmp_path):
    path = write_csv(tmp_path, [["2026-09-07", "1", "rainy", "오늘, 비가 왔다.\n기분이 좋았다."]])
    assert seed.load_rows(path) == [seed.WeeklyRow("오늘, 비가 왔다.\n기분이 좋았다.", "rainy", WEEK, 1)]


@pytest.mark.parametrize("row,match", [
    (["2026-09-08", "1", "sunny", "원고"], "월요일"),
    (["20260907", "1", "sunny", "원고"], "형식 오류"),
    (["2026-09-07", "0", "sunny", "원고"], "양의"),
    (["2026-09-07", "-1", "sunny", "원고"], "양의"),
    (["2026-09-07", "1.0", "sunny", "원고"], "양의"),
    (["2026-09-07", "2147483648", "sunny", "원고"], "양의"),
    (["2026-09-07", "1", "snowy", "원고"], "weather"),
    (["2026-09-07", "1", "sunny", " "], "빈 본문"),
    (["2026-09-07", "1", "sunny", "***"], "빈 본문"),
    (["2026-09-07", "1", "sunny", "깨짐�"], "깨진 문자"),
    (["2026-09-07", "1", "sunny", "원고", "extra"], "열 수"),
])
def test_weekly_invalid_row(tmp_path, row, match):
    with pytest.raises(SystemExit, match=match):
        seed.load_rows(write_csv(tmp_path, [row]))


def test_duplicate_slots_rejected(tmp_path):
    row = ["2026-09-07", "1", "sunny", "원고"]
    with pytest.raises(SystemExit, match="순번 중복"):
        seed.load_rows(write_csv(tmp_path, [row, row]))


@pytest.mark.parametrize("fields", [
    ["diary_date", "week_start_date", "sequence_no", "weather", "content"],
    ["week_start_date", "weather", "content"],
    ["week_start_date", "sequence_no", "content", "content"],
])
def test_mixed_or_missing_headers_rejected(tmp_path, fields):
    with pytest.raises(SystemExit, match="헤더 오류"):
        seed.load_rows(write_csv(tmp_path, [], fields))


async def test_existing_inactive_row_is_noop_and_never_reactivated():
    c = AsyncMock()
    c.fetch.return_value = [{"sequence_no": 1, "content": "원고", "weather": "sunny", "is_active": False}]
    assert await seed.insert_weekly_rows(c, [seed.WeeklyRow("원고", "sunny", WEEK, 1)]) == (0, 1)
    c.executemany.assert_not_awaited()
    assert "is_active" not in seed._WEEK_ROWS


@pytest.mark.parametrize("content,weather", [("변경", "sunny"), ("원고", "rainy")])
async def test_registered_content_and_weather_are_immutable(content, weather):
    c = AsyncMock()
    c.fetch.return_value = [{"sequence_no": 1, "content": "원고", "weather": "sunny"}]
    with pytest.raises(ValueError, match="수정 불가"):
        await seed.insert_weekly_rows(c, [seed.WeeklyRow(content, weather, WEEK, 1)])
    c.executemany.assert_not_awaited()


async def test_cannot_fill_gap_before_inactive_maximum():
    c = AsyncMock()
    c.fetch.return_value = [{"sequence_no": 3, "content": "회수", "weather": "sunny", "is_active": False}]
    with pytest.raises(ValueError, match="뒤에만"):
        await seed.insert_weekly_rows(c, [seed.WeeklyRow("추가", "sunny", WEEK, 2)])
    c.executemany.assert_not_awaited()


async def test_sorted_week_locks_and_append_with_existing_noop():
    c = AsyncMock()
    next_week = date(2026, 9, 14)
    c.fetch.side_effect = [[{"sequence_no": 3, "content": "회수", "weather": "sunny"}], []]
    rows = [seed.WeeklyRow("다음 주", "sunny", next_week, 1),
            seed.WeeklyRow("추가", "rainy", WEEK, 4), seed.WeeklyRow("회수", "sunny", WEEK, 3)]
    assert await seed.insert_weekly_rows(c, rows) == (2, 1)
    assert [call.args[1] for call in c.execute.await_args_list] == [WEEK.toordinal(), next_week.toordinal()]
    assert c.method_calls[0][0] == c.method_calls[1][0] == "execute"
    c.executemany.assert_awaited_once_with(seed._WEEK_INSERT,
                                         [("추가", "rainy", WEEK, 4), ("다음 주", "sunny", next_week, 1)])


async def test_later_week_invalid_prevents_all_inserts():
    c = AsyncMock()
    c.fetch.side_effect = [[], [{"sequence_no": 1, "content": "원래", "weather": "sunny"}]]
    with pytest.raises(ValueError, match="수정 불가"):
        await seed.insert_weekly_rows(c, [seed.WeeklyRow("추가", "sunny", WEEK, 1),
                                         seed.WeeklyRow("변경", "sunny", date(2026, 9, 14), 1)])
    c.executemany.assert_not_awaited()


def fake_connection(monkeypatch):
    c, tx = AsyncMock(), AsyncMock()
    # asyncpg.transaction() is synchronous; its lifecycle methods are asynchronous.
    c.transaction = lambda: tx
    c.fetch.return_value = []
    connect = AsyncMock(return_value=c)
    monkeypatch.setattr(seed.asyncpg, "connect", connect)
    monkeypatch.setattr(seed, "load_conn", lambda env: "fake-dsn")
    monkeypatch.setattr(seed, "announce", lambda *a, **kw: None)
    monkeypatch.setattr(seed, "is_prod", lambda env: False)
    return c, tx, connect


@pytest.mark.parametrize("commit", [False, True])
async def test_whole_file_dry_run_or_commit(tmp_path, monkeypatch, commit):
    c, tx, connect = fake_connection(monkeypatch)
    path = write_csv(tmp_path, [["2026-09-07", "1", "sunny", "원고"]])
    await seed.main(commit, path, "dev")
    connect.assert_awaited_once_with("fake-dsn", statement_cache_size=0)
    tx.start.assert_awaited_once()
    (tx.commit if commit else tx.rollback).assert_awaited_once()
    (tx.rollback if commit else tx.commit).assert_not_awaited()
    c.close.assert_awaited_once()


async def test_failure_rolls_back_file_and_closes(tmp_path, monkeypatch):
    c, tx, _ = fake_connection(monkeypatch)
    c.executemany.side_effect = RuntimeError("write failed")
    path = write_csv(tmp_path, [["2026-09-07", "1", "sunny", "원고"]])
    with pytest.raises(RuntimeError, match="write failed"):
        await seed.main(True, path, "dev")
    tx.rollback.assert_awaited_once()
    tx.commit.assert_not_awaited()
    c.close.assert_awaited_once()


async def test_invalid_file_never_connects(tmp_path, monkeypatch):
    _, _, connect = fake_connection(monkeypatch)
    path = write_csv(tmp_path, [["2026-09-07", "1", "sunny", "원고"], ["2026-09-07", "2", "sunny", ""]])
    with pytest.raises(SystemExit):
        await seed.main(True, path, "dev")
    connect.assert_not_awaited()


def test_weekly_template_default_separate_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    legacy = tmp_path / "db/capi_diaries.csv"
    legacy.write_text("기존 원고", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["template", "--weekly", "--week-start", "2026-09-07", "--count", "3"])
    template.main()
    assert legacy.read_text(encoding="utf-8") == "기존 원고"
    with (tmp_path / "db/capi_diaries_weekly.csv").open() as f:
        rows = list(csv.DictReader(f))
    assert [r["sequence_no"] for r in rows] == ["1", "2", "3"]
    assert {r["week_start_date"] for r in rows} == {"2026-09-07"}
    with pytest.raises(SystemExit, match="이미 존재"):
        template.main()


@pytest.mark.parametrize("args", [
    ["--weekly"], ["--weekly", "--week-start", "2026-09-08", "--count", "3"],
    ["--weekly", "--week-start", "2026-09-07", "--count", "0"],
    ["--weekly", "--week-start", "2026-09-07", "--count", "1", "--days", "2"],
    ["--week-start", "2026-09-07"], ["--days", "0"],
])
def test_template_invalid_arguments(tmp_path, monkeypatch, args):
    out = tmp_path / "absent.csv"
    monkeypatch.setattr("sys.argv", ["template", *args, "--out", str(out)])
    with pytest.raises(SystemExit):
        template.main()
    assert not out.exists()
