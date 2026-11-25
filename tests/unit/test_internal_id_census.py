"""Recensement des identifiants générés par le noyau (C7f) : tout préfixe d'id fabriqué dans nokker/ est soit caviardé
par `scrub_internal_ids`, soit listé comme sans danger AVEC sa raison (nokker/core/idforms.py). Un nouveau générateur
dont le préfixe n'est dans aucune des deux tables fait échouer ce test : on le classe."""
from __future__ import annotations

import re
import secrets
import uuid
from pathlib import Path

import pytest

from nokker.core.idforms import B64URL, HEX, SAFE, SCRUBBED, scrubbed_prefixes
from nokker.core.loop import scrub_internal_ids

ROOT = Path(__file__).resolve().parents[2] / "nokker"
GEN = re.compile(r"token_hex|token_urlsafe|uuid4?\(|new_id\(|_new_id\(|monotonic_ns|hexdigest|new_[a-z_]*id\(|secrets\.choice")
LITERALS = (
    re.compile(r"""\bf?["']([a-z]{1,6}[_-])(?:\{|["']\s*\+)"""),
    re.compile(r"""["']([a-z]{1,6}[_-])["']\s*\+"""),
    re.compile(r"""\b(?:new_id|_new_id)\(\s*["']([a-z]{1,6})["']"""),
)


def generated_prefixes() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(ROOT.rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if not GEN.search(line) or line.lstrip().startswith("#"):
                continue
            for k, rx in enumerate(LITERALS):
                for m in rx.finditer(line):
                    pre = m.group(1) if k < 2 else m.group(1) + "_"
                    found.setdefault(pre, f"{path.relative_to(ROOT.parent)}:{i + 1}")
    return found


def test_every_generated_prefix_is_scrubbed_or_declared_safe():
    known = scrubbed_prefixes() | set(SAFE)
    unknown = {p: w for p, w in generated_prefixes().items() if p not in known}
    assert not unknown, (f"préfixe(s) d'identifiant généré(s) sans décision : {unknown}. Ajouter à SCRUBBED (la personne "
                         "ne doit pas le lire) ou à SAFE avec la raison, dans nokker/core/idforms.py")


def test_census_sees_the_known_generators():
    found = generated_prefixes()
    assert {"th_", "bu_", "tn_", "ag_", "gl_", "mc_"} <= set(found) and len(found) > 40  # le scanneur ne dort pas


def test_safe_prefixes_are_justified_and_not_also_scrubbed():
    assert all(len(why) > 15 for why in SAFE.values())
    scrubbed = {p for p, _, _ in SCRUBBED}
    assert not (scrubbed & set(SAFE))


def _sample(prefix: str, chars: str, n: int | None) -> str:
    if n is None:
        return prefix + secrets.token_urlsafe(24)[:24]
    if chars == HEX:
        return prefix + ("1" + secrets.token_hex(n))[:n]
    if chars == B64URL:
        return prefix + ("aB3-_" * 8)[:n]
    return prefix + chars[:n]  # alphabet réduit donné en clair


@pytest.mark.parametrize("prefix,chars,n", SCRUBBED, ids=[p for p, _, _ in SCRUBBED])
def test_each_generated_form_is_removed(prefix, chars, n):
    ident = _sample(prefix, chars, n)
    assert scrub_internal_ids(f"voir {ident} puis continuer") == "voir puis continuer", ident


def test_real_generators_are_removed():
    from nokker.agents.base import new_agent_id, new_run_id
    from nokker.bubbles.base import new_bubble_id
    from nokker.threads.base import new_thread_id
    from nokker.tenancy.store import _new_id
    ids = [new_agent_id(), new_run_id(), new_bubble_id(), new_thread_id(), _new_id("tn"), _new_id("us"),
           str(uuid.uuid4()), f"th_{uuid.uuid4().hex[:16]}", f"x/sa-{uuid.uuid4().hex[:8]}"]
    for _ in range(50):  # pas de chiffre dans un hex tiré : rare, jamais 50 fois de suite
        for ident in ids:
            out = scrub_internal_ids(f"cf. {ident} svp")
            assert out == "cf. svp" or (ident.startswith("x/") and out == "cf. x svp"), (ident, out)
        ids = [new_agent_id(), new_run_id(), new_bubble_id(), new_thread_id(), str(uuid.uuid4())]


def test_subagent_thread_is_removed_whole():
    assert scrub_internal_ids("le fil th_0123456789ab/sa-1a2b3c4d a échoué") == "le fil a échoué"


@pytest.mark.parametrize("text", [
    "le champ ag_total vaut 3", "df.ag_total + ar_results", "th_count et bu_name restent", "bg_color: red",
    "sa-lesson, sa-deadbeef", "mp_ab12 est court", "cli-args et code-review et mcp-server", "pg_dump et pg_basebackup",
    "app_name = 'x'", "st_mtime, st_size, dr_rows", "ex_ante, in_progress, as_dict, fd_open", "dn_resolver",
    "rs_codes ep_status md_files cs_total", "SELECT * FROM lk-table", "whsec_court", "ev_name",
    "123e4567-e89b-12d3-a456 (uuid tronqué)", "version 1a2b3c4d",
])
def test_ordinary_text_is_untouched(text):
    assert scrub_internal_ids(text) == text
