-- ===========================================================================
-- MoA 문서 변환기 – Supabase SQL Schema
-- ===========================================================================
--
-- Supabase SQL Editor에서 이 스크립트를 실행하세요.
-- Supabase Auth (auth.users)와 연동되는 public.user_profiles 테이블과
-- 크레딧/결제 관련 테이블을 생성합니다.
--
-- 실행 방법:
--   1. Supabase 대시보드 > SQL Editor
--   2. 이 파일 내용을 붙여넣기
--   3. Run 클릭
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 1. 사용자 프로필 테이블 (회원정보)
-- ---------------------------------------------------------------------------
-- Supabase Auth의 auth.users.id를 참조합니다.
-- 회원가입 시 자동으로 프로필 행이 생성됩니다.

CREATE TABLE IF NOT EXISTS public.user_profiles (
    id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    nationality TEXT NOT NULL DEFAULT '',
    gender TEXT NOT NULL DEFAULT '' CHECK (gender IN ('', 'male', 'female', 'other')),
    birth_date DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 인덱스
CREATE INDEX IF NOT EXISTS idx_user_profiles_email ON public.user_profiles(email);
CREATE INDEX IF NOT EXISTS idx_user_profiles_nationality ON public.user_profiles(nationality);

-- updated_at 자동 갱신 트리거
CREATE OR REPLACE FUNCTION public.update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_user_profiles_updated_at ON public.user_profiles;
CREATE TRIGGER trg_user_profiles_updated_at
    BEFORE UPDATE ON public.user_profiles
    FOR EACH ROW
    EXECUTE FUNCTION public.update_updated_at();

-- ---------------------------------------------------------------------------
-- 2. 회원가입 시 자동 프로필 생성 트리거
-- ---------------------------------------------------------------------------
-- Supabase Auth에서 새 사용자가 생성되면 user_profiles 행을 자동 삽입합니다.

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO public.user_profiles (id, email)
    VALUES (NEW.id, NEW.email)
    ON CONFLICT (id) DO NOTHING;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW
    EXECUTE FUNCTION public.handle_new_user();

-- ---------------------------------------------------------------------------
-- 3. 크레딧 계정 테이블
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.credit_accounts (
    user_id UUID PRIMARY KEY REFERENCES public.user_profiles(id) ON DELETE CASCADE,
    balance_usd NUMERIC(12, 6) NOT NULL DEFAULT 0.0,
    total_purchased_usd NUMERIC(12, 6) NOT NULL DEFAULT 0.0,
    total_consumed_usd NUMERIC(12, 6) NOT NULL DEFAULT 0.0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_credit_accounts_updated_at ON public.credit_accounts;
CREATE TRIGGER trg_credit_accounts_updated_at
    BEFORE UPDATE ON public.credit_accounts
    FOR EACH ROW
    EXECUTE FUNCTION public.update_updated_at();

-- ---------------------------------------------------------------------------
-- 4. 크레딧 사용 내역 테이블
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.credit_history (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES public.user_profiles(id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK (type IN ('purchase', 'usage', 'refund')),
    amount_usd NUMERIC(12, 6) NOT NULL,
    balance_after NUMERIC(12, 6) NOT NULL,
    num_pages INT,
    doc_type TEXT,            -- 'image_pdf' | 'digital_pdf' | 'other'
    description TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_credit_history_user_id ON public.credit_history(user_id);
CREATE INDEX IF NOT EXISTS idx_credit_history_created_at ON public.credit_history(created_at DESC);

-- ---------------------------------------------------------------------------
-- 5. 결제 기록 테이블
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.payments (
    id TEXT PRIMARY KEY,                  -- internal payment_id
    user_id UUID NOT NULL REFERENCES public.user_profiles(id) ON DELETE CASCADE,
    gateway TEXT NOT NULL CHECK (gateway IN ('stripe', 'toss')),
    gateway_payment_id TEXT DEFAULT '',   -- Stripe session_id / Toss paymentKey
    amount_usd NUMERIC(12, 6) NOT NULL DEFAULT 0.0,
    amount_krw INT NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'usd',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed', 'failed', 'cancelled')),
    method TEXT DEFAULT '',               -- card, kakaopay, naverpay, etc.
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_payments_user_id ON public.payments(user_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON public.payments(status);
CREATE INDEX IF NOT EXISTS idx_payments_created_at ON public.payments(created_at DESC);

-- ---------------------------------------------------------------------------
-- 6. Row Level Security (RLS) 정책
-- ---------------------------------------------------------------------------
-- 사용자는 자신의 데이터만 읽기/수정 가능

ALTER TABLE public.user_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.credit_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.credit_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.payments ENABLE ROW LEVEL SECURITY;

-- user_profiles: 본인만 조회/수정
CREATE POLICY "Users can view own profile"
    ON public.user_profiles FOR SELECT
    USING (auth.uid() = id);

CREATE POLICY "Users can update own profile"
    ON public.user_profiles FOR UPDATE
    USING (auth.uid() = id)
    WITH CHECK (auth.uid() = id);

-- credit_accounts: 본인만 조회 (수정은 서버에서만)
CREATE POLICY "Users can view own credits"
    ON public.credit_accounts FOR SELECT
    USING (auth.uid() = user_id);

-- credit_history: 본인만 조회
CREATE POLICY "Users can view own credit history"
    ON public.credit_history FOR SELECT
    USING (auth.uid() = user_id);

-- payments: 본인만 조회
CREATE POLICY "Users can view own payments"
    ON public.payments FOR SELECT
    USING (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- 7. 서비스 역할 정책 (서버 백엔드용)
-- ---------------------------------------------------------------------------
-- service_role 키를 사용하는 백엔드 서버는 모든 테이블에 접근 가능합니다.
-- Supabase의 service_role은 기본적으로 RLS를 우회합니다.

-- ---------------------------------------------------------------------------
-- 완료!
-- ---------------------------------------------------------------------------
-- 이 스크립트 실행 후 확인 사항:
-- 1. Supabase 대시보드 > Authentication > Settings에서 이메일 인증 설정
-- 2. Railway 환경변수에 SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_KEY 추가
-- 3. UPSTAGE_API_KEY는 Railway 환경변수로만 설정 (사용자 접근 불가)
