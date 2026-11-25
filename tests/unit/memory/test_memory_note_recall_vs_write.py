"""Une question de rappel (« quelle est ma préférence ? ») ne déclenche jamais une écriture (recette rc.9.30 : le modèle
appelait `memory_note` au lieu de répondre). La consigne est dans le préfixe mémoire ET dans la description de l'outil ;
ce test les fige (empreinte sha256[:16] de la ligne du préfixe, texte exact de la phrase)."""
import hashlib
from pathlib import Path

from nokker.context import prefix_tools
from nokker.context import project_memory as PM

PREFIX = Path(PM.MEMORY_PREFIX_PATH)
RULE_TEXT = ("N'appelle `memory_note` que quand la personne énonce une règle ou une préférence, jamais pour répondre à "
             "une question sur ce qui est déjà noté : réponds depuis la mémoire du projet.")
DESC_TEXT = ("À n'appeler que quand la personne énonce une règle ou une préférence, jamais pour répondre à une question "
             "sur ce qui est déjà noté : réponds alors depuis la mémoire du projet.")
LEARN_LINE = "- Quand l'utilisateur te corrige ou fixe une règle durable"


def _line() -> str:
    # la ligne telle que le modèle la reçoit (marque de ligne `needs:` ôtée) : le texte figé n'a pas changé
    return next(ln for ln in prefix_tools.render(PREFIX.read_text(encoding="utf-8")).splitlines() if ln.startswith(LEARN_LINE))


def test_prefix_line_separates_fixing_a_preference_from_asking_for_it():
    line = _line()
    assert RULE_TEXT in line and line.index("note-la aussitôt") < line.index(RULE_TEXT)
    assert hashlib.sha256(line.encode()).hexdigest()[:16] == "bcf74e65542a98b5"


def test_tool_description_carries_the_same_instruction():
    assert DESC_TEXT in PM.MEMORY_NOTE_DESCRIPTION
