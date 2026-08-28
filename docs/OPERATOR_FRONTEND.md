# Operator frontend (OMNI-28) — design note

**Status:** stack, auth and TLS decided (2026-08-16); first implementation slice
in PR #93 — the schema keys, the tier-gated write path and the Panel app. The
second slice (2026-08-28) closed the three parts of the ticket the first left
out: the CEO brake panel, the episodic-history panel, and the justification a
`strict_` edit now has to carry. Deployment artifacts (`Dockerfile.frontend`,
`Caddyfile`) follow separately.
See [OMNI-28](https://olafzumpe.atlassian.net/browse/OMNI-28). **Depends on OMNI-27**
(PR #92, merged 2026-08-16): `sis/config.py`'s schema, the committed `config.yml`, and
`sis.config.effective()` (already exercised by `main.py --show-config`) are what
this UI renders and edits.

## What it needs to do

View system state; edit config gated by each key's `ConfigTier`
(`sis/config.py`): `forbidden_` keys are not editable in the UI at all,
`strict_` keys are editable behind a justified confirmation, `soft_` keys are
editable freely.

**The gate lives in the write path, not in the UI.** `sis/operator.py` refuses
a `forbidden_` edit, and an unjustified `strict_` edit, no matter what the
browser sends — a widget that is merely disabled is not a gate, it is a
suggestion. The Panel layer renders that same tier information, but nothing
depends on it doing so.

## What the read side shows

Four independent reads, rendered as four sections that each degrade on their
own. That independence is the design point: this console is most useful when
something is already broken, and a single shared failure path would blank the
whole page at exactly the wrong moment. A cluster with no CEO still shows its
episodic history; an unreadable episodic store still shows the brakes.

| Section | Source | Needs a cluster? |
| --- | --- | --- |
| System state | `SelfModel.snapshot()` — registry, deploy slots, provenance, substrate | yes |
| Brakes | `CEO.economics()` + `CEO.state_snapshot()` | yes |
| Episodic history | `EpisodicStore.summary()` | **no** — reads the store on disk |
| Configuration | `config.effective()` | no |

Two details that are decisions rather than plumbing:

- **The brakes are read from the live actor, not recomputed from this
  console's config.** They can legitimately disagree: the CEO snapshots its
  thresholds when it is created, so a console started later against an edited
  `config.yml` would otherwise display a budget that nothing is enforcing.
  Showing an unenforced number next to real spend is the worst of both.
- **`cost_per_accepted_usd` is `-1.0` when nothing has been accepted yet**,
  because the CEO cannot put a JSON `inf` on the wire. Rendered verbatim that
  reads as *earning* a dollar per improvement, so the formatter special-cases
  it. Pinned by a test.

The episodic section leads with the `rejected_by_gate` breakdown, since which
gate is doing the rejecting is the operationally interesting half. An empty
breakdown is stated rather than omitted — "nothing was rejected" and "we did
not record why" must not look identical.

## A strict_ edit has to say why

The confirmation is an `Approval` (`sis/operator.py`), not a boolean:
`Justification.HUMAN_REQUEST` from `sis/policy.py` plus a written note of at
least `MIN_JUSTIFICATION_CHARS`. Reusing the policy's enum is deliberate — the
loop's "a human asked for this" and the console's are the same claim about the
same system, and letting one be an enum while the other is a bare boolean is
how two subtly different meanings of "approved" come to exist.

A `strict_` key is by definition one whose edit removes or weakens a check, so
the interesting part was always *why*, and a checkbox cannot carry that. The
confirmation and the reason stay two separate claims: prose without a ticked
box is refused, and a ticked box without prose is refused.

The threshold is deliberately low and deliberately not zero. It exists to stop
a single keystroke standing in for a reason, not to judge whether the reason is
a good one — no rule can do that, and one that pretended to would only teach
operators to pad. For the same reason `soft_` keys demand nothing at all: a
console that wanted a written justification to change a poll interval would
train people to type "x", which is how the requirement stops meaning anything
where it does matter.

## Every committed change is recorded

`save_edits()` appends to `runtime/operator_audit.jsonl` (gitignored, local
operational state): timestamp, key, tier, before/after, and the justification.
Three choices worth noting:

- **The write path does the recording, not the UI** — so the record is a
  property of saving, not of the caller remembering to log it.
- **`soft_` edits are recorded too.** A log holding only `strict_` edits could
  not answer "what changed on this box last week", which is the question it
  will actually be asked.
- **`before`/`after` are the *file* layer's values**, which is what the save
  altered. The effective value can still differ from both when a `SIS_*`
  variable shadows the key; recording the file layer keeps the log a true
  statement about the write rather than a guess about the next process to read
  it.

A refused edit is never audited — the log records what happened, and a refusal
changed nothing. Reads skip corrupt lines rather than raising, so a crash
mid-append costs one entry and not the whole history.

Separately, the loop's code-generation path never reaches `config.yml` at any
tier, since both the file and `sis/config.py` are POLICY-FORBIDDEN
(`sis/policy.py`). The tier prefix is a statement about **human** authority
through this UI; it grants the loop nothing either way.

Edits take effect on restart: `config.yml` is parsed once per process and
cached, so this UI cannot change a running loop's behaviour mid-cycle, by
design.

## Where an edit is written

Straight into `config.yml`, the same committed file. The alternative — a
gitignored `config.local.yml` overlay — was rejected because it contradicts the
reason that file is committed in the first place: a changed brake or sandbox
mode should arrive as a reviewable diff rather than as invisible local state.

That required loosening one guarantee. `render_config_file()` previously
emitted `key.default` for every key and a test asserted the committed file was
byte-identical to that render, which made `config.yml` a generated artifact
that could not hold a value at all. It now takes the current values and renders
those, and the drift test re-renders *from the file's own values* — so it still
catches an added or removed key, a stale doc comment, a wrong tier prefix, or
reordering, while permitting a value to differ from its default.

What replaced the old defaults check is stricter where it matters:
**no `forbidden_` key may differ from its built-in default in the committed
file.** Deleting `config.yml` therefore can never weaken a guardrail — the
spend cap, the sandbox mode, the policy target list and the episodic backend
all still fall back to exactly what shipped. Per-run experiments on those knobs
belong in the environment layer, which is what `sis/config.py` already
recommends. A deliberate change to a guardrail default is a change to `SCHEMA`,
reviewed as code, not a quiet value edit in a generated file.

## Stack: Panel

Chosen over Streamlit for one reason: built-in OAuth. `panel serve` accepts
`--oauth-provider` / `--oauth-key` / `--oauth-secret` and handles the redirect
flow itself (GitHub, Google, Azure AD, Okta) — no separate auth library to
integrate. Otherwise it's the same reactive Python-dashboard model as
Streamlit, so the widget/layout side of the decision doesn't change.

## Auth: GitHub OAuth

Single-operator tool, so identity reduces to "is this GitHub account on a
short allowlist" — checked once per session, not per key. No password store to
secure or rotate; MFA is inherited from GitHub's side.

Three rules make that allowlist meaningful rather than decorative, all enforced
in `sis/operator.py` and unit-tested:

- **The allowlist is a `forbidden_` config key.** `frontend.allowed_logins`
  cannot be edited through the UI it governs, because an operator who could
  append a login to it could grant access to anyone — privilege escalation
  through the tool's own front door. Same for `frontend.auth` and
  `frontend.bind`: turning off authentication, or moving what interface the app
  answers on, must not be reachable from inside an authenticated session.
- **An empty allowlist denies everyone.** Failing closed matters more than
  convenience here: an allowlist that defaults to "anyone" is worse than no
  allowlist, because it looks like a control.
- **`auth: none` is refused on a non-loopback bind.** Local development needs
  to run without registering an OAuth app, but "no auth" plus a public
  interface is the one combination that must never start. This is the same
  shape as the existing M1 rule that a real proposer requires the docker
  sandbox: refuse the unsafe *combination* rather than trusting the operator to
  avoid it.

The OAuth client secret is a credential, so it lives in `secrets.local.yml`
behind `sis/settings.py` (AWS Secrets Manager when `SIS_ENV=aws`) — never in
`config.yml`, which is committed.

## Deployment: ports

- **8080 inside the container** — Panel listens here, plain HTTP. Never
  published to the host directly.
- **443 externally** — the only port reachable from outside the container.

Docker's port mapping alone (`443:8080`) does **not** add TLS — it's just a
socket redirect. Something has to terminate TLS before traffic reaches Panel.

**Decided: Caddy, in front of the container.** It obtains *and renews* Let's
Encrypt certificates itself, so there is no certbot, no renewal timer and no
reload hook to maintain:

```
ops.example.com {
    reverse_proxy panel:8080
}
```

Chosen over nginx + certbot for two reasons. The first is that the nginx
equivalent is a server block, a certbot install, a renewal timer and a reload
hook for an identical outcome. The second is Panel-specific and is the one that
actually bites: **Panel is Bokeh underneath and holds a websocket** for its
reactive updates. nginx will not proxy that unless the `Upgrade`/`Connection`
headers are set explicitly, and the failure mode is a page that loads correctly
and then silently never updates. Caddy's `reverse_proxy` handles websockets
without configuration. Either way Panel needs `--allow-websocket-origin` set to
the public hostname or it rejects the proxied connection.

Two constraints on any ACME-based approach, worth knowing before building
around it:

- **Let's Encrypt will not issue for a bare IP.** A throwaway one-node AWS box
  reachable only by public IP cannot use it; that needs a real DNS name.
- **Locally you need none of this.** GitHub exempts `localhost` from the HTTPS
  redirect-URI rule, so `panel serve --port 8080` with a callback to
  `http://localhost:8080/...` works as-is. TLS is a deployment concern, not an
  inner-loop one.

If this is ever fronted by real AWS infrastructure, ALB + ACM is the cleaner
answer — auto-renewing certs, no ACME challenge on the box, no port 80 exposure
— at roughly $16–20/month for an idle load balancer.

## HTTPS is required, not optional

Two independent reasons, not just convention:

- **The OAuth provider requires it.** GitHub rejects or warns on non-HTTPS
  redirect URIs for anything other than `localhost`, and RFC 6749 requires TLS
  for the authorization/token endpoints in general.
- **The session cookie needs `Secure`.** Panel's OAuth flow sets a session
  cookie after login; over plain HTTP that cookie — and the OAuth `state`/code
  exchange — travels in the clear, which defeats the point of gating this UI
  with OAuth at all.

So plain HTTP is not a fallback mode: the container has no business being
reachable on 443 without TLS terminating somewhere before the browser.

## A shadowed key is shown as shadowed

`config.yml` is the *third* layer of four: CLI flag > env var > file > default.
So an operator can edit a key in this UI, save it, restart, and see no change
at all — because a `SIS_*` variable in the environment still outranks the file.

The UI therefore renders `sis.config.effective()`'s `Source` next to every key
and marks the ones currently supplied by `env` or `cli` as shadowed, saying so
at the point of editing rather than leaving the operator to discover it. This
is the single most likely way for this tool to lie to someone, and it is a
display problem, not a precedence problem — the precedence is correct.

## Open questions

- Where the GitHub OAuth app is registered, and how its client secret reaches
  a deployed container.
- Whether this is exposed publicly at all. A tunnel or Tailscale, with the app
  bound to loopback, is a stronger posture than any TLS configuration for a
  console that edits the settings of a system which executes generated code —
  and Tailscale supplies an auto-renewing certificate on a real name, which
  removes the certificate problem rather than solving it. Caddy is the answer
  *if* it must be publicly reachable.
- Deployment artifacts (`Dockerfile.frontend`, `Caddyfile`) — deliberately not
  in the first PR, since neither can be meaningfully tested without a host and
  a DNS name.
