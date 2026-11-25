"""Fermeture déterministe du client HTTP d'une route BYOK : compteur d'emprunts, route retirée (TTL, oubli, nouvelle clé,
arrêt) fermée dès que plus aucun tour ne l'emprunte, jamais pendant un tour en vol."""
from __future__ import annotations

import os
import threading

import httpx
import pytest

from nokker.byok.service import CloudByok
from nokker.core.turn import TurnInput
from nokker.tenancy.context import tenant_scope
from tests.unit.byok.conftest import FakeEndpoint, make_service

EP = "https://api.example.com/v1"
KEY1, KEY2 = "sk-tenant-first-0123456789abcdef", "sk-tenant-second-0123456789abcd"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class PipeTransport(httpx.BaseTransport):
    """Tient un descripteur réel tant qu'il n'est pas fermé : stand-in d'un pool de connexions."""

    def __init__(self, ep: FakeEndpoint, registry: list) -> None:
        self.inner = httpx.MockTransport(ep.handler)
        self.r, self.w = os.pipe()
        self.closed = 0
        registry.append(self)

    def handle_request(self, request):
        return self.inner.handle_request(request)

    def close(self) -> None:
        self.closed += 1
        if self.closed == 1:
            os.close(self.r)
            os.close(self.w)


def world(**kw):
    ep = FakeEndpoint(keys={"api.example.com": KEY1})
    transports: list[PipeTransport] = []
    clock = Clock()
    svc, _ = make_service(set(), ep, clock=clock, **kw)
    svc.entitled = lambda tid: True
    svc.transport_factory = lambda base: PipeTransport(ep, transports)
    return svc, ep, transports, clock


def setup(svc: CloudByok, tid: str, key: str = KEY1) -> None:
    with tenant_scope(tid):
        svc.put({"default": "byok:perso", "byok": [{"name": "perso", "base_url": EP, "model": "m-large", "api_key": key}]})


def live(transports) -> int:  # fermés = descripteurs rendus
    return sum(1 for t in transports if not t.closed)


T = "tn_aaaaaaaaaaaa"




def test_ttl_expiry_during_use_closes_only_at_release():
    svc, _, tr, clock = world()
    setup(svc, T)
    base = len(tr)
    with tenant_scope(T):
        with svc.route_scope() as route:
            held = route.provider
            assert live(tr[base:]) == 1
            clock.t += svc.route_ttl_s + 1
            with svc.route_scope() as route2:  # TTL passé : nouvelle route, l'ancienne est retirée mais empruntée
                assert route2.provider is not held
                assert held._closed is False and live(tr[base:]) == 2
            assert held._closed is False  # le tour en vol continue
        assert held._closed is True  # fermée à la sortie du dernier emprunteur
    assert live(tr[base:]) == 1


def test_forget_without_use_closes_immediately_and_is_idempotent():
    svc, _, tr, _ = world()
    setup(svc, T)
    base = len(tr)
    with tenant_scope(T), svc.route_scope() as route:
        provider = route.provider
    assert provider._closed is False  # route toujours en cache
    svc.forget(T)
    assert provider._closed is True and live(tr[base:]) == 0
    svc.forget(T)
    svc.close()
    assert tr[-1].closed == 1


def test_overwrite_by_new_key_closes_old_after_release_and_keeps_new():
    svc, _, tr, _ = world()
    setup(svc, T)
    base = len(tr)
    with tenant_scope(T):
        with svc.route_scope() as route:
            old = route.provider
            setup(svc, T, KEY2)  # PUT : oublie la route en cours d'usage
            assert old._closed is False
        assert old._closed is True
        with svc.route_scope() as route:
            new = route.provider
            assert new is not old and new.api_key == KEY2 and new._closed is False
    assert live(tr[base:]) == 1


def test_shutdown_closes_free_now_and_inflight_at_release():
    svc, _, tr, _ = world()
    setup(svc, "tn_bbbbbbbbbbbb")
    setup(svc, T)
    with tenant_scope("tn_bbbbbbbbbbbb"), svc.route_scope() as rb:
        pass
    with tenant_scope(T), svc.route_scope() as ra:
        with tenant_scope("tn_bbbbbbbbbbbb"):
            pass
        svc.close()
        assert rb.provider._closed is True and ra.provider._closed is False
    assert ra.provider._closed is True


def test_500_tenants_forgotten_or_expired_keep_descriptors_stable():
    if not os.path.isdir("/proc/self/fd"):
        pytest.skip("Linux seulement")
    svc, _, tr, clock = world()

    def fds() -> int:
        return len(os.listdir("/proc/self/fd"))

    base = fds()
    peak = 0
    for i in range(500):
        tid = f"tn_{i:012d}"
        setup(svc, tid)
        with tenant_scope(tid), svc.route_scope():
            pass
        if i % 2:
            svc.forget(tid)
        else:
            clock.t += svc.route_ttl_s + 1  # expirée : retirée par le balayage du tour suivant
        peak = max(peak, fds())
    svc.close()
    assert peak - base <= 4, (base, peak)  # borné pendant
    assert fds() - base <= 1, (base, fds())  # rendu après
    assert live(tr) == 0


def test_concurrent_access_no_double_close_no_leak():
    svc, _, tr, clock = world()
    tids = [f"tn_{i:012d}" for i in range(8)]
    for t in tids:
        setup(svc, t)
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def work(tid: str) -> None:
        try:
            barrier.wait()
            for i in range(60):
                with tenant_scope(tid), svc.route_scope() as route:
                    assert route.provider._closed is False  # jamais fermé sous un emprunteur
                    if i % 7 == 0:
                        svc.forget(tid)
                    if i % 11 == 0:
                        clock.t += svc.route_ttl_s + 1
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work, args=(t,)) for t in tids]
    [t.start() for t in threads]
    [t.join() for t in threads]
    svc.close()
    assert not errors
    assert all(t.closed == 1 for t in tr if t.closed) and live(tr) == 0


def test_normal_call_unchanged_same_provider_same_reply():
    from tests.unit.byok.test_turn import world as turn_world

    kernel, _, svc, _ = turn_world()
    with tenant_scope("tn_aaaaaaaaaaaa"):
        with svc.route_scope() as r1:
            p1 = r1.provider
        with svc.route_scope() as r2:
            assert r2.provider is p1 and r2.name == "perso"  # même fournisseur tant que le TTL tient
        assert svc.route_for_turn() is r1
        t = kernel.run_turn(TurnInput(thread_id="tn_aaaaaaaaaaaa:th", user_text="salut", channel="web", sender="us_aaaaaaaaaaaa"))
    assert t.reply == "réponse de api.example.com (m-large)"
    assert p1._closed is False  # le tour a rendu son emprunt sans fermer une route encore en cache


def test_inflight_turn_keeps_its_client_when_forgotten_midway():
    from tests.unit.byok.test_turn import world as turn_world

    kernel, _, svc, ep = turn_world()
    seen: list[bool] = []
    held: list = []
    inner = ep.handler

    def handler(request):
        if request.url.path.endswith("/chat/completions"):
            with tenant_scope("tn_aaaaaaaaaaaa"):
                lease = svc._routes["tn_aaaaaaaaaaaa"]
                svc.forget("tn_aaaaaaaaaaaa")  # oubli en plein tour
                seen.append(lease.route.provider._closed)
                held.append(lease)
        return inner(request)

    ep.handler = handler
    with tenant_scope("tn_aaaaaaaaaaaa"):
        t = kernel.run_turn(TurnInput(thread_id="tn_aaaaaaaaaaaa:th", user_text="salut", channel="web", sender="us_aaaaaaaaaaaa"))
    assert t.reply == "réponse de api.example.com (m-large)" and seen == [False]
    assert held[0].closed and held[0].route.provider._closed  # fermé à la fin du tour, pas avant
