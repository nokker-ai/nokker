-- Pages servies depuis le miroir quand la machine est éteinte (abonnés ; docs/LOKKAL.md §6.6 et §7.23,
-- nokker/cloud/mirror_pages.py). Une ligne courante par (machine, page) : la dernière version que l'utilisateur a
-- demandé de copier, jamais une page `local` (filtrée par la machine, refusée ici aussi). Lue et écrite en connaissant
-- le locataire (machine authentifiée, ou relais qui a vérifié la preuve du navigateur), donc sous RLS activée ET forcée
-- par tenant_id (docs/TENANCY.md §4), comme lokkal_mirror_notes (025) ; effacée par `nokker erase` (tenant_id), par
-- `POST /v1/mirror/erase` et à la révocation de la machine. Plafond de taille par compte : somme de `size` (contrôlée
-- sous verrou consultatif du locataire, nokker/cloud/pg.py::PgMirrorPages.put). Pas de clé étrangère. Rejouée à chaque
-- démarrage. Numérotée 033 : 031 (déclencheur mail) et 032 (SSO) sont prises par d'autres branches.
CREATE TABLE IF NOT EXISTS lokkal_mirror_pages (
    machine_id       text NOT NULL,
    page_id          text NOT NULL,                -- pg_<16 hex> (contrôlé par nokker/cloud/mirror_pages.py)
    tenant_id        text NOT NULL DEFAULT current_setting('app.tenant_id'),
    title            text NOT NULL DEFAULT '',
    version          integer NOT NULL,
    size             integer NOT NULL,             -- octets, 5 Mio au plus (contrôlé à l'arrivée)
    sha256           text NOT NULL,
    html             bytea NOT NULL,
    page_updated_at  double precision NOT NULL,   -- heure de la machine (indication seulement)
    mirrored_at      double precision NOT NULL,   -- heure de nokker.ai à la réception : la date du bandeau
    PRIMARY KEY (machine_id, page_id)
);
CREATE INDEX IF NOT EXISTS lokkal_mirror_pages_tenant ON lokkal_mirror_pages (tenant_id);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'lokkal_mirror_pages' AND relrowsecurity
                   AND relforcerowsecurity) THEN
        ALTER TABLE lokkal_mirror_pages ENABLE ROW LEVEL SECURITY;
        ALTER TABLE lokkal_mirror_pages FORCE ROW LEVEL SECURITY;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'lokkal_mirror_pages'
                   AND policyname = 'tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON lokkal_mirror_pages
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    END IF;
END $$;
