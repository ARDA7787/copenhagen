# Register your company's operations

A domain worker calls your service's API. Backend implementations stay in your services;
Copenhagen stores contracts, permissions, run state, and evidence. Use separate queues
and worker environments for domains that hold different credentials.

## Capability and recipe

Create a read capability first to validate connectivity without side effects:

```yaml
apiVersion: copenhagen/v1
kind: Capability
name: company.get_job
version: 1
owner: operations
summary: Read the status of a company operation.
inputs:
  job_id: {type: string, minLength: 1}
outputs:
  status: {type: string}
executor:
  adapter: http
  backend: company
  operation: GET /v1/jobs/{job_id}
  queue: operations
  credential: company_api
  allowed_hosts: [api.your-company.com]
  output_mapping: {status: data.status}
risk: {class: read}
verify: none
verify_reason: This is the authoritative status read.
compensate: none
compensate_reason: A read has no side effect.
```

```yaml
apiVersion: copenhagen/v1
kind: Recipe
name: company.check_job
version: 1
owner: operations
description: Check an operation in the company service.
parameters:
  job_id: {type: string, minLength: 1}
steps:
  - id: status
    capability: company.get_job@1
    inputs: {job_id: '{{job_id}}'}
```

Configure the worker's `backends.yaml`:

```yaml
company: https://api.your-company.com
```

Its environment includes `COPENHAGEN_CREDENTIALS=company_api` and
`COPENHAGEN_SECRET_COMPANY_API=<restricted vendor token>`. The API/control processes
must not receive that vendor token. Every process needs the same strong
`COPENHAGEN_AUTHORIZATION_KEY`. The `operations` worker reads its own environment;
it intentionally does not read `.env`.

## HTTP mapping

- Path placeholders are URL-escaped inputs. `query_inputs` moves named values to the
  query string; GET/HEAD send the remaining inputs as query parameters.
- `input_mapping: {local_field: vendor_field}` renames remaining body/query fields.
- `body_encoding` is `json` (default) or `form`.
- `output_mapping: {local_output: response.nested.field}` projects responses; numeric
  segments index arrays. Without a mapping, the entire response must match the output schema.
- `headers` configures non-secret static headers. Reserved authorization, host, cookie,
  run, and idempotency headers cannot be overridden.
- `credential_header` and `credential_prefix` default to `Authorization` and `Bearer `.
  Set them to `X-Api-Key` and `''` for a header API key. Secrets belong in the worker
  environment, never capability YAML or static headers.
- No redirects are followed. Production requires HTTPS and an exact allowed hostname.
- Every write carries `Idempotency-Key` and `X-Copenhagen-Run`. The backend must implement
  idempotency for safe repeats. Copenhagen cannot manufacture atomicity in a remote API.

For a write, add a separate read-only verification capability and declare `verify`:

```yaml
verify:
  capability: company.get_job@1
  inputs: {job_id: '${job_id}'}
  expect: {output: status, op: eq, value: succeeded}
  within: 10m
```

Verification inputs resolve against the original inputs, returned outputs, and
`idempotency_key`. To recover from a dropped response, design a verifier that can look
up the effect by a known input or idempotency key. If the verifier cannot reconstruct
required outputs, the run remains in attention rather than inventing them.

## Asynchronous backends

Set `executor.mode: webhook_callback`. An HTTP 202 must return `{"job_id":"opaque_id"}`.
The engine polls `poll_operation` (default `GET /jobs/{job_id}`) within the verification
window, using the same host checks, credential and output mapping. Polling must return
202 with the job ID while pending, or the final JSON outputs.

Alternatively send a signed callback to `/v1/callbacks/{run_id}/{step_id}` with
`{"job_id":"opaque_id","outputs":{...}}`. Configure `CALLBACK_SECRET` on the API and
callback sender. The hex HMAC-SHA256 signs these exact bytes:

`run_id + '.' + step_id + '.' + unix_timestamp + '.' + nonce + '.' + raw_body`

Headers: `X-Copenhagen-Timestamp`, `X-Copenhagen-Nonce`, `X-Copenhagen-Signature`.
Timestamps allow five minutes of skew; nonces are 16–128 characters. Callbacks bind to
the registered run, step and job and validate the output schema. A callback arriving
before the job has been registered returns 409; retry with the same signed body.
Successful delivery is idempotent. Completion still passes the configured verifier.

## Signed inbound events

Provision a service principal, grant its capability roles, and configure a source in
Administration's `hooks`, with `principal_id` and `allowed_recipes`. The recipe must set
`allow_event_trigger: true`, be independently pre-approved, and cover every step.

POST `/v1/hooks/{source}` with `{"recipe":"company.recipe","version":1,"parameters":{...}}`.
Use a separate `HOOK_SECRET`. Sign:

`source + '.' + unix_timestamp + '.' + nonce + '.' + raw_body`

Use the same three headers as callbacks. Binding the source prevents moving a signed
request to a source with a more privileged identity. Nonces are consumed in the same
transaction as the run and delivery message. Invalid parameters do not burn the nonce.

## Source labels, approvals and recovery

Recipe form parameters are user-supplied. Reference origins follow the producing
capability and propagate untrusted input through later steps. Caller-supplied Plan IR
source labels are discarded. Approval edits revalidate inputs, recalculate the hash,
and recheck hard policy limits. Pre-approval never overrides a denial or tainted
sensitive values. Changes to a recipe version or deprecation of its capabilities void
its pre-approval. Use distinct human requesters and approvers.

Use `POST /v1/recipes/{name}/run` with a scoped API key and an `Idempotency-Key` for
machine-triggered requests. Keep that key unchanged when retrying a lost response.
Scope API keys to their use; new keys cannot expand the caller's scopes. Revoke a key
using `POST /v1/api-keys/{key_id}/revoke`.

Schema `pattern` and condition `matches` accept a bounded regex subset: literals,
character classes, anchors, simple alternatives, and one repetition. Groups,
backreferences and compound repetitions are rejected at validation to prevent
catastrophic backtracking.
