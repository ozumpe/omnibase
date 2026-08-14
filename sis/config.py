"""sis.config — one schema for every knob: YAML file, env var, CLI flag.

Before this module the engine had ~27 ``SIS_*`` environment variables, each read
ad hoc with ``os.getenv()`` at its point of use, each with its own default
spelled inline, and none of them discoverable except by grepping. That is
workable for a handful of flags and stops being workable at this size: the
`README` table drifted from the code, a typo in ``SIS_SANDBOX`` silently
selected the *soft* sandbox, and nothing could enumerate "what is this run
actually configured to do".

**One schema.** :data:`SCHEMA` declares every knob once — its section, its
protection tier, its type, its default, its environment variable, and what it
is for. Everything else in this module is derived from it: the typed config
objects, the committed ``config.yml``, the CLI flags, and the
effective-configuration view an operator UI renders.

**Precedence** (highest first)::

    CLI flag  >  environment variable  >  config.yml  >  built-in default

Each layer is optional and each is reported: :func:`effective` says not just
what a value is but *where it came from*, which is what makes a config
inspectable rather than merely settable.

**Tiers.** Every key is prefixed in YAML by how strongly it is protected —
``forbidden_``, ``strict_``, ``soft_`` — colocating the protection level with
the value instead of keeping a separate allowlist that decays. Two audiences
read that prefix, and they read it very differently:

- *The loop's code-generation path* ignores it entirely. ``config.yml`` and this
  module are both in ``sis.policy.GUARDRAIL_PATHS``, so the loop may not write
  either one, whatever the key says. The prefix grants the loop nothing.
- *A human, through the operator UI* is gated by it: ``forbidden_`` is not
  editable in the UI at all, ``strict_`` is editable behind an explicit
  confirmation, ``soft_`` is editable freely.

So the prefix is a statement about **human** authority. The loop was never
reaching this file either way.

**Reaching the Ray actors.** The role actors are detached Ray actors in their
own OS processes; they inherit the driver's environment *when they are created*
and never see a later change to it. Two consequences, both deliberate:

- The **file** layer works everywhere, because each process reads
  ``config.yml`` from disk itself.
- The **CLI** layer only works if it is applied *before* ``bootstrap()``, which
  is why :func:`apply_cli_overrides` writes the chosen values into
  ``os.environ`` as well as into this process's overlay. Without that,
  ``--sandbox-mode docker`` would configure the driver and quietly leave the
  actor that actually runs the gauntlet on the subprocess sandbox.

**Not re-read mid-run.** The file is parsed once per process and cached, so
editing ``config.yml`` while a loop is running cannot change its behaviour
half-way through a cycle. Operator edits take effect on restart, by design.
Environment variables *are* consulted on each read, which is what they did
before this module existed and what the test suite relies on; nothing in the
engine mutates its own environment after start-up.

Secrets are **not** here. Credentials live in ``secrets.local.yml`` behind
:mod:`sis.settings`, which masks them in ``repr()``; ``config.yml`` is
committed, so anything in it is public by construction.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from sis.paths import PROJECT_ROOT

# Committed, with the safe defaults, and POLICY-FORBIDDEN. Committed rather than
# gitignored on purpose: a change to a spend brake or the sandbox mode then
# arrives as a reviewable diff instead of as invisible local state. Per-run
# experiments (a deliberately tiny budget, say) belong in the environment layer,
# which overrides this file without touching the tree.
CONFIG_FILE = PROJECT_ROOT / "config.yml"


class ConfigTier(str, Enum):
    """How strongly a key is protected *from a human operator*.

    Deliberately a separate enum from :class:`sis.policy.ChangeTier`, which
    classifies *files the loop may rewrite*. The names match because the mental
    model matches, and ``tests/test_config.py`` pins that they stay in step —
    but importing one into the other would be a cycle (``policy`` reads its
    target paths from here) and would conflate two different questions.
    """

    FORBIDDEN = "forbidden"
    STRICT = "strict"
    SOFT = "soft"


class Source(str, Enum):
    """Which layer supplied a value. Reported by :func:`effective`."""

    DEFAULT = "default"
    FILE = "file"
    ENV = "env"
    CLI = "cli"


class Kind(str, Enum):
    """A key's type, used to parse the string layers and validate the file one."""

    STR = "str"
    OPT_STR = "opt_str"
    INT = "int"
    OPT_INT = "opt_int"
    FLOAT = "float"
    BOOL = "bool"
    STR_LIST = "str_list"


@dataclass(frozen=True)
class Key:
    """One configuration knob, declared once and never repeated."""

    section: str
    name: str
    tier: ConfigTier
    kind: Kind
    default: Any
    env: str
    doc: str
    # Restrict the accepted values. Used where a typo would otherwise fail
    # *silently and unsafely* — `SIS_SANDBOX=dcoker` used to mean "not docker",
    # i.e. run untrusted code without kernel isolation and say nothing.
    choices: tuple[str, ...] | None = None
    # Numbers are rejected when negative; `positive` additionally rejects zero,
    # for the knobs where zero is not a weaker setting but a broken one (a 0s
    # timeout fails every gate instantly).
    positive: bool = False
    # An extra CLI spelling, for flags that predate this module.
    alias: str | None = None

    @property
    def yaml_key(self) -> str:
        """The key as written in ``config.yml`` — tier prefix included."""
        return f"{self.tier.value}_{self.name}"

    @property
    def flag(self) -> str:
        """The derived CLI flag, e.g. ``sandbox.mode`` → ``--sandbox-mode``."""
        return f"--{self.section}-{self.name}".replace("_", "-")

    @property
    def path(self) -> str:
        """``section.name`` — how a key is addressed in code and in errors."""
        return f"{self.section}.{self.name}"


# --------------------------------------------------------------------------
# The schema — every knob the engine has
# --------------------------------------------------------------------------

# The SOFT-tier optimisation targets. Lives here rather than in sis/policy.py so
# that the default and the config key that overrides it cannot drift apart;
# `policy.DEFAULT_TARGET_PATHS` re-exports it, and both modules are FORBIDDEN,
# so the guarantee is unchanged by the move.
DEFAULT_TARGET_PATHS: tuple[str, ...] = (
    "runtime/target.py",
    "runtime/sort_target.py",
    # The first Class-2 target (contract `roman`). Unlike the two above, this
    # file does not exist yet — that is the point of feature construction, and
    # the tier has to be declared before the implementer may write it.
    "runtime/roman.py",
)

SCHEMA: tuple[Key, ...] = (
    # --- brakes: the CEO's spend authority. All forbidden: these are the only
    # thing standing between a bug and an unbounded API bill. -----------------
    Key("brakes", "budget_usd", ConfigTier.FORBIDDEN, Kind.FLOAT, 5.0,
        "SIS_BUDGET_USD", "Hard cap on total LLM spend, in USD."),
    Key("brakes", "breaker_threshold", ConfigTier.FORBIDDEN, Kind.INT, 3,
        "SIS_BREAKER_THRESHOLD",
        "Consecutive failed cycles that trip the circuit breaker."),
    Key("brakes", "max_cost_per_accepted_usd", ConfigTier.FORBIDDEN, Kind.FLOAT, 2.0,
        "SIS_MAX_COST_PER_ACCEPTED_USD",
        "Economics SLO: USD per accepted improvement before the loop is frozen."),
    Key("brakes", "slo_min_spend_usd", ConfigTier.FORBIDDEN, Kind.FLOAT, 0.50,
        "SIS_SLO_MIN_SPEND_USD",
        "Spend below which the economics SLO is not judged (too little signal)."),

    # --- sandbox: how untrusted, generated code is contained. ----------------
    Key("sandbox", "mode", ConfigTier.FORBIDDEN, Kind.STR, "subprocess",
        "SIS_SANDBOX",
        "Isolation for generated code: 'subprocess' (soft) or 'docker' "
        "(kernel-enforced, --network none).",
        choices=("subprocess", "docker")),
    Key("sandbox", "image", ConfigTier.FORBIDDEN, Kind.STR, "sis-gauntlet:latest",
        "SIS_SANDBOX_IMAGE", "Container image for the docker sandbox."),
    Key("sandbox", "memory", ConfigTier.FORBIDDEN, Kind.STR, "1g",
        "SIS_SANDBOX_MEMORY", "Memory ceiling for a docker-sandboxed gate."),
    Key("sandbox", "cpus", ConfigTier.FORBIDDEN, Kind.STR, "2",
        "SIS_SANDBOX_CPUS", "CPU ceiling for a docker-sandboxed gate."),
    Key("sandbox", "timeout_seconds", ConfigTier.FORBIDDEN, Kind.FLOAT, 120.0,
        "SIS_GAUNTLET_TIMEOUT",
        "Wall-clock cap per gate — what contains an infinite loop in generated code.",
        positive=True),
    Key("sandbox", "allow_unsandboxed_llm", ConfigTier.FORBIDDEN, Kind.BOOL, False,
        "SIS_ALLOW_UNSANDBOXED_LLM",
        "Override the rule that a real (non-stub) proposer requires the docker "
        "sandbox. Lets untrusted code read host files; for deliberate risk only."),

    # --- policy: what the loop may rewrite. ----------------------------------
    Key("policy", "target_paths", ConfigTier.FORBIDDEN, Kind.STR_LIST,
        DEFAULT_TARGET_PATHS, "SIS_TARGET_PATHS",
        "Repo-relative paths in the SOFT tier — the designated optimisation "
        "targets. Guardrail paths always win over this list."),
    Key("policy", "allow_strict_changes", ConfigTier.FORBIDDEN, Kind.BOOL, False,
        "SIS_ALLOW_STRICT_CHANGES",
        "Let the loop propose changes to STRICT engine code (still requires "
        "human approval and a justification)."),

    # --- episodic: the dataset the system learns from. -----------------------
    # Forbidden for *every* transition, not merely for switching to 'none'. The
    # episodic log may outlast the target it was collected around and be the
    # most durable thing this project produces, so no backend change happens
    # without a human in the loop — not just no destructive one.
    Key("episodic", "store", ConfigTier.FORBIDDEN, Kind.STR, "jsonl",
        "SIS_EPISODIC_STORE",
        "Episodic/provenance backend: 'jsonl', 'duckdb', or 'none'.",
        choices=("jsonl", "duckdb", "none")),

    # --- adapters: which outside world the engine talks to. ------------------
    Key("adapters", "mode", ConfigTier.FORBIDDEN, Kind.STR, "memory",
        "SIS_ADAPTERS",
        "'memory' for in-process fakes, 'real' for Confluence/Jira/GitHub/AWS.",
        choices=("memory", "real")),
    Key("adapters", "env", ConfigTier.FORBIDDEN, Kind.STR, "local",
        "SIS_ENV", "Deployment: 'local' (file secrets) or 'aws' (Secrets Manager).",
        choices=("local", "aws")),
    Key("adapters", "secrets_file", ConfigTier.FORBIDDEN, Kind.STR,
        "secrets.local.yml", "SIS_SECRETS_FILE",
        "Path to the gitignored local secrets file."),
    Key("adapters", "aws_region", ConfigTier.FORBIDDEN, Kind.STR, "us-east-1",
        "SIS_AWS_REGION", "AWS region for Secrets Manager and cloud adapters."),
    Key("adapters", "aws_secret_id", ConfigTier.FORBIDDEN, Kind.OPT_STR, None,
        "SIS_AWS_SECRET_ID", "Secrets Manager secret id (required when env=aws)."),
    Key("adapters", "http_timeout_seconds", ConfigTier.SOFT, Kind.FLOAT, 30.0,
        "SIS_HTTP_TIMEOUT",
        "Per-request timeout for real-adapter HTTP calls.", positive=True),

    # --- proposer: who writes the candidate, and with what model. ------------
    # Strict rather than forbidden: switching proposer changes *who* writes the
    # code, which the sandbox rule already reacts to (a non-stub proposer
    # requires docker), so the dangerous direction is already blocked downstream.
    Key("proposer", "backend", ConfigTier.STRICT, Kind.STR, "stub",
        "SIS_PROPOSER",
        "'stub' for the offline hand-written candidate; anything else is a real "
        "LLM and is treated as untrusted."),
    Key("proposer", "llm_provider", ConfigTier.STRICT, Kind.STR, "anthropic",
        "SIS_LLM_PROVIDER", "LLM vendor backing a real proposer."),
    Key("proposer", "llm_model", ConfigTier.STRICT, Kind.OPT_STR, None,
        "SIS_LLM_MODEL", "Model id; unset uses the provider's default."),

    # --- canary: how a candidate is judged against traffic. ------------------
    # Strict despite the asymmetry: turning the live canary *on* only adds rigour,
    # but the same key turns it off, and that removes a check.
    Key("canary", "backend", ConfigTier.STRICT, Kind.OPT_STR, None,
        "SIS_CANARY",
        "'serve' judges the canary against real Ray Serve traffic; unset uses "
        "the legacy in-memory recording.",
        choices=("serve", "legacy")),

    # --- loop: pacing. Genuinely soft — nothing here weakens a check. --------
    Key("loop", "interval_seconds", ConfigTier.SOFT, Kind.FLOAT, 30.0,
        "SIS_LOOP_INTERVAL", "Seconds between cycles in --loop mode."),
    Key("loop", "max_cycles", ConfigTier.SOFT, Kind.OPT_INT, None,
        "SIS_LOOP_MAX_CYCLES", "Stop after N cycles; unset runs until signalled."),

    # --- contracts: which target a cycle optimises. --------------------------
    Key("contracts", "default", ConfigTier.SOFT, Kind.OPT_STR, None,
        "SIS_CONTRACT",
        "Default contract name; --contract still overrides it per cycle.",
        alias="--contract"),
)

SECTIONS: tuple[str, ...] = tuple(dict.fromkeys(key.section for key in SCHEMA))

_BY_PATH: dict[str, Key] = {key.path: key for key in SCHEMA}
_BY_FLAG: dict[str, Key] = {key.flag: key for key in SCHEMA}
_BY_FLAG.update({key.alias: key for key in SCHEMA if key.alias})
# `--canary` predates the derived `--canary-backend` and means the same thing;
# declared here rather than as an `alias` because it collides with its own
# section name, which the derived-flag rule would otherwise produce twice.
_BY_FLAG["--canary"] = _BY_PATH["canary.backend"]


def key_for(path: str) -> Key:
    """The schema entry named ``section.name``. Raises on an unknown path."""
    try:
        return _BY_PATH[path]
    except KeyError:
        raise KeyError(f"unknown config key {path!r}") from None


# --------------------------------------------------------------------------
# Parsing a raw value from any layer
# --------------------------------------------------------------------------

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def label_for(key: Key, origin: Source) -> str:
    """How to name *key* in an error, given which layer supplied the value.

    An operator who typed ``SIS_BUDGET_USD=0.1O`` needs to be told about
    ``SIS_BUDGET_USD`` — naming the internal dotted path would send them looking
    in the wrong place for something they never set.
    """
    if origin is Source.ENV:
        return key.env
    if origin is Source.CLI:
        return key.flag
    if origin is Source.FILE:
        return f"{CONFIG_FILE.name}: {key.section}.{key.yaml_key}"
    return key.path


def _parse_bool(label: str, raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    # Loud, not lenient. The old `== "1"` test made SIS_ALLOW_STRICT_CHANGES=yes
    # mean "disabled", which is the safe direction but an invisible one: the
    # operator believes they enabled something and nothing says otherwise.
    raise ValueError(
        f"{label}={raw!r} is not a boolean "
        f"(use one of {sorted(_TRUE)} / {sorted(_FALSE)})"
    )


def _parse_number(key: Key, label: str, raw: object) -> float | int:
    cast: Any = int if key.kind in (Kind.INT, Kind.OPT_INT) else float
    if isinstance(raw, bool):  # bool is an int subclass; 'true' is not a budget
        raise ValueError(f"{label}={raw!r} is not a number")
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{label}={raw!r} is not a valid number") from None
    # A mistyped spend cap must never quietly become the permissive default —
    # the reason this validation exists at all (KNOWN_ISSUES.md M5).
    if value < 0:
        raise ValueError(f"{label}={raw!r} must not be negative")
    if key.positive and value == 0:
        raise ValueError(f"{label}={raw!r} must be positive")
    number: float | int = value
    return number


def _parse_str_list(raw: object) -> tuple[str, ...]:
    parts: list[str]
    if isinstance(raw, list | tuple):
        parts = [str(item) for item in raw]
    else:
        parts = str(raw).split(",")
    return tuple(
        # removeprefix, not lstrip("./") — lstrip strips `.`/`/` *characters*,
        # mangling ".github/x" → "github/x" and "../x" → "x".
        part.strip().replace("\\", "/").removeprefix("./")
        for part in parts
        if part.strip()
    )


def parse_value(key: Key, raw: object, origin: Source = Source.DEFAULT) -> Any:
    """Coerce *raw* — a YAML scalar, an env string, or a CLI string — to *key*'s type.

    Raises :class:`ValueError` naming the layer *origin* came from, so a bad
    value points at the thing the operator actually has to fix rather than
    surfacing three frames later as a confusing ``TypeError``.
    """
    label = label_for(key, origin)
    optional = key.kind in (Kind.OPT_STR, Kind.OPT_INT)
    # `null` in YAML and an empty string in the env/CLI layers both mean "not
    # set" for an optional key — and must short-circuit before any type or
    # choices check, since "unset" is not one of the choices.
    if optional and (raw is None or (isinstance(raw, str) and raw == "")):
        return None
    if raw is None:
        raise ValueError(f"{label} has no value (null), and is not optional")

    if key.kind is Kind.BOOL:
        return _parse_bool(label, raw)
    if key.kind in (Kind.INT, Kind.OPT_INT, Kind.FLOAT):
        return _parse_number(key, label, raw)
    if key.kind is Kind.STR_LIST:
        return _parse_str_list(raw)

    text = str(raw)
    if key.choices is not None and text not in key.choices:
        raise ValueError(f"{label}={raw!r} is not one of {list(key.choices)}")
    return text


# --------------------------------------------------------------------------
# The file layer
# --------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # lazy: an optional dep (`poetry install --with real`)
    except ModuleNotFoundError:
        # YAML is a superset of JSON, so a JSON-shaped config still loads
        # without the dependency. Same fallback as sis.settings.
        try:
            data: Any = json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError(
                f"{path} needs PyYAML to parse (poetry install --with real), "
                "or must be written as JSON"
            ) from None
    else:
        data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a top-level mapping of sections")
    return dict(data)


def load_file(path: Path | None = None) -> dict[str, Any]:
    """Parse a config file into ``{"section.name": raw_value}``.

    Validates as it goes, and every failure is loud:

    - an unknown section or key — a typo'd ``forbidden_budget_used`` that is
      silently ignored is a spend cap that silently does not apply;
    - a **wrong tier prefix** — writing ``soft_budget_usd`` is an attempt to
      relabel how strongly a key is protected, and it must not be possible to
      do that by editing the file the key lives in.
    """
    resolved = CONFIG_FILE if path is None else path
    if not resolved.exists():
        return {}
    raw = _read_yaml(resolved)

    by_yaml_key = {(k.section, k.yaml_key): k for k in SCHEMA}
    known_names = {(k.section, k.name): k for k in SCHEMA}
    out: dict[str, Any] = {}
    for section, body in raw.items():
        if section not in SECTIONS:
            raise ValueError(
                f"{resolved}: unknown section {section!r} (known: {list(SECTIONS)})"
            )
        if not isinstance(body, dict):
            raise ValueError(f"{resolved}: section {section!r} must be a mapping")
        for yaml_key, value in body.items():
            key = by_yaml_key.get((str(section), str(yaml_key)))
            if key is None:
                _reject_unknown_key(resolved, str(section), str(yaml_key), known_names)
            else:
                out[key.path] = parse_value(key, value, Source.FILE)
    return out


def _reject_unknown_key(
    path: Path, section: str, yaml_key: str, known: Mapping[tuple[str, str], Key]
) -> None:
    """Explain *why* a key is unknown: a typo, or a tier that was changed."""
    for tier in ConfigTier:
        stripped = yaml_key.removeprefix(f"{tier.value}_")
        if stripped == yaml_key:
            continue
        declared = known.get((section, stripped))
        if declared is not None:
            raise ValueError(
                f"{path}: {section}.{yaml_key} has the wrong tier prefix — "
                f"{stripped!r} is declared {declared.tier.value!r}, so it must be "
                f"written {declared.yaml_key!r}. A key's protection level is set "
                "in sis/config.py, not by how it is spelled here."
            )
    expected = sorted(k.yaml_key for (sec, _), k in known.items() if sec == section)
    raise ValueError(
        f"{path}: unknown key {section}.{yaml_key} (known in {section}: {expected})"
    )


_FILE_CACHE: dict[str, Any] | None = None


def file_layer() -> Mapping[str, Any]:
    """The parsed ``config.yml``, read once per process.

    Cached because a running loop must not change behaviour half-way through
    because someone saved the file — operator edits land on restart. Tests and
    tools that need a re-read call :func:`reset_config_cache`.
    """
    global _FILE_CACHE
    if _FILE_CACHE is None:
        _FILE_CACHE = load_file()
    return _FILE_CACHE


def reset_config_cache() -> None:
    """Drop the cached file layer — for tests and tools that rewrite the file."""
    global _FILE_CACHE
    _FILE_CACHE = None


# --------------------------------------------------------------------------
# The CLI layer
# --------------------------------------------------------------------------

_CLI_OVERRIDES: dict[str, Any] = {}


def parse_cli(argv: list[str]) -> dict[str, Any]:
    """Extract ``--section-name value`` overrides from *argv*.

    Every schema key gets a flag for free, plus the two aliases that predate
    this module (``--contract``, ``--canary``). Unknown arguments are left
    alone: the caller owns mode flags like ``--loop``.

    A flag with nothing after it raises rather than swallowing the next flag as
    its value, which is how ``main.py``'s hand-rolled parser behaved and is
    worth keeping.
    """
    out: dict[str, Any] = {}
    index = 0
    while index < len(argv):
        key = _BY_FLAG.get(argv[index])
        if key is None:
            index += 1
            continue
        if index + 1 >= len(argv):
            raise SystemExit(f"{argv[index]} needs a value")
        out[key.path] = parse_value(key, argv[index + 1], Source.CLI)
        index += 2
    return out


def apply_cli_overrides(overrides: Mapping[str, Any]) -> None:
    """Install CLI overrides for this process **and for actors created after this**.

    Call before :func:`sis.org.bootstrap`. The role actors are detached Ray
    actors in their own OS processes: they snapshot the driver's environment at
    creation and never see a later change, so an override kept only in this
    module's memory would configure the driver and silently leave the actor that
    runs the gauntlet on the old value. Writing through to ``os.environ`` is
    what makes ``--sandbox-mode docker`` mean what it says.

    The in-process overlay is kept as well, so :func:`effective` can still
    report ``cli`` rather than ``env`` as the source.
    """
    for path, value in overrides.items():
        key = key_for(path)
        _CLI_OVERRIDES[path] = value
        os.environ[key.env] = _to_env_string(value)


def _to_env_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple):
        return ",".join(str(item) for item in value)
    return "" if value is None else str(value)


def clear_cli_overrides() -> None:
    """Drop the CLI overlay (not the environment) — for tests."""
    _CLI_OVERRIDES.clear()


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolved:
    key: Key
    value: Any
    source: Source


def resolve(key: Key, *, env: Mapping[str, str] | None = None) -> Resolved:
    """Apply the precedence chain to one key: CLI > env > file > default."""
    if key.path in _CLI_OVERRIDES:
        return Resolved(key, _CLI_OVERRIDES[key.path], Source.CLI)

    environ = os.environ if env is None else env
    raw = environ.get(key.env)
    if raw is not None and raw != "":
        return Resolved(key, parse_value(key, raw, Source.ENV), Source.ENV)

    layer = file_layer()
    if key.path in layer:
        return Resolved(key, layer[key.path], Source.FILE)

    return Resolved(key, key.default, Source.DEFAULT)


def get(path: str, *, env: Mapping[str, str] | None = None) -> Any:
    """The effective value of one key, by dotted path."""
    return resolve(key_for(path), env=env).value


def effective(env: Mapping[str, str] | None = None) -> list[Resolved]:
    """Every key with its value **and where that value came from**.

    The model an operator UI renders: the tier says whether a key may be
    edited, the source says whether editing ``config.yml`` would even take
    effect — a key currently supplied by an environment variable will keep
    winning until that variable goes away.
    """
    return [resolve(key, env=env) for key in SCHEMA]


# --------------------------------------------------------------------------
# Typed views
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BrakesConfig:
    budget_usd: float
    breaker_threshold: int
    max_cost_per_accepted_usd: float
    slo_min_spend_usd: float


@dataclass(frozen=True)
class SandboxConfig:
    mode: str
    image: str
    memory: str
    cpus: str
    timeout_seconds: float
    allow_unsandboxed_llm: bool


@dataclass(frozen=True)
class PolicyConfig:
    target_paths: tuple[str, ...]
    allow_strict_changes: bool


@dataclass(frozen=True)
class EpisodicConfig:
    store: str


@dataclass(frozen=True)
class AdaptersConfig:
    mode: str
    env: str
    secrets_file: str
    aws_region: str
    aws_secret_id: str | None
    http_timeout_seconds: float


@dataclass(frozen=True)
class ProposerConfig:
    backend: str
    llm_provider: str
    llm_model: str | None


@dataclass(frozen=True)
class CanaryConfig:
    backend: str | None


@dataclass(frozen=True)
class LoopConfig:
    interval_seconds: float
    max_cycles: int | None


@dataclass(frozen=True)
class ContractsConfig:
    default: str | None


@dataclass(frozen=True)
class Config:
    """The whole effective configuration, frozen."""

    brakes: BrakesConfig
    sandbox: SandboxConfig
    policy: PolicyConfig
    episodic: EpisodicConfig
    adapters: AdaptersConfig
    proposer: ProposerConfig
    canary: CanaryConfig
    loop: LoopConfig
    contracts: ContractsConfig


def _section(name: str, env: Mapping[str, str] | None) -> dict[str, Any]:
    return {
        key.name: resolve(key, env=env).value
        for key in SCHEMA
        if key.section == name
    }


def config(*, env: Mapping[str, str] | None = None) -> Config:
    """Build the typed configuration by resolving every key.

    Not cached: resolution is a handful of dict lookups over an already-parsed
    file layer, and reading the environment at the point of use is exactly what
    the ~27 ``os.getenv`` calls this replaced did. Keeping that means a test
    that sets an environment variable still sees it, and the engine gains one
    schema without gaining a new lifecycle to reason about. The *file* layer is
    the one that is frozen (see :func:`file_layer`).
    """
    return Config(
        brakes=BrakesConfig(**_section("brakes", env)),
        sandbox=SandboxConfig(**_section("sandbox", env)),
        policy=PolicyConfig(**_section("policy", env)),
        episodic=EpisodicConfig(**_section("episodic", env)),
        adapters=AdaptersConfig(**_section("adapters", env)),
        proposer=ProposerConfig(**_section("proposer", env)),
        canary=CanaryConfig(**_section("canary", env)),
        loop=LoopConfig(**_section("loop", env)),
        contracts=ContractsConfig(**_section("contracts", env)),
    )


# --------------------------------------------------------------------------
# Rendering the committed file
# --------------------------------------------------------------------------


def _render_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, list | tuple):
        return "[" + ", ".join(f'"{item}"' for item in value) + "]"
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)


def _wrap(text: str, width: int, prefix: str) -> Iterator[str]:
    words = text.split()
    line = ""
    for word in words:
        candidate = word if not line else f"{line} {word}"
        if line and len(prefix) + len(candidate) > width:
            yield prefix + line
            line = word
        else:
            line = candidate
    if line:
        yield prefix + line


def render_config_file() -> str:
    """Render ``config.yml`` from the schema — defaults, tiers, and docs.

    The committed file is generated rather than hand-maintained so it cannot
    drift from the code: ``tests/test_config.py`` regenerates it and compares.
    """
    lines = [
        "# config.yml — every knob the engine has, with its default.",
        "#",
        "# GENERATED FROM sis/config.py. Do not hand-edit the structure: run",
        "#     poetry run python -m sis.config --write",
        "# after changing SCHEMA. Values may be edited; keys and comments may not.",
        "#",
        "# Precedence: CLI flag > environment variable > this file > built-in default.",
        "#",
        "# The prefix on each key is its protection tier, and it governs what a",
        "# *human* may change through the operator UI — forbidden_ is not editable",
        "# there at all, strict_ needs an explicit confirmation, soft_ is free. The",
        "# self-improvement loop cannot write this file at any tier: it is",
        "# POLICY-FORBIDDEN (sis/policy.py), like the gauntlet and the contracts.",
        "#",
        "# Secrets do NOT belong here — this file is committed. See secrets.example.yml.",
    ]
    for section in SECTIONS:
        lines.append("")
        lines.append(f"{section}:")
        for key in SCHEMA:
            if key.section != section:
                continue
            lines.extend(_wrap(key.doc, 88, "  # "))
            lines.append(f"  # env: {key.env}   flag: {key.flag}")
            lines.append(f"  {key.yaml_key}: {_render_scalar(key.default)}")
    return "\n".join(lines) + "\n"


def _main() -> None:
    import sys

    if "--write" in sys.argv:
        CONFIG_FILE.write_text(render_config_file(), encoding="utf-8")
        print(f"wrote {CONFIG_FILE}")
        return
    for item in effective():
        print(f"{item.key.path:<38} {str(item.value):<24} "
              f"[{item.key.tier.value}, from {item.source.value}]")


if __name__ == "__main__":
    _main()
