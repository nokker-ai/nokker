---
name: ops-service-down
description: Runbook for a service that is down or failing
---
# Runbook: a service is down

Read `ops-basics` first. Goal: understand why before restarting; a restart that hides the cause comes back.

## 1. State (reads)

For a systemd unit:

```
systemctl status <unit> --no-pager -l
systemctl show <unit> -p ActiveState -p SubState -p NRestarts -p ExecMainStatus
journalctl -u <unit> -n 200 --no-pager
```

For a container:

```
docker ps -a --filter name=<name>
docker inspect --format "{{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}} {{.RestartCount}}" <name>
docker logs --tail 200 <name>
```

## 2. Usual causes (reads)

- **Out of memory**: `OOMKilled true`, or `journalctl -k -n 100 --no-pager | grep -i -E "out of memory|oom"`.
- **Disk full**: `df -hT` (then `ops-disk-full`).
- **Port taken**: `ss -ltnp` and look for the port of the service.
- **Bad configuration after a change**: the service's own check, for example `nginx -t`, `apachectl configtest`,
  `sshd -t`, `haproxy -c -f <its config file>`; and when the configuration last changed
  (`ls -l --time-style=long-iso <config file>`).
- **A dependency is down**: database, cache, DNS (`getent hosts <name>`), certificate (`ops-cert-expiry`).
- **Crash loop**: `NRestarts` high, the same error at the end of each log run.

## 3. Fix (one change at a time)

- Fix the cause first (free space, correct the configuration with the user, bring the dependency back).
- Then restart: `systemctl restart <unit>` or `docker restart <name>`. On a prod host this asks for a strong card.
- If a configuration change caused it, propose reverting to the previous version rather than editing live.

## 4. Verify

`systemctl status` (or `docker ps`) shows it running for more than one minute, the logs show no new error, and the
service answers: `curl -fsS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:<port>/<health path>`.

Report the cause, the change, the result, and anything that will make it fail again.
