# Переменные окружения

<!-- Файл собран из кода: python tools/env_reference.py. Руками не править. -->

Бот читает 261 переменных. Почти все — точная настройка со значением
по умолчанию: задавать их нужно, только если дефолт не подошёл.

## Без чего бот не работает как задумано

- `BOT_TOKEN` — без него бот не запустится
- `ADMIN_ID` — кому принадлежат кнопки и отчёты
- `CHANNEL_ID` — куда публиковать
- `DATA_DIR` — где хранить состояние; без постоянного тома оно теряется при каждом перезапуске

## Все переменные

### Адаптивное расписание

| Переменная | По умолчанию | Где читается |
| --- | --- | --- |
| `ADAPTIVE_AUTO_FORMAT` | `False` | anime_news_bot.py |
| `ADAPTIVE_AUTO_INTERVAL` | `False` | anime_news_bot.py |
| `ADAPTIVE_DIVERSITY_MAX_MULTIPLIER` | `1.75` | anime_news_bot.py |
| `ADAPTIVE_DIVERSITY_MIN_STORIES` | `8` | anime_news_bot.py |
| `ADAPTIVE_DIVERSITY_TARGET_SHARE` | `0.35` | anime_news_bot.py |
| `ADAPTIVE_DIVERSITY_WINDOW_HOURS` | `24` | anime_news_bot.py |
| `ADAPTIVE_EVAL_MINUTES` | `60` | anime_news_bot.py |
| `ADAPTIVE_FORMAT_MARGIN` | `0.08` | anime_news_bot.py |
| `ADAPTIVE_FORMAT_MAX_PERCENT` | `50.0` | anime_news_bot.py |
| `ADAPTIVE_FORMAT_MIN_OUTCOMES` | `20` | anime_news_bot.py |
| `ADAPTIVE_FORMAT_STEP_PERCENT` | `10.0` | anime_news_bot.py |
| `ADAPTIVE_HOUR_MIN_SAMPLES` | `4` | anime_news_bot.py |
| `ADAPTIVE_INTERVAL_MAX` | `90` | anime_news_bot.py |
| `ADAPTIVE_INTERVAL_MIN` | `10` | anime_news_bot.py |
| `ADAPTIVE_INTERVAL_STEP` | `5` | anime_news_bot.py |

### Аналитика

| Переменная | По умолчанию | Где читается |
| --- | --- | --- |
| `ANALYTICS_DEFAULT_DAYS` | `30` | anime_news_bot.py |
| `ANALYTICS_MAX_EVENTS` | `6000` | anime_news_bot.py |
| `ANALYTICS_MIN_SAMPLES` | `8` | anime_news_bot.py |
| `ANALYTICS_RECOMMEND_MARGIN` | `0.1` | anime_news_bot.py |

### Защита от перегрузки

| Переменная | По умолчанию | Где читается |
| --- | --- | --- |
| `BACKPRESSURE_HARD_NEW` | `2` | anime_news_bot.py |
| `BACKPRESSURE_HARD_QUEUE` | — | anime_news_bot.py |
| `BACKPRESSURE_SOFT_NEW` | `6` | anime_news_bot.py |
| `BACKPRESSURE_SOFT_QUEUE` | — | anime_news_bot.py |
| `BACKPRESSURE_THREAD_MAX_PER_CYCLE` | `20` | anime_news_bot.py |

### Админ-дашборд

| Переменная | По умолчанию | Где читается |
| --- | --- | --- |
| `DASHBOARD_FAIL_DELAY_SEC` | `0.25` | anime_news_bot.py |
| `DASHBOARD_FAIL_LIMIT` | `10` | anime_news_bot.py |
| `DASHBOARD_FAIL_WINDOW_SEC` | `300` | anime_news_bot.py |
| `DASHBOARD_REFRESH_SEC` | `30` | anime_news_bot.py |
| `DASHBOARD_TOKEN` | — | anime_news_bot.py |
| `DASHBOARD_USER` | — | anime_news_bot.py |

### Журнал событий

| Переменная | По умолчанию | Где читается |
| --- | --- | --- |
| `EVENT_LOG_BACKUP_COUNT` | `3` | anime_news_bot.py |
| `EVENT_LOG_MAX_MB` | `8` | anime_news_bot.py |
| `EVENT_LOOP_LAG_INTERVAL_SEC` | `1.0` | anime_news_bot.py |
| `EVENT_LOOP_LAG_WARN_MS` | `1000` | anime_news_bot.py |

### Переключатели возможностей

| Переменная | По умолчанию | Где читается |
| --- | --- | --- |
| `FEATURE_ACTIVE_VERIFICATION` | `True` | anime_news_bot.py |
| `FEATURE_ADAPTIVE_PUBLISHING` | `True` | anime_news_bot.py |
| `FEATURE_ADAPTIVE_RETRY` | `True` | anime_news_bot.py |
| `FEATURE_ADMIN_AUDIT` | `True` | anime_news_bot.py |
| `FEATURE_ADMIN_DASHBOARD` | `True` | anime_news_bot.py |
| `FEATURE_ANALYTICS_FEEDBACK` | `True` | anime_news_bot.py |
| `FEATURE_BACKPRESSURE` | `True` | anime_news_bot.py |
| `FEATURE_BACKUP_VERIFY` | `True` | anime_news_bot.py |
| `FEATURE_BREAKING_NEWS` | `True` | anime_news_bot.py |
| `FEATURE_CANARY_PUBLISH` | `True` | anime_news_bot.py |
| `FEATURE_CHAT_MODERATION` | `False` | anime_news_bot.py |
| `FEATURE_CIRCUIT_BREAKERS` | `True` | anime_news_bot.py |
| `FEATURE_CONFIDENCE_MODERATION` | `True` | anime_news_bot.py |
| `FEATURE_CONFIDENCE_SCORING` | `True` | anime_news_bot.py |
| `FEATURE_CONFIG_RELOAD` | `True` | anime_news_bot.py |
| `FEATURE_DIVERSITY_SCHEDULER` | `True` | anime_news_bot.py |
| `FEATURE_DOCTOR` | `True` | anime_news_bot.py |
| `FEATURE_EDITORIAL_GLOSSARY` | `True` | anime_news_bot.py |
| `FEATURE_EDITORIAL_LEARNING` | `True` | anime_news_bot.py |
| `FEATURE_EDITORIAL_RULES` | `True` | anime_news_bot.py |
| `FEATURE_ENTITY_MEMORY` | `True` | anime_news_bot.py |
| `FEATURE_ERROR_FINGERPRINTING` | `True` | anime_news_bot.py |
| `FEATURE_EXPERIMENTS` | `True` | anime_news_bot.py |
| `FEATURE_GOLDEN_DATASET` | `True` | anime_news_bot.py |
| `FEATURE_INDEPENDENT_PUBLISHER` | `True` | anime_news_bot.py |
| `FEATURE_LIFECYCLE_DIAGNOSTICS` | `True` | anime_news_bot.py |
| `FEATURE_LLM_BATCHING` | `True` | anime_news_bot.py |
| `FEATURE_LLM_BUDGET` | `True` | anime_news_bot.py |
| `FEATURE_LLM_JUDGE` | `False` | anime_news_bot.py |
| `FEATURE_LLM_QUALITY_ROUTING` | `True` | anime_news_bot.py |
| `FEATURE_MEDIA_QUALITY` | `True` | anime_news_bot.py |
| `FEATURE_MEDIA_SMART_CROP` | `True` | anime_news_bot.py |
| `FEATURE_METRICS` | `True` | anime_news_bot.py |
| `FEATURE_PERCEPTUAL_MEDIA_DEDUP` | `True` | anime_news_bot.py |
| `FEATURE_REPLAY` | `True` | anime_news_bot.py |
| `FEATURE_RUNTIME_MIGRATIONS` | `True` | anime_news_bot.py |
| `FEATURE_SHADOW_MODE` | `False` | anime_news_bot.py |
| `FEATURE_SOURCE_DISCOVERY` | `True` | anime_news_bot.py |
| `FEATURE_SOURCE_INTELLIGENCE` | `True` | anime_news_bot.py |
| `FEATURE_SOURCE_REPUTATION` | `True` | anime_news_bot.py |
| `FEATURE_SOURCE_YIELD` | `True` | anime_news_bot.py |
| `FEATURE_STORY_CLUSTERING` | `True` | anime_news_bot.py |
| `FEATURE_STORY_REGISTRY` | `True` | anime_news_bot.py |
| `FEATURE_STORY_UPDATES` | `True` | anime_news_bot.py |
| `FEATURE_STRUCTURED_LOGGING` | `True` | anime_news_bot.py |
| `FEATURE_VALUE_MODERATION_QUEUE` | `True` | anime_news_bot.py |
| `FEATURE_VIDEO_NORMALIZE` | `False` | anime_news_bot.py |
| `FEATURE_VIDEO_PROBE` | `True` | anime_news_bot.py |
| `FEATURE_VIDEO_THUMBNAILS` | `True` | anime_news_bot.py |

### Health-порт и метрики

| Переменная | По умолчанию | Где читается |
| --- | --- | --- |
| `HEALTH_HOST` | — | anime_news_bot.py |
| `HEALTH_MAX_CONNECTIONS` | `32` | anime_news_bot.py |
| `HEALTH_MAX_CONNECTIONS_PER_IP` | — | anime_news_bot.py |
| `HEALTH_METRICS_TOKEN` | — | anime_news_bot.py |
| `HEALTH_PORT` | — | anime_news_bot.py |
| `HEALTH_REQUEST_TIMEOUT_SEC` | `3` | anime_news_bot.py |
| `HEALTH_STORAGE_PROBE_CACHE_SEC` | `5.0` | anime_news_bot.py |
| `HEALTH_STRICT_READINESS` | `False` | anime_news_bot.py |

### Сетевые повторы

| Переменная | По умолчанию | Где читается |
| --- | --- | --- |
| `HTTP_RETRY_JITTER_RATIO` | `0.2` | anime_news_bot.py |
| `HTTP_RETRY_MAX_DELAY` | `30.0` | anime_news_bot.py |

### Языковая модель новостей

| Переменная | По умолчанию | Где читается |
| --- | --- | --- |
| `LLM_API_KEY` | — | anime_news_bot.py |
| `LLM_BASE_URL` | — | anime_news_bot.py |
| `LLM_BATCH_ITEM_TEXT_MAX` | `2000` | anime_news_bot.py |
| `LLM_BATCH_SIZE` | `4` | anime_news_bot.py |
| `LLM_BUDGET_WARN_RATIO` | `0.8` | anime_news_bot.py |
| `LLM_CIRCUIT_BASE_SEC` | `300` | anime_news_bot.py |
| `LLM_CIRCUIT_MAX_SEC` | `3600` | anime_news_bot.py |
| `LLM_DAILY_LIMIT` | `900` | anime_news_bot.py |
| `LLM_DAILY_TOKEN_BUDGET` | `0` | anime_news_bot.py |
| `LLM_DEFER_MAX_AGE_SEC` | `900` | anime_news_bot.py |
| `LLM_DEFER_MAX_ATTEMPTS` | `3` | anime_news_bot.py |
| `LLM_DEFER_RETRY_SEC` | `300` | anime_news_bot.py |
| `LLM_EDITORIAL_CACHE_MAX` | `400` | anime_news_bot.py |
| `LLM_EDITORIAL_CACHE_TTL_SEC` | — | anime_news_bot.py |
| `LLM_EXTRA_PARAMS` | — | anime_news_bot.py |
| `LLM_FALLBACK_API_KEY` | — | anime_news_bot.py |
| `LLM_FALLBACK_BASE_URL` | — | anime_news_bot.py |
| `LLM_FALLBACK_MODEL` | — | anime_news_bot.py |
| `LLM_FALLBACK_PROVIDER` | — | anime_news_bot.py |
| `LLM_FAST_API_KEY` | — | anime_news_bot.py |
| `LLM_FAST_BASE_URL` | — | anime_news_bot.py |
| `LLM_FAST_MODEL` | — | anime_news_bot.py |
| `LLM_FAST_PROVIDER` | — | anime_news_bot.py |
| `LLM_FAST_TASKS` | `judge` | anime_news_bot.py |
| `LLM_INLINE_RETRY_MAX_SEC` | `20.0` | anime_news_bot.py |
| `LLM_JUDGE_MAX_TOKENS` | `180` | anime_news_bot.py |
| `LLM_MAX_TOKENS` | `700` | anime_news_bot.py |
| `LLM_MIN_INTERVAL` | `1.2` | anime_news_bot.py |
| `LLM_MODEL` | — | anime_news_bot.py |
| `LLM_MODEL_ALTERNATES` | — | anime_news_bot.py |
| `LLM_PACE_MAX_SEC` | `60.0` | anime_news_bot.py |
| `LLM_PRIMARY_RETRY_MAX_SEC` | — | anime_news_bot.py |
| `LLM_PRIMARY_RETRY_SEC` | `1800` | anime_news_bot.py |
| `LLM_PROMPT_VERSION` | `editorial-v2-2026-08` | anime_news_bot.py |
| `LLM_PROVIDER` | — | anime_news_bot.py |
| `LLM_TIMEOUT` | `30` | anime_news_bot.py |

### Логи

| Переменная | По умолчанию | Где читается |
| --- | --- | --- |
| `LOG_BACKUP_COUNT` | `3` | anime_news_bot.py |
| `LOG_MAX_MB` | `5` | anime_news_bot.py |

### Картинки и видео в постах

| Переменная | По умолчанию | Где читается |
| --- | --- | --- |
| `MEDIA_CROP_MAX_DIM` | `1600` | anime_news_bot.py |
| `MEDIA_CROP_MAX_LOSS` | `0.38` | anime_news_bot.py |
| `MEDIA_CROP_PORTRAIT_BELOW` | `0.62` | anime_news_bot.py |
| `MEDIA_CROP_WIDE_ABOVE` | `2.15` | anime_news_bot.py |
| `MEDIA_DROP_BELOW_SCORE` | `18` | anime_news_bot.py |
| `MEDIA_MIN_HEIGHT` | `360` | anime_news_bot.py |
| `MEDIA_MIN_WIDTH` | `640` | anime_news_bot.py |
| `MEDIA_PRIMARY_REPLACE_SCORE` | `42` | anime_news_bot.py |
| `MEDIA_PROBE_MAX_IMAGES` | `6` | anime_news_bot.py |

### Модерация чата

| Переменная | По умолчанию | Где читается |
| --- | --- | --- |
| `MODERATION_ACTION_COOLDOWN_SEC` | `0` | anime_news_bot.py |
| `MODERATION_ADMINS_ENABLED` | `true` | anime_news_bot.py |
| `MODERATION_BELITTLING_STREAK` | `3` | anime_news_bot.py |
| `MODERATION_BELITTLING_WINDOW_SEC` | — | anime_news_bot.py |
| `MODERATION_CHATS` | — | anime_news_bot.py |
| `MODERATION_CONTEXT_SIZE` | `12` | anime_news_bot.py |
| `MODERATION_FLOOD_MESSAGES` | `6` | anime_news_bot.py |
| `MODERATION_FLOOD_WINDOW_SEC` | `12` | anime_news_bot.py |
| `MODERATION_LLM_API_KEY` | — | anime_news_bot.py |
| `MODERATION_LLM_BASE_URL` | — | anime_news_bot.py |
| `MODERATION_LLM_DAILY_LIMIT` | `120` | anime_news_bot.py |
| `MODERATION_LLM_ENABLED` | — | anime_news_bot.py |
| `MODERATION_LLM_MODEL` | — | anime_news_bot.py |
| `MODERATION_LLM_PROVIDER` | — | anime_news_bot.py |
| `MODERATION_LLM_TIMEOUT` | `12` | anime_news_bot.py |
| `MODERATION_LLM_TOKEN_BUDGET` | `50000` | anime_news_bot.py |
| `MODERATION_LOG_MAX` | `50` | anime_news_bot.py |
| `MODERATION_MAX_MESSAGE_CHARS` | `1000` | anime_news_bot.py |
| `MODERATION_MEDIA_ENABLED` | `true` | anime_news_bot.py |
| `MODERATION_MEDIA_EXPLICIT_THRESHOLD` | `0.8` | anime_news_bot.py |
| `MODERATION_MEDIA_MEMORY_MB` | — | anime_news_bot.py |
| `MODERATION_MEDIA_QUEUE` | `4` | anime_news_bot.py |
| `MODERATION_MEDIA_SUGGESTIVE_THRESHOLD` | `0.85` | anime_news_bot.py |
| `MODERATION_MEDIA_TIMEOUT_SEC` | `25` | anime_news_bot.py |
| `MODERATION_MODE` | — | anime_news_bot.py |
| `MODERATION_REPEAT_LIMIT` | `3` | anime_news_bot.py |
| `MODERATION_REPEAT_WINDOW_SEC` | `120` | anime_news_bot.py |
| `MODERATION_STORE_MAX_USERS` | `5000` | anime_news_bot.py |
| `MODERATION_WARN_TTL_HOURS` | — | anime_news_bot.py |

### Опрос Telegram

| Переменная | По умолчанию | Где читается |
| --- | --- | --- |
| `POLLING_BOOTSTRAP_RETRIES` | `-1` | anime_news_bot.py |
| `POLLING_CONFLICT_BACKOFF_MAX_SEC` | `60` | anime_news_bot.py |
| `POLLING_CONFLICT_BACKOFF_SEC` | `15` | anime_news_bot.py |
| `POLLING_CONFLICT_RETRIES` | `-1` | anime_news_bot.py |

### Источники новостей

| Переменная | По умолчанию | Где читается |
| --- | --- | --- |
| `SOURCE_BREAKER_BASE_SEC` | `300` | anime_news_bot.py |
| `SOURCE_BREAKER_FAIL_THRESHOLD` | `3` | anime_news_bot.py |
| `SOURCE_BREAKER_MAX_SEC` | `3600` | anime_news_bot.py |
| `SOURCE_DISCOVERY_FEED_MAX_KB` | `768` | anime_news_bot.py |
| `SOURCE_DISCOVERY_HTTP_TIMEOUT` | `6` | anime_news_bot.py |
| `SOURCE_DISCOVERY_MAX_CANDIDATES` | `500` | anime_news_bot.py |
| `SOURCE_DISCOVERY_MAX_LINKS_PER_ARTICLE` | `10` | anime_news_bot.py |
| `SOURCE_DISCOVERY_MAX_SCANNED` | `1500` | anime_news_bot.py |
| `SOURCE_DISCOVERY_MIN_MENTIONS` | `2` | anime_news_bot.py |
| `SOURCE_DISCOVERY_PROBES_PER_CYCLE` | `1` | anime_news_bot.py |
| `SOURCE_DISCOVERY_PROBE_COOLDOWN_HOURS` | `24` | anime_news_bot.py |
| `SOURCE_DISCOVERY_SCAN_PER_CYCLE` | `2` | anime_news_bot.py |
| `SOURCE_DISCOVERY_SCAN_TTL_DAYS` | `14` | anime_news_bot.py |
| `SOURCE_DISCOVERY_SUGGEST_SCORE` | `0.62` | anime_news_bot.py |
| `SOURCE_FETCH_CONCURRENCY` | `5` | anime_news_bot.py |
| `SOURCE_FETCH_WALL_TIMEOUT` | `60` | anime_news_bot.py |
| `SOURCE_INTEL_MIN_COMPARISONS` | `4` | anime_news_bot.py |
| `SOURCE_INTEL_STORY_MAX` | `2500` | anime_news_bot.py |
| `SOURCE_INTEL_STORY_TTL_DAYS` | `21` | anime_news_bot.py |
| `SOURCE_INTEL_WEIGHT_MAX` | `0.12` | anime_news_bot.py |
| `SOURCE_PROBATION_MIN_DAYS` | `3` | anime_news_bot.py |
| `SOURCE_PROBATION_MIN_STORIES` | `12` | anime_news_bot.py |
| `SOURCE_REPOST_LAG_HOURS` | `6.0` | anime_news_bot.py |
| `SOURCE_TIMELINESS_WINDOW_HOURS` | `24.0` | anime_news_bot.py |

### Сюжеты и обновления

| Переменная | По умолчанию | Где читается |
| --- | --- | --- |
| `STORY_CLUSTER_MAX_COMPARE` | `120` | anime_news_bot.py |
| `STORY_CLUSTER_SIMILARITY` | `0.88` | anime_news_bot.py |
| `STORY_REGISTRY_MAX` | `2500` | anime_news_bot.py |
| `STORY_REGISTRY_TTL_DAYS` | `14` | anime_news_bot.py |
| `STORY_UPDATE_LOOKBACK_DAYS` | `21` | anime_news_bot.py |
| `STORY_UPDATE_SIMILARITY` | `0.76` | anime_news_bot.py |

### Проверка фактов

| Переменная | По умолчанию | Где читается |
| --- | --- | --- |
| `VERIFICATION_CONFIDENCE_BELOW` | `0.72` | anime_news_bot.py |
| `VERIFICATION_MAX_OFFICIAL_LINKS` | `2` | anime_news_bot.py |
| `VERIFICATION_MAX_PER_CYCLE` | `4` | anime_news_bot.py |
| `VERIFICATION_PAGE_MAX_KB` | `384` | anime_news_bot.py |
| `VERIFICATION_TIMEOUT_SEC` | `7` | anime_news_bot.py |

### Видео: нормализация и превью

| Переменная | По умолчанию | Где читается |
| --- | --- | --- |
| `VIDEO_DIR` | — | anime_news_bot.py |
| `VIDEO_NORMALIZE_CRF` | `27` | anime_news_bot.py |
| `VIDEO_NORMALIZE_MAX_WIDTH` | `1280` | anime_news_bot.py |
| `VIDEO_PROBE_TIMEOUT_SEC` | `8` | anime_news_bot.py |
| `VIDEO_THUMB_SEEK_SEC` | `1.0` | anime_news_bot.py |

### Прочее

| Переменная | По умолчанию | Где читается |
| --- | --- | --- |
| `ADMIN_AUDIT_BACKUPS` | `2` | anime_news_bot.py |
| `ADMIN_AUDIT_MAX_MB` | `4` | anime_news_bot.py |
| `ADMIN_ID` | — | anime_news_bot.py |
| `ALLOW_LEGACY_IDS` | — | anime_news_bot.py |
| `AUTO_CYCLE_RETRY_BASE_SEC` | `120` | anime_news_bot.py |
| `AUTO_CYCLE_RETRY_MAX_SEC` | `900` | anime_news_bot.py |
| `BACKUP_VERIFY_MAX_FILES` | `2000` | anime_news_bot.py |
| `BACKUP_VERIFY_MAX_MB` | `64` | anime_news_bot.py |
| `BOT_TOKEN` | — | anime_news_bot.py |
| `BREAKING_MIN_CONFIDENCE` | `0.68` | anime_news_bot.py |
| `BREAKING_PRIORITY_BOOST` | `10.0` | anime_news_bot.py |
| `CANARY_CHANNEL_ID` | — | anime_news_bot.py |
| `CANARY_MIRROR_PERCENT` | `0.0` | anime_news_bot.py |
| `CHANNEL_ID` | — | anime_news_bot.py |
| `CHAOS_FUZZ_MAX_CHARS` | `4096` | anime_news_bot.py |
| `CHAOS_SELFTEST_ROUNDS` | `40` | anime_news_bot.py |
| `CHECK_INTERVAL_SEC` | `1800` | anime_news_bot.py |
| `CONFIDENCE_AUTO_MIN` | `0.48` | anime_news_bot.py |
| `CONFIDENCE_REVIEW_MAX_PER_CYCLE` | `3` | anime_news_bot.py |
| `DATA_DIR` | — | anime_news_bot.py |
| `DEEPL_API_KEY` | — | anime_news_bot.py |
| `DISCUSSION_CHAT_ID` | — | anime_news_bot.py |
| `DISCUSSION_THREAD_ID` | — | anime_news_bot.py |
| `DOCTOR_MIN_FREE_MB` | `256` | anime_news_bot.py |
| `EDITORIAL_LEARNING_MIN_SAMPLES` | `5` | anime_news_bot.py |
| `ERROR_FINGERPRINT_NOTIFY_EVERY` | `10` | anime_news_bot.py |
| `ERROR_FINGERPRINT_WINDOW_SEC` | `1800` | anime_news_bot.py |
| `EXPERIMENT_SALT` | `anime-news-bot-v1` | anime_news_bot.py |
| `FRANCHISE_COOLDOWN_MIN` | `180` | anime_news_bot.py |
| `FRANCHISE_COOLDOWN_PENALTY` | `7.0` | anime_news_bot.py |
| `IMAGE_BYTES_CACHE_MAX_MB` | `48` | anime_news_bot.py |
| `INSTANCE_LOCK_POLL_SEC` | `2.0` | anime_news_bot.py |
| `INSTANCE_LOCK_WAIT_SEC` | `-1` | anime_news_bot.py |
| `MAX_PHOTOS_PER_POST` | `6` | anime_news_bot.py |
| `NEWS_PER_SOURCE` | `5` | anime_news_bot.py |
| `PORT` | `0` | anime_news_bot.py |
| `POST_FORMAT_COMPACT_PERCENT` | `0.0` | anime_news_bot.py |
| `PUBLISHER_TICK_SEC` | `60` | anime_news_bot.py |
| `QUEUE_MAX_SIZE` | `30` | anime_news_bot.py |
| `REPLAY_BUFFER_MAX` | `300` | anime_news_bot.py |
| `RESTART_LOOP_INTERVAL_SEC` | `300` | anime_news_bot.py |
| `RESTART_STORM_THRESHOLD` | `4` | anime_news_bot.py |
| `RESTART_STORM_WINDOW_SEC` | `900` | anime_news_bot.py |
| `SENT_LINKS_MAX` | `5000` | anime_news_bot.py |
| `SENT_LINKS_TRIM_TO` | — | anime_news_bot.py |
| `STARTUP_REPORT_MAX_IN_WINDOW` | `3` | anime_news_bot.py |
| `STARTUP_REPORT_WINDOW_SEC` | `1800` | anime_news_bot.py |
| `TELEGRAM_BOT_TOKEN` | — | anime_news_bot.py |
