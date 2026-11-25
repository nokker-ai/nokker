"""Le gabarit des refus rendus au modèle : un seul préfixe, jamais « le noyau » dans un résultat d'outil.

Un modèle qui lit « REFUSÉ par le noyau » le répète à la personne : le texte ne nomme plus qui refuse."""
from __future__ import annotations

import io
import pathlib
import tokenize

from nokker.core.refusal import REFUSED_PREFIX, RETRY_ADVICE, is_refusal, refused_text

ROOT = pathlib.Path(__file__).resolve().parents[2] / "nokker"

# Littéraux qui contiennent « par le noyau » sans être rendus au modèle comme résultat d'outil.
ALLOWED = {
    # rapport d'import montré à la personne (et clé de classement du rapport), pas un résultat d'outil
    ("importers/base.py", "refusé par le noyau"),
    ("importers/apply.py", "refusé par le noyau"),
    # note de la liste préparée pour la personne (déclaration à remplir à la main)
    ("household/paper/service.py", "liste préparée par le noyau"),
    # message de journal, jamais rendu au modèle
    ("improve/overlay.py", "appliqué par le noyau"),
}


def test_template_is_frozen():
    assert REFUSED_PREFIX == "Refusé : "
    assert refused_text("mode plan, lecture seule") == "Refusé : mode plan, lecture seule."
    assert refused_text("règle: motif.") == "Refusé : règle: motif."  # pas de double point
    assert "noyau" not in refused_text("x").lower()


def test_retry_advice_is_unambiguous():
    assert RETRY_ADVICE == "Ne le réessaie pas et ne cherche pas à le contourner."
    assert "autrement" not in RETRY_ADVICE


def test_is_refusal_reads_the_marker():
    assert is_refusal(refused_text("x")) and is_refusal("REFUSÉ : ancien gabarit")
    assert not is_refusal("[]") and not is_refusal(None) and not is_refusal("Le refus : non")


def _string_literals(path: pathlib.Path):
    src = path.read_text()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", -1)):
            if tok.type == tokenize.STRING and tok.string.lstrip("rbfRBF").startswith(('"""', "'''")):
                continue  # docstring ou bloc : commentaire, pas un texte rendu
            yield tok.start[0], tok.string


def test_no_tool_result_says_par_le_noyau():
    bad = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for line, text in _string_literals(path):
            low = text.lower()
            if "par le noyau" in low and not any(rel == f and w in low for f, w in ALLOWED):
                bad.append(f"{rel}:{line}: {text[:80]}")
    assert not bad, "« par le noyau » dans un texte rendu au modèle (utiliser nokker.core.refusal) :\n" + "\n".join(bad)
