"""Banc du SSO : un propriétaire, un projet, une connexion OIDC vers le faux fournisseur, un domaine prouvé, et une
fonction `login` qui joue le parcours complet d'un navigateur (start → fournisseur → callback signé par la clé de
l'appareil)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from nokker.approvals.memory import InMemoryApprovals
from nokker.sso.idp import Idp
from nokker.sso.secrets import MemorySsoSecrets
from nokker.sso.service import Sso
from nokker.sso.store import MemorySsoStore
from nokker.team.service import Team
from nokker.team.store import MemoryTeamStore
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.resolve import account_tenant_fn
from nokker.tenancy.store import MemoryTenancy
from tests.unit.approvals.conftest import gen_keypair, sign_p1363
from tests.unit.sso import fake_idp as f

REDIRECT = "https://app.example.com/sso/callback"


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


@dataclass
class Rig:
    sso: Sso
    team: Team
    tenancy: MemoryTenancy
    approvals: InMemoryApprovals
    idp: f.FakeIdp
    clock: Clock
    owner: Any
    pid: str
    cid: str
    dns: dict[str, list[str]]
    responses: list[Any] = field(default_factory=list)

    def as_owner(self, fn, *a, **kw):
        with tenant_scope(self.owner.tenant_id):
            out = fn(self.owner, *a, **kw)
        self.responses.append(out)
        return out

    def begin(self, *, connection: str | None = None, email: str | None = None, name: str = "portable"):
        priv, spki = gen_keypair()
        body: dict[str, Any] = {"public_key_spki": spki, "name": name}
        if email:
            body["email"] = email
        else:
            body["connection"] = connection or self.cid
        out = self.sso.start(body, "198.51.100.7")
        self.responses.append(out)
        grant = self.idp.authorize(out["authorize_url"])
        return priv, grant

    def finish(self, priv, grant, *, sign_with=None, **over):
        state = grant["state"]
        body = {"state": state, "code": grant["code"],
                "signature": sign_p1363(sign_with or priv, f"{state}.oidc".encode()), **over}
        out = self.sso.callback(body, "198.51.100.7")
        self.responses.append(out)
        return out

    def login(self, email: str = "alice@example.com", sub: str = "u-1001", groups: list[str] | None = None,
              **claims: Any) -> dict[str, Any]:
        self.idp.next_claims = {"email": email, "sub": sub, "groups": ["eng"] if groups is None else groups,
                                "now": self.clock.t, **claims}
        priv, grant = self.begin()
        return self.finish(priv, grant)

    def role_of(self, account_id: str, pid: str | None = None) -> str | None:
        m = self.team.store.access.get((pid or self.pid, account_id))  # type: ignore[attr-defined]
        return m.role if m else None


def make_rig() -> Rig:
    clock = Clock()
    tenancy = MemoryTenancy()
    owner = tenancy.create_account("owner@example.com")
    approvals = InMemoryApprovals(account_tenant=account_tenant_fn(tenancy), clock=clock)
    team = Team(store=MemoryTeamStore(), tenancy=tenancy, clock=clock)
    idp = f.FakeIdp()
    dns: dict[str, list[str]] = {}
    sso = Sso(store=MemorySsoStore(), team=team, tenancy=tenancy, approvals=approvals, secrets=MemorySsoSecrets(),
              idp=Idp(http=idp, clock=clock), redirect_uri=REDIRECT, resolve_txt=lambda name: dns.get(name, []),
              clock=clock)
    team.sso_gate = sso.gate_reason
    rig = Rig(sso=sso, team=team, tenancy=tenancy, approvals=approvals, idp=idp, clock=clock, owner=owner, pid="",
              cid="", dns=dns)
    rig.pid = rig.as_owner(team.create, {"name": "Plateforme"})["id"]
    conn = rig.as_owner(sso.create_connection, {"name": "Acme", "issuer": f.ISSUER, "client_id": f.CLIENT_ID,
                                                "client_secret_name": "acme-oidc"})
    rig.cid = conn["id"]
    rig.as_owner(sso.set_client_secret, rig.cid, {"value": f.CLIENT_SECRET})
    d = rig.as_owner(sso.add_domain, rig.cid, {"domain": "example.com"})
    dns[d["txt"]["name"]] = [d["txt"]["value"]]
    rig.as_owner(sso.verify_domain, rig.cid, "example.com")
    rig.as_owner(sso.put_binding, rig.pid, {"connection": rig.cid, "required": False, "mappings": [
        {"group": "eng", "role": "member"}, {"group": "leads", "role": "maintainer"},
        {"group": "auditors", "role": "viewer"}]})
    return rig


@pytest.fixture
def rig() -> Rig:
    return make_rig()
