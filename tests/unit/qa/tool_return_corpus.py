"""Corpus synthétique de RETOURS D'OUTILS pour mesurer le caviardage des résultats non étiquetés SECRET (G2).

Aucune donnée réelle : tout est fabriqué ici (empreintes par `hashlib` sur des chaînes fixes, UUID par `uuid5`, faux
secrets par CONCATÉNATION pour qu'aucun scanneur de secrets ne les prenne pour des vrais). Deux listes :

- `LEGIT` : sorties courantes qui ne contiennent AUCUN secret (hashes git, sha256sum, UUID, docker, k8s, JSON d'API,
  journaux, code lu avec `password` dans un nom de champ…) : tout caviardage dessus est un faux positif ;
- `SECRETS` : retours d'outils qui contiennent un faux secret, `(texte, valeur)` : la valeur ne doit plus y apparaître.
"""
from __future__ import annotations

import hashlib
import json
import uuid


def _h(label: str, algo: str = "sha1") -> str:
    return hashlib.new(algo, label.encode()).hexdigest()


def _u(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, label))


def _legit() -> list[str]:
    out: list[str] = []
    # git : log court, log complet, rev-parse, show --stat, blame
    for i in range(12):
        c = [_h(f"commit-{i}-{j}") for j in range(6)]
        out.append("\n".join(f"{x[:7]} fix: correctif {n}" for n, x in enumerate(c)))
        out.append(f"commit {c[0]}\nAuthor: Dev <dev@example.org>\nDate:   Mon Nov 2 09:00:00 2026\n\n    feat: étape {i}\n\n"
                   f" src/a.py | 4 ++--\n 1 file changed, 2 insertions(+), 2 deletions(-)")
        out.append(f"{c[1]}\n")
        out.append(f"^{c[2][:8]} (Dev 2026-11-02 09:00:00 +0100 1) import os\n{c[3][:8]} (Dev 2026-11-03 10:00:00 +0100 2) x = 1")
    # sha256sum / md5sum / digests d'images
    for i in range(10):
        out.append(f"{_h(f'file-{i}', 'sha256')}  dist/app-{i}.tar.gz\n{_h(f'f2-{i}', 'sha256')}  dist/app-{i}.whl")
        out.append(f"{_h(f'm-{i}', 'md5')}  README.md")
        out.append(f"web:{i} sha256:{_h(f'img-{i}', 'sha256')} 2 weeks ago 120MB")
    # UUID
    for i in range(12):
        out.append(json.dumps({"id": _u(f"a{i}"), "owner": _u(f"o{i}"), "state": "active", "items": [_u(f"i{i}{k}") for k in range(3)]}))
        out.append(f"request_id={_u(f'r{i}')} status=200 took=31ms")
    # docker ps / k8s
    for i in range(10):
        out.append(f"CONTAINER ID   IMAGE   COMMAND   STATUS\n{_h(f'ct{i}')[:12]}   nginx:1.27   \"nginx -g 'daemon…\"   Up {i} hours")
        out.append(f"NAME READY STATUS\nnginx-7d8b49557c-x{i}v9z 1/1 Running\napi-5f6c9d84b7-k{i}m2q 1/1 Running")
    # intégrité npm / pip / lockfiles
    for i in range(6):
        out.append(f'"node_modules/lib{i}": {{"version": "1.{i}.0", "integrity": "sha512-{_h(f"n{i}", "sha512")[:60]}=="}}')
        out.append(f"requests==2.3{i}.0\nurllib3==2.{i}.1\n--hash=sha256:{_h(f'p{i}', 'sha256')}")
    # fichiers, disque, processus, journaux
    for i in range(12):
        out.append(f"-rw-r--r-- 1 root root {1000 + i * 37} Nov  2 09:{i:02d} data_{i}.json\ndrwxr-xr-x 2 root root 4096 Nov  2 09:00 src")
        out.append(f"Filesystem Size Used Avail Use% Mounted on\n/dev/nvme0n1p{i + 1} 200G 80G 120G 40% /var/lib/data")
        out.append(f"Nov 02 09:{i:02d}:11 host1 sshd[{2100 + i}]: Accepted publickey for root from 192.0.2.10 port 5{i}022")
        out.append(f'10.0.0.{i + 3} - - [02/Nov/2026:09:00:{i:02d} +0100] "GET /api/v1/items?page={i} HTTP/1.1" 200 {512 + i} "-" "Mozilla/5.0"')
    # JSON d'API : agenda, mail, tâches, météo
    for i in range(12):
        out.append(json.dumps({"id": f"INBOX:1:{40 + i}", "from": "alice@example.org", "subject": f"Réunion {i}",
                               "snippet": "Bonjour, voici l'ordre du jour de la semaine prochaine.", "unread": True}))
        out.append(json.dumps({"events": [{"id": _h(f"ev{i}")[:26], "summary": f"Point {i}", "start": "2026-11-03T09:00:00+01:00"}]}))
        out.append(json.dumps({"task": {"id": 9000 + i, "title": f"Écrire le rapport {i}", "labels": ["pro"], "done": False}}))
        out.append(f"<CAF{_h(f'mid{i}')[:30]}@mail.example.org>")
    # URLs, chemins, dépôts
    for i in range(8):
        out.append(f"https://example.org/docs/page-{i}?ref=nav&utm_source=news&id={_h(f'u{i}', 'md5')[:12]}")
        out.append(f"/var/lib/nokker/state/threads/tn_aaaaaaaaaaaa/th_{_h(f't{i}')[:16]}/episodes.jsonl")
        out.append(f"git@git.example.org:org/repo-{i}.git\nbranch p2/integ-{i} tracking origin/p2/integ-{i}")
    # code et configuration lus avec le mot « password » comme NOM de champ (cas piégeux, honnêtes)
    out += [
        "def connect(host, user, password):\n    return db.connect(host=host, user=user, password=password)",
        "class Config:\n    password: str\n    user: str = 'app'",
        "password = os.environ['DB_PASSWORD']",
        "password: ${DB_PASSWORD}",
        "DB_PASSWORD=${DB_PASSWORD:?missing}",
        "self.password = hash_password(raw)",
        "<input type=\"password\" name=\"password\" placeholder=\"Mot de passe\">",
        "Mot de passe oublié ? Cliquez ici pour le réinitialiser.",
        "Le mot de passe est dans le coffre ; demandez-le à l'administrateur.",
        "Le mot de passe doit contenir au moins 12 caractères.",
        "password: required\nusername: required",
        "password: string (min 8)\nname: string",
        "--password <value>   mot de passe de l'utilisateur",
        "psql: FATAL: password authentication failed for user \"app\"",
        "password: ********",
        "Enter password: ",
        "passwd: password updated successfully",
        "mdp : voir Vaultwarden",
        "pip install --index-url https://pypi.example.org/simple/ package",
        "Résumé : 3 comptes, aucun mot de passe expiré.",
        "pin: false\npin_memory: true",
        "PIN_MEMORY=true\nBATCH=4096",
        "version pin = 1.4.2\nrequirements pinned: numpy==1.26.4",
        "The pin button is at the top right of the board.",
        "pw: /home/user/project",
        "export PASSWORD_MIN_LENGTH=12\nexport PASSWORD_MAX_AGE_DAYS=90",
        "Authorization: Bearer <token>",
        "{\"password\": null, \"user\": \"app\"}",
        "{\"password\": \"\", \"user\": \"app\"}",
        "{\"has_password\": true, \"two_factor\": false}",
        "login: ok (mot de passe : non modifié)",
    ]
    # sorties de tests, builds, versions
    for i in range(10):
        out.append(f"============ {40 + i} passed, 2 skipped in 12.{i}s ============\ntests/unit/test_a.py::test_case_{i} PASSED")
        out.append(f"Successfully built image sha256:{_h(f'b{i}', 'sha256')[:12]}\nTag: v1.{i}.0")
        out.append(f"Python 3.12.{i}\nnode v20.{i}.1\nUbuntu 24.04.{i} LTS")
    return out


LEGIT: list[str] = _legit()


def _secrets() -> list[tuple[str, str]]:
    pw = "Xk9" + "!qpzT" + "42Lm"
    pw2 = "Tr0ub" + "4dor" + "&3x"
    pw3 = "hunter" + "2" + "Zz"
    pin = "48" + "39" + "20"
    tok = "9fB2kQ7x" + "Lm3ZpR8t" + "Vw1YcN5d"  # 24 car. mélangés, sans mot-clé
    hexs = "3f9a1c7d" + "e24b86a05" + "c1d7e9f2b"  # 26 hexa
    out: list[tuple[str, str]] = []
    forms = [
        "mot de passe : {v}", "Mot de passe: {v}", "mdp : {v}", "password: {v}", "Password: {v}", "password={v}",
        "PASSWORD={v}", "passwd: {v}", "passwd={v}", "pw: {v}", "wifi (mot de passe : {v})", "le mot de passe du portail c'est {v}",
        "The password is {v}", "mot de passe du wifi : {v}", "{{\"user\": \"admin\", \"password\": \"{v}\"}}",
        "login: admin\npassword: {v}\nhost: db1", "DB_USER=app\nDB_PASSWORD={v}\nDB_HOST=db1", "user=admin passwd={v}",
        "mysql -u root --password={v} -h db1", "Identifiants du compte : utilisateur admin, mot de passe : {v}.",
    ]
    for v in (pw, pw2, pw3):
        for f in forms:
            out.append((f.format(v=v), v))
    for f in ("PIN : {v}", "code PIN = {v}", "pin: {v}", "Votre code pin: {v}", "pin={v}"):
        out.append((f.format(v=pin), pin))
    # sans mot-clé : seul le repli par entropie les voit
    for f in ("Voici la clé de l'appareil : {v}", "valeur retournée {v} fin", "{{\"result\": \"{v}\"}}"):
        out.append((f.format(v=tok), tok))
        out.append((f.format(v=hexs), hexs))
    return out


SECRETS: list[tuple[str, str]] = _secrets()
# Ceux qui n'ont AUCUN mot-clé : le mode « mot-clé seulement » ne peut pas les voir (limite assumée, mesurée).
NO_KEYWORD = [s for s in SECRETS if s[1] in ("9fB2kQ7xLm3ZpR8tVw1YcN5d", "3f9a1c7de24b86a05c1d7e9f2b")]
