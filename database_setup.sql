-- 維大力體育APP Restart：只執行一次。
-- 使用 Supabase SQL Editor 執行；不需要也不應把資料庫密碼貼入此檔。
create table if not exists public.vd_daily_snapshots (
  sport text not null check (sport in ('mlb', 'football')),
  date_key date not null,
  automatic_payload jsonb,
  automatic_at timestamptz,
  published_payload jsonb,
  published_at timestamptz,
  primary key (sport, date_key)
);

alter table public.vd_daily_snapshots enable row level security;
-- 會員瀏覽器不直接讀 Supabase；只有 Streamlit server 使用 Service Role Key。
revoke all on table public.vd_daily_snapshots from anon, authenticated;
