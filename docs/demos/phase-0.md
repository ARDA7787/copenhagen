# Phase 0 demo: setup and spikes

Takes about five minutes. Docker Desktop must be running.

1. **Start the stack.**
   ```sh
   make up
   ```
   Postgres and the Temporal dev server come up healthy. Open the Temporal UI at
   <http://localhost:8233>.

2. **Run the checks.**
   ```sh
   make check
   ```
   ruff, formatting, pyright, import rules, unit tests (including spikes 1 and 2) and the `.pth`
   check all pass. Spike 1 waits two days for an approval and finishes in under a second.

3. **Restart Temporal while a workflow waits (Spike 3).**
   ```sh
   make test-int
   ```
   The test starts a workflow, restarts the Temporal container, checks the workflow is still running,
   sends the approval signal and sees it complete. In the UI, the run `run_spike3_…` shows the signal in
   its history.

4. **Show a clear failure.** Quit Docker Desktop, run `make up`, and read:
   "Docker daemon is not running. Start Docker Desktop, then re-run this command."

5. **Read** [`docs/walkthroughs/00-workflow-vs-activity.md`](../walkthroughs/00-workflow-vs-activity.md).
