"""Validate one shard's coverage database before it is combined.

Split out of `check_coverage.sh` rather than inlined: a heredoc that strips leading tabs
silently destroyed this code's indentation, and the gate then reported every shard as
"could not validate" — a true-sounding message for a broken validator. A file cannot be
reindented by the shell.

Exit codes are consumed by `check_coverage.sh` and must stay stable:
    0  usable
    3  readable, but measured no files at all
    4  unreadable / corrupt / not a coverage database
"""

import sys

from coverage import CoverageData


def main(path: str) -> int:
	data = CoverageData(basename=path)
	try:
		data.read()
	except Exception as exc:  # noqa: BLE001 - any read failure means the shard is unusable
		print(f"unreadable: {exc}", file=sys.stderr)
		return 4
	if not data.measured_files():
		print("no measured files", file=sys.stderr)
		return 3
	return 0


if __name__ == "__main__":
	if len(sys.argv) != 2:
		print("usage: validate_coverage_shard.py <coverage-data-file>", file=sys.stderr)
		sys.exit(4)
	sys.exit(main(sys.argv[1]))
