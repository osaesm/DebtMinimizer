import argparse
import calendar
import copy
import datetime
from datetime import timedelta
import math
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

import gspread
from google.oauth2.service_account import Credentials


# =====================================================================
# DATE UTILITIES
# =====================================================================

def clamp_day(year: int, month: int, day: int) -> int:
    """Clamps calendar day to valid maximum for given year and month."""
    return min(day, calendar.monthrange(year, month)[1])


def make_date(year: int, month: int, day: int) -> datetime.date:
    """Safely constructs datetime.date with month-end day clamping."""
    return datetime.date(year, month, clamp_day(year, month, day))


def advance_month(dt: datetime.date, target_day: int, months: int = 1) -> datetime.date:
    """Advances or retreats a date by N calendar months, clamping day to month end."""
    m = dt.year * 12 + dt.month - 1 + months
    return make_date(m // 12, (m % 12) + 1, target_day)


def get_first_due_date(start_date: datetime.date, due_day: int) -> datetime.date:
    """Finds first calendar due date on or after start_date with day clamping."""
    first = make_date(start_date.year, start_date.month, due_day)
    return first if first >= start_date else advance_month(first, due_day)


def clamp_zero(val: float) -> float:
    """Clamps sub-cent floating point dust to 0.0."""
    return 0.0 if abs(val) < 0.005 else val


# =====================================================================
# DATACLASSES
# =====================================================================

@dataclass
class IncomeSchedule:
    schedule_type: str  # 'biweekly' or 'semi-monthly'
    amount: float
    start_date: Optional[Union[str, datetime.date]] = None
    amount_2: Optional[float] = None
    name: str = ""

    def __post_init__(self):
        if isinstance(self.start_date, str):
            self.start_date = parse_date(self.start_date)

    def is_payday(self, dt: datetime.date) -> bool:
        if self.schedule_type == 'semi-monthly':
            return dt.day in (1, 15)
        return bool(self.start_date and dt >= self.start_date and (dt - self.start_date).days % 14 == 0)

    def get_amount(self, dt: datetime.date) -> float:
        return self.amount_2 if (self.schedule_type == 'biweekly' and self.amount_2 is not None and 15 <= dt.day <= 28) else self.amount


@dataclass
class MonthlyPayment:
    name: str
    amount: float
    due_day: int
    next_due: Optional[datetime.date] = None


@dataclass
class Loan:
    name: str
    amount_left: float
    apr: float
    minimum_payment: float
    due_day: int
    next_due: Optional[datetime.date] = None
    remaining_min_due: float = 0.0
    accrued_interest: float = 0.0
    amount_paid_this_cycle: float = 0.0

    def apply_payment(self, amount: float) -> float:
        """Applies payment: interest first, then principal. Returns interest paid."""
        pay = min(amount, self.amount_left + self.accrued_interest)
        if pay <= 0:
            return 0.0
        int_pay = min(pay, self.accrued_interest)
        self.accrued_interest -= int_pay
        self.amount_left -= (pay - int_pay)
        if self.amount_left < 0.005:
            self.amount_left = self.accrued_interest = self.remaining_min_due = 0.0
        else:
            self.remaining_min_due = max(0.0, self.remaining_min_due - pay)
        self.amount_paid_this_cycle += pay
        return int_pay


@dataclass
class CreditCard:
    card: str
    issuer: str
    apr: float
    balance: float
    promo_balance: float = 0.0
    promo_end_date: Optional[datetime.date] = None
    statement_cycle_end_day: int = 0
    statement_balance: float = 0.0
    grace_active: bool = False
    due_day: int = 0
    grace_period_days: int = 0
    cycle_end_nth_weekday: int = 0
    min_floor: float = 0.0
    annual_fee: float = 0.0
    renewal_date: Optional[datetime.date] = None
    initial_min_payment: Optional[float] = None
    next_due: Optional[datetime.date] = None
    cycle_due_date: Optional[datetime.date] = None
    next_cycle_end: Optional[datetime.date] = None
    days_in_cycle: int = 0
    adb_reg_acc: float = 0.0
    remaining_min_due: float = 0.0
    amount_paid_this_cycle: float = 0.0
    consecutive_full_payments: int = 0

    def apply_due_payment(self, pay: float):
        """Applies payment on due date: min to promo first; extra to regular balance first."""
        min_p = min(pay, self.remaining_min_due)
        extra = pay - min_p
        promo_p = min(min_p, self.promo_balance)
        self.promo_balance -= promo_p
        self.balance = max(0.0, self.balance - (min_p - promo_p))
        reg_extra = min(extra, self.balance)
        self.balance = clamp_zero(self.balance - reg_extra)
        self.promo_balance = clamp_zero(self.promo_balance - (extra - reg_extra))
        self.amount_paid_this_cycle += pay
        self.remaining_min_due = max(0.0, self.remaining_min_due - min_p)

    def record_full_payment_streak(self, paid_in_full: bool):
        """Updates consecutive full payment streak for grace restoration (requires 2 consecutive)."""
        if paid_in_full:
            self.consecutive_full_payments += 1
            if self.consecutive_full_payments >= 2:
                self.grace_active, self.consecutive_full_payments = True, 0
        else:
            self.consecutive_full_payments = 0


# =====================================================================
# SIMULATION ENGINE
# =====================================================================

class DebtOptimizer:
    """Deterministic day-by-day personal finance simulation and debt avalanche optimization engine."""

    def __init__(self, params: Dict[str, Any], ccs: List[Dict[str, Any]],
                 loans: List[Dict[str, Any]], monthly: List[Dict[str, Any]],
                 income: Any):
        self.raw_params, self.raw_ccs, self.raw_loans, self.raw_monthly, self.raw_income = map(copy.deepcopy, (params, ccs, loans, monthly, income))
        self.start_date, self.end_date = self.raw_params['Start Date'], self.raw_params['End Date']
        self.checking, self.buffer = float(self.raw_params['Initial Checking Balance']), float(self.raw_params['Checking Buffer'])
        self.current_date = self.start_date
        self.schedule_log: List[Dict[str, Any]] = []
        self.total_interest_paid: float = 0.0

        # Build income schedules
        raw_list = self.raw_income if isinstance(self.raw_income, list) else [self.raw_income]
        self.incomes: List[IncomeSchedule] = []
        for inc in raw_list:
            stype = str(inc.get('Type') or '').strip().lower()
            name = str(inc.get('Name') or '')
            raw_amt = inc.get('Paycheck Amount') if inc.get('Paycheck Amount') not in (None, '') else inc.get('Paychecks 1 and 3')
            if raw_amt in (None, ''):
                raise ValueError(f"Income schedule '{name or 'Income'}' is missing 'Paycheck Amount'.")
            amt = float(raw_amt)
            if stype in ('semi-monthly', 'semimonthly', 'semi monthly'):
                self.incomes.append(IncomeSchedule('semi-monthly', amt, name=name))
            elif stype == 'biweekly' or ('Paychecks 1 and 3' in inc and not inc.get('Type')):
                start = inc.get('Start Date')
                if start in (None, ''):
                    raise ValueError(f"Biweekly income schedule '{name or 'Income'}' is missing 'Start Date'.")
                amt2 = inc.get('Paycheck 2 Amount') if 'Paycheck 2 Amount' in inc else inc.get('Paycheck 2')
                self.incomes.append(IncomeSchedule('biweekly', amt, start_date=start, amount_2=float(amt2) if amt2 not in (None, '') else None, name=name))
            else:
                raise ValueError(f"Invalid or missing income schedule Type: '{inc.get('Type')}'. Must be 'Biweekly' or 'Semi-Monthly'.")

        # Build Monthly Payments & Loans
        self.monthly = [MonthlyPayment(m['Name'], float(m['Amount']), int(m['Due Day']), get_first_due_date(self.start_date, int(m['Due Day']))) for m in self.raw_monthly]
        self.loans = []
        for l in self.raw_loans:
            d_day, amt, min_p = int(l['Due Day']), float(l['Amount Left']), float(l['Minimum Payment'])
            self.loans.append(Loan(l['Name'], amt, float(l['APR']), min_p, d_day, get_first_due_date(self.start_date, d_day), min(amt, min_p)))

        # Build Credit Cards
        self.ccs = []
        for c in self.raw_ccs:
            for req in ('Card', 'Issuer', 'APR', 'Balance', 'Statement Balance', 'Grace Active', 'Due Day', 'Min Floor'):
                if c.get(req) in (None, ''):
                    raise ValueError(f"Credit card '{c.get('Card', '') or 'Unnamed'}' is missing '{req}'.")
            d_day, bal, p_bal = int(c['Due Day']), float(c['Balance']), float(c.get('Promo Balance', 0.0) or 0.0)
            init_m = c.get('Initial Minimum Payment') or c.get('First Month Min Payment') or c.get('Initial Min Payment')
            cc = CreditCard(
                card=c['Card'], issuer=c['Issuer'], apr=float(c['APR']), balance=max(0.0, bal - p_bal) if p_bal > 0 else bal,
                promo_balance=p_bal, promo_end_date=c.get('Promo End Date'), statement_cycle_end_day=int(c.get('Statement Cycle End Day', 0) or 0),
                statement_balance=float(c['Statement Balance']), grace_active=bool(c['Grace Active']), due_day=d_day,
                grace_period_days=int(c.get('Grace Period Days', 0) or 0), cycle_end_nth_weekday=int(c.get('Cycle End Nth Weekday', 0) or 0),
                min_floor=float(c['Min Floor']), annual_fee=float(c.get('Annual Fee', 0.0) or 0.0),
                renewal_date=parse_date(c.get('Renewal Date')) if isinstance(c.get('Renewal Date'), str) else c.get('Renewal Date'),
                initial_min_payment=parse_float(init_m) if init_m not in (None, '') else None
            )
            # Cycle dates & ADB initialization
            cc.next_due = get_first_due_date(self.start_date, d_day)
            due_end = self.calculate_cycle_end_date(cc.issuer, cc.next_due, cc.grace_period_days, cc.cycle_end_nth_weekday, cc.statement_cycle_end_day)
            if due_end >= self.start_date:
                cc.cycle_due_date, cc.next_cycle_end = cc.next_due, due_end
                prev_end = self.calculate_cycle_end_date(cc.issuer, advance_month(cc.next_due, d_day, -1), cc.grace_period_days, cc.cycle_end_nth_weekday, cc.statement_cycle_end_day)
            else:
                f_due = advance_month(cc.next_due, d_day)
                n_end = self.calculate_cycle_end_date(cc.issuer, f_due, cc.grace_period_days, cc.cycle_end_nth_weekday, cc.statement_cycle_end_day)
                while n_end < self.start_date:
                    due_end = n_end
                    f_due = advance_month(f_due, d_day)
                    n_end = self.calculate_cycle_end_date(cc.issuer, f_due, cc.grace_period_days, cc.cycle_end_nth_weekday, cc.statement_cycle_end_day)
                cc.cycle_due_date, cc.next_cycle_end, prev_end = f_due, n_end, due_end

            if prev_end < self.start_date:
                cc.days_in_cycle = max(0, (self.start_date - prev_end).days)
                cc.adb_reg_acc = cc.balance * cc.days_in_cycle
            cc.remaining_min_due = min(cc.statement_balance, cc.initial_min_payment) if (cc.initial_min_payment and cc.initial_min_payment > 0 and cc.statement_balance > 0) else self.calc_cc_min_payment(cc.issuer, cc.statement_balance, cc.min_floor)
            self.ccs.append(cc)

    # -----------------------------------------------------------------
    # Incomes, Logging & Formulas
    # -----------------------------------------------------------------

    def get_total_income(self, dt: datetime.date) -> float:
        """Returns total income credited on dt across all active schedules."""
        return sum(inc.get_amount(dt) for inc in self.incomes if inc.is_payday(dt))

    def get_next_payday(self) -> datetime.date:
        """Earliest next biweekly payday strictly after current_date (backward compat)."""
        bi = [inc.start_date + timedelta(days=((self.current_date - inc.start_date).days // 14 + 1) * 14) if self.current_date >= inc.start_date else inc.start_date for inc in self.incomes if inc.schedule_type == 'biweekly' and inc.start_date]
        return min(bi) if bi else self.current_date + timedelta(days=14)

    def get_paycheck_amount(self, dt: datetime.date) -> float:
        """Total biweekly income amount on pay_date (backward compat)."""
        return sum(inc.get_amount(dt) for inc in self.incomes if inc.schedule_type == 'biweekly')

    def log_transaction(self, desc: str, amount: float, consolidate: bool = False):
        """Records transaction, updates checking balance, and clamps to buffer floor."""
        amt, d_str = round(amount, 2), self.current_date.strftime('%Y-%m-%d')
        self.checking = round(self.checking + amt, 2)
        if abs(self.checking - self.buffer) < 0.005:
            self.checking = round(self.buffer, 2)
        if consolidate and self.schedule_log and self.schedule_log[-1]['Date'] == d_str and self.schedule_log[-1]['Description'] == desc:
            self.schedule_log[-1]['Amount'] = round(self.schedule_log[-1]['Amount'] + amt, 2)
            self.schedule_log[-1]['Checking Balance'] = self.checking
        else:
            self.schedule_log.append({'Date': d_str, 'Description': desc, 'Amount': amt, 'Checking Balance': self.checking})

    @staticmethod
    def calculate_cycle_end_date(issuer: str, due_date: datetime.date, grace_period_days: int = 0,
                                 cycle_end_nth_weekday: int = 0, statement_cycle_end_day: int = 0) -> datetime.date:
        if issuer == 'Chase':
            if statement_cycle_end_day <= 0:
                raise ValueError("statement_cycle_end_day must be > 0 for Chase")
            if statement_cycle_end_day < due_date.day and (due_date.day - statement_cycle_end_day) >= 15:
                return make_date(due_date.year, due_date.month, statement_cycle_end_day)
            pm = 12 if due_date.month == 1 else due_date.month - 1
            py = due_date.year - 1 if due_date.month == 1 else due_date.year
            return make_date(py, pm, statement_cycle_end_day)

        if issuer == 'Citibank':
            if cycle_end_nth_weekday <= 0:
                raise ValueError("cycle_end_nth_weekday must be > 0 for Citibank")
            pm = 12 if due_date.month == 1 else due_date.month - 1
            py = due_date.year - 1 if due_date.month == 1 else due_date.year
            days = calendar.monthrange(py, pm)[1]
            bdays = [make_date(py, pm, d) for d in range(1, days + 1) if make_date(py, pm, d).weekday() < 5]
            if cycle_end_nth_weekday > len(bdays):
                raise ValueError(f"Citibank cycle_end_nth_weekday={cycle_end_nth_weekday} exceeds the number of business days in prior month ({len(bdays)}).")
            return bdays[cycle_end_nth_weekday - 1]

        if grace_period_days <= 0:
            raise ValueError(f"grace_period_days must be > 0 for {issuer}")
        dt = due_date - timedelta(days=grace_period_days)
        return dt - timedelta(days=1) if issuer == 'X1' and dt.weekday() == 5 else dt

    @staticmethod
    def calc_cc_min_payment(issuer: str, statement_balance: float, min_floor: float,
                            interest_charged: float = 0.0, fees: float = 0.0) -> float:
        if statement_balance <= 0.005:
            return 0.0
        if statement_balance < min_floor:
            return round(statement_balance, 2)
        p = max(0.0, statement_balance - interest_charged - fees)
        if issuer == 'American Express':
            res = max(min_floor, interest_charged + fees + 0.01 * p, 0.02 * p)
        elif issuer == 'Discover':
            res = max(min_floor, 20.0 + interest_charged + fees, 0.02 * statement_balance)
        else:
            res = interest_charged + fees + max(min_floor, 0.01 * p)
        return max(0.0, min(round(statement_balance, 2), round(res, 2)))

    # -----------------------------------------------------------------
    # Multi-Period Forward Lookahead Cushion Engine
    # -----------------------------------------------------------------

    def get_projected_min_margin(self) -> float:
        """Projects forward baseline cash flows to find minimum margin (projected_balance - buffer)."""
        proj_bal = self.checking
        min_margin = proj_bal - self.buffer
        m_t = [{'amt': m.amount, 'day': m.due_day, 'due': m.next_due} for m in self.monthly]
        l_t = [{'bal': l.amount_left + l.accrued_interest, 'day': l.due_day, 'due': l.next_due, 'rem': l.remaining_min_due, 'min': l.minimum_payment} for l in self.loans]
        c_t = [{'bal': c.balance + c.promo_balance, 'day': c.due_day, 'due': c.next_due, 'rem': c.remaining_min_due, 'stmt': c.statement_balance, 'paid': c.amount_paid_this_cycle, 'grace': c.grace_active, 'floor': c.min_floor, 'issuer': c.issuer, 'fee': c.annual_fee, 'ren': c.renewal_date} for c in self.ccs]

        d = self.current_date
        while d <= self.end_date:
            if d > self.current_date:
                for c in c_t:
                    if c['fee'] > 0 and c['ren'] and d.month == c['ren'].month and d.day == clamp_day(d.year, c['ren'].month, c['ren'].day):
                        c['bal'] += c['fee']
                proj_bal += self.get_total_income(d)
            for m in m_t:
                if d == m['due']:
                    proj_bal -= m['amt']
                    m['due'] = advance_month(m['due'], m['day'])
            for l in l_t:
                if l['bal'] > 0.005 and d == l['due']:
                    due = min(l['bal'], l['rem'])
                    proj_bal -= due
                    l['bal'] = max(0.0, l['bal'] - due)
                    l['rem'] = min(l['bal'], l['min'])
                    l['due'] = advance_month(l['due'], l['day'])
            for c in c_t:
                if c['bal'] > 0.005 and d == c['due']:
                    due = min(max(c['rem'], c['stmt'] - c['paid']), c['bal']) if c['grace'] else min(c['rem'], c['bal'])
                    proj_bal -= due
                    c['bal'] = max(0.0, c['bal'] - due)
                    c['stmt'], c['paid'] = c['bal'], 0.0
                    c['rem'] = self.calc_cc_min_payment(c['issuer'], c['stmt'], c['floor'])
                    c['due'] = advance_month(c['due'], c['day'])
            min_margin = min(min_margin, proj_bal - self.buffer)
            d += timedelta(days=1)
        return min_margin

    def calculate_disposable_cash(self) -> float:
        """Calculates safe extra cash that guarantees checking balance never breaches buffer."""
        if self.checking <= self.buffer:
            return 0.0
        m = self.get_projected_min_margin()
        return max(0.0, math.floor(round(m, 4) * 100.0) / 100.0) if m > 0.01 else 0.0

    # -----------------------------------------------------------------
    # Main Simulation Loop
    # -----------------------------------------------------------------

    def run(self) -> List[Dict[str, Any]]:
        while self.current_date <= self.end_date:
            # 1. Annual Fees
            for cc in self.ccs:
                if cc.annual_fee > 0 and cc.renewal_date and self.current_date.month == cc.renewal_date.month and self.current_date.day == clamp_day(self.current_date.year, cc.renewal_date.month, cc.renewal_date.day):
                    cc.balance += cc.annual_fee

            # 2. Income
            inc = self.get_total_income(self.current_date)
            if inc > 0:
                self.log_transaction("Paycheck Received", inc)

            # 3. Daily Accruals
            for l in self.loans:
                if l.amount_left > 0.005:
                    l.accrued_interest += l.amount_left * (l.apr / 365.0)
                else:
                    l.amount_left = l.accrued_interest = l.remaining_min_due = 0.0
            for cc in self.ccs:
                if cc.promo_end_date and self.current_date > cc.promo_end_date:
                    cc.balance += cc.promo_balance
                    cc.promo_balance, cc.promo_end_date = 0.0, None
                cc.balance, cc.promo_balance = clamp_zero(cc.balance), clamp_zero(cc.promo_balance)
                cc.adb_reg_acc += cc.balance
                cc.days_in_cycle += 1

            # 4. Avalanche Extra Payments
            if self.checking > self.buffer:
                while True:
                    disp = self.calculate_disposable_cash()
                    if disp <= 0.01:
                        break
                    cands = [('cc', c.apr, c) for c in self.ccs if c.balance > 0 and not c.grace_active and c.apr > 0] + [('loan', l.apr, l) for l in self.loans if l.amount_left > 0 and l.apr > 0]
                    if not cands:
                        break
                    cands.sort(key=lambda x: x[1], reverse=True)
                    ttype, _, tgt = cands[0]
                    max_extra = max(0.0, math.floor(round(self.checking - self.buffer, 4) * 100.0) / 100.0)
                    if max_extra <= 0.005:
                        break
                    pay = min(disp, max_extra)
                    if ttype == 'cc':
                        pay = min(pay, tgt.balance)
                        if pay <= 0.005:
                            break
                        tgt.balance = clamp_zero(tgt.balance - pay)
                        tgt.remaining_min_due = max(0.0, tgt.remaining_min_due - pay)
                        tgt.amount_paid_this_cycle += pay
                        desc = f"Extra payment to {tgt.card}"
                    else:
                        pay = min(pay, tgt.amount_left + tgt.accrued_interest)
                        if pay <= 0.005:
                            break
                        self.total_interest_paid += tgt.apply_payment(pay)
                        desc = f"Extra payment to {tgt.name}"
                    self.log_transaction(desc, -pay, consolidate=True)

            # 5. Mandatory Payments Due Today
            for m in self.monthly:
                if self.current_date == m.next_due:
                    self.log_transaction(f"Monthly Payment: {m.name}", -m.amount)
                    m.next_due = advance_month(m.next_due, m.due_day)
            for l in self.loans:
                if l.amount_left > 0.005 and self.current_date == l.next_due:
                    due = min(l.amount_left + l.accrued_interest, l.remaining_min_due)
                    if due > 0:
                        self.total_interest_paid += l.apply_payment(due)
                        self.log_transaction(f"Loan Payment: {l.name}", -due)
                    l.remaining_min_due = min(l.amount_left + l.accrued_interest, l.minimum_payment)
                    l.amount_paid_this_cycle = 0.0
                    l.next_due = advance_month(l.next_due, l.due_day)
            for cc in self.ccs:
                if self.current_date == cc.next_due:
                    tot = round(cc.balance + cc.promo_balance, 2)
                    if tot <= 0.005:
                        cc.balance = cc.promo_balance = cc.remaining_min_due = 0.0
                        if not cc.grace_active and cc.statement_balance > 0.01:
                            cc.record_full_payment_streak(True)
                        cc.amount_paid_this_cycle = 0.0
                        cc.next_due = advance_month(cc.next_due, cc.due_day)
                        continue
                    if cc.grace_active:
                        tgt_p = min(tot, max(cc.remaining_min_due, cc.statement_balance - cc.amount_paid_this_cycle))
                        safe_tgt = round(tgt_p + self.get_projected_min_margin(), 2)
                        pay = max(cc.remaining_min_due, min(tgt_p, safe_tgt))
                    else:
                        pay = cc.remaining_min_due
                    pay = max(0.0, min(round(pay, 2), tot))
                    if pay > 0:
                        cc.apply_due_payment(pay)
                        self.log_transaction(f"Payment to {cc.card}", -pay)

                    if cc.grace_active:
                        if cc.amount_paid_this_cycle < cc.statement_balance - 0.01:
                            cc.grace_active = False
                            cc.consecutive_full_payments = 0
                            p_days = cc.grace_period_days if cc.grace_period_days > 0 and cc.issuer != 'X1' else max(1, (cc.next_due - self.calculate_cycle_end_date(cc.issuer, cc.next_due, cc.grace_period_days, cc.cycle_end_nth_weekday, cc.statement_cycle_end_day)).days)
                            pen = cc.statement_balance * (cc.apr / 365.0) * p_days
                            cc.balance += pen
                            self.total_interest_paid += pen
                    else:
                        cc.record_full_payment_streak(cc.statement_balance > 0.01 and cc.amount_paid_this_cycle >= cc.statement_balance - 0.01)
                    cc.next_due = advance_month(cc.next_due, cc.due_day)
                    cc.amount_paid_this_cycle = 0.0

            # 6. Statement Cycle End
            for cc in self.ccs:
                if self.current_date == cc.next_cycle_end:
                    interest = 0.0
                    if not cc.grace_active and cc.days_in_cycle > 0:
                        interest = (cc.adb_reg_acc / cc.days_in_cycle) * (cc.apr / 365.0) * cc.days_in_cycle
                        cc.balance += interest
                        self.total_interest_paid += interest
                    cc.statement_balance = round(cc.balance + cc.promo_balance, 2)
                    cc.remaining_min_due = self.calc_cc_min_payment(cc.issuer, cc.statement_balance, cc.min_floor, interest)
                    cc.adb_reg_acc = cc.days_in_cycle = cc.amount_paid_this_cycle = 0.0
                    cc.cycle_due_date = advance_month(cc.cycle_due_date, cc.due_day)
                    cc.next_cycle_end = self.calculate_cycle_end_date(cc.issuer, cc.cycle_due_date, cc.grace_period_days, cc.cycle_end_nth_weekday, cc.statement_cycle_end_day)

            self.current_date += timedelta(days=1)
        return self.schedule_log


# =====================================================================
# SELF-TEST DELEGATOR & GOOGLE SHEETS HELPERS
# =====================================================================

def run_self_tests():
    """Executes the comprehensive mathematical test suite in test_debt_optimizer.py."""
    from test_debt_optimizer import run_self_tests as _run_tests
    _run_tests()


def parse_date(val: Any) -> Optional[datetime.date]:
    """Robust date parsing for Google Sheets inputs."""
    if not val:
        return None
    if isinstance(val, datetime.date) and not isinstance(val, datetime.datetime):
        return val
    if isinstance(val, datetime.datetime):
        return val.date()
    s = str(val).strip()
    if s.lower() in ('none', 'n/a', '-', '', 'null'):
        return None
    if '/' in s and len(s.split('/')) == 2:
        try:
            m, d = map(int, s.split('/'))
            return datetime.date(2000, m, d)
        except ValueError:
            pass
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y', '%d/%m/%Y'):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Could not parse date string: {s}")


def parse_float(val: Any, default: float = 0.0) -> float:
    """Cleans currency strings, commas, and percentage signs to float."""
    if val in (None, ''):
        return default
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).replace('$', '').replace(',', '').replace('%', '').strip()
    try:
        f = float(s)
        return f / 100.0 if '%' in str(val) else f
    except ValueError:
        return default


def parse_apr(val: Any) -> float:
    """Parses APR ensuring decimal format (e.g. 24.99% or 24.99 -> 0.2499)."""
    f = parse_float(val)
    return f / 100.0 if f > 1.0 else f


def check_required_fields(row: Dict[str, Any], fields: List[str], sheet: str, idx: int, name: str, alerts: List[str]):
    for f in fields:
        if row.get(f) in (None, ''):
            alerts.append(f"Worksheet '{sheet}', row {idx} ({name}): '{f}' is missing.")


def parse_due_day(row: Dict[str, Any], sheet: str, idx: int, name: str, alerts: List[str]) -> int:
    raw = row.get('Due Day')
    if raw in (None, ''):
        alerts.append(f"Worksheet '{sheet}', row {idx} ({name}): 'Due Day' is missing.")
        return 0
    d = int(parse_float(raw))
    if not (1 <= d <= 31):
        alerts.append(f"Worksheet '{sheet}', row {idx} ({name}): 'Due Day' ({d}) must be between 1 and 31.")
        return 0
    return d


def get_google_sheets_client(creds_path="credentials.json", token_path="token.json", sa_path=None):
    if sa_path:
        return gspread.authorize(Credentials.from_service_account_file(sa_path, scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]))
    import os
    if not os.path.exists(creds_path) and not os.path.exists(token_path):
        raise FileNotFoundError(f"OAuth credentials '{creds_path}' not found. Please provide credentials.json or specify --service-account")
    return gspread.oauth(credentials_filename=creds_path, authorized_user_filename=token_path)


def run_optimizer_from_sheet(sheet_name: str, creds_path: str, token_path: str, service_account_path: Optional[str]):
    client = get_google_sheets_client(creds_path, token_path, service_account_path)
    try:
        sheet = client.open(sheet_name)
    except Exception as e:
        print(f"Error opening Google Sheet '{sheet_name}': {e}")
        return

    print("Loading data from worksheets...")
    alerts: List[str] = []

    # 1. Parameters
    p_recs = sheet.worksheet("Parameters").get_all_records()
    if not p_recs:
        raise ValueError("Worksheet 'Parameters' is empty.")
    p = p_recs[0]
    check_required_fields(p, ['Start Date', 'End Date', 'Initial Checking Balance', 'Checking Buffer'], 'Parameters', 2, 'Parameters', alerts)
    params = {'Start Date': parse_date(p.get('Start Date')), 'End Date': parse_date(p.get('End Date')),
              'Initial Checking Balance': parse_float(p.get('Initial Checking Balance')), 'Checking Buffer': parse_float(p.get('Checking Buffer'))}

    # 2. Credit Cards
    ccs = []
    for idx, c in enumerate(sheet.worksheet("CreditCards").get_all_records(), start=2):
        cname, iss = str(c.get('Card') or f'Row {idx}').strip(), str(c.get('Issuer') or '').strip()
        check_required_fields(c, ['Card', 'Issuer', 'APR', 'Balance', 'Statement Balance', 'Grace Active', 'Min Floor'], 'CreditCards', idx, cname, alerts)
        if iss == 'Chase' and not c.get('Statement Cycle End Day'):
            alerts.append(f"Worksheet 'CreditCards', row {idx} ({cname}): 'Statement Cycle End Day' is required for Chase cards.")
        elif iss == 'Citibank' and not c.get('Cycle End Nth Weekday'):
            alerts.append(f"Worksheet 'CreditCards', row {idx} ({cname}): 'Cycle End Nth Weekday' is required for Citibank cards.")
        elif iss in ('American Express', 'Capital One', 'Discover', 'X1') and not c.get('Grace Period Days'):
            alerts.append(f"Worksheet 'CreditCards', row {idx} ({cname}): 'Grace Period Days' is required for {iss} cards.")
        init_m = c.get('Initial Minimum Payment') or c.get('First Month Min Payment') or c.get('Initial Min Payment')
        ccs.append({
            'Card': cname, 'Issuer': iss, 'APR': parse_apr(c.get('APR')), 'Balance': parse_float(c.get('Balance')),
            'Promo Balance': parse_float(c.get('Promo Balance')), 'Promo End Date': parse_date(c.get('Promo End Date')),
            'Statement Cycle End Day': int(parse_float(c.get('Statement Cycle End Day'))), 'Statement Balance': parse_float(c.get('Statement Balance')),
            'Grace Active': str(c.get('Grace Active') or '').strip().upper() == 'TRUE', 'Due Day': parse_due_day(c, 'CreditCards', idx, cname, alerts),
            'Grace Period Days': int(parse_float(c.get('Grace Period Days'))), 'Cycle End Nth Weekday': int(parse_float(c.get('Cycle End Nth Weekday'))),
            'Min Floor': parse_float(c.get('Min Floor')), 'Annual Fee': parse_float(c.get('Annual Fee')), 'Renewal Date': parse_date(c.get('Renewal Date')),
            'Initial Minimum Payment': parse_float(init_m) if init_m not in (None, '') else None
        })

    # 3. Loans & 4. Monthly
    loans = [{'Name': str(l.get('Name') or f'Row {i}').strip(), 'Amount Left': parse_float(l.get('Amount Left')), 'APR': parse_apr(l.get('APR')),
              'Minimum Payment': parse_float(l.get('Minimum Payment')), 'Due Day': parse_due_day(l, 'Loans', i, str(l.get('Name') or f'Row {i}').strip(), alerts)}
             for i, l in enumerate(sheet.worksheet("Loans").get_all_records(), start=2) if check_required_fields(l, ['Amount Left', 'APR', 'Minimum Payment'], 'Loans', i, str(l.get('Name') or f'Row {i}').strip(), alerts) is None]
    monthly = [{'Name': str(m.get('Name') or f'Row {i}').strip(), 'Amount': parse_float(m.get('Amount')), 'Due Day': parse_due_day(m, 'MonthlyPayments', i, str(m.get('Name') or f'Row {i}').strip(), alerts)}
               for i, m in enumerate(sheet.worksheet("MonthlyPayments").get_all_records(), start=2) if check_required_fields(m, ['Amount'], 'MonthlyPayments', i, str(m.get('Name') or f'Row {i}').strip(), alerts) is None]

    # 5. Income
    inc_recs = sheet.worksheet("Income").get_all_records()
    if not inc_recs:
        raise ValueError("Worksheet 'Income' is empty.")
    income = []
    for idx, inc in enumerate(inc_recs, start=2):
        check_required_fields(inc, ['Type', 'Paycheck Amount'], 'Income', idx, f'Row {idx}', alerts)
        stype = str(inc.get('Type') or '').strip().lower()
        if stype and stype not in ('biweekly', 'semi-monthly', 'semimonthly', 'semi monthly'):
            alerts.append(f"Worksheet 'Income', row {idx}: 'Type' must be 'Biweekly' or 'Semi-Monthly' (got '{inc.get('Type')}').")
        inc_start = parse_date(inc.get('Start Date')) if stype == 'biweekly' else None
        if stype == 'biweekly' and not inc_start:
            alerts.append(f"Worksheet 'Income', row {idx}: 'Start Date' is required for Biweekly income.")
        amt2 = inc.get('Paycheck 2 Amount')
        income.append({'Name': str(inc.get('Name') or '').strip(), 'Type': inc.get('Type'), 'Start Date': inc_start,
                       'Paycheck Amount': parse_float(inc.get('Paycheck Amount')), 'Paycheck 2 Amount': parse_float(amt2) if amt2 not in (None, '') else None})

    if alerts:
        print("\n" + "!" * 75 + "\nALERT: MISSING OR INCOMPLETE INFORMATION DETECTED IN SPREADSHEET:")
        for a in alerts:
            print(f"  [!] {a}")
        print("!" * 75 + "\n")
        raise ValueError("Simulation halted: Required configuration fields are missing or invalid. See alerts above.")

    print("Data loaded successfully. Running Debt Minimizer optimization engine...")
    optimizer = DebtOptimizer(params, ccs, loans, monthly, income)
    schedule = optimizer.run()
    schedule.sort(key=lambda r: (r['Date'], -r['Amount']))
    buf = round(float(params['Checking Buffer']), 2)

    print(f"Optimization finished ({len(schedule)} transactions). Writing to 'Output' sheet...")
    try:
        output_ws = sheet.worksheet("Output")
    except Exception:
        output_ws = sheet.add_worksheet(title="Output", rows=len(schedule) + 10, cols=10)
    if output_ws.col_count < 7:
        output_ws.add_cols(7 - output_ws.col_count)

    output_ws.clear()
    rows = [["Date", "Description", "Amount", "Checking Balance", "", "", "Total Interest Paid"]]
    for idx, r in enumerate(schedule):
        row = [r['Date'], r['Description'], r['Amount'], r['Checking Balance']]
        if idx == 0:
            row.extend(["", "", round(optimizer.total_interest_paid, 2)])
        rows.append(row)
    if not schedule:
        rows.append(["", "", "", "", "", "", round(optimizer.total_interest_paid, 2)])

    output_ws.update(rows)
    apply_output_formatting(sheet, output_ws, len(rows), buf)
    print(f"Success! Schedule & total interest paid (${round(optimizer.total_interest_paid, 2):,.2f}) written to Google Sheet.")


def apply_output_formatting(sheet, output_ws, num_rows: int, buffer_val: float):
    """Applies deep red (< 0) and light red (< buffer) conditional formatting to Column D."""
    try:
        meta = sheet.fetch_sheet_metadata({'includeGridData': False})
        existing = sum(len(s.get('conditionalFormats', [])) for s in meta.get('sheets', []) if s.get('properties', {}).get('sheetId') == output_ws.id)
        requests = [{"deleteConditionalFormatRule": {"sheetId": output_ws.id, "index": 0}} for _ in range(existing)]

        def make_rule(val_str: str, bg: Dict[str, float], fg: Dict[str, float]):
            return {"addConditionalFormatRule": {"rule": {
                "ranges": [{"sheetId": output_ws.id, "startRowIndex": 1, "endRowIndex": num_rows, "startColumnIndex": 3, "endColumnIndex": 4}],
                "booleanRule": {
                    "condition": {"type": "NUMBER_LESS", "values": [{"userEnteredValue": val_str}]},
                    "format": {"backgroundColor": bg, "textFormat": {"foregroundColor": fg, "bold": True}}
                }
            }, "index": 0}}

        if buffer_val > 0:
            requests.append(make_rule(str(buffer_val), {"red": 1.0, "green": 0.85, "blue": 0.85}, {"red": 0.6, "green": 0.0, "blue": 0.0}))
        requests.append(make_rule("0", {"red": 0.85, "green": 0.25, "blue": 0.25}, {"red": 1.0, "green": 1.0, "blue": 1.0}))
        sheet.batch_update({"requests": requests})
    except Exception as e:
        print(f"Note: Could not apply conditional formatting to 'Output' sheet: {e}")


def parse_args():
    parser = argparse.ArgumentParser(description="Debt Minimization & Cash Flow Payment Schedule Optimizer")
    parser.add_argument("--test", action="store_true", help="Run offline test suite and exit.")
    parser.add_argument("--sheet", default="Bank Balance Script 2", help="Google Spreadsheet title.")
    parser.add_argument("--creds", default="credentials.json", help="OAuth credentials path.")
    parser.add_argument("--token", default="token.json", help="Cached token path.")
    parser.add_argument("--service-account", default=None, help="Service account JSON path.")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.test:
        run_self_tests()
        sys.exit(0)
    run_optimizer_from_sheet(args.sheet, args.creds, args.token, args.service_account)


if __name__ == "__main__":
    main()
