-- 이미 online 구조가 있는 개발 DB용 좁은 호환 보완이다.
-- 기존 공개 행은 보존하고 NULL 준비 영역과 각 준비 ID의 게시 경계를 분리한다.
-- 짧은 잠금/전체 단일 TX 제한에서 실패하면 기존 index까지 함께 rollback한다.
DROP INDEX public.conversation_checkpoints_published_window_uq;
CREATE UNIQUE INDEX conversation_checkpoints_published_window_uq
    ON public.conversation_checkpoints USING btree (user_id, coverage_through_message_id)
    WHERE kind = 'window' AND publish_state = 'published' AND preparation_id IS NULL;
CREATE UNIQUE INDEX conversation_checkpoints_prepared_window_uq
    ON public.conversation_checkpoints USING btree (preparation_id, user_id, coverage_through_message_id)
    WHERE kind = 'window' AND publish_state = 'published' AND preparation_id IS NOT NULL;
