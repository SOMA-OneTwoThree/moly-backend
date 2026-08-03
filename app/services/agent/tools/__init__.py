"""W6 — 도구 4종. 도구당 파일 1개 + `registry.py`.

registry에 **실제로 등록된 것은 2개**(`get_diary`·`get_routines`)다. 나머지 둘은 파일은 있지만
등록하지 않는다 — 사유는 `registry.py`의 `DISABLED` 표에 있다.

모든 도구의 공통 계약(`base.py`):
- 쿼리에는 **항상 `user_id = ctx.user_id`**가 들어간다. `user_id`는 서버가 `ToolContext`로 주입하며
  모델 인자에는 `user_id`도, SQL도, 자유 필터도 없다.
- 반환 문자열은 전부 `_sanitize`(=`memory.sanitize_text`)를 통과한다. 루틴 이름처럼 유저 자유 입력이
  섞이는 값이 프롬프트 구조를 흉내내지 못하게 한다.
- 행 수·문자 수·날짜 범위를 전부 제한하고, 잘렸으면 `truncated=True`다. 다만 이 상한들은
  **개별 안전장치일 뿐**이고 비용을 정하는 것은 턴 합계 예산(`agent_tool_result_budget_tokens`)이다 —
  절단의 최종 책임은 runner(`runtime.apply_result_budget`)에 있다.
"""
