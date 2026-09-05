<!-- 자동 생성: db/schema.sql 적용 DB의 information_schema 스냅샷 (2026-07-13, DB_REFACTOR 반영) -->

## Table `profiles`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `nickname` | `text` | Nullable |
| `language` | `text` |  |
| `timezone` | `text` |  |
| `hay_balance` | `int4` |  |
| `trial_ends_at` | `timestamptz` | Nullable |
| `review_prompted_at` | `timestamptz` | Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `products`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `product_type` | `text` |  |
| `name` | `text` |  |
| `description` | `text` | Nullable |
| `slot` | `text` | Nullable |
| `price_hay` | `int4` | Nullable |
| `is_subscriber_only` | `bool` |  |
| `assets` | `jsonb` | Nullable |
| `hay_amount` | `int4` | Nullable |
| `price_krw` | `int4` | Nullable |
| `app_store_product_id` | `text` | Nullable Unique |
| `is_active` | `bool` |  |
| `sort_order` | `int4` |  |

## Table `moly_life_ments`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `content` | `text` |  |
| `weather` | `text` |  |
| `is_active` | `bool` |  |
| `created_at` | `timestamptz` |  |

## Table `app_config`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `key` | `text` | Primary |
| `value` | `jsonb` |  |
| `description` | `text` | Nullable |
| `updated_at` | `timestamptz` |  |

## Table `messages`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `user_id` | `uuid` |  |
| `sender` | `text` |  |
| `kind` | `text` |  |
| `content` | `text` |  |
| `input_tokens` | `int4` | Nullable |
| `output_tokens` | `int4` | Nullable |
| `cache_read_tokens` | `int4` | Nullable |
| `cache_write_tokens` | `int4` | Nullable |
| `billable_tokens` | `int4` | Nullable |
| `activity_date` | `date` |  |
| `created_at` | `timestamptz` |  |

## Table `greetings`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `user_id` | `uuid` |  |
| `context` | `text` |  |
| `content` | `text` |  |
| `activity_date` | `date` |  |
| `committed_message_id` | `int8` | Nullable |
| `created_at` | `timestamptz` |  |

## Table `orders`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `user_id` | `uuid` |  |
| `currency` | `text` |  |
| `status` | `text` |  |
| `total_amount` | `int4` |  |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `order_items`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `order_id` | `uuid` |  |
| `product_id` | `uuid` |  |
| `quantity` | `int4` |  |
| `unit_price` | `int4` |  |

## Table `hay_transactions`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `user_id` | `uuid` |  |
| `type` | `text` |  |
| `amount` | `int4` |  |
| `balance_after` | `int4` |  |
| `order_id` | `uuid` | Nullable |
| `created_at` | `timestamptz` |  |

## Table `user_daily_stats`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `user_id` | `uuid` |  |
| `activity_date` | `date` |  |
| `tokens_used` | `int4` |  |
| `ad_reward_count` | `int2` |  |
| `attendance_claimed_at` | `timestamptz` | Nullable |
| `routine_reward_claimed_at` | `timestamptz` | Nullable |

## Table `subscriptions`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `user_id` | `uuid` |  |
| `plan` | `text` |  |
| `status` | `text` |  |
| `original_transaction_id` | `text` | Unique |
| `latest_transaction_id` | `text` | Nullable |
| `purchased_at` | `timestamptz` | Nullable |
| `expires_at` | `timestamptz` | Nullable |
| `auto_renew_enabled` | `bool` |  |
| `environment` | `text` | Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `subscription_hay_grants`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `user_id` | `uuid` |  |
| `plan` | `text` |  |
| `hay_transaction_id` | `int8` | Nullable |
| `granted_at` | `timestamptz` |  |
| `revoked_at` | `timestamptz` | Nullable |
| `clawback_hay_transaction_id` | `int8` | Nullable |

## Table `payments`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `user_id` | `uuid` |  |
| `order_id` | `uuid` | Nullable |
| `subscription_id` | `uuid` | Nullable |
| `store` | `text` |  |
| `store_transaction_id` | `text` | Unique |
| `amount` | `int4` | Nullable |
| `currency` | `text` |  |
| `status` | `text` |  |
| `paid_at` | `timestamptz` | Nullable |
| `created_at` | `timestamptz` |  |

## Table `user_items`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `user_id` | `uuid` |  |
| `product_id` | `uuid` |  |
| `source` | `text` |  |
| `order_id` | `uuid` | Nullable |
| `equipped_slot` | `text` | Nullable |
| `equipped_at` | `timestamptz` | Nullable |
| `acquired_at` | `timestamptz` |  |

## Table `diaries`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `user_id` | `uuid` |  |
| `diary_date` | `date` |  |
| `source` | `text` |  |
| `preset_ment_id` | `uuid` | Nullable |
| `content` | `text` |  |
| `weather` | `text` |  |
| `published_at` | `timestamptz` | Nullable |
| `first_read_at` | `timestamptz` | Nullable |
| `created_at` | `timestamptz` |  |

## Table `routines`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `user_id` | `uuid` |  |
| `name` | `text` |  |
| `frequency_per_week` | `int2` |  |
| `days_of_week` | `_int2` | Nullable |
| `reminder_enabled` | `bool` |  |
| `reminder_time` | `time` | Nullable |
| `deleted_at` | `timestamptz` | Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `routine_completions`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `routine_id` | `uuid` |  |
| `user_id` | `uuid` |  |
| `activity_date` | `date` |  |
| `completed_at` | `timestamptz` |  |

## Table `user_notification_settings`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `user_id` | `uuid` | Primary |
| `type` | `text` | Primary |
| `enabled` | `bool` |  |

## Table `user_devices`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `user_id` | `uuid` |  |
| `platform` | `text` |  |
| `push_token` | `text` | Unique |
| `last_active_at` | `timestamptz` | Nullable |
| `created_at` | `timestamptz` |  |

## Table `idempotency_keys`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `user_id` | `uuid` | Primary |
| `key` | `text` | Primary |
| `response` | `jsonb` |  |
| `created_at` | `timestamptz` |  |

## Table `reward_ad_sessions`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `session_id` | `uuid` | Primary |
| `user_id` | `uuid` |  |
| `activity_date` | `date` |  |
| `ssv_transaction_id` | `text` | Nullable Unique |
| `granted` | `bool` |  |
| `created_at` | `timestamptz` |  |

## Table `chat_contexts`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `user_id` | `uuid` | Primary |
| `anchor_message_id` | `int8` |  |
| `memory_text` | `text` | Nullable |
| `memory_refreshed_at` | `timestamptz` | Nullable |
| `updated_at` | `timestamptz` |  |

