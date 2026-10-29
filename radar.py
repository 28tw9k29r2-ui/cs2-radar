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
from datetime import datetime
from email.utils import parsedate_to_datetime

import requests
import yaml

UA = "cs2-radar/0.3 (personal research tool)"
CUR = ""            # символ валюти для виводу (config.currency_symbol), напр. "₴"
LAST_MEDIAN = {}    # остання медіана реальних угод зі Steam по предмету (заповнює fetch_steam_price)
# Валюти, у яких Skinport віддає ціни, і відповідні id валют Steam (UAH там немає, тож для UAH спред вимкнено)
SKINPORT_CUR = {"USD": 1, "GBP": 2, "EUR": 3, "CHF": 4, "RUB": 5, "PLN": 6, "BRL": 7, "NOK": 9, "TRY": 17,
                "CAD": 20, "AUD": 21, "CNY": 23}
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
    "/target <назва> < 500 — алерт, коли ціна впаде нижче 500 ₴ (або > для росту)\n"
    "/buy <назва> @ 130 x2 sl15 tp40 — позиція (ціна входу в ₴, к-сть, stop-loss %, take-profit %)\n"
    "/portfolio — PnL позицій з урахуванням комісії Steam\n"
    "/movers — топ росту/падіння за останню добу\n"
    "/status — стан бота\n"
    "/analyze [назва] — розбір предмета (без назви: зведення по списку)\n"
    "/review — огляд портфеля і ризиків\n"
    "/report — зведення для копіювання в чат із Claude\n\n"
    "Назва має бути точною, як у Steam Market, напр.: AK-47 | Redline (Field-Tested)\n"
    "Усі ціни в гривнях: Steam віддає їх сам, без перерахунку за курсом НБУ."
)


# --------------------------------------------------------------------------- utils
def now_ts() -> int:
    return int(time.time())


def load_config(path: str) -> dict:
    global CUR
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    CUR = cfg.get("currency_symbol", "")
    return cfg


def mny(x) -> str:
    """Сума з символом валюти: 123.45 ₴"""
    return f"{x:.2f} {CUR}".rstrip()


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
        CREATE TABLE IF NOT EXISTS sales(name TEXT, ts INTEGER, median REAL);
        CREATE INDEX IF NOT EXISTS idx_sales ON sales(name, ts);
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


def due(db, key: str, minutes: float) -> bool:
    """Чи минув інтервал від останнього виконання (із запасом 5 хв на розкид розкладу GitHub)."""
    last = float(meta_get(db, "last:" + key, "0"))
    return now_ts() - last >= minutes * 60 - 300


def mark(db, key: str):
    meta_set(db, "last:" + key, now_ts())


def steam_blocked_for(db) -> int:
    """Скільки секунд ще діє пауза після 429 від Steam (0 = можна робити запити)."""
    return max(0, int(float(meta_get(db, "steam_block_until", "0")) - now_ts()))


def steam_hit(db, ok: bool, base_minutes: float = 30):
    """Запам'ятовує успіх чи 429. Після 429 пауза росте вдвічі (до 4 год), успіх її скидає."""
    if ok:
        meta_set(db, "steam_block_until", 0)
        meta_set(db, "steam_429_streak", 0)
        return
    streak = int(meta_get(db, "steam_429_streak", "0")) + 1
    meta_set(db, "steam_429_streak", streak)
    minutes = min(base_minutes * 2 ** (streak - 1), 240)
    meta_set(db, "steam_block_until", now_ts() + int(minutes * 60))


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

    def send(self, key: str, text: str, cooldown_hours=None):
        """Не шле той самий ключ частіше ніж раз на cooldown."""
        if self.silent:
            return
        cutoff = now_ts() - (cooldown_hours * 3600 if cooldown_hours else self.cooldown)
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
    # lowest_price = найдешевша пропозиція зараз; median_price = медіана реальних угод за останню добу.
    # Обидві Steam віддає прямо у валюті currency (напр. 18 = UAH), без жодного перерахунку.
    low = parse_price(data.get("lowest_price"))
    med = parse_price(data.get("median_price"))
    if med:
        LAST_MEDIAN[name] = med
    return low or med, parse_volume(data.get("volume"))


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
                        f"{arrow} {name}\nЦіна {mny(base[1])} → {mny(p_now)} ({pct:+.1f}% за ~{cfg_alerts['window_hours']}г)"))
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
            out.append((f"sma:{name}:up", f"🟢 {name}\nSMA{ind['sma_short']} перетнула SMA{ind['sma_long']} вгору (бичачий сигнал). Ціна {mny(vals[-1])}"))
        elif s_prev >= l_prev and s_now < l_now:
            out.append((f"sma:{name}:down", f"🔴 {name}\nSMA{ind['sma_short']} перетнула SMA{ind['sma_long']} вниз (ведмежий сигнал). Ціна {mny(vals[-1])}"))
    r = rsi(vals, ind["rsi_period"])
    if r is not None:
        if r >= ind["rsi_overbought"]:
            out.append((f"rsi:{name}:hi", f"🌡 {name}\nRSI {r:.0f}: перегрітий ріст, можливий відкат. Ціна {mny(vals[-1])}"))
        elif r <= ind["rsi_oversold"]:
            out.append((f"rsi:{name}:lo", f"🧊 {name}\nRSI {r:.0f}: перепроданість, можливий відскок. Ціна {mny(vals[-1])}"))
    z, move = zscore(vals, ind["z_window"])
    if z is not None and abs(z) >= ind["z_threshold"] and abs(move) >= ind["z_min_move_pct"]:
        word = "різкий стрибок" if z > 0 else "різкий провал"
        out.append((f"z:{name}:{'up' if z > 0 else 'down'}",
                    f"⚡ {name}\nАномальний рух: {word} ({move:+.1f}% від середнього, z={z:.1f}). Ціна {mny(vals[-1])}"))
    return out


def check_prices(cfg, db, notifier, fetch=fetch_steam_price, delay=3.5):
    """Ціни зі списку відстеження. Щоб не ловити 429 від Steam:
    - за прохід не більше max_price_requests_per_run запитів, далі по колу (round-robin);
    - після 429 робимо паузу (30 хв, потім удвічі довше, до 4 год), у цей час Steam не чіпаємо."""
    wait = steam_blocked_for(db)
    if wait:
        log.info("Steam просив зачекати: пропускаю ціни ще ~%d хв", wait // 60 + 1)
        return
    names = watch_names(db)
    if not names:
        return
    budget = cfg.get("max_price_requests_per_run", 20)
    start = int(meta_get(db, "rr_offset", "0")) % len(names)
    order = names[start:] + names[:start]
    processed = 0
    for name in order:
        if processed >= budget:
            break
        try:
            price, vol = fetch(name, cfg["currency"])
        except Exception as e:  # мережа / ліміт — не валимо весь цикл
            log.warning("price %s: %s", name, e)
            if "429" in str(e):
                steam_hit(db, False, cfg.get("steam_block_minutes", 30))
                break  # цей предмет перевіримо першим після паузи
            processed += 1
            continue
        steam_hit(db, True)
        processed += 1
        if price is None:
            log.info("no price for %s", name)
            misses = int(meta_get(db, "miss:" + name, "0")) + 1
            meta_set(db, "miss:" + name, misses)
            if misses == 3:
                notifier.send(f"miss:{name}", f"❓ Не знаходжу ціну для «{name}» (3 проходи поспіль).\n"
                              "Перевірте точну назву зі Steam Market або приберіть: /remove <назва>")
            continue
        meta_set(db, "miss:" + name, 0)
        db.execute("INSERT INTO prices VALUES(?,?,?,?)", (name, now_ts(), price, vol))
        med = LAST_MEDIAN.pop(name, None)
        if med:
            db.execute("INSERT INTO sales VALUES(?,?,?)", (name, now_ts(), med))
        db.commit()
        for key, text in evaluate_price(db, name, cfg["alerts"]) + indicator_signals(db, name, cfg["indicators"]):
            notifier.send(key, text)
        time.sleep(delay)
    meta_set(db, "rr_offset", (start + processed) % len(names))


def check_targets_and_positions(cfg, db, notifier):
    fee = cfg.get("steam_fee", 1.15)
    for tid, name, target, direction in db.execute("SELECT id, name, price, direction FROM targets WHERE active=1").fetchall():
        p = last_price(db, name)
        if p is None:
            continue
        if (direction == "below" and p <= target) or (direction == "above" and p >= target):
            notifier.send(f"target:{tid}", f"🎯 {name}\nЦіна {mny(p)} {'≤' if direction == 'below' else '≥'} цілі {mny(target)}")
            db.execute("UPDATE targets SET active=0 WHERE id=?", (tid,))
            db.commit()
    for pid, name, cost, qty, sl, tp in db.execute("SELECT id, name, price, qty, sl, tp FROM positions").fetchall():
        p = last_price(db, name)
        if p is None or not cost:
            continue
        pnl = (p / fee / cost - 1) * 100
        # Один алерт на подію, а не кожні 6 годин: прапорець знімається, коли PnL відійшов від порога на 2 п.п.
        if sl:
            flag = f"slfired:{pid}"
            if pnl <= -sl:
                if meta_get(db, flag) != "1":
                    notifier.send(f"sl:{pid}", f"🛑 STOP-LOSS {name}\nPnL {pnl:+.1f}% (чисто, після комісії). Вхід {mny(cost)}, зараз {mny(p)}")
                    meta_set(db, flag, 1)
            elif pnl > -sl + 2:
                meta_set(db, flag, 0)
        if tp:
            flag = f"tpfired:{pid}"
            if pnl >= tp:
                if meta_get(db, flag) != "1":
                    notifier.send(f"tp:{pid}", f"💰 TAKE-PROFIT {name}\nPnL {pnl:+.1f}% (чисто, після комісії). Вхід {mny(cost)}, зараз {mny(p)}")
                    meta_set(db, flag, 1)
            elif pnl < tp - 2:
                meta_set(db, flag, 0)


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
            # ціну беремо з тексту (він у запитаній валюті, напр. UAH); числове поле лише запасний варіант
            price = parse_price(it.get("sell_price_text")) if it.get("sell_price_text") else None
            if price is None and it.get("sell_price"):
                price = it["sell_price"] / 100.0
            if price:
                out.append({"name": it.get("hash_name") or it.get("name"), "price": price,
                            "listings": it.get("sell_listings", 0)})
        time.sleep(delay)
    return out


def check_scan_currency(db, notifier, items):
    """Страховка: ціни сканера мають збігатися з цінами списку відстеження (та сама валюта).
    Якщо розбіжність > 1.6 раза, пошук Steam віддає іншу валюту, і абсолютним цінам сканера вірити не можна."""
    t = now_ts()
    ratios = []
    for it in items:
        r = db.execute("SELECT price FROM prices WHERE name=? AND ts>? ORDER BY ts DESC LIMIT 1", (it["name"], t - 10800)).fetchone()
        if r and r[0] and it["price"]:
            ratios.append(it["price"] / r[0])
    if not ratios:
        return True
    ratios.sort()
    med = ratios[len(ratios) // 2]
    if med > 1.6 or med < 1 / 1.6:
        notifier.send("currency-mismatch",
                      f"⚠️ Ціни сканера й списку відстеження розходяться приблизно в {med:.1f} раза: схоже, пошук Steam віддає іншу валюту. "
                      "Відсотки руху в /movers лишаються правильними, а самі суми в сканері вірити не можна.", cooldown_hours=24)
        return False
    return True


def scan_movers(cfg, db, notifier, fetch=fetch_market_popular):
    m = cfg["movers"]
    if not m.get("enabled") or steam_blocked_for(db):
        return
    try:
        items = fetch(cfg["currency"], m["pages"])
    except Exception as e:
        log.warning("movers: %s", e)
        if "429" in str(e):
            steam_hit(db, False, cfg.get("steam_block_minutes", 30))
        return
    steam_hit(db, True)
    check_scan_currency(db, notifier, items)
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
        notifier.send(f"pump:{it['name']}", f"🚀 PUMP {it['name']}\n{mny(base[0])} → {mny(it['price'])} ({pct:+.1f}%), лістингів {it['listings']}")
    for pct, it, base in sorted(dumps, key=lambda x: x[0])[: m["max_alerts"]]:
        notifier.send(f"dump:{it['name']}", f"🔻 DUMP {it['name']}\n{mny(base[0])} → {mny(it['price'])} ({pct:+.1f}%), лістингів {it['listings']}")
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
    fmt = lambda r: f"{r[0]:+.1f}% {r[1]} ({mny(r[2])}→{mny(r[3])})"
    up, down = [r for r in rows[::-1] if r[0] > 0][:n], [r for r in rows if r[0] < 0][:n]
    return "🚀 Ріст:\n" + ("\n".join(fmt(r) for r in up) or "—") + "\n\n🔻 Падіння:\n" + ("\n".join(fmt(r) for r in down) or "—")


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
    # Спред має сенс лише коли Skinport і Steam віддають ціни в одній валюті, без перерахунку за курсом.
    if SKINPORT_CUR.get(str(cfg.get("skinport_currency", "")).upper()) != cfg.get("currency"):
        log.info("спред вимкнено: Skinport не віддає ціни у валюті Steam (id %s)", cfg.get("currency"))
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
                          f"💱 СПРЕД {name}\nSkinport {mny(sp_price)} → Steam {mny(s_price)} (у гаманець ≈{mny(s_price / fee)}) = ROI {roi:+.1f}%\n"
                          "Увага: гроші лишаються в гаманці Steam, а не в кеші.")
        elif sp_price / s_price >= sp["premium_ratio"]:
            notifier.send(f"premium:{name}",
                          f"🌐 {name}\nSkinport {mny(sp_price)} майже дорівнює Steam {mny(s_price)}: на реальному ринку попит тисне ціну вгору.")


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


_REDDIT_TOKEN = {"tok": None, "exp": 0.0}


def reddit_token():
    """OAuth-токен Reddit (REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET). Без нього Reddit часто блокує IP GitHub."""
    cid, sec = os.getenv("REDDIT_CLIENT_ID"), os.getenv("REDDIT_CLIENT_SECRET")
    if not (cid and sec):
        return None
    if _REDDIT_TOKEN["tok"] and time.time() < _REDDIT_TOKEN["exp"]:
        return _REDDIT_TOKEN["tok"]
    r = requests.post("https://www.reddit.com/api/v1/access_token", auth=(cid, sec),
                      data={"grant_type": "client_credentials"}, headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    j = r.json()
    _REDDIT_TOKEN.update(tok=j["access_token"], exp=time.time() + int(j.get("expires_in", 3600)) - 60)
    return _REDDIT_TOKEN["tok"]


def _iso_ts(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


ATOM = "{http://www.w3.org/2005/Atom}"


def parse_reddit_atom(content: bytes):
    """Запасний шлях, коли JSON заблоковано: Atom-стрічка сабреддіта (без рейтингу постів)."""
    root = ET.fromstring(content)
    for e in root.findall(ATOM + "entry"):
        pid = (e.findtext(ATOM + "id") or "").split("_", 1)[-1]
        link = e.find(ATOM + "link")
        yield {
            "id": "reddit:" + pid,
            "title": e.findtext(ATOM + "title") or "",
            "text": strip_tags(e.findtext(ATOM + "content") or ""),
            "url": link.get("href") if link is not None else "",
            "score": 0,
            "ts": _iso_ts(e.findtext(ATOM + "updated")),
        }


def fetch_reddit(sub: str, limit=50):
    try:
        tok = reddit_token()
    except (requests.RequestException, ValueError, KeyError) as e:
        log.warning("reddit token: %s", e)
        tok = None
    try:
        if tok:
            r = requests.get(f"https://oauth.reddit.com/r/{sub}/new", params={"limit": limit},
                             headers={"User-Agent": UA, "Authorization": f"bearer {tok}"}, timeout=20)
        else:
            r = requests.get(f"https://www.reddit.com/r/{sub}/new.json", params={"limit": limit},
                             headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
        children = r.json()["data"]["children"]
    except (requests.RequestException, ValueError, KeyError) as e:
        log.warning("reddit json %s: %s; пробую RSS", sub, e)
        r = requests.get(f"https://www.reddit.com/r/{sub}/new/.rss", params={"limit": limit},
                         headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
        yield from parse_reddit_atom(r.content)
        return
    for c in children:
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
        tm = re.search(r'<time[^>]*datetime="([^"]+)"', block)  # дата публікації, щоб не слати старі пости
        yield {
            "id": "tg:" + m.group(1),
            "title": text.split("\n")[0][:200],
            "text": text,
            "url": "https://t.me/" + m.group(1),
            "score": 0,
            "ts": _iso_ts(tm.group(1)) if tm else None,
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
        params={"query": query, "max_results": 50, "tweet.fields": "public_metrics,lang,created_at"},
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
            "ts": _iso_ts(tw.get("created_at")),
        }


def collect_posts(cfg, rss_fetch=fetch_rss, reddit_fetch=fetch_reddit, tg_fetch=fetch_telegram_channel, x_fetch=fetch_x):
    posts = []
    src = cfg["sources"]
    for kind, items, fn in (("rss", src.get("rss", []), rss_fetch), ("reddit", src.get("reddit_subs", []), reddit_fetch),
                            ("telegram", src.get("telegram_channels", []), tg_fetch), ("x", src.get("x_queries", []), x_fetch)):
        for it in items or []:
            try:
                got = list(fn(it))
            except Exception as e:
                log.warning("%s %s: %s", kind, it, e)
                continue
            for p in got:
                p["src"] = f"{kind}:{it}"  # джерело, щоб нове джерело спершу «прочитувалось мовчки»
            posts += got
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


def _word_re(w: str) -> str:
    """Слово цілком ('gold' не збігається з 'golden'); зірочка в кінці ('наклейк*') дозволяє закінчення."""
    if w.endswith("*"):
        return r"(?<!\w)" + re.escape(w[:-1].lower())
    return r"(?<!\w)" + re.escape(w.lower()) + r"(?!\w)"


def has_craft_context(post: dict, craft_cfg: dict) -> bool:
    blob = (post["title"] + "\n" + post["text"]).lower()
    if STICKER_RE.search(post["title"] + "\n" + post["text"]):
        return True
    if any(h.lower() in blob for h in craft_cfg["hashtags"]):
        return True
    return any(re.search(_word_re(w), blob) for w in craft_cfg["hot_words"])


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
            # дата згадки = дата поста (не момент обробки), без дублів при повторному читанні тих самих постів
            if not db.execute("SELECT 1 FROM mentions WHERE sticker=? AND post_id=?", (s, p["id"])).fetchone():
                db.execute("INSERT INTO mentions VALUES(?,?,?)", (s, int(p.get("ts") or now_ts()), p["id"]))
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
            wait = steam_blocked_for(db)
            if wait:  # Steam щойно просив зачекати: не підливаємо масла
                raise RuntimeError(f"пауза після ліміту, ще ~{wait // 60 + 1} хв")
            price, vol = fetch(rest, cfg["currency"])
        except Exception as e:
            # Steam часто віддає 429 для спільних IP (GitHub Actions): додаємо без перевірки,
            # ціну бот підтягне при наступному проході, а про хибну назву попередить сам.
            if "429" in str(e):
                steam_hit(db, False, cfg.get("steam_block_minutes", 30))
            db.execute("INSERT OR IGNORE INTO watch VALUES(?,?)", (rest, now_ts()))
            db.commit()
            return (f"⚠️ Steam зараз не віддає ціну ({e}). Додав без перевірки: {rest}\n"
                    "Ціну підтягну при наступному проході; якщо назва хибна, повідомлю.")
        steam_hit(db, True)
        if price is None:
            return "Не знайшов на Steam Market. Назва має бути точною (market_hash_name)."
        db.execute("INSERT OR IGNORE INTO watch VALUES(?,?)", (rest, now_ts()))
        db.execute("INSERT INTO prices VALUES(?,?,?,?)", (rest, now_ts(), price, vol))
        med = LAST_MEDIAN.pop(rest, None)
        if med:
            db.execute("INSERT INTO sales VALUES(?,?,?)", (rest, now_ts(), med))
        db.commit()
        return f"✅ Стежу: {rest}\nЗараз: {mny(price)}" + (f" (медіана угод {mny(med)})" if med else "")
    if cmd == "/remove":
        cur = db.execute("DELETE FROM watch WHERE name=?", (rest,))
        db.commit()
        return "🗑 Прибрано." if cur.rowcount else "Такого предмета немає у списку."
    if cmd == "/list":
        names = watch_names(db)
        if not names:
            return "Список порожній. Додайте: /add <назва>"
        return "\n".join(f"• {n}: {mny(last_price(db, n))}" if last_price(db, n) else f"• {n}: ще немає ціни" for n in names)
    if cmd == "/target":
        m = re.match(r"^(.*?)\s*(<|>|below|above)\s*([\d.,]+)$", rest)
        if not m:
            return "Формат: /target AK-47 | Redline (Field-Tested) < 500   (сума в гривнях)"
        name, op, val = m.group(1).strip(), m.group(2), parse_price(m.group(3))
        direction = "below" if op in ("<", "below") else "above"
        db.execute("INSERT OR IGNORE INTO watch VALUES(?,?)", (name, now_ts()))
        db.execute("INSERT INTO targets(name, price, direction) VALUES(?,?,?)", (name, val, direction))
        db.commit()
        return f"🎯 Алерт: {name} {'<' if direction == 'below' else '>'} {mny(val)}"
    if cmd == "/buy":
        m = re.match(r"^(.*?)\s+@\s*([\d.,]+)(?:\s+x(\d+))?(?:\s+sl(\d+(?:\.\d+)?))?(?:\s+tp(\d+(?:\.\d+)?))?$", rest)
        if not m:
            return "Формат: /buy M4A4 | Howl (Minimal Wear) @ 130 x2 sl15 tp40   (ціна входу в гривнях)"
        name, price = m.group(1).strip(), parse_price(m.group(2))
        qty = int(m.group(3) or 1)
        sl = float(m.group(4)) if m.group(4) else None
        tp = float(m.group(5)) if m.group(5) else None
        db.execute("INSERT OR IGNORE INTO watch VALUES(?,?)", (name, now_ts()))
        db.execute("INSERT INTO positions(name, price, qty, sl, tp, ts) VALUES(?,?,?,?,?,?)", (name, price, qty, sl, tp, now_ts()))
        db.commit()
        return f"📌 Позиція: {name} x{qty} @ {mny(price)}" + (f", SL {sl:g}%" if sl else "") + (f", TP {tp:g}%" if tp else "")
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
            lines.append(f"• {name} x{qty}: вхід {mny(cost)}, зараз {mny(p)}, чистий PnL {(net / cost - 1) * 100:+.1f}%")
        if cost_sum:
            lines.append(f"\nРазом: вкладено {mny(cost_sum)}, чиста вартість {mny(net_sum)} ({(net_sum / cost_sum - 1) * 100:+.1f}%)")
        return "\n".join(lines)
    if cmd == "/movers":
        return top_movers_text(db)
    if cmd == "/status":
        n_watch = db.execute("SELECT COUNT(*) FROM watch").fetchone()[0]
        n_pos = db.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        n_tg = db.execute("SELECT COUNT(*) FROM targets WHERE active=1").fetchone()[0]
        n_priced = db.execute("SELECT COUNT(DISTINCT name) FROM prices").fetchone()[0]
        out = f"Працюю. У стежені: {n_watch} (з цінами: {n_priced}), цілей: {n_tg}, позицій: {n_pos}."
        last = float(meta_get(db, "last:prices", "0"))
        if last:
            out += f"\nОстанній збір цін: {(now_ts() - last) // 60:.0f} хв тому."
        wait = steam_blocked_for(db)
        if wait:
            out += (f"\n⏳ Steam обмежує запити (429). Наступна спроба через ~{wait // 60 + 1} хв, "
                    f"спроб поспіль: {meta_get(db, 'steam_429_streak', '0')}.")
        return out
    try:
        import advisor  # додаток: /analyze /review /report (файл advisor.py поруч)
    except ImportError:
        advisor = None
    if advisor:
        try:
            out = advisor.handle(db, cfg, cmd, rest)
        except Exception as e:  # помилка помічника не має валити весь прохід бота
            return f"⚠️ Помічник: {e}"
        if out is not None:
            return out
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
def process_news(cfg, db, notifier, collect=collect_posts):
    """Новини й пости. Кожне джерело при першій появі читається мовчки (запам'ятовуємо наявне),
    тож нове джерело, додане пізніше, не засипає старими постами."""
    posts = collect(cfg)
    by_src = {}
    for p in posts:
        by_src.setdefault(p.get("src", "?"), []).append(p)
    legacy = meta_get(db, "baseline_done") == "1"  # перші запуски до per-source логіки: rss/reddit/x уже «прочитані»
    known, fresh, marks = [], [], []
    for src, ps in by_src.items():
        key = "baseline:" + src
        done = meta_get(db, key) == "1" or (legacy and not src.startswith("telegram:"))
        (known if done else fresh).extend(ps)
        if meta_get(db, key) != "1":
            marks.append(key)
    if known:
        process_events(cfg, db, notifier, known)
        process_posts(cfg, db, notifier, known)
    if fresh:
        notifier.silent = True
        try:
            process_events(cfg, db, notifier, fresh)
            process_posts(cfg, db, notifier, fresh)
        finally:
            notifier.silent = False
    for key in marks:
        meta_set(db, key, 1)


def cleanup(cfg, db):
    """Раз на добу прибирає старі записи, щоб база (і кеш Actions) не росла безкінечно."""
    if not due(db, "cleanup", 24 * 60):
        return
    t = now_ts()
    r = cfg.get("retention_days", {})
    cut = lambda key, default: t - r.get(key, default) * 86400
    db.execute("DELETE FROM prices WHERE ts<?", (cut("prices", 30),))
    db.execute("DELETE FROM sales WHERE ts<?", (cut("prices", 30),))
    db.execute("DELETE FROM scan WHERE ts<?", (cut("scan", 7),))
    db.execute("DELETE FROM mentions WHERE ts<?", (cut("mentions", 14),))
    db.execute("DELETE FROM alerts WHERE ts<?", (cut("alerts", 14),))
    db.execute("DELETE FROM seen WHERE ts<?", (cut("seen", 30),))
    db.commit()
    mark(db, "cleanup")


def ensure_currency(cfg, db, notifier):
    """Історія цін у старій валюті (напр. EUR) не можна змішувати з гривнею: інакше індикатори побачать
    «стрибок ціни у 40 разів». При зміні валюти (або якщо валюта в базі невідома) ціни очищаються."""
    cur = str(cfg["currency"])
    stored = meta_get(db, "currency")
    if stored == cur:
        return
    had = sum(db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("prices", "sales", "scan"))
    if had:
        for t in ("prices", "sales", "scan"):
            db.execute(f"DELETE FROM {t}")
        db.commit()
        log.info("валюта змінилась (%s -> %s): стару історію цін очищено", stored, cur)
    meta_set(db, "currency", cur)
    if had and (db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] or db.execute("SELECT COUNT(*) FROM targets WHERE active=1").fetchone()[0]):
        notifier.reply(f"⚠️ Ціни тепер у гривнях ({CUR or cur}). Стару історію цін очищено. Суми у ваших цілях (/target) і позиціях (/buy) "
                       "лишилися в тій валюті, у якій ви їх вводили: перевірте їх і введіть заново в гривнях.")


def run_cycle(cfg, db, notifier):
    """Один тік: усе, що вже «настав час» робити. run_once викликає його раз, run_loop кожні 20 с."""
    seed_watch(cfg, db)
    ensure_currency(cfg, db, notifier)
    poll_commands(cfg, db, notifier)
    if due(db, "news", cfg["poll_news_minutes"]):
        process_news(cfg, db, notifier)
        mark(db, "news")
    if due(db, "prices", cfg["poll_prices_minutes"]):
        check_prices(cfg, db, notifier)
        mark(db, "prices")
        if cfg["movers"].get("enabled") and due(db, "movers", cfg["movers"].get("every_minutes", 60)):
            scan_movers(cfg, db, notifier)
            mark(db, "movers")
        check_spread(cfg, db, notifier)
    check_targets_and_positions(cfg, db, notifier)
    cleanup(cfg, db)


def run_once(cfg, db, notifier):
    run_cycle(cfg, db, notifier)


def run_loop(cfg, db, notifier):
    while True:
        try:
            run_cycle(cfg, db, notifier)
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
    assert "nsfw_craft" not in cat("nsfw golden clean paris combo deal")  # звичайні слова більше не створюють контекст крафту
    assert "nsfw_craft" in cat("нсфв стикеры и наклейками на AK")  # закінчення (зірочка) працюють
    assert "budget_look" in cat("Budget Howl craft: M4A4 looks like a Howl for $3", "stickers 4x holo")
    assert "budget_look" in cat("Бюджетный хоул: M4A4 выглядит как Howl за 3$", "крафт наклейками")
    assert "wear_upgrade" in cat("Battle-Scarred to Factory New with stickers, scrape craft")
    assert "wear_upgrade" in cat("Скрейп крафт: BS -> FN на наклейках")
    assert "chinese_pump" in cat("Chinese are pumping this skin on buff163", score=50)
    assert "chinese_pump" not in cat("Chinese are pumping this skin", score=1)  # поріг скору
    assert "chinese_pump" in cat("Китайцы качают этот скин, памп", score=20)
    assert "chinese_pump" not in cat("Selling AK-47 Redline, buff163 price is 300 for FT", score=50)  # саме слово buff не тригерить
    assert "chinese_pump" not in cat("Chinese players love this skin", score=50)
    assert "chinese_pump" in cat("Chinese buyers bought out all listings on this skin", score=20)
    assert "chinese_pump" in cat("Buff163 buyers are pumping this sticker", score=20)
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
    page_dt = ('<div class="tgme_widget_message_wrap" data-post="chan/13"><div class="tgme_widget_message_text">Крафт</div>'
               '<a class="tgme_widget_message_date"><time datetime="2020-01-02T10:00:00+00:00" class="time">10:00</time></a></div>')
    p_dt = list(parse_telegram_html(page_dt))[0]
    assert p_dt["ts"] == 1577959200.0 and too_old(p_dt, 72), p_dt  # дата читається, старий пост відсіюється

    # --- Reddit: запасний Atom-шлях
    atom = (b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><id>t3_abc123</id><title>Craft M4A4</title>'
            b'<link href="https://www.reddit.com/r/x/comments/abc123/"/><updated>2026-10-28T10:00:00+00:00</updated>'
            b'<content type="html">&lt;p&gt;4x holo&lt;/p&gt;</content></entry></feed>')
    a = list(parse_reddit_atom(atom))[0]
    assert a["id"] == "reddit:abc123" and a["text"] == "4x holo" and a["ts"] and a["url"].endswith("/abc123/"), a

    # --- гривня: символ і розбір цін Steam без будь-яких курсів
    assert CUR == "₴" and mny(12.5) == "12.50 ₴" and cfg["currency"] == 18
    assert parse_price("₴ 1 234,56") == 1234.56 and parse_price("1 200,00₴") == 1200.0 and parse_price("₴ 85") == 85.0

    class FakeResp:
        status_code = 200

        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

        def raise_for_status(self):
            pass

    real_get = requests.get
    seen_params = {}

    def fake_get(url, params=None, **kw):
        seen_params.update(params or {})
        if "priceoverview" in url:
            return FakeResp({"success": True, "lowest_price": "₴ 1 234,56", "median_price": "1 200,00₴", "volume": "1,234"})
        return FakeResp({"results": [{"hash_name": "Case A", "sell_price_text": "₴ 150,25", "sell_price": 99999, "sell_listings": 500}]})

    requests.get = fake_get
    try:
        assert fetch_steam_price("Item X", 18) == (1234.56, 1234) and LAST_MEDIAN.pop("Item X") == 1200.0
        assert seen_params["currency"] == 18  # запитуємо Steam саме в гривнях
        assert fetch_market_popular(18, pages=1, delay=0) == [{"name": "Case A", "price": 150.25, "listings": 500}]
    finally:
        requests.get = real_get

    # --- захист від 429: пауза, подвоєння, round-robin по бюджету запитів
    dbq = init_db(":memory:")
    nq = Notifier(dbq)
    nq.token = nq.chat = None
    cfgq = dict(cfg, max_price_requests_per_run=2)
    for i in range(5):
        dbq.execute("INSERT INTO watch VALUES(?,?)", (f"Item {i}", t))
    calls = []

    def ok_fetch(name, cur):
        calls.append(name)
        return 100.0 + len(calls), 10

    for _ in range(3):
        check_prices(cfgq, dbq, nq, fetch=ok_fetch, delay=0)
    assert calls == ["Item 0", "Item 1", "Item 2", "Item 3", "Item 4", "Item 0"], calls  # по 2 за прохід, по колу

    def limited2(name, cur):
        calls.append("429:" + name)
        raise RuntimeError("Steam rate limit (429)")

    calls.clear()
    check_prices(cfgq, dbq, nq, fetch=limited2, delay=0)
    assert calls == ["429:Item 1"] and steam_blocked_for(dbq) > 0  # перша ж 429 зупиняє прохід і ставить паузу
    calls.clear()
    check_prices(cfgq, dbq, nq, fetch=ok_fetch, delay=0)
    assert calls == []  # під час паузи Steam не чіпаємо
    assert "Steam обмежує" in handle_command(dbq, cfgq, "/status")
    first_pause = steam_blocked_for(dbq)
    meta_set(dbq, "steam_block_until", now_ts() - 1)
    check_prices(cfgq, dbq, nq, fetch=limited2, delay=0)
    assert steam_blocked_for(dbq) > first_pause * 1.5  # друга 429 поспіль: пауза довша
    meta_set(dbq, "steam_block_until", now_ts() - 1)
    calls.clear()
    check_prices(cfgq, dbq, nq, fetch=ok_fetch, delay=0)
    assert calls and steam_blocked_for(dbq) == 0 and meta_get(dbq, "steam_429_streak") == "0"  # успіх скинув лічильник
    # median зберігається окремо від ціни
    LAST_MEDIAN["Item 2"] = 95.5
    check_prices(dict(cfgq, max_price_requests_per_run=5), dbq, nq, fetch=lambda n_, c: (110.0, 5), delay=0)
    assert dbq.execute("SELECT median FROM sales WHERE name='Item 2'").fetchone()[0] == 95.5

    # --- SL/TP спрацьовують один раз, а не кожні 6 годин; після відновлення можуть спрацювати знову
    dbs = init_db(":memory:")
    ns = Notifier(dbs, cooldown_hours=0)
    ns.token = ns.chat = None
    dbs.execute("INSERT INTO positions(name, price, qty, sl, tp, ts) VALUES('SL Item', 100, 1, 10, 50, ?)", (t,))

    def set_price(v):
        dbs.execute("INSERT INTO prices VALUES('SL Item',?,?,5)", (now_ts(), v))

    set_price(80 * 1.15)   # PnL -20%
    for _ in range(3):
        check_targets_and_positions(cfg, dbs, ns)
    assert sum("STOP-LOSS" in s for s in ns.sent) == 1, ns.sent
    set_price(100 * 1.15)  # PnL 0%: відновилось
    check_targets_and_positions(cfg, dbs, ns)
    set_price(80 * 1.15)
    check_targets_and_positions(cfg, dbs, ns)
    assert sum("STOP-LOSS" in s for s in ns.sent) == 2
    set_price(160 * 1.15)  # PnL +60%
    for _ in range(3):
        check_targets_and_positions(cfg, dbs, ns)
    assert sum("TAKE-PROFIT" in s for s in ns.sent) == 1

    # --- нове джерело читається мовчки; старі джерела (legacy) продовжують слати відразу
    dbn = init_db(":memory:")
    nn = Notifier(dbn)
    nn.token = nn.chat = None
    nn_posts = [{"id": "tg:chan/1", "title": "Craft M4A4 with 4x holo, looks like Howl", "text": "", "url": "u", "score": 0,
                 "ts": now_ts() - 3600, "src": "telegram:chan"}]
    process_news(cfg, dbn, nn, collect=lambda c: nn_posts)
    assert nn.sent == []  # перше читання джерела: тільки запам'ятали
    nn_posts.append({"id": "tg:chan/2", "title": "New craft: 4x holo budget Howl craft", "text": "", "url": "u", "score": 0,
                     "ts": now_ts() - 60, "src": "telegram:chan"})
    process_news(cfg, dbn, nn, collect=lambda c: nn_posts)
    assert len(nn.sent) == 1 and "New craft" in nn.sent[0], nn.sent
    meta_set(dbn, "baseline_done", 1)  # як у вже працюючому боті
    rss_post = [{"id": "rss:9", "title": "Update: new case", "text": "", "url": "u", "score": 0, "ts": now_ts() - 60, "src": "rss:feed"}]
    before = len(nn.sent)
    process_news(cfg, dbn, nn, collect=lambda c: rss_post)
    assert len(nn.sent) == before + 1  # існуюче джерело після міграції не мовчить
    # згадки наклейок з нового джерела не створюють хибний «тренд»: дата береться з поста
    old_mention = {"id": "tg:old/1", "title": "Sticker | Titan (Holo) | Katowice 2014 craft", "text": "", "url": "u", "score": 0,
                   "ts": now_ts() - 2 * 86400, "src": "telegram:old"}
    process_posts(cfg, dbn, nn, [old_mention])
    assert dbn.execute("SELECT ts FROM mentions WHERE post_id='tg:old/1'").fetchone()[0] == int(old_mention["ts"])

    # --- зміна валюти: стара історія цін очищається, а не змішується з гривнею
    dbm = init_db(":memory:")
    nm = Notifier(dbm)
    nm.token = nm.chat = None
    dbm.execute("INSERT INTO prices VALUES('EUR Item', ?, 10.0, 5)", (t,))
    dbm.execute("INSERT INTO scan VALUES('EUR Item', ?, 10.0, 5)", (t,))
    dbm.execute("INSERT INTO positions(name, price, qty, ts) VALUES('EUR Item', 9.0, 1, ?)", (t,))
    ensure_currency(cfg, dbm, nm)  # валюта в базі невідома -> стара історія очищається
    assert dbm.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 0 and meta_get(dbm, "currency") == "18"
    assert any("гривнях" in s for s in nm.sent), nm.sent
    dbm.execute("INSERT INTO prices VALUES('UAH Item', ?, 500.0, 5)", (t,))
    ensure_currency(cfg, dbm, nm)  # та сама валюта -> нічого не чіпаємо
    assert dbm.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 1
    ensure_currency(dict(cfg, currency=3), dbm, nm)  # змінили валюту -> знову очищення
    assert dbm.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 0

    # --- прибирання бази раз на добу
    dbc = init_db(":memory:")
    dbc.execute("INSERT INTO prices VALUES('Old', ?, 1.0, 1)", (t - 40 * 86400,))
    dbc.execute("INSERT INTO prices VALUES('New', ?, 1.0, 1)", (t - 86400,))
    dbc.execute("INSERT INTO scan VALUES('Old', ?, 1.0, 1)", (t - 10 * 86400,))
    cleanup(cfg, dbc)
    assert [r[0] for r in dbc.execute("SELECT name FROM prices")] == ["New"] and dbc.execute("SELECT COUNT(*) FROM scan").fetchone()[0] == 0
    dbc.execute("INSERT INTO prices VALUES('Old2', ?, 1.0, 1)", (t - 40 * 86400,))
    cleanup(cfg, dbc)  # вдруге в ту саму добу не запускається
    assert dbc.execute("SELECT COUNT(*) FROM prices WHERE name='Old2'").fetchone()[0] == 1
    assert due(dbc, "x", 30) and (mark(dbc, "x") or not due(dbc, "x", 30))

    # --- команди
    fake = lambda name, cur: (12.0, 100) if "Redline" in name else (None, None)
    assert "Стежу" in handle_command(db, cfg, "/add AK-47 | Redline (Field-Tested)", fake)
    assert "Не знайшов" in handle_command(db, cfg, "/add Nonexistent Thing", fake)
    def limited(name, cur):
        raise RuntimeError("Steam rate limit (429)")
    assert "без перевірки" in handle_command(db, cfg, "/add ★ StatTrak™ Nomad Knife | Damascus Steel (Field-Tested)", limited)
    assert "★ StatTrak™ Nomad Knife | Damascus Steel (Field-Tested)" in watch_names(db)
    assert steam_blocked_for(db) > 0  # 429 під час /add поставила паузу
    assert "пауза" in handle_command(db, cfg, "/add AK-47 | AWP Test", fake)  # у паузі Steam не чіпаємо
    meta_set(db, "steam_block_until", 0)
    meta_set(db, "steam_429_streak", 0)
    # хибна назва: після 3 промахів приходить попередження
    db.execute("INSERT OR IGNORE INTO watch VALUES(?,?)", ("Wrong Name", t))
    before = len(n.sent)
    for _ in range(3):
        check_prices(cfg, db, n, fetch=lambda name, cur: (None, None) if name == "Wrong Name" else (None, None), delay=0)
    assert any("Wrong Name" in s and "❓" in s for s in n.sent[before:])
    db.execute("DELETE FROM watch WHERE name IN (?, ?)", ("Wrong Name", "★ StatTrak™ Nomad Knife | Damascus Steel (Field-Tested)"))
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
    # (ціни в гривнях; поріг min_price у config = 40 ₴)
    for name, p0, l0 in (("PumpItem", 100.0, 100), ("DumpItem", 100.0, 100), ("SqueezeItem", 100.0, 100), ("Calm", 100.0, 100)):
        db.execute("INSERT INTO scan VALUES(?,?,?,?)", (name, t - 20 * 3600, p0, l0))
    fetch_scan = lambda cur, pages: [
        {"name": "PumpItem", "price": 120.0, "listings": 100}, {"name": "DumpItem", "price": 80.0, "listings": 150},
        {"name": "SqueezeItem", "price": 105.0, "listings": 60}, {"name": "Calm", "price": 101.0, "listings": 100}]
    before = len(n.sent)
    scan_movers(cfg, db, n, fetch_scan)
    new = " ".join(n.sent[before:])
    assert "PUMP PumpItem" in new and "DUMP DumpItem" in new and "СКУПКА ПРОПОЗИЦІЇ SqueezeItem" in new and "Calm" not in new, new
    assert "₴" in new
    tm = top_movers_text(db)
    assert "PumpItem" in tm and "Ріст" in tm
    # страховка від різних валют у сканері та списку відстеження
    db.execute("INSERT INTO prices VALUES(?,?,?,?)", ("Mix Item", t, 500.0, 5))
    before = len(n.sent)
    scan_movers(cfg, db, n, lambda cur, pages: [{"name": "Mix Item", "price": 12.0, "listings": 50}])
    assert any("розходяться" in s for s in n.sent[before:]), n.sent[before:]
    assert check_scan_currency(db, n, [{"name": "Mix Item", "price": 520.0}]) is True

    # --- спред Skinport: для гривні вимкнений (Skinport не віддає UAH), для спільної валюти працює
    db.execute("INSERT INTO prices VALUES(?,?,?,?)", ("Spread Item", t, 23.0, 10))
    db.execute("INSERT INTO prices VALUES(?,?,?,?)", ("Premium Item", t, 20.0, 10))
    db.execute("INSERT OR IGNORE INTO watch VALUES(?,?)", ("Spread Item", t))
    db.execute("INSERT OR IGNORE INTO watch VALUES(?,?)", ("Premium Item", t))
    sp_data = {"Spread Item": (15.0, 5), "Premium Item": (19.8, 3)}  # 23/1.15=20 => +33%; 19.8/20 >= 0.97
    cfg_uah_sp = dict(cfg, spread=dict(cfg["spread"], enabled=True))  # UAH + EUR: не змішуємо валюти
    before = len(n.sent)
    check_spread(cfg_uah_sp, db, n, lambda c: sp_data)
    assert len(n.sent) == before, "спред у змішаних валютах не повинен працювати"
    cfg_eur = dict(cfg, currency=3, skinport_currency="EUR", spread=dict(cfg["spread"], enabled=True, min_price=2.0))
    check_spread(cfg_eur, db, n, lambda c: sp_data)
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
