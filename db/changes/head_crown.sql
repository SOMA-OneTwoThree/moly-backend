-- Adds the subscriber-only crown hat to an existing catalog. Expected: 1 row inserted.
-- Apply after head_crown/v1/thumb.png and head_crown/v1/rightside/upright.png are in shop-assets.
-- Rollback: UPDATE public.products SET is_active = false WHERE public_id = 'head_crown';
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';
INSERT INTO public.products
SELECT * FROM json_populate_recordset(NULL::public.products, $catalog$
[
{
  "id": "00000000-0000-4000-8000-000000000212",
  "product_type": "cosmetic",
  "name": "왕관",
  "description": null,
  "slot": "hat",
  "price_hay": null,
  "is_subscriber_only": true,
  "assets": {
    "rightside": {
      "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_crown/v1/rightside/upright.png"
    },
    "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_crown/v1/thumb.png"
  },
  "hay_amount": null,
  "price_krw": null,
  "app_store_product_id": null,
  "is_active": true,
  "sort_order": 10,
  "public_id": "head_crown",
  "asset_version": 1,
  "is_v2_only": true,
  "play_store_product_id": null,
  "name_i18n": {
    "en": "Crown",
    "ja": "王冠",
    "ko": "왕관"
  }
}
]
$catalog$);
