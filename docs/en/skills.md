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

Skills filtered by `platforms` do not show on other systems. A skill that needs the machine (files, terminal, ssh
hosts, added MCP servers, project memory) says `locality: local` in its frontmatter: it is never installed in the web
app ([packs.md](packs.md#packs-in-the-web-app)). Without it, a skill works on both sides.

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
- Project skills: `<repository>/.nokker/skills/`, same layout. See below.
- If two skills share a name, the one from the more specific folder wins (project, then yours), and `skill_view` says
  so.

## Project skills need your trust

**Beta**: on with `local.project_skills: true` in the core config. The one-shot command `nokker code`
reads them too, with the same flag or `--project-skills` (off by default), under the same rules below; when it finds
untrusted project skills it says so on stderr (`[skills] …`).

A repository's skills come with its code, so possibly from someone else. They follow the same rule as
[repository hooks](hooks.md): they are **not loaded** until you trust them for **this folder** and **this exact
content**.

1. When a session opens in a repository that has a `.nokker/skills` folder you have not trusted, the session's
   `skills` field says so (`project: "untrusted"`, with a `notice`), and a `skill.untrusted` event is sent. The
   fingerprint (`sha256`) covers every file of the folder: paths and contents.
2. Read the skills. Then trust them, from the same machine:
   - `POST /v1/local/skills/trust {"thread_id": "th_…", "sha256": "<fingerprint>"}` with the local token; or
   - `python -m nokker.cli skills show`, then `python -m nokker.cli skills trust --sha256 <fingerprint>` in the
     folder.
3. The trust is written to **your** settings (`trusted_project_skills: [{root, sha256}]`), never to the repository's.
   A repository cannot trust itself.

Then:

- The fingerprint is checked again each time the catalogue is read. If a file of `.nokker/skills` changes during the
  session (also when the agent edits it), the project skills are hidden again, with a new notice and a new fingerprint
  to trust.
- They are never loaded in **plan** mode.
- Trusted project skills still go through the guard above: a `dangerous` one stays hidden, and without the scanner
  none is shown.
- Only sessions whose folder is inside that repository (and their sub-agents) see them. The repository is the nearest
  folder with `.git`, at or above the session folder, never above the mounted folder.
- A `.nokker/skills` that is a symbolic link, or a folder over 500 files or 4 MiB, is ignored.
- Trusting is not possible through the gateway (`<name>.nokker.app`): only on the machine.
- `skill_remove` never touches a repository: only your skills folder.

## Not there yet

- Bundled skills: the [agent packs](packs.md) ship with Nokker and install their skills with `pack_install`. A
  broader curated catalogue is **planned**.
- Writing audit events to Nokker's cost and activity ledger: **planned**. For now they go to the core log.
