"""La consigne de périmètre du préfixe de code (relue, figée) : sa disparition ou une retouche non relue casse ce test."""
import hashlib
from pathlib import Path

PREFIX = Path(__file__).resolve().parents[2] / "nokker" / "prompts" / "prefix-lokkal-code.md"
LINE = next((ln for ln in PREFIX.read_text(encoding="utf-8").splitlines() if ln.startswith("- Reste dans le périmètre")), "")


def test_scope_line_present_and_frozen():
    assert LINE
    assert hashlib.sha256(LINE.encode()).hexdigest()[:16] == "5f08e9e980f00f0e"


def test_scope_line_sits_in_method_section():
    text = PREFIX.read_text(encoding="utf-8")
    assert text.index("## Méthode") < text.index(LINE)


def test_prefix_asks_for_informal_register():
    # 18.11.2026 : « y compris pour demander d'accepter une carte » porte sur la LANGUE, pas sur le tutoiement (attente
    # d'une carte rédigée en français pour une invite anglaise, rc.9.38).
    assert ("- Réponds dans la langue de la personne (celle de son message), y compris pour demander d'accepter une "
            "carte ; en français, tu la tutoies.") in PREFIX.read_text(encoding="utf-8")


def test_every_served_prefix_asks_for_informal_register():
    prompts = PREFIX.parent
    assert "En français, tutoie la personne" in (prompts / "prefix-nokker.md").read_text(encoding="utf-8")
    for name in ("prefix-lokkal-code.md", "prefix-lokkal-folder.md"):  # langue de la personne, tutoiement en français seulement
        line = next((ln for ln in (prompts / name).read_text(encoding="utf-8").splitlines()
                     if ln.startswith("- Réponds dans la langue de la personne (celle de son message)")), "")
        assert "en français, tu la tutoies" in line, name
    for name in ("prefix-nokker.md", "prefix-lokkal-folder.md"):
        assert "« tu m'as dit… »" in (prompts / name).read_text(encoding="utf-8"), name


def test_prefix_nokker_forbids_technical_tool_names_right_after_the_register_line():
    text = (PREFIX.parent / "prefix-nokker.md").read_text(encoding="utf-8")
    line = "- Ne nomme jamais un outil par son nom technique : dis ce que tu fais en mots simples.\n"
    assert line in text
    assert text.index("En français, tutoie la personne") < text.index(line) < text.index("- Clair, direct")
