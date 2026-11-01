"""Canaux de messagerie du noyau au même niveau que Telegram (docs/CHANNELS.md) : Slack (Events API) et WhatsApp
(Business Cloud API). Chacun est un adaptateur propre au noyau, sans la passerelle amont : webhook signé, appairage
d'une identité de canal à un compte, un tour par message dans le locataire du compte, cartes d'approbation en LIEN
vers l'écran web (aucune carte ne se décide dans un canal). Désactivés par défaut (blocs `slack:` / `whatsapp:`)."""
