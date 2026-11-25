"""Banc B1b (docs/DESKTOP.md §9) : vrai agent-desktop 1.2.0, dans un conteneur jetable, sans réseau.
Usage : dbus-run-session -- python3 /opt/b1b/b1b.py virtual   (puis : session)
Écrit /results/<mode>.json et des lignes lisibles sur stdout."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from nokker.core.ports import ToolContext
from nokker.desktop import texts as T
from nokker.desktop.http import node_route
from nokker.desktop.node import AgentDesktop, Executor, Jobs, NodeClient, NodeError, check_pin, health
from nokker.desktop.service import DesktopToolSource, build_desktop
from nokker.desktop.snapshot import render

MODE = sys.argv[1] if len(sys.argv) > 1 else "virtual"
DISPLAY = ":99" if MODE == "virtual" else ":1"
OUT: dict = {"mode": MODE, "steps": []}
RES = Path("/results")
AD = AgentDesktop(binary="/usr/local/bin/agent-desktop")
procs: list[subprocess.Popen] = []


def log(name, ok, **kw):
    OUT["steps"].append({"step": name, "ok": ok, **kw})
    print(("OK  " if ok else "KO  ") + name + " " + json.dumps(kw, ensure_ascii=False)[:400], flush=True)


def bg(*argv, env=None):
    p = subprocess.Popen(list(argv), env=env or os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(p)
    return p


def raw(*args, headed=False, stdin=None, home=None):
    t = time.time()
    env = AD(list(args), headed=headed, stdin=stdin, home=home)
    return env, int((time.time() - t) * 1000)


def windows():
    env, _ = raw("list-windows")
    items = (env.get("data") or {}).get("windows") if isinstance(env.get("data"), dict) else env.get("data")
    return env, [w for w in items or () if isinstance(w, dict)]


def wait_window(pred, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        _, ws = windows()
        for w in ws:
            if pred(w):
                return w
        time.sleep(0.5)
    return None


def main():
    os.environ["DISPLAY"] = DISPLAY
    os.environ.update(GTK_A11Y="atspi", GNOME_ACCESSIBILITY="1", NO_AT_BRIDGE="0", SAL_USE_VCLPLUGIN="gtk3")
    rt = Path(f"/tmp/rt-{MODE}")
    rt.mkdir(mode=0o700, exist_ok=True)
    os.environ["XDG_RUNTIME_DIR"] = str(rt)
    RES.mkdir(exist_ok=True)

    # ── épinglage ──
    lock = json.loads(Path("/usr/local/share/nokker-desktop/agent-desktop.lock.json").read_text())
    try:
        check_pin("/usr/local/bin/agent-desktop", lock)
        log("pin", True)
    except NodeError as e:
        log("pin", False, code=e.code)

    # ── santé sans bus : le code retour de status vaut 0, le JSON dit non ──
    nobus = {k: v for k, v in os.environ.items() if k not in ("DBUS_SESSION_BUS_ADDRESS",)}
    r = subprocess.run(["/usr/local/bin/agent-desktop", "status"], env=nobus, capture_output=True, text=True)
    h = health(AgentDesktop(binary="/usr/local/bin/agent-desktop", env={}, runner=lambda argv, **kw: subprocess.run(
        argv, **{**kw, "env": {k: v for k, v in kw["env"].items() if k != "DBUS_SESSION_BUS_ADDRESS"}})))
    log("health_no_bus", h["desktop"] == "degraded", rc=r.returncode, health=h,
        status=json.loads(r.stdout or "{}").get("data", {}).get("permissions"))

    # ── bureau sans WM ──
    bg("Xvfb", DISPLAY, "-screen", "0", "1600x1000x24", "-nolisten", "tcp", "-noreset")
    for _ in range(50):
        if Path(f"/tmp/.X11-unix/X{DISPLAY[1:]}").exists():
            break
        time.sleep(0.1)
    bg("/usr/libexec/at-spi-bus-launcher", "--launch-immediately")
    time.sleep(1.5)
    bg("gnome-text-editor")
    time.sleep(3)
    h = health(AD)
    log("health_no_wm", h == {"desktop": "degraded", "reason": "no_wm"}, health=h)
    bg("metacity", "--replace", "--no-composite")
    time.sleep(2)
    h = health(AD)
    log("health_ok", h["desktop"] == "on", health=h)

    # ── formes réelles du CLI ──
    env, ws = windows()
    OUT["cli_list_windows"] = env
    gte = wait_window(lambda w: "Text Editor" in str(w.get("title")) or "text" in str(w.get("app_name") or w.get("app") or "").lower())
    log("cli_list_windows_shape", gte is not None and "id" in gte and "app_name" in gte, sample=gte)
    if gte is None:
        return
    wid, app = gte["id"], gte.get("app_name") or gte.get("app")
    env, ms = raw("find", "--window-id", wid, "--state", "focused", "--limit", "1")
    OUT["cli_find_states"] = env
    log("cli_find_states", bool(env.get("ok")), ms=ms, err=env.get("error"))
    env, ms = raw("launch", "thunar", "--activate", "--timeout", "20000")
    OUT["cli_launch"] = env
    log("cli_launch_shape", bool(env.get("ok")), ms=ms, data_keys=sorted((env.get("data") or {}).keys()),
        err=env.get("error"))

    # ── Executor : parcours gnome-text-editor (taper, enregistrer sous, fichier sur le disque) ──
    jobs = Jobs(root=rt / "nokker-desktop")
    jobs.sweep_all()
    ex = Executor(ad=AD, jobs=jobs, mode=MODE)
    _, ws = windows()
    apps = sorted({w.get("app_name") or w.get("app") for w in ws if w.get("app_name") or w.get("app")} | {app})
    pol = {"apps": apps, "physical": "allow" if MODE == "virtual" else "never", "snapshot_max": 12 * 1024}
    job = "b" * 32
    OUT["apps_seen"] = apps
    try:
        lw = ex.handle("desktop.list_windows", {"job": job}, pol)
        snap = ex.handle("desktop.snapshot", {"job": job, "window": wid}, pol)
        log("exec_snapshot_gte", True, shown=snap["shown"], total=snap["total"], text=snap["text"][:600],
            windows=lw["windows"])
        tf = next((ln.split()[0] for ln in snap["text"].splitlines() if "textfield" in ln), None)
        el = ex.jobs._job.snaps[snap["snap_id"]].refs[tf]
        out = ex.handle("desktop.type", {"job": job, "window": wid, "app": app, "snap_id": snap["snap_id"],
                                         "ref": tf, "expect_role": el.role, "expect_name": el.name,
                                         "text": "Bonjour depuis le Bureau B1b\n"}, pol)
        log("exec_type", out["delivery"].startswith("delivered"), out=out)
        if MODE == "session":  # physique interdit : press (XTEST) doit être refusé, rien n'est envoyé
            try:
                ex.handle("desktop.press", {"job": job, "window": wid, "app": app, "keys": "ctrl+shift+s"}, pol)
                log("session_press_refused", False)
            except NodeError as err:
                log("session_press_refused", err.code == "physical_denied", code=err.code)
            raise StopIteration
        out = ex.handle("desktop.press", {"job": job, "window": wid, "app": app, "keys": "ctrl+shift+s"}, pol)
        log("exec_press_save_as", out["mode"] == "physical", out=out)
        dlg = wait_window(lambda w: "Save" in str(w.get("title")) or "Enregistrer" in str(w.get("title")), 15)
        log("save_dialog", dlg is not None, dialog=dlg)
        if dlg:
            ex.jobs._job.windows[dlg["id"]] = (dlg.get("app_name") or dlg.get("app"))
            ds = ex.handle("desktop.snapshot", {"job": job, "window": dlg["id"]}, pol)
            log("exec_snapshot_dialog", True, shown=ds["shown"], text=ds["text"][:1500])
            refs = ex.jobs._job.snaps[ds["snap_id"]].refs
            name_f = next((e for e in refs.values() if e.role == "textfield"), None)
            save_b = next((e for e in refs.values() if e.role == "button" and e.name in ("Save", "Enregistrer")),
                          None)
            if name_f and save_b:
                target = "/home/desk/files/b1b.txt"
                out = ex.handle("desktop.set_value", {"job": job, "window": dlg["id"], "app": (dlg.get("app_name") or dlg.get("app")),
                                                      "snap_id": ds["snap_id"], "ref": name_f.ref,
                                                      "expect_role": name_f.role, "expect_name": name_f.name,
                                                      "text": target}, pol)
                log("exec_set_value_name", True, out=out)
                out = ex.handle("desktop.click", {"job": job, "window": dlg["id"], "app": (dlg.get("app_name") or dlg.get("app")),
                                                  "snap_id": ds["snap_id"], "ref": save_b.ref,
                                                  "expect_role": save_b.role, "expect_name": save_b.name}, pol)
                time.sleep(2)
                p = Path(target)
                log("file_saved", p.is_file() and "Bonjour" in p.read_text(), out=out,
                    content=p.read_text()[:80] if p.is_file() else None)
            else:
                log("dialog_targets", False, name_field=bool(name_f), save=bool(save_b))
    except NodeError as e:
        log("exec_flow", False, code=e.code, detail=e.detail)
    except StopIteration:
        pass

    # ── mesures : gestionnaires de fichiers, Writer ──
    def measure(label, launch_argv, pred):
        bg(*launch_argv)
        w = wait_window(pred, 40)
        if not w:
            log(f"measure_{label}", False, reason="no_window")
            return
        res = {}
        for tier, extra in (("i", []), ("skeleton", ["--skeleton"])):
            env, ms = raw("snapshot", "--window-id", w["id"], "-i", "--compact", *extra)
            if not env.get("ok"):
                res[tier] = {"err": env.get("error")}
                continue
            r = render(env, 12 * 1024, skeleton=bool(extra))
            res[tier] = {"raw_bytes": len(json.dumps(env)), "ref_count": (env.get("data") or {}).get("ref_count"),
                         "rendered_bytes": len(r.text.encode()), "shown": r.shown, "total": r.total, "cut": r.cut,
                         "ms": ms}
        try:
            ex.jobs._job.windows[w["id"]] = (w.get("app_name") or w.get("app"))
            s = ex.handle("desktop.snapshot", {"job": job, "window": w["id"]}, {**pol, "apps": [*apps, (w.get("app_name") or w.get("app"))]})
            res["executor"] = {"shown": s["shown"], "total": s["total"], "skeleton": s["skeleton"]}
        except NodeError as e:
            res["executor"] = {"refused": e.code, **{k: str(v) for k, v in e.detail.items()}}
        log(f"measure_{label}", True, app=(w.get("app_name") or w.get("app")), **res)
        OUT.setdefault("measures", {})[label] = {"app": (w.get("app_name") or w.get("app")), **res}

    measure("thunar", ["thunar", "/home/desk"], lambda w: "thunar" in str(w.get("app_name") or w.get("app") or "").lower())
    measure("nautilus", ["nautilus", "--new-window", "/home/desk"], lambda w: "nautilus" in str(w.get("app_name") or w.get("app") or "").lower())
    measure("writer", ["soffice", "--writer", "--norestore"], lambda w: "Writer" in str(w.get("title")))

    # ── appli GTK4 : champs secrets, case à cocher (physique selon la politique du mode) ──
    bg("python3", "/opt/b1b/gtk4app.py")
    w = wait_window(lambda w: w.get("title") == "Login Demo", 20)
    if w:
        ex.jobs._job.windows[w["id"]] = (w.get("app_name") or w.get("app"))
        p2 = {**pol, "apps": [*apps, (w.get("app_name") or w.get("app"))]}
        s = ex.handle("desktop.snapshot", {"job": job, "window": w["id"]}, p2)
        refs = ex.jobs._job.snaps[s["snap_id"]].refs
        log("gtk4_snapshot", True, text=s["text"])
        for name in ("Mot de passe", "PIN cache"):
            e = next((x for x in refs.values() if x.name == name), None)
            try:
                ex.handle("desktop.type", {"job": job, "window": w["id"], "app": (w.get("app_name") or w.get("app")), "snap_id": s["snap_id"],
                                           "ref": e.ref, "expect_role": e.role, "expect_name": e.name, "text": "x"}, p2)
                log(f"secure_{name}", False)
            except NodeError as err:
                log(f"secure_{name}", err.code == "secure_field", code=err.code)
        cb = next((x for x in refs.values() if x.role == "checkbox"), None)
        try:
            out = ex.handle("desktop.check", {"job": job, "window": w["id"], "app": (w.get("app_name") or w.get("app")), "snap_id": s["snap_id"],
                                              "ref": cb.ref, "expect_role": cb.role, "expect_name": cb.name,
                                              "checked": True}, p2)
            log("gtk4_check", MODE == "virtual", out=out)
        except NodeError as err:
            log("gtk4_check", MODE == "session" and err.code == "physical_denied", code=err.code)
    else:
        log("gtk4_window", False)

    # ── bout en bout : noyau (service + HTTP) + client du nœud + outils, vrai binaire ──
    e2e(apps, wid, app)


def e2e(apps, wid, app):
    import tempfile

    st = Path(tempfile.mkdtemp())
    svc = build_desktop({"enabled": True, "mode": MODE, "apps": {MODE: apps}, "wait_s": 20}, st)
    cards = []
    svc.approvals = SimpleNamespace(request=lambda turn, call, verdict, note: cards.append(
        SimpleNamespace(id=f"ap_{len(cards) + 1}", call=call, verdict=verdict)) or cards[-1])

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            node_route(self, svc, "GET", False)

        def do_POST(self):
            node_route(self, svc, "POST", False)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    ex = Executor(ad=AD, jobs=Jobs(root=Path(os.environ["XDG_RUNTIME_DIR"]) / "nokker-desktop-e2e"), mode=MODE)
    client = NodeClient(base=f"http://127.0.0.1:{httpd.server_port}", state_dir=st / "node", executor=ex)
    threading.Thread(target=client.run, daemon=True).start()
    for _ in range(100):
        if cards:
            break
        time.sleep(0.05)
    src = DesktopToolSource(svc)
    log("e2e_no_tools_before_enrollment", src.specs() == [])
    svc.on_decision(SimpleNamespace(id=cards[0].id, status="approved"))
    for _ in range(200):
        if svc.node_for() is not None:
            break
        time.sleep(0.1)
    log("e2e_node_ready", svc.node_for() is not None, view=svc.view()["nodes"])
    ctx = ToolContext(thread_id="th_b1b", turn_index=0, channel="cli", sender="alice")
    r = src.call(T.LOOK_TOOL, {"what": "status"}, ctx)
    log("e2e_status", "prêt" in r.content, content=r.content)
    r = src.call(T.LOOK_TOOL, {"what": "windows"}, ctx)
    log("e2e_windows", not r.is_error, content=r.content[:800])
    t = time.time()
    r = src.call(T.LOOK_TOOL, {"what": "snapshot", "window": wid}, ctx)
    log("e2e_snapshot", not r.is_error and "untrusted-data" in r.content, ms=int((time.time() - t) * 1000),
        content=r.content[:600])
    client.stop.set()
    httpd.shutdown()
    OUT["e2e_log"] = svc.view()["log"]


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        import traceback

        log("crash", False, error=repr(e), tb=traceback.format_exc()[-1500:])
    finally:
        (RES / f"{MODE}.json").write_text(json.dumps(OUT, ensure_ascii=False, indent=1))
        for p in procs:
            p.terminate()
        ok = sum(1 for s in OUT["steps"] if s["ok"])
        print(f"== {MODE} : {ok}/{len(OUT['steps'])} étapes OK", flush=True)
