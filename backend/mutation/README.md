# Mutation testing (engineering tool)

Coverage says which lines ran; mutation testing says whether the tests would *notice* if a line were
wrong. For each entry in the catalog the runner changes one thing in production code (for example
turns a tenant filter into `column == column`, or removes a signature check), runs the tests that
are supposed to catch that, and restores the file byte for byte. A mutant the tests do not fail on
**survived**: either the tests have a gap (add a test), or the mutant is equivalent (a second guard
makes the first redundant); every survivor is triaged and written down, never ignored.

This is deliberately **not** part of CI: it is slow (each mutant runs a test subset, some on real
PostgreSQL) and it edits source files in place. Run it before a release or after touching a control.

```bash
cd backend
docker compose up -d db redis            # integration mutants need them
python -m mutation.run list              # the catalog (id, file)
python -m mutation.run check             # every snippet matches exactly once; no test is run
python -m mutation.run run               # everything (long); or: run sg- hd- (id prefixes)
python -m mutation.run report            # the last results; `results.json` is git-ignored
```

* `catalog.py`: Phase 4 controls (tenant filtering, OAuth state and binding, shop-domain validation,
  token protection, webhook verification and deduplication, roles, sync claiming, embedding-model
  separation, access-log redaction).
* `catalog_41.py`: Phase 4.1 controls (production gate, request limits and ids, health, worker
  containment, log redaction, secret masking, tenant/role enforcement, Shopify error and version
  handling, webhook self-healing, the disconnect race, the Anthropic adapter and shared HTTP client,
  key rotation, the database engine).

The runner hashes the whole `app/` tree before and after and reports an error if they differ, so an
interrupted run cannot silently leave a mutated file behind (if you ever interrupt one, run
`git diff` / compare before doing anything else). Do not run it while editing `app/`.

## Results and known survivors

`python -m mutation.run report` exits non-zero if any mutant survived that is **not** listed in
`expected.py`, or if any has not run. Last full run (Phase 4.1): **144 mutants, 138 killed, 6
survived, all six documented**: four *equivalent* mutants (a guard that a second guard makes
redundant, so no test can fail), one labelling duplicate (`dedup-03` repeats `dedup-01`) and the
control (`token-08`, a harmless import that must survive). The first run of this catalog had 11
survivors; five were real test gaps and each got a test (an oversized-but-valid AI reply, a declared
oversize body that must not even be read, opaque `Bearer`/`Basic` credentials, a late order page after
a purge, and the atomic OAuth-state consume that only a real PostgreSQL race can expose).
