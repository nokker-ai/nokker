"""Les fragments de COMMANDE (`ls -la`, `mkdir -p a-b-dir && ls -d a-b-dir`, `/etc/hosts`) ne comptent pas comme prose dans la
détection de langue (« dir » est un mot-outil allemand) ; la prose pure garde EXACTEMENT ses scores d'avant."""
from __future__ import annotations

import pytest

from nokker.approvals import rule_labels
from nokker.core.lang_detect import detect_lang, scores
from nokker.core.turn import Message
from tests.unit.test_loop_contract import kernel

# (texte, langue détectée, scores (fr, en, de, it)) relevés AVANT la règle « commande » : doivent rester identiques.
PROSE = [
    ("Peux-tu me dire ce qui s'est passé hier soir avec le serveur ?", 'fr', (5, 1, 0, 0)),
    ("Je voudrais savoir pourquoi la sauvegarde n'a pas fonctionné.", 'fr', (4, 0, 0, 0)),
    ('Bonjour, est-ce que tu peux regarder les journaux de la nuit ?', 'fr', (7, 0, 0, 0)),
    ("Qu'est-ce que tu as trouvé dans le dossier de l'utilisateur ?", 'fr', (6, 0, 0, 0)),
    ("C'est peut-être un problème de droits, mais je ne suis pas sûr.", 'fr', (7, 0, 0, 1)),
    ("Merci beaucoup, c'est très bien comme ça.", 'fr', (7, 0, 0, 0)),
    ('Dis-moi quand le déploiement sera terminé et pour quelle raison il a échoué.', 'fr', (5, 0, 0, 0)),
    ('Liste les fichiers du répertoire et dis-moi lesquels sont les plus gros.', 'fr', (6, 0, 1, 0)),
    ('Can you tell me what changed in the last release and why it broke?', 'en', (0, 8, 0, 0)),
    ('Please check that the backup has finished and send me the result.', 'en', (0, 7, 0, 0)),
    ("I don't know what you want me to do with this file.", 'en', (0, 9, 0, 0)),
    ('Hello, how are you today? I want to make a new project.', 'en', (0, 7, 0, 0)),
    ('Which of these options would you recommend for the team?', 'en', (0, 6, 0, 0)),
    ('Thanks, that works for me and it is what we need.', 'en', (0, 10, 0, 0)),
    ('Show me the list of files that have not been updated since last week.', 'en', (0, 7, 0, 0)),
    ('Ich gebe dir das Buch, wenn du es mir morgen zurückbringst.', 'de', (1, 0, 8, 0)),
    ('Kannst du bitte die Protokolle prüfen und mir sagen, warum es nicht funktioniert?', 'de', (1, 0, 10, 0)),
    ('Wir haben das schon gestern gemacht und es war sehr gut.', 'de', (0, 0, 7, 0)),
    ('Die Rück-Fahrt dauert noch länger als der Hinweg, das ist nicht gut.', 'de', (0, 0, 7, 0)),
    ('Hallo, ich möchte wissen, wo die Dateien sind und wann sie fertig werden.', 'de', (0, 0, 10, 0)),
    ('Dir und mir ist das ganz egal, aber auch für sie ist es wichtig.', 'de', (0, 0, 12, 0)),
    ('Puoi controllare i log e dirmi perché la compilazione non riesce?', 'it', (0, 0, 0, 3)),
    ('Ciao, vorrei sapere quando sarà pronto il file che ho chiesto ieri.', 'it', (0, 0, 0, 5)),
    ('Grazie mille, è molto utile per il nostro lavoro di oggi.', 'it', (0, 0, 0, 5)),
    ('Cosa hai trovato nella cartella e che cosa devo fare adesso?', 'it', (0, 0, 0, 7)),
    ('Non sono sicuro che questo sia quello che vuoi, ma posso provare.', 'it', (1, 0, 0, 5)),
    ('ok', None, (0, 0, 0, 0)),
    ('oui', None, (1, 0, 0, 0)),
    ('yes please', 'en', (0, 2, 0, 0)),
    ('ja bitte', 'de', (0, 0, 2, 0)),
    ('sì grazie', 'it', (0, 0, 0, 2)),
    ('Ejecuta esto y dime qué pasa con el servidor por favor.', None, (0, 0, 0, 1)),
    ('Quel est le meilleur moyen de sauvegarder les données de la base ?', 'fr', (5, 0, 0, 0)),
    ('What is the best way to store the data of the application?', 'en', (0, 7, 0, 0)),
    ('Voir https://example.com/the/and/you et dis-moi ce que tu en penses pour nous.', 'fr', (7, 0, 0, 0)),
]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("NOKKER_LANG", raising=False)
    rule_labels.set_request_lang(None, None)
    yield
    rule_labels.set_request_lang(None, None)


@pytest.mark.parametrize("text,lang,sc", PROSE)
def test_pure_prose_is_unchanged(text, lang, sc):
    s = scores(text)
    assert (s["fr"], s["en"], s["de"], s["it"]) == sc
    assert detect_lang(text) == lang


EN_CMD = "Run exactly this in the terminal: mkdir -p bench-lang-dir && ls -d bench-lang-dir"
FR_CMD = "Lance exactement ceci dans le terminal : mkdir -p bench-lang-dir-fr && ls -d bench-lang-dir-fr"


def _k():
    k, *_ = kernel([Message("assistant", "ok")] * 4)
    return k


def test_the_two_bench_prompts():
    assert detect_lang(EN_CMD) == "en"
    assert scores(EN_CMD)["de"] == 0
    assert scores(FR_CMD)["de"] == 0
    assert _k().resolve_lang("t1", None, text=EN_CMD) == "en"  # avant : égalité, donc la langue de l'installation
    assert _k().resolve_lang("t1", None, text=FR_CMD) == "fr"  # avant : allemand


def test_english_prompt_beats_an_installation_in_french(monkeypatch):
    monkeypatch.setenv("NOKKER_LANG", "fr")
    assert _k().resolve_lang("t1", None, text=EN_CMD) == "en"


@pytest.mark.parametrize("text,lang", [
    ("Bitte führe aus: ls -la /tmp", "de"),
    ("Please run this command: ls -la /tmp and tell me what you see", "en"),
    ("Peux-tu lancer `git log` puis me dire ce qui a changé ?", "fr"),
    ("Peux-tu me dire pourquoi ça échoue quand je lance : cd /tmp && git status", "fr"),
    ("Ich gebe dir das Buch", "de"),
    ("ouvre /etc/hosts et dis-moi ce que tu y vois pour nous", "fr"),
    ("Voir https://example.com/dir/and/you et dis-moi ce que tu en penses pour nous", "fr"),
    ("Dis-moi peut-être ce qu'il y a dans l'utilisateur et pourquoi ça n'a pas marché", "fr"),
    ("Die Rück-Fahrt ist nicht gut und wir wissen es", "de"),
])
def test_mixed_prose_and_command(text, lang):
    assert detect_lang(text) == lang


@pytest.mark.parametrize("text", [
    "ls -la", "ls -d bench-lang-dir", "cd /tmp && git status", "docker ps -a", "git log --oneline | head -5",
    "mkdir -p a-b-dir && ls -d a-b-dir", "Ejecuta: docker ps -a", "Esegui questo comando: cd /tmp && git status",
    "/usr/bin/python3 script.py --dir=/tmp/dir",
])
def test_a_command_alone_gives_no_signal(text):
    assert detect_lang(text) is None


def test_dir_stays_german_in_german_prose():
    assert scores("Ich gebe dir das Buch")["de"] >= 3
    assert scores("Das gehört dir und mir")["de"] >= 3
