# Milestone A demo (acceptance script, written before implementation)

1. `make up`, `make migrate`, `make seed`; `make dev` and domain workers.
2. Open localhost:8000. Sign in as Omar; refund order 1182 is blocked and identifies finance.
3. Sign in as Leela. Preview a $300 refund. Confirm; Sam sees resolved amount, origin and
   inputs hash. Leela cannot approve her own request. Sam approves; refund verifies.
4. A $7,500 refund is denied before any backend side effect.
5. Priya pre-approves onboarding. Google/Slack/GitHub are covered; AWS waits for Omar;
   payroll waits for Leela. Welcome email follows verified completion.
6. Restart API/control/domain workers and Temporal while an approval is waiting. Resume
   with the same run and approval; completed effects are not repeated.
7. Inject `commit_then_drop` into a refund; verify its existence before considering retry.
8. Run the B2B customer onboarding and trading strategy release recipes. Each is data
   interpreted by the same RunPlan workflow; trading release requires independent approval.
9. HMAC hook with nonce and timestamp starts only an eligible pre-approved event recipe;
   replay, tampered signature, unapproved recipe, and disabled capability are refused.
10. `copenhagen audit verify` verifies each tenant chain; `copenhagen audit check` proves
    every step start had an allow decision for the same resolved hash.

Use `make demo` for a scripted acceptance run. Mockworld displays effects at port 8010.
Live credentials and Google OAuth client configuration are separate from this local demo.
