"""Simplified double-entry ledger: posting rules and balance checks."""
import pandas as pd

POSTING_RULES = pd.DataFrame([
    {"event": "settlement", "trigger": "Payment settles", "debit": "customer", "credit": "settlement"},
    {"event": "fee", "trigger": "Fee charged", "debit": "customer", "credit": "fee_income"},
    {"event": "return", "trigger": "Payment returned", "debit": "settlement", "credit": "customer"},
    {"event": "write_off", "trigger": "Fraud loss written off", "debit": "fraud_loss", "credit": "settlement"},
])

UNBALANCED_SQL = """
SELECT payment_id,
       round(sum(debit_gbp), 2)  AS total_debits,
       round(sum(credit_gbp), 2) AS total_credits,
       round(sum(debit_gbp) - sum(credit_gbp), 2) AS difference
FROM ledger_entries
GROUP BY payment_id
HAVING abs(sum(debit_gbp) - sum(credit_gbp)) > 0.005
ORDER BY abs(difference) DESC
"""

TRIAL_BALANCE_SQL = """
SELECT account,
       round(sum(debit_gbp), 2)  AS debits,
       round(sum(credit_gbp), 2) AS credits,
       round(sum(debit_gbp) - sum(credit_gbp), 2) AS net
FROM ledger_entries
GROUP BY account
ORDER BY account
"""


def unbalanced(con):
    return con.execute(UNBALANCED_SQL).df()


def trial_balance(con):
    return con.execute(TRIAL_BALANCE_SQL).df()


def postings_for(con, payment_id):
    return con.execute("SELECT * FROM ledger_entries WHERE payment_id = ? ORDER BY posted_at, entry_id",
                       [payment_id]).df()
