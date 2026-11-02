"""Locataire dans la base CortHeXis (docs/MEMORY-TENANCY.md §5) : la mémoire de session (never clear,
`corthexis_index.py`) et la mémoire longue directe rangent leurs notes dans la base du paquet `corthexis`, dont les
tables n'ont ni locataire ni RLS — l'isolation y reposait sur le seul filtre de projet (`s-<hash du fil>`). Ici :

1. **Schéma** (`ensure`, idempotent, rejoué au démarrage avec le DSN de la base CortHeXis) : colonne `tenant_id`
   (`DEFAULT current_setting('app.tenant_id')`, NOT NULL) sur les tables de contenu (`TABLES`, et `chunks` avec
   chacune de ses partitions `chunks_g<N>`), RLS activée ET forcée (le rôle du noyau en est propriétaire : FORCE
   l'y soumet), politique `tenant_isolation` (locataire posé et non vide). Lignes existantes : rattachées au
   locataire de leur fil (`project_tenants` : projet `s-…` → locataire, d'après les fils de la base du noyau),
   les autres projets (`p-…`, `u-local` : mémoire de projet d'une machine Lokkal, mémoire du locataire interne) au
   locataire interne, un projet `s-…` sans fil connu à `ORPHAN` (invisible de tous).
2. **Connexions** (`guard_store`) : le pool du `Store` CortHeXis du processus est enveloppé ; chaque emprunt d'une
   connexion pose `app.tenant_id` = locataire courant (vide hors d'un `tenant_scope` : rien de visible), et le
   remet à vide au retour. Les connexions du pool sont en autocommit (une recherche = une instruction) : un
   `SET LOCAL` ne survivrait pas à l'instruction, d'où un `set_config(…, false)` reposé à CHAQUE emprunt — même
   effet, sans fuite d'un emprunt à l'autre. Une nouvelle génération d'index (`create_generation`, nouvelle
   partition) reçoit sa RLS AVANT d'être remplie.
3. **Propriété** (`corthexis_schema`, docs/MEMORY-TENANCY.md §5.1). `managed` (Lokkal, développement : défaut) : le
   rôle du noyau possède la base, le paquet crée et migre son schéma au premier usage, `ensure` est rejouée au
   démarrage. `external` (bêta) : la base appartient au rôle de MIGRATION ; `nokker migrate` avec
   `CORTHEXIS_OWNER_DATABASE_URL` (`migrate_schema`) applique les migrations du paquet, `ensure`, les fonctions de
   génération et les droits (DML et séquences seulement pour le rôle du noyau) ; le pod ne crée ni ne modifie rien,
   VÉRIFIE (`verify`) locataire, RLS forcée et propriété, et crée une génération d'index par deux fonctions
   `SECURITY DEFINER` du propriétaire, qui posent la RLS de la partition avant de rendre la main.
4. **Fréquences lexicales** (`lex_df`, IDF de la recherche, docs/MEMORY-TENANCY.md §5.2) : par locataire. Globale,
   elle faisait dépendre les poids lexicaux d'un locataire des notes des autres (canal auxiliaire : l'ordre observé
   disait qu'un mot abonde ailleurs). `ensure` y ajoute `tenant_id`, la reconstruit depuis `notes` (un df et un
   nombre de notes par locataire), passe la clé primaire à (tenant_id, token) sous le même nom et la met sous RLS
   forcée ; les lectures du paquet (`WHERE token = …`) ne voient alors que le locataire courant. Écriture : le
   `Store` gardé remplace `_apply_df` (même requête que le paquet, conflit désigné par la contrainte `lex_df_pkey`,
   qui vaut pour les deux formes de la table).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from nokker.tenancy.context import INTERNAL_TENANT, current_tenant_or_none

log = logging.getLogger("nokker.context.corthexis_tenancy")

ORPHAN = "tn_orphan"  # ligne d'un fil inconnu : invisible de tous, effacée avec la base
# Tables de contenu du paquet corthexis (2.1.2) qui portent `project` ; `chunks` à part (par sa note).
TABLES = ("notes", "links", "note_versions", "recall_log", "note_level_floor", "note_access")
EXTRA = ("recall_turns",)  # sans projet : lignes existantes → ORPHAN
STATS = ("lex_df",)  # statistique dérivée de `notes` : reconstruite par locataire, pas rattachée
LEXDF_PK = "lex_df_pkey"  # nom de la clé primaire du paquet, gardé par la reconstruction
_LOCK = 727060
MANAGED, EXTERNAL = "managed", "external"
OWNER_DSN_ENV = "CORTHEXIS_OWNER_DATABASE_URL"
APP_ROLE = "nokker_app"
BACKUP_ROLE = "nokker_backup"  # dump quotidien (BYPASSRLS, lecture seule), créé hors du noyau ; absent : ignoré
ATTACH_FN, BUILD_FN = "nokker_cx_attach_generation", "nokker_cx_build_generation_index"
_POLICY = ("(tenant_id = current_setting('app.tenant_id', true) AND tenant_id <> '')")


def _one(con: Any, sql: str, params: tuple = ()) -> Any:
    row = con.execute(sql, params).fetchone()  # tuple, ou dict (connexions du pool CortHeXis : dict_row)
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def _exists(con: Any, table: str) -> bool:
    return _one(con, "SELECT to_regclass(%s) IS NOT NULL", (table,))


def _has_col(con: Any, table: str) -> bool:
    return _one(con, "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass(%s) "
                     "AND attname = 'tenant_id' AND NOT attisdropped)", (table,))


def _partitions(con: Any) -> list[str]:
    if not _exists(con, "chunks"):
        return []
    rows = con.execute("SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                       "WHERE i.inhparent = 'chunks'::regclass ORDER BY 1").fetchall()
    return [next(iter(r.values())) if isinstance(r, dict) else r[0] for r in rows]


def _protect(con: Any, table: str) -> None:
    con.execute(f'ALTER TABLE "{table}" ALTER COLUMN tenant_id SET DEFAULT current_setting(\'app.tenant_id\')')
    con.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    con.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    if not _one(con, "SELECT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = %s AND policyname = "
                     "'tenant_isolation')", (table,)):
        con.execute(f'CREATE POLICY tenant_isolation ON "{table}" USING {_POLICY} WITH CHECK {_POLICY}')


def ensure(dsn: str, project_tenants: Callable[[], dict[str, str]] | None = None, *,
           backup_role: str = BACKUP_ROLE) -> bool:
    """Pose locataire et RLS forcée sur la base CortHeXis. Rend False (et journalise en ERREUR) si c'est impossible
    (droits, base injoignable) : l'appelant décide (instance à testeurs : index de session coupé)."""
    import psycopg

    try:
        with psycopg.connect(dsn, connect_timeout=10) as con:
            con.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))
            if not _exists(con, "notes"):
                return True  # base neuve : le paquet crée son schéma au premier usage ; rejoué ensuite
            tables = [t for t in (*TABLES, *EXTRA) if _exists(con, t)]
            parts = _partitions(con)
            fresh = [t for t in [*tables, *(["chunks"] if parts or _exists(con, "chunks") else [])]
                     if not _has_col(con, t)]
            if fresh:
                for t in fresh:
                    con.execute(f'ALTER TABLE "{t}" ADD COLUMN tenant_id text')
                    con.execute(f'ALTER TABLE "{t}" NO FORCE ROW LEVEL SECURITY')
                _backfill(con, tables, project_tenants() if project_tenants is not None else {})
                for t in fresh:
                    con.execute(f'ALTER TABLE "{t}" ALTER COLUMN tenant_id SET NOT NULL')
            if _exists(con, "lex_df"):
                if not _has_col(con, "lex_df"):
                    _tenant_lex_df(con)
                _protect(con, "lex_df")
            for t in tables:
                _protect(con, t)
            if _exists(con, "chunks"):
                _protect(con, "chunks")
                for p in parts:
                    _protect(con, p)
            _grant_backup(con, backup_role, strict=False)
            con.commit()
        return True
    except Exception as e:  # noqa: BLE001
        from nokker.memory.corthexis import _no_credentials

        log.error("base CortHeXis : locataire et RLS non posés (%s) — %s", type(e).__name__,
                  _no_credentials(str(e)).splitlines()[0] if str(e) else "")
        return False


def _backfill(con: Any, tables: list[str], mapping: dict[str, str]) -> None:
    con.execute("CREATE TEMP TABLE nk_project_tenant (project text PRIMARY KEY, tenant text NOT NULL) ON COMMIT DROP")
    if mapping:
        with con.cursor() as cur:
            cur.executemany("INSERT INTO nk_project_tenant VALUES (%s, %s) ON CONFLICT DO NOTHING",
                            list(mapping.items()))
    for t in tables:
        if t in EXTRA:
            con.execute(f'UPDATE "{t}" SET tenant_id = %s WHERE tenant_id IS NULL', (ORPHAN,))
            continue
        con.execute(f'UPDATE "{t}" x SET tenant_id = m.tenant FROM nk_project_tenant m '
                    f'WHERE x.tenant_id IS NULL AND x.project = m.project')
        con.execute(f'UPDATE "{t}" SET tenant_id = CASE WHEN coalesce(project, \'\') LIKE \'s-%%\' THEN %s ELSE %s '
                    f'END WHERE tenant_id IS NULL', (ORPHAN, INTERNAL_TENANT))
    if _exists(con, "chunks"):
        con.execute("UPDATE chunks c SET tenant_id = n.tenant_id FROM notes n WHERE c.note_id = n.id "
                    "AND c.tenant_id IS NULL")
        con.execute("UPDATE chunks SET tenant_id = %s WHERE tenant_id IS NULL", (ORPHAN,))


def _tenant_lex_df(con: Any) -> None:
    """`lex_df` globale → un df par (locataire, mot), et un nombre de notes par locataire (mot vide), recalculés depuis
    `notes` (`lex_tokens` : l'ensemble des mots de la note, ce que le paquet compte). Dans la transaction d'`ensure`,
    après le rattachement des notes ; `notes` est lue sans FORCE le temps de la passe (rétablie par `_protect`)."""
    con.execute('ALTER TABLE lex_df ADD COLUMN tenant_id text')
    con.execute('ALTER TABLE notes NO FORCE ROW LEVEL SECURITY')
    con.execute('ALTER TABLE lex_df NO FORCE ROW LEVEL SECURITY')
    pk = con.execute("SELECT conname FROM pg_constraint WHERE conrelid = 'lex_df'::regclass AND contype = 'p'"
                     ).fetchone()
    if pk is not None:
        name = next(iter(pk.values())) if isinstance(pk, dict) else pk[0]
        con.execute(f'ALTER TABLE lex_df DROP CONSTRAINT "{name}"')
    con.execute('DELETE FROM lex_df')
    con.execute("INSERT INTO lex_df(tenant_id, token, df) "
                "SELECT n.tenant_id, t, count(*) FROM notes n, unnest(n.lex_tokens) AS t GROUP BY 1, 2 "
                "UNION ALL SELECT tenant_id, '', count(*) FROM notes GROUP BY 1")
    con.execute('ALTER TABLE lex_df ALTER COLUMN tenant_id SET NOT NULL')
    con.execute(f'ALTER TABLE lex_df ADD CONSTRAINT {LEXDF_PK} PRIMARY KEY (tenant_id, token)')


def _apply_df(con: Any, delta: dict[str, int]) -> None:
    """`Store._apply_df` du paquet (corthexis 2.1.2), conflit désigné par la contrainte : `ON CONFLICT (token)` n'a plus
    d'index unique quand la clé est (tenant_id, token). Le locataire de la ligne : défaut de la colonne (connexion
    gardée) ; la RLS borne la suppression aux lignes du locataire courant."""
    delta = {t: d for t, d in delta.items() if d}
    if not delta:
        return
    toks = sorted(delta)  # ordre fixe des verrous, comme le paquet
    con.execute(
        "INSERT INTO lex_df(token, df) SELECT t, d FROM unnest(%s::text[], %s::int[]) AS x(t, d) ORDER BY t "
        f"ON CONFLICT ON CONSTRAINT {LEXDF_PK} DO UPDATE SET df = lex_df.df + excluded.df",
        (toks, [delta[t] for t in toks]))
    neg = [t for t in toks if delta[t] < 0]
    if neg:
        con.execute("DELETE FROM lex_df WHERE token = ANY(%s) AND df <= 0", (neg,))


def _role_exists(con: Any, role: str) -> bool:
    return bool(_one(con, "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)", (role,)))


def _grant_backup(con: Any, role: str, *, strict: bool) -> bool:
    """Lecture de toute la base pour le rôle de sauvegarde (CONNECT, USAGE, SELECT sur tables et séquences, droits par
    défaut des objets futurs du rôle courant). Rôle absent (Lokkal, développement) : rien. Idempotent. Hors `strict`
    (`ensure`, mode managed) : un droit refusé se journalise, sans casser la passe ; le droit sur la BASE (affaire de
    l'administrateur : propriétaire de la base) ne fait jamais échouer."""
    from psycopg import errors
    from psycopg import sql as q

    if not role or not _role_exists(con, role):
        return False
    r = q.Identifier(role)
    db = q.Identifier(_one(con, "SELECT current_database()"))
    me = q.Identifier(_one(con, "SELECT current_user"))
    steps = [(q.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(db, r), False),
             (q.SQL("GRANT USAGE ON SCHEMA public TO {}").format(r), strict),
             (q.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(r), strict),
             (q.SQL("GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(r), strict),
             (q.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT ON TABLES TO {}").format(me, r),
              strict),
             (q.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT ON SEQUENCES TO {}")
              .format(me, r), strict)]
    for stmt, must in steps:
        try:
            with con.transaction():  # point de sauvegarde : un refus n'annule pas le reste de la passe
                con.execute(stmt)
        except errors.InsufficientPrivilege as e:
            if must:
                raise
            log.warning("base CortHeXis : droit de sauvegarde non accordé (%s)", str(e).splitlines()[0])
    return True


# Génération d'index en mode `external` : le pod (DML seulement) insère la ligne de `index_generations`, le PROPRIÉTAIRE
# crée sa partition (CREATE TABLE … PARTITION OF chunks : il faut posséder `chunks`) et la met sous RLS forcée AVANT de
# rendre la main ; l'index HNSW et ANALYZE aussi. Seules entrées : un id de génération existante (entier), et ses
# paramètres lus dans la table (entiers bornés par CHECK et par pgvector). search_path figé.
_POLICY_SQL = _POLICY.replace("'", "''")
_FUNCTIONS = f"""
CREATE OR REPLACE FUNCTION {ATTACH_FN}(gen integer) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $nk$
DECLARE
    d integer;
    t text := 'chunks_g' || gen;
BEGIN
    SELECT dim INTO d FROM index_generations WHERE id = gen;
    IF d IS NULL THEN
        RAISE EXCEPTION 'génération d''index inconnue : %', gen;
    END IF;
    IF to_regclass(t) IS NULL THEN
        EXECUTE format('CREATE TABLE %I PARTITION OF chunks FOR VALUES IN (%s)', t, gen);
        EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I CHECK (vector_dims(embedding) = %s)', t, t || '_dim', d);
    END IF;
    EXECUTE format('ALTER TABLE %I ALTER COLUMN tenant_id SET DEFAULT current_setting(''app.tenant_id'')', t);
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = t AND policyname = 'tenant_isolation') THEN
        EXECUTE format('CREATE POLICY tenant_isolation ON %I USING {_POLICY_SQL} WITH CHECK {_POLICY_SQL}', t);
    END IF;
END
$nk$;
CREATE OR REPLACE FUNCTION {BUILD_FN}(gen integer) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $nk$
DECLARE
    g record;
    t text := 'chunks_g' || gen;
BEGIN
    SELECT dim, hnsw_m, hnsw_ef_construction INTO g FROM index_generations WHERE id = gen;
    IF NOT FOUND OR to_regclass(t) IS NULL THEN
        RAISE EXCEPTION 'génération d''index inconnue : %', gen;
    END IF;
    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I USING hnsw ((embedding::halfvec(%s)) halfvec_ip_ops) '
                   'WITH (m = %s, ef_construction = %s)', t || '_hnsw', t, g.dim, g.hnsw_m, g.hnsw_ef_construction);
    UPDATE index_generations SET index_built = true WHERE id = gen;
    EXECUTE format('ANALYZE %I', t);
END
$nk$;
"""


def _grant_app(con: Any, role: str) -> None:
    """Rôle du noyau : DML et séquences, rien d'autre (ni TRUNCATE, ni REFERENCES, ni TRIGGER, ni CREATE sur le
    schéma) ; exécution des deux fonctions de génération. Idempotent ; les droits par défaut couvrent les tables futures
    du propriétaire (partitions de génération comprises)."""
    from psycopg import sql as q

    r = q.Identifier(role)
    for stmt in ("GRANT USAGE ON SCHEMA public TO {r}",
                 "REVOKE CREATE ON SCHEMA public FROM {r}",
                 "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {r}",
                 "REVOKE TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public FROM {r}",
                 "GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {r}",
                 "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {r}",
                 "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE ON SEQUENCES TO {r}",
                 f"REVOKE ALL ON FUNCTION {ATTACH_FN}(integer), {BUILD_FN}(integer) FROM PUBLIC",
                 f"GRANT EXECUTE ON FUNCTION {ATTACH_FN}(integer), {BUILD_FN}(integer) TO {{r}}"):
        con.execute(q.SQL(stmt).format(r=r))


def migrate_schema(owner_dsn: str, *, app_role: str = APP_ROLE, backup_role: str = BACKUP_ROLE,
                   project_tenants: Callable[[], dict[str, str]] | None = None) -> dict[str, Any]:
    """`nokker migrate` (mode `external`), avec le DSN du rôle PROPRIÉTAIRE : migrations du paquet `corthexis`,
    locataire et RLS forcée (`ensure`), fonctions de génération, droits du rôle du noyau et de la sauvegarde. Idempotent.
    Lève à la première erreur (le job d'init échoue franchement)."""
    import psycopg
    from corthexis.store import Store

    class _Migrator(Store):  # `Store.migrate` seul : ni pool, ni connexion gardée
        def __init__(self, dsn: str) -> None:  # pas d'appel au parent : il ouvrirait un pool
            self.dsn = dsn

    applied = _Migrator(owner_dsn).migrate()
    if not ensure(owner_dsn, project_tenants, backup_role=backup_role):
        raise RuntimeError("base CortHeXis : locataire et RLS non posés (voir le journal)")
    with psycopg.connect(owner_dsn, connect_timeout=10) as con:
        con.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))
        if not _role_exists(con, app_role):
            raise RuntimeError(f"rôle du noyau {app_role!r} absent de la base CortHeXis")
        con.execute(_FUNCTIONS)
        _grant_app(con, app_role)
        backup = _grant_backup(con, backup_role, strict=True)
        con.commit()
    return {"applied": applied, "app_role": app_role, "backup": backup}


def verify(dsn: str) -> list[str]:
    """Mode `external`, avec le DSN du POD : ce qui manque pour servir la base (liste vide : tout est en place).
    Lecture seule (pg_class, pg_attribute, pg_policies, droits). Exige : tables du paquet présentes, chacune (et chaque
    partition de `chunks`) avec `tenant_id`, RLS activée ET forcée, politique `tenant_isolation`, et NON possédée par le
    rôle du pod ni par un rôle dont il est membre ; schéma ni possédé ni modifiable par lui ; fonctions de génération
    exécutables ; rôle du pod ni superutilisateur ni BYPASSRLS."""
    import psycopg

    problems: list[str] = []
    with psycopg.connect(dsn, connect_timeout=10) as con:
        me = _one(con, "SELECT current_user")
        if _one(con, "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"):
            problems.append(f"rôle du pod {me} superutilisateur ou BYPASSRLS")
        if not _exists(con, "notes"):
            return [*problems, "schéma CortHeXis absent (job `nokker migrate` avec " + OWNER_DSN_ENV + ")"]
        for t in (*TABLES, *EXTRA, *STATS, "chunks", *_partitions(con)):
            row = con.execute(
                "SELECT c.relrowsecurity, c.relforcerowsecurity, pg_get_userbyid(c.relowner), "
                "EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'tenant_id' "
                "AND NOT a.attisdropped), "
                "EXISTS (SELECT 1 FROM pg_policies p WHERE p.tablename = c.relname AND p.policyname = 'tenant_isolation'), "
                "pg_has_role(current_user, c.relowner, 'MEMBER') "
                "FROM pg_class c WHERE c.oid = to_regclass(%s)", (t,)).fetchone()
            if row is None:
                problems.append(f"{t} : table absente")
                continue
            rls, force, owner, col, policy, member = row
            if not col:
                problems.append(f"{t} : sans tenant_id")
            if not (rls and force):
                problems.append(f"{t} : RLS non forcée")
            if not policy:
                problems.append(f"{t} : sans politique tenant_isolation")
            if t in STATS and col and _one(con, "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = "
                                               "to_regclass(%s) AND contype = 'p'", (t,)) != \
                    "PRIMARY KEY (tenant_id, token)":
                problems.append(f"{t} : clé primaire sans tenant_id")
            if owner == me or member:  # membre du propriétaire : mêmes pouvoirs (SET ROLE, droits hérités)
                problems.append(f"{t} : appartient au rôle du pod" if owner == me
                                else f"{t} : le rôle du pod est membre de son propriétaire {owner}")
        sch_owned, sch_create = con.execute(
            "SELECT pg_has_role(current_user, nspowner, 'MEMBER'), has_schema_privilege('public', 'CREATE') "
            "FROM pg_namespace WHERE nspname = 'public'").fetchone()
        if sch_owned or sch_create:
            problems.append("schéma public : le rôle du pod peut y créer ou en supprimer")
        for fn in (ATTACH_FN, BUILD_FN):
            ok = _one(con, "SELECT to_regprocedure(%s) IS NOT NULL AND has_function_privilege(%s, 'EXECUTE')",
                      (f"{fn}(integer)", f"{fn}(integer)"))
            if not ok:
                problems.append(f"fonction {fn} absente ou non exécutable")
    return problems


def project_tenants_from(nokker_dsn: str) -> Callable[[], dict[str, str]]:
    """Projet `s-…` de chaque fil connu de la base du noyau → son locataire (lu locataire par locataire, sous RLS)."""

    def mapping() -> dict[str, str]:
        from nokker.context.corthexis_index import project_of
        from nokker.store.conn import connect, known_tenants
        from nokker.tenancy.context import tenant_scope

        out: dict[str, str] = {}
        for tid in known_tenants(nokker_dsn):
            with tenant_scope(tid), connect(nokker_dsn) as c:
                ids: set[str] = set()
                for table in ("threads", "nokker_episodes"):
                    if c.execute("SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone()[0]:
                        ids |= {r[0] for r in c.execute(f"SELECT DISTINCT thread_id FROM {table}").fetchall()}
            for th in ids:
                out.setdefault(project_of(th), tid)
        return out

    return mapping


class _TenantPool:
    """Enveloppe du `ConnectionPool` du Store CortHeXis : `app.tenant_id` posé à chaque emprunt, remis à vide au
    retour. Tout le reste délègue."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @contextmanager
    def connection(self, *args: Any, **kwargs: Any):
        with self._pool.connection(*args, **kwargs) as con:
            con.execute("SELECT set_config('app.tenant_id', %s, false)", (current_tenant_or_none() or "",))
            try:
                yield con
            finally:
                try:
                    con.execute("SELECT set_config('app.tenant_id', '', false)")
                except Exception:
                    log.debug("remise à zéro du locataire impossible", exc_info=True)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)


_strict = False  # instance à testeurs (`require_rls`) : pas de base CortHeXis sans RLS
_mode = MANAGED


def set_strict(strict: bool) -> None:
    global _strict
    _strict = bool(strict)


def set_mode(mode: str | None) -> str:
    """`corthexis_schema` : `managed` (défaut) | `external`. Une autre valeur lève (`ValueError`) : une faute de frappe
    ne doit pas rendre au pod le droit de migrer."""
    global _mode
    m = str(mode or MANAGED).strip().lower()
    if m not in (MANAGED, EXTERNAL):
        raise ValueError(f"corthexis_schema inconnu : {mode!r} (managed | external)")
    _mode = m
    return m


def schema_mode() -> str:
    return _mode


def guard_store(store: Any) -> Any:
    """Enveloppe (une fois) le pool d'un `corthexis.store.Store` ; pose locataire et RLS sur son schéma (le paquet
    vient de le créer ou de le migrer) ; une génération créée reçoit sa RLS aussitôt, avant d'être remplie. Strict :
    une base dont la RLS ne peut être posée n'est pas servie (exception : rappel vide, indexation en échec)."""
    if getattr(store, "_nokker_tenant_guard", False):
        return store
    if not getattr(store, "dsn", None) or getattr(store, "pool", None) is None:
        return store  # pas un Store Postgres du paquet (double de test) : rien à garder
    if _mode == EXTERNAL:
        problems = verify(store.dsn)
        if problems:
            msg = "base CortHeXis non conforme (corthexis_schema: external) : " + " ; ".join(problems[:5])
            if _strict:
                raise RuntimeError(msg + " — non servie (require_rls)")
            log.warning(msg)
        store.pool = _TenantPool(store.pool)
        store._apply_df = _apply_df
        _external_generations(store)
        store._nokker_tenant_guard = True
        return store
    if not ensure(store.dsn) and _strict:
        raise RuntimeError("base CortHeXis sans locataire ni RLS : non servie (require_rls)")
    store.pool = _TenantPool(store.pool)
    store._apply_df = _apply_df
    create = store.create_generation

    def create_generation(*args: Any, **kwargs: Any) -> Any:
        g = create(*args, **kwargs)
        with store.pool._pool.connection() as con:
            if _has_col(con, "chunks"):
                with con.transaction():
                    _protect(con, g.table)
        return g

    store.create_generation = create_generation
    store._nokker_tenant_guard = True
    return store


def _external_generations(store: Any) -> None:
    """Mode `external` : `create_generation` et `build_index` du Store passent par les fonctions du propriétaire (le pod
    n'a que le DML) ; même contrat que ceux du paquet (corthexis 2.1.2)."""
    from corthexis.store import _GEN_COLS, StoreError, _gen

    def create_generation(embed_identity: str, dim: int, *, hnsw_m: int = 16, hnsw_ef_construction: int = 64,
                          build_index: bool = False, lexical_weight: float | None = None) -> Any:
        if not 1 <= int(dim) <= 4000:
            raise StoreError(f"dimension {dim} out of range (1..4000 for halfvec HNSW)")
        with store.pool.connection() as con, con.transaction():
            g = _gen(con.execute(
                "INSERT INTO index_generations(embed_identity, dim, hnsw_m, hnsw_ef_construction, lexical_weight) "
                f"VALUES (%s, %s, %s, %s, %s) RETURNING {_GEN_COLS}",
                (embed_identity, int(dim), int(hnsw_m), int(hnsw_ef_construction), lexical_weight)).fetchone())
            con.execute(f"SELECT {ATTACH_FN}(%s)", (g.id,))
        if build_index:
            store.build_index(g.id)
            g = store.get_generation(g.id)
        return g

    def build_index(generation: Any, *, maintenance_work_mem: str | None = None) -> None:
        g = store._require_gen(generation)
        if g.index_built:
            return
        with store.pool.connection() as con, con.transaction():
            if maintenance_work_mem:
                con.execute("SELECT set_config('maintenance_work_mem', %s, true)", (maintenance_work_mem,))
            con.execute(f"SELECT {BUILD_FN}(%s)", (g.id,))

    store.create_generation = create_generation
    store.build_index = build_index


def _seed_external_store(cx_service: Any) -> None:
    """Mode `external` : le Store du processus est ouvert SANS migration (`migrate=False`) ; le paquet ne tente aucun
    CREATE ni ALTER au premier usage (le job `nokker migrate` s'en est chargé)."""
    if cx_service._store is not None:
        return
    with cx_service._lock:
        if cx_service._store is None:
            from corthexis.config import env
            from corthexis.store import SearchConfig, Store

            cx_service._store = Store(config=SearchConfig.from_env(), max_size=int(env("DB_POOL", "4") or 4),
                                      migrate=False)


def guarded_store() -> Any:
    """Le Store CortHeXis du processus, gardé (à appeler avant tout usage : `service.reset()` en crée un neuf)."""
    from corthexis import service as cx_service

    if _mode == EXTERNAL:
        _seed_external_store(cx_service)
    return guard_store(cx_service.get_store())


__all__ = ["APP_ROLE", "BACKUP_ROLE", "EXTERNAL", "EXTRA", "MANAGED", "ORPHAN", "OWNER_DSN_ENV", "STATS", "TABLES",
           "ensure", "guard_store", "guarded_store", "migrate_schema", "project_tenants_from", "schema_mode",
           "set_mode", "set_strict", "verify"]
