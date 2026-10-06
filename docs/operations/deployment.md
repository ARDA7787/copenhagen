# Deployment and operations

Run one Copenhagen workspace per company. The API/control processes own Copenhagen's
PostgreSQL state. Each domain worker owns only its backend credentials. Use a managed
or independently operated Temporal service for deployment; the repository's
`docker-compose.yml` starts a local development server, not an HA production cluster.

## Environments and startup

1. Install with Python 3.13 and `uv sync --frozen --no-dev`, or build the application
   image with `docker build -t copenhagen:local .`. The image excludes `.env`, local data,
   development catalogs, and demo documentation. Dependencies come from `uv.lock`.
2. Provision PostgreSQL with separate schema-owner and restricted application roles.
   The migrations currently grant to `copenhagen_app`; use that role name. The SQL in
   `docker/postgres/init.sql` illustrates the grants but uses development passwords.
   Set deployment passwords outside the repository.
3. Apply `uv run alembic upgrade head` with `DATABASE_OWNER_URL` supplied only to the
   migration process. Back up the database first when upgrading an existing workspace.
   There is no destructive automatic downgrade.
4. Set `TENANT_ID`, `PUBLIC_URL`, company OIDC settings and a strong shared
   `COPENHAGEN_AUTHORIZATION_KEY`. Run `copenhagen init --name 'Company' --admin-id owner
   --admin-email owner@company.com`. No sample data is needed.
5. Start `copenhagen serve --host 0.0.0.0`, `copenhagen control-worker`, and
   `copenhagen worker <queue>` per domain. Put HTTPS termination in front of the API.
   Bind Temporal/PostgreSQL to private interfaces; neither is a public application API.

`compose.application.yaml` defines the API, control worker, and one domain worker for
existing infrastructure. Copy environment files to `deploy/control.env` and
`deploy/operations.env` (git-ignored), configure `config/backends.yaml`, then:

```sh
docker compose -f compose.application.yaml build
docker compose -f compose.application.yaml up -d
```

Control environment (never include vendor credentials):

```dotenv
ENV=prod
TENANT_ID=company
DATABASE_URL=postgresql+psycopg://copenhagen_app:REPLACE@postgres.internal:5432/copenhagen
PUBLIC_URL=https://operations.company.com
OIDC_CLIENT_ID=REPLACE
OIDC_CLIENT_SECRET=REPLACE
OIDC_ALLOWED_DOMAIN=company.com
COPENHAGEN_DEV_LOGIN=0
COPENHAGEN_DEV_BACKENDS=0
COPENHAGEN_AUTHORIZATION_KEY=REPLACE_WITH_RANDOM_SECRET
TEMPORAL_ADDRESS=temporal.internal:7233
TEMPORAL_NAMESPACE=company
```

Domain environment (no `DATABASE_URL`, `DATABASE_OWNER_URL`, or OIDC secret):

```dotenv
ENV=prod
TEMPORAL_ADDRESS=temporal.internal:7233
TEMPORAL_NAMESPACE=company
COPENHAGEN_AUTHORIZATION_KEY=REPLACE_WITH_SAME_RANDOM_SECRET
COPENHAGEN_CREDENTIALS=company_api
COPENHAGEN_SECRET_COMPANY_API=REPLACE_WITH_RESTRICTED_TOKEN
COPENHAGEN_BACKENDS_FILE=/config/backends.yaml
```

For Temporal over TLS/API-key authentication export `TEMPORAL_TLS=1` and
`TEMPORAL_API_KEY` to every process. Transport settings are read from process environment,
not a domain worker's dotenv. The application supports TLS/API-key transport; client
certificate (mTLS) provisioning is not implemented by this CLI.

For additional domains, define another domain service with its own queue and env file.
Never reuse a control env file for a worker. The worker refuses to start when database
credentials are present. Container egress firewalling and a managed secret broker are
Phase 7; configure host/network policy for your deployment now.

## Google Workspace login

Configure an OAuth web client with the redirect URI
`https://operations.company.com/auth/callback`. Copenhagen uses authorization code with
PKCE, nonce/state validation through Authlib, verified email, and the exact Workspace
`hd` domain. Provision the person's email first. On first verified sign-in, their
provider subject is bound to that identity; future sign-ins must match it and the email.
An unknown user receives no automatic account or permissions.

Sessions are server-side, expiring, HttpOnly and SameSite=Lax; production cookies are
Secure. Forms require CSRF tokens. Revisit `/auth/login` to perform a fresh sign-in for
high-risk approvals, publication reviews, and recipe pre-approval. API keys cannot
satisfy production step-up authentication. Disable a departing identity through the
admin API; every request and runtime authorization rechecks its status and roles.

## Policy and budgets

Bundled Cedar defaults are included in the wheel. Set `POLICY_DIRECTORY` to a directory
of reviewed `.cedar` files to replace them, mounted into API/control processes. Policies
are schema-validated and parsed at startup. Restart API and control workers together
when deploying a policy change. Each decision records the policy version.

Administration manages company domains, a kill switch, monetary ceilings, daily monetary,
message and record limits, and permitted event sources. Reservations are transactional
and shared across concurrent runs, principals and capabilities. Uncertain side effects
retain reservations; investigate them before changing limits. The PRD limits plans to
25 steps and execution to four ready steps per run.

## Recovery and audit

- `GET /healthz` verifies the API's database connection. Inspect Temporal worker health
  separately; an API health response does not prove a domain worker is available.
- A confirmed run is committed with its outbox message. API restart automatically resumes
  pending delivery. A workflow ID cannot be restarted accidentally. Workflow history,
  not in-process memory, owns progress and approval waits.
- Outbox delivery is per-run ordered and safe across several API instances. Transient
  failures (Temporal unavailable) back off exponentially up to five minutes. Permanent
  failures, or more than 50 attempts, move the message to `dead`, mark the run
  `needs_attention` and write a `dispatch.dead_lettered` audit event. List dead messages
  with `GET /v1/admin/outbox` and requeue one with
  `POST /v1/admin/outbox/{id}/requeue` after fixing the cause.
- A reconciler runs every `RECONCILE_MINUTES`. It closes runs whose Temporal workflow
  ended or vanished without recording a result (status `failed`, audit `run.orphaned`)
  and expires their open approvals and tasks. Trigger a pass with
  `POST /v1/admin/reconcile`.
- Approvers and task owners are notified through `NOTIFY_WEBHOOK_URL` (signed with
  `NOTIFY_WEBHOOK_SECRET`, HTTPS in production). Notifications are queued in the same
  transaction as the approval, retried by the outbox, and never carry step inputs.
- For a failed step, investigate its reason, then use **Retry safely**, **Skip** with a
  reason, or **Cancel**. An uncertain write must be verified absent before it is invoked
  again. Cancellation does not reverse completed work unless explicitly requested.
- Avoid terminating Temporal workflows manually. They may already have external effects.
  Use Copenhagen cancellation to retain the audit and compensation semantics.
- Back up both PostgreSQL and Temporal's persistence. A database backup alone does not
  include workflow histories. Test restoring both into an isolated environment before
  putting the restored system on a network that can reach live backends.
- Run `copenhagen audit verify` and `copenhagen audit check` daily from the operator
  environment, and export evidence with `copenhagen audit export <path>`. The application
  DB role cannot update/delete/truncate the audit. External anchoring is a Phase 6 item;
  the current hash chain does not protect against a database owner rewriting all history.
- Inputs/outputs needed to resume work exist in PostgreSQL and Temporal history. Protect
  both stores and backups accordingly. Confidential raw inputs are excluded from the
  append-only audit. This milestone does not implement encrypted payload codecs or a
  retention/deletion service.

## Acceptance against company systems

Automated tests exercise real local PostgreSQL/Temporal and an independent HTTP backend,
plus injected failures and workflow replay. Before a company's live rollout, supply its
OIDC client, least-privilege backend accounts, and reviewed capability contracts. Verify
that provider outputs, error semantics and idempotency match those contracts. The bundled
example vendor capabilities use mock conventions and are not certified vendor connectors.
No live vendor operation or real Google sign-in can be validated without those accounts.
