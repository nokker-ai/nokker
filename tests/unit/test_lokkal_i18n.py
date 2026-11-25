"""Langue des messages de l'installeur et de la sauvegarde Lokkal (deploy/lokkal/tests/i18n.sh).

Anglais par défaut, français par la règle de la commande nokker (--lang > NOKKER_LANG > LC_ALL > LC_MESSAGES > LANG).
Le script contrôle install.sh et backup.sh (catalogue complet, mêmes %s, ordre des variables, sous dash/bash/busybox),
et install.ps1 quand pwsh est installé (ASCII sans BOM, catalogue, Get-NokkerLang, T).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "lokkal" / "tests" / "i18n.sh"


@pytest.mark.skipif(shutil.which("sh") is None, reason="sh absent")
def test_lokkal_scripts_i18n():
    r = subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
