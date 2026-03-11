-- ===========================================================================
-- MoA 문서 변환기 – Supabase SQL Schema (Complete)
-- ===========================================================================
--
-- Supabase SQL Editor에서 이 스크립트를 실행하세요.
--
-- 포함 내용:
--   1. user_profiles      – 회원 프로필 (Supabase Auth 연동)
--   2. credit_accounts    – 크레딧 잔액
--   3. credit_history     – 크레딧 사용/충전 내역
--   4. payments           – 결제 기록 (Stripe / Toss)
--   5. conversion_jobs    – 문서 변환 작업 이력
--   6. exchange_rates     – KRW/USD 환율 캐시
--   7. RLS 정책           – Row Level Security
--   8. RPC 함수           – 서버에서 호출하는 원자적 크레딧 연산
--   9. 자동 트리거         – 회원가입 시 프로필/크레딧 계정 자동 생성
--
-- 실행 방법:
--   1. Supabase 대시보드 > SQL Editor
--   2. 이 파일 내용을 붙여넣기
--   3. Run 클릭
-- ===========================================================================


-- ===========================================================================
-- 0. 유틸리티 함수
-- ===========================================================================

-- updated_at 자동 갱신 트리거 함수
CREATE OR REPLACE FUNCTION public.update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- ===========================================================================
-- 1. 사용자 프로필 테이블 (회원정보)
-- ===========================================================================
-- Supabase Auth의 auth.users.id를 참조합니다.

CREATE TABLE IF NOT EXISTS public.user_profiles (
    id              UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email           TEXT UNIQUE NOT NULL,
    display_name    TEXT NOT NULL DEFAULT '',
    phone           TEXT NOT NULL DEFAULT '',
    nationality     TEXT NOT NULL DEFAULT '',
    gender          TEXT NOT NULL DEFAULT ''
                    CHECK (gender IN ('', 'male', 'female', 'other')),
    birth_date      DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 인덱스
CREATE INDEX IF NOT EXISTS idx_user_profiles_email
    ON public.user_profiles(email);
CREATE INDEX IF NOT EXISTS idx_user_profiles_nationality
    ON public.user_profiles(nationality);

-- updated_at 트리거
DROP TRIGGER IF EXISTS trg_user_profiles_updated_at ON public.user_profiles;
CREATE TRIGGER trg_user_profiles_updated_at
    BEFORE UPDATE ON public.user_profiles
    FOR EACH ROW
    EXECUTE FUNCTION public.update_updated_at();


-- ===========================================================================
-- 2. 회원가입 시 자동 프로필 + 크레딧 계정 생성 트리거
-- ===========================================================================

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
    -- 프로필 생성
    INSERT INTO public.user_profiles (id, email, display_name)
    VALUES (
        NEW.id,
        NEW.email,
        COALESCE(NEW.raw_user_meta_data ->> 'display_name', split_part(NEW.email, '@', 1))
    )
    ON CONFLICT (id) DO NOTHING;

    -- 크레딧 계정 생성 (잔액 0)
    INSERT INTO public.credit_accounts (user_id)
    VALUES (NEW.id)
    ON CONFLICT (user_id) DO NOTHING;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW
    EXECUTE FUNCTION public.handle_new_user();


-- ===========================================================================
-- 3. 크레딧 계정 테이블
-- ===========================================================================

CREATE TABLE IF NOT EXISTS public.credit_accounts (
    user_id             UUID PRIMARY KEY REFERENCES public.user_profiles(id) ON DELETE CASCADE,
    balance_usd         NUMERIC(12, 6) NOT NULL DEFAULT 0.0
                        CHECK (balance_usd >= 0),
    total_purchased_usd NUMERIC(12, 6) NOT NULL DEFAULT 0.0,
    total_consumed_usd  NUMERIC(12, 6) NOT NULL DEFAULT 0.0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_credit_accounts_updated_at ON public.credit_accounts;
CREATE TRIGGER trg_credit_accounts_updated_at
    BEFORE UPDATE ON public.credit_accounts
    FOR EACH ROW
    EXECUTE FUNCTION public.update_updated_at();


-- ===========================================================================
-- 4. 크레딧 사용 내역 테이블
-- ===========================================================================

CREATE TABLE IF NOT EXISTS public.credit_history (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES public.user_profiles(id) ON DELETE CASCADE,
    type            TEXT NOT NULL CHECK (type IN ('purchase', 'usage', 'refund', 'bonus')),
    amount_usd      NUMERIC(12, 6) NOT NULL,   -- 양수: 충전/환불, 음수: 사용
    balance_after   NUMERIC(12, 6) NOT NULL,
    num_pages       INT,                         -- 변환 시 페이지 수
    doc_type        TEXT,                         -- 'image_pdf' | 'digital_pdf' | 'other'
    job_id          UUID,                         -- 연결된 변환 작업 ID (nullable)
    description     TEXT DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_credit_history_user_id
    ON public.credit_history(user_id);
CREATE INDEX IF NOT EXISTS idx_credit_history_created_at
    ON public.credit_history(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_credit_history_type
    ON public.credit_history(type);


-- ===========================================================================
-- 5. 결제 기록 테이블
-- ===========================================================================

CREATE TABLE IF NOT EXISTS public.payments (
    id                  TEXT PRIMARY KEY,                  -- internal payment_id
    user_id             UUID NOT NULL REFERENCES public.user_profiles(id) ON DELETE CASCADE,
    gateway             TEXT NOT NULL CHECK (gateway IN ('stripe', 'toss')),
    gateway_payment_id  TEXT DEFAULT '',                   -- Stripe session_id / Toss paymentKey
    amount_usd          NUMERIC(12, 6) NOT NULL DEFAULT 0.0,
    amount_krw          INT NOT NULL DEFAULT 0,
    currency            TEXT NOT NULL DEFAULT 'usd'
                        CHECK (currency IN ('usd', 'krw')),
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'completed', 'failed', 'cancelled', 'refunded')),
    method              TEXT DEFAULT '',                   -- card, kakaopay, naverpay, etc.
    metadata            JSONB DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_payments_user_id
    ON public.payments(user_id);
CREATE INDEX IF NOT EXISTS idx_payments_status
    ON public.payments(status);
CREATE INDEX IF NOT EXISTS idx_payments_created_at
    ON public.payments(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_payments_gateway
    ON public.payments(gateway);


-- ===========================================================================
-- 6. 문서 변환 작업 이력 테이블
-- ===========================================================================

CREATE TABLE IF NOT EXISTS public.conversion_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES public.user_profiles(id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'cancelled')),
    -- 입력 정보
    file_name       TEXT NOT NULL DEFAULT '',
    file_extension  TEXT NOT NULL DEFAULT '',
    file_size_bytes BIGINT DEFAULT 0,
    num_pages       INT DEFAULT 0,
    doc_type        TEXT NOT NULL DEFAULT 'image_pdf'
                    CHECK (doc_type IN ('image_pdf', 'digital_pdf', 'docx', 'hwpx', 'xlsx', 'pptx', 'other')),
    -- 출력 설정
    output_formats  TEXT[] NOT NULL DEFAULT ARRAY['html', 'markdown'],
    translate       BOOLEAN NOT NULL DEFAULT false,
    source_language TEXT DEFAULT '',
    target_language TEXT DEFAULT 'ko',
    -- 처리 결과
    engine          TEXT DEFAULT '',                       -- 'upstage_gemini' | 'hancom' | 'native'
    progress        REAL DEFAULT 0.0,                      -- 0.0 ~ 1.0
    output_files    TEXT[] DEFAULT ARRAY[]::TEXT[],         -- 결과 파일 경로/URL 목록
    error_message   TEXT DEFAULT '',
    -- 비용
    cost_usd        NUMERIC(12, 6) DEFAULT 0.0,
    -- 타임스탬프
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_conversion_jobs_user_id
    ON public.conversion_jobs(user_id);
CREATE INDEX IF NOT EXISTS idx_conversion_jobs_status
    ON public.conversion_jobs(status);
CREATE INDEX IF NOT EXISTS idx_conversion_jobs_created_at
    ON public.conversion_jobs(created_at DESC);


-- ===========================================================================
-- 7. 환율 캐시 테이블
-- ===========================================================================

CREATE TABLE IF NOT EXISTS public.exchange_rates (
    currency_pair   TEXT PRIMARY KEY,    -- e.g. 'USD_KRW'
    rate            NUMERIC(12, 4) NOT NULL,
    source          TEXT DEFAULT '',     -- e.g. 'open.er-api.com'
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 환율 조회/업데이트 함수 (서버에서 호출)
CREATE OR REPLACE FUNCTION public.upsert_exchange_rate(
    p_pair TEXT,
    p_rate NUMERIC,
    p_source TEXT DEFAULT ''
)
RETURNS void AS $$
BEGIN
    INSERT INTO public.exchange_rates (currency_pair, rate, source, fetched_at)
    VALUES (p_pair, p_rate, p_source, now())
    ON CONFLICT (currency_pair)
    DO UPDATE SET rate = p_rate, source = p_source, fetched_at = now();
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;


-- ===========================================================================
-- 8. RPC 함수 – 원자적 크레딧 연산 (서버 백엔드에서 service_role로 호출)
-- ===========================================================================

-- ───────────────────────────────────────────────────────────────────────────
-- 8-1. 크레딧 충전 (결제 완료 후 호출)
-- ───────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.add_credits(
    p_user_id UUID,
    p_amount_usd NUMERIC,
    p_description TEXT DEFAULT '크레딧 충전'
)
RETURNS TABLE(new_balance NUMERIC) AS $$
DECLARE
    v_balance NUMERIC;
BEGIN
    -- 크레딧 계정이 없으면 생성
    INSERT INTO public.credit_accounts (user_id)
    VALUES (p_user_id)
    ON CONFLICT (user_id) DO NOTHING;

    -- 잔액 업데이트 (원자적)
    UPDATE public.credit_accounts
    SET balance_usd = balance_usd + p_amount_usd,
        total_purchased_usd = total_purchased_usd + p_amount_usd
    WHERE user_id = p_user_id
    RETURNING balance_usd INTO v_balance;

    -- 이력 기록
    INSERT INTO public.credit_history (user_id, type, amount_usd, balance_after, description)
    VALUES (p_user_id, 'purchase', p_amount_usd, v_balance, p_description);

    RETURN QUERY SELECT v_balance;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;


-- ───────────────────────────────────────────────────────────────────────────
-- 8-2. 크레딧 차감 (변환 작업 시 호출)
-- ───────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.debit_credits(
    p_user_id UUID,
    p_amount_usd NUMERIC,
    p_num_pages INT DEFAULT NULL,
    p_doc_type TEXT DEFAULT NULL,
    p_job_id UUID DEFAULT NULL,
    p_description TEXT DEFAULT '문서 변환'
)
RETURNS TABLE(new_balance NUMERIC, success BOOLEAN) AS $$
DECLARE
    v_balance NUMERIC;
    v_current NUMERIC;
BEGIN
    -- 현재 잔액 확인 (FOR UPDATE로 행 잠금)
    SELECT balance_usd INTO v_current
    FROM public.credit_accounts
    WHERE user_id = p_user_id
    FOR UPDATE;

    -- 잔액 부족 시 실패 반환
    IF v_current IS NULL OR v_current < p_amount_usd THEN
        RETURN QUERY SELECT COALESCE(v_current, 0::NUMERIC), false;
        RETURN;
    END IF;

    -- 잔액 차감
    UPDATE public.credit_accounts
    SET balance_usd = balance_usd - p_amount_usd,
        total_consumed_usd = total_consumed_usd + p_amount_usd
    WHERE user_id = p_user_id
    RETURNING balance_usd INTO v_balance;

    -- 이력 기록
    INSERT INTO public.credit_history
        (user_id, type, amount_usd, balance_after, num_pages, doc_type, job_id, description)
    VALUES
        (p_user_id, 'usage', -p_amount_usd, v_balance, p_num_pages, p_doc_type, p_job_id, p_description);

    RETURN QUERY SELECT v_balance, true;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;


-- ───────────────────────────────────────────────────────────────────────────
-- 8-3. 크레딧 환불
-- ───────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.refund_credits(
    p_user_id UUID,
    p_amount_usd NUMERIC,
    p_job_id UUID DEFAULT NULL,
    p_description TEXT DEFAULT '크레딧 환불'
)
RETURNS TABLE(new_balance NUMERIC) AS $$
DECLARE
    v_balance NUMERIC;
BEGIN
    UPDATE public.credit_accounts
    SET balance_usd = balance_usd + p_amount_usd,
        total_consumed_usd = total_consumed_usd - p_amount_usd
    WHERE user_id = p_user_id
    RETURNING balance_usd INTO v_balance;

    INSERT INTO public.credit_history
        (user_id, type, amount_usd, balance_after, job_id, description)
    VALUES
        (p_user_id, 'refund', p_amount_usd, v_balance, p_job_id, p_description);

    RETURN QUERY SELECT v_balance;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;


-- ───────────────────────────────────────────────────────────────────────────
-- 8-4. 변환 비용 추산 (클라이언트에서도 호출 가능)
-- ───────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.estimate_conversion_cost(
    p_num_pages INT,
    p_doc_type TEXT DEFAULT 'image_pdf'
)
RETURNS TABLE(per_page_usd NUMERIC, total_usd NUMERIC) AS $$
DECLARE
    v_per_page NUMERIC;
BEGIN
    v_per_page := CASE p_doc_type
        WHEN 'image_pdf'   THEN 0.02
        WHEN 'digital_pdf' THEN 0.005
        ELSE 0.0
    END;

    RETURN QUERY SELECT v_per_page, v_per_page * p_num_pages;
END;
$$ LANGUAGE plpgsql STABLE;


-- ===========================================================================
-- 9. Row Level Security (RLS) 정책
-- ===========================================================================

-- ── RLS 활성화 ──
ALTER TABLE public.user_profiles    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.credit_accounts  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.credit_history   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.payments         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.conversion_jobs  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.exchange_rates   ENABLE ROW LEVEL SECURITY;

-- ── user_profiles: 본인만 조회/수정 ──
DROP POLICY IF EXISTS "Users can view own profile" ON public.user_profiles;
CREATE POLICY "Users can view own profile"
    ON public.user_profiles FOR SELECT
    USING (auth.uid() = id);

DROP POLICY IF EXISTS "Users can update own profile" ON public.user_profiles;
CREATE POLICY "Users can update own profile"
    ON public.user_profiles FOR UPDATE
    USING (auth.uid() = id)
    WITH CHECK (auth.uid() = id);

-- ── credit_accounts: 본인만 조회 (수정은 서버 RPC만) ──
DROP POLICY IF EXISTS "Users can view own credits" ON public.credit_accounts;
CREATE POLICY "Users can view own credits"
    ON public.credit_accounts FOR SELECT
    USING (auth.uid() = user_id);

-- ── credit_history: 본인만 조회 ──
DROP POLICY IF EXISTS "Users can view own credit history" ON public.credit_history;
CREATE POLICY "Users can view own credit history"
    ON public.credit_history FOR SELECT
    USING (auth.uid() = user_id);

-- ── payments: 본인만 조회 ──
DROP POLICY IF EXISTS "Users can view own payments" ON public.payments;
CREATE POLICY "Users can view own payments"
    ON public.payments FOR SELECT
    USING (auth.uid() = user_id);

-- ── conversion_jobs: 본인만 조회 ──
DROP POLICY IF EXISTS "Users can view own jobs" ON public.conversion_jobs;
CREATE POLICY "Users can view own jobs"
    ON public.conversion_jobs FOR SELECT
    USING (auth.uid() = user_id);

-- ── exchange_rates: 모든 인증 사용자 조회 가능 ──
DROP POLICY IF EXISTS "Authenticated users can view rates" ON public.exchange_rates;
CREATE POLICY "Authenticated users can view rates"
    ON public.exchange_rates FOR SELECT
    USING (auth.role() = 'authenticated');


-- ===========================================================================
-- 10. 서비스 역할 정책 안내
-- ===========================================================================
-- Supabase의 service_role 키를 사용하는 백엔드 서버는 기본적으로 RLS를
-- 우회하므로 별도 정책이 필요 없습니다.
--
-- 서버 백엔드에서는 다음 RPC 함수를 사용합니다:
--   - add_credits(user_id, amount_usd, description)
--   - debit_credits(user_id, amount_usd, num_pages, doc_type, job_id, description)
--   - refund_credits(user_id, amount_usd, job_id, description)
--   - estimate_conversion_cost(num_pages, doc_type)
--   - upsert_exchange_rate(pair, rate, source)


-- ===========================================================================
-- 완료!
-- ===========================================================================
-- 이 스크립트 실행 후 확인 사항:
--
-- 1. Supabase 대시보드 > Authentication > Settings
--    - Email 인증 활성화
--    - (선택) Google/GitHub OAuth 설정
--
-- 2. Supabase 대시보드 > Settings > API
--    - anon key, service_role key 복사
--
-- 3. Railway/서버 환경변수 설정:
--    - SUPABASE_URL          = https://<project>.supabase.co
--    - SUPABASE_ANON_KEY     = eyJ...
--    - SUPABASE_SERVICE_KEY  = eyJ... (서버 전용, 절대 클라이언트에 노출 금지)
--    - UPSTAGE_API_KEY       = (서버 전용)
--    - GEMINI_API_KEY        = (서버 전용)
--    - STRIPE_SECRET_KEY     = sk_live_...
--    - STRIPE_WEBHOOK_SECRET = whsec_...
--    - TOSS_CLIENT_KEY       = (토스 클라이언트 키)
--    - TOSS_SECRET_KEY       = (토스 시크릿 키)
--
-- 4. 테이블 확인 쿼리:
--    SELECT table_name FROM information_schema.tables
--    WHERE table_schema = 'public' ORDER BY table_name;
