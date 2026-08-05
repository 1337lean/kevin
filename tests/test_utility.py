import pytest

from kevin.cogs.utility import safe_calculate


def test_safe_calculator() -> None:
    assert safe_calculate("2 + 3 * 4") == 14
    assert safe_calculate("(10 - 2) / 4") == 2


@pytest.mark.parametrize("expression", ["__import__('os')", "2 ** 100", "[1, 2]", "x + 1"])
def test_calculator_rejects_unsafe_expressions(expression: str) -> None:
    with pytest.raises(ValueError):
        safe_calculate(expression)
