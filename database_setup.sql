-- 維大力體育APP Restart：只執行一次。
-- 使用 Supabase SQL Editor 執行；不需要也不應把資料庫密碼貼入此檔。
create table if not exists public.vd_daily_snapshots (
  sport text not null check (sport in ('mlb', 'nba', 'football')),
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


-- 已建立舊版資料庫時，也可安全重新執行本檔以加入 NBA 類別。
alter table public.vd_daily_snapshots drop constraint if exists vd_daily_snapshots_sport_check;
alter table public.vd_daily_snapshots
  add constraint vd_daily_snapshots_sport_check check (sport in ('mlb', 'nba', 'football'));
