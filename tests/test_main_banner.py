"""OMNI-121: main.py names the contract before it spends anything."""

import main


def test_a_chosen_contract_is_named() -> None:
    assert main.contract_banner("sort") == "[sis] contract: sort"


def test_a_defaulted_contract_says_so_and_how_to_choose() -> None:
    banner = main.contract_banner(None)
    assert "sum_of_divisors" in banner
    assert "default" in banner and "--contract" in banner


def test_a_feature_is_proposed_as_one() -> None:
    # L51 (OMNI-148): run #6 filed "Speed up the roman target" in Confluence and Jira.
    assert main._proposal("roman")[0] == "Build the roman feature"
    assert "specification" in main._proposal("roman")[1]
    assert main._proposal("sort")[0] == "Speed up the sort target"
    assert main._proposal(None)[0] == "Speed up the sum_of_divisors target"
