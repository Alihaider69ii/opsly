"""Mutants that are EXPECTED to survive, each with the reason. Anything else surviving is a test gap.

An *equivalent* mutant removes a guard that a second guard makes redundant, so behaviour cannot
change: the right response is to document it, not to write a test that cannot fail. The control must
survive (it proves the runner does not call every change a "kill").
"""

EXPECTED_SURVIVORS = {}
