#!/usr/bin/env python3
"""Помічник з інвестицій для CS2 Radar. Без зовнішніх сервісів, ключів і Skinport.

Команди Telegram (підключаються з radar.py через advisor.handle):
  /analyze [назва]  розбір предмета; без назви: зведення по всьому списку відстеження
  /review           огляд портфеля: PnL після комісії, близькість до SL/TP, концентрація, беззбитковість
  /report           текстове зведення, яке зручно скопіювати в чат із Claude і поставити питання

Усе рахується з даних, які бот уже збирає в radar.db (таблиці prices, scan, positions, targets, mentions, alerts).
Це підсумок минулих цін, а не прогноз і не фінансова порада.

Перевірка: python advisor.py selftest
"""
import re
import sys
import time

DISCLAIMER = "Це підсумок минулих даних, а не прогноз чи фінансова порада."
CURRENCIES = {1: "USD", 2: "GBP", 3: "EUR", 5: "RUB", 18: "UAH"}


# --------------------------------------------------------------------------- helpers
def now_ts() -> int:
    return int(time.time())


def _mean(x):
    return sum(x) / len(x)


def _sma(vals, n):
    return _mean(vals[-n:]) if len(vals) >= n else None


def _rsi(vals, n):
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


def _z(vals, n):
    w = vals[-n:]
    if len(w) < n:
        return None, None
    hist = w[:-1]
    m = _mean(hist)
    sd = (sum((x - m) ** 2 for x in hist) / len(hist)) ** 0.5
    if sd == 0 or m == 0:
        return None, None
    return (w[-1] - m) / sd, (w[-1] - m) / m * 100


def _pct(x):
    return "н/д" if x is None else f"{x:+.1f}%"


def _m(cfg, x):
    """Сума з символом валюти з config.currency_symbol (напр. 123.45 ₴)."""
    return f"{x:.2f} {cfg.get('currency_symbol', '')}".rstrip()


def _median(db, name, max_age=6 * 3600):
    """Остання медіана реальних угод зі Steam (таблиця sales), якщо свіжа."""
    r = db.execute("SELECT median FROM sales WHERE name=? AND ts>? ORDER BY ts DESC LIMIT 1", (name, now_ts() - max_age)).fetchone()
    return r[0] if r else None


def _clip(text: str, limit: int = 3900) -> str:
    return text if len(text) <= limit else text[: limit - 20].rstrip() + "\n…(скорочено)"


def item_kind(name: str) -> str:
    n = name.strip()
    low = n.lower()
    if low.startswith("sticker |"):
        return "наклейка"
    if n.startswith("★"):
        return "ніж/рукавиці"
    if low.endswith(" case") or "capsule" in low or "package" in low or low.endswith(" key"):
        return "кейс/капсула/ключ"
    if "|" in n:
        return "скін"
    return "інше"


def _prices(db, name, limit=400):
    rows = db.execute(
        "SELECT ts, price, volume FROM prices WHERE name=? AND price IS NOT NULL ORDER BY ts DESC LIMIT ?", (name, limit)
    ).fetchall()
    return rows[::-1]


def _last_price(db, name):
    r = db.execute("SELECT price FROM prices WHERE name=? ORDER BY ts DESC LIMIT 1", (name,)).fetchone()
    return r[0] if r else None


def known_names(db):
    names = {r[0] for r in db.execute("SELECT name FROM watch")}
    names |= {r[0] for r in db.execute("SELECT DISTINCT name FROM positions")}
    names |= {r[0] for r in db.execute("SELECT DISTINCT name FROM prices")}
    return sorted(names)


def resolve_name(db, text):
    """Точний збіг, інакше пошук підрядка без урахування регістру. -> список збігів."""
    names = known_names(db)
    if text in names:
        return [text]
    low = text.lower()
    exact = [n for n in names if n.lower() == low]
    if exact:
        return exact
    return [n for n in names if low in n.lower()]


# --------------------------------------------------------------------------- analysis of one item
def analyze_item(db, cfg, name):
    ind, al, mv = cfg["indicators"], cfg["alerts"], cfg.get("movers", {})
    rows = _prices(db, name)
    d = {"name": name, "kind": item_kind(name), "n": len(rows), "score": 0, "reasons": [], "enough": False,
         "price": None, "c24": None, "c7": None, "rsi": None, "trend": None, "sma_s": None, "sma_l": None,
         "z": None, "zmove": None, "vol_now": None, "vol_avg": None, "range7": None, "supply": None, "median": None}
    if not rows:
        return d
    ts_now, p_now, v_now = rows[-1]
    vals = [r[1] for r in rows]
    d["price"] = p_now
    d["enough"] = len(vals) >= ind["min_points"]
    med = _median(db, name)
    d["median"] = med
    if med and p_now < med * 0.85:
        d["reasons"].append(f"⚠️ Найдешевша пропозиція на {(1 - p_now / med) * 100:.0f}% нижча за медіану реальних угод: можливий демпінг або тонкий ринок")
    elif med and p_now > med * 1.15:
        d["reasons"].append(f"⚠️ Найдешевша пропозиція на {(p_now / med - 1) * 100:.0f}% вища за медіану реальних угод: ціна може бути завищена")

    def change(sec):
        base = next((r for r in rows if r[0] >= ts_now - sec), None)
        # історія має реально покривати період, інакше "24г" було б зміною за кілька годин
        if base and base[0] <= ts_now - sec * 0.75 and base[1]:
            return (p_now - base[1]) / base[1] * 100
        return None

    d["c24"], d["c7"] = change(86400), change(7 * 86400)
    s, l = _sma(vals, ind["sma_short"]), _sma(vals, ind["sma_long"])
    d["sma_s"], d["sma_l"] = s, l
    if s is not None and l is not None:
        d["trend"] = "up" if s > l else "down"
        d["score"] += 1 if s > l else -1
        d["reasons"].append(("🟢 Короткий тренд вище довгого (SMA%d > SMA%d)" if s > l else "🔴 Короткий тренд нижче довгого (SMA%d < SMA%d)")
                            % (ind["sma_short"], ind["sma_long"]))
    thr = al["price_change_pct"]
    if d["c24"] is not None and abs(d["c24"]) >= thr:
        d["score"] += 1 if d["c24"] > 0 else -1
        d["reasons"].append(f"{'🟢' if d['c24'] > 0 else '🔴'} За добу {d['c24']:+.1f}% (поріг {thr}%)")
    r = _rsi(vals, ind["rsi_period"])
    d["rsi"] = r
    if r is not None:
        if r >= ind["rsi_overbought"]:
            d["score"] -= 1
            d["reasons"].append(f"🌡 RSI {r:.0f}: ринок перегрітий, можливий відкат")
        elif r <= ind["rsi_oversold"]:
            d["score"] += 1
            d["reasons"].append(f"🧊 RSI {r:.0f}: перепроданість, можливий відскок")
    z, zmove = _z(vals, ind["z_window"])
    d["z"], d["zmove"] = z, zmove
    if z is not None and abs(z) >= ind["z_threshold"] and abs(zmove) >= ind["z_min_move_pct"]:
        d["reasons"].append(f"⚡ Аномальний рух ({zmove:+.1f}% від середнього, z={z:.1f}), напрям неоднозначний")
    week = [r_[2] for r_ in rows if r_[2] is not None and r_[0] >= ts_now - 7 * 86400][:-1]
    d["vol_now"] = v_now
    if week:
        d["vol_avg"] = _mean(week)
        if v_now and d["vol_avg"] > 0 and v_now >= d["vol_avg"] * al["volume_spike_x"]:
            d["reasons"].append(f"🔥 Обсяг {v_now} у {v_now / d['vol_avg']:.1f}× вище середнього: хтось активно торгує")
    wk = [r_[1] for r_ in rows if r_[0] >= ts_now - 7 * 86400]
    if len(wk) >= 3:
        lo, hi = min(wk), max(wk)
        d["range7"] = (lo, hi, (hi - lo) / _mean(wk) * 100)
    sc = db.execute("SELECT price, listings FROM scan WHERE name=? AND ts>=? ORDER BY ts", (name, now_ts() - 86400)).fetchall()
    if len(sc) >= 2 and sc[0][0] and sc[0][1]:
        lst = (sc[-1][1] - sc[0][1]) / sc[0][1] * 100
        pr = (sc[-1][0] - sc[0][0]) / sc[0][0] * 100
        d["supply"] = (sc[0][1], sc[-1][1], lst)
        sq = mv.get("squeeze_pct", 25)
        if lst <= -sq and pr >= 3:
            d["score"] += 1
            d["reasons"].append(f"🧲 Лістинги {sc[0][1]}→{sc[-1][1]} ({lst:.0f}%) при зростанні ціни: пропозицію скуповують")
        elif lst >= sq and pr <= -3:
            d["score"] -= 1
            d["reasons"].append(f"🌊 Лістингів побільшало ({lst:+.0f}%), ціна падає: тиск продавців")
    return d


def verdict(d):
    if not d["price"]:
        return "❔", "немає цінових даних"
    if not d["enough"]:
        return "❔", "даних замало, висновок ненадійний"
    if d["score"] >= 2:
        return "🟢", "скоріше бичачі сигнали"
    if d["score"] <= -2:
        return "🔴", "скоріше ведмежі сигнали"
    return "⚪", "сигнали змішані / нейтрально"


def format_item(db, cfg, d):
    fee = cfg.get("steam_fee", 1.15)
    ind = cfg["indicators"]
    name = d["name"]
    if not d["price"]:
        return (f"🔎 {name}\nУ базі ще немає ціни. Бот збирає знімки при кожному проході; "
                "якщо Steam віддає 429, ціна може з'явитися із запізненням.")
    em, word = verdict(d)
    L = [f"🔎 {name} [{d['kind']}]",
         f"Ціна: {_m(cfg, d['price'])} (знімків: {d['n']}, для висновків потрібно ≥{ind['min_points']})",
         f"Зміна: 24г {_pct(d['c24'])}, 7д {_pct(d['c7'])}"]
    if d["median"]:
        L.insert(2, f"Медіана реальних угод (24г): {_m(cfg, d['median'])}; найдешевша пропозиція зараз: {_m(cfg, d['price'])}")
    if d["sma_s"] is not None and d["sma_l"] is not None:
        L.append(f"Тренд: SMA{ind['sma_short']} {_m(cfg, d['sma_s'])} / SMA{ind['sma_long']} {_m(cfg, d['sma_l'])} ({'вгору' if d['trend'] == 'up' else 'вниз'})")
    if d["rsi"] is not None:
        L.append(f"RSI: {d['rsi']:.0f}")
    if d["vol_now"] is not None:
        L.append(f"Обсяг: {d['vol_now']}" + (f" (середній {d['vol_avg']:.0f})" if d["vol_avg"] else ""))
    if d["supply"]:
        L.append(f"Лістинги за добу: {d['supply'][0]} → {d['supply'][1]} ({d['supply'][2]:+.0f}%)")
    if d["range7"]:
        lo, hi, w = d["range7"]
        L.append(f"Діапазон 7д: {_m(cfg, lo)}–{_m(cfg, hi)} (розмах ≈{w:.0f}% від середньої)")
    L.append(f"Комісія Steam: щоб вийти в плюс, продати треба принаймні на {(fee - 1) * 100:.0f}% дорожче, ніж купили")
    for cost, qty, sl, tp in db.execute("SELECT price, qty, sl, tp FROM positions WHERE name=?", (name,)):
        pnl = (d["price"] / fee / cost - 1) * 100 if cost else 0
        L.append(f"Ваша позиція: вхід {_m(cfg, cost)} x{qty}, чистий PnL {pnl:+.1f}%"
                 + (f", SL {sl:g}%" if sl else ", без stop-loss") + (f", TP {tp:g}%" if tp else ""))
    for price, direction in db.execute("SELECT price, direction FROM targets WHERE name=? AND active=1", (name,)):
        L.append(f"Ціль: {'<' if direction == 'below' else '>'} {_m(cfg, price)}")
    L.append(f"\nВисновок: {em} {word}")
    L += d["reasons"] or ["Помітних сигналів немає"]
    L.append(DISCLAIMER)
    return "\n".join(L)


def analyze_cmd(db, cfg, rest):
    if not rest:
        names = [r[0] for r in db.execute("SELECT name FROM watch ORDER BY name")]
        if not names:
            return "Список відстеження порожній. Додайте: /add <назва>"
        items = sorted((analyze_item(db, cfg, n) for n in names), key=lambda d: (-d["score"], d["name"]))
        lines = ["📋 Зведення по списку (від найбичачіших):"]
        for d in items[:20]:
            em, _ = verdict(d)
            lines.append(f"{em} {d['name']}: " + (f"{_m(cfg, d['price'])} | 24г {_pct(d['c24'])}" + (f" | RSI {d['rsi']:.0f}" if d["rsi"] is not None else "")
                                                  if d["price"] else "ще немає ціни"))
        lines.append("\nДеталі по предмету: /analyze <частина назви>")
        return _clip("\n".join(lines))
    found = resolve_name(db, rest)
    if not found:
        return "Не знаю такого предмета. Додайте його: /add <точна назва зі Steam Market>"
    if len(found) > 1:
        return "Знайшов кілька, уточніть:\n" + "\n".join("• " + n for n in found[:8])
    return _clip(format_item(db, cfg, analyze_item(db, cfg, found[0])))


# --------------------------------------------------------------------------- portfolio review
def review_cmd(db, cfg):
    fee = cfg.get("steam_fee", 1.15)
    rows = db.execute("SELECT name, price, qty, sl, tp FROM positions ORDER BY name").fetchall()
    if not rows:
        return "Позицій немає. Додайте: /buy <назва> @ <ціна> x<к-сть> sl<%> tp<%>"
    lines, by_name, by_kind = ["🧾 Огляд портфеля"], {}, {}
    cost_sum = net_sum = 0.0
    for name, cost, qty, sl, tp in rows:
        p = _last_price(db, name)
        if p is None or not cost:
            lines.append(f"• {name} x{qty}: ще немає ціни")
            continue
        net = p / fee
        pnl = (net / cost - 1) * 100
        val = net * qty
        cost_sum += cost * qty
        net_sum += val
        by_name[name] = by_name.get(name, 0) + val
        kind = item_kind(name)
        by_kind[kind] = by_kind.get(kind, 0) + val
        flags = []
        if not sl:
            flags.append("⚠️ без stop-loss")
        elif pnl <= -sl:
            flags.append("🛑 SL досягнуто")
        elif pnl + sl <= 5:
            flags.append(f"близько до SL (запас {pnl + sl:.1f} п.п.)")
        if tp and pnl >= tp:
            flags.append("💰 TP досягнуто")
        elif tp and tp - pnl <= 5:
            flags.append(f"близько до TP ({tp - pnl:.1f} п.п.)")
        if pnl < 0:
            flags.append(f"до беззбитковості треба ще {(cost * fee / p - 1) * 100:+.0f}%")
        em, _ = verdict(analyze_item(db, cfg, name))
        lines.append(f"{em} {name} x{qty}: вхід {_m(cfg, cost)}, зараз {_m(cfg, p)}, чистий PnL {pnl:+.1f}%" + ("\n    " + "; ".join(flags) if flags else ""))
    if net_sum:
        lines.append(f"\nРазом: вкладено {_m(cfg, cost_sum)}, чиста вартість {_m(cfg, net_sum)} ({(net_sum / cost_sum - 1) * 100:+.1f}%)")
        if len(by_name) >= 2:
            top_n = max(by_name.items(), key=lambda x: x[1])
            if top_n[1] / net_sum > 0.5:
                lines.append(f"⚠️ Концентрація: «{top_n[0]}» це {top_n[1] / net_sum * 100:.0f}% портфеля")
            kinds = ", ".join(f"{k} {v / net_sum * 100:.0f}%" for k, v in sorted(by_kind.items(), key=lambda x: -x[1]))
            lines.append(f"Структура: {kinds}")
        else:
            lines.append("Усе в одній позиції: ризик повністю зосереджений у ній.")
    lines.append("Емодзі біля назви: підсумок сигналів із /analyze.")
    lines.append(DISCLAIMER)
    return _clip("\n".join(lines))


# --------------------------------------------------------------------------- report for pasting into a Claude chat
def _top_movers(db, n=4):
    t = now_ts()
    rows = []
    for (name,) in db.execute("SELECT DISTINCT name FROM scan WHERE ts>=?", (t - 7200,)):
        new = db.execute("SELECT price FROM scan WHERE name=? ORDER BY ts DESC LIMIT 1", (name,)).fetchone()
        old = db.execute("SELECT price FROM scan WHERE name=? AND ts>=? ORDER BY ts LIMIT 1", (name, t - 86400)).fetchone()
        if new and old and old[0] and new[0] != old[0]:
            rows.append(((new[0] - old[0]) / old[0] * 100, name))
    rows.sort()
    return [r for r in rows[::-1] if r[0] > 0][:n], [r for r in rows if r[0] < 0][:n]


def report_cmd(db, cfg):
    fee = cfg.get("steam_fee", 1.15)
    cur = CURRENCIES.get(cfg.get("currency"), f"id {cfg.get('currency')}") + " (ціни Steam без перерахунку за курсом)"
    t = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    parts = [f"=== CS2 RADAR REPORT {t} ===",
             f"Валюта Steam: {cur}. Комісія Steam: продавець отримує ціна/{fee}. Дані зібрав мій бот зі Steam Market, Reddit, Telegram."]
    names = [r[0] for r in db.execute("SELECT name FROM watch ORDER BY name")]
    w = ["\nСписок відстеження:"]
    for n in names[:12]:
        d = analyze_item(db, cfg, n)
        if d["price"]:
            w.append(f"- {n} | {_m(cfg, d['price'])}" + (f" (медіана угод {_m(cfg, d['median'])})" if d["median"] else "")
                     + f" | 24г {_pct(d['c24'])} | 7д {_pct(d['c7'])}"
                     + (f" | RSI {d['rsi']:.0f}" if d["rsi"] is not None else "")
                     + (f" | тренд {'вгору' if d['trend'] == 'up' else 'вниз'}" if d["trend"] else "")
                     + f" | знімків {d['n']}")
        else:
            w.append(f"- {n} | ціни ще немає")
    if len(names) > 12:
        w.append(f"- ...ще {len(names) - 12}")
    parts += w if names else ["\nСписок відстеження порожній."]
    pos = db.execute("SELECT name, price, qty, sl, tp FROM positions ORDER BY name").fetchall()
    if pos:
        p_ = ["\nПозиції:"]
        for name, cost, qty, sl, tp in pos[:10]:
            p = _last_price(db, name)
            pnl = f"{(p / fee / cost - 1) * 100:+.1f}%" if p and cost else "н/д"
            p_.append(f"- {name} x{qty} | вхід {_m(cfg, cost)} | зараз {_m(cfg, p)}" if p else f"- {name} x{qty} | вхід {_m(cfg, cost)} | зараз н/д")
            p_[-1] += f" | чистий PnL {pnl}" + (f" | SL {sl:g}%" if sl else " | без SL") + (f" | TP {tp:g}%" if tp else "")
        parts += p_
    tg = db.execute("SELECT name, price, direction FROM targets WHERE active=1").fetchall()
    if tg:
        parts += ["\nАктивні цілі:"] + [f"- {n} {'<' if dr == 'below' else '>'} {_m(cfg, p)}" for n, p, dr in tg[:8]]
    up, down = _top_movers(db)
    if up or down:
        parts.append("\nСканер популярних предметів Steam за 24г:")
        parts += [f"- ріст {p:+.1f}% {n}" for p, n in up] + [f"- падіння {p:+.1f}% {n}" for p, n in down]
    t0 = now_ts()
    st = db.execute("SELECT sticker, COUNT(DISTINCT post_id) c FROM mentions WHERE ts>? GROUP BY sticker ORDER BY c DESC LIMIT 4", (t0 - 86400,)).fetchall()
    if st:
        parts += ["\nНаклейки з найбільшою кількістю згадок за 24г:"] + [f"- {s}: {c}" for s, c in st]
    prefixes = ("price:", "pump:", "dump:", "squeeze:", "vol:", "sma:", "rsi:", "z:", "trend:", "sl:", "tp:", "target:")
    al = [r for r in db.execute("SELECT key, ts FROM alerts WHERE ts>? ORDER BY ts DESC LIMIT 60", (t0 - 86400,)) if r[0].startswith(prefixes)][:10]
    if al:
        parts += ["\nОстанні сигнали бота (24г):"] + [f"- {k} ({(t0 - ts) // 3600} г тому)" for k, ts in al]
    body = _clip("\n".join(parts), 3650)
    return body + "\n\n=== Питання до аналітика: [впишіть своє] ===\nПопередження: це дані мого бота, можуть бути неповними. Дай обережний аналіз ризиків."


# --------------------------------------------------------------------------- entry point for radar.py
HELP_LINES = (
    "/analyze [назва] — розбір предмета (без назви: зведення по списку)\n"
    "/review — огляд портфеля і ризиків\n"
    "/report — зведення для копіювання в чат із Claude\n"
)


def handle(db, cfg, cmd, rest):
    """Повертає текст відповіді або None, якщо команда не з цього модуля."""
    if cmd == "/analyze":
        return analyze_cmd(db, cfg, rest)
    if cmd == "/review":
        return review_cmd(db, cfg)
    if cmd == "/report":
        return report_cmd(db, cfg)
    return None


# --------------------------------------------------------------------------- selftest
def selftest():
    import os

    import radar  # init_db і load_config із основного файлу

    cfg = radar.load_config(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"))
    db = radar.init_db(":memory:")
    t = now_ts()

    def seed(name, vals, step=3600, vol=50):
        for i, v in enumerate(vals):
            db.execute("INSERT INTO prices VALUES(?,?,?,?)", (name, t - (len(vals) - 1 - i) * step, v, vol))
        db.execute("INSERT OR IGNORE INTO watch VALUES(?,?)", (name, t))

    bull, bear, p = [], [], 10.0
    q = 20.0
    for i in range(30):
        p += 0.3 if i % 2 == 0 else -0.15
        q += -0.3 if i % 2 == 0 else 0.15
        bull.append(round(p, 2))
        bear.append(round(q, 2))
    seed("Bull Item (Field-Tested)", bull)
    seed("Bear Item (Field-Tested)", bear)
    seed("Flat Item", [5.0, 5.1, 5.0, 5.2, 5.1])
    seed("Sticker | Titan (Holo) | Katowice 2014", [100, 101, 99, 100, 102])
    seed("★ Knife | Test", [300] * 5)

    b = analyze_item(db, cfg, "Bull Item (Field-Tested)")
    assert b["score"] >= 2 and verdict(b)[0] == "🟢", (b["score"], b["reasons"])
    assert b["c24"] is not None and b["c7"] is None  # історія ~29 год: тиждень не рахуємо
    r = analyze_item(db, cfg, "Bear Item (Field-Tested)")
    assert r["score"] <= -2 and verdict(r)[0] == "🔴", (r["score"], r["reasons"])
    f = analyze_item(db, cfg, "Flat Item")
    assert verdict(f)[0] == "❔" and not f["enough"]
    assert item_kind("Revolution Case") == "кейс/капсула/ключ" and item_kind("★ Knife | Test") == "ніж/рукавиці"
    assert item_kind("Sticker | X") == "наклейка" and item_kind("AK-47 | Redline (Field-Tested)") == "скін"

    # гривня і медіана реальних угод
    assert cfg["currency"] == 18 and "₴" in handle(db, cfg, "/analyze", "bull")
    assert _median(db, "Bull Item (Field-Tested)") is None
    db.execute("INSERT INTO sales VALUES(?,?,?)", ("Bull Item (Field-Tested)", t, 15.0))  # медіана угод вища за найдешевшу пропозицію
    bm = analyze_item(db, cfg, "Bull Item (Field-Tested)")
    assert bm["median"] == 15.0 and any("нижча за медіану" in x for x in bm["reasons"]) and bm["score"] == b["score"]
    assert "Медіана реальних угод" in handle(db, cfg, "/analyze", "bull") and "медіана угод" in handle(db, cfg, "/report", "")
    db.execute("DELETE FROM sales")

    # скупка пропозиції дає додатковий бал
    db.execute("INSERT INTO scan VALUES(?,?,?,?)", ("Bull Item (Field-Tested)", t - 20 * 3600, 10.0, 100))
    db.execute("INSERT INTO scan VALUES(?,?,?,?)", ("Bull Item (Field-Tested)", t, 10.6, 60))
    b2 = analyze_item(db, cfg, "Bull Item (Field-Tested)")
    assert b2["score"] == b["score"] + 1 and any("🧲" in x for x in b2["reasons"])

    # пошук за назвою
    assert resolve_name(db, "bull") == ["Bull Item (Field-Tested)"]
    assert len(resolve_name(db, "item")) == 3 and resolve_name(db, "zzz") == []
    out = handle(db, cfg, "/analyze", "bull")
    assert "Висновок: 🟢" in out and "Комісія Steam" in out and DISCLAIMER in out, out
    assert "кілька" in handle(db, cfg, "/analyze", "item")
    assert "Не знаю" in handle(db, cfg, "/analyze", "zzz")
    assert "даних замало" in handle(db, cfg, "/analyze", "flat")
    summary = handle(db, cfg, "/analyze", "")
    assert summary.index("Bull Item") < summary.index("Bear Item"), summary  # бичачі вище

    # портфель
    for args in (("Bull Item (Field-Tested)", 10.0, 2, 10.0, 40.0), ("Bear Item (Field-Tested)", 20.0, 1, None, None),
                 ("Sticker | Titan (Holo) | Katowice 2014", 100.0, 3, 15.0, 50.0)):
        db.execute("INSERT INTO positions(name, price, qty, sl, tp, ts) VALUES(?,?,?,?,?,?)", (*args, t))
    db.execute("INSERT INTO targets(name, price, direction) VALUES(?,?,?)", ("Flat Item", 4.0, "below"))
    rv = handle(db, cfg, "/review", "")
    assert "без stop-loss" in rv and "беззбитковості" in rv and "Концентрація" in rv and "Структура" in rv, rv
    assert handle(radar.init_db(":memory:"), cfg, "/review", "").startswith("Позицій немає")

    # звіт для Claude
    db.execute("INSERT INTO alerts VALUES(?,?)", ("pump:Some Item", t - 3600))
    db.execute("INSERT INTO mentions VALUES(?,?,?)", ("Sticker | Titan (Holo) | Katowice 2014", t - 600, "reddit:1"))
    rp = handle(db, cfg, "/report", "")
    assert "CS2 RADAR REPORT" in rp and "Список відстеження" in rp and "Позиції:" in rp and "pump:Some Item" in rp, rp
    assert "Питання до аналітика" in rp and len(rp) <= 4096, len(rp)
    assert "падіння" not in rp  # один зростаючий предмет не має дублюватись у падіннях
    # довгий звіт не перевищує ліміт Telegram
    for i in range(40):
        seed(f"Very Long Item Name Number {i} (Field-Tested)", [10 + i] * 3)
    assert len(handle(db, cfg, "/report", "")) <= 4096
    assert handle(db, cfg, "/other", "") is None

    print("advisor selftest OK")
    print("\n--- приклад /analyze bull ---\n" + out)
    print("\n--- приклад /review ---\n" + rv)
    print("\n--- приклад /report ---\n" + rp)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        selftest()
    else:
        print(__doc__)
