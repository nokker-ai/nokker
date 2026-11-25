"""Textes vus du MODÈLE produits par les outils « natifs » et les modules métier (suite de `model_texts.py`, lot
`turn-lang-5`) : mêmes règles — français figé mot pour mot, tutoiement, noms d'outils inchangés, langue du tour par
`pick(table)` — et même inscription dans `ALL_TABLES` (le test de garde et les empreintes les couvrent). Ce module est
importé à la fin de `model_texts.py` ; les clés sont « domaine.nom ».
"""
from __future__ import annotations

from nokker.core.model_texts import _t

# ═══ Fichiers et terminal (`tools/code.py`) ═════════════════════════════════════════════════════════════════════════════
CODE_OUTSIDE_ROOT = _t(
    "code.outside_root", "{path} sort du dossier de travail", "{path} is outside the working folder",
    "{path} liegt außerhalb des Arbeitsordners", "{path} esce dalla cartella di lavoro")
UNKNOWN_TOOL_BARE = _t("tool.unknown_tool_bare", "outil inconnu : {name}", "unknown tool: {name}",
                       "unbekanntes Werkzeug: {name}", "strumento sconosciuto: {name}")
CODE_REPLACE_ARGS = _t(
    "code.replace_args", "mode replace : path, old_string et new_string sont requis",
    "mode replace: path, old_string and new_string are required",
    "Modus replace: path, old_string und new_string sind erforderlich",
    "modalità replace: path, old_string e new_string sono richiesti")
CODE_UNKNOWN_MODE = _t("code.unknown_mode", "mode inconnu : {mode} (replace | patch)", "unknown mode: {mode} (replace | patch)",
                       "unbekannter Modus: {mode} (replace | patch)", "modalità sconosciuta: {mode} (replace | patch)")
CODE_PATHS_LIST = _t("code.paths_list", "paths : liste de chemins attendue", "paths: a list of paths is expected",
                     "paths: Eine Liste von Pfaden wird erwartet", "paths: è attesa una lista di percorsi")
CODE_SEVERITY_UNKNOWN = _t(
    "code.severity_unknown", "severity inconnue : {severity} (error | warning)", "unknown severity: {severity} (error | warning)",
    "unbekannte severity: {severity} (error | warning)", "severity sconosciuta: {severity} (error | warning)")
CODE_PORT_INVALID = _t("code.port_invalid", "port invalide : {port}", "invalid port: {port}", "ungültiger Port: {port}",
                       "porta non valida: {port}")
CODE_PROCESS_HINT = _t(
    "code.process_hint", "la suite : process_logs ; l'arrêter : process_stop ; il s'arrête aussi à la fin du fil",
    "to follow it: process_logs; to stop it: process_stop; it also stops when the thread ends",
    "Fortsetzung: process_logs; zum Stoppen: process_stop; er endet auch mit dem Ende des Gesprächs",
    "per seguirlo: process_logs; per fermarlo: process_stop; si ferma anche alla fine della conversazione")
CODE_PROCESS_HINT_PORT = _t(
    "code.process_hint_port", " ; le port {port} est proposé à l'utilisateur en aperçu privé (ne donne pas d'URL)",
    " ; port {port} is offered to the user as a private preview (do not give a URL)",
    " ; der Port {port} wird dem Nutzer als private Vorschau angeboten (gib keine URL an)",
    " ; la porta {port} viene proposta all'utente come anteprima privata (non dare un URL)")
CODE_EXEC_LOCAL = _t(
    "code.exec_local", "execute_code tourne dans un bac à sable (docker ou kubernetes), jamais sur l'hôte",
    "execute_code runs in a sandbox (docker or kubernetes), never on the host",
    "execute_code läuft in einer Sandbox (docker oder kubernetes), nie auf dem Host",
    "execute_code gira in una sandbox (docker o kubernetes), mai sull'host")
CODE_EXEC_ENV_NAMES = _t(
    "code.exec_env_names", "variables d'environnement par nom : bac à sable docker seulement",
    "environment variables by name: docker sandbox only",
    "Umgebungsvariablen nach Namen: nur in der docker-Sandbox",
    "variabili d'ambiente per nome: solo sandbox docker")

# ═══ Web (`tools/web.py`, `tools/web_fence.py`) ════════════════════════════════════════════════════════════════════════
WEB_NO_URLS = _t(
    "web.no_urls", "aucune URL fournie (paramètre `urls`).", "no URL provided (parameter `urls`).",
    "keine URL angegeben (Parameter `urls`).", "nessun URL fornito (parametro `urls`).")
WEB_SITE_CONNECTED = _t(
    "web.site_connected", "site_connected : « {label} » est un site connecté ; pour ses pages, utilise sites_read site={label}.",
    "site_connected: \"{label}\" is a connected site; to read its pages, use sites_read site={label}.",
    "site_connected: „{label}“ ist eine verbundene Website; für ihre Seiten nutze sites_read site={label}.",
    "site_connected: «{label}» è un sito connesso; per le sue pagine, usa sites_read site={label}.")
WEB_TIMEOUT = _t("web.timeout", "délai dépassé", "timed out", "Zeitüberschreitung", "tempo scaduto")
WEB_FETCH_FAILED = _t("web.fetch_failed", "{i}. {url} — échec : {error}", "{i}. {url} — failed: {error}",
                      "{i}. {url} — fehlgeschlagen: {error}", "{i}. {url} — non riuscito: {error}")
WEB_UNTITLED = _t("web.untitled", "(sans titre)", "(untitled)", "(ohne Titel)", "(senza titolo)")
WEB_PII_EMAIL = _t("web.pii_email", "adresse e-mail", "email address", "E-Mail-Adresse", "indirizzo e-mail")
WEB_PII_PHONE = _t("web.pii_phone", "numéro de téléphone", "phone number", "Telefonnummer", "numero di telefono")
WEB_PII_AVS = _t("web.pii_avs", "numéro AVS", "AVS number", "AHV-Nummer", "numero AVS")
WEB_PII_BLOCKED = _t("web.pii_blocked", "terme bloqué : {term}", "blocked term: {term}", "gesperrter Begriff: {term}",
                     "termine bloccato: {term}")
WEB_FENCE_RESULTS = _t(
    "web.fence_results", "Résultats web pour « {query} » : {n}.", "Web results for \"{query}\": {n}.",
    "Webergebnisse für „{query}“: {n}.", "Risultati web per «{query}»: {n}.")
WEB_FENCE_RESULTS_RAW = _t(
    "web.fence_results_raw", "Résultats web pour « {query} » (texte brut) :", "Web results for \"{query}\" (plain text):",
    "Webergebnisse für „{query}“ (Rohtext):", "Risultati web per «{query}» (testo semplice):")
WEB_FENCE_PAGE = _t(
    "web.fence_page", "Page{where} ({n} caractères) :", "Page{where} ({n} characters):",
    "Seite{where} ({n} Zeichen):", "Pagina{where} ({n} caratteri):")
WEB_FENCE_PAGE_FAILED = _t(
    "web.fence_page_failed", "Page{where} : échec : {error}", "Page{where}: failed: {error}",
    "Seite{where}: fehlgeschlagen: {error}", "Pagina{where}: non riuscita: {error}")
WEB_FENCE_OTHER_FIELDS = _t(
    "web.fence_other_fields", "Autres champs du résultat :", "Other fields of the result:",
    "Weitere Felder des Ergebnisses:", "Altri campi del risultato:")

# ═══ ssh (`tools/ssh.py`) ═════════════════════════════════════════════════════════════════════════════════════════════
SSH_GATE_UNKNOWN_HOST = _t(
    "ssh.gate_unknown_host", "hôte inconnu de l'inventaire : {name} (hôtes : {known})",
    "host unknown to the inventory: {name} (hosts: {known})", "Host im Inventar unbekannt: {name} (Hosts: {known})",
    "host sconosciuto all'inventario: {name} (host: {known})")
SSH_GATE_FIRST_CONTACT = _t(
    "ssh.gate_first_contact",
    "clé d'hôte de {hosts} absente de known_hosts : appelle ssh_trust {{host: {first}}} pour voir son empreinte, "
    "l'utilisateur l'approuve, puis reprends",
    "host key of {hosts} missing from known_hosts: call ssh_trust {{host: {first}}} to see its fingerprint, the user "
    "approves it, then continue",
    "Host-Schlüssel von {hosts} fehlt in known_hosts: Rufe ssh_trust {{host: {first}}} auf, um den Fingerabdruck zu "
    "sehen, der Nutzer gibt ihn frei, dann mach weiter",
    "chiave host di {hosts} assente da known_hosts: chiama ssh_trust {{host: {first}}} per vedere l'impronta, "
    "l'utente la approva, poi riprendi")
TOOL_NONE = _t("tool.none", "aucun", "none", "keine", "nessuno")
SSH_HINT_EGRESS_REFUSED = _t(
    "ssh.hint_egress_refused",
    "la sortie du bac à sable (egress) a refusé : l'adresse ET le port doivent être déclarés dans hosts.yaml, tels "
    "quels (après une modification, ouvre une nouvelle session) ; une adresse de bouclage, de lien local (169.254…) ou "
    "des réseaux de Lokkal reste refusée, même déclarée",
    "the sandbox egress refused: the address AND the port must be declared in hosts.yaml, exactly as is (after a "
    "change, open a new session); a loopback address, a link-local address (169.254…) or Lokkal networks stay refused, "
    "even if declared",
    "Der Ausgang der Sandbox (Egress) hat abgelehnt: Adresse UND Port müssen in hosts.yaml genau so deklariert sein "
    "(nach einer Änderung öffne eine neue Sitzung); eine Loopback-Adresse, eine Link-Local-Adresse (169.254…) oder "
    "Lokkal-Netze bleiben abgelehnt, auch wenn sie deklariert sind",
    "l'uscita della sandbox (egress) ha rifiutato: l'indirizzo E la porta devono essere dichiarati in hosts.yaml, "
    "così come sono (dopo una modifica, apri una nuova sessione); un indirizzo di loopback, di link locale "
    "(169.254…) o le reti di Lokkal restano rifiutati, anche se dichiarati")
SSH_HINT_EGRESS_UNREACHABLE = _t(
    "ssh.hint_egress_unreachable",
    "hôte injoignable depuis la sortie du bac à sable (egress) : nom, adresse, port ou réseau",
    "host unreachable from the sandbox egress: name, address, port or network",
    "Host vom Ausgang der Sandbox (Egress) nicht erreichbar: Name, Adresse, Port oder Netz",
    "host irraggiungibile dall'uscita della sandbox (egress): nome, indirizzo, porta o rete")
SSH_HINT_HOST_KEY = _t(
    "ssh.hint_host_key",
    "la clé présentée ne correspond pas à known_hosts : possible interception. Ne contourne pas ; l'utilisateur "
    "vérifie et corrige known_hosts lui-même (ssh_trust ne remplace jamais une clé)",
    "the presented key does not match known_hosts: possible interception. Do not work around it; the user checks and "
    "fixes known_hosts themselves (ssh_trust never replaces a key)",
    "Der vorgelegte Schlüssel stimmt nicht mit known_hosts überein: mögliches Abfangen. Umgehe das nicht; der Nutzer "
    "prüft und korrigiert known_hosts selbst (ssh_trust ersetzt nie einen Schlüssel)",
    "la chiave presentata non corrisponde a known_hosts: possibile intercettazione. Non aggirarlo; l'utente verifica e "
    "corregge known_hosts da sé (ssh_trust non sostituisce mai una chiave)")
SSH_HINT_AUTH = _t(
    "ssh.hint_auth", "connexion ou authentification ssh en échec (code 255) : hôte, port, utilisateur ou clé au coffre",
    "ssh connection or authentication failed (code 255): host, port, user or key in the vault",
    "ssh-Verbindung oder -Authentifizierung fehlgeschlagen (Code 255): Host, Port, Nutzer oder Schlüssel im Tresor",
    "connessione o autenticazione ssh non riuscita (codice 255): host, porta, utente o chiave nella cassaforte")
SSH_HINT_TIMEOUT = _t("ssh.hint_timeout", "délai dépassé : la commande a été arrêtée",
                      "timed out: the command was stopped", "Zeitüberschreitung: Der Befehl wurde abgebrochen",
                      "tempo scaduto: il comando è stato fermato")
SSH_HINT_UID = _t(
    "ssh.hint_uid",
    "le bac à sable ne connaît pas l'uid de l'utilisateur et le client ssh refuse de tourner : limite connue de "
    "l'image du bac à sable (uid 1000 seulement), à signaler à l'utilisateur",
    "the sandbox does not know the user's uid and the ssh client refuses to run: known limit of the sandbox image "
    "(uid 1000 only), to report to the user",
    "Die Sandbox kennt die uid des Nutzers nicht und der ssh-Client weigert sich zu laufen: bekannte Grenze des "
    "Sandbox-Images (nur uid 1000), melde das dem Nutzer",
    "la sandbox non conosce l'uid dell'utente e il client ssh rifiuta di girare: limite noto dell'immagine della "
    "sandbox (solo uid 1000), da segnalare all'utente")
SSH_REMOTE_PATH_REQUIRED = _t(
    "ssh.remote_path_required", "remote_path est requis", "remote_path is required", "remote_path ist erforderlich",
    "remote_path è richiesto")

# ═══ Points de retour (`tools/checkpoints.py`) ═════════════════════════════════════════════════════════════════════════
CKPT_NO_CHANGE_SINCE_LAST = _t(
    "ckpt.no_change_since_last", "aucun changement depuis le dernier point de retour",
    "no change since the last checkpoint", "keine Änderung seit dem letzten Rücksprungpunkt",
    "nessuna modifica dall'ultimo punto di ripristino")
CKPT_NO_CHANGE = _t("ckpt.no_change", "aucun changement", "no change", "keine Änderung", "nessuna modifica")
CKPT_DIFF_FAILED = _t("ckpt.diff_failed", "diff impossible", "diff not possible", "Diff nicht möglich",
                      "diff impossibile")
CKPT_DELETE_FAILED = _t(
    "ckpt.delete_failed", "suppression de {rel} impossible : {exc}", "cannot delete {rel}: {exc}",
    "Löschen von {rel} nicht möglich: {exc}", "impossibile eliminare {rel}: {exc}")

# ═══ Compétences (`tools/skills.py`) et packs (`tools/packs.py`) ══════════════════════════════════════════════════════
SKILL_CAUTION = _t(
    "skill.caution", "compétence à vérifier avant usage : {summary}", "skill to check before use: {summary}",
    "Skill vor der Nutzung prüfen: {summary}", "skill da verificare prima dell'uso: {summary}")
SKILL_UNSCANNABLE_NOTE = _t(
    "skill.unscannable_note",
    "{n} compétence(s) non fournie(s) masquée(s) : le scanner de sécurité n'est pas disponible, elles ne sont pas "
    "affichées par prudence.",
    "{n} non-bundled skill(s) hidden: the security scanner is not available, they are not shown as a precaution.",
    "{n} nicht mitgelieferte Skill(s) ausgeblendet: Der Sicherheitsscanner ist nicht verfügbar, sie werden "
    "vorsichtshalber nicht angezeigt.",
    "{n} skill non fornite con il programma nascoste: lo scanner di sicurezza non è disponibile, per prudenza non "
    "vengono mostrate.")
SKILL_FILE_BINARY = _t("skill.file_binary", "fichier binaire refusé : {file}", "binary file refused: {file}",
                       "Binärdatei abgelehnt: {file}", "file binario rifiutato: {file}")
SKILL_FILE_NOT_TEXT = _t("skill.file_not_text", "fichier non textuel refusé : {file}", "non-text file refused: {file}",
                         "Nicht-Textdatei abgelehnt: {file}", "file non testuale rifiutato: {file}")
SKILL_NO_SCANNER = _t(
    "skill.no_scanner",
    "installation impossible ici : le scanner de sécurité des compétences n'est pas disponible (rien n'a été installé)",
    "installation not possible here: the skills security scanner is not available (nothing was installed)",
    "Installation hier nicht möglich: Der Sicherheitsscanner für Skills ist nicht verfügbar (nichts wurde installiert)",
    "installazione impossibile qui: lo scanner di sicurezza delle skill non è disponibile (non è stato installato "
    "nulla)")
SKILL_BAD_GITHUB = _t(
    "skill.bad_github", "source github invalide : github:<owner>/<repo>[/<chemin>]",
    "invalid github source: github:<owner>/<repo>[/<path>]", "ungültige github-Quelle: github:<owner>/<repo>[/<Pfad>]",
    "sorgente github non valida: github:<owner>/<repo>[/<percorso>]")
SKILL_BAD_SOURCE = _t(
    "skill.bad_source", "source non reconnue : github:<owner>/<repo>[/<chemin>], URL https, ou official/<famille>/<nom>",
    "unrecognised source: github:<owner>/<repo>[/<path>], https URL, or official/<family>/<name>",
    "Quelle nicht erkannt: github:<owner>/<repo>[/<Pfad>], https-URL oder official/<Familie>/<Name>",
    "sorgente non riconosciuta: github:<owner>/<repo>[/<percorso>], URL https, oppure official/<famiglia>/<nome>")
SKILL_STATE_DIR_MISMATCH = _t(
    "skill.state_dir_mismatch", "installation refusée : le dossier d'état des compétences ne correspond pas à $NOKKER_HOME",
    "installation refused: the skills state folder does not match $NOKKER_HOME",
    "Installation abgelehnt: Der Zustandsordner der Skills stimmt nicht mit $NOKKER_HOME überein",
    "installazione rifiutata: la cartella di stato delle skill non corrisponde a $NOKKER_HOME")
SKILL_REMOVE_SYMLINK = _t(
    "skill.remove_symlink", "retrait refusé : le dossier de la compétence est un lien symbolique",
    "removal refused: the skill folder is a symbolic link",
    "Entfernen abgelehnt: Der Skill-Ordner ist ein symbolischer Link",
    "rimozione rifiutata: la cartella della skill è un collegamento simbolico")
SKILL_REMOVE_OUTSIDE = _t(
    "skill.remove_outside", "retrait refusé : chemin hors de la racine utilisateur",
    "removal refused: path outside the user root", "Entfernen abgelehnt: Pfad außerhalb des Nutzer-Stammverzeichnisses",
    "rimozione rifiutata: percorso fuori dalla radice utente")
PACK_BAD_BIND = _t(
    "pack.bad_bind", "bind : clés « rôle.outil » ou « secret:NOM » seulement ({bad})",
    "bind: only \"role.tool\" or \"secret:NAME\" keys ({bad})",
    "bind: nur Schlüssel „Rolle.Werkzeug“ oder „secret:NAME“ ({bad})",
    "bind: solo chiavi «ruolo.strumento» o «secret:NOME» ({bad})")
PACK_SCAN_REFUSED = _t(
    "pack.scan_refused", "installation REFUSÉE par le scanner (rien n'a été installé).\n",
    "installation REFUSED by the scanner (nothing was installed).\n",
    "Installation vom Scanner ABGELEHNT (nichts wurde installiert).\n",
    "installazione RIFIUTATA dallo scanner (non è stato installato nulla).\n")
PACK_BIND_NOT_OBJECT = _t("pack.bind_not_object", "bind doit être un objet", "bind must be an object",
                          "bind muss ein Objekt sein", "bind deve essere un oggetto")
TOOL_REFUSED = _t("tool.refused", "refusé : {error}", "refused: {error}", "abgelehnt: {error}", "rifiutato: {error}")
PACK_UNKNOWN = _t(
    "pack.unknown", "pack inconnu : {name} (packs : {known})", "unknown pack: {name} (packs: {known})",
    "unbekanntes Pack: {name} (Packs: {known})", "pack sconosciuto: {name} (pack: {known})")
PACK_NOTE_NOTHING_ARMED = _t(
    "pack.note_nothing_armed",
    "aucun agent n'est créé ni armé : chaque carte s'active par l'utilisateur, et armer une écriture demande sa carte forte",
    "no agent is created or armed: each card is activated by the user, and arming a write requires its strong card",
    "Es wird kein Agent angelegt oder scharf gestellt: Jede Karte wird vom Nutzer aktiviert, und das Scharfstellen "
    "eines Schreibzugriffs verlangt seine starke Karte",
    "nessun agente viene creato né armato: ogni scheda viene attivata dall'utente, e armare una scrittura richiede "
    "la sua scheda forte")
PACK_NEXT_PROPOSE = _t(
    "pack.next_propose", "propose chaque modèle avec agent_draft (mêmes arguments), sans rien changer",
    "propose each template with agent_draft (same arguments), without changing anything",
    "Schlage jede Vorlage mit agent_draft vor (gleiche Argumente), ohne etwas zu ändern",
    "proponi ogni modello con agent_draft (stessi argomenti), senza cambiare nulla")
PACK_SETUP_CONNECTORS = _t(
    "pack.setup_connectors", "connecteurs à poser : {names}", "connectors to set up: {names}",
    "einzurichtende Konnektoren: {names}", "connettori da configurare: {names}")
PACK_SETUP_MCP_EFFECTS = _t(
    "pack.setup_mcp_effects",
    "les effets déclarés des outils MCP (jamais plus bas que ceux des outils intégrés) s'appliquent au prochain "
    "redémarrage de Nokker",
    "the declared effects of the MCP tools (never lower than those of the built-in tools) apply at the next restart "
    "of Nokker",
    "Die deklarierten Effekte der MCP-Werkzeuge (nie niedriger als die der integrierten Werkzeuge) gelten ab dem "
    "nächsten Neustart von Nokker",
    "gli effetti dichiarati degli strumenti MCP (mai inferiori a quelli degli strumenti integrati) si applicano al "
    "prossimo riavvio di Nokker")
PACK_REMOVE_KEPT_AGENTS = _t(
    "pack.remove_kept_agents",
    "les agents créés depuis ce pack sont gardés tels quels : la carte propose de les désarmer ou de les supprimer, "
    "l'utilisateur choisit",
    "the agents created from this pack are kept as they are: the card offers to disarm or delete them, the user chooses",
    "Die aus diesem Pack erstellten Agenten bleiben unverändert: Die Karte bietet an, sie unscharf zu stellen oder zu "
    "löschen, der Nutzer entscheidet",
    "gli agenti creati da questo pack restano così come sono: la scheda propone di disarmarli o eliminarli, "
    "l'utente sceglie")
PACK_REMOVE_AGENTS_UNTOUCHED = _t(
    "pack.remove_agents_untouched",
    "les agents déjà créés depuis ce pack ne sont pas touchés : retire-les depuis Crew si l'utilisateur le demande",
    "the agents already created from this pack are not touched: remove them from Crew if the user asks",
    "Die bereits aus diesem Pack erstellten Agenten bleiben unberührt: Entferne sie in Crew, wenn der Nutzer es "
    "verlangt",
    "gli agenti già creati da questo pack non vengono toccati: rimuovili da Crew se l'utente lo chiede")
PACK_BAD_NAME = _t("pack.bad_name", "nom de pack invalide : {name}", "invalid pack name: {name}",
                   "ungültiger Pack-Name: {name}", "nome di pack non valido: {name}")

# ═══ Sous-agents (`tools/delegate.py`) ═════════════════════════════════════════════════════════════════════════════════
DELEGATE_FORK_NOTE = _t(
    "delegate.fork_note",
    "[Sous-agent fork] Tu es un sous-agent lancé depuis la conversation ci-dessus : tu en as tout le contexte. Ne "
    "poursuis pas le tour de l'agent principal : fais seulement la tâche ci-dessous, puis réponds par un résumé de ce "
    "que tu as fait et trouvé ; ce résumé revient à l'agent principal. Tu ne peux ni déléguer, ni poser de question à "
    "l'utilisateur, ni envoyer de message.\n[/Sous-agent]\n\nTâche : ",
    "[Fork sub-agent] You are a sub-agent launched from the conversation above: you have all of its context. Do not "
    "continue the main agent's turn: only do the task below, then reply with a summary of what you did and found; "
    "this summary goes back to the main agent. You can neither delegate, nor ask the user a question, nor send a "
    "message.\n[/Sub-agent]\n\nTask: ",
    "[Fork-Unter-Agent] Du bist ein Unter-Agent, der aus dem obigen Gespräch gestartet wurde: Du hast den gesamten "
    "Kontext. Setze die Antwort des übergeordneten Agenten nicht fort: Erledige nur die Aufgabe unten und antworte dann mit "
    "einer Zusammenfassung dessen, was du getan und gefunden hast; diese Zusammenfassung geht an den übergeordneten "
    "Agenten zurück. Du kannst weder delegieren noch dem Nutzer eine Frage stellen noch eine Nachricht senden.\n"
    "[/Unter-Agent]\n\nAufgabe: ",
    "[Sotto-agente fork] Sei un sotto-agente avviato dalla conversazione qui sopra: ne hai tutto il contesto. Non "
    "proseguire il turno dell'agente principale: fai solo il compito qui sotto, poi rispondi con un riassunto di ciò "
    "che hai fatto e trovato; questo riassunto torna all'agente principale. Non puoi né delegare, né fare domande "
    "all'utente, né inviare messaggi.\n[/Sotto-agente]\n\nCompito: ")
DELEGATE_CONTEXT_LABEL = _t("delegate.context_label", "\n\nContexte :\n", "\n\nContext:\n", "\n\nKontext:\n",
                            "\n\nContesto:\n")
DELEGATE_REVIEWER_ROLE = _t(
    "delegate.reviewer_role",
    "\n\nRôle : relecteur. Tu lis, lances les tests et rapportes ; tu ne modifies aucun fichier.",
    "\n\nRole: reviewer. You read, run the tests and report; you do not modify any file.",
    "\n\nRolle: Reviewer. Du liest, führst die Tests aus und berichtest; du änderst keine Datei.",
    "\n\nRuolo: revisore. Leggi, lanci i test e riferisci; non modifichi alcun file.")
DELEGATE_OUTPUT_CONTRACT = _t(
    "delegate.output_contract",
    "[Contrat de sortie] Ta réponse finale doit être UN objet JSON conforme à ce schéma, sans autre texte :\n",
    "[Output contract] Your final answer must be ONE JSON object that conforms to this schema, with no other text:\n",
    "[Ausgabevertrag] Deine endgültige Antwort muss EIN JSON-Objekt sein, das diesem Schema entspricht, ohne weiteren "
    "Text:\n",
    "[Contratto di uscita] La tua risposta finale deve essere UN oggetto JSON conforme a questo schema, senza altro "
    "testo:\n")
DELEGATE_WORKTREE_NOTE = _t(
    "delegate.worktree_note",
    "\n\n[ISOLATION WORKTREE] Tu travailles dans un worktree git isolé : {path}\nTa branche : {branch}\nToutes tes "
    "modifications et commandes restent dans ce dossier (ton terminal y démarre). Ne va PAS dans le dépôt principal. "
    "Commite ton travail sur ta branche quand tu as fini ; le parent relira et fusionnera. Si tu ne commites rien et "
    "laisses l'arbre propre, le worktree est supprimé automatiquement.",
    "\n\n[WORKTREE ISOLATION] You work in an isolated git worktree: {path}\nYour branch: {branch}\nAll your changes "
    "and commands stay in this folder (your terminal starts there). Do NOT go into the main repository. Commit your "
    "work on your branch when you are done; the parent will review and merge. If you commit nothing and leave the "
    "tree clean, the worktree is removed automatically.",
    "\n\n[WORKTREE-ISOLATION] Du arbeitest in einem isolierten git-Worktree: {path}\nDein Branch: {branch}\nAlle "
    "deine Änderungen und Befehle bleiben in diesem Ordner (dein Terminal startet dort). Gehe NICHT in das "
    "Hauptrepository. Committe deine Arbeit auf deinem Branch, wenn du fertig bist; der übergeordnete Agent prüft und führt "
    "zusammen. Wenn du nichts committest und das Arbeitsverzeichnis sauber lässt, wird der Worktree automatisch gelöscht.",
    "\n\n[ISOLAMENTO WORKTREE] Lavori in un worktree git isolato: {path}\nIl tuo branch: {branch}\nTutte le tue "
    "modifiche e i tuoi comandi restano in questa cartella (il tuo terminale parte da lì). NON andare nel "
    "repository principale. Fai il commit del tuo lavoro sul tuo branch quando hai finito; l'agente principale rivedrà e "
    "unirà. Se non fai commit e lasci l'albero pulito, il worktree viene eliminato automaticamente.")
DELEGATE_CANCELLED = _t(
    "delegate.cancelled", "annulé à la demande : le sous-agent a été arrêté", "cancelled on request: the sub-agent was stopped",
    "auf Anforderung abgebrochen: Der Unter-Agent wurde gestoppt",
    "annullato su richiesta: il sotto-agente è stato fermato")
DELEGATE_CANCELLED_PARKED = _t(
    "delegate.cancelled_parked", " (sa carte en attente ne sera pas exécutée)", " (its pending card will not be executed)",
    " (seine wartende Karte wird nicht ausgeführt)", " (la sua scheda in attesa non verrà eseguita)")
DELEGATE_NOTE_NO_ISOLATE = _t(
    "delegate.note_no_isolate", "isolate=false : l'enfant travaille directement dans le dossier de travail.",
    "isolate=false: the child works directly in the working folder.",
    "isolate=false: Das Kind arbeitet direkt im Arbeitsordner.",
    "isolate=false: il figlio lavora direttamente nella cartella di lavoro.")
DELEGATE_NOTE_NO_WORKDIR = _t(
    "delegate.note_no_workdir", "pas de dossier de travail : aucune isolation.", "no working folder: no isolation.",
    "kein Arbeitsordner: keine Isolation.", "nessuna cartella di lavoro: nessun isolamento.")
DELEGATE_NOTE_NO_GIT = _t(
    "delegate.note_no_git",
    "pas de dépôt git (ou aucun commit) : isolation impossible, l'enfant travaille dans le dossier de travail.",
    "no git repository (or no commit): isolation not possible, the child works in the working folder.",
    "kein git-Repository (oder kein Commit): Isolation nicht möglich, das Kind arbeitet im Arbeitsordner.",
    "nessun repository git (o nessun commit): isolamento impossibile, il figlio lavora nella cartella di lavoro.")
DELEGATE_INSPECTION_FAILED = _t(
    "delegate.inspection_failed",
    "inspection git en échec ({failed} inconnus, non prouvés nuls) : worktree et branche conservés, à relire avant de "
    "conclure que l'enfant n'a rien fait.",
    "git inspection failed ({failed} unknown, not proven zero): worktree and branch kept, to review before "
    "concluding that the child did nothing.",
    "git-Prüfung fehlgeschlagen ({failed} unbekannt, nicht als null bewiesen): Worktree und Branch bleiben erhalten, "
    "prüfe sie, bevor du daraus schließt, dass das Kind nichts getan hat.",
    "ispezione git non riuscita ({failed} sconosciuti, non provati nulli): worktree e branch conservati, da rileggere "
    "prima di concludere che il figlio non ha fatto nulla.")
DELEGATE_RESTART_INTERRUPTED = _t(
    "delegate.restart_interrupted",
    "interrompu par un redémarrage : le sous-agent ne tourne plus et n'est pas repris",
    "interrupted by a restart: the sub-agent is no longer running and is not resumed",
    "durch einen Neustart unterbrochen: Der Unter-Agent läuft nicht mehr und wird nicht fortgesetzt",
    "interrotto da un riavvio: il sotto-agente non è più in esecuzione e non viene ripreso")
DELEGATE_RESTART_PARKED = _t(
    "delegate.restart_parked", "; sa carte en attente est refusée (jamais exécutée avec les outils du parent)",
    "; its pending card is refused (never executed with the parent's tools)",
    "; seine wartende Karte wird abgelehnt (nie mit den Werkzeugen des übergeordneten Agenten ausgeführt)",
    "; la sua scheda in attesa è rifiutata (mai eseguita con gli strumenti dell'agente principale)")
DELEGATE_RESTART_KEPT = _t(
    "delegate.restart_kept", "; worktree conservé : relis la branche", "; worktree kept: review the branch",
    "; Worktree bleibt erhalten: Prüfe den Branch", "; worktree conservato: rileggi il branch")
DELEGATE_RESTART_WORKTREE_NOTE = _t(
    "delegate.restart_worktree_note", "conservé au redémarrage, non inspecté", "kept at restart, not inspected",
    "beim Neustart erhalten, nicht geprüft", "conservato al riavvio, non ispezionato")
DELEGATE_NOT_LAUNCHED = _t(
    "delegate.not_launched", "{message} : la tâche n'a pas été lancée (aucun appel du modèle).",
    "{message}: the task was not launched (no model call).",
    "{message}: Die Aufgabe wurde nicht gestartet (kein Modellaufruf).",
    "{message}: il compito non è stato avviato (nessuna chiamata al modello).")
DELEGATE_CARD_PENDING = _t(
    "delegate.card_pending", "\n[carte d'approbation en attente sur le fil {thread} : {names}]",
    "\n[approval card pending on thread {thread}: {names}]",
    "\n[Freigabekarte wartet im Gespräch {thread}: {names}]",
    "\n[scheda di approvazione in attesa nella conversazione {thread}: {names}]")
DELEGATE_DONE_NOTE = _t(
    "delegate.done_note", "[sous-agents en arrière-plan] terminés : {done} — delegate_status pour lire leurs résultats.",
    "[background sub-agents] finished: {done} — delegate_status to read their results.",
    "[Unter-Agenten im Hintergrund] fertig: {done} — delegate_status, um ihre Ergebnisse zu lesen.",
    "[sotto-agenti in background] terminati: {done} — delegate_status per leggere i loro risultati.")
DELEGATE_SCHEMA_NOT_JSON = _t(
    "delegate.schema_not_json", "output_schema n'est pas un JSON valide", "output_schema is not valid JSON",
    "output_schema ist kein gültiges JSON", "output_schema non è un JSON valido")
DELEGATE_SCHEMA_NOT_OBJECT = _t(
    "delegate.schema_not_object", "output_schema doit être un objet JSON Schema", "output_schema must be a JSON Schema object",
    "output_schema muss ein JSON-Schema-Objekt sein", "output_schema deve essere un oggetto JSON Schema")
DELEGATE_REPLY_NOT_JSON = _t(
    "delegate.reply_not_json", "la réponse n'est pas un JSON valide", "the answer is not valid JSON",
    "Die Antwort ist kein gültiges JSON", "la risposta non è un JSON valido")

# ═══ Mémoire longue (`memory/corthexis.py`, `memory/tenant_long.py`, `memory/long_note.py`) ═══════════════════════════════
MEMORY_NOTE_NOT_FOUND = _t("memory.note_not_found", "note introuvable : {name}", "note not found: {name}",
                           "Notiz nicht gefunden: {name}", "nota non trovata: {name}")
MEMORY_UNKNOWN_TOOL = _t("memory.unknown_tool", "outil mémoire inconnu : {name}", "unknown memory tool: {name}",
                         "unbekanntes Gedächtnis-Werkzeug: {name}", "strumento di memoria sconosciuto: {name}")
MEMORY_ERROR = _t("memory.error", "erreur mémoire ({name}): {error}", "memory error ({name}): {error}",
                  "Gedächtnisfehler ({name}): {error}", "errore di memoria ({name}): {error}")
MEMORY_SIZE_REQUIRED = _t(
    "memory.size_required", "title (≤ {max_title} car.) et text (≤ {max_text} car.) requis : une note par idée, courte",
    "title (≤ {max_title} chars) and text (≤ {max_text} chars) required: one note per idea, short",
    "title (≤ {max_title} Zeichen) und text (≤ {max_text} Zeichen) erforderlich: eine Notiz pro Idee, kurz",
    "title (≤ {max_title} car.) e text (≤ {max_text} car.) richiesti: una nota per idea, breve")
MEMORY_QUOTE_REQUIRED = _t(
    "memory.quote_required",
    "cite dans `quote` les mots de l'utilisateur de ce message (au moins {min_quote} caractères, tels quels)",
    "quote in `quote` the user's words from this message (at least {min_quote} characters, as they are)",
    "zitiere in `quote` die Worte des Nutzers aus dieser Nachricht (mindestens {min_quote} Zeichen, unverändert)",
    "cita in `quote` le parole dell'utente di questo messaggio (almeno {min_quote} caratteri, così come sono)")
MEMORY_ACCOUNT_REQUIRED = _t(
    "memory.account_required", "mémoire longue : compte requis", "long-term memory: account required",
    "Langzeitgedächtnis: Konto erforderlich", "memoria a lungo termine: account richiesto")
MEMORY_AUDIENCE_HOUSEHOLD_ONLY = _t(
    "memory.audience_household_only", "audience : seulement pour une note du foyer", "audience: only for a household note",
    "audience: nur für eine Haushaltsnotiz", "audience: solo per una nota della famiglia")
MEMORY_TITLE_TEXT_REQUIRED = _t(
    "memory.title_text_required", "title (≤ {max_title} car.) et text (≤ {max_text} car.) requis",
    "title (≤ {max_title} chars) and text (≤ {max_text} chars) required",
    "title (≤ {max_title} Zeichen) und text (≤ {max_text} Zeichen) erforderlich",
    "title (≤ {max_title} car.) e text (≤ {max_text} car.) richiesti")
MEMORY_NO_HOUSEHOLD = _t(
    "memory.no_household", "aucun foyer actif pour ce compte", "no active household for this account",
    "kein aktiver Haushalt für dieses Konto", "nessuna famiglia attiva per questo account")
MEMORY_PAYER_WRITES = _t(
    "memory.payer_writes", "seul le payeur écrit dans la mémoire du foyer", "only the payer writes to the household memory",
    "nur der Zahler schreibt in das Haushaltsgedächtnis", "solo il pagante scrive nella memoria della famiglia")
MEMORY_REVIEW_OFF = _t(
    "memory.review_off", "proposer au foyer demande la relecture du payeur (memory_pending éteint)",
    "proposing to the household requires the payer's review (memory_pending off)",
    "Ein Vorschlag an den Haushalt erfordert die Prüfung durch den Zahler (memory_pending aus)",
    "proporre alla famiglia richiede la revisione del pagante (memory_pending spento)")

# ═══ Courrier (`connectors/mail.py`) ═══════════════════════════════════════════════════════════════════════════════════
MAIL_ATT_NOTE = _t(
    "mail.att_note",
    "has:attachment approximé par l'en-tête Content-Type multipart/mixed (IMAP n'a pas de critère « pièce jointe ») : "
    "peut manquer des messages (multipart/related, signés) ou en inclure sans pièce jointe",
    "has:attachment approximated by the Content-Type multipart/mixed header (IMAP has no \"attachment\" criterion): "
    "may miss messages (multipart/related, signed) or include some without an attachment",
    "has:attachment wird durch den Content-Type-Header multipart/mixed angenähert (IMAP kennt kein Kriterium "
    "„Anhang“): kann Nachrichten übersehen (multipart/related, signiert) oder Nachrichten ohne Anhang einschließen",
    "has:attachment approssimato dall'intestazione Content-Type multipart/mixed (IMAP non ha un criterio "
    "«allegato»): possono mancare dei messaggi (multipart/related, firmati) o essercene alcuni senza allegato")
MAIL_IGNORED_NOTE = _t(
    "mail.ignored_note", "critère « {key}:{val} » non géré hors Gmail (ou valeur invalide), ignoré",
    "criterion \"{key}:{val}\" not supported outside Gmail (or invalid value), ignored",
    "Kriterium „{key}:{val}“ außerhalb von Gmail nicht unterstützt (oder ungültiger Wert), ignoriert",
    "criterio «{key}:{val}» non gestito fuori da Gmail (o valore non valido), ignorato")
MAIL_FOLDER_NOT_FOUND = _t("mail.folder_not_found", "dossier introuvable : {name}", "folder not found: {name}",
                           "Ordner nicht gefunden: {name}", "cartella non trovata: {name}")
MAIL_BAD_ID = _t(
    "mail.bad_id", "id de message invalide (utiliser un id de mail_search)",
    "invalid message id (use an id from mail_search)", "ungültige Nachrichten-ID (verwende eine ID aus mail_search)",
    "id di messaggio non valido (usa un id di mail_search)")
MAIL_STALE_ID = _t(
    "mail.stale_id", "id périmé : le dossier a été reconstruit, relancer mail_search",
    "stale id: the folder was rebuilt, run mail_search again",
    "veraltete ID: Der Ordner wurde neu aufgebaut, starte mail_search erneut",
    "id scaduto: la cartella è stata ricostruita, rilancia mail_search")
MAIL_INTERNAL_ERROR = _t("mail.internal_error", "erreur interne : {detail}", "internal error: {detail}",
                         "interner Fehler: {detail}", "errore interno: {detail}")
MAIL_MESSAGE_NOT_FOUND = _t("mail.message_not_found", "message introuvable", "message not found",
                            "Nachricht nicht gefunden", "messaggio non trovato")
MAIL_NEWLINE_IN_SUBJECT = _t(
    "mail.newline_in_subject", "retour à la ligne interdit dans le sujet", "line break not allowed in the subject",
    "Zeilenumbruch im Betreff nicht erlaubt", "a capo non consentito nell'oggetto")
MAIL_BAD_ADDRESS = _t("mail.bad_address", "adresse invalide dans « {field} »", "invalid address in \"{field}\"",
                      "ungültige Adresse in „{field}“", "indirizzo non valido in «{field}»")
MAIL_NO_DRAFTS = _t(
    "mail.no_drafts", "dossier Brouillons introuvable sur ce compte", "Drafts folder not found on this account",
    "Entwurfsordner in diesem Konto nicht gefunden", "cartella Bozze non trovata su questo account")
MAIL_ALL_REJECTED = _t(
    "mail.all_rejected", "tous les destinataires ont été refusés", "all recipients were refused",
    "alle Empfänger wurden abgelehnt", "tutti i destinatari sono stati rifiutati")
MAIL_IS_TRASH = _t(
    "mail.is_trash", "ce dossier est la corbeille du compte : utiliser mail_trash (action destructive, carte d'approbation)",
    "this folder is the account's trash: use mail_trash (destructive action, approval card)",
    "dieser Ordner ist der Papierkorb des Kontos: Nutze mail_trash (destruktive Aktion, Freigabekarte)",
    "questa cartella è il cestino dell'account: usa mail_trash (azione distruttiva, scheda di approvazione)")
MAIL_ALREADY_THERE = _t("mail.already_there", "déjà dans ce dossier", "already in this folder",
                        "bereits in diesem Ordner", "già in questa cartella")
MAIL_NO_TRASH = _t(
    "mail.no_trash", "dossier Corbeille introuvable sur ce compte", "Trash folder not found on this account",
    "Papierkorb in diesem Konto nicht gefunden", "cartella Cestino non trovata su questo account")
MAIL_ALREADY_TRASHED = _t("mail.already_trashed", "déjà dans la corbeille", "already in the trash",
                          "bereits im Papierkorb", "già nel cestino")
MAIL_SEARCH_REFUSED = _t("mail.search_refused", "SEARCH refusé par le serveur", "SEARCH refused by the server",
                         "SEARCH vom Server abgelehnt", "SEARCH rifiutato dal server")

# ═══ Agenda (`connectors/calendar.py`, `connectors/cloudcal.py`) ═══════════════════════════════════════════════════════
CAL_TRUNCATED_NOTE = _t(
    "cal.truncated_note", "récurrence tronquée : trop d'occurrences, liste partielle",
    "recurrence truncated: too many occurrences, partial list",
    "Wiederholung abgeschnitten: zu viele Termine, Teilliste", "ricorrenza troncata: troppe occorrenze, elenco parziale")
CAL_RECURRENCE_UNSUPPORTED = _t(
    "cal.recurrence_unsupported", "récurrence non gérée : seule la première occurrence est affichée",
    "recurrence not supported: only the first occurrence is shown",
    "Wiederholung nicht unterstützt: nur der erste Termin wird angezeigt",
    "ricorrenza non gestita: viene mostrata solo la prima occorrenza")
CAL_UNREADABLE_IGNORED = _t(
    "cal.unreadable_ignored", "un événement illisible a été ignoré", "an unreadable event was ignored",
    "ein unlesbarer Termin wurde ignoriert", "un evento illeggibile è stato ignorato")
CAL_TZ_UNKNOWN_NOTE = _t(
    "cal.tz_unknown_note", "fuseau {name} inconnu, fuseau du compte utilisé", "time zone {name} unknown, account time zone used",
    "Zeitzone {name} unbekannt, Zeitzone des Kontos verwendet", "fuso {name} sconosciuto, usato il fuso dell'account")
CAL_BAD_DATETIME = _t(
    "cal.bad_datetime",
    "date/heure invalide : {value} — attendu ISO 8601, ex. 2026-10-27T14:00 (ou 2026-10-27 pour une journée entière)",
    "invalid date/time: {value} — expected ISO 8601, e.g. 2026-10-27T14:00 (or 2026-10-27 for a whole day)",
    "ungültiges Datum/ungültige Uhrzeit: {value} — erwartet ISO 8601, z. B. 2026-10-27T14:00 (oder 2026-10-27 für einen ganzen Tag)",
    "data/ora non valida: {value} — atteso ISO 8601, es. 2026-10-27T14:00 (o 2026-10-27 per un giorno intero)")
CAL_BAD_GUEST = _t("cal.bad_guest", "adresse d'invité invalide : {value}", "invalid guest address: {value}",
                   "ungültige Gastadresse: {value}", "indirizzo dell'invitato non valido: {value}")
CAL_MASTER_NOT_FOUND = _t("cal.master_not_found", "événement maître introuvable", "master event not found",
                          "Haupttermin nicht gefunden", "evento principale non trovato")
CAL_UNKNOWN_MODE = _t(
    "cal.unknown_mode", "mode agenda inconnu (ical ou caldav)", "unknown calendar mode (ical or caldav)",
    "unbekannter Kalendermodus (ical oder caldav)", "modalità calendario sconosciuta (ical o caldav)")
CAL_UNKNOWN_TZ = _t("cal.unknown_tz", "fuseau horaire inconnu", "unknown time zone", "unbekannte Zeitzone",
                    "fuso orario sconosciuto")
CAL_INTERNAL_ERROR = _t(
    "cal.internal_error", "erreur interne du connecteur agenda", "calendar connector internal error",
    "interner Fehler des Kalender-Konnektors", "errore interno del connettore calendario")
CAL_DAYS_INT = _t("cal.days_int", "`days` doit être un entier", "`days` must be an integer",
                  "`days` muss eine ganze Zahl sein", "`days` deve essere un intero")
CAL_DAYS_RANGE = _t("cal.days_range", "`days` doit être compris entre 1 et 366", "`days` must be between 1 and 366",
                    "`days` muss zwischen 1 und 366 liegen", "`days` deve essere compreso tra 1 e 366")
CAL_EVENT_NOT_FOUND = _t(
    "cal.event_not_found", "événement introuvable (id inconnu de cet agenda)", "event not found (id unknown to this calendar)",
    "Termin nicht gefunden (ID in diesem Kalender unbekannt)", "evento non trovato (id sconosciuto a questo calendario)")
CAL_SUMMARY_REQUIRED = _t("cal.summary_required", "`summary` requis", "`summary` required", "`summary` erforderlich",
                          "`summary` richiesto")
CAL_START_END_KIND = _t(
    "cal.start_end_kind", "`start` et `end` doivent être tous deux des dates ou tous deux des heures",
    "`start` and `end` must both be dates or both be times",
    "`start` und `end` müssen beide Datumsangaben oder beide Uhrzeiten sein",
    "`start` e `end` devono essere entrambi date o entrambi orari")
CAL_END_AFTER_START = _t("cal.end_after_start", "`end` doit être postérieur à `start`", "`end` must be after `start`",
                         "`end` muss nach `start` liegen", "`end` deve essere successivo a `start`")
CAL_EVENT_ID_REQUIRED = _t("cal.event_id_required", "`event_id` requis", "`event_id` required", "`event_id` erforderlich",
                           "`event_id` richiesto")
CAL_GUESTS_NO_INVITE = _t(
    "cal.guests_no_invite", "invités inscrits dans l'événement ; aucune invitation envoyée",
    "guests recorded in the event; no invitation sent", "Gäste im Termin eingetragen; keine Einladung gesendet",
    "invitati registrati nell'evento; nessun invito inviato")
CAL_NO_INVITE_SENT = _t("cal.no_invite_sent", "aucune invitation envoyée", "no invitation sent",
                        "keine Einladung gesendet", "nessun invito inviato")
CAL_READ_ONLY = _t(
    "cal.read_only", "agenda en lecture seule (iCal) : écriture impossible", "read-only calendar (iCal): writing not possible",
    "schreibgeschützter Kalender (iCal): Schreiben nicht möglich", "calendario in sola lettura (iCal): scrittura impossibile")
CAL_BAD_UID = _t("cal.bad_uid", "uid invalide", "invalid uid", "ungültige uid", "uid non valido")
CAL_UNREADABLE_EVENT = _t("cal.unreadable_event", "événement illisible", "unreadable event", "unlesbarer Termin",
                          "evento illeggibile")
CAL_SERIES_TIMES = _t(
    "cal.series_times", "série récurrente : la modification des horaires n'est pas prise en charge ici",
    "recurring series: changing the times is not supported here",
    "Terminserie: Das Ändern der Zeiten wird hier nicht unterstützt",
    "serie ricorrente: la modifica degli orari non è supportata qui")
CAL_ALLDAY_SWITCH = _t(
    "cal.allday_switch", "changer entre journée entière et heures impose de donner `start` et `end`",
    "switching between a whole day and times requires giving `start` and `end`",
    "Der Wechsel zwischen ganztägig und Uhrzeiten erfordert die Angabe von `start` und `end`",
    "passare da giorno intero a orari impone di fornire `start` e `end`")
CAL_NOTHING_TO_CHANGE = _t(
    "cal.nothing_to_change", "rien à modifier : donner au moins un champ", "nothing to change: give at least one field",
    "nichts zu ändern: Gib mindestens ein Feld an", "niente da modificare: dai almeno un campo")
CAL_SERIES_WHOLE = _t(
    "cal.series_whole", "événement récurrent : la modification vaut pour toute la série",
    "recurring event: the change applies to the whole series",
    "wiederkehrender Termin: Die Änderung gilt für die ganze Serie",
    "evento ricorrente: la modifica vale per tutta la serie")
CAL_CHANGED_MEANWHILE = _t(
    "cal.changed_meanwhile", "événement modifié entre-temps : relire puis réessayer",
    "event changed in the meantime: read it again, then retry",
    "Termin zwischenzeitlich geändert: Lies ihn erneut und versuche es dann noch einmal",
    "evento modificato nel frattempo: rileggilo, poi riprova")
CAL_UNKNOWN_CALENDAR = _t(
    "cal.unknown_calendar", "agenda inconnu : {name} (disponibles : {available})",
    "unknown calendar: {name} (available: {available})", "unbekannter Kalender: {name} (verfügbar: {available})",
    "calendario sconosciuto: {name} (disponibili: {available})")
CAL_BAD_EVENT_ID = _t(
    "cal.bad_event_id", "id d'événement invalide (utiliser un id de calendar_events)",
    "invalid event id (use an id from calendar_events)", "ungültige Termin-ID (verwende eine ID aus calendar_events)",
    "id evento non valido (usa un id di calendar_events)")
CAL_EVENT_OR_CALENDAR_NOT_FOUND = _t(
    "cal.event_or_calendar_not_found", "événement ou agenda introuvable", "event or calendar not found",
    "Termin oder Kalender nicht gefunden", "evento o calendario non trovato")
CAL_CLOUD_INTERNAL_ERROR = _t("cal.cloud_internal_error", "erreur interne : {detail}", "internal error: {detail}",
                              "interner Fehler: {detail}", "errore interno: {detail}")

# ═══ Compétences apprises, pages, sessions de code : refus et introuvables ═══════════════════════════════════════════════
SKILL_NOT_FOUND = _t(
    "skill.not_found", "compétence introuvable : {name} (skill_list pour voir les noms)",
    "skill not found: {name} (skill_list to see the names)", "Skill nicht gefunden: {name} (skill_list zeigt die Namen)",
    "skill non trovata: {name} (skill_list per vedere i nomi)")
PAGE_OUTSIDE_ROOT = _t("page.outside_root", "REFUSÉ : {path} est hors du dossier de travail",
                       "REFUSED: {path} is outside the working folder", "ABGELEHNT: {path} liegt außerhalb des Arbeitsordners",
                       "RIFIUTATO: {path} è fuori dalla cartella di lavoro")
PAGE_FILE_NOT_FOUND = _t(
    "page.file_not_found", "fichier introuvable : {path} (écris-le d'abord avec write_file)",
    "file not found: {path} (write it first with write_file)", "Datei nicht gefunden: {path} (schreibe sie zuerst mit write_file)",
    "file non trovato: {path} (scrivilo prima con write_file)")
PAGE_TOO_BIG = _t("page.too_big", "REFUSÉ : {path} dépasse 2 Mo", "REFUSED: {path} exceeds 2 MB",
                  "ABGELEHNT: {path} überschreitet 2 MB", "RIFIUTATO: {path} supera 2 MB")
PAGE_PUBLISHED = _t(
    "page.published",
    "page {page_id} « {title} », version {version}, privée ; elle est affichée dans le chat et suit les modifications de {rel}.",
    "page {page_id} \"{title}\", version {version}, private; it is shown in the chat and follows the changes of {rel}.",
    "Seite {page_id} „{title}“, Version {version}, privat; sie wird im Chat angezeigt und folgt den Änderungen von {rel}.",
    "pagina {page_id} «{title}», versione {version}, privata; è mostrata nella chat e segue le modifiche di {rel}.")
CODE_SESSION_OTHER_USER = _t(
    "code_session.other_user", "REFUSÉ : cette session de code appartient à un autre utilisateur",
    "REFUSED: this code session belongs to another user", "ABGELEHNT: Diese Code-Sitzung gehört einem anderen Nutzer",
    "RIFIUTATO: questa sessione di codice appartiene a un altro utente")
CODE_SESSION_SKILLS_CHANGED = _t(
    "code_session.skills_changed",
    "REFUSÉ : les skills du dépôt ont changé pendant la lecture ; ils sont de nouveau ignorés jusqu'à une nouvelle confiance",
    "REFUSED: the repository's skills changed during the read; they are ignored again until trusted anew",
    "ABGELEHNT: Die Skills des Repositorys haben sich während des Lesens geändert; sie werden wieder ignoriert, bis "
    "ihnen erneut vertraut wird",
    "RIFIUTATO: le skill del repository sono cambiate durante la lettura; sono di nuovo ignorate finché non viene "
    "data nuova fiducia")
DELEGATE_SCHEMA_INVALID = _t(
    "delegate.schema_invalid", "output_schema n'est pas un JSON Schema valide (champ « {path} »)",
    "output_schema is not a valid JSON Schema (field \"{path}\")",
    "output_schema ist kein gültiges JSON-Schema (Feld „{path}“)",
    "output_schema non è un JSON Schema valido (campo «{path}»)")
DELEGATE_REPLY_EMPTY = _t(
    "delegate.reply_empty", "réponse vide : un objet JSON conforme au schéma est attendu",
    "empty response: a JSON object matching the schema is expected",
    "leere Antwort: Ein JSON-Objekt gemäß Schema wird erwartet",
    "risposta vuota: è atteso un oggetto JSON conforme allo schema")
DELEGATE_REPLY_FIELD_INVALID = _t(
    "delegate.reply_field_invalid", "{path} : ne respecte pas le schéma", "{path}: does not match the schema",
    "{path}: entspricht nicht dem Schema", "{path}: non rispetta lo schema")
