-- Atomic transaction write with a row lock per affected account.
-- This migration is intended to be applied only after the isolated PostgreSQL tests pass.
CREATE OR REPLACE FUNCTION public.apply_transaction_with_balance_guard(
  p_payload jsonb,
  p_allow_negative boolean DEFAULT false,
  p_existing_id uuid DEFAULT NULL
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
DECLARE
  v_family_id uuid := (p_payload->>'family_id')::uuid;
  v_type text := p_payload->>'type';
  v_amount numeric := (p_payload->>'amount')::numeric;
  v_currency text := upper(p_payload->>'currency');
  v_from_id uuid := nullif(p_payload->>'account_from_id','')::uuid;
  v_to_id uuid := nullif(p_payload->>'account_to_id','')::uuid;
  v_destination_amount numeric := nullif(p_payload->>'destination_amount','')::numeric;
  v_opening numeric;
  v_balance numeric;
  v_source_currency text;
  v_destination_currency text;
  v_id uuid;
  v_old_from uuid;
  v_old_to uuid;
  v_old_family uuid;
  v_account record;
BEGIN
  IF v_family_id IS NULL THEN
    RAISE EXCEPTION 'family_id is required';
  END IF;
  IF v_type NOT IN ('expense','income','transfer') THEN
    RAISE EXCEPTION 'invalid transaction type';
  END IF;
  IF v_amount IS NULL OR v_amount <= 0 THEN
    RAISE EXCEPTION 'amount must be positive';
  END IF;
  IF v_currency NOT IN ('TRY','RUB') THEN
    RAISE EXCEPTION 'currency must be TRY or RUB';
  END IF;

  IF p_existing_id IS NOT NULL THEN
    SELECT t.family_id, t.account_from_id, t.account_to_id
      INTO v_old_family, v_old_from, v_old_to
    FROM public.transactions t
    WHERE t.id = p_existing_id
    FOR UPDATE;
    IF NOT FOUND OR v_old_family IS DISTINCT FROM v_family_id THEN
      RAISE EXCEPTION 'transaction not found';
    END IF;
  END IF;

  -- Lock every affected account in stable order to prevent concurrent overspending
  -- and avoid deadlocks when a transfer touches two accounts.
  FOR v_account IN
    SELECT a.id
    FROM public.accounts a
    WHERE a.family_id = v_family_id
      AND a.id = ANY(array_remove(ARRAY[v_from_id,v_to_id,v_old_from,v_old_to]::uuid[], NULL))
    ORDER BY a.id
    FOR UPDATE
  LOOP
    NULL;
  END LOOP;

  IF v_type IN ('expense','transfer') THEN
    IF v_from_id IS NULL THEN
      RAISE EXCEPTION 'source account is required';
    END IF;

    SELECT a.opening_balance, a.currency
      INTO v_opening, v_source_currency
    FROM public.accounts a
    WHERE a.id = v_from_id AND a.family_id = v_family_id;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'source account not found';
    END IF;
    IF v_source_currency <> v_currency THEN
      RAISE EXCEPTION 'source account currency mismatch';
    END IF;

    SELECT COALESCE(v_opening,0) + COALESCE(SUM(
      CASE
        WHEN t.type='income' AND t.account_to_id=v_from_id THEN t.amount
        WHEN t.type='expense' AND t.account_from_id=v_from_id THEN -t.amount
        WHEN t.type='transfer' AND t.account_from_id=v_from_id THEN -t.amount
        WHEN t.type='transfer' AND t.account_to_id=v_from_id THEN COALESCE(t.destination_amount,t.amount)
        ELSE 0
      END
    ),0)
    INTO v_balance
    FROM public.transactions t
    WHERE t.family_id=v_family_id
      AND (t.account_from_id=v_from_id OR t.account_to_id=v_from_id)
      AND (p_existing_id IS NULL OR t.id<>p_existing_id);

    IF NOT p_allow_negative AND v_amount > v_balance THEN
      RAISE EXCEPTION 'INSUFFICIENT_BALANCE: available %, requested %', v_balance, v_amount;
    END IF;
  END IF;

  IF v_type='income' THEN
    IF v_to_id IS NULL THEN RAISE EXCEPTION 'destination account is required'; END IF;
    IF NOT EXISTS (
      SELECT 1 FROM public.accounts a
      WHERE a.id=v_to_id AND a.family_id=v_family_id AND a.currency=v_currency
    ) THEN RAISE EXCEPTION 'destination account not found or currency mismatch'; END IF;
  ELSIF v_type='transfer' THEN
    IF v_to_id IS NULL OR v_to_id=v_from_id THEN RAISE EXCEPTION 'transfer needs two different accounts'; END IF;
    IF NOT EXISTS (SELECT 1 FROM public.accounts a WHERE a.id=v_to_id AND a.family_id=v_family_id) THEN
      RAISE EXCEPTION 'destination account not found';
    END IF;
    IF v_destination_amount IS NULL OR v_destination_amount<=0 THEN
      SELECT a.currency INTO v_destination_currency FROM public.accounts a WHERE a.id=v_to_id AND a.family_id=v_family_id;
      IF v_destination_currency <> v_currency THEN
        RAISE EXCEPTION 'destination amount is required for cross-currency transfer';
      END IF;
      v_destination_amount := v_amount;
    END IF;
  END IF;

  IF p_existing_id IS NULL THEN
    INSERT INTO public.transactions(
      family_id, occurred_at, type, amount, currency,
      account_from_id, account_to_id, destination_amount,
      category_id, comment, source, exchange_rate, updated_at,
      entered_by_telegram_id, updated_by_telegram_id
    ) VALUES (
      v_family_id,
      COALESCE(nullif(p_payload->>'occurred_at','')::timestamptz, now()),
      v_type, v_amount, v_currency,
      CASE WHEN v_type='income' THEN NULL ELSE v_from_id END,
      CASE WHEN v_type='expense' THEN NULL ELSE v_to_id END,
      CASE WHEN v_type='transfer' THEN COALESCE(v_destination_amount,v_amount) ELSE NULL END,
      nullif(p_payload->>'category_id','')::uuid,
      nullif(p_payload->>'comment',''),
      COALESCE(nullif(p_payload->>'source',''),'manual'),
      nullif(p_payload->>'exchange_rate','')::numeric,
      now(),
      nullif(p_payload->>'entered_by_telegram_id','')::bigint,
      nullif(p_payload->>'updated_by_telegram_id','')::bigint
    ) RETURNING id INTO v_id;
  ELSE
    UPDATE public.transactions SET
      occurred_at=COALESCE(nullif(p_payload->>'occurred_at','')::timestamptz, occurred_at),
      type=v_type, amount=v_amount, currency=v_currency,
      account_from_id=CASE WHEN v_type='income' THEN NULL ELSE v_from_id END,
      account_to_id=CASE WHEN v_type='expense' THEN NULL ELSE v_to_id END,
      destination_amount=CASE WHEN v_type='transfer' THEN COALESCE(v_destination_amount,v_amount) ELSE NULL END,
      category_id=nullif(p_payload->>'category_id','')::uuid,
      comment=nullif(p_payload->>'comment',''),
      source=COALESCE(nullif(p_payload->>'source',''),source),
      exchange_rate=nullif(p_payload->>'exchange_rate','')::numeric,
      updated_at=now()
    WHERE id=p_existing_id AND family_id=v_family_id
    RETURNING id INTO v_id;
  END IF;

  RETURN v_id;
END;
$function$;

REVOKE ALL ON FUNCTION public.apply_transaction_with_balance_guard(jsonb,boolean,uuid) FROM PUBLIC;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN
    EXECUTE 'REVOKE ALL ON FUNCTION public.apply_transaction_with_balance_guard(jsonb,boolean,uuid) FROM anon';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
    EXECUTE 'REVOKE ALL ON FUNCTION public.apply_transaction_with_balance_guard(jsonb,boolean,uuid) FROM authenticated';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN
    EXECUTE 'GRANT EXECUTE ON FUNCTION public.apply_transaction_with_balance_guard(jsonb,boolean,uuid) TO service_role';
  END IF;
END $$;
