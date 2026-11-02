"""Le rôle du noyau : coffre (valeur dans SON environnement), déclaration du secret, ouverture de la session au proxy,
environnement du bac à sable écrit dans /work/.nokker/sandbox.env (sans aucune valeur)."""
import os
import shlex
import sys
from pathlib import Path

from nokker.egress.kernel import EgressSecrets, EgressSecretsConfig


class Vault:
    values = {"GH_TOKEN": os.environ["TEST_SECRET_VALUE"]}

    def list(self):
        return [type("I", (), {"name": n})() for n in self.values]

    def get(self, name):
        return self.values.get(name)


cfg = EgressSecretsConfig.from_config({
    "enabled": True, "proxy_url": "http://egress:3128", "control_url": "http://egress:3129", "ca_cert": "/ca/ca.crt",
    "secrets": {"GH_TOKEN": {"hosts": ["api.example.test"], "headers": ["authorization"]}}})
setup = EgressSecrets(cfg).open_session("cs_compose00000001", "tn_alice", Vault(), "/work", "/workspace")
if setup is None:
    sys.exit("session non accordée")
Path("/work/.nokker/sandbox.env").write_text("".join(f"export {k}={shlex.quote(v)}\n" for k, v in setup.env.items()))
print("session accordée :", ",".join(setup.names))
