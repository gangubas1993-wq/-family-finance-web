import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
import psycopg


DSN = os.environ["TEST_DATABASE_URL"]


SCHEMA = """
DROP SCHEMA IF EXISTS guard_test CASCADE;
CREATE SCHEMA guard_test;
CREATE TABLE guard_test.accounts (
    id integer PRIMARY KEY,
    opening_balance numeric(14,2) NOT NULL DEFAULT 0
);
CREATE TABLE guard_test.transactions (
    id bigserial PRIMARY KEY,
    account_id integer NOT NULL REFERENCES guard_test.accounts(id),
    amount numeric(14,2) NOT NULL CHECK (amount > 0),
    source text NOT NULL DEFAULT 'web'
);
CREATE OR REPLACE FUNCTION guard_test.try_expense(
    p_account_id integer,
    p_amount numeric,
    p_source text DEFAULT 'web'
)
RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
    v_opening numeric(14,2);
    v_spent numeric(14,2);
    v_balance numeric(14,2);
BEGIN
    IF p_amount IS NULL OR p_amount <= 0 THEN
        RAISE EXCEPTION 'amount must be positive';
    END IF;

    -- Explicit history / Telegram force paths are deliberate, recorded exceptions.
    IF p_source IN ('web_history', 'telegram_force') THEN
        INSERT INTO guard_test.transactions(account_id, amount, source)
        VALUES (p_account_id, p_amount, p_source);
        RETURN true;
    END IF;

    -- Serialize spending attempts for this account to avoid check-then-insert races.
    SELECT opening_balance INTO v_opening
    FROM guard_test.accounts
    WHERE id = p_account_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'account not found';
    END IF;

    SELECT COALESCE(SUM(amount), 0) INTO v_spent
    FROM guard_test.transactions
    WHERE account_id = p_account_id;

    v_balance := v_opening - v_spent;
    IF p_amount > v_balance THEN
        RAISE EXCEPTION 'insufficient funds: available %, requested %', v_balance, p_amount;
    END IF;

    INSERT INTO guard_test.transactions(account_id, amount, source)
    VALUES (p_account_id, p_amount, p_source);
    RETURN true;
END;
$$;
"""


class AtomicBalanceGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(SCHEMA)

    def setUp(self):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("TRUNCATE guard_test.transactions RESTART IDENTITY")
            conn.execute("DELETE FROM guard_test.accounts")
            conn.execute("INSERT INTO guard_test.accounts(id, opening_balance) VALUES (1, 100.00)")

    def balance_and_count(self):
        with psycopg.connect(DSN) as conn:
            row = conn.execute("""
                SELECT a.opening_balance - COALESCE(SUM(t.amount), 0),
                       COUNT(t.id)
                FROM guard_test.accounts a
                LEFT JOIN guard_test.transactions t ON t.account_id = a.id
                WHERE a.id = 1
                GROUP BY a.id, a.opening_balance
            """).fetchone()
            return row[0], row[1]

    def attempt(self, amount, barrier=None, source="web"):
        try:
            with psycopg.connect(DSN) as conn:
                if barrier:
                    barrier.wait(timeout=5)
                conn.execute("SELECT guard_test.try_expense(1, %s, %s)", (amount, source))
            return "accepted"
        except psycopg.Error as exc:
            return "rejected:" + str(exc).splitlines()[0]

    def test_expense_above_balance_is_rejected_without_write(self):
        result = self.attempt(100.01)
        self.assertTrue(result.startswith("rejected:"))
        self.assertEqual(self.balance_and_count(), (100, 0))

    def test_exact_balance_is_allowed(self):
        self.assertEqual(self.attempt(100), "accepted")
        self.assertEqual(self.balance_and_count(), (0, 1))

    def test_two_concurrent_expenses_cannot_overspend(self):
        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.attempt(80, barrier), range(2)))
        self.assertEqual(results.count("accepted"), 1, results)
        self.assertEqual(sum(result.startswith("rejected:") for result in results), 1, results)
        self.assertEqual(self.balance_and_count(), (20, 1))

    def test_non_positive_expense_is_rejected(self):
        result = self.attempt(0)
        self.assertTrue(result.startswith("rejected:"))
        self.assertEqual(self.balance_and_count(), (100, 0))

    def test_historical_expense_can_create_negative_balance_and_is_tagged(self):
        self.assertEqual(self.attempt(150, source="web_history"), "accepted")
        self.assertEqual(self.balance_and_count(), (-50, 1))
        with psycopg.connect(DSN) as conn:
            source = conn.execute("SELECT source FROM guard_test.transactions").fetchone()[0]
        self.assertEqual(source, "web_history")

    def test_telegram_explicit_override_is_recorded_as_exception(self):
        self.assertEqual(self.attempt(150, source="telegram_force"), "accepted")
        self.assertEqual(self.balance_and_count(), (-50, 1))
        with psycopg.connect(DSN) as conn:
            source = conn.execute("SELECT source FROM guard_test.transactions").fetchone()[0]
        self.assertEqual(source, "telegram_force")


if __name__ == "__main__":
    unittest.main(verbosity=2)
