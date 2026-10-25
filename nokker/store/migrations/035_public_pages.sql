-- Pages publiques (docs/LOKKAL.md §7.26, nokker/cloud/mirror_pages.py) et registre des jetons de pages consommés pour
-- un relais à plusieurs répliques. Rejouée à chaque démarrage. Numérotée 035 (034 réservée à une autre branche).
--
-- 1. La copie d'une page gardée en miroir (033) peut porter sa publication : l'EMPREINTE sha256 de l'id public (jamais
--    l'id lui-même) et le choix d'indexation. Posée seulement si la version copiée est celle que l'utilisateur a
--    publiée (carte forte sur sa machine) et si le compte peut publier ; remise à NULL par toute copie sans
--    publication et par `DELETE /v1/mirror/pages/{id}/public`. Même RLS que la table (033), mêmes purges.
ALTER TABLE lokkal_mirror_pages ADD COLUMN IF NOT EXISTS public_sha256 text;
ALTER TABLE lokkal_mirror_pages ADD COLUMN IF NOT EXISTS public_index boolean NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS lokkal_mirror_pages_public ON lokkal_mirror_pages (machine_id, public_sha256)
    WHERE public_sha256 IS NOT NULL;

-- 2. Jti des jetons de pages (60 s, usage unique) déjà présentés au relais, quelle que soit sa réplique. HORS RLS comme
--    lokkal_gateway_codes (019) : cherché par empreinte, sans locataire ; aucune donnée personnelle (empreinte d'un
--    identifiant aléatoire), effacé à l'expiration (au plus 10 min après la présentation).
CREATE TABLE IF NOT EXISTS lokkal_relay_used_jti (
    jti_sha256  text PRIMARY KEY,
    expires_at  double precision NOT NULL
);
CREATE INDEX IF NOT EXISTS lokkal_relay_used_jti_exp ON lokkal_relay_used_jti (expires_at);
