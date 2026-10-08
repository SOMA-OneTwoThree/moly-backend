-- Routine redesign 3/3: template catalogue and data backfill. Run after 2/3; safe to run again.
-- Each run writes at most 50,000 rows per statement. Repeat it until the "remaining" query below
-- returns 0 for every column (prod needs one run), then run db.verify and only then merge/deploy the
-- new image.
-- Target: catalogue rows missing by id (6 categories, 23 templates); one routine_schedules row for each
-- routine (deleted included) that has none, effective from created_at in the profile time zone
-- (Asia/Seoul when the zone is unknown) with the current days; deleted_on from deleted_at the same way;
-- template icon, color and template_id for default routines whose localized name is untouched.
-- Before and after each run (read-only, the expected/remaining rows):
--   SELECT
--     (SELECT count(*) FROM public.routines r WHERE NOT EXISTS
--        (SELECT 1 FROM public.routine_schedules s WHERE s.routine_id = r.id)) AS schedules,
--     (SELECT count(*) FROM public.routines
--      WHERE deleted_at IS NOT NULL AND deleted_on IS NULL) AS deleted_on,
--     (SELECT count(*) FROM public.routines
--      WHERE template_id IS NULL AND name_i18n ->> 'ko' IN ('이불 정리하기', '물 마시기')) AS defaults,
--     (SELECT 29 - (SELECT count(*) FROM public.routine_template_categories)
--        - (SELECT count(*) FROM public.routine_templates)) AS catalogue;
--   prod 2026-10-08 before the first run: routines 3,376 (schedules 3,376), deleted 351,
--   routines with name_i18n 1,826 (the default-routine upper bound), catalogue 29.
--   Time zones that fall back to Asia/Seoul:
--   SELECT count(*) FROM public.profiles p
--   WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_timezone_names z WHERE z.name = p.timezone);
-- Lock: ROW EXCLUSIVE only; reads go on. A request that updates or deletes one of the rows written here
-- waits until COMMIT. Updated routine rows get a new updated_at from the trigger. If a deadlock or lock
-- timeout rolls a run back, run it again.
-- Mixed versions: the previous image keeps working. Routines it creates, edits or deletes during or
-- after this file are covered by the code fallbacks (created_at/updated_at/deleted_at in the profile
-- time zone); a later run also backfills the ones it created meanwhile.
-- Rollback: the previous image never reads these rows. Before the new image is deployed they can be
-- removed with DELETE FROM public.routine_schedules; DELETE FROM public.routine_templates;
-- DELETE FROM public.routine_template_categories; and
-- UPDATE public.routines SET icon = 'seedling', color = 'peach', template_id = NULL, deleted_on = NULL
-- WHERE template_id IN ('make_bed', 'drink_water') OR deleted_on IS NOT NULL;
-- BEGIN/COMMIT keep SET LOCAL effective under plain psql; db.apply strips them and uses its own transaction.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';

INSERT INTO public.routine_template_categories (id, name_i18n, sort_order) VALUES
('morning', '{"ko":"아침","en":"Morning","ja":"朝"}', 1),
('health', '{"ko":"건강","en":"Health","ja":"健康"}', 2),
('sleep', '{"ko":"숙면","en":"Sleep","ja":"睡眠"}', 3),
('mind', '{"ko":"마음","en":"Mind","ja":"こころ"}', 4),
('home', '{"ko":"생활","en":"Home","ja":"暮らし"}', 5),
('growth', '{"ko":"자기계발","en":"Growth","ja":"自分磨き"}', 6)
ON CONFLICT (id) DO NOTHING;
INSERT INTO public.routine_templates
  (id, category_id, name_i18n, icon, color, days_of_week, is_recommended, sort_order) VALUES
('make_bed', 'morning', '{"ko":"이불 정리하기","en":"Make the bed","ja":"布団を整える"}', 'bed', 'peach', '{1,2,3,4,5,6,7}', true, 1),
('drink_water', 'morning', '{"ko":"물 마시기","en":"Drink water","ja":"水を飲む"}', 'droplet', 'blue', '{1,2,3,4,5,6,7}', true, 2),
('morning_stretch', 'morning', '{"ko":"아침 스트레칭","en":"Morning stretch","ja":"朝のストレッチ"}', 'person_cartwheeling', 'green', '{1,2,3,4,5,6,7}', false, 3),
('eat_breakfast', 'morning', '{"ko":"아침 챙겨 먹기","en":"Eat breakfast","ja":"朝ごはんを食べる"}', 'cooking', 'yellow', '{1,2,3,4,5,6,7}', false, 4),
('get_sunlight', 'morning', '{"ko":"햇볕 쬐기","en":"Get some sunlight","ja":"日光を浴びる"}', 'sun_with_face', 'yellow', '{1,2,3,4,5,6,7}', false, 5),
('take_vitamins', 'health', '{"ko":"영양제 챙기기","en":"Take vitamins","ja":"サプリを飲む"}', 'pill', 'pink', '{1,2,3,4,5,6,7}', false, 1),
('walk_10_min', 'health', '{"ko":"10분 걷기","en":"Walk for 10 minutes","ja":"10分歩く"}', 'person_walking', 'green', '{1,2,3,4,5,6,7}', false, 2),
('eat_vegetables', 'health', '{"ko":"채소 먹기","en":"Eat vegetables","ja":"野菜を食べる"}', 'broccoli', 'green', '{1,2,3,4,5,6,7}', false, 3),
('work_out', 'health', '{"ko":"운동하기","en":"Work out","ja":"運動する"}', 'flexed_biceps', 'peach', '{1,3,5}', false, 4),
('sleep_early', 'sleep', '{"ko":"일찍 잠자리에 들기","en":"Go to bed early","ja":"早めに寝る"}', 'crescent_moon', 'lavender', '{1,2,3,4,5,6,7}', false, 1),
('no_phone_in_bed', 'sleep', '{"ko":"자기 전 폰 내려놓기","en":"No phone in bed","ja":"寝る前はスマホを置く"}', 'no_mobile_phones', 'lavender', '{1,2,3,4,5,6,7}', false, 2),
('warm_tea', 'sleep', '{"ko":"따뜻한 차 마시기","en":"Drink warm tea","ja":"温かいお茶を飲む"}', 'teacup_without_handle', 'mint', '{1,2,3,4,5,6,7}', false, 3),
('deep_breaths', 'mind', '{"ko":"심호흡하기","en":"Take deep breaths","ja":"深呼吸する"}', 'wind_face', 'blue', '{1,2,3,4,5,6,7}', false, 1),
('meditate', 'mind', '{"ko":"명상하기","en":"Meditate","ja":"瞑想する"}', 'person_in_lotus_position', 'lavender', '{1,2,3,4,5,6,7}', false, 2),
('gratitude_note', 'mind', '{"ko":"감사한 일 적기","en":"Gratitude note","ja":"感謝したことを書く"}', 'memo', 'yellow', '{1,2,3,4,5,6,7}', false, 3),
('look_at_sky', 'mind', '{"ko":"하늘 보기","en":"Look at the sky","ja":"空を見上げる"}', 'cloud', 'blue', '{1,2,3,4,5,6,7}', false, 4),
('tidy_room', 'home', '{"ko":"방 정리하기","en":"Tidy up my room","ja":"部屋を片付ける"}', 'broom', 'mint', '{1,2,3,4,5,6,7}', false, 1),
('do_dishes', 'home', '{"ko":"설거지하기","en":"Do the dishes","ja":"皿洗いをする"}', 'fork_and_knife_with_plate', 'mint', '{1,2,3,4,5,6,7}', false, 2),
('water_plants', 'home', '{"ko":"식물에 물 주기","en":"Water the plants","ja":"植物に水をやる"}', 'potted_plant', 'green', '{1,4}', false, 3),
('do_laundry', 'home', '{"ko":"빨래하기","en":"Do the laundry","ja":"洗濯する"}', 't_shirt', 'blue', '{7}', false, 4),
('read_book', 'growth', '{"ko":"책 읽기","en":"Read a book","ja":"本を読む"}', 'books', 'peach', '{1,2,3,4,5,6,7}', false, 1),
('study', 'growth', '{"ko":"공부하기","en":"Study","ja":"勉強する"}', 'pencil', 'yellow', '{1,2,3,4,5,6,7}', false, 2),
('learn_words', 'growth', '{"ko":"단어 외우기","en":"Learn new words","ja":"単語を覚える"}', 'open_book', 'pink', '{1,2,3,4,5,6,7}', false, 3)
ON CONFLICT (id) DO NOTHING;

INSERT INTO public.routine_schedules (routine_id, user_id, effective_from, days_of_week)
SELECT r.id, r.user_id, (r.created_at AT TIME ZONE COALESCE(z.name, 'Asia/Seoul'))::date, r.days_of_week
FROM public.routines r
JOIN public.profiles p ON p.id = r.user_id
LEFT JOIN pg_catalog.pg_timezone_names z ON z.name = p.timezone
WHERE NOT EXISTS (SELECT 1 FROM public.routine_schedules s WHERE s.routine_id = r.id)
LIMIT 50000
ON CONFLICT (routine_id, effective_from) DO NOTHING;

UPDATE public.routines r
SET deleted_on = (r.deleted_at AT TIME ZONE COALESCE(z.name, 'Asia/Seoul'))::date
FROM public.profiles p
LEFT JOIN pg_catalog.pg_timezone_names z ON z.name = p.timezone
WHERE p.id = r.user_id AND r.id IN (
  SELECT id FROM public.routines WHERE deleted_at IS NOT NULL AND deleted_on IS NULL LIMIT 50000
);

UPDATE public.routines SET icon = 'bed', color = 'peach', template_id = 'make_bed'
WHERE id IN (
  SELECT id FROM public.routines
  WHERE name_i18n ->> 'ko' = '이불 정리하기' AND template_id IS NULL LIMIT 50000
);
UPDATE public.routines SET icon = 'droplet', color = 'blue', template_id = 'drink_water'
WHERE id IN (
  SELECT id FROM public.routines
  WHERE name_i18n ->> 'ko' = '물 마시기' AND template_id IS NULL LIMIT 50000
);
COMMIT;
