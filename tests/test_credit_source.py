"""Paying from a credit card — the source the picker used to hide.

A transfer to a legal entity (transfer-legal), to a person, or a bill CAN be funded
from a credit card; the app does it routinely. But two things made it impossible
through the MCP even though nothing in the Python actually forbids it:

  1. `_source_account()` / `ruble_source_accounts()` only ever return Current
     (debit) accounts, so a credit card was never the default AND never appeared in
     the «С какого счёта списать?» picker. The only way to reach it was to already
     know the account id and pass `from_account` by hand.
  2. A commission preview from a credit source comes back `unfinishedFlag: true`
     even for a correct, fully-specified transfer-legal — and the tool docstring
     told the agent to read that as «not a quote» and refuse.

Together: an agent asked to pay a legal-entity invoice from the credit card had no
offered way to select it and a written reason to give up. This file pins the fix.

What it checks:
  1. credit_source_accounts() selects Credit RUB accounts with limit left; and
     ruble_source_accounts() still excludes them (the DEFAULT must stay debit).
  2. The source picker (_resolve_source) OFFERS the credit card — marked — and
     returns it when the human chooses it.
  3. transfer_legal builds the SAME /v1/pay body from a credit account as from the
     capture-verified debit one — only `account` differs (the shape is reused, not
     guessed, for the credit source).
  4. `unfinishedFlag: true` from a credit-source commission does NOT block the
     payment: the chain reaches /v1/pay and returns the paymentId, debited from the
     credit account that was chosen.

    python3 tests/test_credit_source.py
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp()
os.environ.setdefault("TBANK_ATTEMPTS", os.path.join(_TMP, "attempts.jsonl"))
os.environ.setdefault("TBANK_EVENTS", os.path.join(_TMP, "events.jsonl"))
os.environ.setdefault("TBANK_TRACE_FILE", os.path.join(_TMP, "calls.jsonl"))

from elicit_fake import FakeCtx, accept_ctx               # noqa: E402
from tbank_mcp import server                              # noqa: E402
from tbank_mcp.client import MobileSession                # noqa: E402
from test_requisites import LegalSession, fixture, run_tool  # noqa: E402

# Synthetic account ids (the repo is public — never real numbers). The test's
# correctness depends only on accountType / currency / balance, not on the digits.
# The account record is shaped as accounts_light returns it: moneyAmount.value on a
# Credit account is the AVAILABLE credit, alongside creditLimit and debtAmount.
# Declared synthetic in tests/test_no_personal_data.py's ALLOWED set (the 001x
# series is this file's block there).
CREDIT_ID = "0000000011"
DEBIT_ID = "0000000010"

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def accounts():
    """A realistic mix: one debit RUB, a foreign-currency debit, the credit card,
    a foreign-currency credit, a maxed-out credit, and a cash loan."""
    def rub(v):
        return {"value": v, "currency": {"name": "RUB", "code": 643}}
    return [
        {"id": DEBIT_ID, "name": "Дебетовая RUB", "accountType": "Current",
         "currency": {"name": "RUB"}, "moneyAmount": rub(9000.0)},
        {"id": "0000000020", "name": "Дебетовая USD", "accountType": "Current",
         "currency": {"name": "USD"},
         "moneyAmount": {"value": 20.0, "currency": {"name": "USD"}}},
        {"id": CREDIT_ID, "name": "Кредитная RUB", "accountType": "Credit",
         "currency": {"name": "RUB"}, "moneyAmount": rub(200000.0),
         "creditLimit": rub(360000.0), "debtAmount": rub(160000.0)},
        {"id": "0000000030", "name": "Кредитная EUR", "accountType": "Credit",
         "currency": {"name": "EUR"},
         "moneyAmount": {"value": 100.0, "currency": {"name": "EUR"}}},
        {"id": "0000000040", "name": "Кредитка без лимита", "accountType": "Credit",
         "currency": {"name": "RUB"}, "moneyAmount": rub(0.0)},
        {"id": "0000000050", "name": "Ипотека", "accountType": "CashLoan",
         "currency": {"name": "RUB"}, "moneyAmount": rub(5.0)},
    ]


class AcctSession(MobileSession):
    """Just enough session to run the two source-selection methods for real."""

    def __init__(self, accts):
        self._accts = accts

    def list_accounts(self):
        return self._accts

    def ensure_fresh(self, *a, **kw):
        return None


def resolve_source(session, ctx, from_account):
    """Run the async _resolve_source with server._require patched to `session`."""
    saved = server._require
    server._require = lambda: session
    try:
        return asyncio.run(server._resolve_source(ctx, from_account))
    finally:
        server._require = saved


# ---- 1. which accounts each list returns ----------------------------------

def test_credit_list_selects_credit_and_debit_list_still_excludes_it():
    s = AcctSession(accounts())
    debit = s.ruble_source_accounts()
    credit = s.credit_source_accounts()

    check([a["id"] for a in debit] == [DEBIT_ID],
          f"ruble_source_accounts must be debit-RUB-only, got {[a['id'] for a in debit]}")
    check(all(not a.get("credit") for a in debit),
          "a debit-source entry must not be flagged credit")

    check([a["id"] for a in credit] == [CREDIT_ID],
          f"credit_source_accounts must be Credit-RUB-with-limit only, got "
          f"{[a['id'] for a in credit]} (foreign-currency, zero-limit and CashLoan "
          f"must all be excluded)")
    check(credit and credit[0].get("credit") is True,
          "a credit-source entry must carry credit=True so the picker can mark it")
    check(credit and credit[0]["balance"] == 200000.0,
          f"balance must be the AVAILABLE credit, got "
          f"{credit[0]['balance'] if credit else None}")
    print("  lists: credit card is a credit-source, never a debit-source; foreign/"
          "maxed/loan excluded")


# ---- 2. the picker offers the credit card ---------------------------------

def test_the_source_picker_offers_the_credit_card():
    s = AcctSession(accounts())
    # Debit is listed first, the credit card second — so pick=1 chooses it.
    ctx = FakeCtx(action="accept", pick=1)
    chosen, refusal = resolve_source(s, ctx, "")
    check(refusal is None, f"the picker refused instead of returning a choice: {refusal}")
    check(chosen == CREDIT_ID,
          f"choosing the second option must return the credit card, got {chosen!r}")

    # The offered options must actually contain the credit card, marked as credit.
    check(ctx.asked, "the picker never asked")
    if ctx.asked:
        _, schema = ctx.asked[0]
        enum = schema.model_json_schema()["properties"]["choice"]["enum"]
        credit_labels = [o for o in enum if CREDIT_ID in o]
        check(credit_labels, f"the credit card is not among the offered options: {enum}")
        check(any("КРЕДИТНАЯ" in o for o in credit_labels),
              f"the credit option must be marked as credit, got {credit_labels}")
    print("  picker: the credit card is offered, marked «КРЕДИТНАЯ», and selectable")


def test_a_single_debit_account_still_gets_no_picker():
    """One debit + no usable credit → the guess is right, no picker. The credit
    card must not turn a single-account user's silent default into a question."""
    only_debit = [a for a in accounts()
                  if a["accountType"] == "Current" and a["currency"]["name"] == "RUB"]
    s = AcctSession(only_debit)
    chosen, refusal = resolve_source(s, accept_ctx(), "")
    check((chosen, refusal) == ("", None),
          f"a lone debit account must fall through to the default, got {(chosen, refusal)}")
    print("  picker: a single debit account is still not bothered with a choice")


# ---- 3. the pay body from a credit source ---------------------------------

def test_transfer_legal_body_from_credit_matches_the_debit_capture():
    """The only capture-verified transfer-legal /v1/pay was debited from a Current
    account. Paying from a credit card must reuse that exact envelope and change
    ONLY `account` — not invent a credit-specific shape."""
    fx = fixture()
    s = LegalSession(fx)
    s.transfer_legal(fx["pay_parameters"]["moneyAmount"],
                     fx["pay_parameters"]["providerFields"],
                     account=CREDIT_ID,
                     user_payment_id=fx["pay_parameters"]["userPaymentId"],
                     from_qr=True)
    got = s.sent_pay_parameters()
    check(got.get("account") == CREDIT_ID,
          f"the pay body must debit the chosen credit account, got {got.get('account')!r}")
    check(sorted(got) == sorted(fx["pay_parameters"]),
          f"a credit source must not change the payParameters key set\n"
          f"    ours={sorted(got)}\n    capture={sorted(fx['pay_parameters'])}")
    check("paymentType" not in got,
          "paymentType belongs to the commission call, not /v1/pay — credit or not")
    print("  body: a credit source reuses the captured debit envelope, only account differs")


# ---- 4. unfinishedFlag:true from a credit source does not block ------------

def test_unfinished_flag_from_a_credit_source_does_not_block_the_payment():
    """A credit-source commission preview comes back unfinishedFlag:true with a
    zero fee and a valid total. That must NOT stop the payment: the recipient is
    resolved and the amount is positive, so the chain reaches /v1/pay."""
    fx = fixture()
    credit_quote = {
        "providerId": "transfer-legal",
        "description": "Комиссия не взимается",
        "value": {"value": 0, "currency": {"name": "RUB", "strCode": "643", "code": 643}},
        "minAmount": 0.01, "maxAmount": 1000000000.0,
        "total": {"value": 100, "currency": {"name": "RUB", "strCode": "643", "code": 643}},
        "unfinishedFlag": True, "externalFees": [], "additionalParameters": [],
    }
    open(os.environ["TBANK_ATTEMPTS"], "w").close()
    s = LegalSession(fx, commission=credit_quote)
    out = run_tool(s, server.transfer_requisites, amount=100, comment="Счет 918",
                   from_account=CREDIT_ID, ctx=accept_ctx(), **fx["tool_args"])
    check(s.body is not None,
          f"unfinishedFlag:true from a credit source blocked the pay body: {out}")
    check(fx["pay_response"]["paymentId"] in out,
          f"the payment must complete and return its paymentId: {out}")
    check(s.sent_pay_parameters().get("account") == CREDIT_ID,
          "the payment was debited from the wrong account")

    # And the commission the tool actually sent named the credit account, so the
    # preview it (correctly) ignored the flag on was the credit-source one.
    check((s.commission_body or {}).get("account") == CREDIT_ID,
          f"the commission preview was run for the wrong source: {s.commission_body}")
    print("  guard: a credit source's unfinishedFlag:true is not read as a refusal")


def main():
    print("paying from a credit card:")
    test_credit_list_selects_credit_and_debit_list_still_excludes_it()
    test_the_source_picker_offers_the_credit_card()
    test_a_single_debit_account_still_gets_no_picker()
    test_transfer_legal_body_from_credit_matches_the_debit_capture()
    test_unfinished_flag_from_a_credit_source_does_not_block_the_payment()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
