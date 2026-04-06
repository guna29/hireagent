"""Telegram bot for remote control of HireAgent from your phone.

Setup:
  1. Message @BotFather on Telegram → /newbot → copy the token
  2. Start the bot once, send any message, then run:
       hireagent bot --setup
     to find your chat ID automatically.
  3. Set env vars (add to ~/.zshrc or ~/.bashrc):
       export HIREAGENT_TELEGRAM_TOKEN="your_bot_token"
       export HIREAGENT_TELEGRAM_CHAT_ID="your_chat_id"
  4. Run:  hireagent bot

Commands (send from Telegram):
  /status    - Job counts by stage
  /run       - Run full pipeline (discover → enrich → score → tailor)
  /apply     - Apply (dry-run, safe preview)
  /apply_go  - Actually submit applications (limit 10)
  /logs      - Last 20 log lines
  /stop      - Stop current running command
  /help      - Show commands
"""

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

from hireagent import config
from hireagent.database import get_connection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------

def _api(token: str, method: str, **kwargs) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        r = requests.post(url, json=kwargs, timeout=30)
        return r.json()
    except Exception as e:
        logger.warning("Telegram API error: %s", e)
        return {}


def send(token: str, chat_id: str, text: str, parse_mode: str = "Markdown") -> None:
    """Send a message, splitting if > 4096 chars."""
    max_len = 4000
    for i in range(0, len(text), max_len):
        chunk = text[i:i + max_len]
        _api(token, "sendMessage", chat_id=chat_id, text=chunk, parse_mode=parse_mode)


def get_updates(token: str, offset: int = 0, timeout: int = 30) -> list:
    data = _api(token, "getUpdates", offset=offset, timeout=timeout, allowed_updates=["message"])
    return data.get("result", [])


def notify(text: str) -> None:
    """Send a one-shot notification using env vars. No-op if not configured."""
    token = os.environ.get("HIREAGENT_TELEGRAM_TOKEN", "")
    chat_id = os.environ.get("HIREAGENT_TELEGRAM_CHAT_ID", "")
    if token and chat_id:
        send(token, chat_id, text)


def wait_for_reply(question: str, timeout_seconds: int = 600) -> str | None:
    """Send question to Telegram and block until the user replies or timeout.

    Returns the user's reply text, or None on timeout.
    Uses env vars HIREAGENT_TELEGRAM_TOKEN + HIREAGENT_TELEGRAM_CHAT_ID.
    """
    token = os.environ.get("HIREAGENT_TELEGRAM_TOKEN", "")
    chat_id = os.environ.get("HIREAGENT_TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("Telegram not configured — cannot wait for human input")
        return None

    # Get current update offset so we only read NEW messages
    updates = get_updates(token, offset=0, timeout=1)
    offset = 0
    if updates:
        offset = updates[-1]["update_id"] + 1

    send(token, chat_id, question)

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        remaining = int(deadline - time.time())
        poll_timeout = min(30, remaining)
        if poll_timeout <= 0:
            break
        updates = get_updates(token, offset=offset, timeout=poll_timeout)
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message", {})
            if str(msg.get("chat", {}).get("id", "")) != str(chat_id):
                continue
            text = msg.get("text", "").strip()
            if text:
                send(token, chat_id, f"✅ Got it: _{text}_\nResuming application...")
                return text

    send(token, chat_id, "⏱️ No reply received — marking job as paused for manual review.")
    return None


# ---------------------------------------------------------------------------
# Job stats
# ---------------------------------------------------------------------------

def get_status_text() -> str:
    try:
        conn = get_connection()
        stats = {
            "total": conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            "enriched": conn.execute("SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL").fetchone()[0],
            "scored": conn.execute("SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL").fetchone()[0],
            "tailored": conn.execute("SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL").fetchone()[0],
            "applied": conn.execute("SELECT COUNT(*) FROM jobs WHERE apply_status = 'applied'").fetchone()[0],
            "failed": conn.execute("SELECT COUNT(*) FROM jobs WHERE apply_status = 'failed'").fetchone()[0],
            "in_progress": conn.execute("SELECT COUNT(*) FROM jobs WHERE apply_status = 'in_progress'").fetchone()[0],
            "ready": conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL "
                "AND (apply_status IS NULL OR apply_status = 'skipped_preflight') "
                "AND (apply_attempts IS NULL OR apply_attempts < 3)"
            ).fetchone()[0],
        }
        lines = [
            "📊 *HireAgent Status*",
            f"• Discovered: {stats['total']}",
            f"• Enriched: {stats['enriched']}",
            f"• Scored: {stats['scored']}",
            f"• Tailored: {stats['tailored']}",
            f"• Ready to apply: {stats['ready']}",
            f"• Applied ✅: {stats['applied']}",
            f"• Failed ❌: {stats['failed']}",
            f"• In progress 🔄: {stats['in_progress']}",
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"❌ DB error: {e}"


def get_recent_logs(n: int = 20) -> str:
    try:
        log_dir = config.LOG_DIR
        log_files = sorted(log_dir.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not log_files:
            return "No log files found."
        latest = log_files[0]
        lines = latest.read_text(encoding="utf-8", errors="replace").strip().split("\n")
        tail = lines[-n:] if len(lines) > n else lines
        return f"📋 *Last {len(tail)} lines* (`{latest.name}`):\n```\n" + "\n".join(tail) + "\n```"
    except Exception as e:
        return f"❌ Log error: {e}"


# ---------------------------------------------------------------------------
# Command runner (subprocess)
# ---------------------------------------------------------------------------

_current_proc: dict = {"proc": None, "lock": threading.Lock()}


def _run_command(token: str, chat_id: str, args: list[str], label: str) -> None:
    """Run an hireagent subcommand and stream output to Telegram."""
    python = sys.executable
    cmd = [python, "-m", "hireagent"] + args

    send(token, chat_id, f"🚀 Starting: `{' '.join(args)}`")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        with _current_proc["lock"]:
            _current_proc["proc"] = proc

        # Collect output and send periodic updates
        lines: list[str] = []
        last_send = time.time()
        SEND_INTERVAL = 8  # seconds between Telegram updates

        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            lines.append(line)
            # Send buffered lines every SEND_INTERVAL seconds
            if time.time() - last_send > SEND_INTERVAL and lines:
                chunk = "\n".join(lines[-15:])  # last 15 lines
                send(token, chat_id, f"```\n{chunk}\n```")
                last_send = time.time()

        proc.wait(timeout=600)
        rc = proc.returncode

        # Send final summary
        final_lines = lines[-10:] if lines else ["(no output)"]
        summary = "\n".join(final_lines)
        icon = "✅" if rc == 0 else "⚠️"
        send(token, chat_id, f"{icon} *{label} finished* (exit {rc})\n```\n{summary}\n```")

    except subprocess.TimeoutExpired:
        proc.kill()
        send(token, chat_id, f"⏱️ *{label} timed out* (10 min limit)")
    except Exception as e:
        send(token, chat_id, f"❌ *{label} error*: {e}")
    finally:
        with _current_proc["lock"]:
            _current_proc["proc"] = None


def _run_in_thread(token: str, chat_id: str, args: list[str], label: str) -> None:
    with _current_proc["lock"]:
        if _current_proc["proc"] is not None:
            send(token, chat_id, "⚠️ A command is already running. Use /stop first.")
            return
    t = threading.Thread(target=_run_command, args=(token, chat_id, args, label), daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "🤖 *HireAgent Bot Commands*\n\n"
    "/status — Job counts at each stage\n"
    "/run — Full pipeline (discover→enrich→score→tailor)\n"
    "/discover — Discovery only\n"
    "/score — Score + tailor only\n"
    "/apply — Dry-run apply (safe, no submissions)\n"
    "/apply\\_go — *Actually submit* applications (limit 10)\n"
    "/logs — Recent log output\n"
    "/stop — Kill current running command\n"
    "/help — Show this message"
)


def handle_command(token: str, chat_id: str, text: str) -> None:
    cmd = text.strip().lower().split()[0] if text.strip() else ""

    if cmd in ("/start", "/help"):
        send(token, chat_id, HELP_TEXT)

    elif cmd == "/status":
        send(token, chat_id, get_status_text())

    elif cmd == "/logs":
        send(token, chat_id, get_recent_logs())

    elif cmd == "/run":
        _run_in_thread(token, chat_id,
                       ["run", "discover", "enrich", "score", "tailor"],
                       "Full pipeline")

    elif cmd == "/discover":
        _run_in_thread(token, chat_id, ["run", "discover"], "Discover")

    elif cmd == "/score":
        _run_in_thread(token, chat_id, ["run", "score", "tailor"], "Score + Tailor")

    elif cmd == "/apply":
        _run_in_thread(token, chat_id,
                       ["apply", "--dry-run", "--limit", "5", "--headless"],
                       "Apply (dry-run)")

    elif cmd == "/apply_go":
        send(token, chat_id, "⚡ Starting live apply (limit 10, headless)...")
        _run_in_thread(token, chat_id,
                       ["apply", "--limit", "10", "--headless"],
                       "Apply (live)")

    elif cmd == "/stop":
        with _current_proc["lock"]:
            proc = _current_proc["proc"]
        if proc and proc.poll() is None:
            proc.terminate()
            send(token, chat_id, "🛑 Stopped current command.")
        else:
            send(token, chat_id, "Nothing is running.")

    else:
        send(token, chat_id, f"Unknown command: `{cmd}`\nSend /help for options.")


# ---------------------------------------------------------------------------
# Setup helper (find your chat ID)
# ---------------------------------------------------------------------------

def run_setup(token: str) -> None:
    """Poll for a message to discover the user's chat ID."""
    print("Send any message to your bot on Telegram now...")
    offset = 0
    for _ in range(60):  # 60 second timeout
        updates = get_updates(token, offset=offset, timeout=5)
        for upd in updates:
            msg = upd.get("message", {})
            chat = msg.get("chat", {})
            chat_id = str(chat.get("id", ""))
            username = chat.get("username", "")
            first = chat.get("first_name", "")
            if chat_id:
                print(f"\nFound chat!")
                print(f"  Name: {first} (@{username})")
                print(f"  Chat ID: {chat_id}")
                print(f"\nAdd these to your environment:")
                print(f'  export HIREAGENT_TELEGRAM_TOKEN="{token}"')
                print(f'  export HIREAGENT_TELEGRAM_CHAT_ID="{chat_id}"')
                print(f"\nOr add to ~/.hireagent/telegram.env and run:")
                print(f'  source ~/.hireagent/telegram.env && hireagent bot')
                # Save to file
                env_file = Path.home() / ".hireagent" / "telegram.env"
                env_file.parent.mkdir(exist_ok=True)
                env_file.write_text(
                    f'export HIREAGENT_TELEGRAM_TOKEN="{token}"\n'
                    f'export HIREAGENT_TELEGRAM_CHAT_ID="{chat_id}"\n'
                )
                print(f"\nSaved to: {env_file}")
                return
            offset = upd["update_id"] + 1
        time.sleep(1)
    print("Timed out. Make sure you sent a message to your bot.")


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------

def run_bot(token: str, chat_id: str) -> None:
    """Start the Telegram bot polling loop."""
    print(f"🤖 HireAgent bot started. Send /help in Telegram to begin.")
    print(f"   Chat ID: {chat_id}")
    print(f"   Press Ctrl+C to stop.\n")

    # Send startup message
    send(token, chat_id, "🤖 *HireAgent bot online*\nSend /help for commands.")

    offset = 0
    while True:
        try:
            updates = get_updates(token, offset=offset, timeout=30)
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                msg_chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "")

                # Security: only respond to authorized chat
                if msg_chat_id != str(chat_id):
                    logger.warning("Ignored message from unauthorized chat_id: %s", msg_chat_id)
                    continue

                if text:
                    logger.info("Received: %s", text[:60])
                    try:
                        handle_command(token, chat_id, text)
                    except Exception as e:
                        send(token, chat_id, f"❌ Error: {e}")

        except KeyboardInterrupt:
            send(token, chat_id, "🛑 Bot stopped.")
            print("\nBot stopped.")
            break
        except requests.exceptions.ConnectionError:
            logger.warning("Network error, retrying in 10s...")
            time.sleep(10)
        except Exception as e:
            logger.warning("Bot loop error: %s", e)
            time.sleep(5)
