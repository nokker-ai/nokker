-- Lexique de prononciation du foyer (docs/VOICE.md §6, nokker/voice/speak_lexicon.py). Rejouée à chaque démarrage :
-- idempotente. Numéro 060 (gardé à l'intégration dans le train).
-- Une ligne = un mot ou groupe de mots du foyer et sa prononciation, au locataire du FOYER (celui du payeur), sous RLS
-- activée ET forcée (docs/TENANCY.md §4). `word_key` : minuscules, espaces repliés (une seule entrée par mot).
-- Effacé avec le locataire (`nokker erase`, colonne tenant_id).
CREATE TABLE IF NOT EXISTS household_lexicon (
    tenant_id     text NOT NULL DEFAULT current_setting('app.tenant_id'),
    household_id  text NOT NULL,
    word_key      text NOT NULL,
    word          text NOT NULL,
    say           text NOT NULL,
    updated_by    text NOT NULL DEFAULT '',
    updated_at    double precision NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, household_id, word_key)
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'household_lexicon' AND relrowsecurity AND relforcerowsecurity) THEN
        ALTER TABLE household_lexicon ENABLE ROW LEVEL SECURITY;
        ALTER TABLE household_lexicon FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'household_lexicon' AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON household_lexicon
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;
