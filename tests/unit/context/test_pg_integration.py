"""Never clear sur un VRAI Postgres (`NOKKER_TEST_DSN`, sinon ignoré) : tours en `PgStore`, épisodes et réservoir en
`PgEpisodeStore`/`PgReservoirStore` — une session de plusieurs jours, puis une REPRISE par un nouveau gestionnaire
(redémarrage du processus) qui retrouve exactement la même vue."""
from __future__ import annotations

import os
import uuid

import pytest

from nokker.context.manager import ContextManager
from nokker.store.pg import PgStore, apply_migrations

from .harness import make_session
from .test_never_clear import FILLER

DSN = os.environ.get("NOKKER_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="NOKKER_TEST_DSN absent (Postgres réel)")


@pytest.fixture(autouse=True)
def _tenant():
    """Les stores Postgres lisent et écrivent dans le locataire courant (RLS, docs/TENANCY.md §4)."""
    from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

    with tenant_scope(INTERNAL_TENANT):
        yield


def test_session_persists_and_resumes_after_restart(tmp_path):
    from nokker.context.pg import PgEpisodeStore, PgReservoirStore

    apply_migrations(DSN)
    thread = "th_" + uuid.uuid4().hex[:8]
    store = PgStore(DSN)
    s = make_session(tmp_path=tmp_path, store=store, thread=thread, episodes=PgEpisodeStore(DSN),
                     reservoir=PgReservoirStore(DSN))
    s.say("Le numéro du dossier de l'atelier est 4711.")
    for i in range(70):
        if i % 12 == 0:
            s.say(f"gros résultat {i} [[BIG]]")
        s.say(f"sujet {i} : {FILLER}")
    eps = s.manager.episodes.list(thread, closed_only=True)
    assert len(eps) >= 3
    assert store.history_from(thread, 5)[0].index == 5 and len(store.history_from(thread, 0)) == len(store.history(thread))
    window_before = [t.index for t in s.manager.history_window(thread)]
    block_before = s.manager.memory_block(thread)

    # « redémarrage » : nouveau gestionnaire, mêmes bases
    mgr2 = ContextManager(store=PgStore(DSN), episodes=PgEpisodeStore(DSN), reservoir=PgReservoirStore(DSN),
                          budgets=s.manager.budgets, clock=s.clock)
    assert [t.index for t in mgr2.history_window(thread)] == window_before
    assert mgr2.memory_block(thread) == block_before
    assert mgr2.state(thread).shed == s.manager.state(thread).shed
    assert "4711" in "\n".join(mgr2.search_turns(thread, "numéro du dossier de l'atelier"))
    item = next(iter(mgr2.reservoir.list(thread, kind="tool_result")), None)
    assert item is not None and mgr2.recall(thread, ref=item.id, max_chars=300).startswith("[tool_result rs_")
