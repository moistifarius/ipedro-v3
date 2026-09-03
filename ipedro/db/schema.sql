-- iPedro V2 schema. Applied idempotently at startup by migrations.py.
-- pgvector is optional: when the extension is missing, embedding-related
-- features degrade gracefully and the rest of the bot still works.

CREATE EXTENSION IF NOT EXISTS vector;

-- Schema version tracking ----------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Chats ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chats (
    chat_id     BIGINT PRIMARY KEY,
    type        TEXT NOT NULL,                  -- private | group | supergroup | channel
    title       TEXT,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Users ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    user_id     BIGINT PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    last_name   TEXT,
    is_bot      BOOLEAN NOT NULL DEFAULT FALSE,
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Per-chat config ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chat_config (
    chat_id              BIGINT PRIMARY KEY REFERENCES chats(chat_id) ON DELETE CASCADE,
    response_policy      TEXT NOT NULL DEFAULT 'mention',  -- commands|mention|reply|ambient|always
    ambient_probability  REAL NOT NULL DEFAULT 0.03,
    persona              TEXT NOT NULL DEFAULT 'pedro',
    persona_custom       TEXT,
    duckhunt_enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    voice_transcribe     BOOLEAN NOT NULL DEFAULT TRUE,
    memory_enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    share_photo_enabled  BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Back-fill new columns on existing installs (CREATE TABLE IF NOT EXISTS
-- won't add columns to an existing table).
ALTER TABLE chat_config
    ADD COLUMN IF NOT EXISTS share_photo_enabled BOOLEAN NOT NULL DEFAULT FALSE;

-- Messages -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    id           BIGSERIAL PRIMARY KEY,
    chat_id      BIGINT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    message_id   BIGINT,                                -- Telegram message id (NULL for synthetic)
    user_id      BIGINT,                                -- NULL for bot messages
    role         TEXT NOT NULL,                         -- user | assistant | system | tool
    content      TEXT NOT NULL,
    tokens       INTEGER,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS messages_chat_created_idx
    ON messages (chat_id, created_at DESC);

-- Rolling conversation summaries --------------------------------------------
CREATE TABLE IF NOT EXISTS summaries (
    id                 BIGSERIAL PRIMARY KEY,
    chat_id            BIGINT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    summary            TEXT NOT NULL,
    covers_until_id    BIGINT NOT NULL,               -- last message.id included
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS summaries_chat_idx
    ON summaries (chat_id, created_at DESC);

-- Durable per-chat facts (high-signal, long-lived) --------------------------
CREATE TABLE IF NOT EXISTS facts (
    id          BIGSERIAL PRIMARY KEY,
    chat_id     BIGINT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    user_id     BIGINT,
    fact        TEXT NOT NULL,
    source_msg  BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS facts_chat_idx ON facts (chat_id);

-- Embeddings (semantic memory) ----------------------------------------------
-- ref_kind: 'message' | 'summary' | 'fact'
CREATE TABLE IF NOT EXISTS embeddings (
    id           BIGSERIAL PRIMARY KEY,
    chat_id      BIGINT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    ref_kind     TEXT NOT NULL,
    ref_id       BIGINT NOT NULL,
    content      TEXT NOT NULL,
    embedding    vector(1536),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (chat_id, ref_kind, ref_id)
);

CREATE INDEX IF NOT EXISTS embeddings_chat_idx ON embeddings (chat_id);
-- ivfflat requires ANALYZE before being useful but is cheap to create empty.
CREATE INDEX IF NOT EXISTS embeddings_vec_idx
    ON embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Command log (audit) -------------------------------------------------------
CREATE TABLE IF NOT EXISTS command_log (
    id           BIGSERIAL PRIMARY KEY,
    chat_id      BIGINT,
    user_id      BIGINT,
    command      TEXT NOT NULL,
    args         TEXT,
    success      BOOLEAN NOT NULL DEFAULT TRUE,
    error        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS command_log_recent_idx
    ON command_log (created_at DESC);

-- Duckhunt ------------------------------------------------------------------
-- Persistent state for active spawns. At most one active duck per chat.
CREATE TABLE IF NOT EXISTS duck_events (
    id              BIGSERIAL PRIMARY KEY,
    chat_id         BIGINT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    rarity          TEXT NOT NULL,                -- common|uncommon|rare|epic|legendary
    spawned_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    resolved        BOOLEAN NOT NULL DEFAULT FALSE,
    resolved_by     BIGINT,
    resolved_action TEXT,                         -- bang|bef|ignore|expired|departed
    resolved_at     TIMESTAMPTZ,
    points_awarded  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS duck_events_chat_active_idx
    ON duck_events (chat_id, resolved);

CREATE TABLE IF NOT EXISTS duck_stats (
    chat_id        BIGINT NOT NULL,
    user_id        BIGINT NOT NULL,
    display_name   TEXT NOT NULL,
    killed         INTEGER NOT NULL DEFAULT 0,
    befriended     INTEGER NOT NULL DEFAULT 0,
    ignored        INTEGER NOT NULL DEFAULT 0,
    -- Vestigial: the berate action was removed in V2.1 but the column
    -- is preserved so existing rows / historical data remain readable.
    berated_win    INTEGER NOT NULL DEFAULT 0,
    misses         INTEGER NOT NULL DEFAULT 0,
    points         INTEGER NOT NULL DEFAULT 0,
    streak         INTEGER NOT NULL DEFAULT 0,
    best_streak    INTEGER NOT NULL DEFAULT 0,
    last_action_at TIMESTAMPTZ,
    PRIMARY KEY (chat_id, user_id)
);

CREATE INDEX IF NOT EXISTS duck_stats_leaderboard_idx
    ON duck_stats (chat_id, points DESC);

-- Per-duck nickname (set with /duckname). NULL means unnamed.
ALTER TABLE duck_events
    ADD COLUMN IF NOT EXISTS name TEXT;

-- When TRUE (the default), this chat's named ducks are surfaced by the
-- global `/ducknames` command. Flip OFF in chats that want their roster
-- private — those ducks become invisible to any chat that runs the
-- command. Doesn't affect /duckfriends, /duckstats, or anything inside
-- the chat itself.
ALTER TABLE chat_config
    ADD COLUMN IF NOT EXISTS duck_names_public BOOLEAN NOT NULL DEFAULT TRUE;

-- Reminders --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reminders (
    id          BIGSERIAL PRIMARY KEY,
    chat_id     BIGINT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    user_id     BIGINT,
    text        TEXT NOT NULL,
    fire_at     TIMESTAMPTZ NOT NULL,
    fired       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS reminders_due_idx
    ON reminders (fired, fire_at);

-- Per-chat persona state ----------------------------------------------------
-- Holds the chat's current mood, word-of-the-day, and any currently-stuck
-- word that Pedro fixates on. All three are nullable and refreshed lazily
-- when build_context runs.
CREATE TABLE IF NOT EXISTS chat_state (
    chat_id               BIGINT PRIMARY KEY REFERENCES chats(chat_id) ON DELETE CASCADE,
    mood                  TEXT,
    mood_set_at           TIMESTAMPTZ,
    word_of_day           TEXT,
    word_of_day_at        TIMESTAMPTZ,
    stuck_word            TEXT,
    stuck_word_expires_at TIMESTAMPTZ,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Saved quotes (per chat) ---------------------------------------------------
CREATE TABLE IF NOT EXISTS quotes (
    id                BIGSERIAL PRIMARY KEY,
    chat_id           BIGINT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    seq               BIGINT,          -- per-chat display number: #1, #2, #3…
    quoted_user_id    BIGINT,
    quoted_name       TEXT,
    text              TEXT NOT NULL,
    saved_by          BIGINT,
    source_message_id BIGINT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS quotes_chat_idx ON quotes (chat_id);

-- Per-chat sequential quote numbers. The BIGSERIAL `id` is global across all
-- chats, so a single chat used to see gappy numbers like #3, #17, #42 (the
-- gaps being other chats' quotes) — confusing, and /unquote needed those odd
-- ids. `seq` gives each chat its own #1, #2, #3… The one-time back-fill below
-- numbers existing rows per chat by id order; it only touches rows where seq
-- IS NULL, so it is a no-op on every startup after the first.
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS seq BIGINT;
UPDATE quotes q
   SET seq = n.rn
  FROM (
        SELECT id,
               ROW_NUMBER() OVER (PARTITION BY chat_id ORDER BY id) AS rn
          FROM quotes
         WHERE seq IS NULL
       ) n
 WHERE q.id = n.id
   AND q.seq IS NULL;

-- Birthdays / anniversaries -------------------------------------------------
-- One row per (chat_id, user_id, label). Year is optional; if set we can
-- compute "Nth anniversary" / "age". The daily celebrations loop posts when
-- today's MM-DD matches and last_celebrated is older than today.
CREATE TABLE IF NOT EXISTS chat_dates (
    id              BIGSERIAL PRIMARY KEY,
    chat_id         BIGINT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    user_id         BIGINT,
    label           TEXT NOT NULL,                   -- "birthday", "anniversary", etc.
    month           SMALLINT NOT NULL,
    day             SMALLINT NOT NULL,
    year            SMALLINT,
    note            TEXT,
    last_celebrated DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (chat_id, user_id, label)
);

CREATE INDEX IF NOT EXISTS chat_dates_today_idx
    ON chat_dates (month, day);

-- Daily comic strip opt-in --------------------------------------------------
ALTER TABLE chat_config
    ADD COLUMN IF NOT EXISTS comic_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE chat_config
    ADD COLUMN IF NOT EXISTS last_comic_at TIMESTAMPTZ;

-- Boss-duck columns ---------------------------------------------------------
-- A boss duck takes multiple hits across multiple users to defeat. If
-- boss_required_hits is NULL the row is a normal duck.
ALTER TABLE duck_events
    ADD COLUMN IF NOT EXISTS boss_required_hits INTEGER;
ALTER TABLE duck_events
    ADD COLUMN IF NOT EXISTS boss_current_hits INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS duck_boss_hits (
    duck_id   BIGINT NOT NULL REFERENCES duck_events(id) ON DELETE CASCADE,
    user_id   BIGINT NOT NULL,
    display_name TEXT NOT NULL,
    hits      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (duck_id, user_id)
);

-- OpenAI usage log (cost tracking) ------------------------------------------
CREATE TABLE IF NOT EXISTS openai_usage (
    id                BIGSERIAL PRIMARY KEY,
    chat_id           BIGINT,
    kind              TEXT NOT NULL,         -- chat | embed | image | transcribe | translate
    model             TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    cost_usd          REAL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS openai_usage_chat_recent_idx
    ON openai_usage (chat_id, created_at DESC);
CREATE INDEX IF NOT EXISTS openai_usage_recent_idx
    ON openai_usage (created_at DESC);

-- Karma --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS karma (
    chat_id      BIGINT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    user_id      BIGINT NOT NULL,
    display_name TEXT NOT NULL,
    score        INTEGER NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chat_id, user_id)
);

CREATE INDEX IF NOT EXISTS karma_chat_score_idx
    ON karma (chat_id, score DESC);

-- Anonymous confessions ----------------------------------------------------
-- chat_id is intentionally nullable — confessions live in a global pool
-- and can surface in any chat. submitted_by is stored for audit but never
-- displayed.
CREATE TABLE IF NOT EXISTS confessions (
    id           BIGSERIAL PRIMARY KEY,
    submitted_by BIGINT,
    text         TEXT NOT NULL,
    surfaced_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS confessions_unsurfaced_idx
    ON confessions (surfaced_at) WHERE surfaced_at IS NULL;

-- Track the last year we posted a year-in-review per chat, so we don't
-- double-fire on Dec 31.
ALTER TABLE chat_state
    ADD COLUMN IF NOT EXISTS last_retrospective_year INTEGER;

-- Per-(chat,user) moderation flags ---------------------------------------
-- One row per active flag. flag is 'shutup' | 'snark' | 'grudge'. The
-- grudge flag is auto-managed (insults add it, decays after 24h);
-- shutup/snark are admin-set via /shutup / /snark_at.
CREATE TABLE IF NOT EXISTS user_flags (
    chat_id    BIGINT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    user_id    BIGINT NOT NULL,
    flag       TEXT NOT NULL,
    expires_at TIMESTAMPTZ,
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chat_id, user_id, flag)
);

CREATE INDEX IF NOT EXISTS user_flags_lookup_idx
    ON user_flags (chat_id, user_id, flag);

-- Daily fortune cookie opt-in + last-posted-date
ALTER TABLE chat_config
    ADD COLUMN IF NOT EXISTS fortune_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE chat_state
    ADD COLUMN IF NOT EXISTS last_fortune_date DATE;

-- Ether (cross-chat pager garbling) opt-in + receiver cooldown timestamp.
ALTER TABLE chat_config
    ADD COLUMN IF NOT EXISTS ether_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE chat_state
    ADD COLUMN IF NOT EXISTS last_ether_at TIMESTAMPTZ;

-- 'On this day' nostalgia. VESTIGIAL: the daily auto-post loop was replaced
-- by the monthly recap; /onthisday is manual-only. Both columns kept to
-- avoid a pointless migration — nothing writes them any more.
ALTER TABLE chat_config
    ADD COLUMN IF NOT EXISTS on_this_day_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE chat_state
    ADD COLUMN IF NOT EXISTS last_on_this_day_date DATE;

-- Monthly recap — a 'month in review' best-of, posted at the start of each new
-- local month for the month just finished. On by default; last_monthly_recap
-- holds the first day of the most recently recapped month (restart-safe).
ALTER TABLE chat_config
    ADD COLUMN IF NOT EXISTS monthly_recap_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE chat_state
    ADD COLUMN IF NOT EXISTS last_monthly_recap DATE;

-- AutoMod canned responses (keyword -> copypasta / meme media). On by
-- default; the per-chat kill switch for when the bit gets old.
ALTER TABLE chat_config
    ADD COLUMN IF NOT EXISTS automod_enabled BOOLEAN NOT NULL DEFAULT TRUE;

-- Generic key/value store for global tunables (e.g. the master Pedro prompt).
CREATE TABLE IF NOT EXISTS kv_store (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One outstanding bef challenge per (chat, user). The user must reply to
-- `prompt_message_id` with an answer that the AI judge accepts before they
-- can attempt /bef again. There is no time-based cooldown on bef itself;
-- this challenge IS the cooldown.
CREATE TABLE IF NOT EXISTS bef_challenges (
    chat_id            BIGINT NOT NULL,
    user_id            BIGINT NOT NULL,
    challenge          TEXT NOT NULL,
    kind               TEXT NOT NULL,                  -- captcha|trivia|recipe
    prompt_message_id  BIGINT,                         -- Telegram msg_id the user must reply to
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chat_id, user_id)
);

CREATE INDEX IF NOT EXISTS bef_challenges_prompt_idx
    ON bef_challenges (chat_id, prompt_message_id);

-- Disgust personality test -------------------------------------------------
-- In-progress quiz state: one session per (chat, user), so concurrent takers
-- never collide and re-taking simply overwrites. `answers` accumulates the
-- ordered 1-6 ratings; cardinality(answers) is the current question index.
CREATE TABLE IF NOT EXISTS disgust_test_sessions (
    chat_id     BIGINT NOT NULL,
    user_id     BIGINT NOT NULL,
    message_id  BIGINT,                              -- the quiz message being edited
    answers     INTEGER[] NOT NULL DEFAULT '{}',
    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chat_id, user_id)
);

-- Latest result per (chat, user). Overwritten on re-take. Scores are 1-6
-- means; overall_score drives the /disgustboard leaderboard.
CREATE TABLE IF NOT EXISTS disgust_test_results (
    chat_id       BIGINT NOT NULL,
    user_id       BIGINT NOT NULL,
    display_name  TEXT NOT NULL,
    food_score    REAL NOT NULL,
    general_score REAL NOT NULL,
    core_score    REAL,
    animal_score  REAL,
    contam_score  REAL,
    overall_score REAL NOT NULL,
    biggest_ick   TEXT,
    taken_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chat_id, user_id)
);

CREATE INDEX IF NOT EXISTS disgust_results_board_idx
    ON disgust_test_results (chat_id, overall_score DESC);

-- Cached per-question illustrations. The 16 quiz items are fixed, so each
-- image is generated once (via the image model) and reused for every taker
-- in every chat — the quiz shows real pictures with no per-test cost or
-- latency. Generated lazily/by /disgust_warmup; the quiz falls back to an
-- emoji text flow until all 16 are present.
CREATE TABLE IF NOT EXISTS disgust_item_images (
    item_key   TEXT PRIMARY KEY,
    png        BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Generic quiz engine ------------------------------------------------------
-- Multiple personality quizzes (disgust, dark triad, food neophobia, big
-- five, …) share these tables, keyed by quiz_id. The disgust_* tables above
-- predate the engine; their data is migrated in below and they go vestigial.
CREATE TABLE IF NOT EXISTS quiz_sessions (
    quiz_id     TEXT NOT NULL,
    chat_id     BIGINT NOT NULL,
    user_id     BIGINT NOT NULL,
    message_id  BIGINT,
    answers     INTEGER[] NOT NULL DEFAULT '{}',
    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (quiz_id, chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS quiz_results (
    quiz_id        TEXT NOT NULL,
    chat_id        BIGINT NOT NULL,
    user_id        BIGINT NOT NULL,
    display_name   TEXT NOT NULL,
    headline_score REAL NOT NULL,
    summary        TEXT,
    detail         JSONB,
    taken_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (quiz_id, chat_id, user_id)
);
CREATE INDEX IF NOT EXISTS quiz_results_board_idx
    ON quiz_results (quiz_id, chat_id, headline_score DESC);

CREATE TABLE IF NOT EXISTS quiz_item_images (
    quiz_id    TEXT NOT NULL,
    item_key   TEXT NOT NULL,
    png        BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (quiz_id, item_key)
);

-- One-time migration of the standalone disgust data into the generic tables.
-- Idempotent (ON CONFLICT DO NOTHING), so it's a no-op on every later startup.
INSERT INTO quiz_results (quiz_id, chat_id, user_id, display_name,
                          headline_score, summary, detail, taken_at)
SELECT 'disgust', chat_id, user_id, display_name, overall_score,
       CASE WHEN overall_score < 2 THEN 'iron-stomached'
            WHEN overall_score < 3 THEN 'pretty unbothered'
            WHEN overall_score < 4 THEN 'middle of the road'
            WHEN overall_score < 5 THEN 'squeamish'
            ELSE 'can barely cope' END,
       jsonb_build_object('food', food_score, 'general', general_score,
                          'overall', overall_score, 'biggest_ick', biggest_ick),
       taken_at
  FROM disgust_test_results
ON CONFLICT (quiz_id, chat_id, user_id) DO NOTHING;

INSERT INTO quiz_item_images (quiz_id, item_key, png, created_at)
SELECT 'disgust', item_key, png, created_at FROM disgust_item_images
ON CONFLICT (quiz_id, item_key) DO NOTHING;

-- Dale Gribble GIF library ---------------------------------------------------
-- GLOBAL on purpose: no chat_id column, because the library is shared by every
-- chat (and because db/chat_migration.py:63-78 re-keys every table that HAS
-- one). Rows arrive seeded from pinned URLs, or captured from Telegram when an
-- admin tags a GIF with /dalegif <tags>.
--
-- Identity is file_unique_id, NOT file_id: file_id is per-bot and Telegram may
-- re-issue it, while file_unique_id is stable and is what makes "we already
-- have this GIF" answerable. url is unique too, which is what makes re-running
-- the seed a no-op. Postgres allows many NULLs in a UNIQUE column, so
-- url-only and file-only rows coexist happily.
--
-- No index: this table is a few dozen rows and every read is either a random
-- pick or a full list. A seq scan is microseconds and an index would be noise.
CREATE TABLE IF NOT EXISTS dale_gifs (
    id             BIGSERIAL PRIMARY KEY,
    tags           TEXT[] NOT NULL DEFAULT '{}',
    file_id        TEXT,
    file_unique_id TEXT UNIQUE,
    url            TEXT UNIQUE,
    added_by       BIGINT,
    send_count     INTEGER NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Vision: what the bot saw in a piece of media --------------------------------
-- GLOBAL, like dale_gifs: file_unique_id is Telegram's stable per-file
-- identity, so the same sticker forwarded into five chats is one row and one
-- vision call. Stickers and memes repeat constantly, which makes this cache
-- most of the cost control rather than a nicety.
--
-- Descriptions are never invalidated: the bytes behind a file_unique_id can't
-- change, so neither can what's in the picture.
CREATE TABLE IF NOT EXISTS media_descriptions (
    file_unique_id TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,          -- photo | sticker | gif | video | …
    description    TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Per-chat switch for looking at media. On by default; the kill switch for a
-- chat that posts hundreds of images a day and doesn't want them described.
ALTER TABLE chat_config
    ADD COLUMN IF NOT EXISTS vision_enabled BOOLEAN NOT NULL DEFAULT TRUE;
