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
COMMIT;
