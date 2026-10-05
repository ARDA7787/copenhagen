# Walkthroughs

A walkthrough explains one piece of code you are about to review. Read it before the diff.
Each one has the same sections:

1. **Purpose:** what this code is for, in two or three sentences.
2. **Invariants enforced:** which of I1–I12 (and which PRD rules) this code is responsible for.
3. **The five lines that matter:** the few places where a mistake would be a safety bug, with file:line.
4. **How to break it:** a table of mutations (for example, "flip `<` to `<=` in the ceiling check")
   and the test that catches each one. If no test catches a mutation, that is a bug in the tests.
5. **Review checklist:** questions to answer before you approve.

For the safety core (`core/canonical`, `core/conditions`, `audit/`, `validator/`, `policy/`, `engine/`),
the walkthrough skeleton and the tests come first, in their own commit. They are the spec. The
implementation follows in the next commit, and the finished walkthrough comes after that.

## How to review

- `git log --oneline main..` to see the commits, then `git diff main...` for the full change.
- Run `make check`. Then run the "How to break it" mutations on a scratch branch for at least one row.
- Merge with `git merge --no-ff` so each review chunk stays visible in history.

## Index

- [00 — Workflow vs activity](00-workflow-vs-activity.md) (Phase 0)
