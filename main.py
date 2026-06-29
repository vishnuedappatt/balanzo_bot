import os
import calendar
import asyncio
import logging

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    BotCommand,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    Defaults,
    filters,
)
from dotenv import load_dotenv

from database import conn, cursor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# All scheduled jobs run in IST. The JobQueue's APScheduler defaults to UTC,
# so without this 9 AM would fire at 9 AM UTC (= 2:30 PM IST).
IST = ZoneInfo("Asia/Kolkata")
import reports
import groups

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
# Telegram's network layer logs every retry at WARNING — keep those quiet so the
# console stays readable; the Updater keeps retrying on its own.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("balanzo")


# -------------------------
# STATE
# -------------------------
user_state = {}      # in-progress add flow per user
summary_ctx = {}     # selected summary date range per user
group_ctx = {}       # active group id per user (for group dashboard navigation)

RUPEE = "₹"


def _row(name, amount, indent=0, width=30):
    """A left-label / right-amount row for monospace <pre> blocks."""
    left = " " * indent + name
    right = f"{RUPEE}{amount:,.2f}"
    pad = max(1, width - len(left) - len(right))
    return f"{left}{' ' * pad}{right}"


# -------------------------
# CONFIG: per-type labels + categories
# -------------------------
TYPE_LABEL = {
    "income": ("💰 Income", "adding money"),
    "expense": ("💸 Expense", "spending money"),
    "lent": ("🤝 Lent", "money you gave out"),
    "borrow": ("💳 Borrow", "money you took"),
}

CATEGORIES = {
    "income": [
        ("💼 Salary", "salary"),
        ("🧑‍💻 Freelance", "freelance"),
        ("🎁 Contribution", "contribution"),
    ],
    "expense": [
        ("🏠 Home", "home"),
        ("🙋 Self", "self"),
        ("🏦 Loan", "loan"),
        ("🚕 Transportation", "transportation"),
        ("📆 EMI", "emi"),
    ],
    "lent": [
        ("👫 Friends", "friends"),
        ("👨‍👩‍👧 Family", "family"),
    ],
    "borrow": [
        ("👫 Friends", "friends"),
    ],
}


# -------------------------
# KEYBOARDS
# -------------------------
def home():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💸 Expense", callback_data="expense"),
            InlineKeyboardButton("💰 Income", callback_data="income"),
        ],
        [
            InlineKeyboardButton("🤝 Lent", callback_data="lent"),
            InlineKeyboardButton("💳 Borrow", callback_data="borrow"),
        ],
        [
            InlineKeyboardButton("📋 Pending Loans", callback_data="pending"),
        ],
        [
            InlineKeyboardButton("📊 Summary", callback_data="summary"),
        ],
        [
            InlineKeyboardButton("👥 Groups / Trips", callback_data="groups"),
        ],
    ])


def category_keyboard(type_):
    """Two buttons per row for the given transaction type."""
    cats = CATEGORIES[type_]
    rows, row = [], []
    for label, value in cats:
        row.append(InlineKeyboardButton(label, callback_data=f"cat_{value}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def note_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Skip", callback_data="note_skip")],
    ])


def calendar_keyboard(year, month):
    """Inline month grid. Past days are disabled; ◀ ▶ change month.

    Day buttons emit `calpick_YYYY-MM-DD`; nav emits `calnav_YYYY-MM`;
    blanks/labels emit `cal_ignore`.
    """
    today = datetime.now().date()
    rows = [
        [InlineKeyboardButton(f"{calendar.month_name[month]} {year}",
                              callback_data="cal_ignore")],
        [InlineKeyboardButton(d, callback_data="cal_ignore")
         for d in ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")],
    ]
    for week in calendar.monthcalendar(year, month):   # weeks start Monday
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="cal_ignore"))
                continue
            d = datetime(year, month, day).date()
            if d < today:
                row.append(InlineKeyboardButton("·", callback_data="cal_ignore"))
            else:
                label = f"[{day}]" if d == today else str(day)
                row.append(InlineKeyboardButton(
                    label, callback_data=f"calpick_{d.strftime('%Y-%m-%d')}"))
        rows.append(row)

    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    # only allow going back while the previous month still holds today-or-future
    if (year, month) > (today.year, today.month):
        prev_btn = InlineKeyboardButton("◀", callback_data=f"calnav_{prev_y}-{prev_m:02d}")
    else:
        prev_btn = InlineKeyboardButton(" ", callback_data="cal_ignore")
    rows.append([
        prev_btn,
        InlineKeyboardButton("▶", callback_data=f"calnav_{next_y}-{next_m:02d}"),
    ])
    return InlineKeyboardMarkup(rows)


def pending_keyboard(loans):
    """One row per open loan, plus Home."""
    rows = []
    for ln in loans:
        arrow = "🤝" if ln["direction"] == "lent" else "💳"
        rows.append([InlineKeyboardButton(
            f"{arrow} {ln['person']} · ₹{ln['outstanding']:,.0f} · due {ln['due_date']}",
            callback_data=f"loan_{ln['id']}")])
    rows.append([InlineKeyboardButton("🏠 Home", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def loan_detail_keyboard(loan_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 Record Repayment", callback_data=f"loanpay_{loan_id}")],
        [InlineKeyboardButton("✅ Mark Fully Settled", callback_data=f"loansettle_{loan_id}")],
        [
            InlineKeyboardButton("⬅ Pending Loans", callback_data="pending"),
            InlineKeyboardButton("🏠 Home", callback_data="home"),
        ],
    ])


def reminder_keyboard(loan_id):
    """Buttons attached to each hourly due/overdue reminder."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I'm aware (mute today)",
                              callback_data=f"loanack_{loan_id}")],
        [InlineKeyboardButton("💵 Record Repayment",
                              callback_data=f"loanpay_{loan_id}")],
    ])


def range_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Today", callback_data="range_today")],
        [InlineKeyboardButton("📅 This Week", callback_data="range_week")],
        [InlineKeyboardButton("📅 This Month", callback_data="range_month")],
        [InlineKeyboardButton("🗓 Custom Date", callback_data="range_custom")],
    ])


def view_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 Balance Check", callback_data="view_balance")],
        [InlineKeyboardButton("📋 Detailed Summary", callback_data="view_detailed")],
        [InlineKeyboardButton("📝 Short Summary", callback_data="view_short")],
        [InlineKeyboardButton("💸 Expense by Category", callback_data="view_expcat")],
        [InlineKeyboardButton("🤝 Loans Detail", callback_data="view_loans")],
        [InlineKeyboardButton("📥 Download Report", callback_data="report")],
        [InlineKeyboardButton("🏠 Home", callback_data="home")],
    ])


def report_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📄 PDF", callback_data="report_pdf"),
            InlineKeyboardButton("🖼 Image", callback_data="report_img"),
        ],
        [InlineKeyboardButton("⬅ Back", callback_data="view_back")],
    ])


# -------------------------
# GROUP KEYBOARDS
# -------------------------
def groups_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Create Group", callback_data="grp_create")],
        [InlineKeyboardButton("🔑 Join Group", callback_data="grp_join")],
        [InlineKeyboardButton("📋 My Groups", callback_data="grp_list")],
        [InlineKeyboardButton("🏠 Home", callback_data="home")],
    ])


def group_dashboard(group_id, is_owner):
    rows = [
        [InlineKeyboardButton("➕ Add Expense", callback_data=f"gexp_{group_id}")],
        [InlineKeyboardButton("📊 Settlement", callback_data=f"gsummary_{group_id}")],
        [
            InlineKeyboardButton("👥 Members", callback_data=f"gmembers_{group_id}"),
            InlineKeyboardButton("📜 History", callback_data=f"ghistory_{group_id}"),
        ],
        [InlineKeyboardButton("📥 Download Report", callback_data=f"greport_{group_id}")],
        [InlineKeyboardButton("🔑 Copy Join Code", callback_data=f"gcode_{group_id}")],
    ]
    if is_owner:
        rows.append([
            InlineKeyboardButton("✅ Approvals", callback_data=f"gpend_{group_id}"),
            InlineKeyboardButton("🗑 Delete Group", callback_data=f"gdelete_{group_id}"),
        ])
    rows.append([
        InlineKeyboardButton("⬅ My Groups", callback_data="grp_list"),
        InlineKeyboardButton("🏠 Home", callback_data="home"),
    ])
    return InlineKeyboardMarkup(rows)


def payer_keyboard(members):
    """Who paid — one button per active member."""
    rows = [[InlineKeyboardButton(f"💳 {m['name']}", callback_data=f"gpay_{m['user_id']}")]
            for m in members]
    return InlineKeyboardMarkup(rows)


def split_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟰 Equally (everyone)", callback_data="gsplit_equal")],
        [InlineKeyboardButton("👤 Selected people", callback_data="gsplit_selected")],
        [InlineKeyboardButton("％ By percentage", callback_data="gsplit_percentage")],
    ])


def members_select_keyboard(members, selected):
    """Toggle list for the 'selected people' split, with a Done button."""
    rows = []
    for m in members:
        mark = "✅" if m["user_id"] in selected else "⬜"
        rows.append([InlineKeyboardButton(
            f"{mark} {m['name']}", callback_data=f"gsel_{m['user_id']}")])
    rows.append([InlineKeyboardButton("✔ Done", callback_data="gsel_done")])
    return InlineKeyboardMarkup(rows)


def report_kind_keyboard(group_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📄 PDF", callback_data="greportpdf"),
            InlineKeyboardButton("🖼 Image", callback_data="greportimg"),
        ],
        [InlineKeyboardButton("⬅ Back", callback_data=f"grp_{group_id}")],
    ])


def approve_keyboard(row_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"gapprove_{row_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"greject_{row_id}"),
    ]])


def back_to_group_keyboard(group_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅ Back to Group", callback_data=f"grp_{group_id}"),
    ]])


def delete_confirm_keyboard(group_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, Delete", callback_data=f"gdelete_confirm_{group_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"grp_{group_id}"),
    ]])


# -------------------------
# START
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏠 Main Menu:", reply_markup=home())


# -------------------------
# DATA HELPERS
# -------------------------
def date_range(mode):
    """Return (start_str, end_str, label) for a named range."""
    now = datetime.now()
    end = now
    if mode == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        label = "Today"
    elif mode == "week":
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        label = "This Week"
    elif mode == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        label = "This Month"
    else:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        label = "Today"
    fmt = "%Y-%m-%d %H:%M:%S"
    return start.strftime(fmt), end.strftime(fmt), label


def get_totals(user_id, start, end):
    cursor.execute("""
        SELECT type, COALESCE(SUM(amount), 0)
        FROM transactions
        WHERE user_id=? AND created_at BETWEEN ? AND ?
        GROUP BY type
    """, (user_id, start, end))
    totals = {"income": 0, "expense": 0, "lent": 0, "borrow": 0}
    for t, amt in cursor.fetchall():
        if t in totals:
            totals[t] = amt or 0
    return totals


def get_breakdowns(user_id, start, end):
    """Per-type category breakdown: {type: [(category, amount), ...]}."""
    cursor.execute("""
        SELECT type, category, COALESCE(SUM(amount), 0)
        FROM transactions
        WHERE user_id=? AND created_at BETWEEN ? AND ?
        GROUP BY type, category
        ORDER BY SUM(amount) DESC
    """, (user_id, start, end))
    out = {"income": [], "expense": [], "lent": [], "borrow": []}
    for t, cat, amt in cursor.fetchall():
        if t in out:
            out[t].append((cat or "uncategorized", amt or 0))
    return out


# -------------------------
# LOAN HELPERS
# -------------------------
def loan_outstanding(loan_id):
    """Original amount minus everything paid back so far."""
    cursor.execute("SELECT amount FROM loans WHERE id=?", (loan_id,))
    row = cursor.fetchone()
    if not row:
        return 0.0
    cursor.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM loan_payments WHERE loan_id=?",
        (loan_id,))
    paid = cursor.fetchone()[0] or 0
    return round((row[0] or 0) - paid, 2)


def open_loans(user_id):
    """All still-open loans for a user, ordered by soonest due date."""
    cursor.execute("""
        SELECT id, direction, person, amount, due_date
        FROM loans
        WHERE user_id=? AND status='open'
        ORDER BY due_date ASC, id ASC
    """, (user_id,))
    rows = []
    for lid, direction, person, amount, due in cursor.fetchall():
        rows.append({
            "id": lid, "direction": direction, "person": person,
            "amount": amount or 0, "due_date": due,
            "outstanding": loan_outstanding(lid),
        })
    return rows


def get_loan(loan_id):
    cursor.execute("""
        SELECT id, user_id, direction, person, amount, category, note,
               due_date, status
        FROM loans WHERE id=?
    """, (loan_id,))
    r = cursor.fetchone()
    if not r:
        return None
    keys = ("id", "user_id", "direction", "person", "amount", "category",
            "note", "due_date", "status")
    return dict(zip(keys, r))


def record_payment(loan_id, amount, note=""):
    """Add a part-payment and auto-settle the loan once fully paid.

    Returns the remaining outstanding balance after this payment.
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT INTO loan_payments(loan_id, amount, note, created_at)
        VALUES (?, ?, ?, ?)
    """, (loan_id, amount, note, now_str))
    conn.commit()
    remaining = loan_outstanding(loan_id)
    if remaining <= 0:
        cursor.execute("UPDATE loans SET status='settled' WHERE id=?", (loan_id,))
        conn.commit()
    return remaining


def loan_payment_history(loan_id):
    cursor.execute("""
        SELECT amount, created_at FROM loan_payments
        WHERE loan_id=? ORDER BY id ASC
    """, (loan_id,))
    return cursor.fetchall()


def loans_in_range(user_id, start, end):
    """Loans created within a date range, with returned / outstanding split."""
    cursor.execute("""
        SELECT id, direction, person, amount, due_date, status, created_at
        FROM loans
        WHERE user_id=? AND created_at BETWEEN ? AND ?
        ORDER BY direction, due_date ASC, id ASC
    """, (user_id, start, end))
    out = []
    for lid, direction, person, amount, due, status, created in cursor.fetchall():
        outstanding = loan_outstanding(lid)
        out.append({
            "id": lid, "direction": direction, "person": person,
            "amount": amount or 0, "due_date": due, "status": status,
            "created": str(created)[:10] if created else "",
            "returned": round((amount or 0) - outstanding, 2),
            "outstanding": outstanding,
        })
    return out


def loan_statement(user_id, start, end):
    """Per-direction loan statement for a range.

    Returns {'lent': {...}, 'borrow': {...}} where each section has:
      loans       - tracked loans (with a `payments` list of (amount, date))
      original    - gross lent/borrow from transactions (incl. legacy rows)
      returned    - total repaid against tracked loans
      untracked   - original minus tracked principal (legacy / category-only)
      outstanding - original minus returned (the net still out)
    """
    cursor.execute("""
        SELECT type, COALESCE(SUM(amount), 0)
        FROM transactions
        WHERE user_id=? AND type IN ('lent', 'borrow')
          AND created_at BETWEEN ? AND ?
        GROUP BY type
    """, (user_id, start, end))
    gross = {"lent": 0.0, "borrow": 0.0}
    for t, amt in cursor.fetchall():
        gross[t] = amt or 0

    all_loans = loans_in_range(user_id, start, end)
    result = {}
    for direction in ("lent", "borrow"):
        loans = [l for l in all_loans if l["direction"] == direction]
        for l in loans:
            l["payments"] = loan_payment_history(l["id"])
        tracked = sum(l["amount"] for l in loans)
        returned = sum(l["returned"] for l in loans)
        result[direction] = {
            "loans": loans,
            "original": gross[direction],
            "returned": round(returned, 2),
            "untracked": round(gross[direction] - tracked, 2),
            "outstanding": round(gross[direction] - returned, 2),
        }
    return result


def report_entries(user_id, start, end):
    """Dated line items per type for the detailed report.

    {type: [(date, name, amount), ...]} — income/expense come straight from
    transactions; lent/borrow list each loan (person + due date) followed by its
    dated repayments as negative lines, plus any legacy 'untracked' remainder.
    """
    out = {"income": [], "expense": [], "lent": [], "borrow": []}

    cursor.execute("""
        SELECT type, created_at, category, note, amount
        FROM transactions
        WHERE user_id=? AND type IN ('income', 'expense')
          AND created_at BETWEEN ? AND ?
        ORDER BY created_at ASC
    """, (user_id, start, end))
    for t, created, cat, note, amt in cursor.fetchall():
        name = cat or "uncategorized"
        if note:
            name += f" - {note}"
        out[t].append((str(created)[:10], name, amt or 0))

    stmt = loan_statement(user_id, start, end)
    for direction in ("lent", "borrow"):
        tag = "returned" if direction == "lent" else "repaid"
        for l in stmt[direction]["loans"]:
            out[direction].append(
                (l["created"], f"{l['person']} (due {l['due_date']})", l["amount"]))
            for amt, dt in l["payments"]:
                out[direction].append((str(dt)[:10], f"  {tag}", -(amt or 0)))
        if stmt[direction]["untracked"] > 0:
            out[direction].append(("-", "other (untracked)",
                                   stmt[direction]["untracked"]))
    return out


# -------------------------
# GROUP HELPERS
# -------------------------
async def notify_group(context, group_id, text, exclude_id=None):
    """Broadcast a message to every active member (skipping exclude_id).

    Wrapped per-member so one blocked/inactive user can't break the broadcast.
    """
    for m in groups.active_members(group_id):
        if m["user_id"] == exclude_id:
            continue
        try:
            await context.bot.send_message(chat_id=m["user_id"], text=text)
        except Exception:
            pass


async def show_dashboard(message, user_id, group_id):
    g = groups.get_group(group_id)
    if not g or not groups.is_active_member(group_id, user_id):
        await message.reply_text("⚠️ Group not found or you're not a member.",
                                 reply_markup=home())
        return
    group_ctx[user_id] = group_id
    members = groups.active_members(group_id)
    owner = groups.is_owner(group_id, user_id)
    await message.reply_text(
        f"👥 *{g['name']}*\n"
        f"Group ID: `{group_id}`\n"
        f"Members: {len(members)}\n"
        f"{'👑 You are the owner.' if owner else ''}",
        parse_mode="Markdown",
        reply_markup=group_dashboard(group_id, owner),
    )


def settlement_text(group_name, s):
    lines = [f"📊 *Settlement — {group_name}*", ""]
    lines.append("*Per person* (net = paid − share):")
    for m in s["per_member"]:
        sign = "🟢 +" if m["net"] >= 0 else "🔴 −"
        lines.append(
            f"• {m['name']}: paid ₹{m['paid']:,.2f}, share ₹{m['owed']:,.2f} "
            f"→ {sign}₹{abs(m['net']):,.2f}"
        )
    lines.append("")
    lines.append("*Who pays whom:*")
    if not s["transfers"]:
        lines.append("✅ All settled up!")
    else:
        for t in s["transfers"]:
            lines.append(f"➡️ {t['from_name']} pays {t['to_name']} ₹{t['amount']:,.2f}")
    lines.append("")
    lines.append(f"🧮 *Total trip expense: ₹{s['total']:,.2f}*")
    return "\n".join(lines)


# -------------------------
# BUTTON HANDLER
# -------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    # ---------------- HOME ----------------
    if data == "home":
        user_state.pop(user_id, None)
        await query.message.reply_text("🏠 Main Menu:", reply_markup=home())
        return

    # ---------------- START ADD FLOW ----------------
    if data in ["expense", "income", "lent", "borrow"]:
        user_state[user_id] = {"type": data, "step": "amount"}
        emoji_label, desc = TYPE_LABEL[data]
        await query.message.reply_text(
            f"{emoji_label} — {desc}\n\n💰 Enter amount:"
        )
        return

    # ---------------- CATEGORY ----------------
    if data.startswith("cat_"):
        state = user_state.get(user_id)
        if not state:
            await query.message.reply_text("Session expired. Tap a button to start.",
                                           reply_markup=home())
            return
        state["category"] = data.replace("cat_", "")
        if state["type"] in ("lent", "borrow"):
            state["step"] = "person"
            who = "lend to" if state["type"] == "lent" else "borrow from"
            await query.message.reply_text(
                f"👤 Who did you {who}? (e.g. Ravi)"
            )
            return
        state["step"] = "note"
        await query.message.reply_text(
            "📝 Add a note for this entry, or tap Skip:",
            reply_markup=note_keyboard(),
        )
        return

    # ---------------- SKIP NOTE ----------------
    if data == "note_skip":
        state = user_state.get(user_id)
        if state and state.get("step") == "note":
            await save_transaction(query.message, user_id, state, "")
        return

    # ---------------- CALENDAR (due-date picker) ----------------
    if data == "cal_ignore":
        return

    if data.startswith("calnav_"):
        ym = data.split("_", 1)[1]
        y, m = (int(x) for x in ym.split("-"))
        try:
            await query.edit_message_reply_markup(reply_markup=calendar_keyboard(y, m))
        except Exception:
            pass  # ignore "message is not modified"
        return

    if data.startswith("calpick_"):
        state = user_state.get(user_id)
        if not state or state.get("step") != "due_date":
            await query.message.reply_text("Session expired. Tap a button to start.",
                                           reply_markup=home())
            return
        picked = data.split("_", 1)[1]            # YYYY-MM-DD
        if datetime.strptime(picked, "%Y-%m-%d").date() < datetime.now().date():
            await query.message.reply_text("❌ That date is in the past.")
            return
        state["due_date"] = picked
        state["step"] = "note"
        await query.message.reply_text(
            f"📅 Due date set to {picked}.\n\n"
            "📝 Add a note for this entry, or tap Skip:",
            reply_markup=note_keyboard(),
        )
        return

    # ---------------- PENDING LOANS ----------------
    if data == "pending":
        user_state.pop(user_id, None)
        await show_pending(query.message, user_id)
        return

    if data.startswith("loan_"):
        loan_id = int(data.split("_")[1])
        await show_loan_detail(query.message, user_id, loan_id)
        return

    if data.startswith("loanpay_"):
        loan_id = int(data.split("_")[1])
        loan = get_loan(loan_id)
        if not loan or loan["user_id"] != user_id or loan["status"] != "open":
            await query.message.reply_text("That loan is no longer open.",
                                           reply_markup=home())
            return
        outstanding = loan_outstanding(loan_id)
        user_state[user_id] = {"step": "loan_payment", "loan_id": loan_id}
        verb = "received from" if loan["direction"] == "lent" else "paid to"
        await query.message.reply_text(
            f"💵 How much was {verb} {loan['person']}?\n"
            f"Outstanding: ₹{outstanding:,.2f}\n\n"
            f"Enter an amount (≤ {outstanding:,.2f}):"
        )
        return

    if data.startswith("loansettle_"):
        loan_id = int(data.split("_")[1])
        loan = get_loan(loan_id)
        if not loan or loan["user_id"] != user_id or loan["status"] != "open":
            await query.message.reply_text("That loan is no longer open.",
                                           reply_markup=home())
            return
        outstanding = loan_outstanding(loan_id)
        record_payment(loan_id, outstanding, note="settled in full")
        user_state.pop(user_id, None)
        await query.message.reply_text(
            f"✅ Marked settled. {loan['person']}'s loan is fully cleared.",
            reply_markup=home())
        return

    if data.startswith("loanack_"):
        loan_id = int(data.split("_")[1])
        loan = get_loan(loan_id)
        if not loan or loan["user_id"] != user_id:
            await query.answer()
            return
        # Mute today's hourly reminders for this loan; it resumes tomorrow if
        # still unpaid. Settling the loan stops reminders for good.
        today = datetime.now().strftime("%Y-%m-%d")
        cursor.execute("UPDATE loans SET reminded_on=? WHERE id=?",
                       (today, loan_id))
        conn.commit()
        await query.message.reply_text(
            f"👍 Okay — no more reminders today about {loan['person']}'s "
            f"₹{loan_outstanding(loan_id):,.2f}. I'll remind you again tomorrow "
            f"if it's still pending.")
        return

    # ---------------- SUMMARY: pick date range ----------------
    if data == "summary":
        await query.message.reply_text(
            "📊 Choose a date range:", reply_markup=range_keyboard()
        )
        return

    if data.startswith("range_"):
        mode = data.replace("range_", "")
        if mode == "custom":
            user_state[user_id] = {"step": "custom_date"}
            await query.message.reply_text(
                "🗓 Send the date range as:\n"
                "`YYYY-MM-DD to YYYY-MM-DD`\n\n"
                "Or a single day: `YYYY-MM-DD`",
                parse_mode="Markdown",
            )
            return
        start, end, label = date_range(mode)
        summary_ctx[user_id] = {"start": start, "end": end, "label": label}
        await query.message.reply_text(
            f"📊 {label} — choose a view:", reply_markup=view_keyboard()
        )
        return

    # ---------------- SUMMARY VIEWS ----------------
    if data == "view_back":
        ctx = summary_ctx.get(user_id)
        label = ctx["label"] if ctx else "Summary"
        await query.message.reply_text(
            f"📊 {label} — choose a view:", reply_markup=view_keyboard()
        )
        return

    if data in ("view_balance", "view_detailed", "view_short", "view_expcat",
                "view_loans"):
        await send_view(query, user_id, data)
        return

    # ---------------- REPORT ----------------
    if data == "report":
        await query.message.reply_text(
            "📥 Choose report format:", reply_markup=report_keyboard()
        )
        return

    if data in ("report_pdf", "report_img"):
        await send_report(query, user_id, data)
        return

    # ================= GROUPS =================
    if data == "groups":
        user_state.pop(user_id, None)
        await query.message.reply_text(
            "👥 *Groups / Trips*\nSplit shared expenses with your team.",
            parse_mode="Markdown", reply_markup=groups_menu())
        return

    if data == "grp_create":
        user_state[user_id] = {"flow": "group", "step": "g_name"}
        await query.message.reply_text("🏷 Send a name for the group/trip (e.g. *Goa Trip*):",
                                       parse_mode="Markdown")
        return

    if data == "grp_join":
        user_state[user_id] = {"flow": "group", "step": "g_code"}
        await query.message.reply_text("🔑 Send the group's *join code*:",
                                       parse_mode="Markdown")
        return

    if data == "grp_list":
        gs = groups.user_groups(user_id)
        if not gs:
            await query.message.reply_text(
                "You're not in any group yet. Create or join one.",
                reply_markup=groups_menu())
            return
        rows = [[InlineKeyboardButton(f"👥 {g['name']}", callback_data=f"grp_{g['id']}")]
                for g in gs]
        rows.append([InlineKeyboardButton("⬅ Back", callback_data="groups")])
        await query.message.reply_text("📋 *Your groups:*", parse_mode="Markdown",
                                       reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("grp_"):
        gid = int(data.split("_")[1])
        await show_dashboard(query.message, user_id, gid)
        return

    if data.startswith("gcode_"):
        gid = int(data.split("_")[1])
        g = groups.get_group(gid)
        if g and groups.is_active_member(gid, user_id):
            await query.message.reply_text(
                f"🔑 Join code for *{g['name']}*:\n\n`{g['join_code']}`\n\n"
                "Share this with people you want to add. They open this bot → "
                "Groups → Join Group → send this code. The owner will approve each request.",
                parse_mode="Markdown",
                reply_markup=back_to_group_keyboard(gid))
        return

    if data.startswith("gpend_"):
        gid = int(data.split("_")[1])
        if not groups.is_owner(gid, user_id):
            return
        pend = groups.pending_members(gid)
        if not pend:
            await query.message.reply_text("✅ No pending requests.",
                                           reply_markup=back_to_group_keyboard(gid))
            return
        for p in pend:
            uname = f" (@{p['username']})" if p['username'] else ""
            await query.message.reply_text(
                f"🙋 *{p['name']}*{uname} wants to join.",
                parse_mode="Markdown", reply_markup=approve_keyboard(p['row_id']))
        return

    if data.startswith("gdelete_confirm_"):
        gid = int(data.split("_")[2])
        if not groups.is_owner(gid, user_id):
            await query.message.reply_text("⚠️ Only the owner can delete this group.")
            return
        g = groups.get_group(gid)
        if groups.delete_group(gid, user_id):
            group_ctx.pop(user_id, None)
            await query.message.reply_text(
                f"🗑 Group *{g['name']}* has been deleted.",
                parse_mode="Markdown",
                reply_markup=groups_menu())
        else:
            await query.message.reply_text("⚠️ Failed to delete group.")
        return

    if data.startswith("gdelete_"):
        gid = int(data.split("_")[1])
        if not groups.is_owner(gid, user_id):
            await query.message.reply_text("⚠️ Only the owner can delete this group.")
            return
        g = groups.get_group(gid)
        await query.message.reply_text(
            f"⚠️ Are you sure you want to delete *{g['name']}*?\n\n"
            "This will permanently delete all expenses, members, and data.",
            parse_mode="Markdown",
            reply_markup=delete_confirm_keyboard(gid))
        return

    if data.startswith("gapprove_") or data.startswith("greject_"):
        row_id = int(data.split("_")[1])
        member = groups.get_member_row(row_id)
        if not member or not groups.is_owner(member["group_id"], user_id):
            await query.message.reply_text("⚠️ Not allowed or request expired.")
            return
        g = groups.get_group(member["group_id"])
        if data.startswith("gapprove_"):
            groups.approve_member(row_id)
            await query.edit_message_text(f"✅ Approved {member['display_name']}.")
            try:
                await context.bot.send_message(
                    chat_id=member["user_id"],
                    text=f"🎉 You've been approved to join *{g['name']}*!",
                    parse_mode="Markdown")
            except Exception:
                pass
            await notify_group(context, member["group_id"],
                               f"👋 {member['display_name']} joined {g['name']}.",
                               exclude_id=member["user_id"])
        else:
            groups.reject_member(row_id)
            await query.edit_message_text(f"❌ Rejected {member['display_name']}.")
            try:
                await context.bot.send_message(
                    chat_id=member["user_id"],
                    text=f"Sorry, your request to join *{g['name']}* was declined.",
                    parse_mode="Markdown")
            except Exception:
                pass
        return

    if data.startswith("gmembers_"):
        gid = int(data.split("_")[1])
        if not groups.is_active_member(gid, user_id):
            return
        members = groups.active_members(gid)
        lines = ["👥 *Members:*"]
        for m in members:
            crown = " 👑" if m["role"] == "owner" else ""
            lines.append(f"• {m['name']}{crown}")
        await query.message.reply_text("\n".join(lines), parse_mode="Markdown",
                                       reply_markup=back_to_group_keyboard(gid))
        return

    if data.startswith("gsummary_"):
        gid = int(data.split("_")[1])
        if not groups.is_active_member(gid, user_id):
            return
        g = groups.get_group(gid)
        s = groups.settlement(gid)
        await query.message.reply_text(
            settlement_text(g["name"], s), parse_mode="Markdown",
            reply_markup=back_to_group_keyboard(gid))
        return

    if data.startswith("ghistory_"):
        gid = int(data.split("_")[1])
        if not groups.is_active_member(gid, user_id):
            return
        exps = groups.list_expenses(gid)
        if not exps:
            await query.message.reply_text("📜 No expenses yet.",
                                           reply_markup=back_to_group_keyboard(gid))
            return
        rows = []
        for e in exps:
            payer = groups.member_name(gid, e["payer_id"])
            label = f"₹{e['amount']:,.0f} · {e['description'][:18]} · {payer}"
            rows.append([InlineKeyboardButton(label, callback_data=f"gview_{e['id']}")])
        rows.append([InlineKeyboardButton("⬅ Back", callback_data=f"grp_{gid}")])
        await query.message.reply_text("📜 *Expense history* (tap one to view):",
                                       parse_mode="Markdown",
                                       reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("gview_"):
        exp_id = int(data.split("_")[1])
        exp = groups.get_expense(exp_id)
        if not exp or not groups.is_active_member(exp["group_id"], user_id):
            return
        gid = exp["group_id"]
        payer = groups.member_name(gid, exp["payer_id"])
        adder = groups.member_name(gid, exp["created_by"])
        shares = groups.get_shares(exp_id)
        lines = [
            f"🧾 *{exp['description']}*",
            f"💰 Amount: ₹{exp['amount']:,.2f}",
            f"💳 Paid by: {payer}",
            f"🔀 Split: {exp['split_type']}",
            f"📅 {exp['created_at']}  · added by {adder}",
            "", "*Shares:*",
        ]
        for uid, sh in shares.items():
            lines.append(f"• {groups.member_name(gid, uid)}: ₹{sh:,.2f}")
        rows = []
        if groups.can_modify(exp, gid, user_id):
            rows.append([
                InlineKeyboardButton("✏️ Edit", callback_data=f"gedit_{exp_id}"),
                InlineKeyboardButton("🗑 Delete", callback_data=f"gdel_{exp_id}"),
            ])
        rows.append([InlineKeyboardButton("⬅ Back", callback_data=f"ghistory_{gid}")])
        await query.message.reply_text("\n".join(lines), parse_mode="Markdown",
                                       reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("gedit_"):
        exp_id = int(data.split("_")[1])
        exp = groups.get_expense(exp_id)
        if not exp or not groups.can_modify(exp, exp["group_id"], user_id):
            await query.message.reply_text("⚠️ Only the creator or owner can edit this.")
            return
        user_state[user_id] = {"flow": "group", "step": "g_edit_amount",
                               "expense_id": exp_id, "group_id": exp["group_id"]}
        await query.message.reply_text(
            f"✏️ Current amount: ₹{exp['amount']:,.2f}\n"
            "Send the *new amount*, or `skip` to keep it:",
            parse_mode="Markdown")
        return

    if data.startswith("gdel_"):
        exp_id = int(data.split("_")[1])
        exp = groups.get_expense(exp_id)
        if not exp or not groups.can_modify(exp, exp["group_id"], user_id):
            await query.message.reply_text("⚠️ Only the creator or owner can delete this.")
            return
        user_state[user_id] = {"flow": "group", "step": "g_del_reason",
                               "expense_id": exp_id, "group_id": exp["group_id"]}
        await query.message.reply_text("🗑 Send a *reason* for deleting this entry:",
                                       parse_mode="Markdown")
        return

    # ---------------- ADD EXPENSE FLOW ----------------
    if data.startswith("gexp_"):
        gid = int(data.split("_")[1])
        if not groups.is_active_member(gid, user_id):
            return
        user_state[user_id] = {"flow": "group", "step": "g_amount", "group_id": gid}
        await query.message.reply_text("💰 Enter the expense amount:")
        return

    if data.startswith("gpay_"):
        state = user_state.get(user_id)
        if not state or state.get("step") != "g_payer":
            return
        state["payer_id"] = int(data.split("_")[1])
        state["step"] = "g_split"
        await query.message.reply_text("🔀 How should this be split?",
                                       reply_markup=split_keyboard())
        return

    if data.startswith("gsplit_"):
        state = user_state.get(user_id)
        if not state or state.get("step") != "g_split":
            return
        gid = state["group_id"]
        members = groups.active_members(gid)
        kind = data.split("_")[1]
        if kind == "equal":
            shares = groups.compute_shares(
                "equal", state["amount"], [m["user_id"] for m in members])
            await finalize_group_expense(context, query.message, user_id, state,
                                         "equal", shares)
            return
        if kind == "selected":
            state["step"] = "g_select"
            state["selected"] = set()
            await query.message.reply_text(
                "👤 Tap everyone this expense is shared between, then ✔ Done:",
                reply_markup=members_select_keyboard(members, state["selected"]))
            return
        if kind == "percentage":
            state["step"] = "g_percent"
            state["pct_members"] = members
            lines = ["％ Send percentages in this order, comma-separated (must total 100):", ""]
            for i, m in enumerate(members, 1):
                lines.append(f"{i}. {m['name']}")
            lines.append("\nExample: `40, 35, 25`")
            await query.message.reply_text("\n".join(lines), parse_mode="Markdown")
            return

    if data.startswith("gsel_"):
        state = user_state.get(user_id)
        if not state or state.get("step") != "g_select":
            return
        gid = state["group_id"]
        members = groups.active_members(gid)
        tail = data.split("_", 1)[1]
        if tail == "done":
            if not state["selected"]:
                await query.answer("Select at least one person.", show_alert=True)
                return
            shares = groups.compute_shares(
                "selected", state["amount"], [m["user_id"] for m in members],
                selected_ids=state["selected"])
            await finalize_group_expense(context, query.message, user_id, state,
                                         "selected", shares)
            return
        uid = int(tail)
        if uid in state["selected"]:
            state["selected"].discard(uid)
        else:
            state["selected"].add(uid)
        await query.edit_message_reply_markup(
            reply_markup=members_select_keyboard(members, state["selected"]))
        return

    # ---------------- GROUP REPORT ----------------
    if data.startswith("greport_"):
        gid = int(data.split("_")[1])
        if not groups.is_active_member(gid, user_id):
            return
        group_ctx[user_id] = gid
        await query.message.reply_text("📥 Choose report format:",
                                       reply_markup=report_kind_keyboard(gid))
        return

    if data in ("greportpdf", "greportimg"):
        gid = group_ctx.get(user_id)
        if not gid:
            return
        g = groups.get_group(gid)
        s = groups.settlement(gid)
        safe = g["name"].replace(" ", "_").replace("/", "-")
        if data == "greportpdf":
            buf = reports.build_group_pdf(g["name"], s)
            await query.message.reply_document(
                InputFile(buf, filename=f"Balanzo_{safe}.pdf"),
                caption=f"📄 {g['name']} — settlement")
        else:
            buf = reports.build_group_image(g["name"], s)
            await query.message.reply_photo(
                InputFile(buf, filename=f"Balanzo_{safe}.png"),
                caption=f"🖼 {g['name']} — settlement")
        return


# -------------------------
# TEXT HANDLER
# -------------------------
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    state = user_state.get(user_id)

    if not state:
        lower = text.lower()

        # ---------------- GREETING ----------------
        if lower in ("hi", "hello", "hey", "hii", "hiya"):
            await update.message.reply_text(
                "👋 Hello! Welcome to Balanzo — your personal finance buddy.\n"
                "Tap a button below to get started 👇",
                reply_markup=home(),
            )
            return

        # ---------------- STATUS CHECK ----------------
        if lower in ("balanzo", "check"):
            await update.message.reply_text("⚡ All systems go! Balanzo is online ✅")
            return
        if lower in ("owner"):
            await update.message.reply_text(
                "👤 Owner: @vishnuedappatt\n"
            )
            return

        # ---------------- FALLBACK: show main menu ----------------
        await update.message.reply_text("🏠 Main Menu:", reply_markup=home())
        return

    # ---------------- GROUP TEXT FLOWS ----------------
    if state.get("flow") == "group":
        await handle_group_text(update, context, user_id, text, state)
        return

    # ---------------- AMOUNT ----------------
    if state["step"] == "amount":
        try:
            amount = float(text)
            if amount <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Enter a valid amount (> 0)")
            return
        state["amount"] = amount
        state["step"] = "category"
        await update.message.reply_text(
            "📌 Select category:",
            reply_markup=category_keyboard(state["type"]),
        )
        return

    # ---------------- PERSON (lent / borrow) ----------------
    if state["step"] == "person":
        person = text.strip()
        if not person:
            await update.message.reply_text("❌ Please enter a name.")
            return
        state["person"] = person
        state["step"] = "due_date"
        now = datetime.now()
        await update.message.reply_text(
            "📅 Pick the due date (or type it as `YYYY-MM-DD`):",
            parse_mode="Markdown",
            reply_markup=calendar_keyboard(now.year, now.month),
        )
        return

    # ---------------- DUE DATE (lent / borrow) ----------------
    if state["step"] == "due_date":
        try:
            due = datetime.strptime(text.strip(), "%Y-%m-%d").date()
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid format. Send the date as `YYYY-MM-DD` (e.g. 2026-07-15):",
                parse_mode="Markdown",
            )
            return
        if due < datetime.now().date():
            await update.message.reply_text(
                "❌ The due date is in the past. Send a today-or-future date "
                "as `YYYY-MM-DD`:", parse_mode="Markdown")
            return
        state["due_date"] = due.strftime("%Y-%m-%d")
        state["step"] = "note"
        await update.message.reply_text(
            "📝 Add a note for this entry, or tap Skip:",
            reply_markup=note_keyboard(),
        )
        return

    # ---------------- LOAN REPAYMENT ----------------
    if state["step"] == "loan_payment":
        loan_id = state["loan_id"]
        loan = get_loan(loan_id)
        if not loan or loan["user_id"] != user_id or loan["status"] != "open":
            user_state.pop(user_id, None)
            await update.message.reply_text("That loan is no longer open.",
                                            reply_markup=home())
            return
        try:
            amount = float(text)
            if amount <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Enter a valid amount (> 0)")
            return
        outstanding = loan_outstanding(loan_id)
        if amount > outstanding + 0.01:
            await update.message.reply_text(
                f"❌ That's more than the ₹{outstanding:,.2f} outstanding. "
                f"Enter an amount up to ₹{outstanding:,.2f}:")
            return
        remaining = record_payment(loan_id, amount)
        user_state.pop(user_id, None)
        verb = "received from" if loan["direction"] == "lent" else "paid to"
        if remaining <= 0:
            await update.message.reply_text(
                f"✅ ₹{amount:,.2f} {verb} {loan['person']} recorded.\n"
                f"🎉 This loan is now fully settled!",
                reply_markup=home())
        else:
            await update.message.reply_text(
                f"✅ ₹{amount:,.2f} {verb} {loan['person']} recorded.\n"
                f"Remaining: ₹{remaining:,.2f}",
                reply_markup=home())
        return

    # ---------------- NOTE ----------------
    if state["step"] == "note":
        note = "" if text.lower() == "skip" else text
        await save_transaction(update.message, user_id, state, note)
        return

    # ---------------- CUSTOM DATE ----------------
    if state["step"] == "custom_date":
        rng = parse_custom_range(text)
        if not rng:
            await update.message.reply_text(
                "❌ Invalid format. Use `YYYY-MM-DD to YYYY-MM-DD` or `YYYY-MM-DD`",
                parse_mode="Markdown",
            )
            return
        start, end, label = rng
        summary_ctx[user_id] = {"start": start, "end": end, "label": label}
        user_state.pop(user_id, None)
        await update.message.reply_text(
            f"📊 {label} — choose a view:", reply_markup=view_keyboard()
        )
        return


def parse_custom_range(text):
    fmt_in = "%Y-%m-%d"
    fmt_out = "%Y-%m-%d %H:%M:%S"
    try:
        if "to" in text:
            a, b = [p.strip() for p in text.split("to", 1)]
            d1 = datetime.strptime(a, fmt_in)
            d2 = datetime.strptime(b, fmt_in)
        else:
            d1 = d2 = datetime.strptime(text.strip(), fmt_in)
        start = d1.replace(hour=0, minute=0, second=0)
        end = d2.replace(hour=23, minute=59, second=59)
        label = (start.strftime(fmt_in) if start.date() == end.date()
                 else f"{start.strftime(fmt_in)} to {end.strftime(fmt_in)}")
        return start.strftime(fmt_out), end.strftime(fmt_out), label
    except ValueError:
        return None


# -------------------------
# SAVE
# -------------------------
async def save_transaction(message, user_id, state, note):
    # store created_at in LOCAL time so it matches date_range() filtering
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT INTO transactions(user_id, type, amount, category, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, state["type"], state["amount"], state["category"], note, now_str))
    conn.commit()

    # Lent / Borrow also get a tracked loan row (who + due date + repayments)
    if state["type"] in ("lent", "borrow"):
        cursor.execute("""
            INSERT INTO loans(user_id, direction, person, amount, category,
                              note, due_date, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
        """, (user_id, state["type"], state["person"], state["amount"],
              state["category"], note, state["due_date"], now_str))
        conn.commit()

    user_state.pop(user_id, None)

    emoji_label, _ = TYPE_LABEL[state["type"]]
    extra = ""
    if state["type"] in ("lent", "borrow"):
        who = "To" if state["type"] == "lent" else "From"
        extra = (f"👤 {who}: {state['person']}\n"
                 f"📅 Due: {state['due_date']}\n")
    await message.reply_text(
        f"✅ Saved!\n\n"
        f"{emoji_label}\n"
        f"💰 Amount: ₹{state['amount']:,.2f}\n"
        f"🏷 Category: {state['category']}\n"
        f"{extra}"
        f"📝 Note: {note if note else 'None'}\n"
        f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        reply_markup=home(),
    )


# -------------------------
# PENDING LOANS: display
# -------------------------
async def show_pending(message, user_id):
    loans = open_loans(user_id)
    if not loans:
        await message.reply_text(
            "📋 *Pending Loans*\n\nNothing outstanding — you're all settled! 🎉",
            parse_mode="Markdown", reply_markup=home())
        return

    today = datetime.now().date()
    lent_lines, borrow_lines = [], []
    for ln in loans:
        due = datetime.strptime(ln["due_date"], "%Y-%m-%d").date()
        flag = " ⚠️ overdue" if due < today else (" (due today)" if due == today else "")
        line = (f"• {ln['person']}: ₹{ln['outstanding']:,.2f} "
                f"of ₹{ln['amount']:,.2f} · due {ln['due_date']}{flag}")
        (lent_lines if ln["direction"] == "lent" else borrow_lines).append(line)

    parts = ["📋 *Pending Loans*", ""]
    if lent_lines:
        parts.append("🤝 *They owe you:*")
        parts += lent_lines
        parts.append("")
    if borrow_lines:
        parts.append("💳 *You owe them:*")
        parts += borrow_lines
        parts.append("")
    parts.append("Tap a loan below to record a repayment.")

    await message.reply_text("\n".join(parts), parse_mode="Markdown",
                             reply_markup=pending_keyboard(loans))


async def show_loan_detail(message, user_id, loan_id):
    loan = get_loan(loan_id)
    if not loan or loan["user_id"] != user_id:
        await message.reply_text("Loan not found.", reply_markup=home())
        return

    outstanding = loan_outstanding(loan_id)
    who = "To" if loan["direction"] == "lent" else "From"
    arrow = "🤝 Lent" if loan["direction"] == "lent" else "💳 Borrow"
    status = "✅ Settled" if loan["status"] != "open" else "🔓 Open"

    lines = [
        f"{arrow}",
        f"👤 {who}: {loan['person']}",
        f"💰 Original: ₹{loan['amount']:,.2f}",
        f"💵 Outstanding: ₹{outstanding:,.2f}",
        f"📅 Due: {loan['due_date']}",
        f"📌 Status: {status}",
        f"📝 Note: {loan['note'] if loan['note'] else 'None'}",
    ]

    history = loan_payment_history(loan_id)
    if history:
        lines.append("")
        lines.append("Repayments:")
        for amt, created in history:
            lines.append(f"• ₹{amt:,.2f} — {created[:16]}")

    if loan["status"] != "open":
        await message.reply_text("\n".join(lines), reply_markup=home())
    else:
        await message.reply_text("\n".join(lines),
                                 reply_markup=loan_detail_keyboard(loan_id))


# -------------------------
# HOURLY REMINDERS: due / overdue loans
# -------------------------
async def check_due_loans(context: ContextTypes.DEFAULT_TYPE):
    """Hourly sweep: nag about loans due today or overdue until acknowledged.

    A loan keeps pinging every hour, every day, while it's open and due. It goes
    quiet only when the user taps "I'm aware" (mutes for that day, set via
    reminded_on=today) or once the loan is settled (status != 'open').
    """
    today = datetime.now().strftime("%Y-%m-%d")
    cursor.execute("""
        SELECT id, user_id, direction, person, due_date
        FROM loans
        WHERE status='open' AND due_date <= ?
          AND (reminded_on IS NULL OR reminded_on != ?)
    """, (today, today))
    due = cursor.fetchall()

    for lid, uid, direction, person, due_date in due:
        outstanding = loan_outstanding(lid)
        if outstanding <= 0:
            continue
        overdue = due_date < today
        suffix = f" (overdue since {due_date})" if overdue else " today"
        if direction == "lent":
            text = (f"🔔 ₹{outstanding:,.2f} that {person} owes you "
                    f"is due back{suffix}.")
        else:
            text = (f"🔔 You need to repay ₹{outstanding:,.2f} to {person}{suffix}.")
        try:
            # Buttons let the user mute for the day or record a repayment.
            await context.bot.send_message(
                uid, text, reply_markup=reminder_keyboard(lid))
        except Exception as e:
            print(f"reminder send failed for loan {lid}: {e}")


# -------------------------
# GROUP: SAVE EXPENSE + TEXT FLOWS
# -------------------------
async def finalize_group_expense(context, message, user_id, state, split_type, shares):
    gid = state["group_id"]
    groups.add_expense(
        gid, state["payer_id"], state["amount"], state["description"],
        "general", split_type, shares, user_id)
    user_state.pop(user_id, None)

    g = groups.get_group(gid)
    payer = groups.member_name(gid, state["payer_id"])
    adder = groups.member_name(gid, user_id)

    lines = [f"✅ *Expense added to {g['name']}*",
             f"🧾 {state['description']} — ₹{state['amount']:,.2f}",
             f"💳 Paid by {payer} · split {split_type}", "", "Shares:"]
    for uid, sh in shares.items():
        lines.append(f"• {groups.member_name(gid, uid)}: ₹{sh:,.2f}")
    await message.reply_text("\n".join(lines), parse_mode="Markdown",
                             reply_markup=back_to_group_keyboard(gid))

    await notify_group(
        context, gid,
        f"➕ {adder} added an expense in {g['name']}:\n"
        f"🧾 {state['description']} — ₹{state['amount']:,.2f} (paid by {payer})",
        exclude_id=user_id)


async def handle_group_text(update, context, user_id, text, state):
    step = state["step"]
    msg = update.message

    # ----- CREATE GROUP -----
    if step == "g_name":
        u = update.effective_user
        gid, code = groups.create_group(
            user_id, text, u.full_name, u.username)
        user_state.pop(user_id, None)
        group_ctx[user_id] = gid
        await msg.reply_text(
            f"✅ Group *{text}* created!\n\n🔑 Join code: `{code}`\n\n"
            "Share this code so others can join (you'll approve each request).",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔑 Copy Join Code", callback_data=f"gcode_{gid}")],
                [InlineKeyboardButton("🗑 Delete Group", callback_data=f"gdelete_{gid}")],
                [InlineKeyboardButton("🏠 Home", callback_data="home")]
            ]))
        return

    # ----- JOIN GROUP -----
    if step == "g_code":
        g = groups.get_group_by_code(text)
        user_state.pop(user_id, None)
        if not g:
            await msg.reply_text("❌ No group found with that code.",
                                 reply_markup=groups_menu())
            return
        u = update.effective_user
        result = groups.request_join(g["id"], user_id, u.full_name, u.username)
        if result == "already":
            await msg.reply_text("✅ You're already a member.",
                                 reply_markup=back_to_group_keyboard(g["id"]))
            return
        await msg.reply_text(
            f"⏳ Request sent to join *{g['name']}*. Waiting for owner approval.",
            parse_mode="Markdown", reply_markup=groups_menu())
        try:
            await context.bot.send_message(
                chat_id=g["owner_id"],
                text=f"🙋 {u.full_name} requested to join *{g['name']}*.",
                parse_mode="Markdown",
                reply_markup=approve_keyboard(
                    groups.get_membership(g["id"], user_id)["row_id"]))
        except Exception:
            pass
        return

    # ----- ADD EXPENSE: amount -----
    if step == "g_amount":
        try:
            amount = float(text)
            if amount <= 0:
                raise ValueError
        except ValueError:
            await msg.reply_text("❌ Enter a valid amount (> 0)")
            return
        state["amount"] = amount
        state["step"] = "g_desc"
        await msg.reply_text("📝 What was it for? (description)")
        return

    # ----- ADD EXPENSE: description -----
    if step == "g_desc":
        state["description"] = text
        state["step"] = "g_payer"
        members = groups.active_members(state["group_id"])
        await msg.reply_text("💳 Who paid?", reply_markup=payer_keyboard(members))
        return

    # ----- ADD EXPENSE: percentage -----
    if step == "g_percent":
        members = state.get("pct_members") or groups.active_members(state["group_id"])
        parts = [p.strip() for p in text.replace("%", "").split(",")]
        if len(parts) != len(members):
            await msg.reply_text(
                f"❌ Send exactly {len(members)} numbers, comma-separated.")
            return
        try:
            pcts = [float(p) for p in parts]
        except ValueError:
            await msg.reply_text("❌ Use numbers only, e.g. `40, 35, 25`",
                                 parse_mode="Markdown")
            return
        if abs(sum(pcts) - 100) > 0.01:
            await msg.reply_text(f"❌ Percentages must total 100 (got {sum(pcts):g}).")
            return
        percents = {members[i]["user_id"]: pcts[i] for i in range(len(members))}
        shares = groups.compute_shares(
            "percentage", state["amount"], [m["user_id"] for m in members],
            percents=percents)
        await finalize_group_expense(context, msg, user_id, state,
                                     "percentage", shares)
        return

    # ----- EDIT: amount -----
    if step == "g_edit_amount":
        if text.lower() != "skip":
            try:
                amount = float(text)
                if amount <= 0:
                    raise ValueError
            except ValueError:
                await msg.reply_text("❌ Enter a valid amount, or `skip`.",
                                     parse_mode="Markdown")
                return
            state["new_amount"] = amount
        exp = groups.get_expense(state["expense_id"])
        state["step"] = "g_edit_desc"
        await msg.reply_text(
            f'✏️ Current note: "{exp["description"]}"\n'
            "Send a *new description*, or `skip` to keep it:",
            parse_mode="Markdown")
        return

    # ----- EDIT: description -----
    if step == "g_edit_desc":
        if text.lower() != "skip":
            state["new_desc"] = text
        state["step"] = "g_reason"
        await msg.reply_text("📝 Why are you changing this? (reason — required)")
        return

    # ----- EDIT: reason -> apply -----
    if step == "g_reason":
        exp_id = state["expense_id"]
        gid = state["group_id"]
        exp = groups.get_expense(exp_id)
        new_amount = state.get("new_amount")
        new_desc = state.get("new_desc")

        shares = None
        if new_amount is not None and exp and exp["amount"]:
            # scale existing shares proportionally to the new amount
            old_shares = groups.get_shares(exp_id)
            factor = new_amount / exp["amount"]
            shares = {uid: round(sh * factor, 2) for uid, sh in old_shares.items()}
            drift = round(new_amount - sum(shares.values()), 2)
            if drift and shares:
                first = next(iter(shares))
                shares[first] = round(shares[first] + drift, 2)

        result = groups.edit_expense(
            exp_id, user_id, amount=new_amount, description=new_desc,
            shares=shares, reason=text)
        user_state.pop(user_id, None)
        actor = groups.member_name(gid, user_id)
        g = groups.get_group(gid)
        await msg.reply_text("✅ Entry updated and team notified.",
                             reply_markup=back_to_group_keyboard(gid))
        await notify_group(
            context, gid,
            f"✏️ {actor} edited an entry in {g['name']}:\n"
            f"{result['details']}\n📝 Reason: {text}",
            exclude_id=user_id)
        return

    # ----- DELETE: reason -> apply -----
    if step == "g_del_reason":
        exp_id = state["expense_id"]
        gid = state["group_id"]
        exp = groups.delete_expense(exp_id, user_id, reason=text)
        user_state.pop(user_id, None)
        actor = groups.member_name(gid, user_id)
        g = groups.get_group(gid)
        await msg.reply_text("🗑 Entry deleted and team notified.",
                             reply_markup=back_to_group_keyboard(gid))
        if exp:
            await notify_group(
                context, gid,
                f"🗑 {actor} deleted an entry in {g['name']}:\n"
                f'"{exp["description"]}" ₹{exp["amount"]:,.2f}\n📝 Reason: {text}',
                exclude_id=user_id)
        return


# -------------------------
# SUMMARY VIEWS
# -------------------------
async def send_view(query, user_id, view):
    ctx = summary_ctx.get(user_id)
    if not ctx:
        await query.message.reply_text(
            "Pick a date range first.", reply_markup=range_keyboard()
        )
        return

    totals = get_totals(user_id, ctx["start"], ctx["end"])
    label = ctx["label"]
    # Net out repayments: lent/borrow count only what's still outstanding, so a
    # fully-returned loan nets to zero in the balance.
    stmt = loan_statement(user_id, ctx["start"], ctx["end"])
    net_totals = dict(totals)
    net_totals["lent"] = stmt["lent"]["outstanding"]
    net_totals["borrow"] = stmt["borrow"]["outstanding"]
    balance = reports.cash_in_hand(net_totals)

    if view == "view_balance":
        lent_ret = stmt["lent"]["returned"]
        bor_ret = stmt["borrow"]["returned"]
        lent_note = f"  _(₹{lent_ret:,.2f} returned)_" if lent_ret > 0 else ""
        bor_note = f"  _(₹{bor_ret:,.2f} repaid)_" if bor_ret > 0 else ""
        msg = (
            f"💵 *Balance Check* — {label}\n\n"
            f"💰 Income: ₹{net_totals['income']:,.2f}\n"
            f"💸 Expense: ₹{net_totals['expense']:,.2f}\n"
            f"🤝 Lent outstanding: ₹{net_totals['lent']:,.2f}{lent_note}\n"
            f"💳 Borrow outstanding: ₹{net_totals['borrow']:,.2f}{bor_note}\n\n"
            f"🧮 *Cash in hand: ₹{balance:,.2f}*"
        )
        await query.message.reply_text(msg, parse_mode="Markdown",
                                        reply_markup=view_keyboard())
        return

    if view == "view_short":
        msg = (
            f"📝 *Short Summary* — {label}\n\n"
            f"💰 Total Income: ₹{totals['income']:,.2f}\n"
            f"💸 Total Expense: ₹{totals['expense']:,.2f}\n"
            f"📉 Balance: ₹{balance:,.2f}"
        )
        await query.message.reply_text(msg, parse_mode="Markdown",
                                        reply_markup=view_keyboard())
        return

    # ---------------- LOANS DETAIL ----------------
    if view == "view_loans":
        loans = loans_in_range(user_id, ctx["start"], ctx["end"])
        lent = [l for l in loans if l["direction"] == "lent"]
        borrow = [l for l in loans if l["direction"] == "borrow"]

        def _loan_line(l):
            if l["status"] != "open":
                tail = "✅ settled"
            else:
                got = f" · got back ₹{l['returned']:,.2f}" if l["returned"] > 0 else ""
                tail = f"₹{l['outstanding']:,.2f} left{got} · due {l['due_date']}"
            return f"• {l['person']}: of ₹{l['amount']:,.2f} → {tail}"

        parts = [f"🤝 *Loans Detail* — {label}", ""]
        if not loans:
            parts.append("No lent/borrow entries in this range.")
        if lent:
            owed_to_you = sum(l["outstanding"] for l in lent)
            parts.append("🤝 *They owe you*")
            parts += [_loan_line(l) for l in lent]
            parts.append(f"_Still owed to you: ₹{owed_to_you:,.2f}_")
            parts.append("")
        if borrow:
            you_owe = sum(l["outstanding"] for l in borrow)
            parts.append("💳 *You owe them*")
            parts += [_loan_line(l) for l in borrow]
            parts.append(f"_You still owe: ₹{you_owe:,.2f}_")
            parts.append("")
        if loans:
            parts.append("💵 Got money back? Open 📋 Pending Loans → tap the "
                         "person → Record Repayment.")

        await query.message.reply_text(
            "\n".join(parts), parse_mode="Markdown", reply_markup=view_keyboard())
        return

    breakdowns = get_breakdowns(user_id, ctx["start"], ctx["end"])

    # ---------------- EXPENSE BY CATEGORY ----------------
    if view == "view_expcat":
        rows = breakdowns["expense"]
        total = totals["expense"]
        header = f"💸 *Expense by Category* — {label}\n"
        if not rows or total == 0:
            await query.message.reply_text(
                header + "\nNo expenses in this range.",
                parse_mode="Markdown", reply_markup=view_keyboard())
            return
        lines = []
        for cat, amt in rows:
            pct = amt / total * 100
            bar = "█" * max(1, round(pct / 10))
            lines.append(_row(cat, amt) + f"\n   {bar} {pct:.0f}%")
        body = "\n".join(lines)
        msg = (header + f"<pre>{body}</pre>\n"
               f"<b>Total expense: {RUPEE} {total:,.2f}</b>")
        await query.message.reply_text(
            msg, parse_mode="HTML", reply_markup=view_keyboard())
        return

    # ---------------- DETAILED ----------------
    lines = []
    # Income / Expense: category breakdown as before
    for key in ("income", "expense"):
        lines.append(_row(key.upper(), totals[key]))
        for cat, amt in breakdowns[key]:
            lines.append(_row(cat, amt, indent=3))
        lines.append("")

    # Lent / Borrow: a proper statement — each loan, its returns (with dates),
    # and the net still outstanding.
    verb = {"lent": "returned", "borrow": "repaid"}
    for key in ("lent", "borrow"):
        section = stmt[key]
        lines.append(_row(f"{key.upper()} (outstanding)", section["outstanding"]))
        for l in section["loans"]:
            tag = f"{l['person']} · due {l['due_date']}"
            lines.append(_row(tag, l["amount"], indent=3))
            for amt, dt in l["payments"]:
                lines.append(_row(f"{verb[key]} {dt[:10]}", -amt, indent=6))
            if l["status"] != "open":
                lines.append(" " * 6 + "= settled ✓")
            elif l["returned"] > 0:
                lines.append(_row("= still out", l["outstanding"], indent=6))
        if section["untracked"] > 0:
            lines.append(_row("other (untracked)", section["untracked"], indent=3))
        lines.append("")

    lines.append(_row("Cash in hand", balance))
    body = "\n".join(lines)
    msg = f"📋 <b>Detailed Summary — {label}</b>\n<pre>{body}</pre>"
    await query.message.reply_text(
        msg, parse_mode="HTML", reply_markup=view_keyboard()
    )


# -------------------------
# REPORT DOWNLOAD
# -------------------------
async def send_report(query, user_id, kind):
    ctx = summary_ctx.get(user_id)
    if not ctx:
        await query.message.reply_text(
            "Pick a date range first.", reply_markup=range_keyboard()
        )
        return

    totals = get_totals(user_id, ctx["start"], ctx["end"])
    label = ctx["label"]
    now = datetime.now()
    generated = now.strftime("%Y-%m-%d %H:%M")
    stamp = now.strftime("%Y-%m-%d_%H%M")
    safe = label.replace(" ", "_").replace("→", "to").replace("/", "-")
    fname = f"Balanzo_{safe}_{stamp}"

    # Net out repayments so the report matches the bot's Balance Check.
    stmt = loan_statement(user_id, ctx["start"], ctx["end"])
    totals = dict(totals)
    totals["lent"] = stmt["lent"]["outstanding"]
    totals["borrow"] = stmt["borrow"]["outstanding"]

    # Dated line items for every entry (income/expense/lent/borrow + repayments).
    entries = report_entries(user_id, ctx["start"], ctx["end"])

    is_pdf = kind == "report_pdf"

    # Loading indicator while we render + upload (this can take a few seconds).
    loading = await query.message.reply_text("⏳ Building your report…")
    try:
        await query.message.chat.send_action(
            ChatAction.UPLOAD_DOCUMENT if is_pdf else ChatAction.UPLOAD_PHOTO)
    except Exception:
        pass

    # Generous timeouts so large uploads don't trip the default ~5s write limit.
    timeouts = dict(read_timeout=60, write_timeout=120,
                    connect_timeout=30, pool_timeout=30)
    try:
        # Build off the event loop — reportlab/PIL are blocking and would
        # otherwise stall the bot (and its network heartbeats) while rendering.
        if is_pdf:
            buf = await asyncio.to_thread(
                reports.build_pdf, label, totals, entries, generated)
            await query.message.reply_document(
                InputFile(buf, filename=f"{fname}.pdf"),
                caption=f"📄 Balanzo report — {label}", **timeouts)
        else:
            buf = await asyncio.to_thread(
                reports.build_image, label, totals, entries, generated)
            await query.message.reply_photo(
                InputFile(buf, filename=f"{fname}.png"),
                caption=f"🖼 Balanzo report — {label}", **timeouts)
    except Exception as e:
        print(f"report send failed: {e}")
        await query.message.reply_text(
            "⚠ Couldn't deliver the report (it may have timed out). "
            "Please try again — if it keeps failing, pick a smaller date range.",
            reply_markup=view_keyboard())
    finally:
        try:
            await loading.delete()
        except Exception:
            pass


# -------------------------
# MAIN
# -------------------------
async def setup_bot_commands(app):
    """Set up the bot's command menu - only show start command."""
    commands = [
        BotCommand("start", "Start the bot"),
    ]
    await app.bot.set_my_commands(commands)


async def error_handler(update, context):
    """Catch any exception raised in a handler or job so the bot keeps running."""
    logger.error("Unhandled error while processing an update: %s",
                 context.error, exc_info=context.error)


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .defaults(Defaults(tzinfo=IST))   # schedule jobs in IST, not UTC
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(120)        # default uploads
        .media_write_timeout(120)  # file/photo uploads
        .pool_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    # Keeps a single bad update/job from bringing the whole bot down.
    app.add_error_handler(error_handler)

    # Hourly loan-due reminder. Each due/overdue loan pings every hour until the
    # user taps "I'm aware" (mutes for the day) or the loan is settled.
    if app.job_queue:
        app.job_queue.run_repeating(check_due_loans, interval=3600, first=60)
    else:
        print("⚠ JobQueue unavailable — install python-telegram-bot[job-queue] "
              "for loan reminders.")

    async def post_init(app):
        await setup_bot_commands(app)

    app.post_init = post_init

    print("Bot running...")
    # drop_pending_updates avoids replaying a backlog of taps after a restart.
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    # Self-heal: if polling ever crashes (e.g. network drop at startup), rebuild
    # and restart instead of exiting. Ctrl+C / SIGTERM still stop cleanly.
    while True:
        try:
            main()
            break
        except (KeyboardInterrupt, SystemExit):
            break
        except Exception as exc:
            logger.error("Bot crashed: %s — restarting in 5s…", exc, exc_info=exc)
            import time as _time
            _time.sleep(5)
