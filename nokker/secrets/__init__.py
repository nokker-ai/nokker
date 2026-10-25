"""Coffre local de Nokker Lokkal (lot X4) : interface `SecretsProvider`, fournisseurs `file` et `openbao`,
références `secret:NOM`, caviardage de toute valeur connue. Contrat : docs/en/secrets.md, docs/LOKKAL.md §7.10.

(Paquet `nokker.secrets` : toujours importé par son nom complet ; `import secrets` reste le module standard.)
"""
from nokker.secrets.base import SecretInfo, SecretsError, SecretsProvider, check_name, check_value
from nokker.secrets.refs import REF_PREFIX, SecretResolver, ref_name, refs_in
from nokker.secrets.vault import Vault, VaultKnownSecrets, chosen_provider

__all__ = ["REF_PREFIX", "SecretInfo", "SecretResolver", "SecretsError", "SecretsProvider", "Vault",
           "VaultKnownSecrets", "check_name", "check_value", "chosen_provider", "ref_name", "refs_in"]
