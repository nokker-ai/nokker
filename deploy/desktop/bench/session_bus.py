"""Banc du mode `session` SANS le bus de session de la personne (docs/DESKTOP.md §11, correctif du 06.11.2026).

Dans un même conteneur jetable, simule :
  - l'« hôte » : bus de session brut (dbus-run-session), bus d'accessibilité, Xvfb + metacity, gnome-text-editor, et
    trois FAUX services sensibles (org.freedesktop.secrets, Notifications, portal.Desktop) ;
  - le « conteneur » du nœud : SON bus de session privé (dbus-run-session) où seul nokker.desktop.a11y_shim existe,
    `NOKKER_A11Y_BUS` = l'adresse lue côté hôte par session-run.sh.
Usage : dbus-run-session -- python3 /opt/b1b/session_bus.py   → /results/session_bus.json
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

RES = Path("/results")
OUT: dict = {"steps": []}
procs: list[subprocess.Popen] = []
SENSITIVE = ("org.freedesktop.secrets", "org.freedesktop.Notifications", "org.freedesktop.portal.Desktop")


def log(name, ok, **kw):
    OUT["steps"].append({"step": name, "ok": ok, **kw})
    print(("OK  " if ok else "KO  ") + name + " " + json.dumps(kw, ensure_ascii=False)[:500], flush=True)


def bg(*argv, env=None):
    p = subprocess.Popen(list(argv), env=env or os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(p)
    return p


FAKE = r'''
from gi.repository import Gio, GLib
XML = """<node><interface name="org.example.Probe"><method name="Ping"><arg type="s" direction="out"/></method></interface></node>"""
info = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]
def own(name):
    def call(conn, sender, path, iface, method, params, inv):
        inv.return_value(GLib.Variant("(s)", ("SECRET-" + name,)))
    Gio.bus_own_name(Gio.BusType.SESSION, name, 0, lambda c, n: c.register_object("/x", info, call, None, None),
                     None, None)
for n in %r:
    own(n)
GLib.MainLoop().run()
''' % (SENSITIVE,)


def probe(dest: str) -> tuple[bool, str]:
    r = subprocess.run(["gdbus", "call", "--session", "--dest", dest, "--object-path", "/x", "--method",
                        "org.example.Probe.Ping", "--timeout", "3"], capture_output=True, text=True)
    return r.returncode == 0, (r.stdout or r.stderr).strip()[:160]


def names() -> list[str]:
    r = subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path",
                        "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.ListNames"],
                       capture_output=True, text=True)
    return sorted(t for t in r.stdout.replace("'", " ").replace(",", " ").replace("[", " ").replace("]", " ")
                  .replace("(", " ").replace(")", " ").split() if t.startswith("org."))


def host() -> None:
    os.environ.update(DISPLAY=":1", GTK_A11Y="atspi", GNOME_ACCESSIBILITY="1", NO_AT_BRIDGE="0")
    rt = Path("/tmp/rt-host")
    rt.mkdir(mode=0o700, exist_ok=True)
    os.environ["XDG_RUNTIME_DIR"] = str(rt)
    RES.mkdir(exist_ok=True)
    bg("python3", "-c", FAKE)
    bg("Xvfb", ":1", "-screen", "0", "1600x1000x24", "-nolisten", "tcp", "-noreset")
    time.sleep(1.5)
    bg("/usr/libexec/at-spi-bus-launcher", "--launch-immediately")
    bg("metacity", "--replace", "--no-composite")
    time.sleep(1.5)
    bg("gnome-text-editor")
    time.sleep(4)
    ok, out = probe("org.freedesktop.secrets")
    log("host_bus_has_secrets", ok, out=out)  # témoin : le bus de la personne, lui, a le service
    r = subprocess.run(["sh", "/opt/b1b/session-run.sh", "--print-a11y"], capture_output=True, text=True)
    addr = r.stdout.strip()
    log("session_run_reads_a11y_address", addr.startswith("unix:path=/"), address=addr, err=r.stderr[-200:])
    for bad in ("unix:abstract=/tmp/dbus-x", "tcp:host=1.2.3.4,port=9", ""):
        rr = subprocess.run(["sh", "/usr/local/bin/nokker-desktop-session"], env={**os.environ, "NOKKER_A11Y_BUS": bad},
                            capture_output=True, text=True)
        log(f"entrypoint_refuses[{bad or 'vide'}]", rr.returncode == 2)
    # « conteneur » : bus privé, jamais celui de l'hôte
    cenv = {k: v for k, v in os.environ.items() if k != "DBUS_SESSION_BUS_ADDRESS"}
    cenv.update(NOKKER_A11Y_BUS=addr, XDG_RUNTIME_DIR="/tmp/rt-c", PYTHONPATH="/opt/nokker")
    Path("/tmp/rt-c").mkdir(mode=0o700, exist_ok=True)
    r = subprocess.run(["dbus-run-session", "--config-file=/usr/local/share/nokker-desktop/private-bus.conf", "--",
                        "sh", "-c",
                        "python3 -m nokker.desktop.a11y_shim & i=0; while ! gdbus call --session --dest org.a11y.Bus "
                        "--object-path /org/a11y/bus --method org.a11y.Bus.GetAddress >/dev/null 2>&1; do i=$((i+1)); "
                        "[ $i -lt 50 ] || exit 1; sleep 0.1; done; exec python3 /opt/b1b/session_bus.py container"],
                       env=cenv, capture_output=True, text=True, timeout=180)
    for line in r.stdout.splitlines():
        if line.startswith("{"):
            OUT["steps"].append(json.loads(line))
            s = OUT["steps"][-1]
            print(("OK  " if s["ok"] else "KO  ") + "[conteneur] " + s["step"] + " " +
                  json.dumps({k: v for k, v in s.items() if k not in ("step", "ok")}, ensure_ascii=False)[:400],
                  flush=True)


def container() -> None:
    """Côté conteneur : sortie = une ligne JSON par contrôle (relue par l'hôte)."""
    def out(step, ok, **kw):
        print(json.dumps({"step": step, "ok": ok, **kw}, ensure_ascii=False), flush=True)

    out("private_bus_is_not_the_host_bus", "rt-host" not in os.environ.get("DBUS_SESSION_BUS_ADDRESS", ""),
        address=os.environ.get("DBUS_SESSION_BUS_ADDRESS"))
    out("private_bus_names", names() == ["org.a11y.Bus", "org.freedesktop.DBus"], names=names())
    for dest in SENSITIVE:
        ok, msg = probe(dest)
        out(f"blocked[{dest}]", not ok, out=msg)
    from nokker.desktop.node import AgentDesktop, Executor, Jobs, NodeError, health

    ad = AgentDesktop(binary="/usr/local/bin/agent-desktop")
    h = health(ad)
    out("health", h["desktop"] == "on", health=h)
    ex = Executor(ad=ad, jobs=Jobs(root=Path("/tmp/rt-c/nokker-desktop")), mode="session")
    ex.jobs.sweep_all()
    job, pol = "e" * 32, {"apps": ["gnome-text-editor"], "physical": "never", "snapshot_max": 12 * 1024}
    raw = subprocess.run(["agent-desktop", "list-windows"], capture_output=True, text=True).stdout
    out("raw_list_windows", '"ok":true' in raw, raw=raw[:600])
    try:
        w = ex.handle("desktop.list_windows", {"job": job}, pol)["windows"][0]["window"]
        s = ex.handle("desktop.snapshot", {"job": job, "window": w}, pol)
        tf = next(e for e in ex.jobs._job.snaps[s["snap_id"]].refs.values() if e.role == "textfield")
        r = ex.handle("desktop.type", {"job": job, "window": w, "app": "gnome-text-editor", "snap_id": s["snap_id"],
                                       "ref": tf.ref, "expect_role": tf.role, "expect_name": tf.name,
                                       "text": "Bus privé, AT-SPI intact"}, pol)
        out("type", r["delivery"] == "delivered_verified", result=r)
        s2 = ex.handle("desktop.snapshot", {"job": job, "window": w}, pol)
        out("read_back", "Bus privé, AT-SPI intact" in s2["text"], text=s2["text"][:200])
        try:
            ex.handle("desktop.press", {"job": job, "window": w, "app": "gnome-text-editor", "keys": "ctrl+s"}, pol)
            out("press_refused", False)
        except NodeError as e:
            out("press_refused", e.code == "physical_denied", code=e.code)
    except (NodeError, StopIteration, IndexError, KeyError) as e:
        out("flow", False, error=repr(e), detail=getattr(e, "detail", None))


if __name__ == "__main__":
    if sys.argv[1:] == ["container"]:
        sys.path.insert(0, "/opt/nokker")
        container()
        raise SystemExit(0)
    try:
        host()
    finally:
        (RES / "session_bus.json").write_text(json.dumps(OUT, ensure_ascii=False, indent=1))
        for p in procs:
            p.terminate()
        ok = sum(1 for s in OUT["steps"] if s["ok"])
        print(f"== session_bus : {ok}/{len(OUT['steps'])} étapes OK", flush=True)
