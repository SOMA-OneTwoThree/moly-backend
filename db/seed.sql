-- Initial catalog for a NEW environment. Structural truth is schema.sql.
-- Existing catalog changes use reviewed SQL; replaying this file is not an upgrade.
-- NULL prices remain unsellable; the retired workout theme remains inactive.
-- AI prices preserve the effective-dated operational catalog, including unknown NULL rates.
BEGIN;
SET LOCAL lock_timeout = '2s';
-- products
INSERT INTO public.products
SELECT * FROM json_populate_recordset(NULL::public.products, $catalog$
[
  {
    "id": "00000000-0000-4000-8000-000000000101",
    "product_type": "cosmetic",
    "name": "오두막집",
    "description": null,
    "slot": "theme",
    "price_hay": null,
    "is_subscriber_only": false,
    "assets": {
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
            "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v2/detail.png",
            "z_index": 0
          }
        ],
        "character_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v2/thumb.png",
        "character_frame": {
          "x": 172,
          "y": 415,
          "width": 185,
          "height": 89
        }
      },
      "detail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v2/detail.png",
      "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v2/thumb.png"
    },
    "hay_amount": null,
    "price_krw": null,
    "app_store_product_id": null,
    "is_active": true,
    "sort_order": 1,
    "public_id": "theme_default",
    "asset_version": 2,
    "is_v2_only": false,
    "play_store_product_id": null,
    "name_i18n": {
      "en": "Cabin",
      "ja": "山小屋",
      "ko": "오두막집"
    }
  },
  {
    "id": "00000000-0000-4000-8000-000000000102",
    "product_type": "cosmetic",
    "name": "운동",
    "description": null,
    "slot": "theme",
    "price_hay": 4000,
    "is_subscriber_only": false,
    "assets": {
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
            "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_workout/v1/background-day.png",
            "z_index": 0
          },
          {
            "id": "photo",
            "frame": {
              "x": 39,
              "y": 150,
              "width": 96,
              "height": 99.3
            },
            "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_workout/v1/photo-day.png",
            "z_index": 10
          },
          {
            "id": "window",
            "frame": {
              "x": 192,
              "y": 155,
              "width": 171,
              "height": 113
            },
            "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_workout/v1/window-day.png",
            "z_index": 20
          },
          {
            "id": "light",
            "frame": {
              "x": 234,
              "y": 0,
              "width": 61.5,
              "height": 130
            },
            "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_workout/v1/light-day.png",
            "z_index": 30
          },
          {
            "id": "dumbbell",
            "frame": {
              "x": 25,
              "y": 350,
              "width": 125,
              "height": 98
            },
            "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_workout/v1/dumbbell-day.png",
            "z_index": 40
          },
          {
            "id": "treadmill",
            "frame": {
              "x": 200,
              "y": 286.9,
              "width": 155,
              "height": 214.7
            },
            "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_workout/v1/treadmill-day.png",
            "z_index": 50
          },
          {
            "id": "mat",
            "frame": {
              "x": 72,
              "y": 584.8,
              "width": 233,
              "height": 91.3
            },
            "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_workout/v1/mat-day.png",
            "z_index": 60
          },
          {
            "id": "water",
            "frame": {
              "x": 315,
              "y": 585.2,
              "width": 33.2,
              "height": 83.6
            },
            "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_workout/v1/water-day.png",
            "z_index": 70
          }
        ],
        "character_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_workout/v1/character.png",
        "character_frame": {
          "x": 98.1,
          "y": 466.1,
          "width": 196.8,
          "height": 182
        }
      },
      "detail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_workout/v1/detail.png",
      "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_workout/v1/thumb.png"
    },
    "hay_amount": null,
    "price_krw": null,
    "app_store_product_id": null,
    "is_active": false,
    "sort_order": 2,
    "public_id": "theme_workout",
    "asset_version": 1,
    "is_v2_only": false,
    "play_store_product_id": null,
    "name_i18n": {
      "en": "Workout",
      "ja": "運動",
      "ko": "운동"
    }
  },
  {
    "id": "00000000-0000-4000-8000-000000000103",
    "product_type": "cosmetic",
    "name": "온천",
    "description": null,
    "slot": "theme",
    "price_hay": null,
    "is_subscriber_only": true,
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
            "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_onsen/v1/detail.png",
            "z_index": 0
          }
        ],
        "character_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_onsen/v1/thumb.png",
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
  },
  {
    "id": "00000000-0000-4000-8000-000000000201",
    "product_type": "cosmetic",
    "name": "선글라스",
    "description": null,
    "slot": "glasses",
    "price_hay": 1000,
    "is_subscriber_only": false,
    "assets": {
      "rightside": {
        "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_sunglasses/v2/rightside/upright.png"
      },
      "detail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_sunglasses/v2/detail.png",
      "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_sunglasses/v2/thumb.png",
      "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_sunglasses/v2/upright.png"
    },
    "hay_amount": null,
    "price_krw": null,
    "app_store_product_id": null,
    "is_active": true,
    "sort_order": 1,
    "public_id": "head_sunglasses",
    "asset_version": 2,
    "is_v2_only": false,
    "play_store_product_id": null,
    "name_i18n": {
      "en": "Sunglasses",
      "ja": "サングラス",
      "ko": "선글라스"
    }
  },
  {
    "id": "00000000-0000-4000-8000-000000000202",
    "product_type": "cosmetic",
    "name": "머리 위에 귤",
    "description": null,
    "slot": "hat",
    "price_hay": 1000,
    "is_subscriber_only": false,
    "assets": {
      "rightside": {
        "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_mandarin/v2/rightside/upright.png"
      },
      "detail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_mandarin/v2/detail.png",
      "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_mandarin/v2/thumb.png",
      "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_mandarin/v2/upright.png"
    },
    "hay_amount": null,
    "price_krw": null,
    "app_store_product_id": null,
    "is_active": true,
    "sort_order": 2,
    "public_id": "head_mandarin",
    "asset_version": 2,
    "is_v2_only": false,
    "play_store_product_id": null,
    "name_i18n": {
      "en": "Tangerine on Head",
      "ja": "頭の上のみかん",
      "ko": "머리 위에 귤"
    }
  },
  {
    "id": "00000000-0000-4000-8000-000000000203",
    "product_type": "cosmetic",
    "name": "선크림",
    "description": null,
    "slot": "glasses",
    "price_hay": 1000,
    "is_subscriber_only": false,
    "assets": {
      "rightside": {
        "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_suncream/v1/rightside/upright.png"
      },
      "detail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_suncream/v1/detail.png",
      "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_suncream/v1/thumb.png",
      "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_suncream/v1/upright.png"
    },
    "hay_amount": null,
    "price_krw": null,
    "app_store_product_id": null,
    "is_active": true,
    "sort_order": 3,
    "public_id": "head_suncream",
    "asset_version": 1,
    "is_v2_only": false,
    "play_store_product_id": null,
    "name_i18n": {
      "en": "Sunscreen",
      "ja": "日焼け止め",
      "ko": "선크림"
    }
  },
  {
    "id": "00000000-0000-4000-8000-000000000204",
    "product_type": "cosmetic",
    "name": "동글이 안경",
    "description": null,
    "slot": "glasses",
    "price_hay": 1000,
    "is_subscriber_only": false,
    "assets": {
      "rightside": {
        "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_glasses/v1/rightside/upright.png"
      },
      "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_glasses/v1/thumb.png"
    },
    "hay_amount": null,
    "price_krw": null,
    "app_store_product_id": null,
    "is_active": true,
    "sort_order": 4,
    "public_id": "head_glasses",
    "asset_version": 1,
    "is_v2_only": true,
    "play_store_product_id": null,
    "name_i18n": {
      "en": "Round Glasses",
      "ja": "丸メガネ",
      "ko": "동글이 안경"
    }
  },
  {
    "id": "00000000-0000-4000-8000-000000000205",
    "product_type": "cosmetic",
    "name": "버킷햇",
    "description": null,
    "slot": "hat",
    "price_hay": 1000,
    "is_subscriber_only": false,
    "assets": {
      "rightside": {
        "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_bucket/v1/rightside/upright.png"
      },
      "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_bucket/v1/thumb.png"
    },
    "hay_amount": null,
    "price_krw": null,
    "app_store_product_id": null,
    "is_active": true,
    "sort_order": 3,
    "public_id": "head_bucket",
    "asset_version": 1,
    "is_v2_only": true,
    "play_store_product_id": null,
    "name_i18n": {
      "en": "Bucket Hat",
      "ja": "バケットハット",
      "ko": "버킷햇"
    }
  },
  {
    "id": "00000000-0000-4000-8000-000000000206",
    "product_type": "cosmetic",
    "name": "캡모자",
    "description": null,
    "slot": "hat",
    "price_hay": 1000,
    "is_subscriber_only": false,
    "assets": {
      "rightside": {
        "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_cap/v1/rightside/upright.png"
      },
      "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_cap/v1/thumb.png"
    },
    "hay_amount": null,
    "price_krw": null,
    "app_store_product_id": null,
    "is_active": true,
    "sort_order": 4,
    "public_id": "head_cap",
    "asset_version": 1,
    "is_v2_only": true,
    "play_store_product_id": null,
    "name_i18n": {
      "en": "Cap",
      "ja": "キャップ",
      "ko": "캡모자"
    }
  },
  {
    "id": "00000000-0000-4000-8000-000000000207",
    "product_type": "cosmetic",
    "name": "빵모자",
    "description": null,
    "slot": "hat",
    "price_hay": 1000,
    "is_subscriber_only": false,
    "assets": {
      "rightside": {
        "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_beret/v1/rightside/upright.png"
      },
      "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_beret/v1/thumb.png"
    },
    "hay_amount": null,
    "price_krw": null,
    "app_store_product_id": null,
    "is_active": true,
    "sort_order": 5,
    "public_id": "head_beret",
    "asset_version": 1,
    "is_v2_only": true,
    "play_store_product_id": null,
    "name_i18n": {
      "en": "Beret",
      "ja": "ベレー帽",
      "ko": "빵모자"
    }
  },
  {
    "id": "00000000-0000-4000-8000-000000000208",
    "product_type": "cosmetic",
    "name": "두건",
    "description": null,
    "slot": "hat",
    "price_hay": 1000,
    "is_subscriber_only": false,
    "assets": {
      "rightside": {
        "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_bandana/v1/rightside/upright.png"
      },
      "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_bandana/v1/thumb.png"
    },
    "hay_amount": null,
    "price_krw": null,
    "app_store_product_id": null,
    "is_active": true,
    "sort_order": 6,
    "public_id": "head_bandana",
    "asset_version": 1,
    "is_v2_only": true,
    "play_store_product_id": null,
    "name_i18n": {
      "en": "Bandana",
      "ja": "バンダナ",
      "ko": "두건"
    }
  },
  {
    "id": "00000000-0000-4000-8000-000000000209",
    "product_type": "cosmetic",
    "name": "수건",
    "description": null,
    "slot": "hat",
    "price_hay": null,
    "is_subscriber_only": true,
    "assets": {
      "rightside": {
        "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_towel/v1/rightside/upright.png"
      },
      "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_towel/v1/thumb.png"
    },
    "hay_amount": null,
    "price_krw": null,
    "app_store_product_id": null,
    "is_active": true,
    "sort_order": 7,
    "public_id": "head_towel",
    "asset_version": 1,
    "is_v2_only": true,
    "play_store_product_id": null,
    "name_i18n": {
      "en": "Towel",
      "ja": "タオル",
      "ko": "수건"
    }
  },
  {
    "id": "00000000-0000-4000-8000-000000000210",
    "product_type": "cosmetic",
    "name": "수박 모자",
    "description": null,
    "slot": "hat",
    "price_hay": null,
    "is_subscriber_only": true,
    "assets": {
      "rightside": {
        "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_watermelon/v1/rightside/upright.png"
      },
      "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_watermelon/v1/thumb.png"
    },
    "hay_amount": null,
    "price_krw": null,
    "app_store_product_id": null,
    "is_active": true,
    "sort_order": 8,
    "public_id": "head_watermelon",
    "asset_version": 1,
    "is_v2_only": true,
    "play_store_product_id": null,
    "name_i18n": {
      "en": "Watermelon Hat",
      "ja": "スイカ帽子",
      "ko": "수박 모자"
    }
  },
  {
    "id": "00000000-0000-4000-8000-000000000211",
    "product_type": "cosmetic",
    "name": "토끼 모자",
    "description": null,
    "slot": "hat",
    "price_hay": 1000,
    "is_subscriber_only": false,
    "assets": {
      "rightside": {
        "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_rabbit/v1/rightside/upright.png"
      },
      "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/head_rabbit/v1/thumb.png"
    },
    "hay_amount": null,
    "price_krw": null,
    "app_store_product_id": null,
    "is_active": true,
    "sort_order": 9,
    "public_id": "head_rabbit",
    "asset_version": 1,
    "is_v2_only": true,
    "play_store_product_id": null,
    "name_i18n": {
      "en": "Bunny Hood",
      "ja": "うさぎ帽子",
      "ko": "토끼 모자"
    }
  },
  {
    "id": "00000000-0000-4000-8000-000000000301",
    "product_type": "cosmetic",
    "name": "사원증",
    "description": null,
    "slot": "neck",
    "price_hay": 1000,
    "is_subscriber_only": false,
    "assets": {
      "rightside": {
        "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck_employee_badge/v2/rightside/upright.png"
      },
      "detail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck_employee_badge/v2/detail.png",
      "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck_employee_badge/v2/thumb.png",
      "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck_employee_badge/v2/upright.png"
    },
    "hay_amount": null,
    "price_krw": null,
    "app_store_product_id": null,
    "is_active": true,
    "sort_order": 1,
    "public_id": "neck_employee_badge",
    "asset_version": 2,
    "is_v2_only": false,
    "play_store_product_id": null,
    "name_i18n": {
      "en": "ID Badge",
      "ja": "社員証",
      "ko": "사원증"
    }
  },
  {
    "id": "00000000-0000-4000-8000-000000000302",
    "product_type": "cosmetic",
    "name": "목도리",
    "description": null,
    "slot": "neck",
    "price_hay": 1000,
    "is_subscriber_only": false,
    "assets": {
      "rightside": {
        "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck_muffler/v2/rightside/upright.png"
      },
      "detail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck_muffler/v2/detail.png",
      "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck_muffler/v2/thumb.png",
      "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck_muffler/v2/upright.png"
    },
    "hay_amount": null,
    "price_krw": null,
    "app_store_product_id": null,
    "is_active": true,
    "sort_order": 2,
    "public_id": "neck_muffler",
    "asset_version": 2,
    "is_v2_only": false,
    "play_store_product_id": null,
    "name_i18n": {
      "en": "Scarf",
      "ja": "マフラー",
      "ko": "목도리"
    }
  },
  {
    "id": "00000000-0000-4000-8000-000000000303",
    "product_type": "cosmetic",
    "name": "조개 목걸이",
    "description": null,
    "slot": "neck",
    "price_hay": 1000,
    "is_subscriber_only": false,
    "assets": {
      "rightside": {
        "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck_shell/v1/rightside/upright.png"
      },
      "detail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck_shell/v1/detail.png",
      "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck_shell/v1/thumb.png",
      "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/neck_shell/v1/upright.png"
    },
    "hay_amount": null,
    "price_krw": null,
    "app_store_product_id": null,
    "is_active": true,
    "sort_order": 3,
    "public_id": "neck_shell",
    "asset_version": 1,
    "is_v2_only": false,
    "play_store_product_id": null,
    "name_i18n": {
      "en": "Shell Necklace",
      "ja": "貝のネックレス",
      "ko": "조개 목걸이"
    }
  },
  {
    "id": "00000000-0000-4000-8000-000000000401",
    "product_type": "cosmetic",
    "name": "하와이안 바지",
    "description": null,
    "slot": "body",
    "price_hay": 1000,
    "is_subscriber_only": false,
    "assets": {
      "rightside": {
        "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/clothes_hawaiianpants/v1/rightside/upright.png"
      },
      "detail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/clothes_hawaiianpants/v1/detail.png",
      "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/clothes_hawaiianpants/v1/thumb.png",
      "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/clothes_hawaiianpants/v1/upright.png"
    },
    "hay_amount": null,
    "price_krw": null,
    "app_store_product_id": null,
    "is_active": true,
    "sort_order": 1,
    "public_id": "clothes_hawaiianpants",
    "asset_version": 1,
    "is_v2_only": false,
    "play_store_product_id": null,
    "name_i18n": {
      "en": "Hawaiian Pants",
      "ja": "アロハパンツ",
      "ko": "하와이안 바지"
    }
  },
  {
    "id": "00000000-0000-4000-8000-000000000402",
    "product_type": "cosmetic",
    "name": "우비",
    "description": null,
    "slot": "body",
    "price_hay": 2000,
    "is_subscriber_only": false,
    "assets": {
      "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/body_raincoat/v3/thumb.png",
      "rightside": {
        "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/body_raincoat/v3/rightside/upright.png",
        "timer": {
          "body_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/body_raincoat/v3/rightside/timer/body.png",
          "hand_lowered_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/body_raincoat/v3/rightside/timer/hand-lowered.png",
          "hand_raised_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/body_raincoat/v3/rightside/timer/hand-raised.png"
        }
      }
    },
    "hay_amount": null,
    "price_krw": null,
    "app_store_product_id": null,
    "is_active": true,
    "sort_order": 2,
    "public_id": "body_raincoat",
    "asset_version": 3,
    "is_v2_only": true,
    "play_store_product_id": null,
    "name_i18n": {
      "ko": "우비",
      "en": "Raincoat",
      "ja": "レインコート"
    }
  },
  {
    "id": "00000000-0000-4000-8000-000000000403",
    "product_type": "cosmetic",
    "name": "화가",
    "description": null,
    "slot": "body",
    "price_hay": null,
    "is_subscriber_only": true,
    "assets": {
      "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/body_painter/v3/thumb.png",
      "rightside": {
        "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/body_painter/v3/rightside/upright.png",
        "timer": {
          "body_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/body_painter/v3/rightside/timer/body.png",
          "hand_lowered_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/body_painter/v3/rightside/timer/hand-lowered.png",
          "hand_raised_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/body_painter/v3/rightside/timer/hand-raised.png"
        }
      }
    },
    "hay_amount": null,
    "price_krw": null,
    "app_store_product_id": null,
    "is_active": true,
    "sort_order": 3,
    "public_id": "body_painter",
    "asset_version": 3,
    "is_v2_only": true,
    "play_store_product_id": null,
    "name_i18n": {
      "ko": "화가",
      "en": "Painter",
      "ja": "画家"
    }
  },
  {
    "id": "00000000-0000-4000-8000-000000000404",
    "product_type": "cosmetic",
    "name": "복근",
    "description": null,
    "slot": "body",
    "price_hay": 1000,
    "is_subscriber_only": false,
    "assets": {
      "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/body_abs/v1/thumb.png",
      "rightside": {
        "upright_layer_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/body_abs/v1/rightside/upright.png"
      }
    },
    "hay_amount": null,
    "price_krw": null,
    "app_store_product_id": null,
    "is_active": true,
    "sort_order": 4,
    "public_id": "body_abs",
    "asset_version": 1,
    "is_v2_only": true,
    "play_store_product_id": null,
    "name_i18n": {
      "ko": "복근",
      "en": "Abs",
      "ja": "腹筋"
    }
  },
  {
    "id": "725663d6-8c6e-4f4b-bc08-34c29dfcb7c8",
    "product_type": "hay_pack",
    "name": "건초 3000",
    "description": null,
    "slot": null,
    "price_hay": null,
    "is_subscriber_only": false,
    "assets": null,
    "hay_amount": 3000,
    "price_krw": 10000,
    "app_store_product_id": "com.geniusjun.moly.hay.3000",
    "is_active": true,
    "sort_order": 3,
    "public_id": null,
    "asset_version": null,
    "is_v2_only": false,
    "play_store_product_id": "com.geniusjun.moly.hay.3000",
    "name_i18n": null
  },
  {
    "id": "7b2e299a-a387-4b59-b003-71a4209169e8",
    "product_type": "hay_pack",
    "name": "건초 1500",
    "description": null,
    "slot": null,
    "price_hay": null,
    "is_subscriber_only": false,
    "assets": null,
    "hay_amount": 1500,
    "price_krw": 6500,
    "app_store_product_id": "com.geniusjun.moly.hay.1500",
    "is_active": true,
    "sort_order": 2,
    "public_id": null,
    "asset_version": null,
    "is_v2_only": false,
    "play_store_product_id": "com.geniusjun.moly.hay.1500",
    "name_i18n": null
  },
  {
    "id": "d19d6644-b3aa-453b-988c-d4bc2c97ce63",
    "product_type": "hay_pack",
    "name": "건초 300",
    "description": null,
    "slot": null,
    "price_hay": null,
    "is_subscriber_only": false,
    "assets": null,
    "hay_amount": 300,
    "price_krw": 1500,
    "app_store_product_id": "com.geniusjun.moly.hay.300",
    "is_active": true,
    "sort_order": 1,
    "public_id": null,
    "asset_version": null,
    "is_v2_only": false,
    "play_store_product_id": "com.geniusjun.moly.hay.300",
    "name_i18n": null
  }
]
$catalog$);

-- ai_price_catalog
INSERT INTO public.ai_price_catalog
SELECT * FROM json_populate_recordset(NULL::public.ai_price_catalog, $catalog$
[
  {
    "id": "2c0ccf5f-4d6a-4160-a615-93496a42fac5",
    "catalog_version": 1,
    "provider": "openai",
    "model": "gpt-4.1-mini-2025-04-14",
    "input_micro_usd": 400000,
    "cached_input_micro_usd": 100000,
    "cache_write_micro_usd": null,
    "output_micro_usd": 1600000,
    "embedding_micro_usd": null,
    "source_note": "cache write 미지원 — 추정 적용 금지",
    "effective_from": "2026-08-05 00:00:00+00:00",
    "effective_to": null,
    "created_at": "2026-08-06 21:32:15.156131+00:00"
  },
  {
    "id": "56732ad0-3234-44ab-99f1-15ce6e177364",
    "catalog_version": 1,
    "provider": "openai",
    "model": "text-embedding-3-small",
    "input_micro_usd": null,
    "cached_input_micro_usd": null,
    "cache_write_micro_usd": null,
    "output_micro_usd": null,
    "embedding_micro_usd": 20000,
    "source_note": "embedding 전용",
    "effective_from": "2026-08-05 00:00:00+00:00",
    "effective_to": null,
    "created_at": "2026-08-06 21:32:15.156131+00:00"
  },
  {
    "id": "9f0c8316-dc2f-4795-92ed-96926ed09a78",
    "catalog_version": 1,
    "provider": "openai",
    "model": "gpt-5.6-luna",
    "input_micro_usd": 1000000,
    "cached_input_micro_usd": 100000,
    "cache_write_micro_usd": 1250000,
    "output_micro_usd": 6000000,
    "embedding_micro_usd": null,
    "source_note": "2026-08-05 공개 Standard 가격",
    "effective_from": "2026-08-05 00:00:00+00:00",
    "effective_to": null,
    "created_at": "2026-08-06 21:32:15.156131+00:00"
  },
  {
    "id": "a935e66d-9ec2-4a25-bddc-24e85626728a",
    "catalog_version": 1,
    "provider": "openai",
    "model": "gpt-5.6-terra",
    "input_micro_usd": 2500000,
    "cached_input_micro_usd": 250000,
    "cache_write_micro_usd": 3125000,
    "output_micro_usd": 15000000,
    "embedding_micro_usd": null,
    "source_note": "2026-08-05 공개 Standard 가격",
    "effective_from": "2026-08-05 00:00:00+00:00",
    "effective_to": null,
    "created_at": "2026-08-06 21:32:15.156131+00:00"
  }
]
$catalog$);
-- Preserve the installed mobile bundled IDs; no placeholder remote audio.
INSERT INTO public.bgm_tracks (id, category, title, source, revision, sort_order, is_active, is_subscriber_only) VALUES
('felt-piano-memories', 'lofi', 'Soft Piano', 'bundled', 'bundled-v1', 0, true, false),
('night-rain-on-tokyo', 'lofi', 'Tokyo Nights', 'bundled', 'bundled-v1', 1, true, false),
('midnight-tokyo-rain', 'lofi', 'Midnight Drizzle', 'bundled', 'bundled-v1', 2, true, false),
('midnight-tokyo-rain-2', 'lofi', 'Before Dawn', 'bundled', 'bundled-v1', 3, true, false),
('neon-rain', 'lofi', 'Neon Streets', 'bundled', 'bundled-v1', 4, true, false),
('fading-static', 'lofi', 'Old Radio', 'bundled', 'bundled-v1', 5, true, true)
ON CONFLICT (id) DO NOTHING;


-- Append-only: 기존 단가·과거 원장·사용자 quota는 변경하지 않는다.
-- 배포 전에 적용. 최초 적용 시각부터 효력 발생, 재실행은 같은 버전을 유지한다.
INSERT INTO public.ai_price_catalog
    (catalog_version, provider, model, input_micro_usd, cached_input_micro_usd,
     cache_write_micro_usd, output_micro_usd, source_note, effective_from)
VALUES
    (20260923, 'openai', 'gpt-5.6-luna', 200000, 20000, 250000, 1200000,
     'OpenAI Standard <=272K, verified 2026-09-23; https://developers.openai.com/api/docs/models/gpt-5.6-luna', now()),
    (20260923, 'openai', 'gpt-5.6-terra', 2000000, 200000, 2500000, 12000000,
     'OpenAI Standard <=272K, verified 2026-09-23; https://developers.openai.com/api/docs/models/gpt-5.6-terra', now()),
    (20260923, 'openai', 'gpt-6-luna', 100000, 10000, 125000, 500000,
     'OpenAI Standard <=272K, verified 2026-09-23; https://developers.openai.com/api/docs/models/gpt-6-luna', now()),
    (20260923, 'openai', 'gpt-6-sol', 2000000, 200000, 2500000, 10000000,
     'OpenAI Standard <=272K, verified 2026-09-23; https://developers.openai.com/api/docs/models/gpt-6-sol', now())
ON CONFLICT (catalog_version, provider, model) DO NOTHING;

-- 충돌한 버전이 다른 단가라면 조용히 성공시키지 않는다.
DO $$
BEGIN
    IF (SELECT count(*) FROM public.ai_price_catalog p
        JOIN (VALUES
            ('gpt-5.6-luna', 200000, 20000, 250000, 1200000),
            ('gpt-5.6-terra', 2000000, 200000, 2500000, 12000000),
            ('gpt-6-luna', 100000, 10000, 125000, 500000),
            ('gpt-6-sol', 2000000, 200000, 2500000, 10000000)
        ) AS expected(model, i, r, w, o) ON p.model = expected.model
        WHERE p.catalog_version = 20260923 AND p.provider = 'openai'
          AND (p.input_micro_usd, p.cached_input_micro_usd,
               p.cache_write_micro_usd, p.output_micro_usd) =
              (expected.i, expected.r, expected.w, expected.o)) <> 4 THEN
        RAISE EXCEPTION 'GPT-6 catalog version conflicts with expected Standard rates';
    END IF;
END $$;
-- routine templates
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
-- Explore articles (Korean manuscript; other locales fall back to ko).
INSERT INTO public.articles
SELECT * FROM json_populate_recordset(NULL::public.articles, $articles$
[
  {"id": "sample-1", "sort_order": 0, "is_active": true, "cover": "mug", "read_minutes": 3, "preview_block_count": null,
   "content_i18n": {"ko": {
    "title": "나에게만 유독 엄격한 당신에게",
    "summary": "실수해도 흔들리지 않는 단단한 마음, 자기 자비의 힘",
    "blocks": [
     {"kind": "paragraph", "text": "가장 친한 친구가 실수를 해서 풀죽어 있을 때, 우리는 어떤 말을 건네나요? 아마 \"누구나 그럴 수 있어, 괜찮아\"라며 따뜻하게 위로해줄 거예요.", "highlights": []},
     {"kind": "paragraph", "text": "그런데 똑같은 실수를 내가 했을 때는 어떤가요? 작은 말실수 하나에도 밤새 이불을 차며 \"난 왜 맨날 이 모양이지?\"라며 스스로를 몰아세우곤 해요. 남에게는 한없이 너그러우면서, 왜 나에게만 이렇게 가혹한 심판관이 되는 걸까요?", "highlights": []},
     {"kind": "heading", "text": "자기 자비, 그게 뭐예요?", "highlights": []},
     {"kind": "paragraph", "text": "스스로를 탓하는 대신 따뜻하게 안아주는 태도를 심리학에서는 [자기 자비]라고 불러요. 내가 부족하거나 실패했을 때, 소중한 친구를 대하듯 나를 따뜻하게 이해해 주는 태도예요.", "highlights": ["소중한 친구를 대하듯 나를 따뜻하게 이해해 주는 태도"]},
     {"kind": "paragraph", "text": "마치 넘어져서 무릎이 까진 아이에게 혼을 내는 대신 먼저 약을 발라주는 것처럼, 상처 입은 내 마음을 가장 먼저 보듬어주는 거죠. 다이어트 중에 야식을 먹었거나 중요한 발표에서 실수를 했을 때, 자책에 빠지기보다 내 마음을 먼저 알아차려 주는 거예요.", "highlights": []},
     {"kind": "heading", "text": "나에게 너무 관대하면 나태해지지 않나요?", "highlights": []},
     {"kind": "paragraph", "text": "스스로를 봐주면 발전이 없을 거라고 걱정하는 분들이 많아요. 나를 너그럽게 봐주면 나태해지고 제자리에 머물게 될까 봐 두려워하죠. 하지만 심리학 연구 결과는 정반대였어요.", "highlights": []},
     {"kind": "paragraph", "text": "실패했을 때 스스로를 심하게 자책한 사람들보다, \"그럴 수도 있지\"라며 자신을 다독인 사람들이 자신의 부족한 점을 훨씬 더 객관적으로 돌아보고 다음 기회에 더 적극적으로 도전했어요. 자책은 나를 움직이게 하는 채찍이 아니라, 실패에 대한 두려움을 키워 다음 걸음을 무겁게 만드는 모래주머니에 가까워요.", "highlights": []},
     {"kind": "heading", "text": "자존감이랑은 다른 건가요?", "highlights": []},
     {"kind": "paragraph", "text": "맞아요, 우리가 흔히 말하는 자존감과는 조금 달라요. 자존감은 대개 내가 남들보다 뛰어나거나 무언가를 잘 해냈을 때 올라가요. 하지만 일이 뜻대로 풀리지 않거나 남들과 비교해 뒤처진다고 느끼면 순식간에 바닥으로 떨어지기 쉬워요. 잘할 때만 나를 사랑하는 [조건부 마음]이기 때문이에요.", "highlights": []},
     {"kind": "paragraph", "text": "반면에 자기 자비는 평가나 비교를 하지 않아요. 자존감이 경쟁에서 이겼을 때 받는 트로피 같다면, 자기 자비는 비 오는 날 나를 젖지 않게 감싸주는 우산에 가까워요. 내가 잘하든 못하든, 불완전한 모습 그대로 내 편이 되어 지켜주는 거예요.", "highlights": []},
     {"kind": "heading", "text": "그래서 어떻게 하는 건가요?", "highlights": []},
     {"kind": "paragraph", "text": "평생 나에게 엄격했던 습관을 하루아침에 바꾸기는 어려워요. 아래 3가지를 가볍게 따라 하며 나를 대하는 방식을 조금씩 바꿔보세요.", "highlights": []},
     {"kind": "paragraph", "text": "1️⃣ 친구에게 해주듯 말 바꿔보기\n내가 실수를 했거나 마음에 들지 않는 모습을 발견했을 때, 내가 가장 아끼는 친구가 똑같은 상황이라면 뭐라고 말해줄지 먼저 떠올려보세요. 그리고 그 문장을 나에게 그대로 들려주는 거예요.", "highlights": []},
     {"kind": "callout", "text": "✅ 이렇게 해보세요", "highlights": []},
     {"kind": "bullet", "text": "계획대로 일을 못 끝냈을 때: '오늘 변수가 많았잖아. 내일 다시 해보자.'", "highlights": []},
     {"kind": "bullet", "text": "남들과 비교하며 작아질 때: '사람마다 꽃피는 시기가 다를 뿐이야. 지금도 잘하고 있어.'", "highlights": []},
     {"kind": "bullet", "text": "말실수가 자꾸 생각날 때: '완벽한 사람은 없어. 다음엔 조금 더 신경 쓰면 돼.'", "highlights": []},
     {"kind": "paragraph", "text": "2️⃣ 나만 겪는 일이 아님을 기억하기\n사람은 힘들 때 '왜 나만 이렇게 부족할까'라며 고립감을 느낄 때 가장 괴로워해요. 하지만 실수하고 흔들리는 건 나만의 결함이 아니라, 세상 모든 사람이 겪는 자연스러운 과정이에요.", "highlights": []},
     {"kind": "callout", "text": "✅ 이렇게 해보세요", "highlights": []},
     {"kind": "bullet", "text": "외롭고 불안할 때: '지금 이 순간에도 수많은 사람이 나와 비슷한 고민을 하고 있을 거야'라고 생각하기", "highlights": []},
     {"kind": "bullet", "text": "가슴에 손을 얹고 따뜻한 체온을 느끼며 10초간 숨 고르기", "highlights": []},
     {"kind": "bullet", "text": "좋아하는 에세이나 영화 속 공감 가는 문장 찾아 읽기", "highlights": []},
     {"kind": "paragraph", "text": "3️⃣ 다정한 시선으로 하루 기록하기\n내 마음을 들여다볼 때, 잘잘못을 따지는 심판관이 아니라 따뜻한 제3자의 시선으로 바라보는 연습을 해보세요. 혼자서는 자꾸 자책이 앞선다면, 저녁에 캐피와 도란도란 대화를 나누고 다음 날 아침 캐피가 써준 일기를 읽어보는 것도 좋은 방법이에요.", "highlights": []},
     {"kind": "callout", "text": "✅ 이렇게 해보세요", "highlights": []},
     {"kind": "bullet", "text": "잠들기 전: 오늘 내가 해낸 아주 작은 일 하나 찾아 칭찬 적기", "highlights": []},
     {"kind": "bullet", "text": "속상한 날: 내 감정을 판단하지 않고 있는 그대로 적어 보기", "highlights": []},
     {"kind": "bullet", "text": "BeCappy에서 캐피와 도란도란 대화하며 지친 내 마음 토닥여주기 ⭐️", "highlights": []},
     {"kind": "heading", "text": "불완전한 나도 사랑받을 자격이 있어요", "highlights": []},
     {"kind": "paragraph", "text": "완벽한 사람은 세상에 없어요. 우리는 모두 때때로 실수하고, 길을 헤매고, 후회하면서 살아가요.", "highlights": []},
     {"kind": "paragraph", "text": "자기 자비는 완벽한 내가 되는 법이 아니라, 불완전한 나와 평화롭게 지내는 법을 배우는 과정이에요. 오늘부터는 세상 누구보다 내가 먼저 나의 가장 다정한 편이 되어주기로 해요!", "highlights": ["불완전한 나와 평화롭게 지내는 법"]}
    ]}}},
  {"id": "sample-2", "sort_order": 1, "is_active": true, "cover": "sofa", "read_minutes": 4, "preview_block_count": 1,
   "content_i18n": {"ko": {
    "title": "아직 일어나지 않은 일을 걱정하느라 지칠 때",
    "summary": "꼬리에 꼬리를 무는 불안의 스위치를 끄는 방법",
    "blocks": [
     {"kind": "paragraph", "text": "내일 있을 중요한 일정이나 발표를 앞두고, 침대에 누워 최악의 상황을 끝없이 상상해 본 적 있나요? '만약 실수하면 어떡하지?', '분위기가 싸해지면 어쩌지?' 하며 아직 일어나지도 않은 일 때문에 심장이 두근거리고 식은땀이 나기도 해요. 머릿속으로는 걱정해 봤자 소용없다는 걸 알면서도, 한 번 시작된 불안은 눈덩이처럼 불어나 밤새 나를 괴롭히죠. 도대체 우리 뇌는 왜 이렇게 사서 고생을 하는 걸까요?", "highlights": []},
     {"kind": "heading", "text": "예기 불안, 그게 뭐예요?", "highlights": []},
     {"kind": "paragraph", "text": "앞으로 다가올 일에 대해 미리 겁을 먹고 불안해하는 마음을 심리학에서는 [예기 불안]이라고 불러요. 아직 아무 일도 일어나지 않았는데, 머릿속에서 최악의 시나리오를 미리 써 내려가며 지금 당장 고통받는 상태예요.", "highlights": []},
     {"kind": "paragraph", "text": "예를 들어 상사가 \"잠깐 이야기 좀 할까?\"라고 부르기만 해도, '내가 무슨 실수를 했나? 혼나면 어떡하지?'라며 온갖 걱정을 끌어오는 거예요. 즐거운 여행을 앞두고도 비행기를 놓치거나 짐을 잃어버리는 상상을 하느라 설렘보다 피로감을 먼저 느끼는 것도 예기 불안이에요.", "highlights": []},
     {"kind": "heading", "text": "왜 자꾸 최악의 상황만 상상하게 될까요?", "highlights": []},
     {"kind": "paragraph", "text": "우리가 유독 나쁜 상상을 멈추지 못하는 건 뇌가 가진 [부정성 편향] 때문이에요. 진화심리학자들은 이것이 인류가 살아남기 위해 물려받은 본능이라고 설명해요.", "highlights": []},
     {"kind": "paragraph", "text": "먼 옛날 수풀 속에서 부스럭거리는 소리가 났을 때, '바람이겠지' 하고 넘긴 사람보다 '맹수면 어떡하지?'라고 걱정하며 도망친 사람이 살아남을 확률이 높았어요. 우리 뇌는 행복해지는 것보다 위험을 피하는 것을 훨씬 더 중요하게 여긴다는 뜻이에요.", "highlights": []},
     {"kind": "paragraph", "text": "문제는 지금 우리가 맹수가 없는 안전한 방 안에 누워 있는데도, 뇌의 경보 장치가 여전히 똑같이 작동한다는 거예요. 나를 지키려고 울리는 경보음이 오히려 내 일상을 망치는 소음이 되어버린 셈이죠.", "highlights": []},
     {"kind": "heading", "text": "그럼 걱정을 아예 안 하고 살 수는 없나요?", "highlights": []},
     {"kind": "paragraph", "text": "불안을 완전히 없애려고 애쓰는 게 항상 좋다고 할 수는 없어요. 물론 걱정이 하나도 없으면 마음은 편하겠지만, 적당한 긴장감은 실수를 줄이고 준비를 철저히 하게 도와주는 긍정적인 역할도 하거든요.", "highlights": ["적당한 긴장감은 실수를 줄이고 준비를 철저히 하게 도와주는"]},
     {"kind": "paragraph", "text": "미국 펜실베이니아주립대학교의 연구에 따르면, 우리가 밤새도록 하는 걱정의 91%는 실제로 일어나지 않는다고 해요. 설령 나머지 9%의 일이 일어나더라도 대부분은 생각보다 쉽게 해결됐어요. 그러니 불안이 올라올 때 겁먹지 말고, 걱정을 흘려보내는 요령만 익혀두면 충분해요.", "highlights": ["걱정을 흘려보내는 요령만 익혀두면 충분해요."]},
     {"kind": "heading", "text": "그래서 어떻게 하는 건가요?", "highlights": []},
     {"kind": "paragraph", "text": "꼬리에 꼬리를 무는 걱정을 멈추고 싶다면, 아래 3가지를 가볍게 따라 해보세요.", "highlights": []},
     {"kind": "paragraph", "text": "1️⃣ 걱정에도 전용 시간 정해주기\n불안이 불쑥 찾아올 때마다 그때그때 억누르려고 하면 오히려 하루 종일 그 생각에 사로잡히게 돼요. 차라리 하루 중 딱 15분만 [걱정 시간]으로 정해두고, 그 전까진 걱정을 미뤄두는 거예요!", "highlights": []},
     {"kind": "callout", "text": "✅ 이렇게 해보세요", "highlights": []},
     {"kind": "bullet", "text": "낮에 갑자기 불안할 때: '이 걱정은 오늘 저녁 8시에만 딱 15분 동안 생각하자.'", "highlights": []},
     {"kind": "bullet", "text": "잠자리에서 걱정이 떠오를 때: '지금은 잘 시간이니까 내일 아침 출근길에 고민하자.'", "highlights": []},
     {"kind": "bullet", "text": "정해둔 시간이 지났을 때: 알람이 울리면 자리에서 일어나 물 마시며 생각 끊어내기", "highlights": []},
     {"kind": "paragraph", "text": "2️⃣ 상상과 사실을 분리해서 적어보기\n머릿속에서만 맴도는 불안은 거대한 괴물처럼 보이지만, 글로 적어 눈으로 확인하면 생각보다 별것 아닌 경우가 많아요. 내가 두려워하는 상상이 실제 사실인지 한 걸음 떨어져서 확인해 보세요!", "highlights": []},
     {"kind": "callout", "text": "✅ 이렇게 해보세요", "highlights": []},
     {"kind": "bullet", "text": "막연한 두려움이 들 때: '내가 진짜 걱정하는 최악의 결과는 정확히 무엇이지?' 적어보기", "highlights": []},
     {"kind": "bullet", "text": "그 일이 일어날 확률 따져보기: '그건 실제 사실일까, 아니면 내 불안이 만든 상상일까?'", "highlights": []},
     {"kind": "bullet", "text": "대비책 딱 하나만 적기: '만약 정말 그 일이 일어난다면, 그때 내가 할 수 있는 행동은 무엇일까?'", "highlights": []},
     {"kind": "paragraph", "text": "3️⃣ 다른 시선으로 내 불안 들여다보기\n불안 속에 갇혀 있을 때는 시야가 좁아져서 내 상황을 객관적으로 보기 어려워요. 내 마음속 이야기를 밖으로 꺼내어 제3자의 시선으로 다시 읽어보면, 내가 불필요한 걱정에 에너지를 쓰고 있었다는 걸 쉽게 알아차릴 수 있어요.", "highlights": []},
     {"kind": "callout", "text": "✅ 이렇게 해보세요", "highlights": []},
     {"kind": "bullet", "text": "불안한 밤: 오늘 나를 긴장하게 만든 생각들을 메모장에 솔직하게 털어놓기", "highlights": []},
     {"kind": "bullet", "text": "기록을 다시 읽으며: 친구의 고민을 읽듯 따뜻한 시선으로 바라보기", "highlights": []},
     {"kind": "bullet", "text": "BeCappy에서 캐피가 대신 써준 일기를 읽으며 몰랐던 나를 발견하기! ⭐️", "highlights": []},
     {"kind": "heading", "text": "걱정이 많은 성격은 고칠 수 없을까요?", "highlights": []},
     {"kind": "paragraph", "text": "걱정이 많다는 건 그만큼 삶을 진지하게 대하고, 다가올 일을 잘 해내고 싶은 책임감이 크다는 뜻이기도 해요. 나를 괴롭히던 불안도 자세히 들여다보면 결국 나를 다치지 않게 지키려던 서툰 마음이에요.", "highlights": []},
     {"kind": "paragraph", "text": "오늘 밤에도 또다시 걱정이 고개를 든다면, 불안해하는 나를 탓하는 대신 \"나를 지키려고 또 애쓰고 있구나, 이제는 안심해도 괜찮아\"라고 다정하게 말해주기로 해요!", "highlights": []}
    ]}}},
  {"id": "sample-3", "sort_order": 2, "is_active": true, "cover": "night", "read_minutes": 4, "preview_block_count": 1,
   "content_i18n": {"ko": {
    "title": "눈을 감아도 생각이 멈추지 않는 밤",
    "summary": "잠들려고 애쓸수록 또렷해지는 뇌를 부드럽게 재우는 법",
    "blocks": [
     {"kind": "paragraph", "text": "분명히 몸은 녹초가 되도록 피곤한데, 불을 끄고 자리에 눕기만 하면 머릿속이 바빠지진 않나요? 낮에 있었던 사소한 일부터 내일 해야 할 목록까지 꼬리에 꼬리를 물고 떠오르다 보면 어느새 새벽 두 시가 훌쩍 넘어가곤 해요. 시계를 볼 때마다 '지금 자도 다섯 시간밖에 못 자네, 내일 피곤하면 어떡하지?'라며 초조하게 잠을 재촉하죠. 그런데 이상하게도 빨리 자야겠다고 마음먹을수록 정신은 오히려 더 말똥말똥해져요. 피곤한 몸과 달리, 우리 뇌는 왜 밤마다 홀로 깨어 있는 걸까요?", "highlights": []},
     {"kind": "heading", "text": "과각성 상태, 그게 뭐예요?", "highlights": []},
     {"kind": "paragraph", "text": "몸은 잠들 준비가 되었는데도 뇌의 신경계가 한낮처럼 날카롭게 깨어 있는 현상을 [과각성] 상태라고 불러요. 쉽게 말해 몸의 엔진은 껐는데, 머릿속 시동은 여전히 켜져 있어서 계속 공회전을 하고 있는 거예요.", "highlights": []},
     {"kind": "paragraph", "text": "낮 동안 처리하지 못한 감정이나 스트레스가 많이 쌓인 날일수록 뇌는 조용한 밤을 틈타 밀린 생각들을 한꺼번에 쏟아내요. 침대에 누워 눈을 감는 순간이 그날 처음으로 아무런 방해 없이 혼자만의 생각에 잠길 수 있는 시간이기 때문이에요.", "highlights": []},
     {"kind": "heading", "text": "왜 자려고 애쓸수록 잠이 더 달아날까요?", "highlights": []},
     {"kind": "paragraph", "text": "잠을 자야 한다는 압박감이 오히려 뇌를 깨우는 [교감신경]을 자극하기 때문이에요. 영국 옥스퍼드대학교의 수면 의학 연구진이 그 이유를 밝혀냈어요.", "highlights": ["잠을 자야 한다는 압박감"]},
     {"kind": "paragraph", "text": "잠은 내가 의지를 가지고 노력한다고 해서 해낼 수 있는 일이 아니에요. 그런데 '빨리 자야 해!'라고 스스로를 다그치면, 뇌는 지금을 위기 상황으로 받아들여요. 결국 심장 박동이 빨라지고 몸이 긴장하면서 잠에서 더 멀어지게 되는 거예요.", "highlights": []},
     {"kind": "paragraph", "text": "이렇게 침대에서 뒤척이는 날이 반복되면, 뇌는 침대를 [잠자는 공간]이 아니라 [걱정하며 씨름하는 공간]으로 잘못 학습하게 돼요. 그래서 거실 소파에서는 꾸벅꾸벅 졸다가도 막상 침대에만 누우면 눈이 번쩍 떠지는 일이 벌어지는 거예요.", "highlights": []},
     {"kind": "heading", "text": "그럼 잠이 올 때까지 침대에서 버텨야 하나요?", "highlights": []},
     {"kind": "paragraph", "text": "잠이 안 오는데도 눈을 질끈 감고 침대에 계속 누워 있는 게 항상 좋다고 할 수는 없어요. 물론 몸을 눕혀 두면 어느 정도 휴식은 되겠지만, 잠들지 못하는 시간이 길어질수록 침대에 대한 나쁜 기억만 강해진다는 연구도 있어요.", "highlights": []},
     {"kind": "paragraph", "text": "수면 전문가들은 침대에 누운 지 20분이 지나도 잠이 오지 않는다면, 차라리 자리에서 잠시 일어나는 게 낫다고 조언해요. 잠을 억지로 붙잡으려 애쓰기보다, 먼저 뇌의 긴장을 풀어주는 게 가장 좋아요.", "highlights": []},
     {"kind": "heading", "text": "그래서 어떻게 하는 건가요?", "highlights": []},
     {"kind": "paragraph", "text": "과열된 머릿속을 차분하게 가라앉히고 싶다면, 오늘 밤부터 아래 3가지를 가볍게 따라 해보세요.", "highlights": []},
     {"kind": "paragraph", "text": "1️⃣ 잠들어야 한다는 강박 내려놓기\n잠이 오지 않을 때 가장 먼저 해야 할 일은 시계를 치우고 초조한 마음을 덜어내는 거예요. 남은 수면 시간을 계산하며 불안해하는 대신, 가만히 눈을 감고 누워 있는 것만으로도 충분히 쉬고 있다고 마음을 다독여 주세요!", "highlights": []},
     {"kind": "callout", "text": "✅ 이렇게 해보세요", "highlights": []},
     {"kind": "bullet", "text": "시계를 자꾸 보게 될 때: 스마트폰을 손이 닿지 않는 멀찍한 곳에 뒤집어 두기", "highlights": []},
     {"kind": "bullet", "text": "초조함이 올라올 때: '잠들지 못해도 괜찮아. 눈을 감고 누워 있는 것만으로도 내 몸은 쉬고 있어.'", "highlights": []},
     {"kind": "bullet", "text": "20분 이상 잠이 안 올 때: 잠시 거실로 나와 어두운 조명 아래서 가벼운 책 읽기", "highlights": []},
     {"kind": "paragraph", "text": "2️⃣ 호흡으로 몸의 긴장 스위치 끄기\n머릿속 생각으로 날카로워진 신경을 가라앉히는 가장 빠른 방법은 호흡을 천천히 늘리는 거예요. 숨을 길게 내뱉으면 몸을 이완시키는 [부교감신경]이 작동하면서 뇌에 이제 안심하고 쉬어도 된다는 신호가 전달돼요!", "highlights": []},
     {"kind": "callout", "text": "✅ 이렇게 해보세요", "highlights": []},
     {"kind": "bullet", "text": "코로 4초 동안 조용히 숨을 들이마시기", "highlights": []},
     {"kind": "bullet", "text": "입으로 7초 동안 몸속 공기를 끝까지 천천히 내뱉기", "highlights": []},
     {"kind": "bullet", "text": "어깨와 턱, 미간에 들어간 힘을 툭 풀며 5번 반복하기", "highlights": []},
     {"kind": "paragraph", "text": "3️⃣ 침대에 눕기 전 머릿속 비워내기\n생각이 많아 잠들기 어렵다면, 아예 잠자리에 들기 전에 머릿속 할 일과 감정을 밖으로 꺼내어 정리해 보세요. 미국 베일러대학교의 연구에 따르면, 잠들기 전 머릿속 생각을 글로 적어 내려간 그룹이 그렇지 않은 그룹보다 훨씬 더 빨리 잠들었다고 해요.", "highlights": []},
     {"kind": "callout", "text": "✅ 이렇게 해보세요", "highlights": []},
     {"kind": "bullet", "text": "침대에 들어가기 30분 전: 내일 해야 할 일을 메모지에 미리 적어두고 생각 닫기", "highlights": []},
     {"kind": "bullet", "text": "마음이 복잡할 때: 오늘 나를 신경 쓰이게 한 일들을 한 줄로 짧게 털어놓기", "highlights": []},
     {"kind": "bullet", "text": "BeCappy에서 감정 일기로 오늘의 기분을 남기고, 속마음 적어보기! ⭐️", "highlights": []},
     {"kind": "heading", "text": "매일 8시간씩 못 자면 큰일 나나요?", "highlights": []},
     {"kind": "paragraph", "text": "꼭 8시간을 꽉 채워 자야만 건강한 건 아니에요. 사람마다 필요한 수면의 양은 모두 다르고, 어쩌다 하루 이틀 잠을 설쳤다고 해서 건강이 무너지거나 다음 날 일을 완전히 망치지도 않아요.", "highlights": []},
     {"kind": "paragraph", "text": "잠은 내가 애써서 쟁취해야 하는 숙제가 아니라, 긴장을 내려놓았을 때 자연스럽게 찾아오는 손님과 같아요. 오늘 밤에는 빨리 잠들려고 애쓰는 대신, 고단했던 내 몸과 마음을 편안하게 놓아주기로 해요!", "highlights": ["긴장을 내려놓았을 때 자연스럽게 찾아오는 손님"]}
    ]}}},
  {"id": "sample-4", "sort_order": 3, "is_active": true, "cover": "tasks", "read_minutes": 3, "preview_block_count": 2,
   "content_i18n": {"ko": {
    "title": "작심삼일도 괜찮아요",
    "summary": "의지력 없이도 매일이 단단해지는 작은 루틴의 비밀",
    "blocks": [
     {"kind": "paragraph", "text": "아침 일찍 일어나기, 매일 운동하기, 자기 전 책 읽기. 우리 마음속에는 늘 되고 싶은 나의 모습이 있어요. 하지만 큰맘 먹고 세운 계획이 사흘을 넘기지 못하고 무너질 때면, \"나는 왜 이렇게 의지력이 약할까?\" 하고 쉽게 실망하곤 하죠.", "highlights": []},
     {"kind": "paragraph", "text": "만약 매번 계획만 세우다 지치기를 반복하고 있다면, 그건 여러분의 의지가 부족해서가 아니에요. 그저 습관을 만드는 방식이 조금 거창했을 뿐이에요.", "highlights": []},
     {"kind": "heading", "text": "왜 우리는 자꾸 작심삼일을 반복할까요?", "highlights": []},
     {"kind": "paragraph", "text": "새로운 습관을 들일 때 가장 흔히 하는 실수는 처음부터 완벽하게 해내려는 거예요. 살을 빼려면 식단 전체를 바꿔야 하고, 독서 습관을 들이려면 하루에 한 시간은 읽어야 한다고 스스로를 압박하죠.", "highlights": []},
     {"kind": "paragraph", "text": "여기에 우리 뇌의 본능도 한몫을 해요. 인간은 본능적으로 당장 눈앞에 보이는 즐거움을 좋아하거든요. 운동이나 독서가 주는 보상은 몇 달 뒤에나 찾아오지만, 침대에 누워 스마트폰을 보는 즐거움은 지금 당장 찾아오잖아요. 그러니 의지력만으로 이 싸움에서 이기기는 원래부터 너무나 어려운 일이에요.", "highlights": []},
     {"kind": "heading", "text": "하루 1%만 바뀌어도 정말 효과가 있을까요?", "highlights": []},
     {"kind": "paragraph", "text": "베스트셀러 『아주 작은 습관의 힘』에서는 습관을 복리에 비유해요. 하루에 딱 1%씩 나아지는 건 그날 당장에는 아무런 티도 나지 않아요. 하지만 그 사소한 1%가 1년 동안 매일 쌓이면 무려 37배나 성장한 내가 되어 있어요.", "highlights": []},
     {"kind": "paragraph", "text": "결국 일상을 바꾸는 건 단 한 번의 거창한 결심이 아니라, 매일 반복되는 아주 작고 사소한 행동들이에요. 목표를 높게 세우는 데 에너지를 쓰기보다, 작더라도 매일 기분 좋게 굴러가는 나만의 [루틴 시스템]을 만드는 것이 훨씬 중요해요.", "highlights": ["매일 반복되는 아주 작고 사소한 행동"]},
     {"kind": "heading", "text": "의지력이 약한 사람도 할 수 있나요?", "highlights": []},
     {"kind": "paragraph", "text": "물론이에요! 우리가 흔히 자제력이 대단하다고 생각하는 사람들도 사실은 남들보다 의지력이 특별히 강한 게 아니에요. 그저 의지력을 발휘하지 않아도 자연스럽게 행동할 수 있도록 주변 환경을 설계해 둔 사람들이죠.", "highlights": []},
     {"kind": "paragraph", "text": "스스로를 다그치며 억지로 참는 대신, 내 뇌가 귀찮아하지 않고 기꺼이 움직이도록 달래주는 몇 가지 요령만 알면 누구나 꾸준해질 수 있어요.", "highlights": []},
     {"kind": "heading", "text": "그래서 나만의 루틴, 어떻게 시작하면 될까요?", "highlights": []},
     {"kind": "paragraph", "text": "오늘부터 의지력을 쓰지 않고도 일상에 안정감을 주는 3가지 방법을 따라 해보세요.", "highlights": []},
     {"kind": "paragraph", "text": "1️⃣ 겁먹지 않도록 터무니없이 사소하게 줄이기\n새로운 루틴을 시작할 때는 너무 사소해서 웃음이 날 정도로 문턱을 낮춰야 해요. 절대로 도전하는 기분이 들지 않게, 2분 안에 끝낼 수 있는 크기로 쪼개는 거예요. 시작이 쉬워지면 우리 뇌는 거부감을 느끼지 않고 자연스럽게 움직이게 돼요.", "highlights": []},
     {"kind": "callout", "text": "✅ 이렇게 해보세요", "highlights": []},
     {"kind": "bullet", "text": "독서 루틴: '잠들기 전 책 딱 1페이지만 펼치기'", "highlights": []},
     {"kind": "bullet", "text": "운동 루틴: '퇴근 후 집에 오자마자 운동복으로 갈아입기'", "highlights": []},
     {"kind": "bullet", "text": "아침 루틴: '눈 뜨자마자 미지근한 물 한 모금 마시기'", "highlights": []},
     {"kind": "paragraph", "text": "2️⃣ 원래 하던 행동에 찰떡같이 붙여두기\n아무것도 없는 상태에서 새로운 행동을 시작하려면 힘이 들지만, 이미 매일 무의식적으로 하고 있는 습관 위에 새로운 행동을 살짝 얹으면 훨씬 쉬워져요. 여기에 시간과 장소까지 명확하게 정해주면 잊어버릴 일도 없어요.", "highlights": []},
     {"kind": "callout", "text": "✅ 이렇게 해보세요", "highlights": []},
     {"kind": "bullet", "text": "아침에 양치질을 한 직후에: 세면대 옆에 둔 비타민 챙겨 먹기", "highlights": []},
     {"kind": "bullet", "text": "출근 전 집 불을 다 끈 직후에: 현관 앞에 운동화 꺼내두기", "highlights": []},
     {"kind": "bullet", "text": "저녁 샤워를 마친 직후에: 침대 머리맡에 읽을 책 올려두기", "highlights": []},
     {"kind": "paragraph", "text": "3️⃣ 해낸 나에게 눈에 보이는 보상 바로 주기\n좋은 습관이 오래가려면, 행동이 끝난 직후에 기분 좋은 만족감을 바로 느껴야 해요. 우리 뇌는 보상을 기대할 때 행동할 동기를 얻는 [도파민]이 분비되거든요. 내 루틴이 차곡차곡 쌓이는 걸 눈으로 직접 확인하고 작은 보상을 주는 장치를 만들어보세요.", "highlights": []},
     {"kind": "callout", "text": "✅ 이렇게 해보세요", "highlights": []},
     {"kind": "bullet", "text": "달력이나 다이어리에 오늘 해낸 루틴 동그라미 치기", "highlights": []},
     {"kind": "bullet", "text": "군것질 참은 날엔 아낀 금액만큼 선물 계좌에 바로 저축하기", "highlights": []},
     {"kind": "bullet", "text": "BeCappy에서 가볍게 루틴 완료하고 건초 받기 ⭐️", "highlights": []},
     {"kind": "heading", "text": "매일 완벽하게 해내지 못하면 어떡하죠?", "highlights": []},
     {"kind": "paragraph", "text": "루틴을 지키다 보면 너무 피곤해서 건너뛰는 날도 분명 있을 거예요. 하지만 하루를 놓쳤다고 해서 지금까지 쌓아온 노력까지 사라지는 건 절대 아니에요.", "highlights": []},
     {"kind": "paragraph", "text": "루틴은 나를 가두는 숙제가 아니라, 흔들리는 일상에 편안한 리듬과 안정감을 만들어주는 안전망이에요. 꼭 매일 백 점을 맞지 않아도 괜찮으니, 오늘 내가 해낸 작은 행동 하나를 아낌없이 칭찬해 주세요. 그 다정한 하루하루가 모여 어느새 가장 단단한 나를 만들어줄 거예요!", "highlights": ["오늘 내가 해낸 작은 행동 하나를 아낌없이 칭찬해 주세요."]}
    ]}}},
  {"id": "sample-5", "sort_order": 4, "is_active": true, "cover": "tea", "read_minutes": 5, "preview_block_count": 1,
   "content_i18n": {"ko": {
    "title": "싫다는 말이 입 밖으로 안 나올 때",
    "summary": "나를 잃지 않으면서도 관계가 편안해지는 거절 연습",
    "blocks": [
     {"kind": "paragraph", "text": "내 일도 산더미처럼 밀려 있는데, 동료가 급한 부탁을 해오면 망설이다가 결국 \"네, 제가 할게요\"라고 대답하진 않나요? 웃으며 수락하고선 자리로 돌아와 '아, 바쁘다고 말할걸' 하며 혼자 속앓이를 하곤 해요. 점심 메뉴를 고를 때도, 주말 약속을 잡을 때도 늘 남의 의견에 맞추다 보면 밤마다 허무한 기분이 들죠. 거절 한 번 하는 게 왜 이렇게 무섭고 죄책감이 드는 걸까요? 사실 우리가 거절을 못 하는 건 성격이 무르기 때문만은 아니에요.", "highlights": []},
     {"kind": "heading", "text": "착한 사람 증후군, 그게 뭐예요?", "highlights": []},
     {"kind": "paragraph", "text": "타인의 부탁을 거절하지 못하고 늘 좋은 사람으로만 남으려는 심리를 [착한 사람 증후군]이라고 불러요. 내 감정이나 욕구는 뒤로 미뤄두고, 남들의 기대에 맞추는 데 모든 에너지를 쏟는 상태예요.", "highlights": []},
     {"kind": "paragraph", "text": "어릴 때부터 양보하고 말 잘 듣는 모습을 보일 때만 칭찬받았던 경험이 쌓이면, 무의식중에 '거절하면 나를 싫어할 거야'라는 두려움이 자리 잡게 돼요. 그래서 상대방이 조금만 아쉬운 표정을 지어도 마치 내가 큰 잘못을 저지른 것처럼 불안해지는 거예요.", "highlights": []},
     {"kind": "heading", "text": "왜 거절할 때마다 죄책감이 드는 건가요?", "highlights": []},
     {"kind": "paragraph", "text": "우리가 거절을 어려워하는 가장 큰 이유는 [부탁 거절]을 [상대방 거절]과 똑같이 느끼기 때문이에요. 미국 스탠퍼드대학교의 심리학 연구진이 그 이유를 밝혀냈어요.", "highlights": []},
     {"kind": "paragraph", "text": "부탁을 받는 입장에서 우리는 거절의 말을 꺼내는 순간 관계가 깨지거나 상대가 크게 상처받을 거라고 지레 겁을 먹어요. 하지만 실제 실험 결과는 달랐어요. 부탁을 한 사람은 거절당하더라도 생각보다 빨리 받아들이고, 다른 대안을 찾는 데 집중했거든요. 관계가 틀어질 거라는 공포는 상대방의 마음이 아니라 내 안의 불안이 만들어낸 착각에 가까웠던 셈이에요.", "highlights": []},
     {"kind": "heading", "text": "그럼 앞으로는 무조건 단호하게 거절해야 하나요?", "highlights": []},
     {"kind": "paragraph", "text": "하루아침에 차갑고 매정하게 선을 그을 필요는 없어요. 물론 내 시간을 철저히 지키면 당장은 편할 수 있지만, 평소에 늘 맞춰주던 사람이 갑자기 날 선 태도로 거절하면 오히려 스스로가 더 큰 스트레스를 받게 된다는 연구도 있어요.", "highlights": []},
     {"kind": "paragraph", "text": "거절은 상대를 밀어내는 싸움이 아니라, 내가 감당할 수 있는 범위를 친절하게 알려주는 일이에요. 그러니 처음부터 완벽하게 딱 잘라 말하려고 애쓰지 않아도 괜찮아요. 내 마음이 불편하지 않을 정도의 작은 선 긋기부터 연습해 보는 게 가장 좋아요.", "highlights": ["내가 감당할 수 있는 범위를 친절하게 알려주는 일"]},
     {"kind": "heading", "text": "그래서 어떻게 하는 건가요?", "highlights": []},
     {"kind": "paragraph", "text": "나를 지키면서도 상대방의 기분을 상하게 하지 않는 3가지 거절 연습을 따라 해보세요.", "highlights": []},
     {"kind": "paragraph", "text": "1️⃣ 대답하기 전에 5분만 시간 벌기\n부탁을 받자마자 그 자리에서 바로 대답하려고 하면, 분위기 때문에 얼결에 수락하기 쉬워요. 일단 즉답을 피하고 내 일정을 먼저 확인하겠다고 말해보세요. 잠깐의 틈만 있어도 내 상태를 객관적으로 따져볼 여유가 생겨요!", "highlights": []},
     {"kind": "callout", "text": "✅ 이렇게 해보세요", "highlights": []},
     {"kind": "bullet", "text": "업무 부탁을 받았을 때: \"지금 하던 일 마치고 일정 확인해서 말씀드릴게요.\"", "highlights": []},
     {"kind": "bullet", "text": "갑작스러운 약속 제안에: \"집에 가서 달력 한번 보고 저녁에 연락줄게!\"", "highlights": []},
     {"kind": "bullet", "text": "무리한 요구를 들었을 때: \"그 부분은 제가 바로 결정하기 어려워서 조금 생각해 볼게요.\"", "highlights": []},
     {"kind": "paragraph", "text": "2️⃣ 미안하다는 말 대신 고맙다는 말 먼저 하기\n거절할 때 습관적으로 \"미안해\"라고 먼저 말하면, 마치 내가 잘못을 저지른 사람처럼 위축되기 쉬워요. 대신 나를 떠올려준 것에 대한 고마움을 먼저 표현하고 사정을 설명해 보세요. 훨씬 부드러우면서도 당당하게 거절할 수 있어요!", "highlights": []},
     {"kind": "callout", "text": "✅ 이렇게 해보세요", "highlights": []},
     {"kind": "bullet", "text": "모임에 가기 힘들 때: \"불러줘서 고마워! 그런데 이번 주는 컨디션이 안 좋아서 쉬어야 할 것 같아.\"", "highlights": []},
     {"kind": "bullet", "text": "동료가 도움을 요청할 때: \"믿고 찾아주셔서 감사해요. 하지만 지금은 제 마감 건이 급해서 도와드리기 어려워요.\"", "highlights": []},
     {"kind": "bullet", "text": "대안을 함께 제시할 때: \"지금은 어렵지만, 내일 오후 3시 이후에는 봐드릴 수 있어요.\"", "highlights": []},
     {"kind": "paragraph", "text": "3️⃣ 내가 진짜 원하는 것 기록해 보기\n거절을 잘하려면 평소 내가 무엇을 좋아하고 무엇을 버거워하는지 내 마음의 기준선부터 잘 알아야 해요. 하루를 돌아보며 남의 눈치를 보느라 삼켰던 내 진짜 속마음을 가볍게 적어보세요. 내 감정을 자주 들여다볼수록 타인의 감정에 덜 휘둘리게 된대요.", "highlights": []},
     {"kind": "callout", "text": "✅ 이렇게 해보세요", "highlights": []},
     {"kind": "bullet", "text": "점심 메뉴 고를 때: 습관적인 \"아무거나\" 대신 오늘 내가 먹고 싶은 메뉴 하나 말해보기", "highlights": []},
     {"kind": "bullet", "text": "퇴근길에: 오늘 억지로 웃으며 넘겼던 순간이 있었는지 떠올려 보기", "highlights": []},
     {"kind": "bullet", "text": "BeCappy에서 오늘의 감정을 고르고, 숨겼던 내 마음 기록하기! ⭐️", "highlights": []},
     {"kind": "heading", "text": "거절했다가 나를 나쁘게 보면 어떡하죠?", "highlights": []},
     {"kind": "paragraph", "text": "모든 사람에게 늘 좋은 사람으로 남을 수는 없어요. 내가 내 시간을 지키기 위해 정중하게 거절했는데도 상대가 화를 내거나 나를 멀리한다면, 그건 내가 나빠서가 아니라 상대방이 내 배려를 당연하게 여겼다는 뜻이에요.", "highlights": []},
     {"kind": "paragraph", "text": "거절 연습은 이기적인 사람이 되는 법이 아니라, 내 마음의 방에 누구를 어디까지 초대할지 스스로 결정하는 과정이에요. 오늘부터는 남의 기분을 챙기기 전에, 내 마음이 괜찮은지 먼저 물어봐 주기로 해요!", "highlights": ["내 마음이 괜찮은지 먼저 물어봐 주기로 해요!"]}
    ]}}}
]
$articles$)
ON CONFLICT (id) DO NOTHING;
COMMIT;
