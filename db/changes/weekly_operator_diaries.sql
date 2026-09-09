-- Weekly operator diary expansion from baseline 1414b435.
-- Release handoff: apply with db.apply; see docs/OPERATIONS.md.
-- Apply before new code, while weekly cutover remains unset.
-- Existing rows need no backfill. Run in an approved maintenance window.
-- Stop on lock timeout; estimate table/index size before choosing concurrent indexes.
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';
ALTER TABLE public.moly_life_ments ADD COLUMN week_start_date date, ADD COLUMN sequence_no integer;
ALTER TABLE public.diary_generation_results ADD COLUMN preset_ment_id uuid;
ALTER TABLE public.diary_generation_results DROP CONSTRAINT diary_generation_results_status_check;
ALTER TABLE public.moly_life_ments ADD CONSTRAINT moly_life_ments_week_shape_check CHECK ((((week_start_date IS NULL) AND (sequence_no IS NULL)) OR ((week_start_date IS NOT NULL) AND (sequence_no IS NOT NULL) AND (diary_date IS NULL) AND (EXTRACT(isodow FROM week_start_date) = 1) AND (sequence_no > 0))));
ALTER TABLE public.diary_generation_results ADD CONSTRAINT diary_generation_results_status_check CHECK ((status = ANY (ARRAY['no_entry'::text, 'preset'::text])));
ALTER TABLE public.diary_generation_results ADD CONSTRAINT diary_generation_results_preset_shape_check CHECK ((((status = 'preset'::text) AND (preset_ment_id IS NOT NULL)) OR ((status = 'no_entry'::text) AND (preset_ment_id IS NULL))));
CREATE UNIQUE INDEX diary_generation_results_user_preset_uq ON public.diary_generation_results USING btree (user_id, preset_ment_id) WHERE (status = 'preset'::text);
CREATE UNIQUE INDEX moly_life_ments_week_sequence_uq ON public.moly_life_ments USING btree (week_start_date, sequence_no) WHERE (week_start_date IS NOT NULL);
ALTER TABLE public.diary_generation_results ADD CONSTRAINT diary_generation_results_preset_ment_id_fkey FOREIGN KEY (preset_ment_id) REFERENCES public.moly_life_ments(id) ON DELETE RESTRICT;
-- Validate with regenerated schema contract and legacy/weekly fixtures.
-- After weekly receipts exist: retain columns, constraints and receipts; recover forward.
-- Do not roll back to a legacy writer or remove the cutover setting.
