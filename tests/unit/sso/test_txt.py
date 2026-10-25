"""Preuve d'un domaine par TXT via un résolveur DoH JSON (nokker/sso/txt.py), faux client HTTP : aucun réseau."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from nokker.sso.idp import IdpError
from nokker.sso.txt import doh_resolver


class FakeDoh:
    def __init__(self, answer, status=200):
        self.answer, self.status, self.urls = answer, status, []

    def get(self, url):
        self.urls.append(url)
        return self.status, json.dumps(self.answer).encode()


def test_txt_records_are_read_and_unquoted():
    http = FakeDoh({"Status": 0, "Answer": [
        {"name": "_nokker-sso.example.com", "type": 16, "data": '"nokker-sso=abc"'},
        {"name": "_nokker-sso.example.com", "type": 16, "data": '"v=spf1" "-all"'},
        {"name": "x", "type": 5, "data": "cname.example.com."}]})
    got = doh_resolver(http, "https://dns.example.net/dns-query")("_nokker-sso.example.com")
    assert got == ["nokker-sso=abc", "v=spf1-all"]
    q = parse_qs(urlsplit(http.urls[0]).query)
    assert q["name"] == ["_nokker-sso.example.com"] and q["type"] == ["TXT"]


def test_nxdomain_is_empty_and_errors_raise():
    assert doh_resolver(FakeDoh({"Status": 3}), "https://dns.example.net/q")("a.example.com") == []
    with pytest.raises(IdpError):
        doh_resolver(FakeDoh({}, status=500), "https://dns.example.net/q")("a.example.com")
