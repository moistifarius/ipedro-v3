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
