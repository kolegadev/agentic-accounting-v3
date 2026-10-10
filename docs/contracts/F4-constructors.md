# CONTRACT F4 — Deterministic Entry Constructors (facts → DR/CR legs)

## Goal

One deterministic Python constructor per catalog skill. The LLM's only jobs
are classification and fact extraction; entry construction — account legs,
VAT arithmetic, verification — is pure code, generated FROM the SKILL.md
files, and proven against the SKILL.md worked examples.

## The contract that makes this work

Every constructor takes a pydantic facts model (derived 1:1 from the skill's
`## Facts` table) and returns a `ConstructedEntry`:

```python
@dataclass(frozen=True)
class Leg:
    account: str          # account CODE resolved from the company COA
    account_class: str    # the class from the skill's DR/CR table (audit trail)
    debit_amount: int     # pence, 0 for credit legs
    credit_amount: int    # pence, 0 for debit legs
    description: str

@dataclass(frozen=True)
class ConstructedEntry:
    skill_id: str
    legs: list[Leg]                     # exactly one of debit/credit > 0 per leg
    formance_postings: list[dict]       # via build_formance_postings (F1)
    derived: dict[str, int]             # vat_pence, gross_pence, ... derived values
    checks: list[str]                   # verification checks executed + results
```

- `formance_postings` MUST be produced by calling the REAL
  `build_formance_postings` from `src/services/transaction_service.py`
  (postings with account_id→code mapping; use deterministic fake UUIDs
  keyed by code) — never a reimplementation.
- Verification checks come from each skill's `## Verification checks`
  section: sum(debits) == sum(credits) > 0, VAT arithmetic exact
  (net + vat == gross, vat == net//5 for 20%, net//20 for 5%), class rules.
  On failure raise `ConstructorError` (never emit an unbalanced entry).
- Account resolution: constructors take `codes: Mapping[account_class, str]`
  (a code resolver) so they stay pure; a helper `resolve_from_coa(coa_accounts)`
  builds the mapping from the company's real accounts (COA templates define
  the canonical classes). Unknown class → `ConstructorError`.

## Deliverables

1. `api/src/constructors/__init__.py` — `Leg`, `ConstructedEntry`,
   `ConstructorError`, the `codes` resolver helper, and the registry mapping
   `skill_id -> constructor function`.
2. `api/src/constructors/facts.py` — one pydantic facts model per skill
   (field names/types from each SKILL.md `## Facts` table; money in pence;
   vat_rate enum `("20%","5%","0%","exempt")`).
3. `api/src/constructors/entry_<skill-id>.py` (or one module with all 10) —
   the 10 constructors.
4. Tests:
   - `api/tests/unit/test_constructors.py` — for EVERY skill: build facts
     from its own SKILL.md worked example (parse the dr-cr block + Facts
     example) and assert `constructor(facts, codes)` legs match the worked
     example exactly (codes, amounts, directions). This binds code to
     catalog: any future SKILL.md edit that changes the example without
     changing the constructor fails CI.
   - Property tests: for each skill, 200 randomized valid facts →
     always balanced, VAT arithmetic holds, legs reference known classes,
     `formance_postings` non-empty and consistent with legs.
   - Error tests: unbalanced/unrepresentable facts (unknown vat_rate,
     unknown class, zero amount) raise ConstructorError.
5. Full suite green:
   `cd api && .venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --timeout=30`
   → 0 failed, 0 errors, count ≥ 888.

## Boundaries

- NO changes to existing services, models, validators, routes, gateway,
  or the F1/F2/F3 artifacts. Additive only: `api/src/constructors/**` +
  `api/tests/unit/test_constructors.py` + this contract.
- The constructors are library code — nothing calls them yet (the
  orchestrator wiring is a later feature).
