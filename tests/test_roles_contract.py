"""Structural invariants of the role implementations (no Ray, no I/O).

Some role behaviour is only reachable with a live Ray cluster plus a
deliberately broken configuration. Where the invariant under test is a property
of the *code shape* rather than of a run — "every exit path reports X" — assert
it against the AST instead of standing up a cluster to visit one branch.
"""

import ast

from sis.paths import PROJECT_ROOT

_ROLES_SRC = (PROJECT_ROOT / "sis" / "roles.py").read_text(encoding="utf-8")


def _method(class_name: str, method_name: str) -> ast.FunctionDef:
    tree = ast.parse(_ROLES_SRC)
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == class_name)
    return next(n for n in cls.body
                if isinstance(n, ast.FunctionDef) and n.name == method_name)


def _returned_dict_literals(func: ast.FunctionDef) -> list[ast.Dict]:
    return [n.value for n in ast.walk(func)
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict)]


def test_swe_implement_reports_candidate_sha_on_every_path() -> None:
    # Regression (2026-07-28 minor list): the policy-block return path omitted
    # candidate_sha while the gauntlet-fail and success paths carried it, so a
    # policy-blocked cycle was logged with candidate_sha=None — the one field
    # tying that episode back to the exact diff the proposer produced, missing
    # from precisely the rejection you most want to audit.
    implement = _method("SWE", "implement")
    returns = _returned_dict_literals(implement)
    assert len(returns) >= 3, "expected the gauntlet-fail, policy-block and success paths"
    for literal in returns:
        keys = {k.value for k in literal.keys if isinstance(k, ast.Constant)}
        assert "candidate_sha" in keys, (
            f"SWE.implement returns at line {literal.lineno} without candidate_sha — "
            "every exit path must report the diff it was judging"
        )
