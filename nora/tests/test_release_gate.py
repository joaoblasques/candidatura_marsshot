"""Locks the local test-gate policy: release.sh must run the full suite before tagging, so no
change can be promoted to the Mini without passing tests on the dev machine first."""

from pathlib import Path

_RELEASE = Path(__file__).resolve().parent.parent / "scripts" / "release.sh"


def test_release_runs_tests_and_lint_before_tagging():
    text = _RELEASE.read_text()
    assert "uv run pytest" in text and "uv run ruff check" in text
    # the verification must come BEFORE the tag is cut, or it gates nothing
    assert text.index("uv run pytest") < text.index('git tag -a "$tag"')


def test_release_gate_default_on():
    # the gate runs unless the explicit emergency override is set
    text = _RELEASE.read_text()
    assert "NORA_RELEASE_SKIP_TESTS" in text and 'else' in text
