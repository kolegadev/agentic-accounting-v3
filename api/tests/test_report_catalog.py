"""Validation tests for the report-type skill catalog (Contract F8).

Implements the six contract checks:
1. 5 report skills present; registry lists them (category: report),
   bidirectional consistency (all 5 paths exist and are listed).
2. All 9 required sections present per file (frontmatter + 8 headings).
3. Frontmatter constraints (category: report, frameworks valid,
   status v0, version 0.1.0).
4. Every worked example parses and satisfies its identity EXACTLY in
   pence (identities computed here, not asserted as text):
   P&L revenue − expense = profit; BS assets = liabilities + equity;
   TB debits = credits; CF op+inv+fin = net change in cash = bank
   movement; funds sources = applications.
5. Construction rules reference account classes/codes that exist in the
   COA templates (F3-style check).
6. Formance interaction sections mention get_balances/list_transactions
   and the posted+reversed scope; never mention ``world``.

Plus cross-report tie-together checks: P&L profit == BS retained
earnings (3020); CF closing bank == BS bank lines == TB bank balance.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_DIR = REPO_ROOT / "skills" / "catalog"
REPORTS_DIR = CATALOG_DIR / "reports"
COA_DIR = REPO_ROOT / "api" / "src" / "coa_templates"

REQUIRED_REPORT_IDS = {
    "profit-and-loss",
    "balance-sheet",
    "trial-balance",
    "cash-flow-statement",
    "funds-statement",
}

REQUIRED_SECTIONS = [
    "When to use",
    "Inputs",
    "Construction rules",
    "Framework variants",
    "Formance interaction",
    "Worked example",
    "Verification checks",
    "Edge cases",
]

VALID_FRAMEWORKS = {"uk-gaap", "ifrs", "us-gaap"}

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
SECTION_RE = re.compile(r"^## (.+?)\s*$", re.MULTILINE)
LINES_FENCE_RE = re.compile(
    r"^```lines\s*$(.*?)^```\s*$", re.MULTILINE | re.DOTALL
)
CODE_RE = re.compile(r"\b\d{4}\b")
CLASS_CELL_RE = re.compile(r"`([A-Z][A-Za-z]+)\s+\(([A-Za-z]+)\)`")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _report_dir(report_id: str) -> Path:
    return REPORTS_DIR / report_id


def _read_report(report_id: str) -> str:
    return (_report_dir(report_id) / "SKILL.md").read_text(encoding="utf-8")


def _registry() -> list[dict]:
    raw = yaml.safe_load((CATALOG_DIR / "registry.yaml").read_text())
    assert isinstance(raw, list), "registry must be a YAML list of entries"
    return raw


def _report_entries() -> list[dict]:
    return [e for e in _registry() if e.get("category") == "report"]


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


def _parse_lines_blocks(text: str) -> list[dict[str, list[tuple[str, list[int]]]]]:
    """Parse ```lines fenced blocks.

    A line with no ``|`` is a section header (e.g. ``REVENUE``); data rows
    are ``name | amount`` (or ``name | debit | credit`` for the trial
    balance). Rows before any header sit under the empty section name.
    """
    blocks: list[dict[str, list[tuple[str, list[int]]]]] = []
    for m in LINES_FENCE_RE.finditer(text):
        sections: dict[str, list[tuple[str, list[int]]]] = {}
        current = ""
        for line in m.group(1).splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "|" not in stripped:
                current = stripped
                sections.setdefault(current, [])
                continue
            cells = [c.strip() for c in stripped.split("|")]
            assert len(cells) >= 2, f"bad lines row: {line!r}"
            values = [int(v) for v in cells[1:]]
            sections.setdefault(current, []).append((cells[0], values))
        assert sections, "empty lines fenced block"
        blocks.append(sections)
    return blocks


def _only_block(report_id: str) -> dict[str, list[tuple[str, list[int]]]]:
    blocks = _parse_lines_blocks(_read_report(report_id))
    assert len(blocks) == 1, f"{report_id}: expected exactly one lines block"
    return blocks[0]


def _section_sum(
    sections: dict[str, list[tuple[str, list[int]]]], name: str
) -> int:
    rows = sections.get(name, [])
    assert rows, f"missing '{name}' section"
    total = 0
    for _, values in rows:
        assert len(values) == 1, f"{name}: rows must be single-column"
        total += values[0]
    return total


def _coa_codes_classes() -> tuple[dict[str, tuple[str, str]], set[str], set[str]]:
    codes: dict[str, tuple[str, str]] = {}
    categories: set[str] = set()
    types: set[str] = set()
    for path in COA_DIR.glob("*.json"):
        for account in json.loads(path.read_text(encoding="utf-8")):
            codes.setdefault(account["code"], (account["category"], account["type"]))
            categories.add(account["category"])
            types.add(account["type"])
    return codes, categories, types


# ---------------------------------------------------------------------------
# Check 1: registry completeness and bidirectionality
# ---------------------------------------------------------------------------


def test_report_registry_is_complete_and_bidirectional() -> None:
    entries = _report_entries()
    assert len(entries) == 5, "catalog must contain exactly 5 report skills"

    reg_ids = [entry["id"] for entry in entries]
    assert len(set(reg_ids)) == 5, "report registry ids must be unique"
    assert set(reg_ids) == REQUIRED_REPORT_IDS, "registry ids mismatch contract"

    dir_ids = {p.parent.name for p in REPORTS_DIR.glob("*/SKILL.md")}
    assert dir_ids == REQUIRED_REPORT_IDS, (
        "every report directory must have a SKILL.md and vice versa"
    )

    all_ids = [entry["id"] for entry in _registry()]
    assert len(set(all_ids)) == len(all_ids), "registry ids must be unique"

    for entry in entries:
        for key in ("id", "name", "category", "frameworks", "status", "path"):
            assert key in entry, f"registry entry {entry['id']} missing {key}"
        path = CATALOG_DIR / entry["path"]
        assert path.is_file(), f"registry path missing on disk: {entry['path']}"
        assert entry["path"] == f"reports/{entry['id']}/SKILL.md"


# ---------------------------------------------------------------------------
# Check 2: all 9 required sections present per file
# ---------------------------------------------------------------------------


def test_all_report_skills_have_required_sections() -> None:
    for report_id in REQUIRED_REPORT_IDS:
        text = _read_report(report_id)
        assert FRONTMATTER_RE.match(text), f"{report_id}: frontmatter missing"
        headings = SECTION_RE.findall(text)
        for heading in REQUIRED_SECTIONS:
            assert heading in headings, f"{report_id}: missing '## {heading}'"
        assert len(set(headings)) == len(headings), (
            f"{report_id}: duplicate section headings"
        )


# ---------------------------------------------------------------------------
# Check 3: frontmatter constraints
# ---------------------------------------------------------------------------


def test_report_frontmatter_constraints() -> None:
    for report_id in REQUIRED_REPORT_IDS:
        fm = _frontmatter(_read_report(report_id))
        assert fm["id"] == report_id, f"{report_id}: id must match directory"
        assert fm.get("name") and isinstance(fm["name"], str)
        assert fm["category"] == "report"
        assert fm["frameworks"], f"{report_id}: frameworks must be non-empty"
        assert set(fm["frameworks"]) <= VALID_FRAMEWORKS
        assert fm["version"] == "0.1.0"
        assert fm["status"] == "v0"

    for entry in _report_entries():
        fm = _frontmatter(_read_report(entry["id"]))
        assert entry["name"] == fm["name"], f"{entry['id']}: name mismatch"
        assert entry["frameworks"] == fm["frameworks"]
        assert entry["status"] == fm["status"]
        assert entry["category"] == fm["category"]


# ---------------------------------------------------------------------------
# Check 4: worked-example identities, computed (not asserted as text)
# ---------------------------------------------------------------------------


def test_pl_worked_example_identity() -> None:
    sections = _only_block("profit-and-loss")
    revenue = _section_sum(sections, "REVENUE")
    profit_rows = sections.get("PROFIT", [])
    assert len(profit_rows) == 1, "P&L must present a single profit figure"
    assert len(profit_rows[0][1]) == 1, "profit line must be single-column"
    profit = profit_rows[0][1][0]
    expenses = sum(
        value
        for sec, rows in sections.items()
        if sec not in ("REVENUE", "PROFIT")
        for _, values in rows
        for value in values
    )
    assert revenue - expenses == profit, (
        f"P&L identity broken: revenue {revenue} - expenses {expenses} "
        f"!= profit {profit} (pence)"
    )


def test_bs_worked_example_identity() -> None:
    sections = _only_block("balance-sheet")
    assets = _section_sum(sections, "ASSETS")
    liabilities = _section_sum(sections, "LIABILITIES")
    equity = _section_sum(sections, "EQUITY")
    assert assets == liabilities + equity, (
        f"BS identity broken: assets {assets} != liabilities {liabilities} "
        f"+ equity {equity} (pence)"
    )
    dep_rows = [values for name, values in sections["ASSETS"] if "1360" in name]
    assert dep_rows, "BS must show 1360 Accumulated Depreciation in ASSETS"
    assert all(values[0] < 0 for values in dep_rows), (
        "1360 must be a contra-asset (negative within ASSETS), never equity"
    )


def test_tb_worked_example_identity() -> None:
    sections = _only_block("trial-balance")
    rows = sections.get("", [])
    assert rows, "TB rows must carry debit and credit columns"
    debits = credits = 0
    for name, values in rows:
        if name.upper() == "TOTALS":
            continue
        assert len(values) == 2, f"TB row {name!r} must have two columns"
        debits += values[0]
        credits += values[1]
    assert debits == credits, (
        f"TB unbalanced: debits {debits} != credits {credits} (pence)"
    )


def test_cf_worked_example_identity() -> None:
    sections = _only_block("cash-flow-statement")

    def section_subtotal(name: str) -> tuple[int, int]:
        rows = sections.get(name, [])
        assert rows, f"missing '{name}' section"
        subtotal_name, subtotal_values = rows[-1]
        assert subtotal_name.startswith("Net cash from"), (
            f"{name}: last row must be the section subtotal"
        )
        assert len(subtotal_values) == 1, f"{name}: subtotal must be single-column"
        body = sum(value for _, values in rows[:-1] for value in values)
        return body, subtotal_values[0]

    op_body, operating = section_subtotal("OPERATING")
    inv_body, investing = section_subtotal("INVESTING")
    fin_body, financing = section_subtotal("FINANCING")
    assert op_body == operating, f"OPERATING body {op_body} != subtotal {operating}"
    assert inv_body == investing, f"INVESTING body {inv_body} != subtotal {investing}"
    assert fin_body == financing, f"FINANCING body {fin_body} != subtotal {financing}"

    rec = {name: values[0] for name, values in sections.get("RECONCILIATION", [])}
    net_change = [v for n, v in rec.items() if n.startswith("Net change in cash")]
    opening = [v for n, v in rec.items() if n.startswith("Bank opening")]
    closing = [v for n, v in rec.items() if n.startswith("Bank closing")]
    assert len(net_change) == len(opening) == len(closing) == 1, (
        "RECONCILIATION needs net change, opening bank and closing bank lines"
    )
    assert operating + investing + financing == net_change[0], (
        f"CF identity broken: {operating} + {investing} + {financing} "
        f"!= net change {net_change[0]} (pence)"
    )
    assert closing[0] - opening[0] == net_change[0], (
        f"CF bank reconciliation broken: closing {closing[0]} - opening "
        f"{opening[0]} != net change {net_change[0]} (pence)"
    )


def test_funds_worked_example_identity() -> None:
    sections = _only_block("funds-statement")
    sources = _section_sum(sections, "SOURCES")
    applications = _section_sum(sections, "APPLICATIONS")
    assert sources == applications, (
        f"funds identity broken: sources {sources} != applications "
        f"{applications} (pence)"
    )
    app_wc = [
        values[0]
        for name, values in sections["APPLICATIONS"]
        if "working capital" in name.lower()
    ]
    assert len(app_wc) == 1, (
        "APPLICATIONS must end with an explicit increase-in-working-capital line"
    )
    split = sections.get("WORKING CAPITAL SPLIT", [])
    assert split, "funds statement must show the working-capital split"
    ca = next(values[0] for name, values in split if "current assets" in name.lower())
    cl = next(
        values[0] for name, values in split if "current liabilities" in name.lower()
    )
    net_wc = next(
        values[0] for name, values in split if "net working capital" in name.lower()
    )
    assert ca - cl == net_wc, (
        f"working-capital split broken: current assets {ca} - current "
        f"liabilities {cl} != net movement {net_wc} (pence)"
    )
    assert net_wc == app_wc[0], (
        f"working-capital movement {net_wc} must equal the balancing "
        f"application line {app_wc[0]} (pence)"
    )


# ---------------------------------------------------------------------------
# Cross-report tie-together (one scenario across all five files)
# ---------------------------------------------------------------------------


def test_worked_examples_tie_together() -> None:
    pl = _only_block("profit-and-loss")
    profit = pl["PROFIT"][0][1][0]

    bs = _only_block("balance-sheet")
    re_rows = [values for name, values in bs["EQUITY"] if "3020" in name]
    assert re_rows and re_rows[0][0] == profit, (
        f"BS retained earnings 3020 {re_rows[0][0] if re_rows else None} "
        f"must equal P&L profit {profit} (pence)"
    )
    bank_bs = sum(
        values[0]
        for name, values in bs["ASSETS"]
        if "1000" in name or "1010" in name
    )

    cf = _only_block("cash-flow-statement")
    cf_profit = next(
        values[0]
        for name, values in cf["OPERATING"]
        if name.lower().startswith("profit")
    )
    assert cf_profit == profit, "CF must start from the P&L profit"
    dep_addback = next(
        values[0]
        for name, values in cf["OPERATING"]
        if name.lower().startswith("add back")
    )
    pl_dep = next(
        values[0] for name, values in pl["DEPRECIATION"] if "6500" in name
    )
    assert dep_addback == pl_dep, "CF depreciation add-back must equal P&L 6500"
    closing = next(
        values[0]
        for name, values in cf["RECONCILIATION"]
        if name.startswith("Bank closing")
    )
    assert closing == bank_bs, (
        f"CF closing bank {closing} must equal BS bank lines {bank_bs} (pence)"
    )

    tb = _only_block("trial-balance")
    tb_bank = next(
        values[0] for name, values in tb.get("", []) if name.startswith("1000")
    )
    assert tb_bank == bank_bs, (
        f"TB bank balance {tb_bank} must equal BS bank lines {bank_bs} (pence)"
    )


# ---------------------------------------------------------------------------
# Check 5: construction rules reference the real COA classes/codes
# ---------------------------------------------------------------------------


def test_construction_rules_reference_coa() -> None:
    codes, categories, types = _coa_codes_classes()
    assert {"Asset", "Liability", "Equity", "Revenue", "Expense"} <= categories

    for report_id in REQUIRED_REPORT_IDS:
        section = _section_text(_read_report(report_id), "Construction rules")
        for token in CODE_RE.findall(section):
            assert token in codes, (
                f"{report_id}: construction rules reference code {token} "
                "which is not in any COA template"
            )
        for m in CLASS_CELL_RE.finditer(section):
            category, acct_type = m.group(1), m.group(2)
            assert category in categories, (
                f"{report_id}: class {category!r} not in COA categories"
            )
            assert acct_type in types, (
                f"{report_id}: type {acct_type!r} not in COA types"
            )


# ---------------------------------------------------------------------------
# Check 6: Formance interaction contract (reads, scope, no 'world')
# ---------------------------------------------------------------------------


def test_formance_interaction_contract() -> None:
    for report_id in REQUIRED_REPORT_IDS:
        text = _read_report(report_id)
        section = _section_text(text, "Formance interaction")
        for token in ("get_balances", "list_transactions", "posted", "reversed"):
            assert token in section, (
                f"{report_id}: Formance section must mention '{token}'"
            )
        assert "verify_parity" in section, (
            f"{report_id}: Formance section must reference verify_parity"
        )
        assert "world" not in text.lower(), (
            f"{report_id}: must never reference the 'world' contra account"
        )


# ---------------------------------------------------------------------------
# Check 7 (F16): US GAAP completeness across the whole catalog
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
        report_id = path.parent.name
        text = path.read_text(encoding="utf-8")
        section = _section_text(text, "Framework variants")
        assert ASC_RE.search(section), (
            f"{report_id}: Framework variants section lacks an 'ASC ddd' "
            "citation"
        )
        fm = _frontmatter(text)
        assert "us-gaap" in fm["frameworks"], (
            f"{report_id}: frontmatter frameworks missing us-gaap"
        )
        assert report_id in registry, f"{report_id}: missing from registry"
        assert registry[report_id]["frameworks"] == fm["frameworks"], (
            f"{report_id}: registry frameworks "
            f"{registry[report_id]['frameworks']} != frontmatter "
            f"{fm['frameworks']}"
        )

