# Iris Cloudflare Email Worker

Cloudflare Email Routing worker that authenticates forwarded emails, parses RFC 5322 MIME payloads with `postal-mime`, strips tiny inline tracking images, and posts normalized JSON payloads to the Iris inbound gateway (`POST /webhook/email`).

## Runtime Configuration

Set the following non-secret environment variables in Cloudflare Workers Dashboard (or `wrangler deploy --var`):

- `IRIS_WEBHOOK_URL`: Public HTTPS webhook endpoint (e.g. `https://iris.example.com/webhook/email`)
- `ALLOWED_FORWARDERS`: Comma-separated list of authorized forwarding addresses (e.g. `user@example.com`)
- `FORWARD_VERIFICATION_TO`: Destination inbox for `forwarding-noreply@google.com` Gmail forwarding confirmation pass-through

Set the secret bearer token via Wrangler:

```bash
npx wrangler secret put IRIS_WEBHOOK_SECRET
```

## Development and Testing

```bash
npm install
npm run typecheck
npm test
```
