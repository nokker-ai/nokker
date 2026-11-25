"""Catalogue des recettes livrées (docs/CONNECTED-SITES.md §7). Lot L0 : le site simulé ; lot L1 : LinkedIn en
LECTURE seule (aucun geste d'écriture n'existe dans ce processus : `send_actions` vide)."""
from __future__ import annotations

from nokker.sites.fakesocial import FAKESOCIAL, LINKEDIN_FR
from nokker.sites.recipes.base import SocialRecipe

FAKESOCIAL_RECIPE = SocialRecipe(site="fakesocial", name="FakeSocial", hosts=("fakesocial.test",), labels=FAKESOCIAL,
                                 list_kinds=("notifications", "messages"), search_kinds=("people", "posts"))

# Libellés d'accessibilité de l'interface LinkedIn en français, relevés à la main (à confirmer par l'essai réel du
# §13.3, sur GO explicite) ; URL publiques et stables de LinkedIn (/notifications/, /messaging/, /in/<slug>/,
# /search/results/people/?keywords=).
LINKEDIN_RECIPE = SocialRecipe(site="linkedin", name="LinkedIn", hosts=("www.linkedin.com", "linkedin.com"),
                               labels=LINKEDIN_FR, list_kinds=("notifications", "messages"),
                               search_kinds=("people", "posts"))

RECIPES: dict[str, SocialRecipe] = {r.site: r for r in (LINKEDIN_RECIPE, FAKESOCIAL_RECIPE)}

__all__ = ["FAKESOCIAL_RECIPE", "LINKEDIN_RECIPE", "RECIPES", "SocialRecipe"]
