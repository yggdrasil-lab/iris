# Iris Cloudflare Email Worker

Cloudflare Email Routing worker that authenticates forwarded emails, parses RFC 5322 MIME payloads with `postal-mime`, strips tiny inline tracking images, and posts normalized JSON payloads to the Iris inbound gateway (`POST /webhook/email`).

## Runtime Configuration

Set the following non-secret environment variables in Cloudflare Workers Dashboard (or `wrangler deploy --var`):

- `IRIS_WEBHOOK_URL`: Public HTTPS webhook endpoint (e.g. `https://iris.example.com/webhook/email`)
- `ALLOWED_FORWARDERS`: Comma-separated list of authorized forwarding addresses (e.g. `user@example.com`)
- `FORWARD_VERIFICATION_TO`: Destination inbox for `forwarding-noreply@google.com` Gmail forwarding confirmation pass-through

Set the secret bearer token via Wrangler (or via `--secrets-file` in CI):

```bash
npx wrangler secret put IRIS_WEBHOOK_SECRET
```

## GitOps CI/CD Deployment

GitHub Actions automates validation and Worker deployment via `.github/workflows/deploy-worker.yml`:

- **Pull Requests & Pushes** touching `email-worker/**` or `.github/workflows/deploy-worker.yml` (plus manual `workflow_dispatch`) run `npm ci`, `npm run typecheck`, `npm test`, and `npx wrangler deploy --dry-run`.
- **Deployments** run on `push` to `main` (when `email-worker/**` or `.github/workflows/deploy-worker.yml` changes) or `workflow_dispatch`, gated by fail-loud configuration validation and serialized under concurrency group `iris-email-worker-deploy`.

### Required GitHub Secrets

- `CLOUDFLARE_API_TOKEN`: Cloudflare API token with **Workers Scripts: Edit** (Workers Admin) permission for the target account.
- `IRIS_WEBHOOK_SECRET`: Bearer secret expected by the Iris gateway `POST /webhook/email` endpoint (uploaded via `--secrets-file` with `0600` temp permissions, never printed in logs or CLI args).

### Required GitHub Variables

- `CLOUDFLARE_ACCOUNT_ID`: Target Cloudflare account ID for Worker deployment.
- `DOMAIN_NAME`: Base domain used to derive `IRIS_WEBHOOK_URL` (`https://iris.<DOMAIN_NAME>/webhook/email`, e.g. `example.com`).
- `IRIS_ALLOWED_FORWARDERS`: Comma-separated forwarding allowlist passed as `ALLOWED_FORWARDERS` (e.g. `sender@example.com,backup@example.com`).
- `IRIS_FORWARD_VERIFICATION_TO`: Destination inbox passed as `FORWARD_VERIFICATION_TO` for `forwarding-noreply@google.com` verification emails (e.g. `inbox@example.com`).

## Development and Testing

```bash
npm ci
npm run typecheck
npm test
npx wrangler deploy --dry-run --outdir /tmp/iris-email-worker-dryrun \
  --var "IRIS_WEBHOOK_URL:https://iris.example.com/webhook/email" \
  --var "ALLOWED_FORWARDERS:sender@example.com" \
  --var "FORWARD_VERIFICATION_TO:inbox@example.com"
```
