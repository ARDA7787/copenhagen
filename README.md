# Copenhagen

Copenhagen runs a company's operational runbooks over its existing APIs. A typed,
versioned capability defines an action. A recipe combines capabilities into a plan.
Copenhagen checks permissions and policy on the actual inputs, requests independent
approval where required, executes durably through Temporal, verifies outcomes, and
records a hash-chained audit trail in PostgreSQL.

Milestone A is governed runbooks without AI. The same interpreter supports operations
for a SaaS business, consumer product, or trading company. Business behavior belongs
in registered APIs and recipe data. Copenhagen is not a strategy runtime or a trading
execution engine.

## Start your own workspace

Requires Python 3.13, `uv`, and Docker for local PostgreSQL/Temporal.

```sh
uv sync --frozen
cp .env.example .env
make up
make migrate
uv run copenhagen init --name 'Your company' --admin-id owner --admin-email owner@your-company.com
make dev
```

Open <http://localhost:8000>. The local development sign-in lets you select your
provisioned identity. No demo catalog, mock backend, or sample employees are installed.
`make dev` starts the API and control worker. Domain workers run separately with only
their own backend credentials.

1. Use **Administration** to provision people and service identities, define capability
   roles, grant domain approver roles, and set budgets and event permissions.
2. Define your API capabilities and recipes in YAML. Follow the
   [integration guide](docs/operations/integrating.md).
3. Validate and publish them with `copenhagen capability` and `copenhagen recipe`, or
   use the authenticated API at `/docs`. High-risk API publications create a review;
   a different authorized publisher signs the exact specification in **Publication reviews**.
4. Start a worker for each capability queue, with a configured backend URL and scoped
   credentials. `COPENHAGEN_BACKENDS_FILE` selects the worker's backend configuration.
5. Choose a runbook, fill its form, inspect the preview, and confirm. Approvals and
   human tasks appear in the assigned person's inbox. Run pages expose retry, skip,
   cancellation, and explicitly requested compensation.

```sh
uv run copenhagen capability validate path/to/capability.yaml
uv run copenhagen capability publish path/to/capability.yaml --actor owner
uv run copenhagen recipe publish path/to/recipe.yaml --actor owner
# Run in a separate environment with no DATABASE_URL or DATABASE_OWNER_URL:
COPENHAGEN_BACKENDS_FILE=config/backends.yaml uv run copenhagen worker operations
```

The local publication CLI is an operator tool with database access. Its
`--second-reviewer` option records the review identity supplied by trusted CI/operators.
Use the API review workflow when Copenhagen must authenticate both reviewers itself.

## Reliability and access

- Plans pin immutable capability versions. Unknown actions, invalid schemas and references,
  missing roles, ceilings, tainted sensitive inputs, and tenant kill switches are checked.
- Domain workers require signed, input-bound control authorization and queue-scoped
  credentials. They do not load the control `.env` and reject database credentials.
- Workflow retries use idempotency keys and verification. An inconclusive write stays
  blocked, including after a human asks to retry. `continue` preserves completed work;
  `atomic` compensates in reverse dependency order through the same policy gates.
- Starts, approvals, human completions, callbacks and recovery commands use a transactional
  outbox. A committed decision survives an API crash or temporary Temporal outage.
- Recipe-run API clients can send `Idempotency-Key` to recover from a lost response without
  creating another run. Reusing the key with different parameters is rejected.
- OIDC company sign-in, expiring server-side sessions, CSRF protection, expiring hashed
  API keys, separation of duties, and fresh sign-in for high-risk approval are enforced.

## Operating and verifying

[Deployment and operations](docs/operations/deployment.md) covers real identity,
separate process environments, containers, migrations, backup, and recovery.
[Milestone A assessment](docs/assessment/milestone-a.md) records implementation evidence
and the acceptance items that require a real company environment.

```sh
make check             # lint, format, types, import boundaries, unit/workflow/replay tests
make test-int          # restricted-role PostgreSQL integration checks
make e2e              # real PostgreSQL + Temporal operational test
uv run copenhagen audit verify
uv run copenhagen audit check
uv run copenhagen audit export audit.jsonl
```

The example files in `capabilities/` and `recipes/`, `mockworld`, and `dev seed` are
optional development fixtures. They do not establish compatibility with vendor accounts.
Production rejects fake adapters and development identity/backend overrides.
Saved-plan infrastructure adapters, external audit anchoring, AI planning, and enterprise
multi-tenancy remain in their later PRD phases.
