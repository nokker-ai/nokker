"""Capture du noyau pendant un login (`McpBrowserDriver.screenshot`) : flou posé AVANT la capture sur les mêmes champs
que sites-chrome — mot de passe, carte (`cc-*`), `one-time-code`, `data-nk-secret`, champs 2FA reconnus au libellé."""
from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from nokker.sites.driver_mcp import BLUR_SCRIPT, BLUR_SELECTORS, OTP_LABEL_RE, McpBrowserDriver


class Caller:
    def __init__(self):
        self.calls = []

    def call(self, name, args):
        self.calls.append((name, args))
        if name == "browser_take_screenshot":
            return "png", False
        return "", False


def test_screenshot_blurs_before_capture_with_the_full_selector_set():
    c = Caller()
    drv = McpBrowserDriver(c, profile=b"")
    assert drv.screenshot() == b"png"
    names = [n for n, _ in c.calls]
    assert names.index("browser_evaluate") < names.index("browser_take_screenshot")
    script = dict(c.calls)["browser_evaluate"]["function"]
    assert script == BLUR_SCRIPT
    for sel in ("input[type=password]", "input[autocomplete^='cc-']", "input[autocomplete='one-time-code']",
                "[data-nk-secret]"):
        assert sel in BLUR_SELECTORS and sel in script


@pytest.mark.parametrize("label", ["Code de vérification", "One-time code", "Enter your 2FA code", "OTP",
                                   "Bestätigungscode", "Codice di verifica", "SMS-Code", "Authenticator app code"])
def test_otp_labels_are_recognized(label):
    assert re.search(OTP_LABEL_RE, label, re.IGNORECASE)


@pytest.mark.parametrize("label", ["Prénom", "E-mail", "Code postal", "Rechercher"])
def test_ordinary_labels_are_not(label):
    assert not re.search(OTP_LABEL_RE, label, re.IGNORECASE)


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_script_is_valid_javascript(tmp_path):
    f = tmp_path / "s.js"
    f.write_text(f"const f = {BLUR_SCRIPT};\nconsole.log(typeof f);\n", encoding="utf-8")
    assert subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=20).stdout.strip() == "function"
