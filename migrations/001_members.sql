-- Review and run ONCE in Supabase SQL Editor after project resume and approval.
-- No changes to snapshots, existing roles, credentials, or subscription plan.
begin;
create table if not exists public.vd_members (
  id uuid primary key default gen_random_uuid(),
  login_id text unique not null,
  label text not null check (length(label) between 1 and 80),
  password_hash text,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null default now() + interval '7 days',
  enabled boolean not null default true,
  deleted_at timestamptz,
  device_hash text,
  session_hash text,
  last_login_at timestamptz,
  failed_attempts integer not null default 0,
  locked_until timestamptz
);
alter table public.vd_members enable row level security;
revoke all on public.vd_members from public, anon, authenticated;
grant select, insert, update on public.vd_members to service_role;

-- Only the trusted Streamlit server may invoke these functions. SECURITY
-- INVOKER deliberately requires the caller's existing service-role access.
create or replace function public.vd_member_issue(p_login_id text, p_label text, p_password_hash text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare m public.vd_members;
begin
  if p_login_id !~ '^VD-[0-9A-F]{12}$' or p_password_hash is null
     or p_password_hash !~ '^scrypt1\$[0-9a-f]{32}\$[0-9a-f]{64}$' then
    raise exception 'invalid member configuration';
  end if;
  insert into public.vd_members(login_id,label,password_hash)
    values(p_login_id,p_label,p_password_hash) returning * into m;
  return jsonb_build_object('id',m.id,'expires_at',m.expires_at);
end $$;

create or replace function public.vd_member_overview(p_offset integer default 0)
returns jsonb language sql security invoker set search_path = '' as $$
  select jsonb_build_object(
    'total_issued',count(*),
    'current',count(*) filter (where deleted_at is null),
    'active',count(*) filter (where deleted_at is null and enabled and expires_at > now()),
    'expired',count(*) filter (where deleted_at is null and enabled and expires_at <= now()),
    'disabled',count(*) filter (where deleted_at is null and not enabled),
    'deleted',count(*) filter (where deleted_at is not null),
    'members',coalesce((select jsonb_agg(row_to_json(s)) from (
      select id,login_id,label,created_at,expires_at,enabled,last_login_at,
             (device_hash is not null) as bound,
             case when not enabled then '停用' when expires_at <= now() then '到期' else '有效' end as state
      from public.vd_members where deleted_at is null
      order by created_at desc,id limit 50 offset greatest(p_offset,0)
    ) s),'[]'::jsonb)
  ) from public.vd_members;
$$;

create or replace function public.vd_member_credential(p_login_id text)
returns jsonb language sql security invoker set search_path = '' as $$
  select jsonb_build_object('password_hash',password_hash) from public.vd_members
  where login_id=p_login_id and deleted_at is null;
$$;

create or replace function public.vd_member_attempt(p_login_id text, p_expected_hash text,
  p_password_ok boolean, p_device_hash text, p_session_hash text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare m public.vd_members;
begin
  if p_device_hash is null or p_device_hash !~ '^[0-9a-f]{64}$'
    or p_session_hash is null or p_session_hash !~ '^[0-9a-f]{64}$' then
    return jsonb_build_object('ok',false,'reason','denied');
  end if;
  -- Row lock makes first-device binding, renewal and revocation atomic.
  select * into m from public.vd_members where login_id=p_login_id for update;
  if not found or m.deleted_at is not null or not m.enabled then
    return jsonb_build_object('ok',false,'reason','denied');
  end if;
  if m.locked_until > now() then return jsonb_build_object('ok',false,'reason','locked'); end if;
  -- Password reset racing with KDF verification cannot accept an old hash.
  if p_password_ok is distinct from true or m.password_hash is distinct from p_expected_hash then
    update public.vd_members set
      failed_attempts=case when locked_until <= now() then 1 else failed_attempts+1 end,
      locked_until=case when (case when locked_until <= now() then 0 else failed_attempts end)+1 >= 5
                        then now()+interval '5 minutes' else null end
      where id=m.id;
    return jsonb_build_object('ok',false,'reason','denied');
  end if;
  if m.expires_at <= now() then return jsonb_build_object('ok',false,'reason','expired'); end if;
  if m.device_hash is not null and m.device_hash <> p_device_hash then
    return jsonb_build_object('ok',false,'reason','device');
  end if;
  update public.vd_members set device_hash=p_device_hash,session_hash=p_session_hash,
    last_login_at=now(),failed_attempts=0,locked_until=null where id=m.id;
  return jsonb_build_object('ok',true,'id',m.id);
end $$;

create or replace function public.vd_member_session(p_id uuid, p_device_hash text, p_session_hash text)
returns jsonb language sql security invoker set search_path = '' as $$
  select jsonb_build_object('ok',true,'id',id,'label',label,'expires_at',expires_at)
  from public.vd_members where id=p_id and deleted_at is null and enabled
    and expires_at > now() and device_hash=p_device_hash and session_hash=p_session_hash;
$$;

create or replace function public.vd_member_manage(p_id uuid, p_action text, p_password_hash text default null)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare m public.vd_members;
begin
  select * into m from public.vd_members where id=p_id and deleted_at is null for update;
  if not found then return jsonb_build_object('ok',false); end if;
  if p_action='renew' then
    update public.vd_members set expires_at=greatest(expires_at,now())+interval '7 days',enabled=true
      where id=p_id;
  elsif p_action='disable' then
    update public.vd_members set enabled=false,session_hash=null where id=p_id;
  elsif p_action='delete' then
    -- Tombstone preserves issued count; remove personal label and credentials.
    update public.vd_members set enabled=false,deleted_at=now(),label='已刪除',password_hash=null,
      device_hash=null,session_hash=null,last_login_at=null,failed_attempts=0,locked_until=null where id=p_id;
  elsif p_action='reset_device' then
    update public.vd_members set device_hash=null,session_hash=null where id=p_id;
  elsif p_action='reset_password' then
    if p_password_hash is null or p_password_hash !~ '^scrypt1\$[0-9a-f]{32}\$[0-9a-f]{64}$' then
      raise exception 'invalid password format';
    end if;
    update public.vd_members set password_hash=p_password_hash,session_hash=null,
      failed_attempts=0,locked_until=null where id=p_id;
  else raise exception 'invalid member action';
  end if;
  return jsonb_build_object('ok',true);
end $$;

create or replace function public.vd_member_logout(p_id uuid, p_session_hash text)
returns void language sql security invoker set search_path = '' as $$
  update public.vd_members set session_hash=null where id=p_id and session_hash=p_session_hash;
$$;

-- Keep table and functions inaccessible to browser/anonymous Supabase clients.
revoke all on function public.vd_member_issue(text,text,text) from public,anon,authenticated;
revoke all on function public.vd_member_overview(integer) from public,anon,authenticated;
revoke all on function public.vd_member_credential(text) from public,anon,authenticated;
revoke all on function public.vd_member_attempt(text,text,boolean,text,text) from public,anon,authenticated;
revoke all on function public.vd_member_session(uuid,text,text) from public,anon,authenticated;
revoke all on function public.vd_member_manage(uuid,text,text) from public,anon,authenticated;
revoke all on function public.vd_member_logout(uuid,text) from public,anon,authenticated;
grant execute on function public.vd_member_issue(text,text,text), public.vd_member_overview(integer),
  public.vd_member_credential(text),public.vd_member_attempt(text,text,boolean,text,text),
  public.vd_member_session(uuid,text,text),public.vd_member_manage(uuid,text,text),
  public.vd_member_logout(uuid,text) to service_role;
commit;
