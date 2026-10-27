#!/usr/bin/env python3
"""CS2 Radar v2.

Модулі:
  1. Ціни та біржові індикатори для списку відстеження (зміна %, сплеск обсягу, SMA-перетин, RSI, z-score).
  2. Сканер ринку: pump / dump / "скупка пропозиції" серед популярних предметів Steam.
  3. Спред Steam vs Skinport (арбітраж з урахуванням комісії Steam, ознаки реального попиту).
  4. Пости (Reddit, Telegram-канали, X) EN+RU: NSFW-крафти, бюджетний скін під дорогий вигляд,
     апгрейд wear наклейками, "китайці качають", швидке падіння; тренд згадок наклейок.
  5. Події Steam News (апдейти, trade ban, нові кейси).
  6. Telegram-команди: /add /remove /list /target /buy /portfolio /movers /status /help.

Запуск:
  python radar.py selftest   # перевірка логіки без мережі
  python radar.py once       # один прохід (для GitHub Actions)
  python radar.py run        # безкінечний цикл (VPS/Termux)
"""
import argparse
import html as htmllib
import json
import logging
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests
import yaml

UA = "cs2-radar/0.2 (personal research tool)"
# "Sticker | Назва [(Holo|Foil|Gold)] [| Подія 2014]" — подія закінчується роком
STICKER_RE = re.compile(
    r"Sticker \| [^|\n\[\]\"“”,;()]{1,49}[^|\s\[\]\"“”,;()]"
    r"(?:\s\([^)\n]{2,20}\))?"
    r"(?:\s\|\s[^|\n\[\]\"“”,;()]*?\d{4})?"
)
log = logging.getLogger("radar")

HELP = (
    "Команди:\n"
    "/add <market_hash_name> — стежити за ціною\n"
    "/remove <назва> — прибрати зі стеження\n"
    "/list — список відстеження з цінами\n"
    "/target <назва> < 12.5 — алерт, коли ціна впаде нижче (або > для росту)\n"
    "/buy <назва> @ 3.2 x2 sl15 tp40 — позиція (ціна входу, к-сть, stop-loss %, take-profit %)\n"
    "/portfolio — PnL позицій з урахуванням комісії Steam\n"
    "/movers — топ росту/падіння за останню добу\n"
    "/status — стан бота\n\n"
    "Назва має бути точною, як у Steam Market, напр.: AK-47 | Redline (Field-Tested)"
)


# --------------------------------------------------------------------------- utils
def now_ts() -> int:
    return int(time.time())


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_price(s):
    """'12,34€' / '$1,234.56' / '1.234,56€' -> float."""
    s = re.sub(r"[^\d.,]", "", s or "")
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        if len(s) - s.rfind(",") - 1 in (1, 2):
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_volume(s):
    digits = re.sub(r"\D", "", s or "")
    return int(digits) if digits else None


def strip_tags(s: str) -> str:
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def too_old(post: dict, max_age_hours: float) -> bool:
    """Старі пости/новини (за датою публікації) не надсилаємо."""
    ts = post.get("ts")
    return bool(ts) and now_ts() - ts > max_age_hours * 3600


# --------------------------------------------------------------------------- storage
def init_db(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS prices(name TEXT, ts INTEGER, price REAL, volume INTEGER);
        CREATE INDEX IF NOT EXISTS idx_prices ON prices(name, ts);
        CREATE TABLE IF NOT EXISTS scan(name TEXT, ts INTEGER, price REAL, listings INTEGER);
        CREATE INDEX IF NOT EXISTS idx_scan ON scan(name, ts);
        CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY, ts INTEGER);
        CREATE TABLE IF NOT EXISTS alerts(key TEXT, ts INTEGER);
        CREATE TABLE IF NOT EXISTS mentions(sticker TEXT, ts INTEGER, post_id TEXT);
        CREATE INDEX IF NOT EXISTS idx_mentions ON mentions(sticker, ts);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS watch(name TEXT PRIMARY KEY, ts INTEGER);
        CREATE TABLE IF NOT EXISTS targets(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price REAL, direction TEXT, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price REAL, qty INTEGER, sl REAL, tp REAL, ts INTEGER);
        """
    )
    return db


def meta_get(db, k, default=None):
    r = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def meta_set(db, k, v):
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))
    db.commit()


def is_new(db, item_id: str) -> bool:
    if db.execute("SELECT 1 FROM seen WHERE id=?", (item_id,)).fetchone():
        return False
    db.execute("INSERT INTO seen VALUES(?,?)", (item_id, now_ts()))
    db.commit()
    return True


def seed_watch(cfg, db):
    for group in cfg.get("watchlist", {}).values():
        for name in group or []:
            db.execute("INSERT OR IGNORE INTO watch VALUES(?,?)", (name, now_ts()))
    db.commit()


def watch_names(db):
    return [r[0] for r in db.execute("SELECT name FROM watch ORDER BY name")]


def last_price(db, name):
    r = db.execute("SELECT price FROM prices WHERE name=? ORDER BY ts DESC LIMIT 1", (name,)).fetchone()
    return r[0] if r else None


# --------------------------------------------------------------------------- notify
class Notifier:
    def __init__(self, db, cooldown_hours=6):
        self.db = db
        self.cooldown = cooldown_hours * 3600
        self.token = os.getenv("TELEGRAM_TOKEN")
        self.chat = os.getenv("TELEGRAM_CHAT_ID")
        self.sent = []  # для selftest
        self.silent = False  # перший прохід: лише запам'ятовуємо старі пости/новини, нічого не шлемо

    def _post(self, text: str):
        self.sent.append(text)
        if self.token and self.chat:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": self.chat, "text": text[:4000], "disable_web_page_preview": True},
                    timeout=15,
                )
            except requests.RequestException as e:
                log.warning("telegram error: %s", e)
        else:
            print("\n[ALERT]\n" + text + "\n")

    def send(self, key: str, text: str):
        """Не шле той самий ключ частіше ніж раз на cooldown."""
        if self.silent:
            return
        cutoff = now_ts() - self.cooldown
        if self.db.execute("SELECT 1 FROM alerts WHERE key=? AND ts>?", (key, cutoff)).fetchone():
            return
        self.db.execute("INSERT INTO alerts VALUES(?,?)", (key, now_ts()))
        self.db.commit()
        self._post(text)

    def reply(self, text: str):
        self._post(text)


# --------------------------------------------------------------------------- 1. prices + indicators
def fetch_steam_price(name: str, currency: int):
    r = requests.get(
        "https://steamcommunity.com/market/priceoverview/",
        params={"appid": 730, "currency": currency, "market_hash_name": name},
        headers={"User-Agent": UA},
        timeout=20,
    )
    if r.status_code == 429:
        raise RuntimeError("Steam rate limit (429)")
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        return None, None
    price = parse_price(data.get("lowest_price") or data.get("median_price"))
    return price, parse_volume(data.get("volume"))


def evaluate_price(db, name: str, cfg_alerts: dict):
    """Алерти за зміною ціни та обсягом за історією з БД (останній запис = поточний)."""
    out = []
    window = cfg_alerts["window_hours"] * 3600
    rows = db.execute("SELECT ts, price, volume FROM prices WHERE name=? ORDER BY ts", (name,)).fetchall()
    if len(rows) < 2:
        return out
    ts_now, p_now, v_now = rows[-1]
    base = next((r for r in rows if r[0] >= ts_now - window), None)
    if base and base[0] < ts_now and base[1]:
        pct = (p_now - base[1]) / base[1] * 100
        if abs(pct) >= cfg_alerts["price_change_pct"]:
            arrow = "📈" if pct > 0 else "📉"
            out.append((f"price:{name}:{'up' if pct > 0 else 'down'}",
                        f"{arrow} {name}\nЦіна {base[1]:.2f} → {p_now:.2f} ({pct:+.1f}% за ~{cfg_alerts['window_hours']}г)"))
    week = [r[2] for r in rows if r[2] is not None and r[0] >= ts_now - 7 * 86400][:-1]
    if v_now and week:
        avg = sum(week) / len(week)
        if avg > 0 and v_now >= avg * cfg_alerts["volume_spike_x"]:
            out.append((f"vol:{name}", f"🔥 {name}\nОбсяг {v_now} (у {v_now / avg:.1f}× вище середнього {avg:.0f})"))
    return out


def _mean(x):
    return sum(x) / len(x)


def sma(vals, n):
    return _mean(vals[-n:]) if len(vals) >= n else None


def rsi(vals, n):
    if len(vals) < n + 1:
        return None
    gains = losses = 0.0
    for a, b in zip(vals[-n - 1:-1], vals[-n:]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    return 100 - 100 / (1 + gains / losses)


def zscore(vals, n):
    w = vals[-n:]
    if len(w) < n:
        return None, None
    hist = w[:-1]
    m = _mean(hist)
    sd = (sum((x - m) ** 2 for x in hist) / len(hist)) ** 0.5
    if sd == 0 or m == 0:
        return None, None
    return (w[-1] - m) / sd, (w[-1] - m) / m * 100


def indicator_signals(db, name: str, ind: dict):
    rows = db.execute("SELECT price FROM prices WHERE name=? ORDER BY ts DESC LIMIT 200", (name,)).fetchall()
    vals = [r[0] for r in reversed(rows) if r[0] is not None]
    if len(vals) < ind["min_points"]:
        return []
    out = []
    s_now, l_now = sma(vals, ind["sma_short"]), sma(vals[:], ind["sma_long"])
    s_prev, l_prev = sma(vals[:-1], ind["sma_short"]), sma(vals[:-1], ind["sma_long"])
    if None not in (s_now, l_now, s_prev, l_prev):
        if s_prev <= l_prev and s_now > l_now:
            out.append((f"sma:{name}:up", f"🟢 {name}\nSMA{ind['sma_short']} перетнула SMA{ind['sma_long']} вгору (бичачий сигнал). Ціна {vals[-1]:.2f}"))
        elif s_prev >= l_prev and s_now < l_now:
            out.append((f"sma:{name}:down", f"🔴 {name}\nSMA{ind['sma_short']} перетнула SMA{ind['sma_long']} вниз (ведмежий сигнал). Ціна {vals[-1]:.2f}"))
    r = rsi(vals, ind["rsi_period"])
    if r is not None:
        if r >= ind["rsi_overbought"]:
            out.append((f"rsi:{name}:hi", f"🌡 {name}\nRSI {r:.0f}: перегрітий ріст, можливий відкат. Ціна {vals[-1]:.2f}"))
        elif r <= ind["rsi_oversold"]:
            out.append((f"rsi:{name}:lo", f"🧊 {name}\nRSI {r:.0f}: перепроданість, можливий відскок. Ціна {vals[-1]:.2f}"))
    z, move = zscore(vals, ind["z_window"])
    if z is not None and abs(z) >= ind["z_threshold"] and abs(move) >= ind["z_min_move_pct"]:
        word = "різкий стрибок" if z > 0 else "різкий провал"
        out.append((f"z:{name}:{'up' if z > 0 else 'down'}",
                    f"⚡ {name}\nАномальний рух: {word} ({move:+.1f}% від середнього, z={z:.1f}). Ціна {vals[-1]:.2f}"))
    return out


def check_prices(cfg, db, notifier, fetch=fetch_steam_price, delay=3.5):
    for name in watch_names(db):
        try:
            price, vol = fetch(name, cfg["currency"])
        except Exception as e:  # мережа / ліміт — не валимо весь цикл
            log.warning("price %s: %s", name, e)
            if "429" in str(e):
                break  # ліміт Steam — чекаємо наступного циклу
            continue
        if price is None:
            log.info("no price for %s", name)
            continue
        db.execute("INSERT INTO prices VALUES(?,?,?,?)", (name, now_ts(), price, vol))
        db.commit()
        for key, text in evaluate_price(db, name, cfg["alerts"]) + indicator_signals(db, name, cfg["indicators"]):
            notifier.send(key, text)
        time.sleep(delay)


def check_targets_and_positions(cfg, db, notifier):
    fee = cfg.get("steam_fee", 1.15)
    for tid, name, target, direction in db.execute("SELECT id, name, price, direction FROM targets WHERE active=1").fetchall():
        p = last_price(db, name)
        if p is None:
            continue
        if (direction == "below" and p <= target) or (direction == "above" and p >= target):
            notifier.send(f"target:{tid}", f"🎯 {name}\nЦіна {p:.2f} {'≤' if direction == 'below' else '≥'} цілі {target:.2f}")
            db.execute("UPDATE targets SET active=0 WHERE id=?", (tid,))
            db.commit()
    for pid, name, cost, qty, sl, tp in db.execute("SELECT id, name, price, qty, sl, tp FROM positions").fetchall():
        p = last_price(db, name)
        if p is None or not cost:
            continue
        pnl = (p / fee / cost - 1) * 100
        if sl and pnl <= -sl:
            notifier.send(f"sl:{pid}", f"🛑 STOP-LOSS {name}\nPnL {pnl:+.1f}% (чисто, після комісії). Вхід {cost:.2f}, зараз {p:.2f}")
        if tp and pnl >= tp:
            notifier.send(f"tp:{pid}", f"💰 TAKE-PROFIT {name}\nPnL {pnl:+.1f}% (чисто, після комісії). Вхід {cost:.2f}, зараз {p:.2f}")


# --------------------------------------------------------------------------- 2. market scanner (pump / dump)
def fetch_market_popular(currency: int, pages: int = 2, per_page: int = 100, delay: float = 3.5):
    out = []
    for page in range(pages):
        r = requests.get(
            "https://steamcommunity.com/market/search/render/",
            params={"query": "", "start": page * per_page, "count": per_page, "search_descriptions": 0,
                    "sort_column": "popular", "sort_dir": "desc", "appid": 730, "norender": 1, "currency": currency},
            headers={"User-Agent": UA},
            timeout=25,
        )
        if r.status_code == 429:
            raise RuntimeError("Steam rate limit (429)")
        r.raise_for_status()
        for it in r.json().get("results", []):
            if it.get("sell_price"):
                out.append({"name": it.get("hash_name") or it.get("name"), "price": it["sell_price"] / 100.0,
                            "listings": it.get("sell_listings", 0)})
        time.sleep(delay)
    return out


def scan_movers(cfg, db, notifier, fetch=fetch_market_popular):
    m = cfg["movers"]
    if not m.get("enabled"):
        return
    try:
        items = fetch(cfg["currency"], m["pages"])
    except Exception as e:
        log.warning("movers: %s", e)
        return
    t = now_ts()
    window = m["window_hours"] * 3600
    pumps, dumps, squeezes = [], [], []
    for it in items:
        base = db.execute(
            "SELECT price, listings FROM scan WHERE name=? AND ts>=? ORDER BY ts LIMIT 1", (it["name"], t - window)
        ).fetchone()
        db.execute("INSERT INTO scan VALUES(?,?,?,?)", (it["name"], t, it["price"], it["listings"]))
        if not base or not base[0] or it["price"] < m["min_price"] or it["listings"] < m["min_listings"]:
            continue
        pct = (it["price"] - base[0]) / base[0] * 100
        lst = (it["listings"] - base[1]) / base[1] * 100 if base[1] else 0
        if pct >= m["pump_pct"]:
            pumps.append((pct, it, base))
        elif pct <= -m["dump_pct"]:
            dumps.append((pct, it, base))
        elif pct >= 3 and lst <= -m["squeeze_pct"]:
            squeezes.append((lst, it, base, pct))
    db.commit()
    for pct, it, base in sorted(pumps, key=lambda x: -x[0])[: m["max_alerts"]]:
        notifier.send(f"pump:{it['name']}", f"🚀 PUMP {it['name']}\n{base[0]:.2f} → {it['price']:.2f} ({pct:+.1f}%), лістингів {it['listings']}")
    for pct, it, base in sorted(dumps, key=lambda x: x[0])[: m["max_alerts"]]:
        notifier.send(f"dump:{it['name']}", f"🔻 DUMP {it['name']}\n{base[0]:.2f} → {it['price']:.2f} ({pct:+.1f}%), лістингів {it['listings']}")
    for lst, it, base, pct in sorted(squeezes, key=lambda x: x[0])[: m["max_alerts"]]:
        notifier.send(f"squeeze:{it['name']}",
                      f"🧲 СКУПКА ПРОПОЗИЦІЇ {it['name']}\nЛістинги {base[1]} → {it['listings']} ({lst:.0f}%), ціна {pct:+.1f}% — часто передує pump")


def top_movers_text(db, hours=24, n=5):
    t = now_ts()
    rows = []
    for (name,) in db.execute("SELECT DISTINCT name FROM scan WHERE ts>=?", (t - 3600 * 2,)):
        new = db.execute("SELECT price FROM scan WHERE name=? ORDER BY ts DESC LIMIT 1", (name,)).fetchone()
        old = db.execute("SELECT price FROM scan WHERE name=? AND ts>=? ORDER BY ts LIMIT 1", (name, t - hours * 3600)).fetchone()
        if new and old and old[0] and new[0] != old[0]:
            rows.append(((new[0] - old[0]) / old[0] * 100, name, old[0], new[0]))
    if not rows:
        return "Даних для movers ще мало: сканер збирає знімки при кожному проході."
    rows.sort()
    fmt = lambda r: f"{r[0]:+.1f}% {r[1]} ({r[2]:.2f}→{r[3]:.2f})"
    return "🚀 Ріст:\n" + "\n".join(fmt(r) for r in rows[::-1][:n]) + "\n\n🔻 Падіння:\n" + "\n".join(fmt(r) for r in rows[:n])


# --------------------------------------------------------------------------- 3. spread Steam vs Skinport
def fetch_skinport(currency: str = "EUR"):
    r = requests.get(
        "https://api.skinport.com/v1/items",
        params={"app_id": 730, "currency": currency},
        headers={"Accept-Encoding": "br", "User-Agent": UA},
        timeout=40,
    )
    r.raise_for_status()
    return {i["market_hash_name"]: (i.get("min_price"), i.get("quantity", 0)) for i in r.json()}


def check_spread(cfg, db, notifier, fetch=fetch_skinport):
    sp = cfg["spread"]
    if not sp.get("enabled"):
        return
    try:
        data = fetch(cfg["skinport_currency"])
    except Exception as e:
        log.warning("skinport: %s", e)
        return
    fee = cfg.get("steam_fee", 1.15)
    names = set(watch_names(db)) | {r[0] for r in db.execute("SELECT DISTINCT name FROM scan WHERE ts>?", (now_ts() - 7200,))}
    for name in names:
        s_price = last_price(db, name)
        if s_price is None:
            r = db.execute("SELECT price FROM scan WHERE name=? ORDER BY ts DESC LIMIT 1", (name,)).fetchone()
            s_price = r[0] if r else None
        entry = data.get(name)
        if not s_price or not entry or not entry[0] or s_price < sp["min_price"] or not entry[1]:
            continue
        sp_price = entry[0]
        roi = (s_price / fee / sp_price - 1) * 100
        if roi >= sp["min_roi_pct"]:
            notifier.send(f"spread:{name}",
                          f"💱 СПРЕД {name}\nSkinport {sp_price:.2f} → Steam {s_price:.2f} (у гаманець ≈{s_price / fee:.2f}) = ROI {roi:+.1f}%\n"
                          "Увага: гроші лишаються в гаманці Steam, а не в кеші.")
        elif sp_price / s_price >= sp["premium_ratio"]:
            notifier.send(f"premium:{name}",
                          f"🌐 {name}\nSkinport {sp_price:.2f} майже дорівнює Steam {s_price:.2f}: на реальному ринку попит тисне ціну вгору.")


# --------------------------------------------------------------------------- 4. posts: sources
def fetch_rss(url: str):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    for it in root.iter("item"):
        ts = None
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate")).timestamp()
        except (TypeError, ValueError):
            pass
        yield {
            "id": "rss:" + (it.findtext("guid") or it.findtext("link") or ""),
            "title": it.findtext("title") or "",
            "text": strip_tags(it.findtext("description") or ""),
            "url": it.findtext("link") or "",
            "score": 0,
            "ts": ts,
        }


def fetch_reddit(sub: str, limit=50):
    r = requests.get(
        f"https://www.reddit.com/r/{sub}/new.json",
        params={"limit": limit},
        headers={"User-Agent": UA},
        timeout=20,
    )
    r.raise_for_status()
    for c in r.json()["data"]["children"]:
        d = c["data"]
        yield {
            "id": "reddit:" + d["id"],
            "title": d.get("title", ""),
            "text": d.get("selftext", ""),
            "url": "https://reddit.com" + d.get("permalink", ""),
            "score": d.get("score", 0),
            "ts": d.get("created_utc"),
        }


def parse_telegram_html(page: str):
    for block in page.split("tgme_widget_message_wrap")[1:]:
        m = re.search(r'data-post="([^"]+)"', block)
        t = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', block, re.S)
        if not (m and t):
            continue
        text = strip_tags(re.sub(r"<br\s*/?>", "\n", t.group(1)))
        yield {
            "id": "tg:" + m.group(1),
            "title": text.split("\n")[0][:200],
            "text": text,
            "url": "https://t.me/" + m.group(1),
            "score": 0,
        }


def fetch_telegram_channel(name: str):
    r = requests.get(f"https://t.me/s/{name}", headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    yield from parse_telegram_html(r.text)


def fetch_x(query: str):
    token = os.getenv("TWITTER_BEARER_TOKEN")
    if not token:
        return
    r = requests.get(
        "https://api.twitter.com/2/tweets/search/recent",
        params={"query": query, "max_results": 50, "tweet.fields": "public_metrics,lang"},
        headers={"Authorization": f"Bearer {token}", "User-Agent": UA},
        timeout=20,
    )
    r.raise_for_status()
    for tw in r.json().get("data", []):
        pm = tw.get("public_metrics", {})
        yield {
            "id": "x:" + tw["id"],
            "title": tw["text"].split("\n")[0][:200],
            "text": tw["text"],
            "url": f"https://x.com/i/web/status/{tw['id']}",
            "score": pm.get("like_count", 0) + 2 * pm.get("retweet_count", 0),
        }


def collect_posts(cfg, rss_fetch=fetch_rss, reddit_fetch=fetch_reddit, tg_fetch=fetch_telegram_channel, x_fetch=fetch_x):
    posts = []
    src = cfg["sources"]
    for kind, items, fn in (("rss", src.get("rss", []), rss_fetch), ("reddit", src.get("reddit_subs", []), reddit_fetch),
                            ("telegram", src.get("telegram_channels", []), tg_fetch), ("x", src.get("x_queries", []), x_fetch)):
        for it in items or []:
            try:
                posts += list(fn(it))
            except Exception as e:
                log.warning("%s %s: %s", kind, it, e)
    return posts


# --------------------------------------------------------------------------- 4. posts: analysis
def llm_json(prompt: str):
    """Опційно: Claude. Потрібен ANTHROPIC_API_KEY і llm.enabled: true."""
    try:
        import anthropic

        msg = anthropic.Anthropic().messages.create(
            model="claude-sonnet-5-5", max_tokens=400, messages=[{"role": "user", "content": prompt}]
        )
        return json.loads(msg.content[0].text)
    except Exception as e:
        log.warning("llm: %s", e)
        return None


def llm_triage(post: dict, cats):
    return llm_json(
        "Пост про ринок CS2 (EN/RU). Категорії: " + ", ".join(cats) + ". Відповідай ТІЛЬКИ JSON: "
        "{\"actionable\": true|false, \"items\": [назви скінів/наклейок], \"why\": \"1 речення\", \"risk\": \"1 речення\"}.\n\n"
        f"Заголовок: {post['title']}\nТекст: {post['text'][:1500]}"
    )


def has_craft_context(post: dict, craft_cfg: dict) -> bool:
    blob = (post["title"] + "\n" + post["text"]).lower()
    if STICKER_RE.search(post["title"] + "\n" + post["text"]):
        return True
    if any(h.lower() in blob for h in craft_cfg["hashtags"]):
        return True
    return any(re.search(r"(?<!\w)" + re.escape(w.lower()), blob) for w in craft_cfg["hot_words"])


def classify_post(post: dict, cfg: dict):
    """-> {категорія: [збіги]} для постів, що проходять пороги."""
    blob = post["title"] + "\n" + post["text"]
    ctx = has_craft_context(post, cfg["craft"])
    found = {}
    for cat, c in cfg["categories"].items():
        hits = [p for p in c["patterns"] if re.search(p, blob, re.I)]
        if not hits:
            continue
        if c.get("need_craft_context") and not ctx:
            continue
        if post.get("score", 0) < c.get("min_score", 0):
            continue
        found[cat] = hits
    return found


def event_hits(post: dict, keywords) -> list:
    blob = (post["title"] + " " + post["text"]).lower()
    return [k for k in keywords if k.lower() in blob]


def process_events(cfg, db, notifier, posts):
    for p in posts:
        if not p["id"].startswith("rss:"):
            continue  # події беремо лише з офіційних джерел; обговорення йдуть у process_posts
        if too_old(p, cfg.get("max_post_age_hours", 72)):
            continue
        hits = event_hits(p, cfg["event_keywords"])
        if hits and is_new(db, "event:" + p["id"]):
            notifier.send("event:" + p["id"], f"🗞 Подія CS2: {p['title']}\nКлючові слова: {', '.join(hits)}\n{p['url']}")


def process_posts(cfg, db, notifier, posts):
    use_llm = cfg.get("llm", {}).get("enabled") and os.getenv("ANTHROPIC_API_KEY")
    for p in posts:
        if p["id"].startswith("rss:") or too_old(p, cfg.get("max_post_age_hours", 72)):
            continue
        stickers = sorted({s.strip() for s in STICKER_RE.findall(p["title"] + "\n" + p["text"])})
        for s in stickers:
            db.execute("INSERT INTO mentions VALUES(?,?,?)", (s, now_ts(), p["id"]))
        db.commit()
        found = classify_post(p, cfg)
        if not found or not is_new(db, "post:" + p["id"]):
            continue
        titles = ", ".join(f"{cfg['categories'][c]['emoji']} {cfg['categories'][c]['title']}" for c in found)
        text = f"{titles}\n{p['title'][:160]}\n"
        if stickers:
            text += "Наклейки: " + ", ".join(stickers) + "\n"
        text += p["url"]
        if use_llm:
            t = llm_triage(p, list(found))
            if t:
                text += f"\nAI: {'варто дивитись' if t.get('actionable') else 'схоже на шум'}. {t.get('why', '')} Ризик: {t.get('risk', '')}"
        notifier.send("post:" + p["id"], text)
    # тренд: наклейки, згадки яких різко зросли
    craft_cfg = cfg["craft"]
    for (s,) in db.execute("SELECT DISTINCT sticker FROM mentions WHERE ts>?", (now_ts() - 86400,)).fetchall():
        last, base = sticker_trend(db, s)
        if last >= craft_cfg.get("trend_min_mentions", 3) and last > 2 * max(base, 0.5):
            notifier.send(f"trend:{s}", f"📊 Росте інтерес до наклейки: {s}\nЗгадок за добу: {last} (звичайно ~{base:.1f}/добу)")


def sticker_trend(db, sticker: str):
    """Згадки за останню добу vs середнє/добу за попередні 6 днів."""
    t = now_ts()
    last = db.execute("SELECT COUNT(DISTINCT post_id) FROM mentions WHERE sticker=? AND ts>?", (sticker, t - 86400)).fetchone()[0]
    prev = db.execute("SELECT COUNT(DISTINCT post_id) FROM mentions WHERE sticker=? AND ts<=? AND ts>?",
                      (sticker, t - 86400, t - 7 * 86400)).fetchone()[0]
    return last, prev / 6


# --------------------------------------------------------------------------- 5. Telegram commands
def handle_command(db, cfg, text, fetch=fetch_steam_price):
    cmd, _, rest = text.strip().partition(" ")
    cmd, rest = cmd.split("@")[0].lower(), rest.strip()
    fee = cfg.get("steam_fee", 1.15)
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/add":
        if not rest:
            return "Формат: /add AK-47 | Redline (Field-Tested)"
        try:
            price, vol = fetch(rest, cfg["currency"])
        except Exception as e:
            return f"Не вдалося перевірити ціну зараз ({e}). Спробуйте пізніше."
        if price is None:
            return "Не знайшов на Steam Market. Назва має бути точною (market_hash_name)."
        db.execute("INSERT OR IGNORE INTO watch VALUES(?,?)", (rest, now_ts()))
        db.execute("INSERT INTO prices VALUES(?,?,?,?)", (rest, now_ts(), price, vol))
        db.commit()
        return f"✅ Стежу: {rest}\nЗараз: {price:.2f}"
    if cmd == "/remove":
        cur = db.execute("DELETE FROM watch WHERE name=?", (rest,))
        db.commit()
        return "🗑 Прибрано." if cur.rowcount else "Такого предмета немає у списку."
    if cmd == "/list":
        names = watch_names(db)
        if not names:
            return "Список порожній. Додайте: /add <назва>"
        return "\n".join(f"• {n}: {last_price(db, n):.2f}" if last_price(db, n) else f"• {n}: ще немає ціни" for n in names)
    if cmd == "/target":
        m = re.match(r"^(.*?)\s*(<|>|below|above)\s*([\d.,]+)$", rest)
        if not m:
            return "Формат: /target AK-47 | Redline (Field-Tested) < 12.5"
        name, op, val = m.group(1).strip(), m.group(2), parse_price(m.group(3))
        direction = "below" if op in ("<", "below") else "above"
        db.execute("INSERT OR IGNORE INTO watch VALUES(?,?)", (name, now_ts()))
        db.execute("INSERT INTO targets(name, price, direction) VALUES(?,?,?)", (name, val, direction))
        db.commit()
        return f"🎯 Алерт: {name} {'<' if direction == 'below' else '>'} {val:.2f}"
    if cmd == "/buy":
        m = re.match(r"^(.*?)\s+@\s*([\d.,]+)(?:\s+x(\d+))?(?:\s+sl(\d+(?:\.\d+)?))?(?:\s+tp(\d+(?:\.\d+)?))?$", rest)
        if not m:
            return "Формат: /buy M4A4 | Howl (Minimal Wear) @ 3.2 x2 sl15 tp40"
        name, price = m.group(1).strip(), parse_price(m.group(2))
        qty = int(m.group(3) or 1)
        sl = float(m.group(4)) if m.group(4) else None
        tp = float(m.group(5)) if m.group(5) else None
        db.execute("INSERT OR IGNORE INTO watch VALUES(?,?)", (name, now_ts()))
        db.execute("INSERT INTO positions(name, price, qty, sl, tp, ts) VALUES(?,?,?,?,?,?)", (name, price, qty, sl, tp, now_ts()))
        db.commit()
        return f"📌 Позиція: {name} x{qty} @ {price:.2f}" + (f", SL {sl:g}%" if sl else "") + (f", TP {tp:g}%" if tp else "")
    if cmd == "/portfolio":
        rows = db.execute("SELECT name, price, qty FROM positions").fetchall()
        if not rows:
            return "Позицій немає. Додайте: /buy <назва> @ <ціна> x<к-сть>"
        lines, cost_sum, net_sum = [], 0.0, 0.0
        for name, cost, qty in rows:
            p = last_price(db, name)
            if p is None:
                lines.append(f"• {name} x{qty}: ще немає ціни")
                continue
            net = p / fee
            cost_sum += cost * qty
            net_sum += net * qty
            lines.append(f"• {name} x{qty}: вхід {cost:.2f}, зараз {p:.2f}, чистий PnL {(net / cost - 1) * 100:+.1f}%")
        if cost_sum:
            lines.append(f"\nРазом: вкладено {cost_sum:.2f}, чиста вартість {net_sum:.2f} ({(net_sum / cost_sum - 1) * 100:+.1f}%)")
        return "\n".join(lines)
    if cmd == "/movers":
        return top_movers_text(db)
    if cmd == "/status":
        n_watch = db.execute("SELECT COUNT(*) FROM watch").fetchone()[0]
        n_pos = db.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        n_tg = db.execute("SELECT COUNT(*) FROM targets WHERE active=1").fetchone()[0]
        return f"Працюю. У стежені: {n_watch}, цілей: {n_tg}, позицій: {n_pos}."
    return "Невідома команда. /help"


def poll_commands(cfg, db, notifier, fetch=fetch_steam_price):
    if not (notifier.token and notifier.chat):
        return
    offset = int(meta_get(db, "tg_offset", "0"))
    try:
        r = requests.get(f"https://api.telegram.org/bot{notifier.token}/getUpdates",
                         params={"offset": offset, "timeout": 0}, timeout=20).json()
    except (requests.RequestException, ValueError) as e:
        log.warning("getUpdates: %s", e)
        return
    for upd in r.get("result", []):
        offset = upd["update_id"] + 1
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(notifier.chat):
            continue  # слухаємо лише власника
        text = (msg.get("text") or "").strip()
        if text.startswith("/"):
            notifier.reply(handle_command(db, cfg, text, fetch))
    meta_set(db, "tg_offset", offset)


# --------------------------------------------------------------------------- main
def process_news(cfg, db, notifier):
    """Новини й пости. Перший запуск мовчки запам'ятовує все наявне, щоб не засипати старим."""
    posts = collect_posts(cfg)
    first = meta_get(db, "baseline_done") != "1"
    notifier.silent = first
    try:
        process_events(cfg, db, notifier, posts)
        process_posts(cfg, db, notifier, posts)
    finally:
        notifier.silent = False
    if first and posts:
        meta_set(db, "baseline_done", "1")


def run_once(cfg, db, notifier):
    seed_watch(cfg, db)
    poll_commands(cfg, db, notifier)
    process_news(cfg, db, notifier)
    check_prices(cfg, db, notifier)
    check_targets_and_positions(cfg, db, notifier)
    scan_movers(cfg, db, notifier)
    check_spread(cfg, db, notifier)


def run_loop(cfg, db, notifier):
    seed_watch(cfg, db)
    last_prices = last_news = 0.0
    while True:
        t = time.time()
        try:
            poll_commands(cfg, db, notifier)
            if t - last_news >= cfg["poll_news_minutes"] * 60:
                process_news(cfg, db, notifier)
                last_news = t
            if t - last_prices >= cfg["poll_prices_minutes"] * 60:
                check_prices(cfg, db, notifier)
                check_targets_and_positions(cfg, db, notifier)
                scan_movers(cfg, db, notifier)
                check_spread(cfg, db, notifier)
                last_prices = t
        except Exception:
            log.exception("loop error")
        time.sleep(20)


def selftest():
    cfg = load_config(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"))
    db = init_db(":memory:")
    n = Notifier(db)
    n.token = n.chat = None
    t = now_ts()

    # --- утиліти
    assert parse_price("12,34€") == 12.34 and parse_price("$1,234.56") == 1234.56 and parse_price("1.234,56€") == 1234.56
    assert parse_volume("1,234") == 1234

    # --- зміна ціни + сплеск обсягу
    for i, (p, v) in enumerate([(10.0, 100), (10.1, 110), (9.9, 90), (11.0, 400)]):
        db.execute("INSERT INTO prices VALUES(?,?,?,?)", ("Test Case", t - (3 - i) * 3600, p, v))
    msgs = evaluate_price(db, "Test Case", cfg["alerts"])
    assert any("📈" in m for _, m in msgs) and any("🔥" in m for _, m in msgs), msgs

    # --- індикатори
    assert sma([10] * 30 + [10.5], 6) > sma([10] * 30 + [10.5], 24)
    rising = [10 + 0.1 * i for i in range(30)]
    for i, p in enumerate(rising):
        db.execute("INSERT INTO prices VALUES(?,?,?,?)", ("Rising", t - (29 - i) * 1800, p, 50))
    sig = " ".join(m for _, m in indicator_signals(db, "Rising", cfg["indicators"]))
    assert "RSI" in sig, sig
    noisy = [10, 10.1] * 15 + [12]
    for i, p in enumerate(noisy):
        db.execute("INSERT INTO prices VALUES(?,?,?,?)", ("Spike", t - (30 - i) * 1800, p, 50))
    sig = " ".join(m for _, m in indicator_signals(db, "Spike", cfg["indicators"]))
    assert "Аномальний рух" in sig, sig
    # sma-перетин: перевіряємо напряму
    flat = [10.0] * 30
    ind = cfg["indicators"]
    s_prev, l_prev = sma(flat, ind["sma_short"]), sma(flat, ind["sma_long"])
    s_now, l_now = sma(flat + [10.5], ind["sma_short"]), sma(flat + [10.5], ind["sma_long"])
    assert s_prev <= l_prev and s_now > l_now

    # --- категорії постів EN + RU
    def cat(title, text="", score=0):
        return set(classify_post({"id": "x", "title": title, "text": text, "url": "u", "score": score}, cfg))

    assert "nsfw_craft" in cat("New NSFW craft with 4x Sticker | Titan (Holo) | Katowice 2014")
    assert "nsfw_craft" in cat("нсфв крафт на AK, наклейки голо")
    assert "nsfw_craft" not in cat("nsfw something without any context at all")  # без контексту крафту
    assert "budget_look" in cat("Budget Howl craft: M4A4 looks like a Howl for $3", "stickers 4x holo")
    assert "budget_look" in cat("Бюджетный хоул: M4A4 выглядит как Howl за 3$", "крафт наклейками")
    assert "wear_upgrade" in cat("Battle-Scarred to Factory New with stickers, scrape craft")
    assert "wear_upgrade" in cat("Скрейп крафт: BS -> FN на наклейках")
    assert "chinese_pump" in cat("Chinese are pumping this skin on buff163", score=50)
    assert "chinese_pump" not in cat("Chinese are pumping this skin", score=1)  # поріг скору
    assert "chinese_pump" in cat("Китайцы качают этот скин, памп", score=20)
    assert "fast_drop" in cat("AWP Asiimov is crashing hard, dump", score=30)
    assert "fast_drop" in cat("Скин падает, обвал цены", score=30)
    assert not cat("Selling my AK-47 Redline for fair price")

    # --- повний цикл по постах + дедуп
    post = {"id": "reddit:x1", "title": "NSFW craft: 4x Sticker | Titan (Holo) | Katowice 2014 #craft", "text": "clean scrape",
            "url": "https://reddit.com/x1", "score": 120}
    stickers = STICKER_RE.findall(post["title"])
    assert [s.strip() for s in stickers] == ["Sticker | Titan (Holo) | Katowice 2014"], stickers
    process_posts(cfg, db, n, [post])
    process_posts(cfg, db, n, [post])
    assert sum("Наклейки" in s for s in n.sent) == 1, n.sent

    # --- події (EN + RU)
    ev = {"id": "rss:1", "title": "Release Notes: new case and trade hold change", "text": "", "url": "u", "score": 0}
    ev_ru = {"id": "rss:2", "title": "Обновление CS2: новый кейс", "text": "", "url": "u", "score": 0}
    process_events(cfg, db, n, [ev, ev_ru])
    assert sum("🗞" in s for s in n.sent) == 2

    # --- старі новини не надсилаються; перший прохід мовчить
    old_ev = {"id": "rss:old", "title": "Update from a month ago", "text": "", "url": "u", "score": 0, "ts": t - 30 * 86400}
    fresh_ev = {"id": "rss:fresh", "title": "Update today", "text": "", "url": "u", "score": 0, "ts": t - 3600}
    before = len(n.sent)
    process_events(cfg, db, n, [old_ev, fresh_ev])
    assert [s for s in n.sent[before:] if "month ago" in s] == [] and any("Update today" in s for s in n.sent[before:])
    db2 = init_db(":memory:")
    n2 = Notifier(db2)
    n2.token = n2.chat = None
    n2.silent = True
    process_events(cfg, db2, n2, [{"id": "rss:z", "title": "Update", "text": "", "url": "u", "score": 0}])
    assert n2.sent == [] and not is_new(db2, "event:rss:z")  # запам'ятали, але не надіслали

    # --- Telegram-канал: парсер
    page = ('<div class="tgme_widget_message_wrap" data-post="chan/12"><div class="tgme_widget_message_text js-message_text">'
            'Новый крафт: AK-47 выглядит как FN<br/>наклейки голо</div></div>')
    posts_tg = list(parse_telegram_html(page))
    assert posts_tg and posts_tg[0]["id"] == "tg:chan/12" and "наклейки" in posts_tg[0]["text"], posts_tg

    # --- команди
    fake = lambda name, cur: (12.0, 100) if "Redline" in name else (None, None)
    assert "Стежу" in handle_command(db, cfg, "/add AK-47 | Redline (Field-Tested)", fake)
    assert "Не знайшов" in handle_command(db, cfg, "/add Nonexistent Thing", fake)
    assert "Redline" in handle_command(db, cfg, "/list", fake)
    assert "🎯" in handle_command(db, cfg, "/target AK-47 | Redline (Field-Tested) < 13", fake)
    assert "📌" in handle_command(db, cfg, "/buy AK-47 | Redline (Field-Tested) @ 8 x2 sl10 tp30", fake)
    assert "PnL" in handle_command(db, cfg, "/portfolio", fake)
    assert "Працюю" in handle_command(db, cfg, "/status", fake)
    assert "Невідома" in handle_command(db, cfg, "/blah", fake)
    check_targets_and_positions(cfg, db, n)
    assert any("🎯" in s for s in n.sent), "target не спрацював"
    assert any("💰" in s for s in n.sent), "take-profit не спрацював"  # 12/1.15 = 10.4 проти входу 8 => +30.4%
    assert "Прибрано" in handle_command(db, cfg, "/remove AK-47 | Redline (Field-Tested)", fake)

    # --- сканер movers: pump, dump, скупка
    for name, p0, l0 in (("PumpItem", 10.0, 100), ("DumpItem", 10.0, 100), ("SqueezeItem", 10.0, 100), ("Calm", 10.0, 100)):
        db.execute("INSERT INTO scan VALUES(?,?,?,?)", (name, t - 20 * 3600, p0, l0))
    fetch_scan = lambda cur, pages: [
        {"name": "PumpItem", "price": 12.0, "listings": 100}, {"name": "DumpItem", "price": 8.0, "listings": 150},
        {"name": "SqueezeItem", "price": 10.5, "listings": 60}, {"name": "Calm", "price": 10.1, "listings": 100}]
    before = len(n.sent)
    scan_movers(cfg, db, n, fetch_scan)
    new = " ".join(n.sent[before:])
    assert "PUMP PumpItem" in new and "DUMP DumpItem" in new and "СКУПКА ПРОПОЗИЦІЇ SqueezeItem" in new and "Calm" not in new, new
    assert "PumpItem" in top_movers_text(db)

    # --- спред Skinport
    db.execute("INSERT INTO prices VALUES(?,?,?,?)", ("Spread Item", t, 23.0, 10))
    db.execute("INSERT INTO prices VALUES(?,?,?,?)", ("Premium Item", t, 20.0, 10))
    db.execute("INSERT OR IGNORE INTO watch VALUES(?,?)", ("Spread Item", t))
    db.execute("INSERT OR IGNORE INTO watch VALUES(?,?)", ("Premium Item", t))
    sp_data = {"Spread Item": (15.0, 5), "Premium Item": (19.8, 3)}  # 23/1.15=20 => +33%; 19.8/20 >= 0.97
    before = len(n.sent)
    check_spread(cfg, db, n, lambda c: sp_data)
    new = " ".join(n.sent[before:])
    assert "СПРЕД Spread Item" in new and "🌐 Premium Item" in new, new

    print(f"selftest OK: {len(n.sent)} alerts/replies generated")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["selftest", "once", "run"])
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"))
    ap.add_argument("--db", default="radar.db")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.cmd == "selftest":
        return selftest()
    cfg = load_config(args.config)
    db = init_db(args.db)
    notifier = Notifier(db)
    (run_once if args.cmd == "once" else run_loop)(cfg, db, notifier)


if __name__ == "__main__":
    sys.exit(main())
