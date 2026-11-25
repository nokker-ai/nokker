"""Le lanceur du kiosque voyage avec le paquet Lokkal (bundle.sh → kiosk/, install.sh → ~/.nokker/kiosk/), sans être
installé ni démarré par l'installeur (docs/KIOSK.md §4)."""
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LOKKAL = ROOT / "deploy" / "lokkal"


def test_install_copies_kiosk_dir_and_never_enables_it():
    text = (LOKKAL / "install.sh").read_text(encoding="utf-8")
    assert 'cp -R "$TMP/nokker/kiosk" "$DIR/kiosk"' in text
    assert "nokker-kiosk.service" not in text.replace("docs/KIOSK.md", "")  # aucune unit posée ni activée ici
    assert "systemctl --user enable --now nokker-kiosk" not in text


@pytest.mark.skipif(subprocess.run(["bash", "--version"], capture_output=True).returncode != 0, reason="bash absent")
def test_bundle_ships_launcher_and_unit(tmp_path):
    subprocess.run(["bash", str(LOKKAL / "bundle.sh"), "0.0.0-test", str(tmp_path)], check=True, capture_output=True)
    with tarfile.open(next(tmp_path.glob("nokker-lokkal-*.tar.gz"))) as t:
        names = t.getnames()
        sh = t.getmember("nokker/kiosk/nokker-kiosk.sh")
    assert "nokker/kiosk/nokker-kiosk.service" in names
    assert sh.mode & 0o111  # exécutable
