-- The default theme is bundled in every app, so its scene only needs to satisfy the
-- contract. Points the scene at the shop images instead of the retired sofa-room files.
-- Expected: 1 row updated.
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';
UPDATE public.products
SET assets = jsonb_set(assets, '{scene}', '{"canvas": {"width": 393, "height": 852}, "layers": [{"id": "background", "frame": {"x": 0, "y": 0, "width": 393, "height": 852}, "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v2/detail.png", "z_index": 0}], "character_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v2/thumb.png", "character_frame": {"x": 172, "y": 415, "width": 185, "height": 89}}'::jsonb)
WHERE public_id = 'theme_default';
