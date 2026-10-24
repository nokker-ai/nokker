import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import egress  # noqa: E402


@pytest.mark.parametrize("ip", ["10.250.90.2", "172.18.0.1", "192.168.1.1", "127.0.0.1", "169.254.169.254",
                                "100.64.0.7", "::1", "fd00::1", "::ffff:10.0.0.1", "0.0.0.0"])
def test_bloque(ip):
    assert egress.is_blocked(ip)


@pytest.mark.parametrize("ip", ["151.101.0.223", "1.1.1.1", "2606:4700::1111"])
def test_public(ip):
    assert not egress.is_blocked(ip)
