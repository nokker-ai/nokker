"""Pile complète des sites connectés sur le site simulé (tests)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from nokker.qa.secrets import KnownSecrets
from nokker.secrets.base import SecretInfo
from nokker.sites.capture import SecretCapture
from nokker.sites.credentials import Credentials
from nokker.sites.crypto import seal_value
from nokker.sites.fakesocial import LINKEDIN_FR, Account, FakeBrowser, FakeServer
from nokker.sites.governor import LINKEDIN_LIMITS, Governor
from nokker.sites.model import State
from nokker.sites.profiles import ProfileStore
from nokker.sites.requests import Requests
from nokker.sites.service import CONSENT_VERSION, SitesService
from nokker.sites.store import SitesStore
from nokker.sites.tools import SitesToolSource
from nokker.core.ports import ToolContext

CANARY_PASSWORD = "Canari-Pw!7341xQ"
CANARY_CODE = "903117"
OWNER = "alex"


@dataclass
class MemoryVault:
    """`SecretsProvider` en mémoire qui compte les lectures (`get`)."""

    kind: str = "file"
    scope: str = "user"
    values: dict[str, str] = field(default_factory=dict)
    gets: list[str] = field(default_factory=list)

    def get(self, name):
        self.gets.append(name)
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value
        return SecretInfo(name, datetime.now(UTC))

    def delete(self, name):
        return self.values.pop(name, None) is not None

    def list(self):
        return [SecretInfo(n, datetime.now(UTC)) for n in sorted(self.values)]


class Clock:
    def __init__(self, t=None):
        self.t = t if t is not None else time.mktime((2026, 11, 5, 10, 0, 0, 0, 0, -1))

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@dataclass
class Stack:
    server: FakeServer
    vault: MemoryVault
    store: SitesStore
    requests: Requests
    service: SitesService
    source: SitesToolSource
    capture: SecretCapture
    known: KnownSecrets
    profiles: ProfileStore
    browsers: list
    mail: list
    notes: list

    def ctx(self, thread="t1", sender="alex", channel="api"):
        return ToolContext(thread_id=thread, turn_index=0, channel=channel, sender=sender)

    def answer(self, kind, value, timeout=5.0):
        """Répond à la demande ouverte (comme la mini-app : saisie scellée)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for r in list(self.requests._by_id.values()):
                if r.kind == kind and not r.answered and not r.cancelled:
                    if kind == "app_approval":
                        self.requests.answer_done(r.id, r.account)
                    else:
                        self.requests.answer_sealed(r.id, seal_value(r.machine_key, r.id, value), r.account)
                    return r
            time.sleep(0.01)
        raise AssertionError(f"aucune demande {kind}")

    def connected(self, cid, timeout=5.0):
        flow = self.service.wait_flow(cid, timeout)
        return flow

    def everything(self, extra_text=""):
        """Tout ce qui pourrait garder une copie, en texte (pour les canaris)."""
        import json

        parts = [extra_text, json.dumps(self.store._data, ensure_ascii=False)]
        for c in self.store.all():
            raw = self.profiles.raw(c.account, c.id)
            if raw:
                parts.append(raw.decode("latin-1"))
        parts.append(repr(self.requests._by_id))
        return "\n".join(parts)


def make_stack(*, skin=None, mfa="none", clock=None, mail_links=True, tmp=None) -> Stack:
    clock = clock or Clock()
    server = FakeServer(skin=dict(skin or LINKEDIN_FR)).seed()
    server.accounts["alex@example.org"] = Account("alex@example.org", CANARY_PASSWORD, "Alex Martin", mfa=mfa,
                                                  code=CANARY_CODE)
    vault = MemoryVault()
    store = SitesStore(tmp / "sites.json" if tmp else None)
    known = KnownSecrets()
    requests = Requests()
    browsers, mail, notes = [], [], []

    def factory(conn, profile):
        b = FakeBrowser(server, profile)
        browsers.append(b)
        return b

    def links(conn, sender, since):
        mail.append((conn.id, sender))
        for m in reversed(server.outbox):
            if m["from"] == sender and m["to"] == conn.username:
                return m["link"]
        return None

    from nokker.sites import recipes

    recipe_map = dict(recipes.RECIPES)
    if skin is not None and skin is not LINKEDIN_FR:
        recipe_map = {"fakesocial": recipes.FAKESOCIAL_RECIPE}
    creds = Credentials(vault, store, known)
    profiles = ProfileStore(b"k" * 32, tmp / "profiles" if tmp else None)
    service = SitesService(store=store, credentials=creds, requests=requests, profiles=profiles,
                           driver_factory=factory, recipes=recipe_map,
                           governor_factory=lambda r: Governor(limits=dict(LINKEDIN_LIMITS), clock=clock,
                                                               sleep=clock.sleep, busy_wait_s=0.2),
                           mail_links=links if mail_links else None,
                           notify=lambda c, ev, d: notes.append((c.id, ev, {k: v for k, v in d.items()
                                                                             if k != "link"})),
                           secure_link=lambda rid: f"https://app.nokker.ai/sites/r/{rid}", clock=clock,
                           sleep=lambda s: None, handoff_wait_s=1.0, handoff_poll_s=0.1, relogin_wait_s=5.0)
    store.consent(OWNER, "linkedin", CONSENT_VERSION)
    store.consent(OWNER, "fakesocial", CONSENT_VERSION)
    from nokker.connectors.service import HouseholdInfo, Member

    members = (Member("alex", "alex@example.org", "tn_a"), Member("camille", "camille@example.org", "tn_c"))
    households = type("H", (), {"of": staticmethod(lambda account: HouseholdInfo("hh_1", "tn_h", members))})()
    source = SitesToolSource(service, lambda: OWNER, households=households)

    def save_password(account, cid, value):
        conn = store.get(cid)
        if conn is None or conn.account != account:
            return None
        creds.save(conn, value)
        return conn.label

    capture = SecretCapture(requests, save_password=save_password,
                            labels=lambda a: [(c.id, c.label) for c in store.of(a)], who=lambda s: OWNER,
                            remember=known.add, clock=clock)
    return Stack(server, vault, store, requests, service, source, capture, known, profiles, browsers, mail, notes)


def connect_ok(st: Stack, label=None) -> str:
    """Ajoute le compte LinkedIn d'Alex et le connecte (mot de passe par la mini-app)."""
    args = {"site": "LinkedIn", "username": "alex@example.org"}
    if label:
        args["label"] = label
    import json
    import threading

    out = {}

    def run():
        out["r"] = st.source.call("sites_connect", args, st.ctx())

    t = threading.Thread(target=run)
    t.start()
    st.answer("password", CANARY_PASSWORD)
    t.join(5)
    res = json.loads(out["r"].content)
    flow = st.service.wait_flow(res["id"], 5)
    assert flow.status == State.CONNECTED.value, flow
    return res["id"]
