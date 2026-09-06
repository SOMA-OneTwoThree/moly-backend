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
    "name": "집",
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
            "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v1/background-day.png",
            "z_index": 0
          },
          {
            "id": "player",
            "frame": {
              "x": 255.5,
              "y": 330.1,
              "width": 137.2,
              "height": 128.8
            },
            "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v1/player-day.png",
            "z_index": 10
          },
          {
            "id": "sofa",
            "frame": {
              "x": 3.7,
              "y": 341.4,
              "width": 268.5,
              "height": 129.3
            },
            "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v1/sofa-day.png",
            "z_index": 20
          },
          {
            "id": "table",
            "frame": {
              "x": 38,
              "y": 543,
              "width": 318.3,
              "height": 145.3
            },
            "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v1/table-day.png",
            "z_index": 30
          },
          {
            "id": "clock",
            "frame": {
              "x": 74.2,
              "y": 157.3,
              "width": 63.7,
              "height": 64.6
            },
            "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v1/clock-day.png",
            "z_index": 40
          },
          {
            "id": "window",
            "frame": {
              "x": 200,
              "y": 111.6,
              "width": 151.1,
              "height": 157
            },
            "day_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v1/window-day.png",
            "z_index": 50,
            "night_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v1/window-night.png"
          }
        ],
        "character_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v1/character.png",
        "character_frame": {
          "x": 51,
          "y": 338.8,
          "width": 171,
          "height": 85.2
        }
      },
      "detail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v1/detail.png",
      "thumbnail_url": "https://qkgjlgzsharnilxnkytd.supabase.co/storage/v1/object/public/shop-assets/theme_default/v1/thumb.png"
    },
    "hay_amount": null,
    "price_krw": null,
    "app_store_product_id": null,
    "is_active": true,
    "sort_order": 1,
    "public_id": "theme_default",
    "asset_version": 1,
    "is_v2_only": false,
    "play_store_product_id": null,
    "name_i18n": {
      "en": "Home",
      "ja": "おうち",
      "ko": "집"
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
    "price_hay": 1000,
    "is_subscriber_only": false,
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
    "price_hay": 1000,
    "is_subscriber_only": false,
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
COMMIT;
