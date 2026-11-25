"""Appli GTK4 du banc B1b (reprise du spike) : un champ ordinaire, un `Gtk.PasswordEntry`, un `Gtk.Entry` masqué non
`secure` (« PIN cache », exposé en clair par l'amont), une case à cocher (`check` headless refusé en GTK4) et un
bouton. Le résultat de « Connexion » est écrit dans /home/desk/gtk4-result.txt (longueurs seulement)."""
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402


def activate(app: Gtk.Application) -> None:
    win = Gtk.ApplicationWindow(application=app, title="Login Demo")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    user = Gtk.Entry()
    user.set_placeholder_text("Utilisateur")
    user.update_property([Gtk.AccessibleProperty.LABEL], ["Utilisateur"])
    password = Gtk.PasswordEntry()
    password.update_property([Gtk.AccessibleProperty.LABEL], ["Mot de passe"])
    pin = Gtk.Entry()
    pin.set_visibility(False)
    pin.update_property([Gtk.AccessibleProperty.LABEL], ["PIN cache"])
    remember = Gtk.CheckButton(label="Se souvenir")
    label = Gtk.Label(label="pret")
    button = Gtk.Button(label="Connexion")

    def go(_button: Gtk.Button) -> None:
        label.set_text(f"OK {user.get_text()} pw={len(password.get_text())} pin={len(pin.get_text())} "
                       f"remember={remember.get_active()}")
        with open("/home/desk/gtk4-result.txt", "w", encoding="utf-8") as f:
            f.write(label.get_text())

    button.connect("clicked", go)
    for widget in (user, password, pin, remember, button, label):
        box.append(widget)
    win.set_child(box)
    win.present()


if __name__ == "__main__":
    application = Gtk.Application(application_id="ch.example.LoginDemo")
    application.connect("activate", activate)
    application.run()
