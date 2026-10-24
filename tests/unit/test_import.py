"""Test trivial F0 — le paquet `nokker` et tous ses sous-paquets squelettes s'importent.

Pas encore de logique à tester (core/context/qa/route/providers/tools/store/ledger/api/bridges
sont des `__init__.py` vides, cf. docs/PLAN-P1.md §2.2) : ce test sert de ratchet minimal pour
la CI (F0) et sera remplacé/étendu par les tests réels de chaque chantier (F2+).
"""

import importlib

SUBPACKAGES = [
    "nokker",
    "nokker.core",
    "nokker.context",
    "nokker.memory",
    "nokker.qa",
    "nokker.route",
    "nokker.providers",
    "nokker.tools",
    "nokker.store",
    "nokker.ledger",
    "nokker.api",
    "nokker.bridges",
]


def test_all_subpackages_import():
    for name in SUBPACKAGES:
        importlib.import_module(name)
