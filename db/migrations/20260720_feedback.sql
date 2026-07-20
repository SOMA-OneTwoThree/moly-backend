-- feedback 테이블 신설 — 인앱 문의 폼(POST /feedback).
-- message는 필수 자유 텍스트, contact는 기프티콘 이벤트용 선택 연락처(이메일·전화·인스타 등).
-- RLS deny-default(서버 서비스 롤만 접근) — 다른 테이블과 동일.
BEGIN;

CREATE TABLE public.feedback (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  message    text NOT NULL CHECK (char_length(message) <= 2000),
  contact    text CHECK (char_length(contact) <= 200),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX feedback_user_idx ON public.feedback (user_id);

ALTER TABLE public.feedback ENABLE ROW LEVEL SECURITY;

COMMIT;
