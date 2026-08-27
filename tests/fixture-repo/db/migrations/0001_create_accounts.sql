-- Initial account tables.
CREATE TABLE IF NOT EXISTS accounts (
    id            BIGSERIAL PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name     TEXT,
    last_login_ip INET,
    signup_notes  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT accounts_email_lower CHECK (email = lower(email))
);

CREATE TABLE billing_profiles (
    id                 BIGSERIAL PRIMARY KEY,
    account_id         BIGINT REFERENCES accounts (id),
    credit_card_number TEXT,
    iban               TEXT,
    updated_at         TIMESTAMPTZ
);
