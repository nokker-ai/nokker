-- Exposé des motifs d'un retrait de page publique à son auteur (docs/LOKKAL.md §7.26 point 6, DSA art. 17 ;
-- nokker/cloud/moderation.py). Rejouée à chaque démarrage.
--
-- Le registre garde le motif du retrait EN COURS (déjà nettoyé : aucune adresse e-mail, 500 caractères au plus),
-- remis à NULL par un `restore`. La machine le lit dans `GET /v1/gateway/machine` → `moderation_notices`. L'audit
-- `lokkal_page_moderation` (040) reste la trace complète, en ajout seul ; rien n'y change.
ALTER TABLE lokkal_public_pages ADD COLUMN IF NOT EXISTS moderation_reason text
    CHECK (moderation_reason IS NULL OR length(moderation_reason) <= 500);

-- Lignes retirées avant cette migration : le motif du dernier retrait de l'audit pour la même page (page_id ou
-- empreinte, même machine). Idempotent : seulement les lignes retirées sans motif.
UPDATE lokkal_public_pages p
   SET moderation_reason = (
       SELECT a.reason FROM lokkal_page_moderation a
        WHERE a.machine_id = p.machine_id AND a.action = 'unpublish'
          AND (a.page_id = p.page_id OR a.public_sha256 = p.public_sha256)
        ORDER BY a.id DESC LIMIT 1)
 WHERE p.moderated_at IS NOT NULL AND p.moderation_reason IS NULL;
