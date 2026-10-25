"""Canal Telegram du noyau pour la bêta multi-locataire (un bot partagé, docs/TENANCY.md §2) : appairage d'un compte
par `/start <code>`, puis chaque message tourne dans le locataire du compte. Remplace, pour les testeurs, la gateway
amont du canari (qui ne transmet aucune identité)."""
