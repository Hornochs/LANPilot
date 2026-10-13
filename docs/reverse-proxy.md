# Reverse proxy

LANPilot listens on `127.0.0.1:9030` by default and expects a reverse proxy in front of it
that provides HTTPS. This page shows what the proxy has to do and gives configurations.

## What LANPilot needs from the proxy

1. **The original `Host` header.** LANPilot compares it with `public_url`. A proxy that
   replaces it with `127.0.0.1:9030` causes "400 Invalid Host header".
2. **`X-Forwarded-For` and `X-Forwarded-Proto`**, and the proxy's address in
   `trusted_proxies`. Then the audit log and the sign-in throttle see the real client
   address. The default `127.0.0.1` fits a proxy on the same machine.
3. **No buffering of the event stream.** `/events` is a long-lived response that delivers
   live updates.

In `lanpilot.toml`, set `public_url` to the address users type, with `https://`.

If the proxy forwards the host name without a non-standard port (Nginx's `$host` does),
LANPilot still accepts the request, because `public_url` names the port.

## Nginx

Tested with the project's own development instance.

```nginx
server {
    listen 80;
    server_name lanpilot.example.lan;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    http2 on;
    server_name lanpilot.example.lan;

    ssl_certificate     /etc/ssl/lanpilot/fullchain.pem;
    ssl_certificate_key /etc/ssl/lanpilot/privkey.pem;

    # Map image uploads are up to 10 MB.
    client_max_body_size 12m;

    location / {
        proxy_pass http://127.0.0.1:9030;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Nothing special is needed for `/events`: LANPilot marks that response with
`X-Accel-Buffering: no`, which Nginx obeys. Note `client_max_body_size`: Nginx's default of
1 MB would reject most map image uploads.

## Caddy

Not yet tested by the project; based on Caddy's documented defaults.

```caddyfile
lanpilot.example.lan {
    reverse_proxy 127.0.0.1:9030
}
```

Caddy keeps the `Host` header, adds the `X-Forwarded-*` headers and streams
`text/event-stream` responses without buffering by default.

## Traefik

Not yet tested by the project; based on Traefik's documented defaults.

```yaml
http:
  routers:
    lanpilot:
      rule: Host(`lanpilot.example.lan`)
      entryPoints: [websecure]
      tls: {}
      service: lanpilot
  services:
    lanpilot:
      loadBalancer:
        servers:
          - url: http://127.0.0.1:9030
```

Traefik passes the `Host` header on and adds the `X-Forwarded-*` headers. If Traefik runs
on another machine or in a container, put its address into `trusted_proxies` and make
LANPilot listen where Traefik can reach it (`bind`).

## Without a reverse proxy

LANPilot can be used directly, for example on a LAN without any certificate
infrastructure:

```toml
[server]
public_url = "http://192.168.1.10:9030"
bind = "0.0.0.0"
trusted_proxies = []
```

Everything works, but passwords and session cookies then cross the network unencrypted,
and LANPilot says so on every page. Set `trusted_proxies = []`, so that nobody can fake
their address with an `X-Forwarded-For` header.

## Troubleshooting

| Symptom | Cause |
|---|---|
| "400 Invalid Host header" | the address in the browser differs from `public_url`; add it to `allowed_hosts` or fix the proxy's `Host` header |
| "403 Cross-site request rejected" when signing in | the page was opened under an address that is neither `public_url` nor an allowed host |
| sign-in succeeds but the next page asks again | `public_url` says `https://` while the browser uses `http://`; the `Secure` cookie is not sent back |
| pages do not update by themselves | the proxy buffers `/events` |
| the audit log shows `127.0.0.1` for everyone | the proxy does not send `X-Forwarded-For`, or its address is missing from `trusted_proxies` |
| "413" when uploading a map image | the proxy's body size limit is below 12 MB |
