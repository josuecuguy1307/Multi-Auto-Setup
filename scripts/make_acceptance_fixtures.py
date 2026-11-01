"""Generates the fixtures of docs/ROUTER_ACCEPTANCE.md into tests/fixtures/acceptance/<id>/ with a fixed seed. Everything here is invented data (nothing real, nothing confidential). The expected
values are computed by code in this file, never by a model; the OLS expectations come from statsmodels (run with `uv run --no-project --with statsmodels --with numpy python scripts/make_acceptance_fixtures.py`
once; the numbers are then committed, and the tests never need statsmodels). `manifest.json` records the SHA-256 of every file, and a test fails if a fixture drifts."""
from __future__ import annotations

import hashlib
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "acceptance"
SEED = 20261031
FILLER = ["The committee reviewed the proposal in detail.", "Several regional offices reported steady demand throughout the period.", "Analysts noted that logistics costs remained broadly stable.",
          "The report describes the methodology used to collect the figures.", "Staffing levels were adjusted to match seasonal patterns.", "Customers continued to ask for clearer pricing.",
          "Management said that the schedule of the programme had not changed.", "A separate section covers the risks identified by the audit team.",
          "The appendix lists the sources consulted for each chapter.", "Local partners provided most of the data used in the comparison."]


def words(n_words: int, rng: random.Random, facts: list[str]) -> str:
    out, i = [], 0
    while sum(len(s.split()) for s in out) < n_words:
        out.append(rng.choice(FILLER))
        if facts and i % max(1, n_words // (len(facts) * 14)) == 0 and facts:
            out.append(facts.pop(0))
        i += 1
    out += facts
    return " ".join(out)


def f_article(rng):
    figs = ["Revenue reached $48.2 million, up 12.5% on the year.", "The company employs 1,340 people in 9 countries.", "Operating margin improved to 18.4%.", "Net debt fell by $6.1 million.",
            "A new plant in Rosario will open in March 2027."]
    text = words(900, rng, list(figs))
    keys = ["$48.2 million", "12.5%", "1,340", "18.4%", "$6.1 million"]
    lookahead = "".join(f"(?=.*{k.replace('$', chr(92) + '$').replace('.', chr(92) + '.')})" for k in keys)
    check = {"kind": "regex", "value": r"\A" + lookahead + r"(?:[-*] [^\n]+\n){4}[-*] [^\n]+\n?\Z"}
    return {"input.txt": "Summarize this article in 5 bullets, one per line, each starting with '- '.\n\n" + text, "check.json": json.dumps(check), "good.txt": "\n".join(f"- {k}" for k in keys) + "\n", "figures.json": json.dumps(keys)}


def f_notes(rng):
    names = ["Quinta Alba", "Taller Norte", "Ferretería Sol", "Imprenta Lago", "Café Ceiba", "Radio Faro", "Panadería Luna", "Estudio Mapa", "Viveros Sur", "Clínica Ruiz", "Textiles Ola", "Gráfica Nube"]
    rows = [(n, rng.randrange(120, 2400), f"2026-0{rng.randrange(1, 9)}-{rng.randrange(10, 28)}") for n in names]
    total = sum(r[1] for r in rows)
    notes = "\n".join(f"{i}. {n} (FAKE-{1000 + i}) paid {a} USD on {d}; reference note: pending review." for i, (n, a, d) in enumerate(rows, 1))
    big = max(rows, key=lambda r: r[1])
    check = {"kind": "regex", "value": rf"(?=.*{total})(?=.*{big[0]})"}
    return {"input.txt": "Summarize these client payment notes in 3 lines: the total paid and the largest payment.\n\n" + notes, "check.json": json.dumps(check),
            "good.txt": f"Total paid: {total} USD.\nLargest payment: {big[0]}, {big[1]} USD.\nTwelve notes.\n"}


def f_long(rng):
    figs = ["Total exports were $812 million.", "Imports fell 4.7% to $903 million.", "The trade deficit narrowed to $91 million.", "Employment in the sector reached 52,400 workers.", "Investment will total $1.6 billion by 2029."]
    keys = ["$812 million", "4.7%", "$91 million", "52,400", "$1.6 billion"]
    text = words(9500, rng, list(figs))  # about 12,000 tokens: longer than an 8192-token context
    lookahead = "".join(f"(?=.*{k.replace('$', chr(92) + '$').replace('.', chr(92) + '.')})" for k in keys)
    check = {"kind": "regex", "value": r"\A" + lookahead + r"(?:\S+\s+){0,349}\S+\s*\Z"}
    return {"input.txt": "Write a one-page brief (at most 350 words) of this public report that carries its five key figures exactly as written.\n\n" + text, "check.json": json.dumps(check),
            "good.txt": "Brief. " + " ".join(keys) + ".\n", "figures.json": json.dumps(keys)}


# ---- project fixtures (code tasks) -------------------------------------------------------------------------------------------------------------------
def cli_module(extra_lines=120):
    body = ['"""A small command-line tool."""', "import argparse", "import json", "import os", "", ""]
    body += ["def rename(path, new, dry_run=False):", "    if dry_run:", "        return new", "    os.rename(path, new)", "    return new", "", ""]
    for i in range(extra_lines // 6):
        body += [f"def helper_{i}(x):", f"    y = x + {i}", f"    return y * 2", "", ""]
    body += ["def main(argv=None):", "    p = argparse.ArgumentParser()", "    p.add_argument('path')", "    p.add_argument('new')", "    a = p.parse_args(argv)", "    print(rename(a.path, a.new))", "", "",
             "if __name__ == '__main__':", "    main()"]
    return "\n".join(body) + "\n"


def f_cli_dryrun(rng):
    tool = cli_module()
    tests = ("import os, tempfile, unittest\nimport tool\n\n\nclass T(unittest.TestCase):\n    def test_rename_moves(self):\n        d = tempfile.mkdtemp(); a = os.path.join(d, 'a'); b = os.path.join(d, 'b'); open(a, 'w').write('x')\n"
             "        tool.rename(a, b); self.assertTrue(os.path.exists(b))\n    def test_helper(self):\n        self.assertEqual(tool.helper_0(1), 2)\n    def test_helper_2(self):\n        self.assertEqual(tool.helper_2(1), 6)\n"
             "    def test_main_prints(self):\n        d = tempfile.mkdtemp(); a = os.path.join(d, 'a'); b = os.path.join(d, 'b'); open(a, 'w').write('x')\n        tool.main([a, b]); self.assertTrue(os.path.exists(b))\n"
             "    def test_rename_returns_new(self):\n        d = tempfile.mkdtemp(); a = os.path.join(d, 'a'); open(a, 'w').write('x')\n        self.assertEqual(tool.rename(a, os.path.join(d, 'c')), os.path.join(d, 'c'))\n"
             "    def test_dry_run_changes_nothing(self):\n        d = tempfile.mkdtemp(); a = os.path.join(d, 'a'); b = os.path.join(d, 'b'); open(a, 'w').write('x')\n        tool.main(['--dry-run', a, b])\n        self.assertTrue(os.path.exists(a)); self.assertFalse(os.path.exists(b))\n")
    good = tool.replace("    p.add_argument('path')", "    p.add_argument('--dry-run', action='store_true')\n    p.add_argument('path')").replace("print(rename(a.path, a.new))", "print(rename(a.path, a.new, dry_run=a.dry_run))")
    check = {"kind": "project_tests", "files": {"tool.py": tool, "test_cli.py": tests}, "pattern": "test_cli.py", "min_tests": 6, "protected": ["test_cli.py"]}
    prompt = "Add a --dry-run flag to this CLI and make test_cli.py pass. Return the whole changed file as a block starting with '# file: tool.py'.\n\n# file: tool.py\n" + tool + "\n# file: test_cli.py\n" + tests
    return {"input.txt": prompt, "check.json": json.dumps(check), "good.txt": "```python\n# file: tool.py\n" + good + "```\n", "bad.txt": "```python\n# file: tool.py\n" + tool + "```\n"}


def f_leap(rng):
    mod = "def is_leap(year):\n    return year % 4 == 0\n\n\ndef days_in_year(year):\n    return 366 if is_leap(year) else 365\n"
    tests = ("import unittest\nfrom dates import is_leap, days_in_year\n\n\nclass T(unittest.TestCase):\n    def test_2024(self):\n        self.assertTrue(is_leap(2024))\n    def test_2023(self):\n        self.assertFalse(is_leap(2023))\n"
             "    def test_1900(self):\n        self.assertFalse(is_leap(1900))\n    def test_2000(self):\n        self.assertTrue(is_leap(2000))\n    def test_2100(self):\n        self.assertEqual(days_in_year(2100), 365)\n"
             "    def test_2400(self):\n        self.assertEqual(days_in_year(2400), 366)\n    def test_1996(self):\n        self.assertTrue(is_leap(1996))\n")
    good = mod.replace("return year % 4 == 0", "return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)")
    check = {"kind": "project_tests", "files": {"dates.py": mod, "test_dates.py": tests}, "pattern": "test_dates.py", "min_tests": 7, "protected": ["test_dates.py"]}
    return {"input.txt": "test_dates.py has failing tests. Fix the bug in dates.py and return the whole file as a block starting with '# file: dates.py'.\n\n# file: dates.py\n" + mod + "\n# file: test_dates.py\n" + tests,
            "check.json": json.dumps(check), "good.txt": "```python\n# file: dates.py\n" + good + "```\n", "bad.txt": "```python\n# file: dates.py\n" + mod + "```\n"}


def f_csv(rng):
    cli = ("import json, sys\n\n\ndef rows():\n    return [{'name': 'a', 'qty': 1}, {'name': 'b', 'qty': 2}]\n\n\ndef render(fmt='json'):\n    if fmt == 'json':\n        return json.dumps(rows())\n    raise ValueError(fmt)\n\n\n"
           "def main(argv=None):\n    argv = argv if argv is not None else sys.argv[1:]\n    fmt = 'json'\n    if '--format' in argv:\n        fmt = argv[argv.index('--format') + 1]\n    print(render(fmt))\n")
    old = ("import unittest\nimport cli\n\n\nclass Old(unittest.TestCase):\n    def test_default_is_json(self):\n        self.assertEqual(cli.render(), '[{\"name\": \"a\", \"qty\": 1}, {\"name\": \"b\", \"qty\": 2}]')\n"
           "    def test_rows(self):\n        self.assertEqual(len(cli.rows()), 2)\n    def test_unknown_format(self):\n        with self.assertRaises(ValueError):\n            cli.render('xml')\n"
           "    def test_main_json(self):\n        cli.main([])\n    def test_main_format_json(self):\n        cli.main(['--format', 'json'])\n")
    new = ("import unittest\nimport cli\n\n\nclass New(unittest.TestCase):\n    def test_csv_header(self):\n        self.assertEqual(cli.render('csv').splitlines()[0], 'name,qty')\n"
           "    def test_csv_rows(self):\n        self.assertEqual(cli.render('csv').splitlines()[1:], ['a,1', 'b,2'])\n    def test_main_csv(self):\n        cli.main(['--format', 'csv'])\n")
    good = cli.replace("    raise ValueError(fmt)", "    if fmt == 'csv':\n        return '\\n'.join(['name,qty'] + [f\"{r['name']},{r['qty']}\" for r in rows()])\n    raise ValueError(fmt)")
    check = {"kind": "project_tests", "files": {"cli.py": cli, "test_old.py": old, "test_new.py": new}, "pattern": "test_*.py", "min_tests": 8, "protected": ["test_old.py", "test_new.py"]}
    shown = {"cli.py": cli, "test_old.py": old}
    return {"input.txt": "Add --format csv to this CLI, with tests that pass; the old tests must still pass. Return whole changed files as blocks starting with '# file: <name>'.\n\n" + "\n".join(f"# file: {k}\n{v}" for k, v in shown.items()),
            "check.json": json.dumps(check), "good.txt": "```python\n# file: cli.py\n" + good + "```\n", "bad.txt": "```python\n# file: cli.py\n" + cli + "```\n"}


def f_split(rng):
    blocks = [f"def op_{i}(x):\n    return x * {i + 2} + {i}\n" for i in range(60)]
    module = '"""inventory"""\n\n\n' + "\n\n".join(blocks) + "\n"
    pad = "\n".join(f"# line {i}" for i in range(600 - len(module.splitlines())))
    module += pad + "\n"
    tests = "import unittest\nimport inventory\n\n\nclass T(unittest.TestCase):\n" + "".join(f"    def test_{i}(self):\n        self.assertEqual(inventory.op_{i * 2}({i}), {i} * {i * 2 + 2} + {i * 2})\n" for i in range(25))
    parts = ["part_a", "part_b", "part_c"]
    chunks = [blocks[0:20], blocks[20:40], blocks[40:60]]
    files = {f"{p}.py": "\n\n".join(c) + "\n" for p, c in zip(parts, chunks)}
    shim = "from part_a import *\nfrom part_b import *\nfrom part_c import *\n"
    check = {"kind": "project_tests", "files": {"inventory.py": module, "test_inventory.py": tests}, "pattern": "test_inventory.py", "min_tests": 25, "protected": ["test_inventory.py"]}
    good = "".join(f"```python\n# file: {k}\n{v}```\n" for k, v in {**files, "inventory.py": shim}.items())
    bad = f"```python\n# file: part_a.py\n{files['part_a.py']}```\n```python\n# file: inventory.py\nfrom part_a import *\n```\n"
    return {"input.txt": "Split this 600-line module into 3 modules without changing behavior; keep `inventory.py` importing them so test_inventory.py stays green. Return every file as a block starting with '# file: <name>'.\n\n# file: inventory.py\n" + module,
            "check.json": json.dumps(check), "good.txt": good, "bad.txt": bad}


def f_invoices_script(rng):
    mod = "def total(invoices):\n    seen = []\n    s = 0\n    for inv in invoices:\n        s += inv['amount']\n        seen.append(inv['id'])\n    return s\n\n\ndef count(invoices):\n    return len(invoices)\n"
    tests = ("import unittest\nfrom invoices import total, count\n\n\nclass T(unittest.TestCase):\n    def test_plain(self):\n        self.assertEqual(total([{'id': 1, 'amount': 10}, {'id': 2, 'amount': 5}]), 15)\n"
             "    def test_double_counted(self):\n        self.assertEqual(total([{'id': 1, 'amount': 10}, {'id': 1, 'amount': 10}]), 10)\n    def test_empty(self):\n        self.assertEqual(total([]), 0)\n"
             "    def test_count(self):\n        self.assertEqual(count([{'id': 1, 'amount': 1}]), 1)\n    def test_three(self):\n        self.assertEqual(total([{'id': 1, 'amount': 1}, {'id': 2, 'amount': 2}, {'id': 1, 'amount': 1}]), 3)\n"
             "    def test_float(self):\n        self.assertEqual(total([{'id': 7, 'amount': 2.5}]), 2.5)\n")
    good = mod.replace("        s += inv['amount']\n        seen.append(inv['id'])", "        if inv['id'] in seen:\n            continue\n        s += inv['amount']\n        seen.append(inv['id'])")
    check = {"kind": "project_tests", "files": {"invoices.py": mod, "test_invoices.py": tests}, "pattern": "test_invoices.py", "min_tests": 6, "protected": ["test_invoices.py"]}
    return {"input.txt": "invoices.py counts an invoice twice when it appears twice. Fix it; return the whole file as a block starting with '# file: invoices.py'.\n\n# file: invoices.py\n" + mod + "\n# file: test_invoices.py\n" + tests,
            "check.json": json.dumps(check), "good.txt": "```python\n# file: invoices.py\n" + good + "```\n", "bad.txt": "```python\n# file: invoices.py\n" + mod + "```\n"}


# ---- data tasks ------------------------------------------------------------------------------------------------------------------------------------------------
def f_bank(rng):
    refs = [f"R{1000 + i}" for i in range(40)]
    ledger = {r: round(rng.uniform(20, 900), 2) for r in refs}
    bank = dict(ledger)
    diffs = []
    miss = refs[3]; del bank[miss]; diffs.append({"ref": miss, "kind": "missing_in_bank"})
    typo = refs[11]; bank[typo] = round(ledger[typo] + 9.0, 2); diffs.append({"ref": typo, "kind": "amount_differs"})
    extra = "R2001"; bank[extra] = 77.5; diffs.append({"ref": extra, "kind": "missing_in_ledger"})
    fee = "R2002"; bank[fee] = 12.0; diffs.append({"ref": fee, "kind": "missing_in_ledger"})
    late = refs[25]; del bank[late]; diffs.append({"ref": late, "kind": "missing_in_bank"})
    diffs.sort(key=lambda d: d["ref"])
    csv = lambda d: "ref,amount\n" + "\n".join(f"{k},{v:.2f}" for k, v in sorted(d.items()))
    expected = json.dumps(diffs, sort_keys=True, separators=(",", ":"))
    return {"input.txt": "Reconcile the bank statement with the ledger. Reply with only a JSON list, sorted by ref, of objects {\"ref\", \"kind\"} where kind is missing_in_bank, missing_in_ledger or amount_differs.\n\nbank.csv\n" + csv(bank) + "\n\nledger.csv\n" + csv(ledger),
            "check.json": json.dumps({"kind": "equals", "value": expected}), "good.txt": expected, "expected.json": expected}


def f_cashflow(rng):
    inflow = [rng.randrange(8000, 20000) for _ in range(12)]
    outflow = [rng.randrange(7000, 21000) for _ in range(12)]
    net = [a - b for a, b in zip(inflow, outflow)]
    neg = [i + 1 for i, n in enumerate(net) if n < 0]
    exp = json.dumps({"net": net, "negative_months": neg}, sort_keys=True, separators=(",", ":"))
    table = "month,inflow,outflow\n" + "\n".join(f"{i + 1},{a},{b}" for i, (a, b) in enumerate(zip(inflow, outflow)))
    return {"input.txt": "From this 12-month table reply with only JSON {\"net\": [monthly inflow minus outflow], \"negative_months\": [month numbers whose net is below zero]}.\n\n" + table,
            "check.json": json.dumps({"kind": "equals", "value": exp}), "good.txt": exp, "expected.json": exp}


CHART = {"1000": "Cash", "1100": "Accounts receivable", "1200": "Inventory", "2000": "Accounts payable", "3000": "Owner equity", "4000": "Sales", "5000": "Cost of goods", "5100": "Rent", "5200": "Utilities"}


def f_journal(rng):
    kinds = [("cash sale", "1000", "4000"), ("sale on credit", "1100", "4000"), ("buy inventory on credit", "1200", "2000"), ("pay rent in cash", "5100", "1000"), ("pay utilities in cash", "5200", "1000"),
             ("collect a receivable", "1000", "1100"), ("pay a supplier", "2000", "1000"), ("owner invests cash", "1000", "3000")]
    txs = []
    for i in range(20):
        k = kinds[i % len(kinds)]
        txs.append({"id": i + 1, "what": k[0], "amount": rng.randrange(50, 3000), "debit": k[1], "credit": k[2]})
    entries = [{"id": t["id"], "debit": t["debit"], "credit": t["credit"], "amount": t["amount"]} for t in txs]
    exp = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    lines = "\n".join(f"{t['id']}. {t['what']} of {t['amount']} (use the chart rule for '{t['what']}')" for t in txs)
    rules = "\n".join(f"- {k[0]}: debit {k[1]}, credit {k[2]}" for k in kinds)
    return {"input.txt": "Turn these 20 transactions into double-entry journal entries. Reply with only a JSON list of {\"id\", \"debit\", \"credit\", \"amount\"} (account numbers from the chart), ordered by id.\n\nChart: " + json.dumps(CHART)
            + "\nRules:\n" + rules + "\n\nTransactions:\n" + lines, "check.json": json.dumps({"kind": "equals", "value": exp}), "good.txt": exp, "expected.json": exp, "chart.json": json.dumps(CHART)}


def f_loan(rng):
    i = 0.12 / 12
    pay = round(10000 * i / (1 - (1 + i) ** -24), 2)
    exp = json.dumps({"final_balance": 0.0, "payment": pay}, sort_keys=True, separators=(",", ":"))
    return {"input.txt": "A loan of $10,000 at 12% per year (1% per month), 24 equal monthly payments, French method. Reply with only JSON {\"payment\": <rounded to cents>, \"final_balance\": <after the last payment, rounded to cents>}.",
            "check.json": json.dumps({"kind": "equals", "value": exp}), "good.txt": exp, "expected.json": exp}


def f_extraction(rng):
    sup = ["Alba Tools", "Norte Print", "Sol Foods", "Lago Paper", "Ceiba Cafe", "Faro Radio", "Luna Bakery", "Mapa Studio", "Sur Plants", "Ruiz Clinic", "Ola Textiles", "Nube Graphics", "Delta Parts", "Roble Wood", "Pampa Seeds"]
    out, texts = [], []
    for n, s in enumerate(sup, 1):
        sub = rng.randrange(100, 5000)
        rate = rng.choice([5, 10, 12])
        tax = round(sub * rate / 100, 2)
        d = f"2026-{rng.randrange(1, 13):02d}-{rng.randrange(10, 28)}"
        y, m, dd = d.split("-")
        layout = n % 3
        if layout == 0:
            t = f"INVOICE {s} / No. INV-{900 + n} / Date {dd}.{m}.{y} / Subtotal {sub:.2f} / Tax {rate}% {tax:.2f} / TOTAL {sub + tax:.2f}"
        elif layout == 1:
            t = f"{s}\nInvoice number: INV-{900 + n}\nIssued: {d}\nNet {sub:.2f}; tax ({rate}%) {tax:.2f}; amount due {sub + tax:.2f}"
        else:
            t = f"From {s}, invoice INV-{900 + n}, dated {d}. Please pay {sub + tax:.2f} (including {tax:.2f} tax)."
        texts.append(f"[{n}] {t}")
        out.append({"supplier": s, "date": d, "total": f"{sub + tax:.2f}", "tax": f"{tax:.2f}"})
    exp = json.dumps(out, sort_keys=True, separators=(",", ":"))
    return {"input.txt": "Extract supplier, date (ISO), total and tax (strings with two decimals) from each of the 15 invoice texts. Reply with only a JSON list in order.\n\n" + "\n".join(texts),
            "check.json": json.dumps({"kind": "equals", "value": exp}), "good.txt": exp, "expected.json": exp}


def f_cleaning(rng):
    rows = [(f"{n}", f"user{n}@x.com", f"{rng.randrange(1, 28):02d}/{rng.randrange(1, 13):02d}/2025") for n in range(1, 31)]
    messy = ["id,email,joined"] + [f" {a} , {b.upper() if i % 4 == 0 else b} ,{c}" for i, (a, b, c) in enumerate(rows)] + [f" {rows[2][0]} , {rows[2][1]} ,{rows[2][2]}", f" {rows[9][0]} , {rows[9][1].upper()} ,{rows[9][2]}", f"{rows[15][0]},{rows[15][1]},{rows[15][2]}"]
    clean = ["id,email,joined"] + [f"{a},{b},{c[6:]}-{c[3:5]}-{c[0:2]}" for a, b, c in rows]
    return {"input.txt": "Clean this CSV: trim spaces, lowercase the emails, drop duplicate rows (keep the first), write dates as YYYY-MM-DD (the input is DD/MM/YYYY). Reply with only the CSV.\n\n" + "\n".join(messy),
            "check.json": json.dumps({"kind": "equals", "value": "\n".join(clean)}), "good.txt": "\n".join(clean), "expected.json": json.dumps(clean)}


def f_ols(rng):
    import numpy as np
    import statsmodels.api as sm
    n = 60
    price = np.round(np.array([rng.uniform(8, 20) for _ in range(n)]), 2)
    ads = np.round(np.array([rng.uniform(1, 10) for _ in range(n)]), 2)
    sales = np.round(200 - 6.5 * price + 9.0 * ads + np.array([rng.gauss(0, 12) for _ in range(n)]), 2)
    X = sm.add_constant(np.column_stack([price, ads]))
    fit = sm.OLS(sales, X).fit()
    exp = {"coef": [round(float(v), 6) for v in fit.params], "r2": round(float(fit.rsquared), 6), "price_significant_5pct": bool(fit.pvalues[1] < 0.05), "statsmodels": sm.__version__}
    data = "price,ads,sales\n" + "\n".join(f"{a},{b},{c}" for a, b, c in zip(price, ads, sales))
    tests = ("import csv, io, unittest\nimport ols\nDATA = '''" + data + "'''\nEXP = " + repr(exp) + "\n\n\nclass T(unittest.TestCase):\n    def setUp(self):\n        rows = list(csv.DictReader(io.StringIO(DATA)))\n"
             "        self.out = ols.fit([float(r['price']) for r in rows], [float(r['ads']) for r in rows], [float(r['sales']) for r in rows])\n    def test_coefficients(self):\n        for got, want in zip(self.out['coef'], EXP['coef']):\n            self.assertAlmostEqual(got, want, places=4)\n"
             "    def test_r2(self):\n        self.assertAlmostEqual(self.out['r2'], EXP['r2'], places=4)\n    def test_significance(self):\n        self.assertEqual(self.out['price_significant_5pct'], EXP['price_significant_5pct'])\n")
    ref = ("def fit(price, ads, sales):\n    n = len(sales)\n    X = [[1.0, p, a] for p, a in zip(price, ads)]\n    def mat_t_mul(A, B):\n        return [[sum(A[k][i] * B[k][j] for k in range(len(A))) for j in range(len(B[0]))] for i in range(len(A[0]))]\n"
           "    def inv(M):\n        m = len(M); A = [r[:] + [1.0 if i == j else 0.0 for j in range(m)] for i, r in enumerate(M)]\n        for c in range(m):\n            p = max(range(c, m), key=lambda r: abs(A[r][c])); A[c], A[p] = A[p], A[c]\n"
           "            d = A[c][c]; A[c] = [v / d for v in A[c]]\n            for r in range(m):\n                if r != c:\n                    f = A[r][c]; A[r] = [v - f * w for v, w in zip(A[r], A[c])]\n        return [r[m:] for r in A]\n"
           "    XtX = mat_t_mul(X, X); inv_ = inv(XtX); Xty = [sum(X[k][i] * sales[k] for k in range(n)) for i in range(3)]\n    beta = [sum(inv_[i][j] * Xty[j] for j in range(3)) for i in range(3)]\n"
           "    fitted = [sum(b * x for b, x in zip(beta, row)) for row in X]; my = sum(sales) / n\n    sse = sum((y - f) ** 2 for y, f in zip(sales, fitted)); sst = sum((y - my) ** 2 for y in sales)\n"
           "    s2 = sse / (n - 3); se = (s2 * inv_[1][1]) ** 0.5; t = beta[1] / se\n    return {'coef': beta, 'r2': 1 - sse / sst, 'price_significant_5pct': abs(t) > 2.0017}\n")
    check = {"kind": "project_tests", "files": {"test_ols.py": tests}, "pattern": "test_ols.py", "min_tests": 3, "protected": ["test_ols.py"]}
    return {"input.txt": "Sales on price and ads (60 rows, CSV below). Write ols.py with fit(price, ads, sales) returning {'coef': [intercept, price, ads], 'r2': R squared, 'price_significant_5pct': whether the price coefficient is significant at the 5% level}. "
            "Pure Python only. Return it as a block starting with '# file: ols.py'.\n\n" + data, "check.json": json.dumps(check), "good.txt": "```python\n# file: ols.py\n" + ref + "```\n", "bad.txt": "```python\n# file: ols.py\ndef fit(price, ads, sales):\n    return {'coef': [0, 0, 0], 'r2': 0, 'price_significant_5pct': False}\n```\n", "expected.json": json.dumps(exp)}


FIXTURES = {"ex1_article": f_article, "ex3_notes": f_notes, "ex4_long": f_long, "ex2_cli_dryrun": f_cli_dryrun, "c7_leap": f_leap, "c8_csv": f_csv, "c9_split": f_split, "c10_invoices": f_invoices_script,
            "f1_bank": f_bank, "f2_cashflow": f_cashflow, "f3_journal": f_journal, "f4_loan": f_loan, "d11_extraction": f_extraction, "d12_cleaning": f_cleaning, "f5_ols": f_ols}


def build(only: list[str] | None = None) -> dict[str, dict[str, str]]:
    out = {}
    for name, fn in FIXTURES.items():
        if only and name not in only:
            continue
        out[name] = fn(random.Random(f"{SEED}:{name}"))
    return out


def main() -> None:
    built = build(None if len(sys.argv) < 2 else sys.argv[1:])
    manifest_path = ROOT / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    for name, files in built.items():
        d = ROOT / name
        d.mkdir(parents=True, exist_ok=True)
        for fname, text in files.items():
            (d / fname).write_text(text, encoding="utf-8")
        manifest[name] = {f: hashlib.sha256(t.encode()).hexdigest() for f, t in sorted(files.items())}
    manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    print("wrote", ", ".join(built))


if __name__ == "__main__":
    main()
