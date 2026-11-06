"""Catalogue du Guide (docs/GUIDE.md) : UNE source de vérité pour « quelles fonctions existent, à quoi elles servent,
comment les activer ». Une entrée par fonction ; le web traduit d'après l'`id` et les codes (`screen`, `ui`), le noyau
ne rend au client que des codes. Les textes français (`name_fr`, `purpose_fr`, `example_fr`, `how_fr`) ne vont qu'au
modèle du Guide (rendu compact, `render_for_model`), jamais dans le préfixe d'un tour ordinaire.

Anti-dérive : `tests/unit/guide/test_catalog.py` casse si un drapeau de `NokkerConfig` n'a ni entrée ni exclusion
explicite (`EXCLUDED`), ou si une entrée cite un drapeau qui n'existe pas.

Drapeaux (`flags`) : nom d'un champ de la config (vrai si la valeur est vraie : booléen, dict non vide, texte non vide)
ou chemin pointé dans un dict (`slack.enabled`, `local.lsp.enabled`). Tous doivent être vrais.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Où la fonction existe : `cloud` (nokker.ai), `lokkal` (machine de la personne).
CLOUD, LOKKAL = "cloud", "lokkal"
BOTH = (CLOUD, LOKKAL)

# Écrans du web (`ViewId` de nokker-apps `apps/web/src/nav/views.ts`) et cibles du mode « montre-moi » (`point_at.ui`).
SCREENS = ("chat", "crew", "incidents", "plan", "team", "household", "packs", "code", "connectors", "account", "machine",
           "learning", "house", "folder")
UI_TARGETS = ("composer", "attach", "send", "new_session", "sessions", "account", "bubbles")

# Qui fait la fonction (notes de fonctions, `nokker/guide/notes.py`, drapeau `feature_notes`).
WHO_VOICE, WHO_TEAM, WHO_GUIDE, WHO_CARD = "voix", "équipe via delegate", "Guide", "carte de la personne"
WHO = (WHO_VOICE, WHO_TEAM, WHO_GUIDE, WHO_CARD)

# Profils du tour d'accueil.
SOLO, HOUSEHOLD = "solo", "household"
PROFILES = (SOLO, HOUSEHOLD, LOKKAL)


@dataclass(frozen=True)
class Feature:
    id: str
    name_fr: str
    name_en: str
    purpose_fr: str  # à quoi ça sert, une phrase
    example_fr: str  # exemple de demande
    how_fr: str  # comment l'activer / s'en servir, une phrase
    flags: tuple[str, ...] = ()
    targets: tuple[str, ...] = BOTH
    screen: str | None = None  # écran du web où ça se règle ou se voit (lien profond)
    ui: str | None = None  # élément de l'interface à montrer (`point_at`)
    command: str | None = None  # commande du terminal ou de Lokkal
    # Fait du compte qu'il faut en plus : `household` (membre d'un foyer), `telegram` (Telegram relié). Allumé côté
    # serveur mais absent pour ce compte : statut `setup` (« à activer »), avec le geste.
    needs: tuple[str, ...] = ()
    adult_only: bool = False  # refusé à un profil mineur du foyer
    profiles: tuple[str, ...] = (SOLO, HOUSEHOLD, LOKKAL)  # profils du tour d'accueil
    intro_rank: int = 100  # ordre dans le tour d'accueil (petit = d'abord)
    signals: tuple[str, ...] = ()  # outils ou événements qui comptent comme un usage
    extra: dict[str, Any] = field(default_factory=dict)
    # Notes de fonctions (drapeau `feature_notes`, `nokker/guide/notes.py`) : textes vus par le modèle, relus avant allumage.
    # Vides : la ligne n'est pas rendue (fonction pas encore décrite, jamais inventée).
    when_fr: str = ""  # quand s'en servir, une phrase
    who: tuple[str, ...] = ()  # qui la fait (WHO)
    tools_how: str = ""  # outils, arguments clés, ordre
    never: str = ""  # interdits
    sees_fr: str = ""  # ce que la personne voit à l'écran


F = Feature

CATALOG: tuple[Feature, ...] = (
    # ── conversation ──
    F("web_search", "Recherche web", "Web search", "Chercher sur le web et lire des pages, avec les sources.",
      "Trouve les horaires d'ouverture de la piscine et donne la source.", "Rien à activer : demande-le dans le fil.",
      flags=("web_search_enabled",), ui="composer", intro_rank=10, signals=("web_search", "web_fetch")),
    F("images", "Images jointes", "Attached images", "Joindre une photo ou une capture et la faire lire.",
      "Voici une photo du tableau : recopie les dates.", "Bouton trombone du fil, ou glisser l'image.",
      flags=("images_dir",), ui="attach", intro_rank=20, signals=("image.attached",)),
    F("text_attachments", "Documents joints", "Attached documents", "Joindre un texte, un PDF ou un tableau à lire.",
      "Résume ce PDF en cinq points.", "Bouton trombone du fil.", flags=("text_attachments_enabled",), ui="attach",
      intro_rank=25),
    F("previews", "Aperçus de liens", "Link previews", "Voir l'aperçu d'une page citée dans une réponse.",
      "Donne-moi trois articles sur le sujet.", "Rien à activer : les liens des réponses ont leur aperçu.",
      flags=("previews_enabled",), intro_rank=90),
    F("steer", "Corriger en cours de route", "Steer a running turn",
      "Ajouter une consigne pendant que l'agent travaille, sans l'arrêter.", "Ah, et seulement les offres en Suisse.",
      "Écris dans le fil pendant que l'agent travaille.", flags=("steer_enabled",), ui="composer", intro_rank=60),
    F("bubbles", "Bulles", "Bubbles", "Montrer une page (profil, article, site) dans une carte d'aperçu qui reste dans le fil.",
      "Montre-moi le profil Instagram de l'artiste.", "Demande-le dans le fil ; les bulles se déplacent d'un fil à l'autre.",
      flags=("bubbles_enabled",), ui="bubbles", intro_rank=30, signals=("bubble_show", "bubble_update")),
    F("show_me", "Montre-moi", "Show me", "L'agent montre où cliquer au lieu de le faire à ta place.",
      "Montre-moi où joindre un fichier.", "Demande « montre-moi » dans le fil.", flags=("show_me",), intro_rank=50,
      signals=("point_at",)),
    F("who_does_what", "Qui fait quoi", "Who does what", "Voir d'un coup d'œil ce que font tes agents et ce qui attend.",
      "Qu'est-ce qui attend mon approbation ?", "Panneau « Qui fait quoi » à côté du fil.", flags=("who_does_what",),
      screen="chat", intro_rank=70),
    # ── sécurité et approbations ──
    F("approvals", "Cartes d'approbation", "Approval cards",
      "Rien d'important ne part sans ton accord : envoi, achat, suppression.", "Prépare le mail, je validerai.",
      "Rien à activer : la carte apparaît dans le fil quand il faut décider.", flags=("approvals_enabled",),
      screen="chat", intro_rank=15, signals=("approval.requested",)),
    F("telegram", "Telegram", "Telegram", "Recevoir les cartes et répondre depuis Telegram.",
      "Préviens-moi sur Telegram quand c'est prêt.", "Mon compte → relier Telegram (code d'appairage).",
      flags=("approvals_telegram_enabled",), targets=(CLOUD,), screen="account", ui="account", needs=("telegram",),
      intro_rank=40),
    F("auto_review", "Relecture automatique", "Automatic review",
      "Les cartes sans risque sont relues et validées seules, selon tes règles.", "Quelles cartes ont été validées seules ?",
      "Réglé par l'installation ; tes règles se voient dans Apprentissage.", flags=("auto_review.enabled",),
      screen="learning", intro_rank=95),
    F("approval_suggestions", "Règles suggérées", "Suggested rules",
      "Après des approbations répétées, proposer une règle pour ne plus redemander.", "Arrête de me demander pour ça.",
      "Écran Apprentissage → règles suggérées.", flags=("approval_suggestions.enabled",), screen="learning",
      intro_rank=80),
    # ── agents ──
    F("subagents", "Sous-agents", "Sub-agents", "Confier un travail technique ou une analyse à l'équipe, qui rend un compte rendu.",
      "Compare les offres des trois opérateurs internet pour la maison.", "Demande-le dans le fil.",
      intro_rank=75, signals=("delegate",)),
    F("delegate_fork", "Sous-agent qui reprend le fil", "Forked sub-agent",
      "Un sous-agent qui part avec tout le contexte du fil.", "Explore l'autre piste avec tout ce qu'on a dit.",
      "Demande-le dans le fil.", flags=("delegate_fork",), intro_rank=85),
    F("crew", "Crew (agents récurrents)", "Crew (recurring agents)",
      "Des agents qui travaillent seuls à heure fixe : veille, brief du matin.", "Chaque lundi, fais-moi un point sur mes mails.",
      "Écran Crew → nouvel agent, ou demande-le dans le fil.", flags=("agents_enabled",), screen="crew",
      intro_rank=35, signals=("agent_draft",)),
    F("goals", "Objectifs", "Goals", "Fixer un but : l'agent continue jusqu'à ce qu'un correcteur le juge atteint.",
      "/goal Le dossier de candidature est complet", "Tape /goal suivi du but dans le fil.", flags=("goals_enabled",),
      command="/goal", intro_rank=55),
    F("rubric", "Critères de réussite", "Success criteria",
      "Donner des critères vérifiables ; une réponse est notée et reprise si elle ne les remplit pas.",
      "La lettre doit tenir sur une page et citer l'annonce.", "Donne les critères dans le fil, puis /goal valide.",
      flags=("outcomes_enabled",), intro_rank=88, signals=("rubric_propose",)),
    F("advisor", "Conseiller", "Advisor", "Demander l'avis d'un modèle plus fort sur un point difficile.",
      "Demande un second avis sur cette architecture.", "Réglé par l'installation ; demande-le dans le fil.",
      flags=("advise.enabled",), intro_rank=92),
    F("plan", "Plan", "Plan", "Découper un projet en lots et tâches suivis.", "Fais-moi un plan pour le déménagement.",
      "Écran Plan.", flags=("plan",), screen="plan", intro_rank=65, signals=("plan_get", "plan_propose_split")),
    F("incidents", "Incidents", "Incidents", "Suivre les alertes et incidents de tes services.",
      "Qu'est-ce qui est tombé cette nuit ?", "Écran Incidents.", flags=("operate",), screen="incidents",
      intro_rank=98),
    F("code", "Sessions de code", "Code sessions", "Travailler sur un dépôt de code dans un bac à sable.",
      "Ouvre une session sur mon dépôt et corrige le test qui casse.", "Écran Code → nouvelle session.",
      flags=("code.enabled",), screen="code", intro_rank=45),
    F("execute_code", "Exécuter du code", "Run code", "Faire calculer ou transformer des données par un script.",
      "Calcule la moyenne de ces relevés.", "Demande-le dans le fil.", flags=("execute_code_enabled",),
      intro_rank=86, signals=("execute_code",)),
    F("packs", "Packs d'agents", "Agent packs", "Ajouter des agents et compétences tout prêts.",
      "Quels packs existent pour la compta ?", "Menu du compte → Packs.", flags=("packs_web",), targets=(CLOUD,),
      screen="packs", intro_rank=58, signals=("skill_list", "skill_view")),
    # ── mémoire ──
    F("long_memory", "Mémoire", "Memory", "Se souvenir de toi d'une conversation à l'autre.",
      "Retiens que je suis végétarien.", "Rien à activer : dis-le simplement.", flags=("tenant_long_memory",),
      intro_rank=12, signals=("memory_search", "nokker_memory_search", "memory_note", "memory")),
    F("learning", "Apprentissage", "Learning", "Voir et corriger ce que l'agent a appris de toi.",
      "Qu'as-tu appris sur moi cette semaine ?", "Écran Apprentissage.", flags=("learning",), screen="learning",
      intro_rank=62),
    F("memory_pending", "Notes à valider", "Notes to review", "Valider une note avant qu'elle entre en mémoire.",
      "Montre-moi les notes en attente.", "Écran Apprentissage → en attente.", flags=("memory_pending",),
      screen="learning", intro_rank=82),
    F("memory_consolidation", "Rangement de la mémoire", "Memory tidy-up",
      "Chaque nuit, la mémoire est rangée : doublons fusionnés, notes périmées signalées.",
      "Qu'est-ce qui a été rangé cette nuit ?", "Rien à activer.", flags=("memory_consolidation",),
      screen="learning", intro_rank=96),
    # ── comptes, équipe, foyer ──
    F("connectors", "Mail et agenda", "Mail and calendar", "Lire, trier et préparer tes mails ; gérer ton agenda.",
      "Qu'est-ce que j'ai reçu d'important aujourd'hui ?", "Menu du compte → Connecteurs.", targets=(CLOUD,),
      screen="connectors", intro_rank=18,
      signals=("mail_search", "mail_read", "mail_draft", "mail_send", "calendar_events", "calendar_create")),
    F("team", "Équipe", "Team", "Inviter des collègues et partager des fils et des agents.", "Invite Bob dans l'équipe.",
      "Menu du compte → Équipe.", flags=("team",), screen="team", profiles=(SOLO,), intro_rank=52),
    F("sso", "Connexion d'entreprise", "Company sign-in", "Se connecter avec le compte de l'entreprise.",
      "Comment brancher la connexion de mon entreprise ?", "Menu du compte → Équipe → connexion.", flags=("sso",),
      screen="team", intro_rank=99),
    F("household", "Foyer", "Household", "Partager l'assistant avec ta famille, chacun son espace.",
      "Ajoute ma sœur au foyer.", "Menu du compte → Foyer → inviter.", flags=("household",), screen="household",
      profiles=(SOLO, HOUSEHOLD), intro_rank=22),
    F("household_minors", "Profils enfants", "Child profiles", "Un profil encadré pour un enfant, avec liste blanche.",
      "Crée un profil pour mon fils de 12 ans.", "Foyer → ajouter un enfant.", flags=("household", "household_minors"),
      screen="household", needs=("household",), adult_only=True, profiles=(HOUSEHOLD,), intro_rank=48),
    F("household_paper", "Papiers du foyer", "Household paperwork",
      "Classer factures et courriers, préparer les paiements et la déclaration.", "Range cette facture et rappelle-moi l'échéance.",
      "Joins le document dans le fil.", flags=("household_paper",), targets=(CLOUD,), ui="attach", adult_only=True,
      profiles=(HOUSEHOLD, SOLO), intro_rank=28, signals=("paper_file", "paper_read", "paper_list", "paper_bill_due")),
    F("family_calendar", "Calendrier familial", "Family calendar",
      "Lire un programme (photo, PDF) et proposer les dates dans l'agenda.", "Voici le planning du foot : ajoute les matchs.",
      "Joins la photo ou le PDF dans le fil.", flags=("family_calendar",), targets=(CLOUD,), ui="attach",
      profiles=(HOUSEHOLD,), intro_rank=26, signals=("family_calendar_propose",)),
    F("payments", "Paiements encadrés", "Guarded payments",
      "Laisser l'agent acheter dans un budget, chaque achat confirmé.", "Commande les cartouches d'encre habituelles.",
      "Mon compte → paiements (budget et clé d'accès).", flags=("payments",), targets=(CLOUD,), screen="account",
      adult_only=True, intro_rank=68, signals=("pay_request",)),
    F("house_room", "Salle Maison", "House room", "Un salon commun où les agents du foyer répondent à tous.",
      "Qui va chercher les enfants mercredi ?", "Écran Maison.", flags=("house_room", "household", "team"),
      screen="house", needs=("household",), profiles=(HOUSEHOLD,), intro_rank=32, signals=("ask_house", "memoire_foyer")),
    F("billing", "Abonnement", "Subscription", "Voir ton offre, ta consommation et changer de formule.",
      "Combien de tours me reste-t-il cette semaine ?", "Mon compte → abonnement.", flags=("billing_enabled",),
      targets=(CLOUD,), screen="account", ui="account", adult_only=True, intro_rank=97),
    F("cloud_byok", "Mon propre modèle", "Bring your own model",
      "Brancher ta propre inférence compatible OpenAI au lieu du modèle de Nokker (formules qui l'incluent).",
      "Comment j'utilise ma clé OpenAI avec Nokker ?", "Mon compte → Modèles.", flags=("cloud_byok.enabled",),
      targets=(CLOUD,), screen="account", ui="account", adult_only=True, intro_rank=98),
    # ── canaux ──
    F("slack", "Slack", "Slack", "Parler à l'agent depuis Slack.", "Réponds-moi dans Slack.",
      "Mon compte → relier Slack.", flags=("slack.enabled",), screen="account", intro_rank=78),
    F("whatsapp", "WhatsApp", "WhatsApp", "Parler à l'agent depuis WhatsApp.", "Écris-moi sur WhatsApp.",
      "Mon compte → relier WhatsApp.", flags=("whatsapp.enabled",), screen="account", intro_rank=77),
    F("voice", "Voix", "Voice", "Faire lire une réponse à voix haute.", "Lis-moi la réponse.",
      "Bouton haut-parleur d'une réponse.", flags=("tts.enabled",), intro_rank=72),
    F("speak_lexicon", "Prononciation", "Pronunciation", "Apprendre à la voix comment prononcer les noms du foyer.",
      "Le nom de notre rue se prononce « Ché-min ».", "Foyer → prononciation.", flags=("speak_lexicon.enabled",),
      screen="household", profiles=(HOUSEHOLD, LOKKAL), intro_rank=93),
    F("glance", "Coup d'œil", "Glance", "Une vue très courte pour montre ou lunettes : ce qui attend, le prochain rendez-vous.",
      "Qu'est-ce que je vois sur ma montre ?", "Mon compte → coup d'œil.", flags=("ambient.glance.enabled",),
      screen="account", intro_rank=87),
    F("eink", "Écrans e-ink", "E-ink screens", "Afficher un résumé sur un écran e-ink de la maison.",
      "Affiche le menu de la semaine sur l'écran de la cuisine.", "Réglé par l'installation.",
      flags=("ambient.eink.enabled",), profiles=(HOUSEHOLD, LOKKAL), intro_rank=89),
    # ── Lokkal (la machine de la personne) ──
    F("lokkal_link", "Machine reliée", "Linked machine", "Relier ta machine Lokkal à nokker.ai et passer de l'une à l'autre.",
      "Lance ça sur ma machine.", "Écran Machine, ou Mon compte → relier une machine.", flags=("cloud_link",),
      screen="machine", intro_rank=66),
    F("folder_mode", "Mode dossier", "Folder mode", "Faire traiter tout un dossier de fichiers d'un coup.",
      "Renomme toutes les factures de ce dossier par date.", "Écran Dossier.", flags=("local.folder_mode",),
      targets=(LOKKAL,), screen="folder", profiles=(LOKKAL,), intro_rank=24),
    F("lsp", "Diagnostics de code", "Code diagnostics", "Voir les erreurs du code juste après une modification.",
      "Corrige les erreurs de types.", "Réglage local.lsp.enabled ; commande /diagnostics du terminal.",
      flags=("local.lsp.enabled",), targets=(LOKKAL,), command="/diagnostics", profiles=(LOKKAL,), intro_rank=64,
      signals=("lsp_diagnostics",)),
    F("project_skills", "Compétences du projet", "Project skills",
      "L'agent suit les consignes et compétences rangées dans ton projet.", "Applique notre guide de style.",
      "Réglage local.project_skills ; fichiers du dossier .nokker.", flags=("local.project_skills",), targets=(LOKKAL,),
      profiles=(LOKKAL,), intro_rank=54),
    F("import_codex", "Import de mémoire", "Memory import", "Reprendre la mémoire d'un autre assistant de code.",
      "Reprends mes notes de l'ancien outil.", "Commande « nokker import codex ».",
      flags=("local.import_codex_memory",), targets=(LOKKAL,), command="nokker import", profiles=(LOKKAL,),
      intro_rank=74),
    F("cast", "Écrans du foyer", "Home screens", "Afficher une page sur la télé ou un écran de la maison.",
      "Mets la recette sur la télé du salon.", "Réglage local.cast et profil home de l'installation.",
      flags=("local.cast.enabled",), targets=(LOKKAL,), profiles=(LOKKAL,), intro_rank=44,
      signals=("show_on_screen", "list_screens")),
    F("home_assistant", "Maison connectée", "Smart home", "Piloter lumières, chauffage et appareils de la maison.",
      "Baisse le chauffage du salon à 19 degrés.", "Réglage home_assistant et profil home de l'installation.",
      flags=("home_assistant.enabled",), targets=(LOKKAL,), profiles=(LOKKAL,), intro_rank=34,
      signals=("maison", "maison_action_forte")),
    F("home_watch", "Veille du foyer", "Home watch", "Surveiller caméras, réseau et stockage de la maison.",
      "Il s'est passé quoi devant la porte cette nuit ?", "Réglage home_watch et profil home de l'installation.",
      flags=("home_watch.enabled",), targets=(LOKKAL,), adult_only=True, profiles=(LOKKAL,), intro_rank=46,
      signals=("veille_cameras", "veille_reseau", "veille_nas")),
    F("room_audio", "Voix dans la pièce", "Room audio", "L'agent annonce un message sur les enceintes d'une pièce.",
      "Annonce dans la cuisine que le repas est prêt.", "Réglage ambient.room_audio et profil home.",
      flags=("ambient.room_audio.enabled",), targets=(LOKKAL,), profiles=(LOKKAL,), intro_rank=76),
    F("knock", "Heurtoir", "Knocker", "Frapper « toc toc toc » pour appeler l'agent, frapper pour approuver.",
      "Comment j'approuve en frappant ?", "Réglage knock.enabled et micro de l'installation.", flags=("knock.enabled",),
      targets=(LOKKAL,), profiles=(LOKKAL,), intro_rank=84),
)

# Drapeaux de la config qui ne sont PAS des fonctions à présenter (réglages techniques, garde-fous, exploitation).
# Toute exclusion porte sa raison : un nouveau drapeau doit être ajouté ici ou au catalogue (test anti-dérive).
# Notes de fonctions (drapeau `feature_notes`) : `when_fr`, `who`, `tools_how`, `never` des fonctions déjà décrites,
# d'après leurs outils réels (schémas du registre) et leurs règles existantes. Une fonction absente d'ici n'a pas ces
# lignes (jamais inventées) : `tests/unit/guide/test_feature_notes.py` liste celles qui restent à décrire.
_NOTES: dict[str, dict[str, Any]] = {
    "web_search": dict(
        when_fr="La réponse demande un fait récent ou une source vérifiable.", who=(WHO_VOICE,),
        tools_how="web_search(query) pour trouver, puis web_fetch(urls) pour lire les pages retenues ; citer les sources.",
        never="Jamais présenter comme vérifié ce qui n'a pas été lu dans une source."),
    "bubbles": dict(
        sees_fr="une carte d'aperçu cliquable (image, titre, description) dans le fil.",
        when_fr="La personne demande d'afficher ou de montrer une page (profil, article, site).", who=(WHO_VOICE,),
        tools_how="bubble_show(url) ouvre la bulle, le noyau remplit la carte ; bubble_update(bubble_id) ne met à jour "
                  "qu'une bulle existante du fil courant.",
        never="Jamais de bubble_update ni de navigation juste après bubble_show."),
    "show_me": dict(
        sees_fr="un repère sur l'élément de l'interface, avec la légende.",
        when_fr="La personne demande où cliquer ou comment faire dans l'interface.", who=(WHO_VOICE,),
        tools_how="point_at(caption, ui) montre l'élément de l'interface ; caption dit le geste à faire.",
        never="Jamais faire le geste à la place de la personne : on montre, elle fait."),
    "subagents": dict(
        when_fr="Un travail technique ou une analyse doit être confié et suivi jusqu'au compte rendu.", who=(WHO_TEAM,),
        tools_how="delegate(tasks=[{goal, context}]) ; avant l'appel, dire en une phrase ce qui est confié et qu'on "
                  "attend la réponse ; ensuite transmettre le compte rendu fidèlement en disant qui a répondu.",
        never="Jamais conclure avant le compte rendu ; jamais déléguer un paiement ni une question simple à laquelle "
              "la voix sait répondre."),
    "approvals": dict(
        sees_fr="une carte dans le fil, à approuver ou refuser.",
        when_fr="Une action forte ou sensible attend l'accord de la personne.", who=(WHO_CARD,),
        tools_how="Le noyau pose la carte quand l'outil l'exige ; l'outil répond EN ATTENTE : dire ce qu'on attend et "
                  "terminer le tour.",
        never="Jamais rappeler l'outil ni escalader pendant qu'une carte attend."),
    "payments": dict(
        sees_fr="une carte « à payer » (marchand, montant, articles), approuvée avec sa passkey.",
        when_fr="Une demande mène à payer : achat, facture, abonnement.", who=(WHO_VOICE, WHO_CARD),
        tools_how="pay_request(merchant, amount, currency, items, url) prépare la carte « à payer » ; la carte EST la "
                  "confirmation : appeler l'outil plutôt que demander en texte. La personne approuve avec sa passkey.",
        never="Jamais saisir un numéro de carte ; jamais confier un paiement à un sous-agent."),
    "crew": dict(
        sees_fr="une carte de création d'agent, qu'elle active.",
        when_fr="La personne veut un agent qui revient régulièrement (veille, rapport).", who=(WHO_VOICE, WHO_CARD),
        tools_how="agent_draft(name, role, instructions) propose la carte de l'agent ; la personne l'active.",
        never=""),
    "rubric": dict(
        when_fr="Un objectif mérite des critères de réussite vérifiables.", who=(WHO_VOICE,),
        tools_how="rubric_propose(criteria, objective) propose les critères à la personne.", never=""),
    "code": dict(
        when_fr="Un travail porte sur un dépôt de code, dans une session de code.", who=(WHO_VOICE, WHO_TEAM),
        tools_how="Dans le fil de la session : lire avec read_file et search_files ; faire construire avec delegate "
                  "(un worktree par sous-agent).",
        never="Jamais modifier un fichier soi-même : la voix lit, l'équipe construit ; aucun outil de code hors d'un "
              "fil de session de code."),
    "connectors": dict(
        sees_fr="pour un envoi, une carte d'approbation avant que le mail parte.",
        when_fr="La demande porte sur le mail ou l'agenda de la personne.", who=(WHO_VOICE, WHO_CARD),
        tools_how="Lire avec mail_search et mail_read, l'agenda avec calendar_events ; préparer avec mail_draft ; "
                  "créer ou modifier un événement avec calendar_create et calendar_update quand ces outils sont "
                  "dans ta liste (sinon l'agenda est en lecture seule : dis-le) ; mail_send et calendar_delete "
                  "posent une carte.",
        never="Jamais d'envoi externe ni de suppression sans la carte de la personne."),
}


def _with_notes(f: Feature) -> Feature:
    from dataclasses import replace

    return replace(f, **_NOTES[f.id]) if f.id in _NOTES else f


CATALOG = tuple(_with_notes(f) for f in CATALOG)

EXCLUDED: dict[str, str] = {
    "judge_enabled": "porte du juge (couche 2), réglage interne",
    "judge_explicit": "posé par le chargement de la config",
    "judge_review": "relecture du juge, réglage interne",
    "judge_advice": "avis du juge sur les cartes, réglage interne",
    "dev_open_api": "développement seulement",
    "internal_tenant": "exploitation (locataire interne)",
    "traces_record": "traces pour l'amélioration, exploitation",
    "require_rls": "garde-fou de la base",
    "admin_enabled": "console d'administration (exploitation)",
    "admin_candidatures": "module candidatures de la console (exploitation)",
    "judge_require_explicit": "garde de démarrage du juge (exploitation)",
    "delegate_tier": "palier et lecture seule des sous-agents (réglage interne, canari)",
    "pay_agent": "agent de paiement : entrée au catalogue à son ouverture (allumage sur décision produit)",
    "essentials_v2": "pack Essentiels vague 2 (docs/ESSENTIALS.md) : entrée au catalogue à son allumage",
    "essentials": "pack Essentiels (docs/ESSENTIALS.md) : entrée au catalogue à son allumage, textes en relecture",
    "pay_tool_choice": "texte de l'outil pay_request (réglage interne, mesuré au banc du palier)",
    "card_mask_notice": "textes du masquage des cartes pour le modèle (réglage interne ; le masquage est toujours actif)",
    "approval_bubbles": "bulle de la carte dans le fil (affichage des cartes existantes, réglage interne)",
    "code_tools_session_only": "outils des sessions de code offerts dans leurs fils seulement (réglage interne)",
    "voice_read_only": "la voix lit, ses sous-agents écrivent (réglage interne)",
    "delegate_router": "routeur des sous-agents du harnais (réglage interne)",
    "feature_notes": "notes des fonctions pour le modèle (réglage interne)",
    "reply_language": "consigne de langue du préfixe (réglage interne, mesuré au banc du palier)",
    "reply_language_tail": "ligne de langue avant le message (réglage interne, mesuré au banc du palier)",
    "route_relay_voice": "routage des résumés et relais sur le palier de la voix (réglage interne, banc du palier)",
    "billing_live": "mode réel de la facturation (exploitation)",
    "oauth_providers": "connecteurs OAuth (exploitation : clients Microsoft et Google ; se voit dans Connecteurs, entrée connecteurs)",
    "vouchers_enabled": "vouchers d'essai (exploitation ; l'essai se voit dans Mon compte, entrée billing_enabled)",
    "voucher_grants_invite": "un code de campagne sert d'invitation (exploitation, parcours d'inscription)",
    "mcp_modern": "transport MCP, technique",
    "mcp_apps": "apps MCP des serveurs du locataire interne seulement",
    "mcp_tool_servers": "serveurs MCP du locataire interne (un testeur passe par ses connecteurs)",
    "memory_search_api": "API de recherche de la mémoire, technique",
    "self_improve": "amélioration nocturne du système, exploitation",
    "context_enabled": "moteur de contexte, technique",
    "learning_nudges": "relances de l'apprentissage, sans geste de la personne",
    "provenance": "garde-fou de provenance, rien à activer par la personne",
    "ssrf": "garde-fou réseau",
    "route": "routage des paliers, technique",
    "memory_direct": "branchement de la mémoire, technique",
    "tenant_limits": "quotas de l'offre (voir abonnement)",
    "effect_overrides": "table des effets, technique",
    "telegram_ids": "identités de l'instance interne",
    "context_budgets": "budgets du moteur de contexte",
    "context_corthexis": "branchement de la mémoire, technique",
    "session_thread_by_sender": "routage des fils, technique",
    "tiers": "paliers de modèles, technique",
    "execute_code": "réglages de execute_code_enabled",
    "admin_operators": "console d'administration (exploitation)",
    "local": "machine Lokkal : ses fonctions sont décrites par leurs clés (local.*)",
    "guide": "le Guide lui-même",
}

# Suffixes de champs qui sont des réglages d'une fonction déjà décrite, jamais des drapeaux.
SETTINGS_SUFFIXES = ("_settings", "_budget", "_budgets", "_plans", "_prices")


def by_id() -> dict[str, Feature]:
    return {f.id: f for f in CATALOG}


def flag_value(config: Any, flag: str) -> Any:
    """Valeur d'un drapeau : champ de la config, ou chemin pointé dans un dict (`slack.enabled`). `images_dir` : le
    magasin d'images réellement servi (`images_root`, Lokkal compris)."""
    if flag == "images_dir" and hasattr(config, "images_root"):
        return config.images_root()
    head, _, rest = flag.partition(".")
    value = getattr(config, head, None)
    for part in rest.split(".") if rest else ():
        value = value.get(part) if isinstance(value, dict) else None
    return value


def flag_on(config: Any, flag: str) -> bool:
    v = flag_value(config, flag)
    if isinstance(v, str):
        return bool(v.strip())
    return bool(v)


def render_for_model(entries: list[tuple[Feature, str]]) -> str:
    """Catalogue compact pour le modèle du Guide : une ligne par fonction et son statut pour CE compte. Textes en
    français, sans code d'écran (le noyau les ajoute à la réponse d'après les identifiants cités)."""
    from nokker.guide.texts import CATALOG_LINE, CATALOG_LINE_OFF, STATUS_FR

    lines = []
    for f, status in entries:
        if status in ("off", "not_for_you"):
            lines.append(CATALOG_LINE_OFF.format(id=f.id, name=f.name_fr, status=STATUS_FR[status]))
        else:
            lines.append(CATALOG_LINE.format(id=f.id, name=f.name_fr, status=STATUS_FR[status], purpose=f.purpose_fr,
                                             example=f.example_fr, how=f.how_fr))
    return "\n".join(lines)


__all__ = [
    "BOTH",
    "CATALOG",
    "CLOUD",
    "EXCLUDED",
    "HOUSEHOLD",
    "LOKKAL",
    "PROFILES",
    "SCREENS",
    "SETTINGS_SUFFIXES",
    "SOLO",
    "UI_TARGETS",
    "Feature",
    "by_id",
    "flag_on",
    "flag_value",
    "render_for_model",
]
