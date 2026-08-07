-- 대화 요약에 기억 세대를 남긴다(W11 × W10).
--
-- 잊어줘는 요약을 전량 지우지만, 지우는 트랜잭션과 요약을 쓰는 트랜잭션이 겹치면 한쪽이
-- 다른 쪽의 미커밋 행을 못 본다(READ COMMITTED). 그 틈으로 들어온 요약은 삭제를 피한다.
-- 잠금으로 막으면 챗 Phase 2와 락 순서가 반대라 교착이 나므로, 지우는 대신 **읽을 때 거른다**.
-- 세대가 현재와 다른 요약은 조회되지 않아 프롬프트에 실리지 않는다.
--
-- 기존 행은 0이고 chat_contexts.memory_generation 기본값도 0이라, 전환 이력이 없는 유저는
-- 종전과 동일하게 조회된다.
ALTER TABLE public.conversation_checkpoints
  ADD COLUMN IF NOT EXISTS memory_generation bigint NOT NULL DEFAULT 0;

-- 최신 조회가 세대까지 함께 거르므로 인덱스도 세대를 앞에 둔다.
CREATE INDEX IF NOT EXISTS conversation_checkpoints_live_idx
  ON public.conversation_checkpoints(user_id, memory_generation, through_message_id DESC);
