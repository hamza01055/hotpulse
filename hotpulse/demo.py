"""Fictional sample data so you can see the site working before any real source is reachable.

All companies, people and numbers below are invented. Every demo source is named "Demo · ..." and
the site shows a banner while demo data exists. Remove it with `hotpulse demo --clear`.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

from .db import session

DEMO_PREFIX = "demo://"

# channel -> list of stories; each story has one or more (source, title, body) variants
STORIES: dict[str, list[list[tuple[str, str, str]]]] = {
    "ai": [
        [("Nimbus Labs Blog", "Nimbus Labs releases Nimbus-3, an open-weight reasoning model",
          "Nimbus Labs today released Nimbus-3, a 70 billion parameter open-weight model focused on reasoning and coding. "
          "The company says Nimbus-3 scores 81% on its internal coding benchmark, up from 64% for Nimbus-2. Weights are "
          "available under an Apache 2.0 licence and the model runs on a single 48 GB GPU when quantised. A smaller 8B "
          "version is also available for laptops."),
         ("Demo Tech Daily", "Nimbus-3 open-weight model from Nimbus Labs targets coding and reasoning",
          "Nimbus Labs has launched Nimbus-3, an open-weight model with 70 billion parameters. Early testers report strong "
          "results on coding tasks. The release includes an 8B variant and an Apache 2.0 licence, making it one of the "
          "largest permissively licensed reasoning models this year."),
         ("Demo Dev Forum", "Nimbus-3 70B weights are out: first impressions",
          "Community members began testing Nimbus Labs' Nimbus-3 within hours of release. The 70B open-weight model runs on "
          "one 48 GB GPU with 4-bit quantisation, and several users report it handles multi-file refactors well.")],
        [("Demo Tech Daily", "Orbitly raises $120 million Series B to build AI agents for small businesses",
          "Orbitly, a startup building AI agents that handle invoicing and customer support for small businesses, raised "
          "$120 million in a Series B round. The company says 40,000 businesses use its agents today. The money will fund "
          "expansion to South Asia and the Middle East."),
         ("Demo Business Wire", "AI agent startup Orbitly secures $120M Series B",
          "Orbitly announced a $120 million Series B. The startup's AI agents automate invoicing and support for small "
          "companies and are used by 40,000 businesses, according to the company.")],
        [("Demo Research Digest", "New study: small models trained on curated data match larger ones on maths",
          "Researchers at the fictional Lakeside Institute show that a 3B parameter model trained on 20 billion carefully "
          "filtered tokens matches a 13B model on grade-school maths benchmarks. The paper releases the filtering code "
          "and dataset so others can reproduce the results.")],
        [("Demo Policy Watch", "Regional AI safety framework published for public comment",
          "A fictional regional regulator published a draft AI safety framework requiring risk assessments for models used "
          "in hiring and lending. The public comment period runs for 60 days. Companies would have 18 months to comply.")],
        [("Demo Dev Forum", "Ten prompts that will change your life forever!!",
          "A listicle of generic prompts with no new information.")],
    ],
    "opportunities": [
        [("Demo Scholarships Hub", "Fully funded Aurora Global Masters Scholarship 2027 open to international students",
          "The fictional Aurora University offers 50 fully funded masters scholarships for international students, "
          "including applicants from Pakistan. The scholarship covers tuition, a monthly stipend and travel. "
          f"Eligibility: bachelor's degree with good grades and English proficiency. Deadline: {(date.today() + timedelta(days=6)).strftime('%B %d, %Y')}."),
         ("Demo Opportunity Board", "Aurora Global Masters Scholarship (fully funded): apply now",
          "Aurora University's fully funded masters scholarship is open to students from all countries. It includes tuition, "
          f"stipend and airfare. Application deadline: {(date.today() + timedelta(days=6)).strftime('%B %d, %Y')}.")],
        [("Demo Remote Jobs", "Junior Python Developer at Brightloop (remote, worldwide)",
          "Brightloop is hiring a Junior Python Developer, fully remote and open worldwide. Salary: $1,800–$2,400 per month. "
          "Skills: Python, FastAPI, SQL. Apply by "
          f"{(date.today() + timedelta(days=20)).strftime('%d %B %Y')}.")],
        [("Demo Opportunity Board", "Youth Climate Innovators Fellowship 2027 for developing countries",
          "A fictional foundation invites young innovators aged 18-30 from developing countries to a 6-month online "
          "fellowship with mentorship and a $5,000 project grant. Pakistan is among eligible countries. "
          f"Deadline: {(date.today() + timedelta(days=35)).isoformat()}.")],
        [("Demo Scholarships Hub", "Win a free laptop, just pay a small registration fee",
          "Pay to apply for a prize draw. Registration fee required.")],
    ],
    "pakistan": [
        [("Demo PK Tech", "Lahore fintech PayNest raises $8 million seed round",
          "PayNest, a fictional Lahore-based fintech startup that lets freelancers receive international payments, raised "
          "an $8 million seed round. The company says it processed Rs 12 billion in payments last year and plans to "
          "launch Raast-linked instant withdrawals."),
         ("Demo Startup Pakistan", "PayNest bags $8M seed to help Pakistani freelancers get paid faster",
          "Freelancer payments startup PayNest has raised $8 million in seed funding. The Lahore company plans instant "
          "withdrawals through Raast and expansion to Karachi and Islamabad.")],
        [("Demo PK Tech", "IT exports rise 18% in first quarter, says fictional ministry data",
          "Pakistan's IT and IT-enabled services exports rose 18% year-on-year in the first quarter to $1.1 billion, "
          "according to sample figures. Freelancers contributed a growing share of remittances.")],
        [("Demo Telecom Review", "Telecom regulator opens consultation on 5G spectrum auction",
          "The telecom regulator published a consultation on the upcoming 5G spectrum auction, inviting comments from "
          "operators within 30 days. The auction is planned for next year.")],
    ],
}


def demo_feeds(now: datetime | None = None) -> dict[str, bytes]:
    """Return {demo_url: rss_bytes} for every demo source."""
    now = now or datetime.now(timezone.utc)
    per_source: dict[tuple[str, str], list[tuple[str, str, str, datetime]]] = {}
    for channel, stories in STORIES.items():
        for s_idx, variants in enumerate(stories):
            for v_idx, (source, title, body) in enumerate(variants):
                when = now - timedelta(hours=2 + s_idx * 3, minutes=v_idx * 25)
                slug = "-".join(title.lower().split()[:6]).replace("/", "")
                link = f"https://example.com/{channel}/{s_idx}-{v_idx}-{slug}"
                per_source.setdefault((channel, source), []).append((title, link, body, when))
    feeds = {}
    for (channel, source), entries in per_source.items():
        items = "".join(
            f"<item><title>{escape(t)}</title><link>{escape(l)}</link><description>{escape(b)}</description>"
            f"<pubDate>{format_datetime(w)}</pubDate><guid>{escape(l)}</guid></item>" for t, l, b, w in entries)
        xml = (f'<?xml version="1.0"?><rss version="2.0"><channel><title>{escape(source)}</title>'
               f"<link>https://example.com</link><description>demo</description>{items}</channel></rss>")
        feeds[demo_url(channel, source)] = xml.encode()
    return feeds


def demo_url(channel: str, source: str) -> str:
    return f"{DEMO_PREFIX}{channel}/{source.lower().replace(' ', '-')}"


def install_demo_sources(settings) -> list[str]:
    tiers = {"Nimbus Labs Blog": "T1", "Demo Dev Forum": "T3", "Demo Remote Jobs": "T1"}
    urls = []
    with session(settings.db_path) as conn:
        for channel, stories in STORIES.items():
            if channel not in settings.channels:
                continue
            for source in sorted({v[0] for variants in stories for v in variants}):
                url = demo_url(channel, source)
                urls.append(url)
                conn.execute("""INSERT INTO sources(channel, name, url, kind, tier, lang, enabled, from_config)
                                VALUES (?,?,?,?,?,?,1,0) ON CONFLICT(channel, url) DO UPDATE SET enabled=1""",
                             (channel, "Demo · " + source.removeprefix("Demo ").strip(), url, "rss", tiers.get(source, "T2"), "en"))
    return urls


def clear_demo(settings) -> int:
    with session(settings.db_path) as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM sources WHERE url LIKE ?", (DEMO_PREFIX + "%",))]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        item_ids = [r["id"] for r in conn.execute(f"SELECT id FROM items WHERE source_id IN ({marks})", ids)]
        for iid in item_ids:
            conn.execute("DELETE FROM items_fts WHERE rowid=?", (iid,))
        conn.execute(f"DELETE FROM items WHERE source_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM sources WHERE id IN ({marks})", ids)
        conn.execute("DELETE FROM events WHERE id NOT IN (SELECT DISTINCT event_id FROM items WHERE event_id IS NOT NULL)")
        conn.execute("DELETE FROM digests")
        return len(item_ids)


def has_demo(conn) -> bool:
    return conn.execute("SELECT 1 FROM sources WHERE url LIKE ? LIMIT 1", (DEMO_PREFIX + "%",)).fetchone() is not None
