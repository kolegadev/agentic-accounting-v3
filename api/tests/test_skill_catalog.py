"""Validation tests for the transaction-type skill catalog (Contract F3).

Implements the six contract checks:
1. 10 transaction skills present; registry complete and consistent
   (bidirectional; report entries under Contract F8 are excluded).
2. Every SKILL.md has all 10 required sections (frontmatter + 9 headings).
3. Frontmatter parses and constraints hold (id matches directory,
   frameworks valid, status v0).
4. Worked examples balance: ``dr-cr`` fenced blocks parsed and asserted
   equal in integer pence.
5. DR/CR construction tables parse and each leg's account class appears in
   the UK COA template categories/types.
6. Every skill's Formance section contains posting dicts with
   source/destination/amount/asset keys and no ``world``.

Plus validation-gap checks (G1-G3):
7. DR/CR table leg directions agree with the worked examples' ``dr-cr``
   legs for the same account class (COA-derived, category/type level).
8. Formance posting fences equal the REAL ``build_formance_postings``
   output for each worked-example ``dr-cr`` block — exact equality of
   source/destination codes AND decimal-string amounts (covers G3).

Plus an accounting ground-truth check the F4 constructors depend on.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.services.transaction_service import build_formance_postings

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_DIR = REPO_ROOT / "skills" / "catalog"
TRANSACTIONS_DIR = CATALOG_DIR / "transactions"
COA_DIR = REPO_ROOT / "api" / "src" / "coa_templates"

REQUIRED_SKILL_IDS = {
    "supplier-invoice",
    "supplier-payment",
    "sales-invoice",
    "customer-receipt",
    "bank-transfer",
    "journal-accrual",
    "prepayment",
    "depreciation",
    "payroll",
    "opening-balances",
}

REQUIRED_SECTIONS = [
    "When to use",
    "Facts",
    "DR/CR construction",
    "VAT treatment",
    "Formance interaction",
    "Worked example",
    "Verification checks",
    "Edge cases",
    "Framework variants",
]

VALID_FRAMEWORKS = {"uk-gaap", "ifrs", "us-gaap"}

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
SECTION_RE = re.compile(r"^## (.+?)\s*$", re.MULTILINE)
DRCR_FENCE_RE = re.compile(
    r"^```dr-cr\s*$(.*?)^```\s*$", re.MULTILINE | re.DOTALL
)
JSON_FENCE_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)
CLASS_CELL_RE = re.compile(r"^([A-Za-z]+)(?:\s*\(([^)]+)\))?$")
AMOUNT_RE = re.compile(r"^\d+\.\d{2}$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _skill_dir(skill_id: str) -> Path:
    return TRANSACTIONS_DIR / skill_id


def _read_skill(skill_id: str) -> str:
    path = _skill_dir(skill_id) / "SKILL.md"
    return path.read_text(encoding="utf-8")


def _registry() -> list[dict]:
    raw = yaml.safe_load((CATALOG_DIR / "registry.yaml").read_text())
    assert isinstance(raw, list), "registry must be a YAML list of entries"
    return raw


def _frontmatter(text: str) -> dict:
    m = FRONTMATTER_RE.match(text)
    assert m is not None, "missing YAML frontmatter delimited by --- lines"
    return yaml.safe_load(m.group(1))


def _section_text(text: str, heading: str) -> str:
    m = re.search(
        rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert m is not None, f"missing required section '## {heading}'"
    return m.group(1)


def _parse_drcr_blocks(text: str) -> list[list[tuple[str, str, int]]]:
    """Parse ``dr-cr`` fenced blocks into [(dir, code, pence), ...] legs."""
    blocks: list[list[tuple[str, str, int]]] = []
    for m in DRCR_FENCE_RE.finditer(text):
        legs: list[tuple[str, str, int]] = []
        for line in m.group(1).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            tokens = line.split()
            assert tokens[0] in ("Dr", "Cr"), f"bad dr-cr leg: {line!r}"
            assert re.fullmatch(r"\d{4}", tokens[1]), f"bad account code: {line!r}"
            amount = int(tokens[-1])
            legs.append((tokens[0], tokens[1], amount))
        assert legs, "empty dr-cr fenced block"
        blocks.append(legs)
    return blocks


def _coa_classes() -> tuple[set[str], set[str]]:
    categories: set[str] = set()
    types: set[str] = set()
    for path in COA_DIR.glob("*.json"):
        for account in json.loads(path.read_text(encoding="utf-8")):
            categories.add(account["category"])
            types.add(account["type"])
    return categories, types


def _drcr_table_rows(text: str) -> list[list[str]]:
    """Rows of the markdown tables inside the DR/CR construction section."""
    section = _section_text(text, "DR/CR construction")
    rows: list[list[str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if cells[0] in ("leg", "") or set(cells[0]) <= {"-"}:
            continue
        rows.append(cells)
    return rows


def _formance_fences(text: str) -> list[list[dict]]:
    """Per-fence lists of posting dicts in the Formance interaction section."""
    section = _section_text(text, "Formance interaction")
    fences: list[list[dict]] = []
    for m in JSON_FENCE_RE.finditer(section):
        parsed = json.loads(m.group(1))
        if isinstance(parsed, dict):
            parsed = [parsed]
        assert isinstance(parsed, list), "Formance block must be a list or dict"
        fences.append(parsed)
    return fences


def _formance_dicts(text: str) -> list[dict]:
    return [posting for fence in _formance_fences(text) for posting in fence]


def _code_to_class() -> dict[str, tuple[str, str]]:
    """Union of COA templates: 4-digit code -> (category, type).

    Client-created accounts (e.g. payroll's ``2041`` Pension Payable) are
    absent from every template and simply do not appear here.
    """
    mapping: dict[str, tuple[str, str]] = {}
    for path in COA_DIR.glob("*.json"):
        for account in json.loads(path.read_text(encoding="utf-8")):
            mapping.setdefault(account["code"], (account["category"], account["type"]))
    return mapping


# ---------------------------------------------------------------------------
# Check 1: registry completeness and bidirectionality
# ---------------------------------------------------------------------------


def test_registry_is_complete_and_bidirectional() -> None:
    registry = [e for e in _registry() if e.get("category") == "transaction"]
    assert len(registry) == 10, "catalog must contain exactly 10 transaction skills"

    reg_ids = [entry["id"] for entry in registry]
    assert len(set(reg_ids)) == 10, "registry ids must be unique"
    assert set(reg_ids) == REQUIRED_SKILL_IDS, "registry ids mismatch contract"

    dir_ids = {p.parent.name for p in TRANSACTIONS_DIR.glob("*/SKILL.md")}
    assert dir_ids == REQUIRED_SKILL_IDS, (
        "every transaction directory must have a SKILL.md and vice versa"
    )

    for entry in registry:
        for key in ("id", "name", "category", "frameworks", "status", "path"):
            assert key in entry, f"registry entry {entry['id']} missing {key}"
        path = CATALOG_DIR / entry["path"]
        assert path.is_file(), f"registry path missing on disk: {entry['path']}"
        assert entry["path"] == f"transactions/{entry['id']}/SKILL.md"


# ---------------------------------------------------------------------------
# Check 2: all 10 required sections present
# ---------------------------------------------------------------------------


def test_all_skills_have_required_sections() -> None:
    for skill_id in REQUIRED_SKILL_IDS:
        text = _read_skill(skill_id)
        assert FRONTMATTER_RE.match(text), f"{skill_id}: frontmatter missing"
        headings = SECTION_RE.findall(text)
        for heading in REQUIRED_SECTIONS:
            assert heading in headings, f"{skill_id}: missing '## {heading}'"
        assert (
            len(set(headings)) == len(headings)
        ), f"{skill_id}: duplicate section headings"


# ---------------------------------------------------------------------------
# Check 3: frontmatter constraints
# ---------------------------------------------------------------------------


def test_frontmatter_constraints() -> None:
    for skill_id in REQUIRED_SKILL_IDS:
        fm = _frontmatter(_read_skill(skill_id))
        assert fm["id"] == skill_id, f"{skill_id}: id must match directory"
        assert fm.get("name") and isinstance(fm["name"], str)
        assert fm["category"] == "transaction"
        assert fm["frameworks"], f"{skill_id}: frameworks must be non-empty"
        assert set(fm["frameworks"]) <= VALID_FRAMEWORKS
        assert fm["version"] == "0.1.0"
        assert fm["status"] == "v0"

    registry = _registry()
    for entry in registry:
        if entry.get("category") != "transaction":
            # Report-type entries (Contract F8) live under reports/ and are
            # validated by test_report_catalog.py, not this F3 test.
            continue
        fm = _frontmatter(_read_skill(entry["id"]))
        assert entry["name"] == fm["name"], f"{entry['id']}: name mismatch"
        assert entry["frameworks"] == fm["frameworks"]
        assert entry["status"] == fm["status"]
        assert entry["category"] == fm["category"]


# ---------------------------------------------------------------------------
# Check 4: worked examples balance in pence
# ---------------------------------------------------------------------------


def test_worked_examples_balance() -> None:
    for skill_id in REQUIRED_SKILL_IDS:
        blocks = _parse_drcr_blocks(_read_skill(skill_id))
        assert blocks, f"{skill_id}: no dr-cr fenced block found"
        for i, legs in enumerate(blocks):
            debits = sum(amount for d, _, amount in legs if d == "Dr")
            credits = sum(amount for d, _, amount in legs if d == "Cr")
            assert debits == credits, (
                f"{skill_id}: dr-cr block {i} unbalanced "
                f"(debits {debits} != credits {credits}, pence)"
            )


# ---------------------------------------------------------------------------
# Check 5: DR/CR account classes match the UK COA templates
# ---------------------------------------------------------------------------


def test_drcr_account_classes_match_coa() -> None:
    categories, types = _coa_classes()
    assert {"Asset", "Liability", "Equity", "Revenue", "Expense"} <= categories

    for skill_id in REQUIRED_SKILL_IDS:
        rows = _drcr_table_rows(_read_skill(skill_id))
        assert len(rows) >= 2, f"{skill_id}: DR/CR table has fewer than 2 legs"
        dirs = {row[0] for row in rows}
        assert dirs <= {"Dr", "Cr"}, f"{skill_id}: bad leg column values"
        assert "Dr" in dirs and "Cr" in dirs, f"{skill_id}: needs Dr and Cr legs"
        for row in rows:
            assert len(row) >= 3, f"{skill_id}: malformed DR/CR row {row}"
            m = CLASS_CELL_RE.match(row[1])
            assert m is not None, (
                f"{skill_id}: account class cell {row[1]!r} must be "
                "'Category' or 'Category (Type)'"
            )
            category, acct_type = m.group(1), m.group(2)
            assert category in categories, (
                f"{skill_id}: class {category!r} not in COA categories"
            )
            if acct_type is not None:
                assert acct_type in types, (
                    f"{skill_id}: type {acct_type!r} not in COA types"
                )


# ---------------------------------------------------------------------------
# Check 6: Formance posting dicts present, well-formed, and world-free
# ---------------------------------------------------------------------------


def test_formance_postings_are_well_formed() -> None:
    for skill_id in REQUIRED_SKILL_IDS:
        dicts = _formance_dicts(_read_skill(skill_id))
        assert dicts, f"{skill_id}: no Formance posting dicts found"
        for posting in dicts:
            assert set(posting.keys()) == {"source", "destination", "amount", "asset"}, (
                f"{skill_id}: posting keys {sorted(posting.keys())} must be "
                "exactly source/destination/amount/asset"
            )
            assert posting["asset"] == "GBP/2"
            assert AMOUNT_RE.match(posting["amount"]), (
                f"{skill_id}: amount {posting['amount']!r} must be a decimal string"
            )
            for side in ("source", "destination"):
                value = posting[side]
                assert value and value.lower() != "world", (
                    f"{skill_id}: 'world' contra account forbidden ({side})"
                )
                assert re.fullmatch(r"\d{4}", value), (
                    f"{skill_id}: {side} {value!r} must be a 4-digit COA code"
                )


def test_no_skill_mentions_world_contra() -> None:
    for skill_id in REQUIRED_SKILL_IDS:
        text = _read_skill(skill_id)
        assert "world" not in text.lower(), (
            f"{skill_id}: must never reference the 'world' contra account"
        )


# ---------------------------------------------------------------------------
# Check 7 (G1): DR/CR table leg directions agree with the worked examples
# ---------------------------------------------------------------------------


def test_drcr_table_directions_match_worked_example() -> None:
    """Every dr-cr worked-example leg must match the DR/CR table's direction
    for the same account class (COA category/type, category at minimum)."""
    code_class = _code_to_class()
    for skill_id in REQUIRED_SKILL_IDS:
        text = _read_skill(skill_id)
        table_dirs: dict[tuple[str, str | None], set[str]] = {}
        for row in _drcr_table_rows(text):
            m = CLASS_CELL_RE.match(row[1])
            assert m is not None, f"{skill_id}: malformed DR/CR row {row}"
            key = (m.group(1), m.group(2))
            table_dirs.setdefault(key, set()).add(row[0])

        for block in _parse_drcr_blocks(text):
            for leg_dir, code, _ in block:
                cls = code_class.get(code)
                if cls is None:
                    # client-created account not in any COA template (e.g.
                    # payroll's 2041 Pension Payable) — nothing to compare.
                    continue
                dirs = table_dirs.get(cls)
                if dirs is None:
                    dirs = set().union(
                        *(v for k, v in table_dirs.items() if k[0] == cls[0])
                    )
                assert dirs, (
                    f"{skill_id}: no DR/CR table row for class {cls} "
                    f"(worked-example leg {leg_dir} {code})"
                )
                assert leg_dir in dirs, (
                    f"{skill_id}: leg {leg_dir} {code} ({cls}) contradicts "
                    f"the DR/CR table, which shows class {cls} only as "
                    f"{sorted(dirs)}"
                )


# ---------------------------------------------------------------------------
# Check 8 (G2+G3): Formance fences equal build_formance_postings output
# ---------------------------------------------------------------------------


def test_formance_postings_match_worked_example() -> None:
    """Each dr-cr block, fed through the REAL build_formance_postings,
    must equal its Formance-interaction fence exactly — source/destination
    codes AND decimal-string amounts (G3 is the amount half of this exact
    equality)."""
    for skill_id in REQUIRED_SKILL_IDS:
        text = _read_skill(skill_id)
        blocks = _parse_drcr_blocks(text)
        fences = _formance_fences(text)
        assert len(fences) == len(blocks), (
            f"{skill_id}: {len(blocks)} dr-cr blocks but {len(fences)} "
            "Formance posting fences — every worked example needs a fence"
        )
        for i, (block, expected) in enumerate(zip(blocks, fences)):
            ids = {
                code: uuid.uuid5(uuid.NAMESPACE_URL, f"{skill_id}:{code}")
                for _, code, _ in block
            }
            codes = {account_id: code for code, account_id in ids.items()}
            postings = [
                SimpleNamespace(
                    account_id=ids[code],
                    debit_amount=amount if direction == "Dr" else 0,
                    credit_amount=amount if direction == "Cr" else 0,
                )
                for direction, code, amount in block
            ]
            actual = build_formance_postings(
                postings, currency="GBP", codes=codes
            )
            assert actual == expected, (
                f"{skill_id}: dr-cr block {i} postings {actual} do not "
                f"match the Formance fence {expected}"
            )


# ---------------------------------------------------------------------------
# Accounting ground truth (spot-check the F4 constructor contract)
# ---------------------------------------------------------------------------


def _leg_counter(legs: list[tuple[str, str, int]], direction: str) -> Counter:
    return Counter(
        (code, amount) for d, code, amount in legs if d == direction
    )


def test_accounting_ground_truth_postings() -> None:
    dep = _parse_drcr_blocks(_read_skill("depreciation"))[0]
    assert _leg_counter(dep, "Dr") == Counter({("6500", 5000): 1})
    assert _leg_counter(dep, "Cr") == Counter({("1360", 5000): 1}), (
        "depreciation must Cr Accumulated Depreciation (1360), not the asset"
    )

    inv = _parse_drcr_blocks(_read_skill("supplier-invoice"))[0]
    assert _leg_counter(inv, "Dr") == Counter(
        {("5010", 50000): 1, ("2220", 10000): 1}
    ), "supplier invoice VAT: Dr expense net + Dr 2220 Input VAT"
    assert _leg_counter(inv, "Cr") == Counter({("2000", 60000): 1}), (
        "supplier invoice VAT: Cr Trade Creditors gross"
    )

    sale = _parse_drcr_blocks(_read_skill("sales-invoice"))[0]
    assert _leg_counter(sale, "Dr") == Counter({("1100", 60000): 1}), (
        "sales invoice VAT: Dr Trade Debtors gross"
    )
    assert _leg_counter(sale, "Cr") == Counter(
        {("4000", 50000): 1, ("2210", 10000): 1}
    ), "sales invoice VAT: Cr revenue net + Cr 2210 Output VAT"

    pay = _parse_drcr_blocks(_read_skill("supplier-payment"))[0]
    assert _leg_counter(pay, "Dr") == Counter({("2000", 60000): 1})
    assert _leg_counter(pay, "Cr") == Counter({("1000", 60000): 1})

    rec = _parse_drcr_blocks(_read_skill("customer-receipt"))[0]
    assert _leg_counter(rec, "Dr") == Counter({("1000", 60000): 1})
    assert _leg_counter(rec, "Cr") == Counter({("1100", 60000): 1})

    xfer = _parse_drcr_blocks(_read_skill("bank-transfer"))[0]
    assert _leg_counter(xfer, "Dr") == Counter({("1010", 50000): 1})
    assert _leg_counter(xfer, "Cr") == Counter({("1000", 50000): 1})

    acc_blocks = _parse_drcr_blocks(_read_skill("journal-accrual"))
    assert _leg_counter(acc_blocks[0], "Dr") == Counter({("6000", 120000): 1})
    assert _leg_counter(acc_blocks[0], "Cr") == Counter({("2010", 120000): 1})
    assert _leg_counter(acc_blocks[1], "Dr") == Counter({("2010", 120000): 1})
    assert _leg_counter(acc_blocks[1], "Cr") == Counter({("6000", 120000): 1})

    prep_blocks = _parse_drcr_blocks(_read_skill("prepayment"))
    assert _leg_counter(prep_blocks[0], "Dr") == Counter({("1110", 120000): 1})
    assert _leg_counter(prep_blocks[0], "Cr") == Counter({("1000", 120000): 1})
    assert _leg_counter(prep_blocks[1], "Dr") == Counter({("6030", 10000): 1})
    assert _leg_counter(prep_blocks[1], "Cr") == Counter({("1110", 10000): 1})

    payroll_blocks = _parse_drcr_blocks(_read_skill("payroll"))
    assert _leg_counter(payroll_blocks[0], "Dr") == Counter({("6600", 250000): 1})
    assert _leg_counter(payroll_blocks[0], "Cr") == Counter(
        {("1000", 215000): 1, ("2030", 22000): 1, ("2030", 13000): 1}
    ), "payroll: Cr bank net + Cr PAYE + Cr employee NIC"
    assert _leg_counter(payroll_blocks[1], "Dr") == Counter({("6610", 15000): 1})
    assert _leg_counter(payroll_blocks[1], "Cr") == Counter({("2030", 15000): 1})

    opening = _parse_drcr_blocks(_read_skill("opening-balances"))[0]
    dr_total = sum(a for d, _, a in opening if d == "Dr")
    cr_total = sum(a for d, _, a in opening if d == "Cr")
    assert dr_total == cr_total == 225000, "opening balances must balance"
    assert _leg_counter(opening, "Dr") == Counter(
        {("1000", 150000): 1, ("1300", 50000): 1, ("1100", 25000): 1}
    )
    assert _leg_counter(opening, "Cr") == Counter(
        {
            ("2000", 30000): 1,
            ("2210", 5000): 1,
            ("2100", 40000): 1,
            ("3000", 150000): 1,
        }
    ), "opening balances: Cr equity 3000 must absorb the residual"


# ---------------------------------------------------------------------------
# Check 9 (F16): US GAAP completeness across the whole catalog
# ---------------------------------------------------------------------------

ASC_RE = re.compile(r"ASC \d{3}")


def test_us_gaap_catalog_complete() -> None:
    """Contract F16: no TODO literal anywhere in skills/catalog/; every
    SKILL.md's Framework variants section carries at least one ASC ddd
    citation; frontmatter frameworks includes us-gaap and matches the
    registry entry."""
    for path in sorted(CATALOG_DIR.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(CATALOG_DIR)
        assert "TODO" not in path.read_text(encoding="utf-8"), (
            f"{rel}: TODO literal found in the catalog"
        )

    registry = {entry["id"]: entry for entry in _registry()}
    for path in sorted(CATALOG_DIR.rglob("SKILL.md")):
        skill_id = path.parent.name
        text = path.read_text(encoding="utf-8")
        section = _section_text(text, "Framework variants")
        assert ASC_RE.search(section), (
            f"{skill_id}: Framework variants section lacks an 'ASC ddd' "
            "citation"
        )
        fm = _frontmatter(text)
        assert "us-gaap" in fm["frameworks"], (
            f"{skill_id}: frontmatter frameworks missing us-gaap"
        )
        assert skill_id in registry, f"{skill_id}: missing from registry"
        assert registry[skill_id]["frameworks"] == fm["frameworks"], (
            f"{skill_id}: registry frameworks {registry[skill_id]['frameworks']} "
            f"!= frontmatter {fm['frameworks']}"
        )

