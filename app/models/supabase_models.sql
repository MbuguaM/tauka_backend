create table public.profiles (
    id uuid primary key references auth.users(id) on delete cascade,
    email text,
    plan text default 'free',
    created_at timestamp with time zone default now()
); 

create table public.plans (
    id text primary key,
    name text,
    monthly_token_limit bigint,
    daily_token_limit bigint,
    requests_per_minute int,
    price_per_1k_tokens numeric
);

create table public.usage_logs (
    id bigserial primary key,
    user_id uuid references auth.users(id),
    model text,
    input_tokens int,
    output_tokens int,
    total_tokens int,
    cost numeric,
    created_at timestamp with time zone default now()
);

create index idx_usage_user_time
on public.usage_logs(user_id, created_at desc);

create table public.usage_aggregates (
    user_id uuid,
    period text, -- 'daily' or 'monthly'
    period_start date,
    total_tokens bigint default 0,
    total_requests int default 0,
    primary key (user_id, period, period_start)
); 


create table public.rate_limit_events (
    id bigserial primary key,
    user_id uuid,
    reason text,
    created_at timestamp default now()
);