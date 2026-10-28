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
-- Explore articles (ko·en·ja; other locales fall back to ko).
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
    ]}, "en": {
    "title": "Why Are You So Hard on Yourself?",
    "summary": "How self-compassion keeps you grounded when you make a mistake.",
    "blocks": [
     {"kind": "paragraph", "text": "When your best friend messes up and feels down, what do you tell them? You probably say something warm like, \"Hey, it happens to everyone. You're okay.\"", "highlights": []},
     {"kind": "paragraph", "text": "Now, what happens when you make that exact same mistake? One awkward comment can keep you staring at the ceiling at 2 a.m., asking yourself, \"Why am I always like this?\" We give so much grace to the people around us. Why do we turn into such harsh judges when it comes to ourselves?", "highlights": []},
     {"kind": "heading", "text": "What is self-compassion, anyway?", "highlights": []},
     {"kind": "paragraph", "text": "In psychology, choosing kindness over self-blame is called [self-compassion]. When you fall short or fail, it means treating yourself with the same warmth you'd offer a dear friend.", "highlights": ["treating yourself with the same warmth you'd offer a dear friend"]},
     {"kind": "paragraph", "text": "Think of a kid who trips and scrapes their knee. You don't scold them first—you clean the cut and put a bandage on it. Self-compassion is tending to your bruised feelings before anything else. Whether you broke your diet with a late-night snack or stumbled through a big presentation, you pause to notice your own hurt instead of jumping straight into guilt.", "highlights": []},
     {"kind": "heading", "text": "Won't going easy on myself make me lazy?", "highlights": []},
     {"kind": "paragraph", "text": "A lot of people worry that letting themselves off the hook means they'll stop growing. We're afraid that if we're too gentle, we'll get complacent and stay stuck. Psychological research actually shows the complete opposite.", "highlights": []},
     {"kind": "paragraph", "text": "People who tell themselves \"It happens\" after a setback look at their weaknesses much more honestly than people who beat themselves up. They also try harder the next time around. Self-criticism isn't a whip that drives you forward. It's a heavy sandbag tied to your ankles, making you too afraid of failing to take the next step.", "highlights": []},
     {"kind": "heading", "text": "How is it different from self-esteem?", "highlights": []},
     {"kind": "paragraph", "text": "It's a bit different from the self-esteem we usually talk about. Self-esteem goes up when you do something well or stand out from the crowd. The catch? When things go sideways or you feel behind everyone else, it crashes right to the floor. That's because it's [conditional love]—it only shows up when you're winning.", "highlights": []},
     {"kind": "paragraph", "text": "Self-compassion doesn't grade or compare you. If self-esteem is a trophy you only get when you win, self-compassion is an umbrella that keeps you dry in the rain. Whether you're doing great or falling apart, it stays on your side, flaws and all.", "highlights": []},
     {"kind": "heading", "text": "How do I actually practice it?", "highlights": []},
     {"kind": "paragraph", "text": "You can't undo a lifetime of being hard on yourself overnight. Try these three small shifts to slowly change the way you talk to yourself.", "highlights": []},
     {"kind": "paragraph", "text": "1️⃣ Talk to yourself like a friend\nNext time you mess up or catch a flaw you hate, pause. Ask yourself what you'd say to your closest friend in the exact same spot. Then, say those exact words to yourself.", "highlights": []},
     {"kind": "callout", "text": "✅ Try this", "highlights": []},
     {"kind": "bullet", "text": "When your day goes off track: \"A lot came up today. Let's try again tomorrow.\"", "highlights": []},
     {"kind": "bullet", "text": "When comparing yourself to others makes you feel small: \"Everyone blooms on their own timeline. You're doing just fine.\"", "highlights": []},
     {"kind": "bullet", "text": "When you keep replaying something awkward you said: \"Nobody's perfect. I'll just be a little more mindful next time.\"", "highlights": []},
     {"kind": "paragraph", "text": "2️⃣ Remind yourself you're not alone\nHard moments hurt the most when we feel isolated, wondering, \"Why am I the only one who can't get it together?\" Making mistakes and feeling shaky isn't a personal defect. It's just part of being human.", "highlights": []},
     {"kind": "callout", "text": "✅ Try this", "highlights": []},
     {"kind": "bullet", "text": "When you feel lonely and anxious: Remind yourself, \"Right now, thousands of people are wrestling with this exact same worry.\"", "highlights": []},
     {"kind": "bullet", "text": "Rest a hand over your heart, feel the warmth, and take slow breaths for 10 seconds", "highlights": []},
     {"kind": "bullet", "text": "Read a comforting line from a favorite book or movie that makes you feel understood", "highlights": []},
     {"kind": "paragraph", "text": "3️⃣ Look back on your day through kind eyes\nWhen you check in with yourself, step out of the judge's seat and practice looking at your day like a caring outsider. If self-blame still creeps in when you're alone, try winding down with a quiet chat with Cappy at night, then reading Cappy's Journal the next morning.", "highlights": []},
     {"kind": "callout", "text": "✅ Try this", "highlights": []},
     {"kind": "bullet", "text": "Before bed: Write down one tiny thing you did today and give yourself credit for it", "highlights": []},
     {"kind": "bullet", "text": "On a rough day: Jot down how you feel without judging whether it's right or wrong", "highlights": []},
     {"kind": "bullet", "text": "Have a cozy chat with Cappy in BeCappy to comfort your tired mind ⭐️", "highlights": []},
     {"kind": "heading", "text": "You don't have to be perfect to deserve kindness", "highlights": []},
     {"kind": "paragraph", "text": "There's no such thing as a perfect person. We all stumble, lose our way, and carry regrets.", "highlights": []},
     {"kind": "paragraph", "text": "Self-compassion isn't about fixing yourself until you're flawless. It's about learning how to make peace with your imperfect self. Starting today, be the kindest person in your own corner.", "highlights": ["learning how to make peace with your imperfect self"]}
    ]}, "ja": {
    "title": "自分にばかり厳しくしてしまうあなたへ",
    "summary": "失敗しても折れない心を育てる「セルフ・コンパッション」の力",
    "blocks": [
     {"kind": "paragraph", "text": "大切な友だちが失敗して落ち込んでいるとき、どんな言葉をかけますか？きっと「誰にでもそういうことはあるよ、大丈夫」と温かく声をかけるはずです。", "highlights": []},
     {"kind": "paragraph", "text": "では、同じ失敗を自分がしたときはどうでしょう。ほんの小さな失言ひとつで、夜中に布団の中で「どうして自分はいつもこうなんだろう」とひとり反省会を開いてしまうことはありませんか。周りの人にはあんなに優しいのに、どうして自分にだけは厳しい裁判官になってしまうのでしょうか。", "highlights": []},
     {"kind": "heading", "text": "「セルフ・コンパッション」ってなに？", "highlights": []},
     {"kind": "paragraph", "text": "自分を責める代わりに、温かく受け止めてあげる姿勢を心理学では[セルフ・コンパッション]（自分への慈悲）と呼びます。うまくいかなかったとき、大切な友だちに接するように自分を温かく理解してあげる姿勢のことです。", "highlights": ["大切な友だちに接するように自分を温かく理解してあげる姿勢"]},
     {"kind": "paragraph", "text": "転んで膝をすりむいた子どもを叱る前にまず手当をしてあげるように、傷ついた自分の心をいちばんに労わってあげることです。ダイエット中につい夜食を食べてしまったり、大事な発表で失敗してしまったりしたとき、反省より先にまず自分のショックな気持ちに気づいてあげます。", "highlights": []},
     {"kind": "heading", "text": "自分に甘くしたら、怠けてしまいませんか？", "highlights": []},
     {"kind": "paragraph", "text": "自分を許してしまったら成長できなくなるのでは、と心配する方は少なくありません。自分に優しくすると、甘えてその場に立ち止まってしまう気がするからです。でも、心理学の研究結果はまったく逆でした。", "highlights": []},
     {"kind": "paragraph", "text": "失敗したときに自分を厳しく責めた人よりも、「そういうこともあるよね」と自分をなだめた人のほうが、自分の課題を冷静に振り返り、次のチャンスにも前向きに挑戦できたのです。自己批判は自分を前に進めるムチではなく、失敗への恐怖を膨らませて足取りを重くする「砂袋」のようなものです。", "highlights": []},
     {"kind": "heading", "text": "自己肯定感とは違うもの？", "highlights": []},
     {"kind": "paragraph", "text": "はい、よく耳にする「自己肯定感」とは少し違います。自己肯定感は、人よりうまくできたときや成果が出たときに高まりやすいものです。でも裏を返せば、物事がうまくいかなかったり、周りと比べて落ち込んだりすると、一気に底まで落ちてしまう危うさがあります。うまくいくときだけ自分を好きになれる[条件付きの心]だからです。", "highlights": []},
     {"kind": "paragraph", "text": "一方で、セルフ・コンパッションは評価も比較もしません。自己肯定感が競争に勝ったときにもらえるトロフィーだとしたら、セルフ・コンパッションは雨の日に自分を濡れないように守ってくれる傘のようなものです。うまくやれてもやれなくても、不完全なままの自分の味方でい続けることです。", "highlights": []},
     {"kind": "heading", "text": "どうやって身につければいい？", "highlights": []},
     {"kind": "paragraph", "text": "ずっと自分に厳しくしてきた習慣を、一日で変えるのは難しいものです。まずは次の3つを気軽に試して、自分との向き合い方を少しずつ変えてみませんか。", "highlights": []},
     {"kind": "paragraph", "text": "1️⃣ 友だちに声をかけるように言葉を言い換える\n失敗したときや自分の嫌なところを見つけたときは、いちばん大切な友だちが同じ状況にいたら何と声をかけるか、先に思い浮かべてみてください。そして、その言葉をそのまま自分にかけてあげます。", "highlights": []},
     {"kind": "callout", "text": "✅ こうしてみましょう", "highlights": []},
     {"kind": "bullet", "text": "予定通りに終えられなかったとき：「今日は予想外のことが多かったよね。また明日やろう」", "highlights": []},
     {"kind": "bullet", "text": "周りと比べて落ち込むとき：「花が咲く時期は人それぞれ。今のままでも十分頑張っているよ」", "highlights": []},
     {"kind": "bullet", "text": "うっかり言った一言を引きずるとき：「完璧な人なんていないよ。次から少し気をつければ大丈夫」", "highlights": []},
     {"kind": "paragraph", "text": "2️⃣ 自分だけじゃないと思い出す\n人がつらいとき、いちばん苦しいのは「どうして自分だけこんなにダメなんだろう」と孤立感に陥ることです。でも、失敗したり揺らいだりするのはあなただけの欠陥ではなく、誰もが経験する自然なことです。", "highlights": []},
     {"kind": "callout", "text": "✅ こうしてみましょう", "highlights": []},
     {"kind": "bullet", "text": "寂しさや不安を感じたとき：「今この瞬間も、たくさんの人が同じようなことで悩んでいるはず」と思い出す", "highlights": []},
     {"kind": "bullet", "text": "胸に手を当てて、手のひらの温かさを感じながら10秒間ゆっくり呼吸する", "highlights": []},
     {"kind": "bullet", "text": "好きなエッセイや映画の中から、共感できる言葉を探して読む", "highlights": []},
     {"kind": "paragraph", "text": "3️⃣ 優しい目線で一日を記録する\n自分の心を見つめるときは、良い悪いを裁く裁判官ではなく、温かい第三者の目線で見守る練習をしてみましょう。ひとりだとつい自分を責めてしまうなら、夜にキャピーとゆっくりおしゃべりをして、翌朝キャピーの日記を読んでみるのもおすすめです。", "highlights": []},
     {"kind": "callout", "text": "✅ こうしてみましょう", "highlights": []},
     {"kind": "bullet", "text": "寝る前に：今日できたほんの小さなことをひとつ見つけて、自分を褒める一行を書く", "highlights": []},
     {"kind": "bullet", "text": "落ち込んだ日に：自分の感情をジャッジせず、ありのまま書き出してみる", "highlights": []},
     {"kind": "bullet", "text": "BeCappyでキャピーとゆっくりおしゃべりして、疲れた心をほぐしてあげる ⭐️", "highlights": []},
     {"kind": "heading", "text": "不完全なままでも、愛されていい", "highlights": []},
     {"kind": "paragraph", "text": "完璧な人間なんてどこにもいません。私たちはみんな、ときどき間違えたり、道に迷ったり、後悔したりしながら生きています。", "highlights": []},
     {"kind": "paragraph", "text": "セルフ・コンパッションは、完璧な自分に生まれ変わる方法ではなく、不完全な自分と仲良く暮らす方法を学ぶプロセスです。今日からは、世界中の誰よりもまず自分が、自分の一番やさしい味方になってあげましょう。", "highlights": ["不完全な自分と仲良く暮らす方法"]}
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
     {"kind": "paragraph", "text": "미국 펜실베이니아주립대학교의 연구에 따르면, 만성적인 불안을 겪는 사람들이 한 걱정 중 91%는 실제로 일어나지 않았다고 해요. 설령 걱정하던 일이 일어나더라도 대부분은 예상했던 것보다 훨씬 잘 대처할 수 있었어요. 그러니 불안이 올라올 때 겁먹지 말고, 걱정을 흘려보내는 요령만 익혀두면 충분해요.", "highlights": ["걱정을 흘려보내는 요령만 익혀두면 충분해요."]},
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
    ]}, "en": {
    "title": "Tired of Worrying About Things That Haven't Happened?",
    "summary": "How to flip the switch on spiraling anxious thoughts.",
    "blocks": [
     {"kind": "paragraph", "text": "Have you ever lain in bed before a big day or presentation, playing out every worst-case scenario in your head? \"What if I mess up?\" \"What if the room goes awkwardly silent?\" Nothing has even happened yet, but your heart is racing and your palms are sweating. You know worrying won't help, yet one small thought snowballs until it keeps you up all night. Why do our brains put us through this?", "highlights": []},
     {"kind": "heading", "text": "What is anticipatory anxiety?", "highlights": []},
     {"kind": "paragraph", "text": "In psychology, dreading the future before it arrives is called [anticipatory anxiety]. Nothing bad has happened yet, but your mind writes out the worst possible script and makes you suffer through it right now.", "highlights": []},
     {"kind": "paragraph", "text": "Say your boss messages you, \"Got a quick minute to chat?\" Instantly, your brain spirals: \"Did I mess something up? Am I in trouble?\" Or maybe you have a fun trip coming up, and instead of feeling excited, you exhaust yourself imagining missed flights and lost luggage. That's anticipatory anxiety at work.", "highlights": []},
     {"kind": "heading", "text": "Why do I always jump to the worst-case scenario?", "highlights": []},
     {"kind": "paragraph", "text": "We get stuck on dark scenarios because of our brain's built-in [negativity bias]. Evolutionary psychologists explain that we inherited this instinct simply to survive.", "highlights": []},
     {"kind": "paragraph", "text": "Thousands of years ago, when bushes rustled in the dark, the person who shrugged and thought \"Must be the wind\" didn't always make it. The one who thought \"What if it's a predator?\" and ran survived. Our brains evolved to prioritize dodging danger over feeling happy.", "highlights": []},
     {"kind": "paragraph", "text": "The trouble is, even when we're lying in a safe room with no predators in sight, that alarm still goes off the same way. The siren meant to protect us ends up as noise that wears down our everyday life.", "highlights": []},
     {"kind": "heading", "text": "Can't I just stop worrying altogether?", "highlights": []},
     {"kind": "paragraph", "text": "Trying to erase anxiety completely isn't always the best goal. Life without a single worry sounds peaceful, sure, but a healthy dose of tension helps you prepare and avoid mistakes.", "highlights": ["a healthy dose of tension helps you prepare and avoid mistakes"]},
     {"kind": "paragraph", "text": "In a Penn State University study of people with chronic worry, 91% of the things they worried about never actually came true. Even when the remaining worries did happen, people handled them much better than they expected. You don't need to panic when anxiety shows up—learning the knack for letting worries pass on by is enough.", "highlights": ["learning the knack for letting worries pass on by is enough."]},
     {"kind": "heading", "text": "How do I do that?", "highlights": []},
     {"kind": "paragraph", "text": "When your thoughts start spiraling, try these three simple steps.", "highlights": []},
     {"kind": "paragraph", "text": "1️⃣ Give your worries an appointment\nIf you try to shove anxiety down every time it pops up, it ends up haunting you all day. Instead, set aside a dedicated 15-minute [worry time] each day, and postpone your worries until then!", "highlights": []},
     {"kind": "callout", "text": "✅ Try this", "highlights": []},
     {"kind": "bullet", "text": "When anxiety hits mid-afternoon: \"I'll save this worry for 8 p.m. and give it 15 minutes.\"", "highlights": []},
     {"kind": "bullet", "text": "When worries creep in at bedtime: \"It's time to sleep right now. I'll think about this on my commute tomorrow.\"", "highlights": []},
     {"kind": "bullet", "text": "When your timer goes off: Stand up, get a glass of water, and physically break the train of thought", "highlights": []},
     {"kind": "paragraph", "text": "2️⃣ Separate facts from imagination on paper\nWorries swirling around in your head look like giant monsters. Once you write them down and look at them in plain sight, they're usually much smaller. Take a step back and check whether your fear is a hard fact or just a story.", "highlights": []},
     {"kind": "callout", "text": "✅ Try this", "highlights": []},
     {"kind": "bullet", "text": "When vague dread sets in: Write down, \"What is the exact worst-case outcome I'm afraid of?\"", "highlights": []},
     {"kind": "bullet", "text": "Check the odds: \"Is this a proven fact, or a story my anxiety is making up?\"", "highlights": []},
     {"kind": "bullet", "text": "Write down just one backup plan: \"If this actually happens, what's one thing I can do?\"", "highlights": []},
     {"kind": "paragraph", "text": "3️⃣ Look at your anxiety from the outside\nWhen you're trapped inside anxious thoughts, your tunnel vision makes it hard to see things clearly. Getting those thoughts out of your head and reading them like an outsider helps you spot how much energy you're spending on things you don't need to carry.", "highlights": []},
     {"kind": "callout", "text": "✅ Try this", "highlights": []},
     {"kind": "bullet", "text": "On a restless night: Dump every thought making you tense into a notes app", "highlights": []},
     {"kind": "bullet", "text": "As you read it back: Look at it with the same warmth you'd have reading a friend's worries", "highlights": []},
     {"kind": "bullet", "text": "Read Cappy's Journal in BeCappy to see yourself from a fresh, gentle perspective! ⭐️", "highlights": []},
     {"kind": "heading", "text": "Am I just wired to be a worrier forever?", "highlights": []},
     {"kind": "paragraph", "text": "Worrying a lot also means you care deeply about your life and want to show up well for what's ahead. If you look closely at the anxiety that bothers you, it's really just a clumsy attempt to keep you safe from getting hurt.", "highlights": []},
     {"kind": "paragraph", "text": "If worries pop up again tonight, don't beat yourself up for feeling anxious. Tell yourself gently, \"You're working hard to protect me again. It's okay, we're safe right now.\"", "highlights": []}
    ]}, "ja": {
    "title": "まだ起きてもいないことを心配して疲れてしまうとき",
    "summary": "次々と湧いてくる不安のスイッチをそっと切る方法",
    "blocks": [
     {"kind": "paragraph", "text": "明日の大事な予定や発表を前に、ベッドの中で最悪の展開ばかりを果てしなく想像してしまったことはありませんか？「もし失敗したらどうしよう」「空気が凍りついたらどうしよう」と、まだ起きてもいないことで心臓がドキドキして冷や汗が出てくることもあります。頭では考えても仕方ないとわかっているのに、一度始まった不安は雪だるま式に膨らんで、一晩中自分を苦しめます。私たちの脳は、どうしてこんなふうに苦労を買って出てしまうのでしょうか。", "highlights": []},
     {"kind": "heading", "text": "「予期不安」ってなに？", "highlights": []},
     {"kind": "paragraph", "text": "これから起きる出来事に対して、前もって怖がったり不安になったりする心の働きを心理学では[予期不安]と呼びます。まだ何も起きていないのに、頭の中で最悪のシナリオを先に書き上げて、今この瞬間に苦しんでいる状態です。", "highlights": []},
     {"kind": "paragraph", "text": "たとえば上司に「ちょっといいかな？」と呼ばれただけで、「何かミスをしたかな？怒られたらどうしよう」とあれこれ心配をかき集めてしまうこと。楽しみな旅行の前なのに、飛行機に乗り遅れたり荷物をなくしたりする想像ばかりして、ワクワクするより先にどっと疲れてしまうのも予期不安のひとつです。", "highlights": []},
     {"kind": "heading", "text": "どうして最悪の場面ばかり想像してしまうの？", "highlights": []},
     {"kind": "paragraph", "text": "私たちが悪い想像ばかり止められないのは、脳に備わった[ネガティビティ・バイアス]のせいです。進化心理学者たちは、これは人類が生き延びるために受け継いできた本能だと説明しています。", "highlights": []},
     {"kind": "paragraph", "text": "はるか昔、茂みの中でガサガサと音がしたとき、「ただの風だろう」と聞き流した人よりも、「猛獣だったらどうしよう」と警戒して逃げた人のほうが生き残る確率が高かったのです。私たちの脳は、幸せを感じることより危険を避けることをずっと大事にするようにできています。", "highlights": []},
     {"kind": "paragraph", "text": "困ったことに、猛獣などいない安全な部屋で横になっているときでも、脳の警報装置は昔と同じように鳴ってしまいます。自分を守るための警報が、かえって日常をかき乱すノイズになってしまっているのです。", "highlights": []},
     {"kind": "heading", "text": "心配をゼロにして生きることはできない？", "highlights": []},
     {"kind": "paragraph", "text": "不安を完全に消し去ろうと頑張ることが、いつも正解とは限りません。もちろん心配事がひとつもなければ気楽ですが、適度な緊張感はミスを減らし、事前準備を丁寧にする助けになるというプラスの役割もあるからです。", "highlights": ["適度な緊張感はミスを減らし、事前準備を丁寧にする助けになる"]},
     {"kind": "paragraph", "text": "アメリカのペンシルベニア州立大学の研究によると、慢性的な不安を抱える人たちが心配していたことの91％は、実際には起きなかったそうです。たとえ心配していたことが起きたとしても、その多くは予想していたよりずっとうまく対処できました。だから不安が湧いてきても怖がらずに、心配を受け流すコツさえ身につけておけば十分です。", "highlights": ["心配を受け流すコツさえ身につけておけば十分です。"]},
     {"kind": "heading", "text": "具体的にどうすればいい？", "highlights": []},
     {"kind": "paragraph", "text": "次々と連鎖する心配を止めたいときは、次の3つを気軽に試してみてください。", "highlights": []},
     {"kind": "paragraph", "text": "1️⃣ 心配する「専用の時間」を決める\n不安が顔を出すたびにその場で押さえ込もうとすると、かえって一日中その考えにとらわれてしまいます。いっそのこと一日の中で15分だけ[心配タイム]を決めておき、それまでは心配するのを後回しにしてしまいましょう。", "highlights": []},
     {"kind": "callout", "text": "✅ こうしてみましょう", "highlights": []},
     {"kind": "bullet", "text": "昼間にふと不安になったとき：「この心配は、今日の夜8時から15分間だけ考えよう」", "highlights": []},
     {"kind": "bullet", "text": "布団の中で心配事が浮かんだとき：「今は寝る時間だから、明日の朝の通勤中に悩もう」", "highlights": []},
     {"kind": "bullet", "text": "決めた時間が過ぎたら：アラームが鳴ったら席を立ち、お水を飲んで思考を切り替える", "highlights": []},
     {"kind": "paragraph", "text": "2️⃣ 想像と事実を分けて書き出す\n頭の中だけで渦巻いている不安は巨大な怪物のように見えますが、文字にして目で見てみると、思ったほど大したことではない場合が多いものです。自分が怖がっている想像が本当の事実なのか、一歩引いて確かめてみましょう。", "highlights": []},
     {"kind": "callout", "text": "✅ こうしてみましょう", "highlights": []},
     {"kind": "bullet", "text": "漠然とした不安があるとき：「自分が本当に心配している最悪の結果は何だろう？」と書き出してみる", "highlights": []},
     {"kind": "bullet", "text": "起きる確率を考えてみる：「それは確かな事実？それとも不安が作った想像？」", "highlights": []},
     {"kind": "bullet", "text": "対策をひとつだけ書く：「もし本当にそれが起きたら、そのとき自分にできる行動は何だろう？」", "highlights": []},
     {"kind": "paragraph", "text": "3️⃣ 別の視点から自分の不安を眺める\n不安の中に閉じこもっていると視野が狭くなり、自分の状況を客観的に見づらくなります。心の中のモヤモヤを外に出して第三者の目線で読み返してみると、不要な心配にエネルギーを使っていたことにすっと気づけます。", "highlights": []},
     {"kind": "callout", "text": "✅ こうしてみましょう", "highlights": []},
     {"kind": "bullet", "text": "不安な夜に：今日自分を緊張させた考えを、メモ帳に正直に書き出してみる", "highlights": []},
     {"kind": "bullet", "text": "読み返すときに：友だちの相談を読むような温かい目線で眺めてみる", "highlights": []},
     {"kind": "bullet", "text": "BeCappyでキャピーの日記を読んで、気づかなかった自分の姿を見つけてみる！ ⭐️", "highlights": []},
     {"kind": "heading", "text": "心配性な性格は直らない？", "highlights": []},
     {"kind": "paragraph", "text": "心配事が多いというのは、それだけ毎日と真剣に向き合い、これからのことをちゃんとやり遂げたいという責任感が強い証拠でもあります。自分を苦しめていた不安も、よく見つめてみれば、自分が傷つかないように守ろうとしていた不器用な心なのです。", "highlights": []},
     {"kind": "paragraph", "text": "今夜また心配が頭をもたげてきたら、不安になる自分を責める代わりに、「私を守ろうとしてまた頑張ってくれているんだね、もう安心しても大丈夫だよ」と優しく声をかけてあげましょう。", "highlights": []}
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
     {"kind": "paragraph", "text": "잠을 자야 한다는 압박감이 오히려 뇌를 깨우고 [교감신경]을 자극하기 때문이에요. 수면 심리학 연구에서도 잠들려는 의도적인 노력이 오히려 수면을 방해한다는 점을 밝혀냈어요.", "highlights": ["잠을 자야 한다는 압박감"]},
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
     {"kind": "paragraph", "text": "3️⃣ 침대에 눕기 전 머릿속 비워내기\n생각이 많아 잠들기 어렵다면, 아예 잠자리에 들기 전에 머릿속 할 일과 감정을 밖으로 꺼내어 정리해 보세요. 미국 베일러대학교의 연구에 따르면, 잠들기 전 앞으로 해야 할 일들을 구체적으로 적어 내려간 그룹이 그렇지 않은 그룹보다 훨씬 더 빨리 잠들었다고 해요.", "highlights": []},
     {"kind": "callout", "text": "✅ 이렇게 해보세요", "highlights": []},
     {"kind": "bullet", "text": "침대에 들어가기 30분 전: 내일 해야 할 일을 메모지에 미리 적어두고 생각 닫기", "highlights": []},
     {"kind": "bullet", "text": "마음이 복잡할 때: 오늘 나를 신경 쓰이게 한 일들을 한 줄로 짧게 털어놓기", "highlights": []},
     {"kind": "bullet", "text": "BeCappy에서 감정 기록으로 오늘의 기분을 남기고, 속마음 적어보기! ⭐️", "highlights": []},
     {"kind": "heading", "text": "매일 8시간씩 못 자면 큰일 나나요?", "highlights": []},
     {"kind": "paragraph", "text": "꼭 8시간을 꽉 채워 자야만 건강한 건 아니에요. 사람마다 필요한 수면의 양은 모두 다르고, 어쩌다 하루 이틀 잠을 설쳤다고 해서 건강이 무너지거나 다음 날 일을 완전히 망치지도 않아요.", "highlights": []},
     {"kind": "paragraph", "text": "잠은 내가 애써서 쟁취해야 하는 숙제가 아니라, 긴장을 내려놓았을 때 자연스럽게 찾아오는 손님과 같아요. 오늘 밤에는 빨리 잠들려고 애쓰는 대신, 고단했던 내 몸과 마음을 편안하게 놓아주기로 해요!", "highlights": ["긴장을 내려놓았을 때 자연스럽게 찾아오는 손님"]}
    ]}, "en": {
    "title": "When Your Eyes Are Closed, but Your Mind Won't Stop",
    "summary": "How to gently quiet a brain that gets wider awake the harder you try to sleep.",
    "blocks": [
     {"kind": "paragraph", "text": "Your body is completely wiped out, yet the second you turn off the lights and hit the pillow, your brain kicks into high gear. Sound familiar? One random moment from earlier leads to tomorrow's to-do list, and suddenly it's past 2 a.m. Every time you check the clock, you do the math: \"If I fall asleep right now, I only get five hours. What if I'm exhausted tomorrow?\" Weirdly enough, the harder you try to force yourself to sleep, the more wide awake you feel. When your body is so tired, why is your brain still up on its own?", "highlights": []},
     {"kind": "heading", "text": "What is hyperarousal?", "highlights": []},
     {"kind": "paragraph", "text": "When your body is ready for bed but your nervous system is still wired like it's the middle of the day, sleep specialists call it [hyperarousal]. Simply put, you've parked the car, but the engine in your head is still running and idling in the driveway.", "highlights": []},
     {"kind": "paragraph", "text": "On days packed with stress or unprocessed feelings, your brain waits for the quiet of the night to unload everything at once. Lying in bed with your eyes closed is often the very first moment all day you've been alone with your thoughts without any distractions.", "highlights": []},
     {"kind": "heading", "text": "Why does trying harder chase sleep away?", "highlights": []},
     {"kind": "paragraph", "text": "Because the pressure to fall asleep wakes your brain up and triggers your [sympathetic nervous system]. Sleep psychology research has shown that actively trying to force sleep actually pushes it further away.", "highlights": ["the pressure to fall asleep"]},
     {"kind": "paragraph", "text": "Sleep isn't something you can power through with willpower. When you scold yourself—\"I have to fall asleep right now!\"—your brain reads that urgency as a threat. Your heart rate ticks up, your muscles tense, and sleep slips even further out of reach.", "highlights": []},
     {"kind": "paragraph", "text": "When nights of tossing and turning pile up, your brain accidentally learns that your bed isn't a [place for sleeping], but a [place for worrying and struggling]. That's why you can nod off on the living room couch, then feel wide awake the second you climb into bed.", "highlights": []},
     {"kind": "heading", "text": "Should I just stay in bed and tough it out?", "highlights": []},
     {"kind": "paragraph", "text": "Squeezing your eyes shut and forcing yourself to stay in bed isn't always helpful. Resting your body counts for something, sure, but studies show that the longer you lie awake frustrated, the stronger that negative link with your bed becomes.", "highlights": []},
     {"kind": "paragraph", "text": "Sleep specialists suggest that if you haven't fallen asleep after about 20 minutes, it's better to get up for a bit. Instead of trying to wrestle sleep into submission, focus on taking the tension out of your brain first.", "highlights": []},
     {"kind": "heading", "text": "How do I cool down an overheated mind?", "highlights": []},
     {"kind": "paragraph", "text": "If you want to quiet a racing head tonight, try these three simple shifts.", "highlights": []},
     {"kind": "paragraph", "text": "1️⃣ Drop the pressure to fall asleep\nWhen you can't sleep, step one is hiding the clock and letting yourself off the hook. Stop calculating how many hours you have left. Remind yourself that simply lying still with your eyes closed is already giving your body real rest.", "highlights": []},
     {"kind": "callout", "text": "✅ Try this", "highlights": []},
     {"kind": "bullet", "text": "When you keep checking the time: Put your phone face down across the room out of reach", "highlights": []},
     {"kind": "bullet", "text": "When frustration builds: Tell yourself, \"It's okay if I don't fall asleep right away. Resting with my eyes closed is still rest.\"", "highlights": []},
     {"kind": "bullet", "text": "When you're awake past 20 minutes: Head to the living room and read an easy book under dim light", "highlights": []},
     {"kind": "paragraph", "text": "2️⃣ Use your breath to flip off tension\nThe fastest way to calm a wired nervous system is to slow down your exhale. Breathing out slowly activates your [parasympathetic nervous system], sending a clear signal to your brain that you're safe and ready to rest.", "highlights": []},
     {"kind": "callout", "text": "✅ Try this", "highlights": []},
     {"kind": "bullet", "text": "Inhale quietly through your nose for 4 seconds", "highlights": []},
     {"kind": "bullet", "text": "Exhale slowly through your mouth for 7 seconds until your lungs feel empty", "highlights": []},
     {"kind": "bullet", "text": "Drop your shoulders, unclench your jaw, soften the space between your brows, and repeat 5 times", "highlights": []},
     {"kind": "paragraph", "text": "3️⃣ Empty your head before you hit the pillow\nIf racing thoughts keep you up, get your tasks and feelings out of your head before you climb into bed. In a Baylor University study, people who wrote down a specific to-do list for the next few days before bed fell asleep noticeably faster than those who didn't.", "highlights": []},
     {"kind": "callout", "text": "✅ Try this", "highlights": []},
     {"kind": "bullet", "text": "30 minutes before bed: Jot down tomorrow's to-do list on a notepad and close the tab in your brain", "highlights": []},
     {"kind": "bullet", "text": "When your mind feels cluttered: Write one quick sentence about whatever bothered you today", "highlights": []},
     {"kind": "bullet", "text": "Save how you're feeling in your mood log on BeCappy and let your thoughts out! ⭐️", "highlights": []},
     {"kind": "heading", "text": "Is it a disaster if I don't get 8 hours every night?", "highlights": []},
     {"kind": "paragraph", "text": "You don't need a strict eight hours every single night to be healthy. Everyone's sleep needs are different, and one or two rough nights won't wreck your health or ruin your entire tomorrow.", "highlights": []},
     {"kind": "paragraph", "text": "Sleep isn't a chore you have to conquer. It's a guest that shows up naturally once you let go of the tension. Tonight, instead of trying so hard to fall asleep, just let your tired body and mind rest.", "highlights": ["a guest that shows up naturally once you let go of the tension"]}
    ]}, "ja": {
    "title": "目を閉じても考え事が止まらない夜に",
    "summary": "眠ろうとするほど冴えてしまう脳を、やさしく休ませる方法",
    "blocks": [
     {"kind": "paragraph", "text": "体はクタクタに疲れているはずなのに、電気を消して布団に入ると頭の中が急に忙しくなることはありませんか？昼間のささいな出来事から明日のやるべきことまで次々と思い浮かんで、気づけば夜中の2時を過ぎている。時計を見るたびに「今寝ても5時間しか眠れない、明日しんどかったらどうしよう」と焦って眠りを急かしてしまいます。でも不思議なことに、早く寝なきゃと思えば思うほど、目はますます冴えてしまいますよね。疲れた体とは反対に、どうして脳は夜になるとひとりだけ起きているのでしょうか。", "highlights": []},
     {"kind": "heading", "text": "「過覚醒」ってなに？", "highlights": []},
     {"kind": "paragraph", "text": "体は眠る準備ができているのに、脳の神経が昼間のようにピリピリと冴えている状態を[過覚醒]と呼びます。わかりやすく言うと、車の駐車を終えたのに、頭の中のエンジンだけがかかったままでずっと空ぶかしをしているような状態です。", "highlights": []},
     {"kind": "paragraph", "text": "日中に処理しきれなかった感情やストレスが溜まった日ほど、脳は静かな夜になってから溜め込んだ考えを一気に吐き出します。ベッドに横になって目を閉じた瞬間が、その日初めて誰にも邪魔されずにひとりきりで考え事ができる時間だからです。", "highlights": []},
     {"kind": "heading", "text": "どうして眠ろうと頑張るほど眠れなくなるの？", "highlights": []},
     {"kind": "paragraph", "text": "「寝なければ」というプレッシャーがかえって脳を覚醒させ、[交感神経]を刺激してしまうからです。睡眠心理学の研究でも、眠ろうと意識的に努力することがかえって睡眠を妨げてしまうことがわかっています。", "highlights": ["「寝なければ」というプレッシャー"]},
     {"kind": "paragraph", "text": "眠りは、気合いや努力でどうにかできるものではありません。それなのに「早く寝なきゃ！」と自分を追い立てると、脳は今を危機的な状況だと勘違いしてしまいます。その結果、心拍数が上がって体がこわばり、ますます眠りから遠ざかってしまうのです。", "highlights": []},
     {"kind": "paragraph", "text": "こうして布団の中で悶々とする夜が続くと、脳はベッドを[眠る場所]ではなく[悩みながら格闘する場所]として間違って学習してしまいます。リビングのソファではうとうと居眠りできたのに、いざベッドに入ると目がぱっちり覚めてしまうのはこのためです。", "highlights": []},
     {"kind": "heading", "text": "眠くなるまで布団の中で粘ったほうがいい？", "highlights": []},
     {"kind": "paragraph", "text": "眠れないのに目をぎゅっと閉じて布団の中にい続けるのが、いつも良いとは限りません。横になっているだけでも体はある程度休まりますが、眠れないまま過ごす時間が長引くほど、ベッドに対する嫌な記憶ばかりが強まってしまうという研究もあります。", "highlights": []},
     {"kind": "paragraph", "text": "睡眠の専門家は、布団に入って20分ほど経っても眠気がないなら、いったん布団から出るほうがいいとアドバイスしています。無理に眠りを捕まえようとするより、まずは脳の緊張をほどいてあげるのが一番です。", "highlights": []},
     {"kind": "heading", "text": "具体的にどうすればいい？", "highlights": []},
     {"kind": "paragraph", "text": "熱を持った頭の中を静かに落ち着かせたいなら、今夜から次の3つを気軽に試してみてください。", "highlights": []},
     {"kind": "paragraph", "text": "1️⃣ 「寝なきゃ」という思い込みを手放す\n眠れないときにまずやりたいのは、時計を見えない場所に置いて焦りを手放すことです。残りの睡眠時間を計算して不安になる代わりに、静かに目を閉じて横になっているだけでも十分休めている、と自分の心をなだめてあげましょう。", "highlights": []},
     {"kind": "callout", "text": "✅ こうしてみましょう", "highlights": []},
     {"kind": "bullet", "text": "つい時計を見てしまうとき：スマホを手の届かない少し離れた場所に裏返して置く", "highlights": []},
     {"kind": "bullet", "text": "焦りが湧いてきたとき：「眠れなくても大丈夫。目を閉じて横になっているだけでも体は休まっているよ」と言い聞かせる", "highlights": []},
     {"kind": "bullet", "text": "20分以上眠れないとき：いったんリビングに出て、暗めの照明の下で軽い本を読む", "highlights": []},
     {"kind": "paragraph", "text": "2️⃣ 呼吸で体の緊張スイッチを切る\n考え事でピリピリした神経を落ち着かせる一番の近道は、呼吸をゆっくり長くすることです。息を長く吐き出すと体をリラックスさせる[副交感神経]が働き、脳に「もう安心して休んでいいよ」というサインが伝わります。", "highlights": []},
     {"kind": "callout", "text": "✅ こうしてみましょう", "highlights": []},
     {"kind": "bullet", "text": "鼻から4秒かけて静かに息を吸い込む", "highlights": []},
     {"kind": "bullet", "text": "口から7秒かけて、体の中の空気を最後までゆっくり吐き出す", "highlights": []},
     {"kind": "bullet", "text": "肩やあご、眉間の力をふっと抜きながら5回繰り返す", "highlights": []},
     {"kind": "paragraph", "text": "3️⃣ 布団に入る前に頭の中を空っぽにする\n考え事が多くて寝つきにくいなら、布団に入る前に頭の中のやるべきことや感情を外に出して整理してみましょう。アメリカのベイラー大学の研究によると、寝る前にこれからやるべきことを具体的に書き出したグループは、そうでないグループよりずっと早く眠りにつけたそうです。", "highlights": []},
     {"kind": "callout", "text": "✅ こうしてみましょう", "highlights": []},
     {"kind": "bullet", "text": "布団に入る30分前に：明日やるべきことをメモに書き出して、考えるのを終える", "highlights": []},
     {"kind": "bullet", "text": "心がざわつくとき：今日気になった出来事を一行だけ短く書き出してみる", "highlights": []},
     {"kind": "bullet", "text": "BeCappyの気分の記録で今日の気分を残して、本音を書き出してみる！ ⭐️", "highlights": []},
     {"kind": "heading", "text": "毎日8時間眠れないとダメ？", "highlights": []},
     {"kind": "paragraph", "text": "きっちり8時間眠らなければ健康でいられない、ということはありません。必要な睡眠時間は人それぞれ違いますし、たまたま1日や2日うまく眠れなかったからといって、健康が崩れたり翌日の仕事が台無しになったりすることもありません。", "highlights": []},
     {"kind": "paragraph", "text": "眠りは、必死に頑張って勝ち取る宿題ではなく、緊張を手放したときに自然と訪ねてくるお客さまのようなものです。今夜は早く眠ろうと頑張る代わりに、一日頑張った体と心をゆったりと解放してあげましょう。", "highlights": ["緊張を手放したときに自然と訪ねてくるお客さま"]}
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
     {"kind": "paragraph", "text": "베스트셀러 『아주 작은 습관의 힘』에서는 습관을 복리에 비유해요. 하루에 딱 1%씩 나아지는 건 그날 당장에는 아무런 티도 나지 않아요. 하지만 그 사소한 1%가 1년 동안 매일 쌓이면 산술적으로 약 37배나 성장한다는 비유예요.", "highlights": []},
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
    ]}, "en": {
    "title": "Quitting After Three Days Is Okay",
    "summary": "The secret to building tiny routines that stick—no willpower required.",
    "blocks": [
     {"kind": "paragraph", "text": "Waking up early, working out every day, reading before bed. We all carry a picture in our heads of the person we'd love to become. When an ambitious new plan falls apart after three days, though, it's easy to sigh and think, \"Why do I have zero willpower?\"", "highlights": []},
     {"kind": "paragraph", "text": "If you're stuck in a loop of making big plans and burning out, your willpower isn't the problem. Your starting line was just set a little too high.", "highlights": []},
     {"kind": "heading", "text": "Why do our new habits fizzle out so fast?", "highlights": []},
     {"kind": "paragraph", "text": "The biggest trap when starting a habit is trying to go all-in on day one. We tell ourselves that losing weight means overhauling every single meal, or that building a reading habit means reading for a full hour a night.", "highlights": []},
     {"kind": "paragraph", "text": "Our brain's wiring plays a part here, too. Humans are naturally drawn to instant rewards. The payoff from working out or reading takes months to show up, while scrolling on your phone in bed feels good right this second. Trying to win that tug-of-war on raw willpower alone was never a fair fight.", "highlights": []},
     {"kind": "heading", "text": "Does getting 1% better a day actually do anything?", "highlights": []},
     {"kind": "paragraph", "text": "In the bestseller Atomic Habits, habits are compared to compound interest. Getting just 1% better today barely feels like anything in the moment. Yet as the math metaphor goes, compounding a tiny 1% gain every day for a year leaves you roughly 37 times further ahead than where you started.", "highlights": []},
     {"kind": "paragraph", "text": "What changes your life isn't one dramatic resolution—it's the tiny, unremarkable actions you repeat every day. Instead of pouring energy into massive goals, it matters much more to build your own [routine system] that feels easy to keep rolling.", "highlights": ["the tiny, unremarkable actions you repeat every day"]},
     {"kind": "heading", "text": "Can this work even if I have zero self-control?", "highlights": []},
     {"kind": "paragraph", "text": "Of course! People who seem to have incredible self-discipline don't actually walk around with superhuman willpower. They just set up their surroundings so the right choices happen naturally without a fight.", "highlights": []},
     {"kind": "paragraph", "text": "Instead of forcing yourself through grit, you only need a few simple tricks that nudge your brain into moving without resistance.", "highlights": []},
     {"kind": "heading", "text": "How do I start my own routine?", "highlights": []},
     {"kind": "paragraph", "text": "Try these three ways to bring a steady rhythm to your day without draining your willpower.", "highlights": []},
     {"kind": "paragraph", "text": "1️⃣ Shrink it until it feels ridiculously easy\nWhen you start a new routine, lower the bar until it almost makes you laugh. Chop it down into something that takes under two minutes so it never feels like a chore. When starting is effortless, your brain stops pushing back.", "highlights": []},
     {"kind": "callout", "text": "✅ Try this", "highlights": []},
     {"kind": "bullet", "text": "Reading routine: \"Open my book to just one page before bed\"", "highlights": []},
     {"kind": "bullet", "text": "Workout routine: \"Change into workout clothes the second I get home\"", "highlights": []},
     {"kind": "bullet", "text": "Morning routine: \"Take one sip of water right after waking up\"", "highlights": []},
     {"kind": "paragraph", "text": "2️⃣ Piggyback on something you already do\nStarting a brand-new habit from scratch takes effort, but stacking it onto something you already do on autopilot makes it much easier. Pair it with a clear time and place so you don't even have to remember.", "highlights": []},
     {"kind": "callout", "text": "✅ Try this", "highlights": []},
     {"kind": "bullet", "text": "Right after brushing your teeth in the morning: Take the vitamins sitting next to the sink", "highlights": []},
     {"kind": "bullet", "text": "Right after turning off the lights to leave for work: Set your running shoes by the front door", "highlights": []},
     {"kind": "bullet", "text": "Right after your evening shower: Place your book on your pillow", "highlights": []},
     {"kind": "paragraph", "text": "3️⃣ Give yourself an instant, visible reward\nFor a habit to stick, your brain needs to feel a little spark of satisfaction right after you do it. Expecting a reward releases [dopamine], the chemical that fuels motivation. Give yourself a way to see your progress stack up right in front of your eyes.", "highlights": []},
     {"kind": "callout", "text": "✅ Try this", "highlights": []},
     {"kind": "bullet", "text": "Draw a big circle or checkmark on your calendar for today's routine", "highlights": []},
     {"kind": "bullet", "text": "On days you skip an impulse treat, move those few dollars straight into a fun-money jar", "highlights": []},
     {"kind": "bullet", "text": "Check off your Routines in BeCappy and collect hay ⭐️", "highlights": []},
     {"kind": "heading", "text": "What if I miss a day?", "highlights": []},
     {"kind": "paragraph", "text": "Some days you'll be wiped out and skip your routine—and that's completely normal. Missing one day doesn't erase the days you already showed up.", "highlights": []},
     {"kind": "paragraph", "text": "A routine isn't a prison sentence. It's a safety net that gives your days a calm, steady rhythm when life gets wobbly. You don't need a perfect score every day, so give yourself generous credit for one small thing you did today. Those kind little days add up to a stronger you.", "highlights": ["give yourself generous credit for one small thing you did today."]}
    ]}, "ja": {
    "title": "三日坊主でも大丈夫",
    "summary": "意志の力に頼らず、毎日が少しずつ整う「小さなルーティン」の秘密",
    "blocks": [
     {"kind": "paragraph", "text": "朝早く起きること、毎日運動すること、寝る前に本を読むこと。心の中にはいつも「こんな自分になりたい」という理想がありますよね。でも、意気込んで立てた計画が3日も続かずに崩れてしまうと、「どうして自分はこんなに意志が弱いんだろう」とがっかりしてしまいがちです。", "highlights": []},
     {"kind": "paragraph", "text": "もし毎回計画を立てては疲れてしまうのを繰り返しているなら、それはあなたの意志が足りないからではありません。ただ、習慣の作り方が少しだけ大がかりすぎただけなのです。", "highlights": []},
     {"kind": "heading", "text": "どうして私たちは三日坊主を繰り返してしまうの？", "highlights": []},
     {"kind": "paragraph", "text": "新しい習慣を始めるときに一番よくある失敗は、最初から完璧にやろうとすることです。痩せるなら食事を全部変えなきゃいけない、読書を習慣にするなら毎日1時間は読まなきゃいけない、と自分にプレッシャーをかけてしまいます。", "highlights": []},
     {"kind": "paragraph", "text": "そこへ脳の本能も加わります。人間は本能的に、目の前にあるすぐ手に入る楽しさを優先するようにできています。運動や読書がくれるご褒美は数ヶ月先にならないとやってきませんが、布団の中でスマホを見る楽しさは今すぐ手に入りますよね。だから意志の力だけでこの勝負に勝つのは、そもそも無理があるのです。", "highlights": []},
     {"kind": "heading", "text": "毎日1％変わるだけでも本当に意味がある？", "highlights": []},
     {"kind": "paragraph", "text": "ベストセラー『ジェームズ・クリアー式 複利で伸びる1つの習慣』（原題：Atomic Habits）では、習慣を「複利」にたとえています。1日にたった1％良くなるだけでは、その日すぐには何の変化も見えません。でも、その小さな1％が1年間毎日積み重なると、計算上は約37倍もの成長になるというたとえです。", "highlights": []},
     {"kind": "paragraph", "text": "結局のところ、日常を変えるのは一度きりの大きな決心ではなく、毎日繰り返されるほんの小さな行動です。高い目標を立てることにエネルギーを使うより、小さくても毎日心地よく回る自分だけの[ルーティンの仕組み]を作ることのほうがずっと大切です。", "highlights": ["毎日繰り返されるほんの小さな行動"]},
     {"kind": "heading", "text": "意志が弱い人でもできる？", "highlights": []},
     {"kind": "paragraph", "text": "もちろんです！私たちが「自制心がすごい」と思う人たちも、実は人より特別に意志が強いわけではありません。意志の力を使わなくても自然と体が動くように、周りの環境をうまく整えている人たちなのです。", "highlights": []},
     {"kind": "paragraph", "text": "自分を叱りつけて無理に我慢する代わりに、脳が面倒くさがらずに喜んで動いてくれるちょっとしたコツさえ知っていれば、誰でも続けられるようになります。", "highlights": []},
     {"kind": "heading", "text": "自分だけのルーティン、どうやって始めればいい？", "highlights": []},
     {"kind": "paragraph", "text": "今日から意志の力を使わずに、毎日に安心感を作ってくれる3つの方法を試してみましょう。", "highlights": []},
     {"kind": "paragraph", "text": "1️⃣ 拍子抜けするくらい小さく減らす\n新しいルーティンを始めるときは、簡単すぎて思わず笑ってしまうくらいハードルを下げることが大切です。「よし、やるぞ」と気合いを入れなくてもいいように、2分以内で終わるサイズに細かく分けます。最初の一歩が軽くなれば、脳は抵抗を感じずに自然と動き出します。", "highlights": []},
     {"kind": "callout", "text": "✅ こうしてみましょう", "highlights": []},
     {"kind": "bullet", "text": "読書ルーティン：「寝る前に本を1ページだけ開く」", "highlights": []},
     {"kind": "bullet", "text": "運動ルーティン：「帰宅したらすぐに動きやすい服に着替える」", "highlights": []},
     {"kind": "bullet", "text": "朝のルーティン：「目が覚めたら常温のお水をひと口飲む」", "highlights": []},
     {"kind": "paragraph", "text": "2️⃣ いつもの習慣にぴったりくっつける\nゼロから新しい行動を始めるのは大変ですが、すでに毎日無意識にやっている習慣の上に新しい行動をそっと乗せると、ぐっと簡単になります。さらに時間と場所まで具体的に決めておけば、うっかり忘れることもありません。", "highlights": []},
     {"kind": "callout", "text": "✅ こうしてみましょう", "highlights": []},
     {"kind": "bullet", "text": "朝、歯を磨いた直後に：洗面台の横に置いたビタミンを飲む", "highlights": []},
     {"kind": "bullet", "text": "出かける前に部屋の電気を消した直後に：玄関にスニーカーを出しておく", "highlights": []},
     {"kind": "bullet", "text": "夜、お風呂から上がった直後に：枕元に読む本を置いておく", "highlights": []},
     {"kind": "paragraph", "text": "3️⃣ できた自分に「目に見えるご褒美」をすぐあげる\n良い習慣を長続きさせるには、行動が終わった直後に「やってよかった」という満足感をすぐ味わうことがポイントです。私たちの脳は、ご褒美を期待するときに行動のやる気を生む[ドーパミン]が出る仕組みになっています。ルーティンが積み重なっていくのを目で見て実感できる、小さなご褒美の仕掛けを作ってみましょう。", "highlights": []},
     {"kind": "callout", "text": "✅ こうしてみましょう", "highlights": []},
     {"kind": "bullet", "text": "カレンダーや手帳に、今日できたルーティンの花丸をつける", "highlights": []},
     {"kind": "bullet", "text": "おやつを我慢した日は、浮いた分のお金を自分へのご褒美貯金に入れる", "highlights": []},
     {"kind": "bullet", "text": "BeCappyで気軽にルーティンを完了して、干し草をもらう ⭐️", "highlights": []},
     {"kind": "heading", "text": "毎日完璧にできなかったらどうしよう？", "highlights": []},
     {"kind": "paragraph", "text": "ルーティンを続けていれば、疲れ果ててお休みする日もきっとあります。でも、1日抜けてしまったからといって、これまで積み重ねてきた努力まで消えてしまうわけではありません。", "highlights": []},
     {"kind": "paragraph", "text": "ルーティンは自分を縛りつける宿題ではなく、揺れやすい日常に心地よいリズムと安心感を作ってくれるセーフティネットです。毎日100点を取らなくても大丈夫ですから、今日できた小さな行動ひとつをたっぷり褒めてあげてください。そんな優しい毎日の積み重ねが、いつの間にか一番しなやかで強いあなたを作ってくれますよ。", "highlights": ["今日できた小さな行動ひとつをたっぷり褒めてあげてください。"]}
    ]}}},
  {"id": "sample-5", "sort_order": 4, "is_active": true, "cover": "tea", "read_minutes": 5, "preview_block_count": 1,
   "content_i18n": {"ko": {
    "title": "싫다는 말이 입 밖으로 안 나올 때",
    "summary": "나를 잃지 않으면서도 관계가 편안해지는 거절 연습",
    "blocks": [
     {"kind": "paragraph", "text": "내 일도 산더미처럼 밀려 있는데, 동료가 급한 부탁을 해오면 망설이다가 결국 \"네, 제가 할게요\"라고 대답하진 않나요? 웃으며 수락하고선 자리로 돌아와 '아, 바쁘다고 말할걸' 하며 혼자 속앓이를 하곤 해요. 점심 메뉴를 고를 때도, 주말 약속을 잡을 때도 늘 남의 의견에 맞추다 보면 밤마다 허무한 기분이 들죠. 거절 한 번 하는 게 왜 이렇게 무섭고 죄책감이 드는 걸까요? 사실 우리가 거절을 못 하는 건 성격이 무르기 때문만은 아니에요.", "highlights": []},
     {"kind": "heading", "text": "착한 사람 증후군, 그게 뭐예요?", "highlights": []},
     {"kind": "paragraph", "text": "타인의 부탁을 거절하지 못하고 늘 좋은 사람으로만 남으려는 심리를 흔히 [착한 사람 증후군]이라고 불러요. 내 감정이나 욕구는 뒤로 미뤄두고, 남들의 기대에 맞추는 데 모든 에너지를 쏟는 상태예요.", "highlights": []},
     {"kind": "paragraph", "text": "어릴 때부터 양보하고 말 잘 듣는 모습을 보일 때만 칭찬받았던 경험이 쌓이면, 무의식중에 '거절하면 나를 싫어할 거야'라는 두려움이 자리 잡게 돼요. 그래서 상대방이 조금만 아쉬운 표정을 지어도 마치 내가 큰 잘못을 저지른 것처럼 불안해지는 거예요.", "highlights": []},
     {"kind": "heading", "text": "왜 거절할 때마다 죄책감이 드는 건가요?", "highlights": []},
     {"kind": "paragraph", "text": "우리가 거절을 어려워하는 가장 큰 이유는 [부탁 거절]을 [상대방 거절]과 똑같이 느끼기 때문이에요. 심리학 연구에서도 우리가 거절의 부정적인 결과를 실제보다 과대평가한다는 점을 밝혀냈어요.", "highlights": []},
     {"kind": "paragraph", "text": "부탁을 받는 입장에서 우리는 거절의 말을 꺼내는 순간 관계가 깨지거나 상대가 크게 상처받을 거라고 지레 겁을 먹어요. 하지만 실제 연구 결과는 달랐어요. 부탁을 한 사람은 거절당하더라도 우리가 걱정하는 것만큼 관계를 나쁘게 보지 않았고, 생각보다 담담하게 받아들였거든요. 관계가 틀어질 거라는 공포는 상대방의 마음이 아니라 내 안의 불안이 만들어낸 착각에 가까웠던 셈이에요.", "highlights": []},
     {"kind": "heading", "text": "그럼 앞으로는 무조건 단호하게 거절해야 하나요?", "highlights": []},
     {"kind": "paragraph", "text": "하루아침에 차갑고 매정하게 선을 그을 필요는 없어요. 물론 내 시간을 철저히 지키면 당장은 편할 수 있지만, 평소에 늘 맞춰주던 사람이 갑자기 날 선 태도로 거절하면 오히려 스스로가 더 큰 부담과 스트레스를 느끼기 쉬워요.", "highlights": []},
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
     {"kind": "bullet", "text": "BeCappy에서 감정 기록으로 오늘의 기분을 고르고, 숨겼던 내 마음 기록하기! ⭐️", "highlights": []},
     {"kind": "heading", "text": "거절했다가 나를 나쁘게 보면 어떡하죠?", "highlights": []},
     {"kind": "paragraph", "text": "모든 사람에게 늘 좋은 사람으로 남을 수는 없어요. 내가 내 시간을 지키기 위해 정중하게 거절했는데도 상대가 화를 내거나 나를 멀리한다면, 그건 내가 나빠서가 아니라 상대방이 내 배려를 당연하게 여겼다는 뜻이에요.", "highlights": []},
     {"kind": "paragraph", "text": "거절 연습은 이기적인 사람이 되는 법이 아니라, 내 마음의 방에 누구를 어디까지 초대할지 스스로 결정하는 과정이에요. 오늘부터는 남의 기분을 챙기기 전에, 내 마음이 괜찮은지 먼저 물어봐 주기로 해요!", "highlights": ["내 마음이 괜찮은지 먼저 물어봐 주기로 해요!"]}
    ]}, "en": {
    "title": "When You Just Can't Bring Yourself to Say No",
    "summary": "How to practice setting kind boundaries without losing yourself—or your relationships.",
    "blocks": [
     {"kind": "paragraph", "text": "Your own to-do list is piled sky-high, a coworker asks for an urgent favor, and somehow you still hear yourself say, \"Sure, I can do that.\" You smile, walk back to your desk, and immediately kick yourself: \"Why didn't I just say I'm swamped?\" When you spend all day going along with everyone else's lunch picks and weekend plans, night rolls around and leaves you feeling hollow. Why does saying one little \"no\" feel so scary and guilt-ridden? The truth is, struggling to say no doesn't just mean you're a pushover.", "highlights": []},
     {"kind": "heading", "text": "What is people-pleasing?", "highlights": []},
     {"kind": "paragraph", "text": "That constant pull to say yes and stay on everyone's good side is what we often call [people-pleasing]. You push your own feelings and needs to the back burner and pour all your energy into meeting everyone else's expectations.", "highlights": []},
     {"kind": "paragraph", "text": "If you grew up mostly getting praised for being easygoing and agreeable, a quiet fear settles in: \"If I say no, they won't like me anymore.\" Even a tiny flash of disappointment on someone's face can set off alarms inside you, making you feel like you've done something terrible.", "highlights": []},
     {"kind": "heading", "text": "Why do I feel so guilty every time I say no?", "highlights": []},
     {"kind": "paragraph", "text": "The biggest reason saying no feels so heavy is that we confuse [turning down a request] with [rejecting the person]. Psychological research shows that we consistently overestimate the fallout of saying no.", "highlights": []},
     {"kind": "paragraph", "text": "When someone asks for a favor, we panic that saying no will ruin the relationship or deeply hurt their feelings. Studies tell a very different story, though. People who hear \"no\" don't judge the relationship nearly as harshly as we fear, and they take it in stride much faster than we expect. That dread of ruining everything comes mostly from our own anxiety, not the other person's reaction.", "highlights": []},
     {"kind": "heading", "text": "Should I just start being blunt and firm?", "highlights": []},
     {"kind": "paragraph", "text": "You don't need to turn cold and draw hard lines overnight. Guarding your time fiercely might sound relieving on paper, but if you're used to accommodating everyone, snapping into a sharp \"no\" usually just leaves you feeling even more stressed and unsettled.", "highlights": []},
     {"kind": "paragraph", "text": "Saying no isn't a fight to push people away—it's kindly letting them know what you have the capacity to carry. You don't have to deliver a textbook-perfect refusal right out of the gate. Start by drawing small boundaries that feel manageable to you.", "highlights": ["kindly letting them know what you have the capacity to carry"]},
     {"kind": "heading", "text": "How do I practice saying no?", "highlights": []},
     {"kind": "paragraph", "text": "Try these three ways to protect your time and energy without leaving hard feelings.", "highlights": []},
     {"kind": "paragraph", "text": "1️⃣ Buy yourself five minutes before answering\nWhen you try to answer on the spot, the pressure of the moment makes an automatic \"yes\" slip right out. Skip the instant answer and tell them you need to check your schedule first. Even a tiny pause gives you the breathing room to check in with how much you can actually handle.", "highlights": []},
     {"kind": "callout", "text": "✅ Try this", "highlights": []},
     {"kind": "bullet", "text": "When asked for a favor at work: \"Let me wrap up what I'm working on, check my schedule, and get back to you.\"", "highlights": []},
     {"kind": "bullet", "text": "When a last-minute plan pops up: \"Let me check my calendar when I get home and text you tonight!\"", "highlights": []},
     {"kind": "bullet", "text": "When an ask feels like too much: \"I can't commit to that on the spot—let me think it over a bit.\"", "highlights": []},
     {"kind": "paragraph", "text": "2️⃣ Lead with \"thank you\" instead of \"I'm sorry\"\nStarting every refusal with a knee-jerk \"I'm so sorry\" makes you shrink, as if you've done something wrong. Instead, thank them for thinking of you first, then share what's on your plate. It keeps your answer warm while standing your ground.", "highlights": []},
     {"kind": "callout", "text": "✅ Try this", "highlights": []},
     {"kind": "bullet", "text": "When you need to skip a hangout: \"Thanks so much for inviting me! I'm running on empty this week, though, so I need to stay in and rest.\"", "highlights": []},
     {"kind": "bullet", "text": "When a coworker asks for help: \"Thanks for thinking of me. I'm heads-down on a tight deadline right now, so I won't be able to jump in.\"", "highlights": []},
     {"kind": "bullet", "text": "When offering a middle ground: \"I can't take this on today, but I can take a look tomorrow after 3 p.m.\"", "highlights": []},
     {"kind": "paragraph", "text": "3️⃣ Keep track of what you actually want\nTo get comfortable saying no, you first need to know your own baseline—what you genuinely enjoy and what drains you. Look back on your day and jot down the real thoughts you swallowed just to keep the peace. The more often you check in with your own feelings, the less you get swept up in everyone else's.", "highlights": []},
     {"kind": "callout", "text": "✅ Try this", "highlights": []},
     {"kind": "bullet", "text": "When picking lunch: Instead of an automatic \"I'm good with whatever,\" name one thing you're actually craving", "highlights": []},
     {"kind": "bullet", "text": "On your way home: Notice if there was a moment today where you forced a smile and bit your tongue", "highlights": []},
     {"kind": "bullet", "text": "Pick today's mood in your mood log on BeCappy and write down what you held back! ⭐️", "highlights": []},
     {"kind": "heading", "text": "What if they think badly of me for saying no?", "highlights": []},
     {"kind": "paragraph", "text": "You can't be the easy, agreeable person for everyone all the time. If you decline politely to protect your bandwidth and someone gets angry or pulls away, that doesn't mean you did something wrong—it means they were taking your kindness for granted.", "highlights": []},
     {"kind": "paragraph", "text": "Practicing saying no isn't about becoming selfish. It's about deciding for yourself who gets invited into your mental space, and how far. Starting today, before you rush to take care of everyone else's feelings, pause and ask if your own heart is okay first!", "highlights": ["pause and ask if your own heart is okay first!"]}
    ]}, "ja": {
    "title": "「NO」がどうしても口に出せないとき",
    "summary": "自分をすり減らさずに、心地よい人間関係を保つ「断る練習」",
    "blocks": [
     {"kind": "paragraph", "text": "自分の仕事も山積みになっているのに、同僚から急ぎの頼み事をされると、迷った末に「わかりました、やっておきますね」と引き受けてしまっていませんか？笑顔でうなずいて席に戻ってから、「ああ、忙しいって言えばよかった」とひとりでため息をつく。ランチのお店を決めるときも、週末の予定を合わせるときも、いつも周りの意見にばかり合わせていると、夜になるとなんだか虚しい気持ちになりますよね。たった一度断ることが、どうしてこんなに怖くて罪悪感があるのでしょうか。実は、断れないのはあなたの性格が弱いからだけではありません。", "highlights": []},
     {"kind": "heading", "text": "「いい人症候群」ってなに？", "highlights": []},
     {"kind": "paragraph", "text": "人からの頼みを断れず、いつも「いい人」でいようとしてしまう心理はよく[いい人症候群]と呼ばれます。自分の感情ややりたいことは後回しにして、周りの期待に応えることにすべてのエネルギーを注いでしまっている状態です。", "highlights": []},
     {"kind": "paragraph", "text": "子どもの頃から、譲ったり聞き分けよくしたりしたときばかり褒められた経験が重なると、無意識のうちに「断ったら嫌われてしまう」という恐れが根づいてしまいます。だから相手がほんの少し残念そうな顔をしただけでも、まるで自分が大きな過ちを犯したかのように不安になってしまうのです。", "highlights": []},
     {"kind": "heading", "text": "どうして断るたびに罪悪感が湧くの？", "highlights": []},
     {"kind": "paragraph", "text": "私たちが断るのを難しく感じる一番の理由は、[頼みを断ること]を[相手そのものを拒絶すること]と同じように感じてしまうからです。心理学の研究でも、人は断ったときの悪影響を実際より大きく見積もりすぎてしまうことがわかっています。", "highlights": []},
     {"kind": "paragraph", "text": "頼まれた側の私たちは、断りの言葉を口にした瞬間に「関係が壊れるのではないか」「相手が深く傷つくのではないか」と先回りして怯えてしまいます。でも、実際の研究結果は違いました。頼んだ側の人は、たとえ断られたとしても、私たちが心配するほど関係を悪く捉えてはおらず、思った以上にすんなり受け止めていたのです。関係がこじれるかもしれないという恐怖は、相手の本心ではなく、自分の中の不安が作り出した錯覚に近いものだったと言えます。", "highlights": []},
     {"kind": "heading", "text": "これからはきっぱり断るようにしたほうがいい？", "highlights": []},
     {"kind": "paragraph", "text": "今日から急に冷たく突き放すような線を引く必要はありません。もちろん自分の時間を徹底して守ればその場は楽かもしれませんが、普段ずっと周りに合わせてきた人が急に強い態度で断ると、かえって自分自身が強い負担やストレスを感じやすくなります。", "highlights": []},
     {"kind": "paragraph", "text": "断ることは相手を突き放す戦いではなく、自分が無理なく引き受けられる範囲を丁寧に伝えることです。だから最初から完璧にきっぱり言おうと頑張らなくても大丈夫。自分の心が苦しくならないくらいの、小さな線引きから練習していくのが一番です。", "highlights": ["自分が無理なく引き受けられる範囲を丁寧に伝えること"]},
     {"kind": "heading", "text": "具体的にどうすればいい？", "highlights": []},
     {"kind": "paragraph", "text": "自分を守りながら、相手の気持ちも傷つけない3つの「断る練習」を試してみてください。", "highlights": []},
     {"kind": "paragraph", "text": "1️⃣ 返事をする前に5分だけ時間をもらう\n頼まれたその場ですぐに答えようとすると、その場の空気に流されてつい引き受けてしまいがちです。まずは即答を避けて、予定を確認してから伝えるとひと声かけてみましょう。ほんの少しの間があるだけで、自分の余裕を冷静に確かめるゆとりが生まれます。", "highlights": []},
     {"kind": "callout", "text": "✅ こうしてみましょう", "highlights": []},
     {"kind": "bullet", "text": "仕事を頼まれたとき：「今手をつけている作業を終えたら、スケジュールを確認してお返事しますね」", "highlights": []},
     {"kind": "bullet", "text": "急なお誘いを受けたとき：「家に帰ってカレンダーを見てから、夜に連絡するね！」", "highlights": []},
     {"kind": "bullet", "text": "無理なお願いをされたとき：「この場ですぐには決められないので、少し考えさせてください」", "highlights": []},
     {"kind": "paragraph", "text": "2️⃣ 「ごめんなさい」の前に「ありがとう」を伝える\n断るときに口癖のように「ごめんね」から切り出すと、まるで自分が悪いことをしたかのように気持ちが縮こまってしまいます。代わりに、自分に声をかけてくれたことへの感謝を先に伝えてから事情を話してみましょう。ずっと柔らかく、それでいて堂々と断ることができますよ。", "highlights": []},
     {"kind": "callout", "text": "✅ こうしてみましょう", "highlights": []},
     {"kind": "bullet", "text": "集まりに行きづらいとき：「誘ってくれてありがとう！でも今週は少し体調が万全じゃなくて、ゆっくり休もうと思うんだ」", "highlights": []},
     {"kind": "bullet", "text": "同僚に手伝いを頼まれたとき：「頼りにしてくださってありがとうございます。ただ、今は自分の締め切りが立て込んでいてお手伝いするのが難しいです」", "highlights": []},
     {"kind": "bullet", "text": "代わりの案を添えるとき：「今は難しいのですが、明日の午後3時以降なら確認できますよ」", "highlights": []},
     {"kind": "paragraph", "text": "3️⃣ 自分が本当に望んでいることを記録してみる\n上手に断れるようになるには、普段自分が何を心地よいと感じ、何を負担に感じるのか、自分の心の基準線をよく知っておくことが大切です。一日を振り返りながら、周りに気を使って飲み込んだ本当の本音を軽く書き留めてみましょう。自分の感情をこまめに見つめるほど、他人の感情に振り回されにくくなります。", "highlights": []},
     {"kind": "callout", "text": "✅ こうしてみましょう", "highlights": []},
     {"kind": "bullet", "text": "ランチを選ぶとき：口癖の「なんでもいいよ」の代わりに、今日自分が食べたいものをひとつ言ってみる", "highlights": []},
     {"kind": "bullet", "text": "帰り道に：今日、無理に笑ってやり過ごした瞬間がなかったか思い返してみる", "highlights": []},
     {"kind": "bullet", "text": "BeCappyの気分の記録で今日の気分を選んで、しまい込んだ本音を記録してみる！ ⭐️", "highlights": []},
     {"kind": "heading", "text": "断って悪く思われたらどうしよう？", "highlights": []},
     {"kind": "paragraph", "text": "すべての人にとって、いつも「都合のいい優しい人」でい続けることはできません。自分の時間を守るために丁寧に断ったのに、相手が怒ったり離れていったりしたなら、それはあなたが悪いのではなく、相手があなたの優しさに甘えて当たり前だと思っていたということです。", "highlights": []},
     {"kind": "paragraph", "text": "断る練習は、わがままな人になる方法ではなく、自分の心の部屋に誰をどこまで招き入れるかを自分で決めるプロセスです。今日からは周りの機嫌を気にする前に、まず自分の心が大丈夫かどうかを一番に聞いてあげましょう！", "highlights": ["まず自分の心が大丈夫かどうかを一番に聞いてあげましょう！"]}
    ]}}}
]
$articles$)
ON CONFLICT (id) DO NOTHING;
COMMIT;
