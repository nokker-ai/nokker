# Tests d'intégration (vrai Postgres)

Ces tests tournent contre un **Postgres jetable**. Sans `NOKKER_TEST_PG_DSN`, ils sont **ignorés** (skip), donc une
suite verte sans cette variable ne prouve rien côté base.

- Le DSN est celui d'un **superutilisateur**.
- Le montage `pg` (`tests/integration/test_rls.py`) crée sa propre base, pose les rôles de `deploy/postgres/roles.sql`
  (`nokker_owner`, `nokker_app`, ni propriétaire ni BYPASSRLS), passe les migrations, puis détruit la base à la fin.
- Les stores du noyau s'y connectent en `nokker_app`, comme en production, donc sous RLS.

## Lancer

Postgres jetable avec pgvector (les migrations créent l'extension `vector`), sur un réseau docker à part :

```sh
docker network create nk-it
docker run -d --rm --name nk-it-pg --network nk-it -e POSTGRES_PASSWORD=test pgvector/pgvector:pg16
until docker exec nk-it-pg pg_isready -U postgres; do sleep 1; done
```

Depuis la racine du dépôt, dans l'image de test du noyau (Python 3.12 et dépendances de `pyproject.toml`) :

```sh
docker run --rm --network nk-it \
  -e NOKKER_TEST_PG_DSN=postgresql://postgres:test@nk-it-pg:5432/postgres \
  -v "$PWD":/src -w /src <image-de-test-du-noyau> \
  python -m pytest -q -p no:cacheprovider tests/integration
```

Un seul fichier : remplacer `tests/integration` par son chemin, par exemple `tests/integration/test_erase.py`.
Sans docker, sur la machine : `pip install -e '.[dev]'` puis `NOKKER_TEST_PG_DSN=… python -m pytest tests/integration`.

Nettoyage :

```sh
docker rm -f nk-it-pg && docker network rm nk-it
```

## À savoir

- Les migrations sont idempotentes. On peut relancer sur le même conteneur, et chaque exécution a sa base neuve.
- Une erreur `CheckViolation` sur une contrainte `*_kind_check` vient en général d'une migration qui a élargi une
  nature sans mettre à jour les données de test d'un autre fichier, ou l'inverse. Relancer d'abord sur la base du train
  pour savoir si l'échec existait avant le lot.
- Tests du relais `deploy/gateway` : à part, avec `aiohttp`, `pytest-asyncio` et `-o asyncio_mode=auto`.
