"""Command line: hotpulse init | run | serve | demo | digest | sources | doctor"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import load_settings


def _settings(args):
    return load_settings(args.config, args.db)


def cmd_init(args) -> None:
    from .pipeline import prepare
    s = _settings(args)
    prepare(s)
    print(f"Database ready at {s.db_path} with {sum(len(c.sources) for c in s.channels.values())} sources "
          f"in {len(s.channels)} channels: {', '.join(s.channels)}")


def cmd_run(args) -> None:
    from .pipeline import run_cycle
    s = _settings(args)
    stats = run_cycle(s, collect_sources=not args.no_collect, channel=args.channel)
    print(json.dumps(stats, indent=2, ensure_ascii=False))


def cmd_serve(args) -> None:
    import uvicorn
    from .web.app import create_app
    s = _settings(args)
    local = args.host in ("127.0.0.1", "localhost")
    if s.admin_token in ("", "change-me") and not local:
        print("WARNING: ADMIN_TOKEN is not set; /admin is disabled until you set it in .env", file=sys.stderr)
    app = create_app(s, start_scheduler=not args.no_scheduler, allow_default_admin=local)
    print(f"→ {s.name} on http://{args.host}:{args.port}  (admin: /admin, scheduler: {'off' if args.no_scheduler else 'on'})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def cmd_demo(args) -> None:
    from .demo import DEMO_PREFIX, clear_demo, demo_feeds, install_demo_sources
    from .digest import build_today_preview
    from .pipeline import prepare, run_cycle
    s = _settings(args)
    prepare(s)
    if args.clear:
        print(f"Removed {clear_demo(s)} demo items.")
        return
    install_demo_sources(s)
    feeds = demo_feeds()

    def fetch(url: str) -> bytes:
        if url in feeds:
            return feeds[url]
        raise RuntimeError("demo mode: network disabled")

    llm = "auto" if args.ai else None
    stats = run_cycle(s, llm=llm, fetcher=fetch, url_prefix=DEMO_PREFIX)
    from .llm import get_llm
    model = get_llm(s) if args.ai else None
    built = [k for k in s.channels if build_today_preview(s, model, k)]
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    print(f"Demo briefings: {', '.join(built)}. Start the site with: hotpulse serve")


def cmd_digest(args) -> None:
    from .digest import build_digest, build_today_preview
    from .llm import get_llm
    s = _settings(args)
    llm = get_llm(s)
    for key in ([args.channel] if args.channel else list(s.channels)):
        d = build_today_preview(s, llm, key) if args.today else build_digest(s, llm, key, args.kind, force=True)
        print(f"{key}: {d['title'] if d else 'no picks in this period'}")


def cmd_sources(args) -> None:
    from .collect import fetch_source, http_fetch
    from .db import session
    from .pipeline import prepare
    s = _settings(args)
    prepare(s)
    with session(s.db_path) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM sources WHERE enabled=1 ORDER BY channel, name")]
    if args.channel:
        rows = [r for r in rows if r["channel"] == args.channel]
    if not args.test:
        for r in rows:
            print(f"[{r['channel']}] {r['tier']} {r['name']:<40} {r['url']}")
        return
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as pool:
        for src, entries, err in pool.map(lambda r: fetch_source(r, http_fetch), rows):
            mark = "OK " if err is None else "ERR"
            info = f"{len(entries)} entries" if err is None else err
            print(f"{mark} [{src['channel']}] {src['name']:<40} {info}")


def cmd_doctor(args) -> None:
    from .llm import get_llm, ollama_status
    s = _settings(args)
    print(f"Config:   {s.root}")
    print(f"Database: {s.db_path}")
    print(f"Channels: {', '.join(f'{k} ({len(c.sources)} sources)' for k, c in s.channels.items())}")
    print(f"LLM mode: {s.llm_mode}")
    st = ollama_status(s.ollama_url, s.ollama_model, force=True)
    print(f"Ollama:   {'OK' if st['ok'] else 'NOT READY'} — {st['detail']}")
    if st.get("models"):
        print(f"          installed models: {', '.join(st['models'])}")
    llm = get_llm(s)
    if llm:
        out = llm.chat_json('Reply with JSON {"ok": true}', "ping", cache=False)
        print(f"Model test: {'OK' if out and out.get('ok') else 'unexpected reply: ' + str(out)}")
    else:
        print("Model test: skipped (heuristic mode will be used)")
    print(f"Admin:    {'set' if s.admin_token not in ('', 'change-me') else 'NOT SET (only works on localhost)'}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="hotpulse", description="Self-hosted AI news radar")
    p.add_argument("--config", help="config folder (default: ./config or $HOTPULSE_CONFIG)")
    p.add_argument("--db", help="SQLite path (default: data/hotpulse.db or $HOTPULSE_DB)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the database and load sources").set_defaults(func=cmd_init)

    r = sub.add_parser("run", help="run one full cycle now")
    r.add_argument("--channel")
    r.add_argument("--no-collect", action="store_true", help="only analyse what's already collected")
    r.set_defaults(func=cmd_run)

    sv = sub.add_parser("serve", help="start the website (+ background scheduler)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--no-scheduler", action="store_true")
    sv.set_defaults(func=cmd_serve)

    d = sub.add_parser("demo", help="load fictional sample data to try the site offline")
    d.add_argument("--clear", action="store_true", help="remove demo data")
    d.add_argument("--ai", action="store_true", help="use Ollama for the demo too (slower)")
    d.set_defaults(func=cmd_demo)

    dg = sub.add_parser("digest", help="(re)build briefings")
    dg.add_argument("--channel")
    dg.add_argument("--kind", choices=["daily", "weekly"], default="daily")
    dg.add_argument("--today", action="store_true", help="'so far today' briefing covering the last 24h")
    dg.set_defaults(func=cmd_digest)

    so = sub.add_parser("sources", help="list sources, or --test them")
    so.add_argument("--channel")
    so.add_argument("--test", action="store_true")
    so.set_defaults(func=cmd_sources)

    sub.add_parser("doctor", help="check config, Ollama and model").set_defaults(func=cmd_doctor)

    args = p.parse_args(argv)
    # Output includes →, — and non-Latin titles; Windows defaults to cp1252 when output is redirected.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
