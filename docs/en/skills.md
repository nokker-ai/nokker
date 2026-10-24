# Skills

A skill is a folder with a `SKILL.md` that teaches the agent a procedure. The agent sees a compact index (one line per
family) and loads a skill's full text only when it needs it.

## Layout

```
<family>/<skill>/SKILL.md          frontmatter: name, description (60 characters or fewer), platforms...
<family>/<skill>/references/       linked files the agent can read
<family>/<skill>/templates/
<family>/<skill>/assets/
<family>/<skill>/scripts/
```

Skills filtered by `platforms` do not show on other systems.

## Tools the agent has

| Tool | Effect | What it does |
|---|---|---|
| `skill_list(category?)` | READ | Names, families, descriptions |
| `skill_view(name, file?)` | READ | The skill body (without frontmatter), or one linked file. The path is confined to the skill's folder: a link that leaves it is refused. Files are limited to 256 KiB and must be text. |
| `skill_install(source, category?)` | DESTRUCTIVE | Install from `github:<owner>/<repo>[/<path>]`, an https URL to a `SKILL.md`, or `official/<family>/<name>`. Asks for a card. |
| `skill_remove(name)` | DESTRUCTIVE | Remove a skill you installed. Asks for a card. |

Skills never run a shell at load time, never install dependencies on their own, and are never pre-processed as
templates.

## The guard

Any skill that is not in a trusted folder is scanned, and the guard **fails closed**:

- verdict `dangerous`: the skill is invisible to the agent, and an audit event `skill_hidden` is logged;
- verdict `caution`: visible, with a warning;
- if the scanner is not available, skills that did not ship with Nokker are invisible, and `skill_list` says why.

Installation goes through a quarantine folder, the scan, an allow decision, and only then lands in your skills folder.
Audit events: `installed`, `install_refused`, `removed`, `skill_hidden`.

## Where skills live

- Your skills: `~/.nokker/state/skills/` (the state folder of an installed Lokkal). Only this folder is written by
  install and remove.
- If two skills share a name, the one from the more specific folder wins, and `skill_view` says so.

## Not there yet

- Project skills in `<folder>/.nokker/skills`: **planned**. The catalogue supports them, but installed sessions only
  load your user folder today.
- A curated set of bundled skills: **planned**. Today the catalogue is what you install.
- Writing audit events to Nokker's cost and activity ledger: **planned**. For now they go to the core log.
