# Portfolio validation false-positive incident

Date: 2026-08-29

## Summary

A one-shot Portfolio workflow was reported as successful even though its own log contained patch, Ruff and Pytest failures. The workflow then deleted convergence scripts and committed the deletion-only result.

This was not a valid acceptance run.

## Root cause

The workflow executed multiple independent gates inside one shell group after `set +e` and captured only the exit code of the final command:

```bash
set +e
{
  patch commands
  ruff commands
  pytest commands
  pip-audit
  bandit
} > validation.log 2>&1
status=$?
```

Shell groups return the status of the last command. Because Bandit succeeded, earlier failures did not make the step fail.

Observed earlier failures included:

- a patch anchor mismatch in `portfolio_store.py`;
- Ruff `F821` for `ExecutionCommandStatus`;
- import failure for `apply_funding_to_position`;
- Pytest collection failures in Portfolio tests.

## Impact

- The branch could appear validated while permanent source remained internally inconsistent.
- One-shot scripts were removed even though materialization had not succeeded.
- Later work accumulated on top of an unverified source boundary.
- Any claim that Portfolio convergence or Exchange Reconciliation had completed was unsupported.

## Corrective actions

1. Every command now runs under normal fail-fast shell behavior.
2. No critical gates are wrapped in `set +e`.
3. The patch chain is applied twice and must be idempotent.
4. One checksummed source artifact is produced once; Python, security, Docker and final commit gates all consume those exact bytes.
5. Python 3.11 and 3.12 validate independently.
6. Security and Docker gates are independent required jobs.
7. Materialization can run only after every validation job succeeds.
8. The materialization job refuses to overwrite a branch head newer than the validated SHA.
9. Failure logs remain in GitHub Actions; failures are not committed back into source history.
10. Backtests default to Portfolio-backed accounting; legacy accounting requires an explicit option.
11. The PR remains draft until permanent-source CI passes after materialization.

## Non-negotiable acceptance rule

A workflow conclusion alone is insufficient. Acceptance requires that the jobs contain executed steps, all required jobs succeed, permanent source imports cleanly, the full test suite passes on both Python versions, security scans pass, Docker builds, and no one-shot patch machinery remains in the final diff.
