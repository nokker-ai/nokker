"""Notes de projet d'équipe poussées ET tirées par la machine (lot X17 + miroir, docs/LOKKAL.md §7.15) : deux machines
de deux membres, le vrai service d'équipe de nokker.ai (magasin en mémoire) derrière un transport httpx simulé.

Prouve : curseur par projet, reprise après une coupure sans doublon ni perte, conflit déterministe (la dernière
écriture reçue gagne, l'autre gardée en quarantaine), oubli propagé, un lecteur ne pousse jamais, une note `local` ne
part jamais même si la source se trompe, et rien ne part tant que l'utilisateur n'a pas mis le projet en miroir sur
SA machine."""
from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from nokker.local.team import TeamRules
from nokker.local.team_notes import TeamNotesSync
from nokker.team.model import TeamError
from nokker.team.service import Team
from nokker.team.store import MemoryTeamStore
from nokker.tenancy.store import MemoryTenancy
from tests.unit.context.test_project_memory import ctx, make_memory, make_repo

REMOTE = "git@forge.example:org/app.git"


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


class Cloud:
    """nokker.ai réduit aux trois routes machine de l'équipe ; `down` coupe le réseau, `fail_after` coupe au milieu."""

    def __init__(self, team: Team, machines: dict[str, tuple[object, str]]) -> None:
        self.team, self.machines = team, machines
        self.down = False
        self.fail_after: int | None = None
        self.calls: list[tuple[str, str, dict | None, str]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("réseau coupé", request=req)
        if self.fail_after is not None:
            if self.fail_after <= 0:
                raise httpx.ReadTimeout("coupure", request=req)
            self.fail_after -= 1
        actor, machine = self.machines[req.headers["Authorization"][7:]]
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, req.url.path, body, machine))
        parts = [p for p in req.url.path.split("/") if p][3:]  # après v1/mirror/projects
        try:
            if not parts:
                return httpx.Response(200, json=self.team.machine_projects(actor))
            if req.method == "GET":
                since = (parse_qs(req.url.query.decode()).get("since") or [None])[0]
                return httpx.Response(200, json=self.team.notes(actor, parts[0], since))
            return httpx.Response(200, json=self.team.push_notes(actor, parts[0], machine, body["notes"]))
        except TeamError as e:
            return httpx.Response(e.status, json={"error": {"code": e.code, "message": e.message}})


class Machine:
    def __init__(self, tmp_path, name: str, token: str, cloud: Cloud, clock: Clock) -> None:
        root = tmp_path / name
        root.mkdir()
        self.repo = make_repo(root, remote=REMOTE)
        self.pm, _ = make_memory(root)
        self.pm.open_session("th_1", self.repo, root)
        self.project = self.pm._session("th_1").project
        self.state: dict = {}
        client = httpx.Client(transport=httpx.MockTransport(cloud))
        self.rules = TeamRules(base_url="https://app.nokker.ai", token=lambda: token, state=self.state,
                               save=lambda: None, client=client, clock=clock, max_age_s=0)
        self.sync = TeamNotesSync(base_url="https://app.nokker.ai", token=lambda: token, state=self.state,
                                  save=lambda: None, memory=self.pm, team=self.rules, client=client, clock=clock)
        self.turn = 0

    def note(self, title: str, text: str) -> str:
        from nokker.context.project_memory import ProjectMemoryTools

        self.turn += 1
        self.pm.core_block("th_1", self.turn, "")
        r = ProjectMemoryTools(self.pm).call("memory_note", {"kind": "fact", "title": title, "text": text,
                                                             "derived_from": "repo", "scope": "project"},
                                             ctx(turn=self.turn))
        assert not r.is_error, r.content
        return r.content.split("noté : ", 1)[1].split(" ", 1)[0]

    def texts(self) -> dict[str, str]:
        return {n.name: n.text for n in self.pm.learned(self.project)}

    def edit(self, name: str, text: str) -> None:
        """Modification locale d'une note (comme un éditeur sur le fichier de la note)."""
        from nokker.context.project_memory import file_name

        p = self.pm.root / self.project / file_name(name)
        old = self.texts()[name]
        p.write_text(p.read_text(encoding="utf-8").replace(old, text), encoding="utf-8")

    def forget(self, name: str) -> None:
        from nokker.context.project_memory import ProjectMemoryTools

        r = ProjectMemoryTools(self.pm).call("memory", {"action": "forget", "name": name}, ctx(turn=self.turn))
        assert not r.is_error, r.content


@pytest.fixture
def w(tmp_path):
    tenancy = MemoryTenancy()
    accs = {k: tenancy.create_account(f"{k}@example.org") for k in ("owner", "mem", "view")}
    clock = Clock()
    team = Team(store=MemoryTeamStore(), tenancy=tenancy, mirror_allowed=lambda acc: True, clock=clock)
    p = team.create(accs["owner"], {"name": "App", "repos": [REMOTE]})
    for who, role in (("mem", "member"), ("view", "viewer")):
        team.accept(accs[who], team.invite(accs["owner"], p["id"], {"email": f"{who}@example.org", "role": role})["id"])
    team.patch(accs["owner"], p["id"], {"mirror": True})
    cloud = Cloud(team, {"mt_a": (accs["owner"], "mc_a"), "mt_b": (accs["mem"], "mc_b"),
                         "mt_v": (accs["view"], "mc_v")})

    class W:
        pass

    x = W()
    x.__dict__.update(team=team, a=accs, pid=p["id"], cloud=cloud, clock=clock,
                      A=Machine(tmp_path, "a", "mt_a", cloud, clock), B=Machine(tmp_path, "b", "mt_b", cloud, clock),
                      V=Machine(tmp_path, "v", "mt_v", cloud, clock))
    for m in (x.A, x.B, x.V):
        m.pm.set_mirrored(m.project, True)  # choix explicite sur CHAQUE machine
    return x


def _posts(cloud):
    return [b for m, path, b, _ in cloud.calls if m == "POST"]


def test_notes_go_both_ways_with_a_cursor_per_project(w):
    n = w.A.note("CI", "La CI tourne sur Forgejo.")
    assert w.A.sync.sync_once()["pushed"] == 1
    got = w.B.sync.sync_once()
    assert got["pulled"] == 1 and w.B.texts() == {n: "La CI tourne sur Forgejo."}
    key = f"{w.pid}|{w.B.project}"
    assert w.B.state["team_notes"][key]["cursor"] != "0"
    assert w.B.sync.status()["projects"][key]["name"] == "App"  # libellé pour l'écran Miroir
    # rien de neuf : B ne renvoie pas ce qu'il vient de tirer, A ne retire pas ce qu'il a poussé
    before = len(_posts(w.cloud))
    assert w.B.sync.sync_once() == {"pushed": 0, "pulled": 0, "forgotten": 0, "kept_local": 0}
    assert w.A.sync.sync_once()["pulled"] == 0 and len(_posts(w.cloud)) == before
    # B modifie, A tire la nouvelle version (base = celle qu'il avait : pas de conflit)
    w.B.edit(n, "La CI tourne sur Forgejo Actions.")
    assert w.B.sync.sync_once()["pushed"] == 1
    assert w.A.sync.sync_once()["pulled"] == 1 and w.A.texts()[n] == "La CI tourne sur Forgejo Actions."
    assert w.team.quarantined(w.a["owner"], w.pid)["quarantine"] == []


def test_network_cut_resumes_without_duplicate_or_loss(w):
    names = [w.A.note(f"Fait {i}", f"fait numéro {i} du projet") for i in range(5)]
    w.cloud.down = True
    with pytest.raises(httpx.HTTPError):
        w.A.sync.sync_once()
    assert w.team.notes(w.a["owner"], w.pid, None)["notes"] == []
    w.cloud.down = False
    w.A.sync.sync_once()
    assert sorted(n["name"] for n in w.team.notes(w.a["owner"], w.pid, None)["notes"]) == sorted(names)
    # B tire, coupé au milieu (après la liste des projets) : rien n'est compté tiré, le curseur n'a pas bougé
    w.cloud.fail_after = 1
    with pytest.raises(httpx.HTTPError):
        w.B.sync.sync_once()
    assert w.B.texts() == {}
    w.cloud.fail_after = None
    assert w.B.sync.sync_once()["pulled"] == 5 and sorted(w.B.texts()) == sorted(names)
    assert w.B.sync.sync_once()["pulled"] == 0  # pas de doublon
    # A pousse, la réponse se perd APRÈS l'écriture chez nokker.ai : le renvoi est sauté, pas dupliqué
    w.A.edit(names[0], "fait numéro 0, corrigé")
    real = w.cloud.__call__

    def lose_reply(req):
        resp = real(req)
        if req.method == "POST":
            raise httpx.ReadTimeout("réponse perdue", request=req)
        return resp

    w.A.sync.client = httpx.Client(transport=httpx.MockTransport(lose_reply))
    with pytest.raises(httpx.HTTPError):
        w.A.sync.sync_once()
    w.A.sync.client = httpx.Client(transport=httpx.MockTransport(w.cloud))
    r = w.A.sync.sync_once()
    assert r["pushed"] == 1  # renvoyée…
    assert w.team.quarantined(w.a["owner"], w.pid)["quarantine"] == []  # … et sautée (même empreinte) : pas de conflit
    assert w.B.sync.sync_once()["pulled"] == 1 and w.B.texts()[names[0]] == "fait numéro 0, corrigé"


def test_conflict_last_received_wins_and_the_other_is_kept(w):
    n = w.A.note("Déploiement", "on déploie le mardi")
    w.A.sync.sync_once()
    w.B.sync.sync_once()
    # les deux machines modifient hors ligne la même note
    w.A.edit(n, "on déploie le jeudi")
    w.B.edit(n, "on déploie le vendredi")
    w.A.sync.sync_once()  # reçue d'abord
    w.clock.t += 5
    r = w.B.sync.sync_once()  # reçue ensuite : gagne
    assert r["pushed"] == 1
    assert [x["text"] for x in w.team.quarantined(w.a["owner"], w.pid)["quarantine"]] == ["on déploie le jeudi"]
    w.A.sync.sync_once()
    assert w.A.texts()[n] == w.B.texts()[n] == "on déploie le vendredi"
    # tirer AVANT d'avoir poussé une modification locale ne l'écrase pas : elle part au tour suivant (conflit gardé)
    w.B.edit(n, "on déploie le lundi")
    w.A.edit(n, "on déploie le samedi")
    w.A.sync.sync_once()
    pulled = w.B.sync._pull_only()  # B tire sans avoir encore poussé
    assert pulled["kept_local"] == 1 and w.B.texts()[n] == "on déploie le lundi"
    w.B.sync.sync_once()
    w.A.sync.sync_once()
    assert w.A.texts()[n] == w.B.texts()[n] == "on déploie le lundi"
    kept = [x["text"] for x in w.team.quarantined(w.a["owner"], w.pid)["quarantine"]]
    assert "on déploie le samedi" in kept


def test_forget_is_propagated_and_kept_in_history(w):
    n = w.A.note("Ancienne règle", "ne jamais toucher à legacy/")
    w.A.sync.sync_once()
    w.B.sync.sync_once()
    assert n in w.B.texts()
    w.A.forget(n)
    assert w.A.sync.sync_once()["pushed"] == 1
    tomb = w.team.notes(w.a["owner"], w.pid, None)["notes"][-1]
    assert tomb["deleted"]
    assert w.B.sync.sync_once()["forgotten"] == 1 and n not in w.B.texts()
    assert (w.B.pm.root / w.B.project / ".forgotten").is_dir()  # sur la machine aussi, rien n'est détruit
    hist = w.team.quarantined(w.a["owner"], w.pid)["quarantine"]
    assert hist[-1]["text"] == "ne jamais toucher à legacy/" and hist[-1]["reason"].startswith("oubliée")
    assert w.A.sync.sync_once() == {"pushed": 0, "pulled": 0, "forgotten": 0, "kept_local": 0}


def test_restored_version_reaches_every_machine_even_where_it_was_forgotten(w):
    n = w.A.note("Ancienne règle", "ne jamais toucher à legacy/")
    w.A.sync.sync_once()
    w.B.sync.sync_once()
    w.A.forget(n)
    w.A.sync.sync_once()
    w.B.sync.sync_once()
    assert n not in w.A.texts() and n not in w.B.texts()
    [q] = w.team.quarantined(w.a["owner"], w.pid)["quarantine"]
    w.team.restore_note(w.a["owner"], w.pid, q["id"])  # §7.23 : un maintainer remet la version oubliée
    assert w.A.sync.sync_once()["pulled"] == 1 and w.B.sync.sync_once()["pulled"] == 1
    assert w.A.texts()[n] == w.B.texts()[n] == "ne jamais toucher à legacy/"
    assert w.A.sync.sync_once() == {"pushed": 0, "pulled": 0, "forgotten": 0, "kept_local": 0}  # rien ne repart


def test_viewer_machine_pulls_but_never_pushes(w):
    n = w.A.note("CI", "La CI tourne sur Forgejo.")
    w.A.sync.sync_once()
    w.V.note("Avis", "le lecteur pense que la CI est lente")
    got = w.V.sync.sync_once()
    assert got["pulled"] == 1 and got["pushed"] == 0 and n in w.V.texts()
    assert not [c for c in w.cloud.calls if c[0] == "POST" and c[3] == "mc_v"]
    assert "lent" not in json.dumps(w.team.notes(w.a["owner"], w.pid, None))
    # même forcée (liste de projets qui ment), la poussée d'un lecteur est refusée par nokker.ai
    w.V.state["team_projects"]["projects"][0]["can_push_notes"] = True
    w.V.rules.max_age_s = 10 ** 9
    r = w.V.sync.sync_once()
    assert r["pushed"] == 0 and w.V.sync.status()["errors"][w.pid]["code"] == "forbidden"
    assert "lent" not in json.dumps(w.team.notes(w.a["owner"], w.pid, None))


def test_local_project_or_lying_source_never_leaves(w):
    w.B.pm.set_mirrored(w.B.project, False)  # B n'a pas choisi le miroir sur SA machine
    w.B.note("Secret maison", "le serveur de build est derrière le NAS")
    w.A.note("CI", "La CI tourne sur Forgejo.")
    w.A.sync.sync_once()
    assert w.B.sync.sync_once() == {"pushed": 0, "pulled": 0, "forgotten": 0, "kept_local": 0}
    assert not [b for b in _posts(w.cloud) if "NAS" in json.dumps(b)]
    # la source se trompe : la mémoire dit le projet en miroir mais rend ses notes `local`
    w.B.pm.set_mirrored(w.B.project, True)
    real = w.B.pm.learned

    def liar(project):
        out = real(project)
        for x in out:
            x.mirrored = False  # locality → local
        return out

    w.B.pm.learned = liar
    w.B.sync.sync_once()
    assert not [b for b in _posts(w.cloud) if "NAS" in json.dumps(b)]
    assert "NAS" not in json.dumps(w.team.notes(w.a["owner"], w.pid, None))
    # un projet retiré du miroir n'envoie pas d'oubli : ses notes restent au projet
    w.B.pm.learned = real
    w.B.sync.sync_once()
    w.B.pm.set_mirrored(w.B.project, False)
    w.B.sync.sync_once()
    assert not [n for n in w.team.notes(w.a["owner"], w.pid, None)["notes"] if n["deleted"]]


def test_resync_replays_from_zero_without_clobbering(w):
    n = w.A.note("CI", "La CI tourne sur Forgejo.")
    w.A.sync.sync_once()
    w.B.sync.sync_once()
    w.B.edit(n, "modif locale de B pas encore partie")
    w.B.sync.reset_cursors()
    assert all(v["cursor"] == "0" for v in w.B.state["team_notes"].values())
    w.B.sync._pull_only()
    assert w.B.texts()[n] == "modif locale de B pas encore partie"  # le tirage complet n'écrase rien de local
    w.B.sync.sync_once()
    assert w.team.notes(w.a["owner"], w.pid, None)["notes"][0]["text"] == "modif locale de B pas encore partie"
