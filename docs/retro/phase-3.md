# Phase 3 retrospective

The one-interpreter architecture works across independently registered company APIs.
The principal gaps were integration boundaries rather than a need for another workflow
engine: lost delivery between database commits and Temporal, batch scheduling blocked by
human waits, and retry paths that lost approval or uncertain-outcome state.

These now have implementation and regression coverage. The application has a clean
company bootstrap and administration path; mockworld and seeded personas are optional.
Capabilities describe APIs, recipes describe business operations, and the engine has no
special refund, onboarding, customer or trading execution paths.

Temporal is retained (ADR-002). Remaining external acceptance needs a company's OIDC
client, real vendor accounts, and operator-reviewed contracts. The local acceptance
suite does not stand in for live vendor certification or design-partner adoption.
