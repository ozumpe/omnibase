"""OMNI-121: main.py names the contract before it spends anything."""

import main


def test_a_chosen_contract_is_named() -> None:
    assert main.contract_banner("sort") == "[sis] contract: sort"


def test_a_defaulted_contract_says_so_and_how_to_choose() -> None:
    banner = main.contract_banner(None)
    assert "sum_of_divisors" in banner
    assert "default" in banner and "--contract" in banner
