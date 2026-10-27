# Deployment

**Status: deployed** at https://pulseops.65-0-42-65.sslip.io (EC2 `t4g.small`, `ap-south-1`), following the steps below. Verified on the live instance:

- `/ready` reports the database and SQS as `ok`.
- Caddy obtained a Let's Encrypt certificate.
- A `pg_dump` backup uploaded to S3 using only the instance role.

Two differences from the steps below: access is through SSM Session Manager instead of SSH (no key pair, port 22 closed), and IMDSv2 is required with hop limit 2 so containers can reach the instance role.

## Recommended target: one small VM

```
Internet ──443──▶ Caddy (auto TLS) ──▶ dashboard (nginx, static SPA)
                                   └──▶ api (uvicorn)
                     relay, worker ──▶ AWS SQS (work queue + DLQ, via instance role)
                     postgres (on the VM, persistent volume)
```

This is the cheapest credible setup. It uses one VM, real SQS, and no load balancer or managed database.

### Cost drivers (estimates, not quotes)

Check current prices for your region in the AWS Pricing Calculator. `ap-south-1` (Mumbai) is the default region for this deployment.

| Item | Driver | Rough order of magnitude |
|---|---|---|
| VM (EC2 `t4g.small`, 2 GB RAM, or a Lightsail 2 GB plan) | Hours running | About $10–20/month |
| EBS / disk (20–30 GB gp3) | GB-month | A few dollars/month |
| SQS | Requests; the first 1M/month are free | Negligible at demo volume |
| Data transfer | Egress GB | Negligible for a demo |
| Claude API | Tokens per investigation; **only if** `PULSEOPS_INVESTIGATOR_PROVIDER=anthropic` | Usage-based. The default `mock` provider costs $0 |

Upgrades that cost more, and why you might want them:

| Upgrade | Cost | Benefit |
|---|---|---|
| RDS Postgres | Tens of dollars/month | Backups and point-in-time recovery |
| ALB + ECS Fargate | Roughly $40–80+/month | Rolling deploys, multiple replicas |

### Steps

1. **AWS resources.** From a machine with admin credentials, run:

   ```bash
   PULSEOPS_AWS_REGION=ap-south-1 deploy/vm/setup-aws.sh
   ```

   This creates `pulseops-work` (60 s visibility, 10 s long polling) and `pulseops-work-dlq` (14-day retention) with a redrive policy (`maxReceiveCount=5`), and prints a least-privilege IAM policy.
2. **VM.**
   - Launch Ubuntu 24.04 on `t4g.small` (arm64 is supported by all images used).
   - Attach an instance role that carries the printed policy. Do not put static AWS keys on the VM.
   - Security group: allow inbound 80/443 from anywhere, and 22 from your IP only.
3. **DNS.** Point an A record (for example `pulseops.example.com`) at the VM's Elastic IP.
4. **Install** Docker Engine and the Compose plugin, then clone the repository.
5. **Secrets.**

   ```bash
   cp deploy/vm/.env.prod.example deploy/vm/.env
   chmod 600 deploy/vm/.env
   ```

   Fill in strong random values (`openssl rand -hex 32`) for `POSTGRES_PASSWORD`, `PULSEOPS_APP_DB_PASSWORD` and `PULSEOPS_SESSION_SECRET`, and set `PULSEOPS_DOMAIN`.
6. **Start.**

   ```bash
   cd deploy/vm
   docker compose -f compose.prod.yml --env-file .env up -d --build
   ```

   `migrate` applies the schema, and Caddy obtains a TLS certificate automatically.
7. **Create real principals.** Keys are shown once:

   ```bash
   docker compose -f compose.prod.yml exec api python -m pulseops.cli create-key ops-approver --role approver
   docker compose -f compose.prod.yml exec api python -m pulseops.cli create-key airflow --role ingest
   ```

8. **Load demo data, optionally.** The HTTP demo endpoint is disabled in prod. An operator with shell access can run:

   ```bash
   docker compose -f compose.prod.yml exec -e PULSEOPS_INGEST_KEY=<ingest key> \
     -e PULSEOPS_API_URL=http://localhost:8000 api python -m simulator run c
   ```

9. **Verify.**
   - `https://<domain>/ready` returns `ready`.
   - Sign in to the dashboard.
   - `docker compose exec api python -m pulseops.cli verify-audit` reports a valid chain.

### Operations
- **Logs:** `docker compose -f compose.prod.yml logs -f api worker relay` (JSON with correlation IDs).
- **Dead letters:** `docker compose exec api python -m pulseops.cli dead-letters`.
- **Metrics:** `/metrics` is blocked at Caddy. Reach it with an SSH tunnel to the api container, or add an authenticated scrape path.
- **Backups (not automated).** Add a cron job running `pg_dump` and copying the output to S3 with lifecycle rules, or move to RDS.
- **Upgrades:** `git pull && docker compose -f compose.prod.yml up -d --build`. Migrations run before the API starts.

## Enabling the Claude investigator
Set `PULSEOPS_INVESTIGATOR_PROVIDER=anthropic` and `PULSEOPS_ANTHROPIC_API_KEY=...` in `.env`. The model defaults to `claude-opus-5-5`, configurable with `PULSEOPS_ANTHROPIC_MODEL`. Each investigation makes up to 10 API calls, so watch usage. The server-side refusal fallback is on by default (`PULSEOPS_ANTHROPIC_REFUSAL_FALLBACK=true`); it works on the Claude API only.

## Never
- Commit `.env` files or put API keys in frontend builds. The dashboard bundle only contains `VITE_API_URL`.
- Enable demo mode in production. The settings validator refuses it.
- Point the simulator sandbox at real infrastructure. Real actions need a new `ExecutionSandbox` implementation, a fresh threat model and their own review.
