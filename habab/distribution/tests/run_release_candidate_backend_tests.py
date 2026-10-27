"""Run the focused release backend suites against the mounted HA component source."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import traceback
import unittest


DIST = Path(__file__).resolve().parents[1]
SRC = DIST / "src"
TESTS = DIST / "tests"


def main() -> int:
    sys.path.insert(0, str(TESTS))
    import homeassistant.components

    homeassistant.components.__path__.insert(0, str(SRC))

    modules = (
        "test_setup_agent",
        "test_setup_actions",
        "test_automation_setup",
        "test_diagnostics",
        "test_telegram_backend",
        "test_idigital_sms",
        "test_feedback",
        "test_feedback_action",
        "test_feedback_transport",
        "test_assistant_status",
        "test_provider_transport",
        "test_beta",
        "test_tool_history",
        "test_diagnostic_redaction",
        "test_telegram_voice",
        "test_telegram_group_invocation",
        "test_voice_route_hooks",
        "test_voice",
        "test_xiaomi_setup",
    )
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in modules)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        return 1

    spec = spec_from_file_location(
        "release_candidate_diagnostics_session_tests",
        TESTS / "test_diagnostics_sessions.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load diagnostics session assertions")
    diagnostics_tests = module_from_spec(spec)
    spec.loader.exec_module(diagnostics_tests)
    functions = [
        getattr(diagnostics_tests, name)
        for name in sorted(dir(diagnostics_tests))
        if name.startswith("test_")
    ]
    failures = 0
    for function in functions:
        try:
            function()
        except Exception:
            failures += 1
            traceback.print_exc()
    print(
        f"Diagnostics session assertions: {len(functions) - failures}/"
        f"{len(functions)} passed"
    )
    return int(failures != 0)


if __name__ == "__main__":
    raise SystemExit(main())
