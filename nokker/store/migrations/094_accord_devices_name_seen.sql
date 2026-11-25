-- Nokker — appareils ACCORD : nom donné par la personne et dernière connexion (docs/ACCORD-DEVICE.md §2). Additive :
-- deux colonnes NULL, rien n'est réécrit. `name` NULL = nom par défaut « ACCORD <key8> », calculé à la lecture ;
-- `last_seen_at` posée au `ready` d'une connexion authentifiée, au plus une fois par minute et par appareil.

ALTER TABLE accord_devices ADD COLUMN IF NOT EXISTS name TEXT CHECK (name IS NULL OR char_length(name) BETWEEN 1 AND 40);
ALTER TABLE accord_devices ADD COLUMN IF NOT EXISTS last_seen_at DOUBLE PRECISION;
