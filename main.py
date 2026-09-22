import base64
import hashlib
import os
import secrets
import sys
import tkinter as tk
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from tkinter import filedialog, messagebox, ttk

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv()


GREEN, TEXT, MUTED, BG = "#1f8f5f", "#17212b", "#667085", "#f5f7fb"
CATEGORIES = ["Food", "Transport", "Bills", "Shopping", "Salary", "Investment", "Health", "Other"]


def amount(value, positive=True):
    try:
        value = Decimal(str(value).replace(",", "").strip()).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if positive and value <= 0:
            raise ValueError
        return value
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a valid positive amount")


def money(value, currency, symbols):
    return f"{symbols.get(currency, currency)} {Decimal(value):,.2f}"


def pin_hash(pin, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 240_000)
    return base64.b64encode(salt + digest).decode()


def check_pin(pin, stored):
    raw = base64.b64decode(stored.encode())
    return secrets.compare_digest(hashlib.pbkdf2_hmac("sha256", pin.encode(), raw[:16], 240_000), raw[16:])


class Store:
    def __init__(self):
        url = os.getenv("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL is missing. Add your PostgreSQL connection string to .env.")
        url = url.replace("postgresql+psycopg://", "postgresql://", 1)
        self.db = psycopg.connect(url, row_factory=dict_row)
        self.db.autocommit = False
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS currencies (code VARCHAR(3) PRIMARY KEY, name TEXT NOT NULL, symbol TEXT NOT NULL, rate_to_base NUMERIC(20,8) NOT NULL);
            CREATE TABLE IF NOT EXISTS accounts (id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, currency VARCHAR(3) NOT NULL REFERENCES currencies(code), balance NUMERIC(20,2) NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS transactions (id BIGSERIAL PRIMARY KEY, account_id BIGINT NOT NULL REFERENCES accounts(id), kind TEXT NOT NULL CHECK(kind IN ('Income','Expense')), amount NUMERIC(20,2) NOT NULL CHECK(amount > 0), currency VARCHAR(3) NOT NULL REFERENCES currencies(code), fx_rate NUMERIC(20,8) NOT NULL, base_amount NUMERIC(20,2) NOT NULL, category TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', happened_on DATE NOT NULL);
            CREATE TABLE IF NOT EXISTS budgets (id BIGSERIAL PRIMARY KEY, category TEXT NOT NULL, amount NUMERIC(20,2) NOT NULL, currency VARCHAR(3) NOT NULL REFERENCES currencies(code), month DATE NOT NULL);
            CREATE TABLE IF NOT EXISTS goals (id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, target NUMERIC(20,2) NOT NULL, saved NUMERIC(20,2) NOT NULL DEFAULT 0, currency VARCHAR(3) NOT NULL REFERENCES currencies(code), deadline DATE);
            CREATE TABLE IF NOT EXISTS bills (id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, amount NUMERIC(20,2) NOT NULL, currency VARCHAR(3) NOT NULL REFERENCES currencies(code), due_date DATE NOT NULL, paid BOOLEAN NOT NULL DEFAULT FALSE);
            CREATE TABLE IF NOT EXISTS assets (id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, value NUMERIC(20,2) NOT NULL, currency VARCHAR(3) NOT NULL REFERENCES currencies(code));
        """)
        self.db.execute("INSERT INTO app_settings(key,value) VALUES ('base_currency','IDR') ON CONFLICT DO NOTHING")
        for row in (("IDR", "Indonesian Rupiah", "Rp", "1"), ("USD", "US Dollar", "$", "16000"), ("EUR", "Euro", "€", "18000"), ("SGD", "Singapore Dollar", "S$", "12500")):
            self.db.execute("INSERT INTO currencies(code,name,symbol,rate_to_base) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", row)
        self.db.commit()

    def setting(self, key):
        row = self.db.execute("SELECT value FROM app_settings WHERE key=%s", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key, value):
        self.db.execute("INSERT INTO app_settings(key,value) VALUES (%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", (key, value))
        self.db.commit()

    def currencies(self):
        return self.db.execute("SELECT * FROM currencies ORDER BY code").fetchall()

    def rates(self):
        return {row["code"]: row for row in self.currencies()}

    def accounts(self):
        return self.db.execute("SELECT * FROM accounts ORDER BY name").fetchall()

    def account(self, account_id):
        return self.db.execute("SELECT * FROM accounts WHERE id=%s", (account_id,)).fetchone()

    def transactions(self, limit=None):
        query = "SELECT t.*, a.name account_name FROM transactions t JOIN accounts a ON a.id=t.account_id ORDER BY happened_on DESC, t.id DESC"
        return self.db.execute(query + (" LIMIT %s" if limit else ""), (limit,) if limit else ()).fetchall()

    def add_account(self, name, kind, currency, balance):
        with self.db.transaction():
            self.db.execute("INSERT INTO accounts(name,kind,currency,balance) VALUES (%s,%s,%s,%s)", (name, kind, currency, balance))

    def update_account(self, account_id, name, kind, currency, balance):
        with self.db.transaction():
            self.db.execute("UPDATE accounts SET name=%s, kind=%s, currency=%s, balance=%s WHERE id=%s", (name, kind, currency, balance, account_id))

    def record(self, table, record_id):
        return self.db.execute(f"SELECT * FROM {table} WHERE id=%s", (record_id,)).fetchone()

    def update_record(self, table, record_id, values):
        assignments = ",".join(f"{key}=%s" for key in values)
        with self.db.transaction():
            self.db.execute(f"UPDATE {table} SET {assignments} WHERE id=%s", (*values.values(), record_id))

    def update_transaction(self, transaction_id, account_id, kind, value, currency, category, note, happened_on):
        old = self.record("transactions", transaction_id)
        rates = self.rates()
        with self.db.transaction():
            old_account = self.account(old["account_id"])
            old_value = old["amount"] * old["fx_rate"] / rates[old_account["currency"]]["rate_to_base"]
            old_change = old_value if old["kind"] == "Income" else -old_value
            self.db.execute("UPDATE accounts SET balance=balance-%s WHERE id=%s", (old_change, old["account_id"]))
            new_account = self.account(account_id)
            fx_rate = rates[currency]["rate_to_base"]
            new_value = value * fx_rate / rates[new_account["currency"]]["rate_to_base"]
            new_change = new_value if kind == "Income" else -new_value
            self.db.execute("UPDATE accounts SET balance=balance+%s WHERE id=%s", (new_change, account_id))
            self.db.execute("UPDATE transactions SET account_id=%s,kind=%s,amount=%s,currency=%s,fx_rate=%s,base_amount=%s,category=%s,note=%s,happened_on=%s WHERE id=%s", (account_id, kind, value, currency, fx_rate, value * fx_rate, category, note, happened_on, transaction_id))

    def add_transaction(self, account_id, kind, value, currency, category, note, happened_on):
        rates = self.rates()
        base_value = value * rates[currency]["rate_to_base"]
        account = self.db.execute("SELECT currency FROM accounts WHERE id=%s", (account_id,)).fetchone()
        account_value = base_value / rates[account["currency"]]["rate_to_base"]
        with self.db.transaction():
            self.db.execute("INSERT INTO transactions(account_id,kind,amount,currency,fx_rate,base_amount,category,note,happened_on) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", (account_id, kind, value, currency, rates[currency]["rate_to_base"], base_value, category, note, happened_on))
            self.db.execute("UPDATE accounts SET balance=balance+%s WHERE id=%s", (account_value if kind == "Income" else -account_value, account_id))

    def add_simple(self, table, values):
        columns = ",".join(values)
        placeholders = ",".join(["%s"] * len(values))
        with self.db.transaction():
            self.db.execute(f"INSERT INTO {table}({columns}) VALUES ({placeholders})", tuple(values.values()))

    def summary(self):
        rates = self.rates()
        total = sum((row["balance"] * rates[row["currency"]]["rate_to_base"] for row in self.accounts()), Decimal(0))
        row = self.db.execute("SELECT COALESCE(SUM(base_amount) FILTER (WHERE kind='Income'),0) income, COALESCE(SUM(base_amount) FILTER (WHERE kind='Expense'),0) expenses FROM transactions").fetchone()
        return total, row["income"], row["expenses"]

    def close(self):
        self.db.close()


def touch_id():
    if sys.platform != "darwin":
        return False
    try:
        from LocalAuthentication import LAContext, LAPolicyDeviceOwnerAuthenticationWithBiometrics
        from threading import Event
        context = LAContext.alloc().init()
        policy = LAPolicyDeviceOwnerAuthenticationWithBiometrics
        can, _ = context.canEvaluatePolicy_error_(policy, None)
        if not can:
            return False
        result, done = [], Event()
        def reply(ok, error):
            result.append(ok)
            done.set()
        context.evaluatePolicy_localizedReason_reply_(policy, "Unlock Money Manager", reply)
        done.wait(30)
        return bool(result and result[0])
    except (ImportError, AttributeError):
        return False


class Auth(tk.Tk):
    def __init__(self, store):
        super().__init__()
        self.store, self.ok = store, False
        self.title("Unlock Money Manager")
        self.geometry("390x270")
        self.resizable(False, False)
        self.configure(bg="white")
        self.pin = tk.StringVar()
        first = not store.setting("pin_hash")
        tk.Label(self, text="Money Manager", bg="white", fg=TEXT, font=("Arial", 22, "bold")).pack(pady=(28, 8))
        tk.Label(self, text="Create a PIN" if first else "Enter your PIN", bg="white", fg=MUTED, font=("Arial", 11)).pack()
        entry = ttk.Entry(self, textvariable=self.pin, show="•", width=22)
        entry.pack(pady=16)
        entry.focus_set()
        ttk.Button(self, text="Save PIN" if first else "Unlock", command=lambda: self.submit(first), style="Accent.TButton").pack()
        if not first and sys.platform == "darwin":
            ttk.Button(self, text="Use Touch ID", command=self.biometric).pack(pady=8)
        self.bind("<Return>", lambda _: self.submit(first))

    def submit(self, first):
        value = self.pin.get()
        if not value.isdigit() or not 4 <= len(value) <= 8:
            return messagebox.showerror("Invalid PIN", "Use 4–8 digits.", parent=self)
        if first:
            self.store.set_setting("pin_hash", pin_hash(value))
        elif not check_pin(value, self.store.setting("pin_hash")):
            return messagebox.showerror("Access denied", "That PIN is incorrect.", parent=self)
        self.ok = True
        self.destroy()

    def biometric(self):
        if touch_id():
            self.ok = True
            self.destroy()
        else:
            messagebox.showinfo("Touch ID unavailable", "Install pyobjc-framework-LocalAuthentication and enable Touch ID, or use your PIN.", parent=self)


class Form(tk.Toplevel):
    def __init__(self, parent, title):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)
        self.body, self.row = ttk.Frame(self, padding=22), 0
        self.body.pack(fill="both", expand=True)

    def entry(self, label, default=""):
        ttk.Label(self.body, text=label).grid(row=self.row, column=0, sticky="w", pady=(0, 4))
        value = tk.StringVar(value=default)
        ttk.Entry(self.body, textvariable=value, width=34).grid(row=self.row + 1, column=0, sticky="ew", pady=(0, 12))
        self.row += 2
        return value

    def combo(self, label, values):
        ttk.Label(self.body, text=label).grid(row=self.row, column=0, sticky="w", pady=(0, 4))
        value = tk.StringVar(value=values[0])
        ttk.Combobox(self.body, textvariable=value, values=values, state="readonly", width=31).grid(row=self.row + 1, column=0, sticky="ew", pady=(0, 12))
        self.row += 2
        return value

    def save(self, command):
        ttk.Button(self.body, text="Save", command=command, style="Accent.TButton").grid(row=self.row, column=0, sticky="e")

    def error(self, message):
        messagebox.showerror("Check your input", message, parent=self)


class MoneyManager(tk.Tk):
    def __init__(self, store):
        super().__init__()
        self.store, self.rates = store, store.rates()
        self.base = store.setting("base_currency")
        self.symbols = {code: row["symbol"] for code, row in self.rates.items()}
        self.title("Money Manager")
        self.geometry("1180x760")
        self.minsize(950, 620)
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.styles()
        self.build()
        self.dashboard()

    def styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background="white")
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Arial", 11))
        style.configure("Title.TLabel", font=("Arial", 25, "bold"))
        style.configure("Muted.TLabel", foreground=MUTED, font=("Arial", 10))
        style.configure("Treeview", rowheight=30, font=("Arial", 10))
        style.configure("Treeview.Heading", font=("Arial", 10, "bold"))
        style.configure("TButton", foreground=TEXT, font=("Arial", 10, "bold"))
        style.configure("Accent.TButton", background=GREEN, foreground="white", padding=(14, 8), font=("Arial", 10, "bold"))
        style.map("TButton", foreground=[("disabled", "#98a2b3"), ("active", GREEN)])
        style.map("Accent.TButton", foreground=[("disabled", "#d0d5dd"), ("active", "white")])

    def build(self):
        side = tk.Frame(self, bg="#0f172a", width=270)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="Money Manager", bg="#0f172a", fg="#ffffff", font=("Arial", 18, "bold"), anchor="w").pack(fill="x", padx=24, pady=(28, 30))
        pages = (("Dashboard", self.dashboard), ("Accounts", self.accounts_page), ("Transactions", self.transactions_page), ("Budgets", self.budgets_page), ("Savings goals", self.goals_page), ("Bills", self.bills_page), ("Assets", self.assets_page), ("Reports", self.reports_page))
        for label, command in pages:
            tk.Button(side, text=label, command=command, anchor="w", relief="flat", bd=0, bg="#e2e8f0", fg="#0f172a", activebackground="#2563eb", activeforeground="#ffffff", font=("Arial", 11, "bold"), padx=24, pady=10).pack(fill="x", pady=2)
        tk.Label(side, text=f"Base currency: {self.base}\nPostgreSQL", bg="#0f172a", fg="#cbd5e1", justify="left", font=("Arial", 9)).pack(side="bottom", anchor="w", padx=24, pady=24)
        self.content = ttk.Frame(self, padding=32)
        self.content.pack(side="left", fill="both", expand=True)

    def clear(self):
        for child in self.content.winfo_children():
            child.destroy()

    def heading(self, title, subtitle, button=None):
        top = ttk.Frame(self.content)
        top.pack(fill="x", pady=(0, 26))
        ttk.Label(top, text=title, style="Title.TLabel").pack(side="left")
        if button and button[1]:
            ttk.Button(top, text=button[0], command=button[1], style="Accent.TButton").pack(side="right")
        ttk.Label(self.content, text=subtitle, style="Muted.TLabel").pack(anchor="w", pady=(0, 22))

    def card(self, parent, label, value, color=TEXT):
        frame = ttk.Frame(parent, style="Card.TFrame", padding=18)
        frame.pack(side="left", fill="both", expand=True, padx=(0, 14))
        tk.Label(frame, text=label.upper(), bg="white", fg=MUTED, font=("Arial", 9)).pack(anchor="w")
        tk.Label(frame, text=value, bg="white", fg=color, font=("Arial", 17, "bold")).pack(anchor="w", pady=(9, 0))

    def dashboard(self):
        self.clear(); self.heading("Dashboard", "Your financial overview", ("+ Add transaction", self.add_transaction))
        total, income, expenses = self.store.summary()
        cards = ttk.Frame(self.content); cards.pack(fill="x", pady=(0, 24))
        self.card(cards, "Net worth", money(total / self.rates[self.base]["rate_to_base"], self.base, self.symbols), GREEN)
        self.card(cards, "Income", money(income / self.rates[self.base]["rate_to_base"], self.base, self.symbols), GREEN)
        self.card(cards, "Expenses", money(expenses / self.rates[self.base]["rate_to_base"], self.base, self.symbols), "#c2413b")
        box = ttk.Frame(self.content, style="Card.TFrame", padding=18); box.pack(fill="both", expand=True)
        ttk.Label(box, text="Recent transactions", background="white", font=("Arial", 13, "bold")).pack(anchor="w", pady=(0, 10))
        self.transaction_table(box, self.store.transactions(10))

    def transaction_table(self, parent, rows):
        tree = ttk.Treeview(parent, columns=("date", "description", "account", "currency", "amount"), show="headings")
        for key, title, width in (("date", "Date", 95), ("description", "Description", 210), ("account", "Account", 140), ("currency", "Currency", 80), ("amount", "Amount", 130)):
            tree.heading(key, text=title); tree.column(key, width=width, anchor="e" if key == "amount" else "w")
        tree.tag_configure("income", foreground=GREEN); tree.tag_configure("expense", foreground="#c2413b")
        for row in rows:
            sign = "+" if row["kind"] == "Income" else "-"
            tree.insert("", "end", iid=str(row["id"]), values=(row["happened_on"], row["note"] or row["category"], row["account_name"], row["currency"], sign + money(row["amount"], row["currency"], self.symbols)), tags=(row["kind"].lower(),))
        tree.pack(fill="both", expand=True)
        return tree

    def accounts_page(self):
        self.clear(); self.heading("Accounts", "Cash, bank, e-wallet, and investment accounts", ("+ Add account", self.add_account))
        box = ttk.Frame(self.content, style="Card.TFrame", padding=18); box.pack(fill="both", expand=True)
        toolbar = ttk.Frame(box, style="Card.TFrame"); toolbar.pack(fill="x", pady=(0, 10))
        tree = ttk.Treeview(box, columns=("name", "kind", "currency", "balance"), show="headings")
        for key, title in (("name", "Account"), ("kind", "Type"), ("currency", "Currency"), ("balance", "Balance")):
            tree.heading(key, text=title); tree.column(key, anchor="e" if key == "balance" else "w")
        for row in self.store.accounts(): tree.insert("", "end", iid=str(row["id"]), values=(row["name"], row["kind"], row["currency"], money(row["balance"], row["currency"], self.symbols)))
        ttk.Button(toolbar, text="Edit selected", command=lambda: self.edit_account(tree)).pack(side="right")
        tree.bind("<Double-1>", lambda _: self.edit_account(tree))
        tree.pack(fill="both", expand=True)

    def transactions_page(self):
        self.clear(); self.heading("Transactions", "Income and spending in any supported currency", ("+ Add transaction", self.add_transaction))
        box = ttk.Frame(self.content, style="Card.TFrame", padding=18); box.pack(fill="both", expand=True)
        toolbar = ttk.Frame(box, style="Card.TFrame"); toolbar.pack(fill="x", pady=(0, 10))
        tree = self.transaction_table(box, self.store.transactions())
        ttk.Button(toolbar, text="Edit selected", command=lambda: self.edit_transaction(tree)).pack(side="right")
        tree.bind("<Double-1>", lambda _: self.edit_transaction(tree))

    def simple_page(self, title, subtitle, table, columns, rows, action, edit=None):
        self.clear(); self.heading(title, subtitle, ("+ Add", action))
        box = ttk.Frame(self.content, style="Card.TFrame", padding=18); box.pack(fill="both", expand=True)
        toolbar = ttk.Frame(box, style="Card.TFrame"); toolbar.pack(fill="x", pady=(0, 10))
        tree = ttk.Treeview(box, columns=columns, show="headings")
        for key in columns: tree.heading(key, text=key.replace("_", " ").title()); tree.column(key, anchor="w")
        for row in rows: tree.insert("", "end", iid=str(row["id"]), values=tuple(row.get(key, "") for key in columns))
        if edit:
            ttk.Button(toolbar, text="Edit selected", command=lambda: edit(tree)).pack(side="right")
            tree.bind("<Double-1>", lambda _: edit(tree))
        tree.pack(fill="both", expand=True)

    def budgets_page(self):
        rows = self.store.db.execute("SELECT * FROM budgets ORDER BY month DESC").fetchall()
        self.simple_page("Budgets", "Plan monthly limits by category", "budgets", ("category", "amount", "currency", "month"), [{**r, "amount": money(r["amount"], r["currency"], self.symbols)} for r in rows], self.add_budget, self.edit_budget)

    def goals_page(self):
        rows = self.store.db.execute("SELECT * FROM goals ORDER BY deadline NULLS LAST").fetchall()
        self.simple_page("Savings goals", "Track progress toward the things that matter", "goals", ("name", "saved", "target", "currency", "deadline"), [{**r, "saved": money(r["saved"], r["currency"], self.symbols), "target": money(r["target"], r["currency"], self.symbols)} for r in rows], self.add_goal, self.edit_goal)

    def bills_page(self):
        rows = self.store.db.execute("SELECT * FROM bills ORDER BY paid, due_date").fetchall()
        self.simple_page("Bills", "Keep recurring payments visible", "bills", ("name", "amount", "currency", "due_date", "paid"), [{**r, "amount": money(r["amount"], r["currency"], self.symbols)} for r in rows], self.add_bill, self.edit_bill)

    def assets_page(self):
        rows = self.store.db.execute("SELECT * FROM assets ORDER BY name").fetchall()
        self.simple_page("Assets", "Track investments and other holdings", "assets", ("name", "kind", "value", "currency"), [{**r, "value": money(r["value"], r["currency"], self.symbols)} for r in rows], self.add_asset, self.edit_asset)

    def reports_page(self):
        rows = self.store.db.execute("SELECT category, currency, SUM(amount) total FROM transactions WHERE kind='Expense' GROUP BY category,currency ORDER BY total DESC").fetchall()
        self.clear(); self.heading("Reports", "Expense breakdown by category")
        box = ttk.Frame(self.content, style="Card.TFrame", padding=18); box.pack(fill="both", expand=True)
        toolbar = ttk.Frame(box, style="Card.TFrame"); toolbar.pack(fill="x", pady=(0, 10))
        ttk.Button(toolbar, text="Export Excel", command=self.export_reports_xlsx).pack(side="right", padx=(8, 0))
        ttk.Button(toolbar, text="Export PDF", command=self.export_reports_pdf).pack(side="right")
        tree = ttk.Treeview(box, columns=("category", "total", "currency"), show="headings")
        for key, title in (("category", "Category"), ("total", "Total"), ("currency", "Currency")):
            tree.heading(key, text=title); tree.column(key, anchor="e" if key == "total" else "w")
        for row in rows:
            tree.insert("", "end", values=(row["category"], money(row["total"], row["currency"], self.symbols), row["currency"]))
        tree.pack(fill="both", expand=True)

    def report_data(self):
        rows = self.store.db.execute("SELECT category, currency, SUM(amount) total FROM transactions WHERE kind='Expense' GROUP BY category,currency ORDER BY total DESC").fetchall()
        transactions = self.store.transactions()
        return rows, transactions

    def export_reports_xlsx(self):
        path = filedialog.asksaveasfilename(title="Export expense report", defaultextension=".xlsx", filetypes=[("Excel workbook", "*.xlsx")])
        if not path:
            return
        try:
            import xlsxwriter
            rows, transactions = self.report_data()
            workbook = xlsxwriter.Workbook(path)
            title = workbook.add_format({"bold": True, "font_size": 16, "font_color": "#17212b"})
            header = workbook.add_format({"bold": True, "font_color": "white", "bg_color": "#1f8f5f", "border": 0})
            currency = workbook.add_format({"num_format": "#,##0.00"})
            sheet = workbook.add_worksheet("Expense report")
            sheet.write("A1", "Money Manager Expense Report", title)
            sheet.write_row("A3", ["Category", "Total", "Currency"], header)
            for index, row in enumerate(rows, 3):
                sheet.write(index, 0, row["category"])
                sheet.write_number(index, 1, float(row["total"]), currency)
                sheet.write(index, 2, row["currency"])
            sheet.set_column("A:A", 22); sheet.set_column("B:B", 16); sheet.set_column("C:C", 12)
            detail = workbook.add_worksheet("Transactions")
            detail.write_row("A1", ["Date", "Type", "Category", "Account", "Amount", "Currency", "Note"], header)
            for index, row in enumerate(transactions, 1):
                detail.write_row(index, 0, [str(row["happened_on"]), row["kind"], row["category"], row["account_name"], float(row["amount"]), row["currency"], row["note"]])
            detail.set_column("A:A", 14); detail.set_column("B:D", 16); detail.set_column("E:E", 16); detail.set_column("F:F", 12); detail.set_column("G:G", 30)
            workbook.close()
            messagebox.showinfo("Export complete", f"Excel report saved to:\n{path}", parent=self)
        except ImportError:
            messagebox.showerror("Missing dependency", "Install the Excel export package with:\n\npython -m pip install XlsxWriter", parent=self)

    def export_reports_pdf(self):
        path = filedialog.asksaveasfilename(title="Export expense report", defaultextension=".pdf", filetypes=[("PDF document", "*.pdf")])
        if not path:
            return
        try:
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.lib.units import mm
            from reportlab.platypus import SimpleDocTemplate, Spacer, Table, TableStyle, Paragraph
            rows, transactions = self.report_data()
            document = SimpleDocTemplate(path, pagesize=A4, rightMargin=16 * mm, leftMargin=16 * mm, topMargin=16 * mm, bottomMargin=16 * mm)
            styles = getSampleStyleSheet()
            content = [Paragraph("Money Manager Expense Report", styles["Title"]), Spacer(1, 8), Paragraph(f"Base currency: {self.base}", styles["Normal"]), Spacer(1, 14)]
            summary = [["Category", "Total", "Currency"]] + [[r["category"], f"{r['total']:,.2f}", r["currency"]] for r in rows]
            table = Table(summary, colWidths=[80 * mm, 45 * mm, 30 * mm], repeatRows=1)
            table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f8f5f")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d0d5dd")), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f7fb")]), ("ALIGN", (1, 1), (1, -1), "RIGHT")]))
            content.append(table)
            content += [Spacer(1, 20), Paragraph("Transactions", styles["Heading2"]), Spacer(1, 8)]
            detail = [["Date", "Type", "Category", "Account", "Amount", "Currency"]] + [[str(r["happened_on"]), r["kind"], r["category"], r["account_name"], f"{r['amount']:,.2f}", r["currency"]] for r in transactions]
            detail_table = Table(detail, colWidths=[23 * mm, 24 * mm, 30 * mm, 42 * mm, 28 * mm, 20 * mm], repeatRows=1)
            detail_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#243b5a")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 8), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d0d5dd")), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f7fb")])]))
            content.append(detail_table)
            document.build(content)
            messagebox.showinfo("Export complete", f"PDF report saved to:\n{path}", parent=self)
        except ImportError:
            messagebox.showerror("Missing dependency", "Install the PDF export package with:\n\npython -m pip install reportlab", parent=self)

    def add_account(self):
        f = Form(self, "Add account"); name = f.entry("Name"); kind = f.combo("Type", ["Cash", "Bank", "E-wallet", "Investment", "Other"]); currency = f.combo("Currency", list(self.rates)); balance = f.entry("Starting balance", "0")
        def save():
            try: value = amount(balance.get(), False)
            except ValueError as e: return f.error(str(e))
            if not name.get().strip(): return f.error("Name is required")
            self.store.add_account(name.get().strip(), kind.get(), currency.get(), value); f.destroy(); self.accounts_page()
        f.save(save)

    def edit_account(self, tree):
        selected = tree.selection()
        if not selected:
            return messagebox.showinfo("Select an account", "Select an account to edit.", parent=self)
        row = self.store.account(int(selected[0]))
        f = Form(self, "Edit account")
        name = f.entry("Name", row["name"])
        kind = f.combo("Type", ["Cash", "Bank", "E-wallet", "Investment", "Other"])
        kind.set(row["kind"])
        currency = f.combo("Currency", list(self.rates))
        currency.set(row["currency"])
        balance = f.entry("Balance", str(row["balance"]))
        def save():
            if not name.get().strip():
                return f.error("Name is required")
            try:
                value = amount(balance.get(), False)
            except ValueError as e:
                return f.error(str(e))
            self.store.update_account(row["id"], name.get().strip(), kind.get(), currency.get(), value)
            f.destroy(); self.accounts_page()

    def selected_record(self, tree, table):
        selected = tree.selection()
        if not selected:
            messagebox.showinfo("Select an item", "Select an item to edit.", parent=self)
            return None
        return self.store.record(table, int(selected[0]))

    def edit_transaction(self, tree):
        row = self.selected_record(tree, "transactions")
        if not row:
            return
        f = Form(self, "Edit transaction")
        kind = f.combo("Type", ["Expense", "Income"]); kind.set(row["kind"])
        accounts = {r["name"]: r["id"] for r in self.store.accounts()}; account_names = {value: key for key, value in accounts.items()}
        account = f.combo("Account", list(accounts)); account.set(account_names[row["account_id"]])
        currency = f.combo("Currency", list(self.rates)); currency.set(row["currency"])
        value = f.entry("Amount", str(row["amount"])); category = f.combo("Category", CATEGORIES); category.set(row["category"])
        note = f.entry("Note", row["note"]); happened = f.entry("Date", str(row["happened_on"]))
        def save():
            try: value2 = amount(value.get()); date.fromisoformat(happened.get().strip())
            except ValueError as e: return f.error(str(e))
            self.store.update_transaction(row["id"], accounts[account.get()], kind.get(), value2, currency.get(), category.get(), note.get().strip(), happened.get().strip())
            f.destroy(); self.transactions_page()
        f.save(save)

    def edit_budget(self, tree):
        row = self.selected_record(tree, "budgets")
        if not row: return
        f = Form(self, "Edit budget"); category = f.combo("Category", CATEGORIES); category.set(row["category"]); value = f.entry("Monthly limit", str(row["amount"])); currency = f.combo("Currency", list(self.rates)); currency.set(row["currency"]); month = f.entry("Month", str(row["month"]))
        def save():
            try: value2 = amount(value.get()); date.fromisoformat(month.get())
            except ValueError as e: return f.error(str(e))
            self.store.update_record("budgets", row["id"], {"category": category.get(), "amount": value2, "currency": currency.get(), "month": month.get()}); f.destroy(); self.budgets_page()
        f.save(save)

    def edit_goal(self, tree):
        row = self.selected_record(tree, "goals")
        if not row: return
        f = Form(self, "Edit savings goal"); name = f.entry("Goal name", row["name"]); target = f.entry("Target", str(row["target"])); saved = f.entry("Already saved", str(row["saved"])); currency = f.combo("Currency", list(self.rates)); currency.set(row["currency"]); deadline = f.entry("Deadline", str(row["deadline"] or ""))
        def save():
            try: target2, saved2 = amount(target.get()), amount(saved.get(), False); date.fromisoformat(deadline.get()) if deadline.get() else None
            except ValueError as e: return f.error(str(e))
            self.store.update_record("goals", row["id"], {"name": name.get().strip(), "target": target2, "saved": saved2, "currency": currency.get(), "deadline": deadline.get() or None}); f.destroy(); self.goals_page()
        f.save(save)

    def edit_bill(self, tree):
        row = self.selected_record(tree, "bills")
        if not row: return
        f = Form(self, "Edit bill"); name = f.entry("Bill name", row["name"]); value = f.entry("Amount", str(row["amount"])); currency = f.combo("Currency", list(self.rates)); currency.set(row["currency"]); due = f.entry("Due date", str(row["due_date"])); paid = f.combo("Status", ["False", "True"]); paid.set(str(row["paid"]))
        def save():
            try: value2 = amount(value.get()); date.fromisoformat(due.get())
            except ValueError as e: return f.error(str(e))
            self.store.update_record("bills", row["id"], {"name": name.get().strip(), "amount": value2, "currency": currency.get(), "due_date": due.get(), "paid": paid.get() == "True"}); f.destroy(); self.bills_page()
        f.save(save)

    def edit_asset(self, tree):
        row = self.selected_record(tree, "assets")
        if not row: return
        f = Form(self, "Edit asset"); name = f.entry("Asset name", row["name"]); kind = f.combo("Type", ["Stock", "Crypto", "Property", "Vehicle", "Other"]); kind.set(row["kind"]); value = f.entry("Current value", str(row["value"])); currency = f.combo("Currency", list(self.rates)); currency.set(row["currency"])
        def save():
            try: value2 = amount(value.get())
            except ValueError as e: return f.error(str(e))
            self.store.update_record("assets", row["id"], {"name": name.get().strip(), "kind": kind.get(), "value": value2, "currency": currency.get()}); f.destroy(); self.assets_page()
        f.save(save)
        f.save(save)

    def add_transaction(self):
        if not self.store.accounts(): return self.add_account()
        f = Form(self, "Add transaction"); kind = f.combo("Type", ["Expense", "Income"]); accounts = {r["name"]: r["id"] for r in self.store.accounts()}; account = f.combo("Account", list(accounts)); currency = f.combo("Currency", list(self.rates)); value = f.entry("Amount"); category = f.combo("Category", CATEGORIES); note = f.entry("Note"); happened = f.entry("Date", date.today().isoformat())
        def save():
            try: value2 = amount(value.get()); date.fromisoformat(happened.get().strip())
            except ValueError as e: return f.error(str(e))
            self.store.add_transaction(accounts[account.get()], kind.get(), value2, currency.get(), category.get(), note.get().strip(), happened.get().strip()); f.destroy(); self.dashboard()
        f.save(save)

    def add_budget(self):
        f = Form(self, "Add budget"); category = f.combo("Category", CATEGORIES); value = f.entry("Monthly limit"); currency = f.combo("Currency", list(self.rates)); month = f.entry("Month", date.today().replace(day=1).isoformat())
        def save():
            try: value2 = amount(value.get()); date.fromisoformat(month.get())
            except ValueError as e: return f.error(str(e))
            self.store.add_simple("budgets", {"category": category.get(), "amount": value2, "currency": currency.get(), "month": month.get()}); f.destroy(); self.budgets_page()
        f.save(save)

    def add_goal(self):
        f = Form(self, "Add savings goal"); name = f.entry("Goal name"); target = f.entry("Target"); saved = f.entry("Already saved", "0"); currency = f.combo("Currency", list(self.rates)); deadline = f.entry("Deadline", "")
        def save():
            try: target2, saved2 = amount(target.get()), amount(saved.get(), False); date.fromisoformat(deadline.get()) if deadline.get() else None
            except ValueError as e: return f.error(str(e))
            self.store.add_simple("goals", {"name": name.get().strip(), "target": target2, "saved": saved2, "currency": currency.get(), "deadline": deadline.get() or None}); f.destroy(); self.goals_page()
        f.save(save)

    def add_bill(self):
        f = Form(self, "Add bill"); name = f.entry("Bill name"); value = f.entry("Amount"); currency = f.combo("Currency", list(self.rates)); due = f.entry("Due date", date.today().isoformat())
        def save():
            try: value2 = amount(value.get()); date.fromisoformat(due.get())
            except ValueError as e: return f.error(str(e))
            self.store.add_simple("bills", {"name": name.get().strip(), "amount": value2, "currency": currency.get(), "due_date": due.get(), "paid": False}); f.destroy(); self.bills_page()
        f.save(save)

    def add_asset(self):
        f = Form(self, "Add asset"); name = f.entry("Asset name"); kind = f.combo("Type", ["Stock", "Crypto", "Property", "Vehicle", "Other"]); value = f.entry("Current value"); currency = f.combo("Currency", list(self.rates))
        def save():
            try: value2 = amount(value.get())
            except ValueError as e: return f.error(str(e))
            self.store.add_simple("assets", {"name": name.get().strip(), "kind": kind.get(), "value": value2, "currency": currency.get()}); f.destroy(); self.assets_page()
        f.save(save)

    def close(self):
        self.store.close(); self.destroy()


def check():
    assert amount("1,250.50") == Decimal("1250.50")
    assert check_pin("1234", pin_hash("1234"))
    assert not check_pin("1235", pin_hash("1234"))
    print("ok")


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
    else:
        store = Store()
        gate = Auth(store)
        gate.mainloop()
        if gate.ok:
            MoneyManager(store).mainloop()
        else:
            store.close()
