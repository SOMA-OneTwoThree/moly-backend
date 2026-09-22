-- Adds the hot spring theme to an existing catalog. Expected: 1 row inserted.
-- assets.bundled: only apps listing theme_onsen in X-Moly-Bundled-Themes see it.
-- Rollback: UPDATE public.products SET is_active = false WHERE public_id = 'theme_onsen';
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';
INSERT INTO public.products
SELECT * FROM json_populate_recordset(NULL::public.products, $catalog$
[
{
  "id": "00000000-0000-4000-8000-000000000103",
  "product_type": "cosmetic",
  "name": "온천",
  "description": null,
  "slot": "theme",
  "price_hay": 4000,
  "is_subscriber_only": false,
  "assets": {
    "bundled": true,
    "scene": {
      "canvas": {
        "width": 393,
        "height": 852
      },
      "layers": [
        {
          "id": "background",
          "frame": {
            "x": 0,
            "y": 0,
            "width": 393,
            "height": 852
          },
          "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_onsen/v1/background-day.png",
          "night_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_onsen/v1/background-night.png",
          "z_index": 0
        }
      ],
      "character_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_onsen/v1/character.png",
      "character_frame": {
        "x": 190.5,
        "y": 424.75,
        "width": 181,
        "height": 91
      }
    },
    "detail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_onsen/v1/detail.png",
    "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_onsen/v1/thumb.png"
  },
  "hay_amount": null,
  "price_krw": null,
  "app_store_product_id": null,
  "is_active": true,
  "sort_order": 3,
  "public_id": "theme_onsen",
  "asset_version": 1,
  "is_v2_only": true,
  "play_store_product_id": null,
  "name_i18n": {
    "en": "Hot Spring",
    "ja": "温泉",
    "ko": "온천"
  }
}
]
$catalog$);
