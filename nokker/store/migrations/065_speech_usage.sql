-- Nokker — caractères lus à voix haute par compte et par jour (bouton « Écouter » du web, nokker/voice/web.py) : suivi
-- de la consommation du crédit de synthèse vocale. Colonne de plus dans tenant_usage (007) : RLS inchangée.
ALTER TABLE tenant_usage ADD COLUMN IF NOT EXISTS speech_chars INTEGER NOT NULL DEFAULT 0;
