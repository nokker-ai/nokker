"""Les installeurs Lokkal ne mettent JAMAIS le jeton local dans une URL (`#token=`, fuite du 18.11.2026) : install.sh
emet un code d'acces a usage unique (`#code=`), install.ps1 aussi (le code est emis DANS WSL, tests/unit/test_install_ps1_code.py).
Les commentaires peuvent nommer l'ancien format ; le code, non."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "deploy" / "lokkal"


@pytest.mark.parametrize("name", ["install.sh", "install.ps1"])
def test_installer_never_builds_a_token_url(name):
    code = [ln for ln in (ROOT / name).read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#")]
    hits = [ln.strip() for ln in code if re.search(r"#token=|[?&]token=", ln)]
    assert hits == []


def test_install_ps1_never_reads_the_token_into_powershell():
    src = (ROOT / "install.ps1").read_text(encoding="ascii")
    assert "$tok" not in src and "Start-Process $openUrl" not in src
    assert "TODO(2026-11-25, rc.9.39)" not in src  # fait : le code est emis (Invoke-WslLocal), plus de repli provisoire
    assert "Invoke-WslLocal" in src and "setup#code=" in src
