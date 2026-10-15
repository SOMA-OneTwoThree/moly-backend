-- Moves the default theme to asset_version 2 so its shop images show the cabin room,
-- and renames it to match. Expected: 1 row updated.
-- Rollback: v2 -> v1, asset_version = 1, name '집', name_i18n Home/おうち/집.
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';
UPDATE public.products
SET asset_version = 2,
    name = '오두막집',
    name_i18n = '{"en": "Cabin", "ja": "山小屋", "ko": "오두막집"}'::jsonb,
    assets = replace(assets::text, '/theme_default/v1/', '/theme_default/v2/')::jsonb
WHERE public_id = 'theme_default' AND asset_version = 1;
