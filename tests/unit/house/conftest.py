"""Un foyer complet pour la salle Maison : le VRAI noyau (`Kernel`) avec un faux modèle qui joue chaque agent d'après
l'en-tête de son message, de vraies approbations en mémoire, le vrai foyer (payeur, adulte, ado, enfant) en mémoire,
un foyer voisin, et des outils de la maison factices (domotique, courrier, veille). Aucun appel réseau."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace

import pytest

from nokker.approvals.executor import ApprovalExecutor
from nokker.approvals.memory import InMemoryApprovals
from nokker.context.engine import ZURICH, ContextEngineV0
from nokker.core.loop import Kernel
from nokker.core.ports import Completion, Recall, ToolSpec, call_effect
from nokker.core.turn import (
    Decision,
    Effect,
    Gate,
    Message,
    Tier,
    ToolCall,
    ToolResult,
    Usage,
    Verdict,
)
from nokker.house.room import HouseRoom
from nokker.house.roster import Settings
from nokker.house.store import MemoryRoomStore
from nokker.household.model import Settings as HHSettings
from nokker.household.model import consent_text
from nokker.household.service import Household
from nokker.household.store import MemoryHouseholdStore
from nokker.ledger.costs import InMemoryLedger
from nokker.store.pg import MemoryStore
from nokker.team.service import Team
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.store import MemoryTenancy
from tests.unit.api.test_server import NoEscalator, ShipOnlyPolicy

ATT = {"version": consent_text("fr").sha256, "lang": "fr", "legal_guardian": True}
PRIVATE_MEMORY = "CANARI-MEMOIRE-PRIVEE-7731"  # mémoire de profil d'un membre : jamais dans la salle
PRIVATE_MAIL = "CANARI-COURRIER-PERSO-5512"  # courrier personnel d'un membre : jamais dans la salle
HOUSE_NOTE = "Le code du portail change le premier du mois"  # note COMMUNE du foyer
_HEAD = re.compile(r"\[Salle Maison — tu es l'agent « [^»]+ » \(@([a-z]+)\)\.\]")


class Brain:
    """Faux modèle : joue l'agent nommé dans l'en-tête du DERNIER message humain. `plan[slug]` : pas rejoués dans
    l'ordre (("tool", nom, args) ou ("text", t)) ; au-delà : « <slug> : fini »."""

    def __init__(self):
        self.plan: dict[str, list] = {}
        self.default: dict[str, tuple] = {}  # pas joué quand le plan de l'agent est vide
        self.seen: list[list[dict]] = []
        self.calls = 0

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.seen.append(messages)
        self.calls += 1
        last_user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
        content = last_user.get("content")
        text = content if isinstance(content, str) else " ".join(
            p.get("text", "") for p in content or [] if isinstance(p, dict))
        heads = _HEAD.findall(text)
        if not heads:  # suite d'une carte : le dernier message humain est le bloc d'approbation, même fil d'agent
            heads = _HEAD.findall(" ".join(str(m.get("content")) for m in messages if m.get("role") == "user"))
        slug = heads[-1] if heads else "web"
        steps = self.plan.setdefault(slug, [])
        step = steps.pop(0) if steps else self.default.get(slug, ("text", f"{slug} : fini"))
        usage = Usage(prompt=100, completion=10)
        if step[0] == "echo":  # modèle qui recopie TOUT ce qu'il voit : la pire fuite possible
            step = ("text", "\n".join(str(m.get("content")) for m in messages))
        if step[0] == "tool":
            tc = ToolCall(f"c{self.calls}", step[1], dict(step[2]))
            return Completion(message=Message("assistant", tool_calls=[tc]), usage=usage, model="m", latency_s=0)
        return Completion(message=Message("assistant", content=step[1]), usage=usage, model="m", latency_s=0)

    def prompts(self) -> str:
        return "\n".join(str(m.get("content")) for msgs in self.seen for m in msgs)


class Gates:
    """Portes d'un testeur réduites : lecture permise ; écriture → carte standard ; destructif → carte forte."""

    def action(self, call, spec, turn):
        eff = call_effect(spec, call.arguments)
        if eff is Effect.READ:
            return [Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="effect:read")]
        return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=f"effect:{eff.value}",
                        strength="strong" if eff is not Effect.WRITE else "standard")]

    def switch(self, reply, turn, tier):
        return []


class Inv:
    def check_input(self, text):
        from nokker.core.turn import InputCheck

        return InputCheck(text=text)

    def check_action(self, call, spec, turn):
        return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="t")

    def redact_result(self, result, spec):
        return result

    def scrub_output(self, text):
        return text or ""


@dataclass
class HomeSource:
    """Outils factices d'un agent : lecture, écriture, action sensible. `calls` : (outil, membre)."""

    prefix: str
    content: str = "état : salon allumé"
    desc: tuple[str, str, str] = ("lit", "agit", "action sensible")  # descriptions (lire, agir, fort)
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    def specs(self):
        p = self.prefix
        r, a, f = self.desc
        return [ToolSpec(f"{p}_lire", r, {"type": "object", "properties": {}}, Effect.READ),
                ToolSpec(f"{p}_agir", a, {"type": "object", "properties": {}}, Effect.WRITE),
                ToolSpec(f"{p}_fort", f, {"type": "object", "properties": {}}, Effect.DESTRUCTIVE)]

    def call(self, name, arguments, ctx):
        self.calls.append((name, ctx.sender))
        return ToolResult("", name, self.content if name.endswith("_lire") else f"{name} fait")


class MemberTools:
    """Registre ordinaire d'un membre (sa session web) : son courrier personnel. Ne doit jamais entrer en salle."""

    def __init__(self):
        self.specs_ = {"mail_perso": ToolSpec("mail_perso", "courrier perso", {}, Effect.READ, core=True)}

    def core(self):
        return list(self.specs_.values())

    def spec(self, n):
        return self.specs_.get(n)

    def find(self, q, limit=5):
        return []

    def index(self):
        return []

    def call(self, n, a, ctx):
        return ToolResult("", n, PRIVATE_MAIL)


class PrivateMemory:
    """Mémoire du noyau ordinaire : la mémoire de PROFIL du membre (privée)."""

    def recall(self, q, k=5):
        return [Recall(note="profil", text=PRIVATE_MEMORY, score=1.0)]

    def propose(self, t, f):
        return None


def make_world(tmp_path, *, providers=None, settings=None):
    tenancy = MemoryTenancy()
    pay, adult, teen, child = (tenancy.create_account(f"{n}@example.com") for n in ("alice", "bob", "carol", "dan"))
    other = tenancy.create_account("eve@example.com")  # payeur d'un AUTRE foyer
    lone = tenancy.create_account("frank@example.com")  # sans foyer
    hh = Household(team=Team(store=MemoryHouseholdStore(), tenancy=tenancy), settings=HHSettings(minors=True))
    with tenant_scope(pay.tenant_id):
        first_household = hh.create(pay, {})["id"]
        ivs = {who: hh.invite(pay, {"email": who.email, **({"profile": prof, "attestation": ATT} if prof else {})})
               for who, prof in ((adult, None), (teen, "teen"), (child, "child"))}
    for who, iv in ivs.items():
        with tenant_scope(who.tenant_id):
            hh.accept(who, iv["id"])
    with tenant_scope(other.tenant_id):
        hh.create(other, {})
    brain = Brain()
    prefix = tmp_path / "prefix.md"
    prefix.write_text("PRÉFIXE", encoding="utf-8")
    member_tools = MemberTools()
    ctx = ContextEngineV0(tools=member_tools, prefix_path=str(prefix),
                          now_fn=lambda: datetime(2026, 11, 2, tzinfo=ZURICH))
    approvals = InMemoryApprovals(is_authorized=lambda u: True)
    kernel = Kernel(store=MemoryStore(), context=ctx, memory=PrivateMemory(), invariants=Inv(), gates=Gates(),
                    escalator=NoEscalator(), policy=ShipOnlyPolicy(), providers={Tier.SHIP: brain},
                    tools=member_tools, ledger=InMemoryLedger(), approvals=approvals, max_calls=8)
    home = HomeSource("maison")
    paper = HomeSource("courrier", content="facture d'électricité à payer le 15")
    watch = HomeSource("veille", content="caméra entrée : mouvement à 03:12")
    provs = {"maison": home, "courrier": paper, "veille": watch} if providers is None else providers
    published: list[tuple[str | None, str, dict]] = []

    def publish(event, data):
        from nokker.tenancy.context import current_tenant_or_none

        published.append((current_tenant_or_none(), event, data))

    def recall(account_id, query, k):
        found = hh.membership(account_id)
        if found is None:
            return []
        mark = " CANARI-NOTE-A" if found[1].id == first_household else " (foyer voisin)"
        return [Recall(note="foyer:portail", text=f"[Mémoire du foyer] portail — {HOUSE_NOTE}{mark}", score=1.0)]

    room = HouseRoom(household=hh, kernel=kernel, store=MemoryRoomStore(), settings=settings or Settings(),
                     providers=provs, recall=recall, publish=publish)
    executor = ApprovalExecutor(kernel=kernel, approvals=approvals, kernel_for=room.kernel_for)
    return SimpleNamespace(hh=hh, tenancy=tenancy, pay=pay, adult=adult, teen=teen, child=child, other=other,
                           lone=lone, brain=brain, kernel=kernel, room=room, approvals=approvals, executor=executor,
                           home=home, paper=paper, watch=watch, published=published, member_tools=member_tools)


@pytest.fixture
def w(tmp_path):
    return make_world(tmp_path)


def post(w, who, text, **kw):
    with tenant_scope(who.tenant_id):
        return w.room.post(who.account_id, text, **kw)


def messages(w, who, **kw):
    with tenant_scope(who.tenant_id):
        return w.room.messages(who.account_id, **kw)["messages"]


def everything(w) -> str:
    """Tout ce qui est écrit dans TOUTES les salles (journal brut) et publié."""
    rows = " ".join(r.text for r in w.room.store.rows)
    return rows + " " + " ".join(str(d) for _, _, d in w.published)


