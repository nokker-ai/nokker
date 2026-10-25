---
name: ops-basics
description: How every ops runbook works with hosts, roles and cards
---
# Working on hosts

Every ops runbook of this pack follows these rules. Read them once per session.

## Hosts by name

- `ssh_hosts` lists the inventory: name, role (`prod`, `preprod`, `dev`), address, jump host, known key.
- Always call `ssh_run` with a host **name** from the inventory, never an address.
- First contact with a host: `ssh_trust {host}` shows the key fingerprint; the user checks it by another channel
  before `ssh_trust {host, fingerprint}` adds it. Never accept a fingerprint the user did not confirm.

## Reads first, then one change at a time

1. **Diagnose with reads only** (`df`, `systemctl status`, `journalctl`, `ss`, `openssl`). Reads on any host run
   without a card.
2. **Write down the plan** before any change: the exact command, the host, why, what it frees or fixes, how to
   undo it, and how you will check the result.
3. **One change per call.** A change is anything that is not a pure read: restart, delete, rotate, prune, reload.
   - on a `prod` host it always asks for a **strong** approval card, decided on the user's device; no mode, rule
     or written "go" in the chat replaces it;
   - on other hosts it follows the session's approval rules.
4. **Verify** with the same reads as the diagnosis, and report before and after.

## Commands that are never run

Wiping file systems or disks, recursive deletes of unknown paths, editing user accounts or keys, disabling a
firewall, piping a download into a shell. If a fix seems to need one, stop and explain to the user.

## Privileges

The runbooks assume the ssh user of the inventory has the rights the user gave it. If a command fails for lack of
rights, report it; do not try to raise privileges.

## Report

Host, role, what you saw (numbers), what you changed (exact commands, approved by whom), result after, and what
is left for the user (a ticket, a capacity plan, a renewal to watch).
