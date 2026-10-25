-- Modération des pages publiques (docs/LOKKAL.md §7.26, point 6 ; nokker/cloud/moderation.py). Rejouée à chaque
-- démarrage. Numérotée 040 : 031 à 039 sont prises (vérifié sur toutes les branches le 25.10).
--
-- 1. Registre des pages publiques que le relais a servies (machine en ligne : POST /v1/gateway/public-page/check ;
--    machine éteinte : copie publique du miroir), et de celles que l'opérateur a retirées. HORS RLS comme
--    lokkal_machines (014) : le relais le consulte avant de connaître le locataire, l'opérateur le lit pour tous les
--    locataires. Ni contenu, ni titre, ni compte : la machine, le locataire, le page_id (dit par la machine),
--    l'EMPREINTE de l'id public (jamais l'id), la taille et des heures. Effacé avec le locataire (`nokker erase`).
CREATE TABLE IF NOT EXISTS lokkal_public_pages (
    id             bigserial PRIMARY KEY,
    machine_id     text NOT NULL,
    tenant_id      text NOT NULL,
    page_id        text,                          -- pg_<16 hex>, NULL tant que la machine ne l'a pas dit
    public_sha256  text,                          -- sha256 de l'id public, NULL pour un retrait par page_id seul
    size           integer NOT NULL DEFAULT 0,    -- octets de la version publiée, vue par le relais
    first_seen_at  double precision NOT NULL,
    last_seen_at   double precision NOT NULL,     -- réécrite toutes les 5 min au plus
    moderated_at   double precision,              -- non NULL : retirée par l'opérateur (le relais répond 410)
    CHECK (page_id IS NOT NULL OR public_sha256 IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS lokkal_public_pages_sha ON lokkal_public_pages (machine_id, public_sha256)
    WHERE public_sha256 IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS lokkal_public_pages_page ON lokkal_public_pages (machine_id, page_id)
    WHERE page_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS lokkal_public_pages_tenant ON lokkal_public_pages (tenant_id);

-- 2. Audit des retraits et des remises (ajout seul : aucune ligne ne change ; supprimées seulement avec le locataire).
--    Page, empreinte, machine, locataire, action, motif (une adresse e-mail y est remplacée avant), date. Hors RLS
--    pour la même raison que le registre.
CREATE TABLE IF NOT EXISTS lokkal_page_moderation (
    id             bigserial PRIMARY KEY,
    at             double precision NOT NULL,
    machine_id     text NOT NULL,
    tenant_id      text NOT NULL,
    page_id        text,
    public_sha256  text,
    action         text NOT NULL CHECK (action IN ('unpublish', 'restore')),
    reason         text NOT NULL CHECK (length(reason) BETWEEN 1 AND 500)
);
CREATE INDEX IF NOT EXISTS lokkal_page_moderation_tenant ON lokkal_page_moderation (tenant_id);

CREATE OR REPLACE FUNCTION nokker_page_moderation_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'lokkal_page_moderation : journal en ajout seul (ligne %)', OLD.id;
END $$;
DROP TRIGGER IF EXISTS lokkal_page_moderation_append_only ON lokkal_page_moderation;
CREATE TRIGGER lokkal_page_moderation_append_only BEFORE UPDATE ON lokkal_page_moderation
    FOR EACH ROW EXECUTE FUNCTION nokker_page_moderation_append_only();
