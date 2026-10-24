"""Mutants that are EXPECTED to survive, each with the reason. Anything else surviving is a test gap.

An *equivalent* mutant removes a guard that a second guard makes redundant, so behaviour cannot
change: the right response is to document it, not to write a test that cannot fail. The control must
survive (it proves the runner does not call every change a "kill").
"""

EXPECTED_SURVIVORS = {
    "org-03-sync-claim-org-check": (
        "equivalent: the claim UPDATE filters by connection id, and the connection was looked up "
        "scoped to the caller's organization one step earlier"
    ),
    "org-10-purge-delete": (
        "equivalent: the batch of ids to delete is selected with the organization filter, so the "
        "outer DELETE's repeat of it cannot match anything else"
    ),
    "oauth-01-replay": (
        "equivalent: the explicit consumed_at check precedes the atomic UPDATE ... WHERE consumed_at "
        "IS NULL, which rejects a replay on its own (oauth-09 removes that one and is killed)"
    ),
    "dedup-01-precheck": (
        "equivalent: the existence pre-check only avoids a failing INSERT; the unique constraint and "
        "its IntegrityError handler give the same answer (dedup-02 and dedup-07 are killed)"
    ),
    "dedup-03-both": "the same mutation as dedup-01 (a labelling duplicate kept for history)",
    "token-08-decrypt-error-leaks": "CONTROL: a harmless extra import, deliberately must survive",
}
