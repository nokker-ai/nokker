"""Ticket T6 (docs/mem-dedup.md) : dédoublonnage des notes à l'écriture et catégorie « preference ».

Constat rc.9.28 (lokkal-test1) : « Préférence : réunions le matin » listée 3 fois par `/memory`, étiquetée « retour »."""
from __future__ import annotations

from types import SimpleNamespace

from nokker.context import project_memory as PM
from nokker.context.project_memory import FORGOTTEN, KINDS, USER_PROJECT, ProjectMemoryTools, file_name
from nokker.learning.pending import PendingMemory
from nokker.learning.store import MemoryLearningStore
from nokker.memory import long_note as LN
from nokker.memory.consolidation.model import KIND_LABEL
from nokker.memory.dedup import equivalent, normalize, tokens
from nokker.memory.tenant_long import KINDS as LONG_KINDS
from tests.unit.context.test_project_memory import ctx, make_memory, make_repo

# ─────────────────────────────── équivalence (pure) ───────────────────────────────


def test_normalize_ignores_case_accents_punctuation_and_spaces():
    assert normalize("  Préfère   les RÉUNIONS, le matin ! ") == "prefere les reunions le matin"


def test_exact_and_lightly_reworded_duplicates_are_equivalent():
    base = "L'utilisateur préfère les réunions le matin."
    assert equivalent(base, "l'utilisateur  préfère les réunions le matin")
    assert equivalent(base, "Préfère les réunions le matin")  # mots-outils et sujet générique
    assert equivalent(base, "Alice préfère avoir ses réunions le matin")  # un seul mot de plus (le sujet)
    assert equivalent(base, "Préfère le matin pour les réunions")  # ordre
    assert equivalent("Préfère la réunion du matin", "Préfère les réunions du matin")  # pluriel


def test_a_changed_word_is_never_a_duplicate_even_in_a_long_note():
    assert not equivalent("Préfère être contacté par SMS", "Préfère être contacté par e-mail")
    assert not equivalent("Préfère les réunions le matin", "Préfère les réunions l'après-midi")
    assert not equivalent("Appeler avant 10 h", "Appeler avant 11 h")
    long_a = ("Pour les revues de code du projet, toujours lancer make check puis relire le diff complet avant de "
              "proposer le commit, et citer les fichiers touchés dans le message")
    assert not equivalent(long_a, long_a.replace("complet", "partiel"))


def test_negation_and_numbers_block_the_one_word_tolerance():
    assert not equivalent("Appeler le client le lundi", "Ne pas appeler le client le lundi")
    assert not equivalent("Réunions le matin au bureau", "Réunions le matin au bureau 2")
    assert {"ne", "pas"} <= tokens("ne pas appeler")  # négations gardées


def test_a_genuinely_new_note_is_not_equivalent():
    assert not equivalent("Préfère les réunions le matin", "Répond en français, en tutoyant")


# ─────────────────────────────── Lokkal : memory_note ───────────────────────────────


def _setup(tmp_path, **kw):
    repo = make_repo(tmp_path)
    pm, _engine = make_memory(tmp_path, **kw)
    pm.open_session("th_1", repo, tmp_path)
    return pm, ProjectMemoryTools(pm)


def _note(tools, turn=1, **kw):
    args = {"kind": "preference", "title": "Préférence : réunions le matin",
            "text": "L'utilisateur préfère les réunions le matin.", "derived_from": "user"}
    args.update(kw)
    return tools.call("memory_note", args, ctx(turn=turn))


def _listed(pm):
    return pm.memory({"action": "list"}, ctx())


def test_repeating_a_preference_three_times_keeps_one_note(tmp_path):
    pm, tools = _setup(tmp_path)
    first = _note(tools)
    assert not first.is_error and first.content.startswith("noté : preference-")
    name = first.content.split()[2]
    again = [_note(tools, turn=2, text="Préfère les réunions le matin"),  # reformulé léger
             _note(tools, turn=3, text="l'utilisateur préfère les réunions le matin"),  # exact à la casse près
             _note(tools, turn=4, kind="feedback", text="Alice préfère avoir ses réunions le matin.")]  # autre type
    assert [r.content for r in again] == [f"déjà noté : {name}"] * 3 and not any(r.is_error for r in again)
    notes = [n for n in pm.learned(pm._session("th_1").project)]
    assert [n.name for n in notes] == [name]
    assert [e[0] for e in pm.events] == ["memory.noted"]


def test_a_user_scope_note_covers_the_same_note_at_project_scope(tmp_path):
    pm, tools = _setup(tmp_path)
    said = "retiens pour tous mes projets que je préfère les réunions le matin"
    pm.core_block("th_1", 1, said)
    r = _note(tools, scope="user", quote="je préfère les réunions le matin")
    assert not r.is_error, r.content
    dup = _note(tools, turn=1)  # portée project : la note user vaut déjà partout
    assert dup.content == f"déjà noté : {r.content.split()[2]}"


def test_a_genuinely_new_note_is_written(tmp_path):
    pm, tools = _setup(tmp_path)
    _note(tools)
    r = _note(tools, turn=2, title="Langue", text="Répondre en français, en tutoyant")
    assert not r.is_error and r.content.startswith("noté : preference-langue-")
    assert len(pm.learned(pm._session("th_1").project)) == 2


def test_a_changed_preference_replaces_the_old_one_same_subject(tmp_path):
    pm, tools = _setup(tmp_path)
    project = pm._session("th_1").project
    old = _note(tools, title="Canal de contact", text="Préfère être contacté par SMS").content.split()[2]
    r = _note(tools, turn=2, title="Canal de contact préféré", text="Préfère être contacté par e-mail")
    new = r.content.split()[2]
    assert r.content.startswith(f"noté : {new} (preference, portée project) — remplace {old} ;")
    assert [n.name for n in pm.learned(project)] == [new]
    assert (tmp_path / "pm" / project / FORGOTTEN / file_name(old)).exists()  # rangée, pas détruite
    assert ("memory.forgotten", {"thread_id": "th_1", "name": old, "kind": "preference", "scope": "project",
                                 "replaced_by": new}) in pm.events
    assert f'replaces: "{old}"' in (tmp_path / "pm" / project / file_name(new)).read_text()


def test_an_explicit_replaces_retires_the_named_note(tmp_path):
    pm, tools = _setup(tmp_path)
    old = _note(tools).content.split()[2]
    r = _note(tools, turn=2, title="Préférence : réunions l'après-midi", text="Préfère les réunions l'après-midi",
              replaces=old)
    assert "— remplace " + old in r.content
    assert [n.text for n in pm.learned(pm._session("th_1").project)] == ["Préfère les réunions l'après-midi"]
    bad = _note(tools, turn=3, title="x", text="autre chose de neuf", replaces="preference-inconnue-000000")
    assert bad.is_error and bad.content.startswith("replaces : note inconnue")


def test_replaces_cannot_retire_a_rule_or_be_used_by_a_non_preference(tmp_path):
    """la relecture (T6) : `replaces` seulement preference → preference. Une injection qui se fait passer pour un changement
    d'avis ne range pas une règle."""
    pm, tools = _setup(tmp_path)
    project = pm._session("th_1").project
    rule = _note(tools, kind="rule", title="Validation", text="Toujours demander avant de supprimer un fichier")
    rule = rule.content.split()[2]
    pref = _note(tools, turn=2).content.split()[2]
    attempts = [
        _note(tools, turn=3, title="Validation", text="Supprimer sans demander", replaces=rule),  # preference → rule
        _note(tools, turn=3, kind="rule", title="Validation", text="Supprimer sans demander", replaces=rule),
        _note(tools, turn=3, kind="fact", title="x", text="Les réunions ont lieu le soir", replaces=pref),
    ]
    assert all(r.is_error and r.content.startswith(PM.ERR_REPLACES_KIND) for r in attempts), [r.content for r in attempts]
    assert sorted(n.name for n in pm.learned(project)) == sorted([rule, pref])  # rien n'est rangé
    assert not (tmp_path / "pm" / project / FORGOTTEN).exists()


def test_rules_and_facts_with_the_same_title_are_not_replaced(tmp_path):
    pm, tools = _setup(tmp_path)
    _note(tools, kind="rule", title="Style", text="Pas d'emoji dans les réponses")
    r = _note(tools, turn=2, kind="rule", title="Style", text="Réponses en trois phrases au plus")
    assert "remplace" not in r.content and len(pm.learned(pm._session("th_1").project)) == 2


# ─────────────────────────────── catégorie ───────────────────────────────


def test_preference_is_a_kind_of_its_own_listed_and_in_the_core(tmp_path):
    assert "preference" in KINDS and KINDS == LONG_KINDS
    assert KIND_LABEL["preference"] == "préférence"
    pm, tools = _setup(tmp_path)
    name = _note(tools).content.split()[2]
    listed = _listed(pm)
    assert '"kind": "preference"' in listed.content and name in listed.content
    assert "(preference, projet) Préférence : réunions le matin" in pm.core_block("th_1", 5, "suite")
    spec = next(s for s in tools.specs() if s.name == "memory_note")
    assert "preference" in spec.parameters["properties"]["kind"]["enum"]
    assert "preference : préférence de l'utilisateur" in spec.parameters["properties"]["kind"]["description"]


# ─────────────────────────────── quarantaine ───────────────────────────────


def _pending_setup(tmp_path):
    store = MemoryLearningStore()
    pending = PendingMemory(store=store, account_of_thread=lambda tid: "acc_1")
    pm, tools = _setup(tmp_path, pending=pending)
    pending.commit_note = pm.commit_pending
    return pm, tools, pending, store


def test_quarantine_holds_one_note_for_equivalent_proposals(tmp_path):
    _pm, tools, _pending, store = _pending_setup(tmp_path)
    a, b = _note(tools), _note(tools, turn=2, text="Préfère les réunions le matin")
    assert a.content == b.content and len(store.list_pending("acc_1")) == 1


def test_approving_an_equivalent_of_an_active_note_writes_nothing(tmp_path):
    pm, tools, pending, store = _pending_setup(tmp_path)
    project = pm._session("th_1").project
    _note(tools)
    (first,) = store.list_pending("acc_1")
    written = pending.decide("acc_1", first.id, True, "acc_1").result
    # Deuxième proposition équivalente, rangée AVANT l'approbation de la première (deux onglets, deux tours) :
    second = SimpleNamespace(**{**first.__dict__, "id": "mp_second", "text": "Préfère les réunions le matin"})
    assert pm.commit_pending(second) == written
    assert [n.name for n in pm.learned(project)] == [written]


# ─────────────────────────────── nokker.ai : memory_note de la mémoire longue ───────────────────────────────


def test_long_note_answers_already_noted_for_an_active_equivalent():
    from tests.unit.memory.test_long_note import Env, args

    env = Env()
    first = env.call(args())
    nid = first.content.split(" : ")[1].split(" ")[0]
    env.decide(nid, True)
    again = env.call(args(text="préfère des réponses courtes sans emoji"))
    assert not again.is_error and again.content.startswith("déjà noté : feedback-")
    assert env.held() == []  # la première est approuvée, rien de neuf en attente
    assert LN.ALREADY in LN.ALL_MODEL_TEXTS and USER_PROJECT == "u-local"


# ─────────────────────────────── gel des textes vus par le modèle (relecture, T6) ───────────────────────────────

# Empreinte de `project_memory.texts_sha256()` (description et paramètres de `memory_note` de Lokkal, « déjà noté »,
# « remplace », erreurs de `replaces`) et du `KIND_LABEL` de la consolidation. Ne se changent qu'avec une relecture.
FROZEN_LOKKAL = "9fb67c959f2f1bae8a2108a8b14d01ac8e05b21ff266c2b7c0915f2af051d7af"  # T6 + B5 + rappel sans écriture (rc.9.31) ; rc.9.32 : portée rétrogradée, « dis-le à la personne », gel de la relecture
FROZEN_KIND_LABEL = "8cb0c2f9f38fd2ff3a2dd0948db08f31e9c162b3cdf3c22ada6148c5994937bf"


def test_lokkal_memory_note_texts_are_frozen():
    assert PM.texts_sha256() == FROZEN_LOKKAL
    spec = next(s for s in ProjectMemoryTools(PM.ProjectMemory(root="/nonexistent")).specs() if s.name == "memory_note")
    assert spec.description == PM.MEMORY_NOTE_DESCRIPTION and spec.parameters == PM.MEMORY_NOTE_PARAMETERS


def test_consolidation_kind_labels_are_frozen():
    import hashlib
    import json

    assert hashlib.sha256(json.dumps(KIND_LABEL, ensure_ascii=False, sort_keys=True).encode()).hexdigest() \
        == FROZEN_KIND_LABEL


# ─────────────────────────────── B5 : un refus ne se confirme pas ───────────────────────────────
# DEMO-1511 §1bis : le modèle disait « C'est noté » alors que rien n'était écrit. Chaque chemin où rien n'est écrit le
# dit au modèle (« rien n'a été noté »), ou dit que la note attend l'approbation ; une note écrite sans son secret le dit.


def _refused(r):
    return r.is_error and r.content.endswith(f" — {PM.NOT_WRITTEN}")


def _nothing_written(pm):
    return not any(e[0] == "memory.noted" for e in pm.events) and pm.learned(USER_PROJECT) == [] and all(
        pm.learned(d.name) == [] for d in (pm.root.glob("p-*") if pm.root.is_dir() else []))


def test_b5_secret_is_removed_and_the_result_says_so(tmp_path):
    pm, tools = _setup(tmp_path)
    pm.scrub = lambda s: s.replace("SECRET-123", "[secret:jeton]")
    r = _note(tools, kind="fact", title="Jeton de déploiement", text="le jeton est SECRET-123")
    assert not r.is_error and r.content.startswith("noté : ") and r.content.endswith(f" — {PM.SCRUBBED}")
    (n,) = pm.learned(pm._session("th_1").project)
    assert "SECRET-123" not in n.text
    clean = _note(tools, turn=2, title="Langue", text="Répondre en français")
    assert PM.SCRUBBED not in clean.content


def test_b5_size_limit_refusal_says_nothing_was_noted(tmp_path):
    """Pas de quota de notes dans le noyau : la seule borne d'écriture est la taille d'une note."""
    pm, tools = _setup(tmp_path)
    r = _note(tools, text="x" * (PM.MAX_TEXT + 1))
    assert _refused(r) and _nothing_written(pm)


def test_b5_forbidden_scope_refusals_say_nothing_was_noted(tmp_path):
    pm, tools = _setup(tmp_path)
    pm.core_block("th_1", 1, "retiens pour tous mes projets que je préfère les réunions le matin")
    quote = "je préfère les réunions le matin"
    # Portée user sans citation valable, dérivée du dépôt ou depuis un sous-agent : plus de refus, la note est écrite en
    # portée project (jamais user) et le résultat le dit ; rien n'entre dans la portée user.
    for i, kw in enumerate(({"quote": "des mots jamais dits ici"}, {"derived_from": "repo", "quote": quote})):
        r = _note(tools, scope="user", text=f"réunions le matin {i}", **kw)
        assert not r.is_error and PM.NARROWED in r.content and "portée project" in r.content, r.content
    r = tools.call("memory_note", {"kind": "preference", "title": "t", "text": "réunions le matin 3", "scope": "user",
                                   "derived_from": "user", "quote": quote}, ctx(thread="th_1/sa-1", turn=1))
    assert not r.is_error and PM.NARROWED in r.content, r.content
    assert pm.learned(USER_PROJECT) == [] and {e[1]["scope"] for e in pm.events} == {"project"}
    pm.events.clear()
    for n in pm.learned(pm._session("th_1").project):
        pm.forget_synced(pm._session("th_1").project, n.name)
    cases = [
        tools.call("memory_note", {"kind": "preference", "title": "t", "text": "réunions le matin",
                                   "scope": "project", "derived_from": "user"}, ctx(thread="th_sans_session")),
        _note(tools, kind="souhait"),  # type inconnu
    ]
    assert all(_refused(r) for r in cases), [r.content for r in cases]
    assert all(r.content.endswith("rien n'a été noté : dis-le à la personne") for r in cases)
    assert not any(e[0] == "memory.noted" for e in pm.events)


def test_b5_quarantine_result_says_not_yet_in_memory(tmp_path):
    pm, tools, _pending, store = _pending_setup(tmp_path)
    r = _note(tools)
    (held,) = store.list_pending("acc_1")
    assert not r.is_error and r.content == PM.PENDING_RESULT.format(id=held.id)
    assert "pas encore en mémoire" in r.content and "ne dis pas que c'est noté" in r.content
    assert _nothing_written(pm)


def test_b5_quarantine_refusal_or_failure_says_nothing_was_noted(tmp_path):
    pm, tools, pending, store = _pending_setup(tmp_path)
    pending.account_of_thread = lambda tid: None  # PendingError 403 : pas de compte pour ce fil
    r = _note(tools)
    assert _refused(r) and r.content.startswith("pas de compte pour ce fil")

    def broken(*a, **k):
        raise RuntimeError("magasin en panne")

    pending.account_of_thread = lambda tid: "acc_1"
    store.put_pending = broken
    r = _note(tools, turn=2)
    assert _refused(r) and r.content.startswith(PM.STORAGE_ERROR)
    assert _nothing_written(pm)


def test_b5_storage_error_says_nothing_was_noted(tmp_path, monkeypatch):
    pm, tools = _setup(tmp_path)

    def disk_full(path, text):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(PM, "_atomic_write", disk_full)
    r = _note(tools)
    assert _refused(r) and r.content.startswith(PM.STORAGE_ERROR)
    assert _nothing_written(pm)


def test_b5_replaces_refusals_say_nothing_was_noted(tmp_path):
    pm, tools = _setup(tmp_path)
    r = _note(tools, replaces="preference-inconnue-000000")
    assert _refused(r) and r.content.startswith("replaces : note inconnue de cette portée")
    assert _nothing_written(pm)


def test_b5_long_note_refusals_and_pending_are_explicit():
    from nokker.tenancy.context import tenant_scope
    from tests.unit.memory.test_long_note import FAKE_KEY, QUOTE, SAID, TENANT, A, Env, args

    env = Env()
    for bad in (args(kind="souhait"), args(title=""), args(quote="des mots jamais dits")):
        r = env.call(bad)
        assert r.is_error and r.content.endswith(f" — {LN.NOT_WRITTEN}"), r.content
    env.threads["th_x"] = A
    env.pending.account_of_thread = lambda tid: None if tid == "th_x" else A
    r = env.call(args(), tid="th_x")
    assert r.is_error and r.content == f"pas de compte pour ce fil — {LN.NOT_WRITTEN}"
    env.pending.account_of_thread = lambda tid: env.threads.get(tid)
    secret = env.call(args(text=f"Préfère des réponses courtes, clé {FAKE_KEY}"), said=SAID)
    assert not secret.is_error and secret.content.endswith(f" — {LN.SCRUBBED}")
    assert "pas encore en mémoire" in secret.content
    with tenant_scope(TENANT[A]):
        assert FAKE_KEY not in env.store.list_pending(A)[0].text

    def broken(*a, **k):
        raise RuntimeError("magasin en panne")

    env.store.put_pending = broken
    r = env.call(args(text="Préfère les réponses en anglais", quote=QUOTE))
    assert r.is_error and r.content == f"{LN.STORAGE_ERROR} — {LN.NOT_WRITTEN}"


def test_b5_a_removed_secret_never_reaches_the_model_neither_in_the_result_nor_in_a_refusal(tmp_path):
    """la relecture (B5) : le résultat et le motif d'un refus portent la MENTION du secret retiré, jamais sa valeur — y
    compris quand le motif reprend un argument (`replaces`, `links`)."""
    value = "SECRET-123"
    pm, tools = _setup(tmp_path)
    pm.scrub = lambda s: s.replace(value, "[secret:jeton]")
    ok = _note(tools, kind="fact", title=f"Jeton {value}", text=f"le jeton de déploiement est {value}")
    assert value not in ok.content and value.lower() not in ok.content and ok.content.endswith(PM.SCRUBBED)
    refusals = [
        _note(tools, turn=2, text=f"{value} " + "x" * PM.MAX_TEXT),  # taille
        _note(tools, turn=2, title="t", text="autre chose", replaces=f"preference-{value}"),  # replaces repris
        _note(tools, turn=2, title="t", text="autre chose", links=[f"note-{value}"]),  # liens repris
    ]
    assert all(r.is_error and value not in r.content for r in refusals), [r.content for r in refusals]
    narrowed = _note(tools, turn=2, scope="user", text=f"clé {value}", quote=f"clé {value}")  # citation absente
    assert not narrowed.is_error and value not in narrowed.content and PM.NARROWED in narrowed.content
    store = MemoryLearningStore()
    pending = PendingMemory(store=store, account_of_thread=lambda tid: "acc_1")
    pm2, tools2 = _setup(tmp_path / "q", pending=pending)
    pm2.scrub = pm.scrub
    held = _note(tools2, kind="fact", title="Jeton", text=f"le jeton est {value}")
    assert value not in held.content and held.content.endswith(PM.SCRUBBED)
    assert all(value not in n.text and value not in n.title for n in store.list_pending("acc_1"))


def test_b5_long_note_never_echoes_the_removed_secret():
    from tests.unit.memory.test_long_note import FAKE_KEY, SAID, TENANT, A, Env, args
    from nokker.tenancy.context import tenant_scope

    env = Env()
    r = env.call(args(title=f"Clé {FAKE_KEY}", text=f"Préfère des réponses courtes, clé {FAKE_KEY}"), said=SAID)
    assert not r.is_error and FAKE_KEY not in r.content and r.content.endswith(LN.SCRUBBED)
    bad = env.call(args(text=f"{FAKE_KEY} " + "x" * 1500), said=SAID)
    assert bad.is_error and FAKE_KEY not in bad.content
    with tenant_scope(TENANT[A]):
        assert all(FAKE_KEY not in n.text and FAKE_KEY not in n.title for n in env.store.list_pending(A))


def test_user_note_without_session_and_without_quote_goes_to_quarantine_not_refusal(tmp_path):
    """Hors session de code, aucune portée plus étroite : avec `memory_pending`, la note attend la validation de la
    personne (qui tient lieu de citation) au lieu d'être refusée ; rien n'est actif ni rappelé avant."""
    pm, tools, pending, store = _pending_setup(tmp_path)
    nosession = ctx(thread="th_sans_session", turn=1)
    args = {"kind": "preference", "title": "Langue", "text": "répondre en français", "scope": "user",
            "derived_from": "user"}
    r = tools.call("memory_note", args, nosession)
    (held,) = store.list_pending("acc_1")
    assert not r.is_error and r.content == PM.PENDING_RESULT.format(id=held.id) and held.scope == "user"
    assert _nothing_written(pm) and pm.recall("th_sans_session", "français") == []
    bad_quote = tools.call("memory_note", {**args, "text": "répondre en allemand", "quote": "mots jamais dits ici"},
                           nosession)
    assert not bad_quote.is_error and len(store.list_pending("acc_1")) == 2
    pending.decide("acc_1", held.id, True, "owner")  # la personne valide : la note devient active en portée user
    assert [n.scope for n in pm.learned(USER_PROJECT)] == ["user"]


def test_user_note_without_session_and_without_quarantine_is_still_refused(tmp_path):
    _pm, tools = _setup(tmp_path)
    r = tools.call("memory_note", {"kind": "preference", "title": "t", "text": "x y z", "scope": "user",
                                   "derived_from": "user"}, ctx(thread="th_sans_session", turn=1))
    assert r.is_error and r.content.endswith("rien n'a été noté : dis-le à la personne")
