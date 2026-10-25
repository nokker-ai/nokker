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
