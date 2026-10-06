# ADR-002: Retain Temporal for governed runbooks

Status: accepted, 6 October 2026.

Keep the PRD's one `RunPlan` interpreter and separate control/domain activity queues.
The implementation now passes workflow replay, independent-branch progress during
approval waits, safe handling of unknown outcomes, and compensation tests. A real
PostgreSQL/Temporal/HTTP acceptance test stops both workers while waiting for approval
and resumes the operation with fresh workers, producing one external write.

No DBOS migration is warranted. Application transactions do need a persistent outbox:
Temporal cannot make a database commit and a network signal atomic. Workflow IDs and
stable signal IDs make repeated delivery safe. Control activities own all audit/state
writes; domain activities return results and cannot import the database layer.

The interpreter processes ready steps as their dependencies finish instead of waiting
for a whole batch containing a human approval. Completion order supplies a reverse
(topologically valid) order for compensation. Existing histories use a Temporal patch
marker to retain the previous scheduling path.
