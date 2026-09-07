"""Markers that make a test's role in a release gate visible to the gate itself.

`.github/helper/check_backup_completeness.sh` names, one by one, the tests whose execution
*is* an exit criterion. That list is only as good as somebody remembering to add to it, and
nothing forced them to: a new refusal guard could be written, pass, and never be watched —
F-2, carried out of P6.

`@refusal_guard` closes the loop from the other end. The gate walks the test tree for the
decorator and fails when a marked test is missing from its `REQUIRED` map, so the omission
shows up as a red gate on the commit that introduces it rather than as a quiet hole.

Marking is per-test on purpose. A class-membership rule cannot work here:
`TestApplyRefusals` deliberately mixes acceptance tests in with the refusals, and
`TestBackupVerification` mixes the "it verifies" case in with the "it refuses" ones.
"""

#: Attribute the AST walk cannot see, but a runtime reader can. The gate matches on the
#: decorator name in the source; this makes the same fact available to any test that wants
#: to introspect a live class.
REFUSAL_GUARD_ATTRIBUTE = "__cfs_refusal_guard__"


def refusal_guard(test):
	"""Mark a test as naming a refusal the app must make.

	The property such a test asserts is that something is *not* done — an unverified artifact
	is not deleted, an unscoped key is not signed, an unapproved cleanup does not start. Those
	are exactly the tests that pass by accident when they stop running, because nothing
	downstream notices a guard that never fired. So they are the ones a release gate has to
	confirm *executed*, rather than count.

	Purely declarative: it returns the function unchanged apart from one attribute.
	"""
	setattr(test, REFUSAL_GUARD_ATTRIBUTE, True)
	return test
