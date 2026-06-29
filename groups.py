"""Group / trip expense splitting: membership, expenses, audit trail, settlement.

Pure DB + math logic — no Telegram dependencies. Reuses the shared sqlite
connection from database.py.
"""
import secrets
import string
from datetime import datetime

from database import conn, cursor

_NOW_FMT = "%Y-%m-%d %H:%M:%S"
_CODE_ALPHABET = string.ascii_uppercase + string.digits


def _now():
    return datetime.now().strftime(_NOW_FMT)


# -------------------------
# GROUPS
# -------------------------
def gen_join_code(length=6):
    """A short unique join code (avoids ambiguous chars, retries on collision)."""
    alphabet = _CODE_ALPHABET.replace("O", "").replace("0", "").replace("I", "").replace("1", "")
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(length))
        cursor.execute("SELECT 1 FROM groups WHERE join_code=?", (code,))
        if not cursor.fetchone():
            return code


def create_group(owner_id, name, owner_name, owner_username):
    code = gen_join_code()
    cursor.execute(
        "INSERT INTO groups(name, owner_id, join_code, created_at) VALUES (?, ?, ?, ?)",
        (name, owner_id, code, _now()),
    )
    group_id = cursor.lastrowid
    cursor.execute(
        """INSERT INTO group_members(group_id, user_id, display_name, username, role, status, joined_at)
           VALUES (?, ?, ?, ?, 'owner', 'active', ?)""",
        (group_id, owner_id, owner_name, owner_username, _now()),
    )
    conn.commit()
    return group_id, code


def get_group(group_id):
    cursor.execute(
        "SELECT id, name, owner_id, join_code FROM groups WHERE id=?", (group_id,)
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "owner_id": row[2], "join_code": row[3]}


def get_group_by_code(code):
    cursor.execute(
        "SELECT id FROM groups WHERE join_code=?", (code.strip().upper(),)
    )
    row = cursor.fetchone()
    return get_group(row[0]) if row else None


def delete_group(group_id, user_id):
    """Delete a group and all its data. Only the owner can delete."""
    g = get_group(group_id)
    if not g or g["owner_id"] != user_id:
        return False
    
    # Delete expense shares
    cursor.execute(
        """DELETE FROM expense_shares 
           WHERE expense_id IN (SELECT id FROM group_expenses WHERE group_id=?)""",
        (group_id,)
    )
    # Delete expense audit
    cursor.execute("DELETE FROM expense_audit WHERE group_id=?", (group_id,))
    # Delete expenses
    cursor.execute("DELETE FROM group_expenses WHERE group_id=?", (group_id,))
    # Delete members
    cursor.execute("DELETE FROM group_members WHERE group_id=?", (group_id,))
    # Delete group
    cursor.execute("DELETE FROM groups WHERE id=?", (group_id,))
    conn.commit()
    return True


# -------------------------
# MEMBERSHIP
# -------------------------
def get_membership(group_id, user_id):
    cursor.execute(
        """SELECT id, role, status FROM group_members
           WHERE group_id=? AND user_id=?""",
        (group_id, user_id),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {"row_id": row[0], "role": row[1], "status": row[2]}


def request_join(group_id, user_id, display_name, username):
    """Returns ('active'|'pending'|'already'). Re-requests reset a prior rejection."""
    existing = get_membership(group_id, user_id)
    if existing:
        if existing["status"] == "active":
            return "already"
        if existing["status"] == "pending":
            return "pending"
        # rejected -> allow re-request
        cursor.execute(
            "UPDATE group_members SET status='pending', joined_at=? WHERE id=?",
            (_now(), existing["row_id"]),
        )
        conn.commit()
        return "pending"
    cursor.execute(
        """INSERT INTO group_members(group_id, user_id, display_name, username, role, status, joined_at)
           VALUES (?, ?, ?, ?, 'member', 'pending', ?)""",
        (group_id, user_id, display_name, username, _now()),
    )
    conn.commit()
    return "pending"


def get_member_row(row_id):
    cursor.execute(
        """SELECT id, group_id, user_id, display_name, status
           FROM group_members WHERE id=?""",
        (row_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {"row_id": row[0], "group_id": row[1], "user_id": row[2],
            "display_name": row[3], "status": row[4]}


def approve_member(row_id):
    cursor.execute("UPDATE group_members SET status='active' WHERE id=?", (row_id,))
    conn.commit()


def reject_member(row_id):
    cursor.execute("UPDATE group_members SET status='rejected' WHERE id=?", (row_id,))
    conn.commit()


def active_members(group_id):
    """[{user_id, name}] for active members, owner first."""
    cursor.execute(
        """SELECT user_id, display_name, role FROM group_members
           WHERE group_id=? AND status='active'
           ORDER BY (role='owner') DESC, joined_at ASC""",
        (group_id,),
    )
    return [{"user_id": r[0], "name": r[1] or "Member", "role": r[2]}
            for r in cursor.fetchall()]


def pending_members(group_id):
    cursor.execute(
        """SELECT id, user_id, display_name, username FROM group_members
           WHERE group_id=? AND status='pending' ORDER BY joined_at ASC""",
        (group_id,),
    )
    return [{"row_id": r[0], "user_id": r[1], "name": r[2] or "Member",
             "username": r[3]} for r in cursor.fetchall()]


def member_name(group_id, user_id):
    cursor.execute(
        "SELECT display_name FROM group_members WHERE group_id=? AND user_id=?",
        (group_id, user_id),
    )
    row = cursor.fetchone()
    return (row[0] if row and row[0] else "Member")


def user_groups(user_id):
    """Groups where this user is an active member: [{id, name, owner_id}]."""
    cursor.execute(
        """SELECT g.id, g.name, g.owner_id
           FROM groups g JOIN group_members m ON m.group_id = g.id
           WHERE m.user_id=? AND m.status='active'
           ORDER BY g.created_at DESC""",
        (user_id,),
    )
    return [{"id": r[0], "name": r[1], "owner_id": r[2]} for r in cursor.fetchall()]


def is_owner(group_id, user_id):
    g = get_group(group_id)
    return bool(g and g["owner_id"] == user_id)


def is_active_member(group_id, user_id):
    m = get_membership(group_id, user_id)
    return bool(m and m["status"] == "active")


# -------------------------
# SHARE MATH
# -------------------------
def compute_shares(split_type, amount, member_ids, selected_ids=None, percents=None):
    """Return {user_id: share}. Rounding remainder is absorbed by the first member
    so the shares always sum exactly to `amount`."""
    amount = round(float(amount), 2)

    if split_type == "selected":
        ids = list(selected_ids or [])
    elif split_type == "percentage":
        ids = list((percents or {}).keys())
    else:  # equal
        ids = list(member_ids)

    if not ids:
        return {}

    shares = {}
    if split_type == "percentage":
        for uid in ids:
            shares[uid] = round(amount * float(percents[uid]) / 100.0, 2)
    else:
        each = round(amount / len(ids), 2)
        for uid in ids:
            shares[uid] = each

    # absorb rounding drift into the first member
    drift = round(amount - sum(shares.values()), 2)
    if drift and ids:
        shares[ids[0]] = round(shares[ids[0]] + drift, 2)
    return shares


# -------------------------
# EXPENSES
# -------------------------
def add_expense(group_id, payer_id, amount, description, category, split_type,
                shares, created_by):
    now = _now()
    cursor.execute(
        """INSERT INTO group_expenses(group_id, payer_id, amount, description, category,
                                      split_type, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (group_id, payer_id, round(float(amount), 2), description, category,
         split_type, created_by, now, now),
    )
    expense_id = cursor.lastrowid
    for uid, share in shares.items():
        cursor.execute(
            "INSERT INTO expense_shares(expense_id, user_id, share) VALUES (?, ?, ?)",
            (expense_id, uid, share),
        )
    cursor.execute(
        """INSERT INTO expense_audit(group_id, expense_id, actor_id, action, reason, details, created_at)
           VALUES (?, ?, ?, 'create', '', ?, ?)""",
        (group_id, expense_id, created_by,
         f'Added "{description}" ₹{amount:,.2f} ({split_type})', now),
    )
    conn.commit()
    return expense_id


def get_expense(expense_id):
    cursor.execute(
        """SELECT id, group_id, payer_id, amount, description, category, split_type,
                  created_by, created_at FROM group_expenses WHERE id=?""",
        (expense_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {"id": row[0], "group_id": row[1], "payer_id": row[2], "amount": row[3],
            "description": row[4], "category": row[5], "split_type": row[6],
            "created_by": row[7], "created_at": row[8]}


def get_shares(expense_id):
    """{user_id: share} for an expense."""
    cursor.execute(
        "SELECT user_id, share FROM expense_shares WHERE expense_id=?", (expense_id,)
    )
    return {r[0]: r[1] for r in cursor.fetchall()}


def list_expenses(group_id, limit=25):
    cursor.execute(
        """SELECT id, payer_id, amount, description, split_type, created_at
           FROM group_expenses WHERE group_id=?
           ORDER BY created_at DESC, id DESC LIMIT ?""",
        (group_id, limit),
    )
    return [{"id": r[0], "payer_id": r[1], "amount": r[2], "description": r[3],
             "split_type": r[4], "created_at": r[5]} for r in cursor.fetchall()]


def can_modify(expense, group_id, user_id):
    """Only the entry's creator or the group owner may edit/delete."""
    if not expense:
        return False
    return expense["created_by"] == user_id or is_owner(group_id, user_id)


def edit_expense(expense_id, actor_id, *, amount=None, description=None,
                 split_type=None, shares=None, reason=""):
    """Update mutable fields and log an 'edit' audit row with a human diff."""
    exp = get_expense(expense_id)
    if not exp:
        return None
    changes = []
    new_amount = exp["amount"] if amount is None else round(float(amount), 2)
    new_desc = exp["description"] if description is None else description
    new_split = exp["split_type"] if split_type is None else split_type

    if amount is not None and new_amount != exp["amount"]:
        changes.append(f"amount ₹{exp['amount']:,.2f} → ₹{new_amount:,.2f}")
    if description is not None and new_desc != exp["description"]:
        changes.append(f'note "{exp["description"]}" → "{new_desc}"')
    if split_type is not None and new_split != exp["split_type"]:
        changes.append(f"split {exp['split_type']} → {new_split}")

    cursor.execute(
        """UPDATE group_expenses SET amount=?, description=?, split_type=?, updated_at=?
           WHERE id=?""",
        (new_amount, new_desc, new_split, _now(), expense_id),
    )
    if shares is not None:
        cursor.execute("DELETE FROM expense_shares WHERE expense_id=?", (expense_id,))
        for uid, share in shares.items():
            cursor.execute(
                "INSERT INTO expense_shares(expense_id, user_id, share) VALUES (?, ?, ?)",
                (expense_id, uid, share),
            )
    details = "; ".join(changes) if changes else "updated"
    cursor.execute(
        """INSERT INTO expense_audit(group_id, expense_id, actor_id, action, reason, details, created_at)
           VALUES (?, ?, ?, 'edit', ?, ?, ?)""",
        (exp["group_id"], expense_id, actor_id, reason, details, _now()),
    )
    conn.commit()
    return {"details": details, "old": exp,
            "new": {"amount": new_amount, "description": new_desc, "split_type": new_split}}


def delete_expense(expense_id, actor_id, reason=""):
    exp = get_expense(expense_id)
    if not exp:
        return None
    cursor.execute(
        """INSERT INTO expense_audit(group_id, expense_id, actor_id, action, reason, details, created_at)
           VALUES (?, ?, ?, 'delete', ?, ?, ?)""",
        (exp["group_id"], expense_id, actor_id, reason,
         f'Deleted "{exp["description"]}" ₹{exp["amount"]:,.2f}', _now()),
    )
    cursor.execute("DELETE FROM expense_shares WHERE expense_id=?", (expense_id,))
    cursor.execute("DELETE FROM group_expenses WHERE id=?", (expense_id,))
    conn.commit()
    return exp


def audit_log(group_id, limit=15):
    cursor.execute(
        """SELECT actor_id, action, reason, details, created_at
           FROM expense_audit WHERE group_id=?
           ORDER BY created_at DESC, id DESC LIMIT ?""",
        (group_id, limit),
    )
    return [{"actor_id": r[0], "action": r[1], "reason": r[2],
             "details": r[3], "created_at": r[4]} for r in cursor.fetchall()]


# -------------------------
# SETTLEMENT
# -------------------------
def settlement(group_id):
    """Compute per-member paid/owed/net and the minimized set of transfers.

    Returns {per_member: [{user_id, name, paid, owed, net}], transfers:
    [{from, from_name, to, to_name, amount}], total}.
    """
    members = active_members(group_id)
    names = {m["user_id"]: m["name"] for m in members}

    paid = {m["user_id"]: 0.0 for m in members}
    owed = {m["user_id"]: 0.0 for m in members}

    cursor.execute(
        "SELECT payer_id, COALESCE(SUM(amount), 0) FROM group_expenses WHERE group_id=? GROUP BY payer_id",
        (group_id,),
    )
    for uid, amt in cursor.fetchall():
        if uid in paid:
            paid[uid] = amt or 0.0

    cursor.execute(
        """SELECT s.user_id, COALESCE(SUM(s.share), 0)
           FROM expense_shares s JOIN group_expenses e ON e.id = s.expense_id
           WHERE e.group_id=? GROUP BY s.user_id""",
        (group_id,),
    )
    for uid, amt in cursor.fetchall():
        if uid in owed:
            owed[uid] = amt or 0.0

    per_member = []
    for m in members:
        uid = m["user_id"]
        per_member.append({
            "user_id": uid, "name": m["name"],
            "paid": round(paid[uid], 2), "owed": round(owed[uid], 2),
            "net": round(paid[uid] - owed[uid], 2),
        })

    transfers = _minimize_transfers(
        {p["user_id"]: p["net"] for p in per_member}, names
    )
    total = round(sum(paid.values()), 2)
    return {"per_member": per_member, "transfers": transfers, "total": total}


def _minimize_transfers(net, names):
    """Greedy min-cash-flow: match the biggest creditor with the biggest debtor."""
    creditors = sorted(((u, n) for u, n in net.items() if n > 0.005),
                       key=lambda x: x[1], reverse=True)
    debtors = sorted(((u, -n) for u, n in net.items() if n < -0.005),
                     key=lambda x: x[1], reverse=True)

    creditors = [list(c) for c in creditors]
    debtors = [list(d) for d in debtors]
    transfers = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        d_uid, d_amt = debtors[i]
        c_uid, c_amt = creditors[j]
        pay = round(min(d_amt, c_amt), 2)
        if pay > 0:
            transfers.append({
                "from": d_uid, "from_name": names.get(d_uid, "Member"),
                "to": c_uid, "to_name": names.get(c_uid, "Member"),
                "amount": pay,
            })
        debtors[i][1] = round(d_amt - pay, 2)
        creditors[j][1] = round(c_amt - pay, 2)
        if debtors[i][1] <= 0.005:
            i += 1
        if creditors[j][1] <= 0.005:
            j += 1
    return transfers
