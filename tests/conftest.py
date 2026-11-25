"""Toute la suite : le cache disque des aperçus de liens (`NokkerConfig.previews_cache_dir`, défaut
`/var/lib/nokker/previews`, chemin du conteneur) part dans un dossier temporaire. Sans ça, le premier test qui construit
le singleton d'aperçus (`build_hub` avec la config par défaut) échoue hors root (PermissionError), et lesquels tombent
dépend de l'ordre des tests : la CI publique tourne en utilisateur normal."""
from __future__ import annotations

import inspect

import pytest

from nokker.config import NokkerConfig


@pytest.fixture(autouse=True, scope="session")
def _previews_cache_in_tmp(tmp_path_factory):
    names = list(inspect.signature(NokkerConfig).parameters)
    defaults = list(NokkerConfig.__init__.__defaults__)
    i = names.index("previews_cache_dir") - (len(names) - len(defaults))
    defaults[i] = str(tmp_path_factory.mktemp("previews"))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(NokkerConfig.__init__, "__defaults__", tuple(defaults))
        assert NokkerConfig().previews_cache_dir == defaults[i]
        yield


# ── fils de fond du noyau : aucun ne survit à son test ───────────────────────────────────────────────────────────────
import re  # noqa: E402
import threading  # noqa: E402


@pytest.fixture(autouse=True)
def _stop_kernel_background_threads():
    """Un noyau construit par un test (`build_kernel` → `memory.from_config`) démarre des fils de fond. Sans arrêt, le
    contrôle de démarrage de la mémoire longue (« nouvel essai dans 15 s », 10 min) survivait à son test et pompait
    CPU/connexions pendant toute la suite. Après chaque test : arrêt (idempotent) des fils connus."""
    yield
    from nokker.memory import corthexis

    corthexis.stop_all_warm_ups()


def _stop_server_and_loop_threads() -> None:
    """Serveurs HTTP (`serve_forever`) et boucles asyncio (`run_forever`) démarrés dans un fil par un test : arrêt par
    leur propre API (`shutdown` + `server_close`, `loop.stop`), jamais en remplaçant un constructeur. Le propriétaire est
    lu sur l'appelable cible du fil (méthode liée) ; un fil dont la cible n'est pas l'une de ces deux méthodes est ignoré."""
    for t in threading.enumerate():
        target = getattr(t, "_target", None)
        owner, name = getattr(target, "__self__", None), getattr(target, "__name__", "")
        if owner is None or not t.is_alive() or t is threading.current_thread():
            continue
        try:
            if name == "serve_forever" and hasattr(owner, "server_close"):
                owner.shutdown()
                owner.server_close()
            elif name == "run_forever" and hasattr(owner, "call_soon_threadsafe"):
                owner.call_soon_threadsafe(owner.stop)
        except Exception:  # noqa: BLE001 — le ménage ne masque jamais le test
            pass


@pytest.fixture(autouse=True, scope="module")
def _stop_module_background_resources():
    """Fin de module (après ses fixtures de module) : ressources de fond inscrites au registre `bg_stop` (ponts maison,
    sessions MCP, boucle AirPlay, balayage des notifications, flux de connexion des sites, écoute du heurtoir) et serveurs
    HTTP de test encore à l'écoute. Par module et non par test : une fixture de module peut garder son serveur."""
    yield
    from nokker import bg_stop

    bg_stop.stop_all()
    _stop_server_and_loop_threads()


LEAKED_DAEMON_MAX = 10


@pytest.fixture(autouse=True, scope="session")
def _no_leaked_nokker_threads():
    """Garde de fin de suite : un fil `nokker-*` NON daemon (il bloquerait la sortie du process) est un bug, et la suite
    échoue. Les fils daemon `nokker-*` encore vivants (serveurs de test, boucles à arrêt par process) ne font que
    s'afficher : leur nombre est borné par les tests qui les créent, pas par le temps."""
    before = {t.ident for t in threading.enumerate()}
    yield
    left = [t for t in threading.enumerate() if t.ident not in before and t.name.startswith("nokker-") and t.is_alive()]
    # Les ouvriers d'un ThreadPoolExecutor (`nokker-recall_0`…) ne sont pas daemon mais s'éteignent à la sortie : exclus.
    bad = [t.name for t in left if not t.daemon and not re.fullmatch(r"nokker-.*_\d+", t.name)]
    assert not bad, f"fils de fond non daemon laissés par la suite : {bad}"
    # Fils daemon des ponts et services (passerelle du foyer, heurtoir, AirPlay, MCP, sites, balayages) : chacun a un arrêt
    # idempotent et la fixture de module les arrête ; ils étaient ~450 en fin de suite (1 à 2 par test), 0 après correction.
    # Seuil bas avec marge : une fuite par test le dépasse au premier ensemble de tests, un fil isolé ne fait pas flaker.
    leaked = [t.name for t in threading.enumerate() if t.ident not in before and t.is_alive() and t.daemon
              and re.match(r"(home-bridge-|knock-|nokker-(?!recall_)|mcp-io|sites-)", t.name)]
    assert len(leaked) <= LEAKED_DAEMON_MAX, f"{len(leaked)} fils daemon de fond laissés par la suite : {sorted(set(leaked))}"


@pytest.fixture(autouse=True, scope="session")
def _thread_report():
    """`NOKKER_THREAD_REPORT=<fichier>` : écrit, en fin de suite, le nombre de fils vivants (total et par nom)."""
    yield
    import collections
    import os

    path = os.environ.get("NOKKER_THREAD_REPORT")
    if path:
        c = collections.Counter(re.sub(r"\d+", "N", t.name) for t in threading.enumerate())
        with open(path, "w") as f:
            f.write(f"total={sum(c.values())}\n" + "\n".join(f"{n} {k}" for k, n in c.most_common()) + "\n")
