---
name: ops-cert-expiry
description: Runbook for a TLS certificate that expires or expired
---
# Runbook: a certificate expires

Read `ops-basics` first. Goal: know the exact expiry, renew before it, and make renewal automatic again.

## 1. What the clients see (read, from any host)

```
echo | openssl s_client -connect <domain>:443 -servername <domain> 2>/dev/null | openssl x509 -noout -subject -issuer -enddate -ext subjectAltName
```

Check: the end date, that the names cover every domain served, and the issuer. Days left:

```
echo | openssl s_client -connect <domain>:443 -servername <domain> 2>/dev/null | openssl x509 -noout -checkend 1209600 && echo "more than 14 days" || echo "less than 14 days"
```

## 2. On the server (reads)

With an ACME client such as certbot:

```
certbot certificates
systemctl list-timers --all | grep -i -E "certbot|acme"
journalctl -u certbot -n 100 --no-pager
```

Files of a certificate (if the client stores them there): `ls -l /etc/letsencrypt/live/<domain>/`.

Usual causes of a failed renewal: the HTTP challenge cannot reach port 80 (firewall, redirect, proxy), a DNS
challenge credential expired, the timer is disabled, or the web server was never reloaded after renewal.

## 3. Renew (changes)

1. Dry run first: `certbot renew --dry-run --cert-name <domain>` (a change on the host: it is a card on prod).
2. If it passes: `certbot renew --cert-name <domain>`.
3. Reload the server that uses it (`systemctl reload nginx`, or the service concerned).

A certificate managed by a proxy, a load balancer or a hosting panel is renewed there: tell the user where to look;
do not try to replace it on the host.

## 4. Verify and prevent

Run the client check of step 1 again and report the new end date. If the timer was off, propose turning it back on
(`systemctl enable --now certbot.timer`) and a check that warns 14 days before expiry.
