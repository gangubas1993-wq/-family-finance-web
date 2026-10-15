import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import uuid

import psycopg


DSN = os.environ["TEST_DATABASE_URL"]
FAMILY_ID = "2606ce94-b7fe-49fb-8ab3-68bfbbe4be86"
ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"
ACCOUNT2_ID = "00000000-0000-0000-0000-000000000002"
MIGRATION = Path(__file__).resolve().parents[1] / "supabase/migrations/20261015000100_atomic_transaction_guard.sql"

SCHEMA = """
DROP TABLE IF EXISTS public.transactions CASCADE;
DROP TABLE IF EXISTS public.accounts CASCADE;
CREATE TABLE public.accounts (
  id uuid PRIMARY KEY,
  family_id uuid NOT NULL,
  name text NOT NULL,
  currency text NOT NULL,
  opening_balance numeric(14,2) DEFAULT 0,
  is_active boolean DEFAULT true
);
CREATE TABLE public.transactions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  family_id uuid NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  type text NOT NULL,
  amount numeric(14,2) NOT NULL CHECK (amount > 0),
  currency text NOT NULL,
  account_from_id uuid,
  account_to_id uuid,
  destination_amount numeric(14,2),
  category_id uuid,
  comment text,
  source text NOT NULL DEFAULT 'manual',
  exchange_rate numeric,
  updated_at timestamptz DEFAULT now(),
  entered_by_telegram_id bigint,
  updated_by_telegram_id bigint
);
"""


class AtomicBalanceGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(SCHEMA)
            conn.execute(MIGRATION.read_text(encoding="utf-8"))

    def setUp(self):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("TRUNCATE public.transactions")
            conn.execute("DELETE FROM public.accounts")
            conn.execute(
                "INSERT INTO public.accounts(id,family_id,name,currency,opening_balance) VALUES (%s,%s,'Test TRY','TRY',100),(%s,%s,'Test RUB','RUB',0)",
                (ACCOUNT_ID, FAMILY_ID, ACCOUNT2_ID, FAMILY_ID),
            )

    def payload(self, amount, *, account=ACCOUNT_ID, kind="expense", source="web", destination=None):
        return {
            "family_id": FAMILY_ID,
            "type": kind,
            "amount": str(amount),
            "currency": "TRY" if account == ACCOUNT_ID else "RUB",
            "account_from_id": account if kind in ("expense", "transfer") else None,
            "account_to_id": ACCOUNT2_ID if kind == "transfer" else (account if kind == "income" else None),
            "destination_amount": str(destination) if destination is not None else None,
            "occurred_at": "2026-10-01T12:00:00+03:00",
            "source": source,
            "comment": "isolated test",
        }

    def attempt(self, amount, barrier=None, allow_negative=False, source="web"):
        try:
            with psycopg.connect(DSN) as conn:
                if barrier:
                    barrier.wait(timeout=5)
                conn.execute(
                    "SELECT public.apply_transaction_with_balance_guard(%s::jsonb,%s,NULL)",
                    (json.dumps(self.payload(amount, source=source)), allow_negative),
                )
            return "accepted"
        except psycopg.Error as exc:
            return "rejected:" + str(exc).splitlines()[0]

    def balance_and_count(self):
        with psycopg.connect(DSN) as conn:
            row = conn.execute("""
                SELECT a.opening_balance + COALESCE(SUM(
                  CASE
                    WHEN t.type='income' AND t.account_to_id=a.id THEN t.amount
                    WHEN t.type='expense' AND t.account_from_id=a.id THEN -t.amount
                    WHEN t.type='transfer' AND t.account_from_id=a.id THEN -t.amount
                    WHEN t.type='transfer' AND t.account_to_id=a.id THEN COALESCE(t.destination_amount,t.amount)
                    ELSE 0
                  END
                ),0), COUNT(t.id)
                FROM public.accounts a
                LEFT JOIN public.transactions t ON t.family_id=a.family_id AND (t.account_from_id=a.id OR t.account_to_id=a.id)
                WHERE a.id=%s
                GROUP BY a.id,a.opening_balance
            """, (ACCOUNT_ID,)).fetchone()
            return row[0], row[1]

    def test_expense_above_balance_rejected_without_write(self):
        result = self.attempt(100.01)
        self.assertTrue(result.startswith("rejected:INSUFFICIENT_BALANCE"), result)
        self.assertEqual(self.balance_and_count(), (100, 0))

    def test_exact_balance_allowed(self):
        self.assertEqual(self.attempt(100), "accepted")
        self.assertEqual(self.balance_and_count(), (0, 1))

    def test_concurrent_expenses_cannot_overspend(self):
        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.attempt(80, barrier), range(2)))
        self.assertEqual(results.count("accepted"), 1, results)
        self.assertEqual(sum(x.startswith("rejected:INSUFFICIENT_BALANCE") for x in results), 1, results)
        self.assertEqual(self.balance_and_count(), (20, 1))

    def test_non_positive_expense_rejected(self):
        result = self.attempt(0)
        self.assertTrue(result.startswith("rejected:amount must be positive"), result)
        self.assertEqual(self.balance_and_count(), (100, 0))

    def test_historical_expense_can_be_negative_and_tagged(self):
        self.assertEqual(self.attempt(150, allow_negative=True, source="web_history"), "accepted")
        self.assertEqual(self.balance_and_count(), (-50, 1))
        with psycopg.connect(DSN) as conn:
            self.assertEqual(conn.execute("SELECT source FROM public.transactions").fetchone()[0], "web_history")

    def test_telegram_force_is_explicit_and_tagged(self):
        self.assertEqual(self.attempt(150, allow_negative=True, source="telegram_force"), "accepted")
        self.assertEqual(self.balance_and_count(), (-50, 1))
        with psycopg.connect(DSN) as conn:
            self.assertEqual(conn.execute("SELECT source FROM public.transactions").fetchone()[0], "telegram_force")

    def test_update_rechecks_balance_excluding_its_own_old_row(self):
        with psycopg.connect(DSN) as conn:
            txid = conn.execute(
                "SELECT public.apply_transaction_with_balance_guard(%s::jsonb,false,NULL)",
                (json.dumps(self.payload(80)),),
            ).fetchone()[0]
        with psycopg.connect(DSN) as conn:
            result = conn.execute(
                "SELECT public.apply_transaction_with_balance_guard(%s::jsonb,false,%s)",
                (json.dumps(self.payload(90)), str(txid)),
            ).fetchone()[0]
        self.assertEqual(str(result), str(txid))
        self.assertEqual(self.balance_and_count(), (10, 1))

    def test_update_cannot_raise_expense_above_starting_balance(self):
        with psycopg.connect(DSN) as conn:
            txid = conn.execute(
                "SELECT public.apply_transaction_with_balance_guard(%s::jsonb,false,NULL)",
                (json.dumps(self.payload(80)),),
            ).fetchone()[0]
        with self.assertRaises(psycopg.Error):
            with psycopg.connect(DSN) as conn:
                conn.execute(
                    "SELECT public.apply_transaction_with_balance_guard(%s::jsonb,false,%s)",
                    (json.dumps(self.payload(100.01)), str(txid)),
                )
        self.assertEqual(self.balance_and_count(), (20, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
