-- Nokker — codes de connexion par e-mail (écran sans mot de passe, scope §16). Un seul code vivant par compte : un
-- nouveau code remplace le précédent. On ne garde que l'empreinte (compte + code) ; 10 minutes, usage unique,
-- 5 essais puis le code est brûlé. Hors RLS comme les identités et l'appairage Telegram : lu avant de connaître le
-- locataire, et seulement par nokker/onboarding/emailcode.py.
CREATE TABLE IF NOT EXISTS auth_email_codes (
    account_id   TEXT PRIMARY KEY REFERENCES accounts(id),
    code_sha256  TEXT NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
