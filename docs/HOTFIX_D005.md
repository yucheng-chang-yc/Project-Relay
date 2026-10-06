# D-005 candidate: shared publication failure classification

Runtime baseline: `0.2.0-preview.3`. Overlay identity: `workbench.shared.PUBLICATION_FIX = D005-v1`. No plugin/API/schema binding change is required. The frozen preview.3 release ZIP remains unchanged; this candidate is an explicit source overlay.

A synchronous permission rejection previously left the pre-publication journal durable, so later writes to that path stayed blocked across grant renewal and restart. The hotfix records bounded phase/error type/errno/Win32 error/outcome in an additive diagnostic table. It never records exception text or file bytes.

Failures before invoking publication release only this invocation's journal. Known local filesystem rejection codes also require the authorized directory identity and original target SHA (or original absence) to remain intact before releasing it. Pending staged bytes can then be retried with the same write ID after correcting the local cause. Changed/unreadable targets, unknown I/O, process interruption, successful publication followed by cleanup/receipt failure, and older unexplained journals remain blocked until audited local recovery. A staging cleanup failure after a durable committed receipt reports the committed result with a warning.

Existing uncertain writes are preserved. The hotfix cannot reconstruct their original error and does not silently clear them. Local exact-SHA recovery remains a separate operation. Configuration, grants, task/result state and plugin binding are preserved.

## Reproduce and validate

```sh
cd local-pack/project-workbench
python -W error::ResourceWarning -m unittest discover -s tests -p 'test_shared_publication.py' -v
python -W error::ResourceWarning -m unittest discover -s tests -p 'test_*.py' -v
```

The 17 new source regressions cover rejection/create retry, verification failure/changed bytes, final precheck rejection, unknown I/O, revocation/restart, cleanup failure, DB receipt/commit failure, publish-then-error, process interruption, idempotency, legacy journal migration, and preserving revoked-grant refusal. They inject failures; they do not establish the cause of a particular Windows incident.

Prepare the overlay and run it against the unpacked, SHA-verified original Local ZIP:

```sh
python scripts/hotfixes/D005/prepare.py
python scripts/hotfixes/D005/test_patch_installer.py --local-pack <unpacked-original-local-package>
```

Default `apply_d005.py --root <installed-root>` is a read-only plan. Stop and verify the owning service locally before `--apply --backup-dir <new-private-folder>`. The tool checks all recorded installed file hashes and the precise baseline, backs up code/receipt and a consistent private SQLite snapshot, updates the receipt to describe the overlay, and refuses live locks or unrelated changes. `--rollback --backup-dir <recorded-folder>` restores code/receipt only and preserves database history. Do not restore the database automatically after later user work.

Deployment requires one controlled restart; no Plugin reimport or MCP rescan is required. Local fixture and GitHub runner results are distinct from operator deployment and a live ChatGPT file-write trial.
