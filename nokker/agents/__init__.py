"""Crew minimal (docs/BUBBLES.md §9.4) : un agent = `{name, role, model, tools, status}`, stockage
mémoire/Postgres. Pas d'exécution réelle (P2, hors de ce chantier) — `status` reste `armed` à la
création. Orchestration (CRUD + événement `agent.created`) : `hub.AgentsHub`."""
