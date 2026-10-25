---
name: ops-disk-full
description: Runbook for a file system that is full or nearly full
---
# Runbook: disk full

Read `ops-basics` first. Goal: free space safely now, and find why it filled.

## 1. Diagnose (reads)

```
df -hT -x tmpfs -x devtmpfs
df -i -x tmpfs -x devtmpfs
```

Note the file system above 90 percent (space) or above 90 percent (inodes: many small files).

Find what grows, staying on that file system (`-x`):

```
du -xh --max-depth=2 <mount point> 2>/dev/null | sort -h | tail -n 25
```

Usual suspects, each a read:

```
journalctl --disk-usage
du -sh /var/log /var/cache /var/lib/docker 2>/dev/null
docker system df
find /var/log -xdev -type f -size +200M -printf "%s %p\n" 2>/dev/null | sort -n | tail
```

Space held by deleted files still open (a process keeps writing to a log that was removed):

```
lsof -nP +L1 2>/dev/null | head -n 40
```

## 2. Choose the fix (one change at a time)

Pick the smallest change that frees enough space, in this order:

| Cause | Change | Undo |
|---|---|---|
| system journal too large | `journalctl --vacuum-size=500M` | none needed, old entries only |
| package cache | `apt-get clean` or `dnf clean packages` | packages are downloaded again when needed |
| old container images and build cache | `docker image prune -a --filter until=168h` then `docker builder prune --filter until=168h` | images are pulled again |
| a log file that grew without rotation | `truncate -s 0 <that log file>` after copying its tail if it matters | none: copy first |
| deleted file still open | restart the process holding it (see `ops-service-down`) | none |

Never delete data directories (databases, volumes, uploads) to free space: report them as the cause instead.

On a prod host each line above is a change and asks for a strong card. Show the plan first.

## 3. Verify

Run `df -hT` again and report the space before and after.

## 4. Prevent

Tell the user what will fill the disk again and propose one lasting fix: log rotation (`logrotate` rule), a
journal limit (`SystemMaxUse=` in the journald configuration), a scheduled image prune, or more space. Do not
apply it unasked.
