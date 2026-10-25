---
name: jobs-setup
description: Connect a job-offers API and record your search profile
locality: local
---
# Setting up the jobs pack

The pack works with any job-offers API that the user connects through an MCP server (role `jobs`). Nothing is
hard-coded: no platform, no address, no account.

## 1. The job-offers server

- If the platform the user relies on offers an MCP server, use it.
- If it only has a REST API, build a small MCP server around it with the `mcp-builder` pack (four tools: search,
  get one offer, list applications, submit an application).
- The default tool names are `search_jobs`, `get_job`, `list_applications`, `submit_application`. If the server
  uses others, install the pack with a binding, for example
  `pack_install {name: "jobs", bind: {"jobs.search_offers": "<their search tool>"}}`.

## 2. Credentials by name

The user stores the key in the vault themselves (`nokker secret set JOBS_API_TOKEN`), then the server is added with
the reference only:

```
mcp_server_add {name: "jobs", url: "https://<your server>/mcp", token: "secret:JOBS_API_TOKEN"}
```

That call asks for an approval card; the server is active after the next restart of the core. Never ask for the key
in the chat.

## 3. The search profile

Ask the user, then record each item as a project note with `memory_note` (`kind: fact`, `derived_from: user`):

- target titles and close variants, seniority;
- places, radius or remote policy, work permit constraints;
- contract types, workload (full time, part time), earliest start date;
- salary floor if they want one;
- languages they work in;
- exclusions: companies, sectors, keywords.

Keep the CV in the project folder (for example `cv/cv.md` or a PDF the user puts there). The `cover-letter-drafts`
skill reads it; it never invents what is not in it.
