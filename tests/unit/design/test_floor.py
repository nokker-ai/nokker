"""Lot V8 : le plancher de design attrape chaque famille de défaut plantée, et laisse passer une page propre."""
from __future__ import annotations


from nokker.core.ports import ToolContext
from nokker.design.floor import check, contrast, report
from nokker.pages.store import PageStore
from nokker.pages.tools import PageToolSource

CLEAN = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Courses</title>
<style>
:root { --ink: #1d1b19; --paper: #fbf7f1; --accent: #b4462d; --body: "Source Serif 4", Georgia, serif; }
html, body { overflow-x: clip; }
body { margin: 0; color: var(--ink); background: var(--paper); font-family: var(--body); font-size: 18px; }
a { color: var(--accent); transition: opacity .2s ease; }
a:focus-visible, button:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) { a { transition: none; } }
</style></head>
<body><main><h1>Liste de la semaine</h1><p>Pain, lait, œufs.</p>
<label for="q">Ajouter</label><input id="q" type="text"><button>Ajouter</button>
<img src="data:image/png;base64,AAAA" alt=""></main></body></html>"""


def rules(html: str) -> set[str]:
    return {f.rule for f in check(html)}


def test_clean_page_has_no_floor_finding():
    assert [f for f in check(CLEAN) if f.level == "floor"] == []
    assert report(check(CLEAN)).startswith("design_check : rien au plancher")


def test_each_planted_defect_is_caught():
    bad = """<html><head><style>
    .card { color: #777; background: #fff; transition: all .3s; }
    html { overflow-x: hidden; } .wide { width: 100vw; }
    .hero { background: linear-gradient(90deg, #7c3aed, #2563eb); }
    .tag { text-transform: uppercase; letter-spacing: .12em; }
    .grow { transition: width .2s; } small { font-size: 10px; }
    @keyframes pulse { from { opacity: .5 } to { opacity: 1 } }
    </style></head><body>
    <a href="#">lien</a><input type="text"><img src="https://cdn.example.com/x.png">
    <button>🚀</button><p>Genève · 12 mars · 3 places</p></body></html>"""
    got = rules(bad)
    for r in ("tokens", "transition-all", "overflow-hidden", "100vw", "purple-blue-gradient", "tracked-caps",
              "animate-layout", "tiny-text", "focus-visible", "reduced-motion", "lang", "viewport", "label",
              "img-alt", "external", "emoji-icon", "middot-meta", "contrast"):
        assert r in got, r


def test_contrast_resolves_tokens_and_matches_wcag():
    assert round(contrast((0, 0, 0), (1, 1, 1)), 1) == 21.0
    low = CLEAN.replace("--ink: #1d1b19", "--ink: #c9c3bb")
    assert "contrast" in rules(low)


def test_report_is_bounded_on_a_huge_page():
    many = "<html lang=x><head><meta name=viewport><style>" + "".join(
        f".c{i} {{ color: #{i:06x}; transition: all 1s; }}" for i in range(500)) + "</style></head></html>"
    out = report(check(many))
    assert len(out.splitlines()) <= 41


def test_design_check_tool_reads_only_inside_the_working_folder(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "page.html").write_text(CLEAN, encoding="utf-8")
    (tmp_path / "dehors.html").write_text(CLEAN, encoding="utf-8")
    off = PageToolSource(PageStore(tmp_path / "pages"), lambda _t: root)
    assert {s.name for s in off.specs()} == {"page_create"}  # drapeau `local.design_check` éteint par défaut
    ctx0 = ToolContext(thread_id="t1", turn_index=0, channel="cli", sender="alice")
    assert off.call("design_check", {"path": "page.html"}, ctx0).is_error
    src = PageToolSource(PageStore(tmp_path / "pages"), lambda _t: root, design_check=True)
    spec = {s.name: s for s in src.specs()}["design_check"]
    assert spec.effect.name == "READ"
    ctx = ToolContext(thread_id="t1", turn_index=0, channel="cli", sender="alice")
    ok = src.call("design_check", {"path": "page.html"}, ctx)
    assert not ok.is_error and "rien au plancher" in ok.content
    out = src.call("design_check", {"path": "../dehors.html"}, ctx)
    assert out.is_error and "hors du dossier" in out.content
    assert not (tmp_path / "pages").exists() or not any((tmp_path / "pages").rglob("*.html"))  # aucune page créée


def test_findings_point_at_the_real_line():
    html = "<html lang=fr><head><meta name=viewport>\n<style>\n:root { --a: #000; }\n\n.x { transition: all 1s; }\n</style>"
    f = next(f for f in check(html) if f.rule == "transition-all")
    assert f.line == 5


def test_large_text_needs_3_to_1_only():
    page = CLEAN.replace("</style>", ".big { color: #8a8580; background: #fbf7f1; font-size: 28px; }\n"
                                     ".small { color: #8a8580; background: #fbf7f1; font-size: 16px; }\n</style>")
    hits = [f.message for f in check(page) if f.rule == "contrast"]
    assert len(hits) == 1 and "small" in hits[0] and "3:1 pour un grand texte" in hits[0]
