from eval_harness.sandbox import run_pytest_check

_PASSING_CODE = "def add(a, b):\n    return a + b\n"
_PASSING_TEST = "from solution import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def test_run_pytest_check_passes_when_code_satisfies_tests():
    passed, _detail = run_pytest_check(_PASSING_CODE, _PASSING_TEST)
    assert passed is True


def test_run_pytest_check_fails_on_wrong_implementation():
    wrong_code = "def add(a, b):\n    return a - b\n"
    passed, detail = run_pytest_check(wrong_code, _PASSING_TEST)
    assert passed is False
    assert "test_add" in detail


def test_run_pytest_check_fails_when_generated_code_does_not_define_expected_name():
    empty_code = "x = 1\n"
    passed, detail = run_pytest_check(empty_code, _PASSING_TEST)
    assert passed is False
    assert detail != ""


def test_run_pytest_check_fails_on_syntax_error_in_generated_code():
    broken_code = "def add(a, b:\n    return a + b\n"
    passed, detail = run_pytest_check(broken_code, _PASSING_TEST)
    assert passed is False
    assert detail != ""


def test_run_pytest_check_times_out_on_infinite_loop():
    hanging_code = "def loop():\n    while True:\n        pass\n"
    hanging_test = "from solution import loop\n\n\ndef test_loop():\n    loop()\n"
    passed, detail = run_pytest_check(hanging_code, hanging_test, timeout=1)
    assert passed is False
    assert "timed out" in detail
