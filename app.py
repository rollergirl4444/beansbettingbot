# app.py
import os
import re
import asyncio
import datetime as dt
from typing import Dict, List, Tuple, Optional

import requests
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------- Config ----------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret123")  # set a real secret in Render
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_URL")  # e.g. https://yourservice.onrender.com

ESPN_SCOREBOARD = {
    "mlb": "https://site.api.espn.com/apis/v2/sports/baseball/mlb/scoreboard",
    "nfl": "https://site.api.espn.com/apis/v2/sports/football/nfl/scoreboard",
}

app = FastAPI()
application: Optional[Application] = None  # telegram app (set in startup)

# ---------- Helpers ----------
def _today_params() -> Dict[str, str]:
    # ESPN uses ISO date; no key required
    today = dt.date.today()
    return {"dates": today.isoformat()}

def _parse_record(rec_summary: str) -> Tuple[int, int]:
    # "85-67" or "10-7-0" (NFL with ties)
    nums = [int(x) for x in re.findall(r"\d+", rec_summary)]
    if len(nums) >= 2:
        return nums[0], nums[1]
    return 0, 0

def fetch_games(sport: str) -> List[Dict]:
    url = ESPN_SCOREBOARD[sport]
    r = requests.get(url, params=_today_params(), timeout=20)
    r.raise_for_status()
    data = r.json()
    return data.get("events", [])

def summarize_game(ev: Dict) -> Dict:
    comp = ev["competitions"][0]["competitors"]
    home = next(c for c in comp if c["homeAway"] == "home")
    away = next(c for c in comp if c["homeAway"] == "away")

    def team_info(c):
        record_summary = ""
        if c.get("records"):
            record_summary = c["records"][0].get("summary", "")
        wins, losses = _parse_record(record_summary)
        score = int(c.get("score", 0)) if c.get("score") else 0
        return {
            "id": c["team"]["id"],
            "name": c["team"]["displayName"],
            "abbrev": c["team"].get("abbreviation", ""),
            "wins": wins,
            "losses": losses,
            "pct": (wins / max(1, (wins + losses))) if (wins + losses) > 0 else 0.0,
            "score": score,
            "home": c["homeAway"] == "home",
        }

    h, a = team_info(home), team_info(away)
    status = ev["status"]["type"]["description"]
    start = ev.get("date", "")
    return {"home": h, "away": a, "status": status, "start": start}

def predict_winner(g: Dict) -> Tuple[str, str]:
    """Very simple heuristic:
       1) Higher win% favored.
       2) If equal, higher current score (if in-progress).
       3) If still equal, home team.
    """
    home, away = g["home"], g["away"]
    reason = []
    if home["pct"] > away["pct"]:
        reason.append("better record")
        pick = home
    elif away["pct"] > home["pct"]:
        reason.append("better record")
        pick = away
    else:
        if home["score"] != away["score"]:
            reason.append("leading now")
            pick = home if home["score"] > away["score"] else away
        else:
            reason.append("home edge")
            pick = home
    return pick["name"], ", ".join(reason)

def build_daily_report(sport: str) -> str:
    try:
        events = fetch_games(sport)
    except Exception as e:
        return f"❌ Could not fetch {sport.upper()} games: {e}"

    if not events:
        return f"No {sport.upper()} games found for today."

    lines = [f"📅 {sport.upper()} games & picks"]
    for ev in events:
        g = summarize_game(ev)
        pick, why = predict_winner(g)
        matchup = f"{g['away']['name']} @ {g['home']['name']}"
        score = f"{g['away']['score']}-{g['home']['score']}"
        status = g["status"]
        lines.append(f"• {matchup} — pick: **{pick}** ({why})  [{status}; score {score}]")
    return "\n".join(lines)

# ---------- Telegram handlers ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hey! I predict winners for MLB & NFL.\n"
        "Use /today to get both, or /today mlb or /today nfl."
    )

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = [a.lower() for a in context.args]
    to_do = ["mlb", "nfl"] if not args else [a for a in args if a in ("mlb", "nfl")]
    if not to_do:
        to_do = ["mlb", "nfl"]
    parts = [build_daily_report(s) for s in to_do]
    await update.message.reply_text("\n\n".join(parts), disable_web_page_preview=True)

def build_bot() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    app_ = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app_.add_handler(CommandHandler("start", start_cmd))
    app_.add_handler(CommandHandler("today", today_cmd))
    return app_

class TelegramUpdate(BaseModel):
    update_id: int | None = None  # allow any payload; PTB will parse

@app.on_event("startup")
async def on_startup():
    global application
    application = build_bot()
    # Optionally auto-set webhook if PUBLIC_URL present
    if PUBLIC_URL and WEBHOOK_SECRET:
        url = f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}"
        await application.bot.set_webhook(url)
    # PTB needs to be initialized (but we won’t run its own web server)
    await application.initialize()
    await application.start()

@app.on_event("shutdown")
async def on_shutdown():
    if application:
        await application.stop()
        await application.shutdown()

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    payload = await request.json()
    update = Update.de_json(payload, application.bot)
    await application.process_update(update)
    return {"ok": True}
