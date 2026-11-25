-- Nokker — écran mural (P10b, docs/AMBIENT.md §6) : une vue glance posée dans une pièce commune. Additive.
-- `surface` : glasses (défaut, comme avant) | wall, fixée à l'appairage. `show_brief` : le titulaire choisit si le mur
-- montre le brief et le titre du prochain événement ; faux à l'appairage d'un mur, toujours vrai pour des lunettes.
-- Le filtrage est fait par le noyau (GET /v1/glance) : rien de masqué ne sort du serveur.

ALTER TABLE ambient_displays ADD COLUMN IF NOT EXISTS surface TEXT NOT NULL DEFAULT 'glasses';
ALTER TABLE ambient_displays ADD COLUMN IF NOT EXISTS show_brief BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE ambient_displays DROP CONSTRAINT IF EXISTS ambient_displays_surface_check;
ALTER TABLE ambient_displays ADD CONSTRAINT ambient_displays_surface_check CHECK (surface IN ('glasses', 'wall'));
