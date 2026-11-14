import datetime
from datetime import timedelta
import calendar
import math
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
import copy
import argparse
import sys
import os

# Optional imports for Google Sheets I/O
try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    gspread = None
    Credentials = None


# =====================================================================
# DATE UTILITIES
# =====================================================================

def clamp_day(year: int, month: int, day: int) -> int:
    """Clamps a day of month to the valid range for that month (e.g. Feb 31 -> Feb 28)."""
    max_days = calendar.monthrange(year, month)[1]
    return min(day, max_days)


def make_date(year: int, month: int, day: int) -> datetime.date:
    """Creates a date object with the day clamped to the valid range for that month."""
    return datetime.date(year, month, clamp_day(year, month, day))


def advance_month(d: datetime.date, target_day: int, months: int = 1) -> datetime.date:
    """Advances a date by N months while preserving or clamping to target_day."""
    total_months = d.month + months - 1
    new_year = d.year + (total_months // 12)
    new_month = (total_months % 12) + 1
    return make_date(new_year, new_month, target_day)


def get_first_due_date(start_date: datetime.date, due_day: int) -> datetime.date:
    """Finds the first upcoming due date on or after start_date."""
    d = make_date(start_date.year, start_date.month, due_day)
    return advance_month(d, due_day) if d < start_date else d


# =====================================================================
# DATA STRUCTURES (Optimized with slots for memory & performance)
# =====================================================================

@dataclass(slots=True)
class CreditCard:
    card: str
    issuer: str
    apr: float
    balance: float              # Regular balance subject to APR
    promo_balance: float        # Balance subject to 0% promo
    promo_end_date: Optional[datetime.date]
    statement_cycle_end_day: int  # Chase fixed cycle end day
    statement_balance: float    # Last statement balance
    grace_active: bool
    due_day: int
    grace_period_days: int      # For Amex, CapOne, Discover, X1
    cycle_end_nth_weekday: int  # For Citi
    min_floor: float
    annual_fee: float
    renewal_date: Optional[datetime.date]

    # Internal dynamic simulation state
    next_due: datetime.date = field(
        default_factory=lambda: datetime.date(2000, 1, 1))
    cycle_due_date: datetime.date = field(
        default_factory=lambda: datetime.date(2000, 1, 1))
    next_cycle_end: datetime.date = field(
        default_factory=lambda: datetime.date(2000, 1, 1))
    adb_reg_acc: float = 0.0
    days_in_cycle: int = 0
    amount_paid_this_cycle: float = 0.0
    remaining_min_due: float = 0.0
    # Tracks months with full statement balance paid; grace restores at 2
    consecutive_full_payments: int = 0


@dataclass(slots=True)
class Loan:
    name: str
    amount_left: float
    apr: float
    minimum_payment: float
    due_day: int

    # Internal dynamic simulation state
    next_due: datetime.date = field(
        default_factory=lambda: datetime.date(2000, 1, 1))
    remaining_min_due: float = 0.0
    accrued_interest: float = 0.0
    amount_paid_this_cycle: float = 0.0


@dataclass(slots=True)
class MonthlyPayment:
    name: str
    amount: float
    due_day: int
    next_due: datetime.date = field(
        default_factory=lambda: datetime.date(2000, 1, 1))


@dataclass(slots=True)
class IncomeSchedule:
    start_date: datetime.date
    paycheck_2: float
    paychecks_1_and_3: float


# =====================================================================
# SIMULATION ENGINE
# =====================================================================

class DebtOptimizer:
    def __init__(self, params: Dict[str, Any], ccs: List[Dict[str, Any]],
                 loans: List[Dict[str, Any]], monthly: List[Dict[str, Any]],
                 income: Dict[str, Any]):
        # Deepcopy to prevent mutating external caller data structures
        self.raw_params = copy.deepcopy(params)
        self.raw_ccs = copy.deepcopy(ccs)
        self.raw_loans = copy.deepcopy(loans)
        self.raw_monthly = copy.deepcopy(monthly)
        self.raw_income = copy.deepcopy(income)

        self.start_date = self.raw_params['Start Date']
        self.end_date = self.raw_params['End Date']
        self.checking = float(self.raw_params['Initial Checking Balance'])
        self.buffer = float(self.raw_params['Checking Buffer'])
        self.current_date = self.start_date

        self.income = IncomeSchedule(
            start_date=self.raw_income['Start Date'],
            paycheck_2=float(self.raw_income['Paycheck 2']),
            paychecks_1_and_3=float(self.raw_income['Paychecks 1 and 3'])
        )

        self.schedule_log: List[Dict[str, Any]] = []
        self.total_interest_paid: float = 0.0

        # Build Monthly Payments
        self.monthly: List[MonthlyPayment] = []
        for m in self.raw_monthly:
            due_day = int(m['Due Day'])
            next_due = get_first_due_date(self.start_date, due_day)
            self.monthly.append(MonthlyPayment(
                name=m['Name'],
                amount=float(m['Amount']),
                due_day=due_day,
                next_due=next_due
            ))

        # Build Loans
        self.loans: List[Loan] = []
        for l in self.raw_loans:
            due_day = int(l['Due Day'])
            next_due = get_first_due_date(self.start_date, due_day)
            amt_left = float(l['Amount Left'])
            min_pay = float(l['Minimum Payment'])
            self.loans.append(Loan(
                name=l['Name'],
                amount_left=amt_left,
                apr=float(l['APR']),
                minimum_payment=min_pay,
                due_day=due_day,
                next_due=next_due,
                remaining_min_due=min(amt_left, min_pay)
            ))

        # Build Credit Cards
        self.ccs: List[CreditCard] = []
        for c in self.raw_ccs:
            due_day = int(c['Due Day'])
            total_bal = float(c.get('Balance', 0.0))
            promo_bal = float(c.get('Promo Balance', 0.0))
            # Spec 2d: If there is a promo balance, regular balance subject to standard APR is (Balance - Promo Balance)
            reg_bal = max(0.0, total_bal -
                          promo_bal) if promo_bal > 0 else total_bal

            p_end = c.get('Promo End Date')
            ren_date = c.get('Renewal Date')
            if isinstance(ren_date, str):
                ren_date = parse_date(ren_date)

            cc_obj = CreditCard(
                card=c['Card'],
                issuer=c['Issuer'],
                apr=float(c['APR']),
                balance=reg_bal,
                promo_balance=promo_bal,
                promo_end_date=p_end,
                statement_cycle_end_day=int(
                    c.get('Statement Cycle End Day', 0) or 0),
                statement_balance=float(c.get('Statement Balance', 0.0)),
                grace_active=bool(c.get('Grace Active', False)),
                due_day=due_day,
                grace_period_days=int(c.get('Grace Period Days', 0) or 0),
                cycle_end_nth_weekday=int(
                    c.get('Cycle End Nth Weekday', 0) or 0),
                min_floor=float(c.get('Min Floor', 0.0)),
                annual_fee=float(c.get('Annual Fee', 0.0) or 0.0),
                renewal_date=ren_date
            )

            # 1. Determine next upcoming due date >= start_date
            next_due = get_first_due_date(self.start_date, due_day)
            cc_obj.next_due = next_due

            # 2. Cycle end date for statement currently due
            due_cycle_end = self.calculate_cycle_end_date(
                cc_obj.issuer, next_due, cc_obj.grace_period_days,
                cc_obj.cycle_end_nth_weekday, cc_obj.statement_cycle_end_day
            )

            # 3. Determine next cycle end date & previous cycle end date for ADB initialization
            if due_cycle_end >= self.start_date:
                # The cycle for next_due has not closed yet; it closes on due_cycle_end
                cc_obj.cycle_due_date = next_due
                cc_obj.next_cycle_end = due_cycle_end
                prior_due = advance_month(next_due, due_day, -1)
                prev_cycle_end = self.calculate_cycle_end_date(
                    cc_obj.issuer, prior_due, cc_obj.grace_period_days,
                    cc_obj.cycle_end_nth_weekday, cc_obj.statement_cycle_end_day
                )
            else:
                # The cycle for next_due already closed before start_date
                following_due = advance_month(next_due, due_day)
                next_cycle_end = self.calculate_cycle_end_date(
                    cc_obj.issuer, following_due, cc_obj.grace_period_days,
                    cc_obj.cycle_end_nth_weekday, cc_obj.statement_cycle_end_day
                )
                while next_cycle_end < self.start_date:
                    due_cycle_end = next_cycle_end
                    following_due = advance_month(following_due, due_day)
                    next_cycle_end = self.calculate_cycle_end_date(
                        cc_obj.issuer, following_due, cc_obj.grace_period_days,
                        cc_obj.cycle_end_nth_weekday, cc_obj.statement_cycle_end_day
                    )
                cc_obj.cycle_due_date = following_due
                cc_obj.next_cycle_end = next_cycle_end
                prev_cycle_end = due_cycle_end

            # 4. Spec 1a: Initialize ADB for mid-cycle start
            if prev_cycle_end < self.start_date:
                elapsed_days = (self.start_date - prev_cycle_end).days
                cc_obj.days_in_cycle = max(0, elapsed_days)
                cc_obj.adb_reg_acc = cc_obj.balance * cc_obj.days_in_cycle

            # 5. Calculate initial minimum payment from starting statement balance
            cc_obj.remaining_min_due = self.calc_cc_min_payment(
                cc_obj.issuer, cc_obj.statement_balance, cc_obj.min_floor, 0.0, 0.0
            )

            self.ccs.append(cc_obj)

    def get_next_payday(self) -> datetime.date:
        """Finds the next payday strictly after current_date in O(1) time."""
        if self.current_date < self.income.start_date:
            return self.income.start_date
        cycles = (self.current_date - self.income.start_date).days // 14
        return self.income.start_date + timedelta(days=(cycles + 1) * 14)

    def is_payday(self, d: datetime.date) -> bool:
        """Determines if date d is a payday in O(1) time."""
        return d >= self.income.start_date and (d - self.income.start_date).days % 14 == 0

    def get_paycheck_amount(self, pay_date: datetime.date) -> float:
        """Determines paycheck amount in O(1) time based on calendar day."""
        return self.income.paycheck_2 if 15 <= pay_date.day <= 28 else self.income.paychecks_1_and_3

    # TODO: X1 calculates cycle end date using 'grace_days', but with a rollback to Friday if the calculated cycle end date is a Saturday.
    @staticmethod
    def calculate_cycle_end_date(issuer: str, due_date: datetime.date,
                                 grace_days: int, citi_weekday: int, chase_day: int) -> datetime.date:
        """Calculates statement cycle end date based on issuer rules."""
        if issuer == 'Chase':
            # Chase uses fixed day of the month
            if chase_day < due_date.day and (due_date.day - chase_day) >= 15:
                y, m = due_date.year, due_date.month
            else:
                m = due_date.month - 1
                y = due_date.year
                if m == 0:
                    m, y = 12, y - 1
            return make_date(y, m, chase_day)
        elif issuer == 'Citibank':
            # Nth weekday of prior month
            m = due_date.month - 1
            y = due_date.year
            if m == 0:
                m, y = 12, y - 1
            count = 0
            d = datetime.date(y, m, 1)
            while count < 24:
                if d.month != m:
                    raise ValueError(
                        f"Citibank Cycle End Nth Weekday ({citi_weekday}) exceeds the number "
                        f"of business days in {y}-{m:02d}."
                    )
                if d.weekday() < 5:  # Monday = 0 ... Friday = 4
                    count += 1
                    if count == citi_weekday:
                        return d
                d += timedelta(days=1)
        else:
            # Amex, Capital One, Discover, X1
            d = due_date - timedelta(days=grace_days)
            if issuer == 'X1' and d.weekday == 5:
                return d - timedelta(days=1)
            return d

    @staticmethod
    def calc_cc_min_payment(issuer: str, statement_balance: float, min_floor: float,
                            interest_charged: float = 0.0, fees: float = 0.0) -> float:
        """Calculates credit card minimum payment per Spec 6."""
        if statement_balance <= 0:
            return 0.0
        if statement_balance < min_floor:
            return statement_balance

        principal = max(0.0, statement_balance - interest_charged - fees)

        if issuer == 'American Express':
            # trunc(max(Min Floor, Interest + Fees + 1% of Principal, 2% of Principal), 2)
            calc_amt = 100*int(max(min_floor, interest_charged +
                               fees + (0.01 * principal), 0.02 * principal)) / 100
        elif issuer == 'Discover':
            # Round(max(Min Floor, 20 + Interest + Fees, 2% of statement balance))
            calc_amt = int(
                round(max(min_floor, 20.0 + interest_charged + fees, 0.02 * statement_balance)))
        elif issuer == 'Capital One':
            # Capital One: Interest + Fees + Round(max(Min Floor, 1% of Principal))
            calc_amt = interest_charged + fees + \
                round(max(min_floor, 0.01 * principal))
        elif issuer == 'Chase':
            # Chase: Round(Interest + Fees + max(Min Floor, 1% of Principal), 2)
            calc_amt = interest_charged + fees + \
                max(min_floor, 0.01 * principal)
        else:
            # Citibank, X1: Round(Interest + Fees + max(Min Floor, 1% of Principal))
            calc_amt = round(interest_charged + fees +
                             max(min_floor, 0.01 * principal))

        return min(statement_balance, round(calc_amt, 2))

    def log_transaction(self, description: str, amount: float):
        """Logs an actual checking account cash flow transaction."""
        amt = round(amount, 2)
        self.checking = round(self.checking + amt, 2)
        # Prevent floating-point sub-cent drift around buffer floor
        if abs(self.checking - self.buffer) < 0.005:
            self.checking = round(self.buffer, 2)
        self.schedule_log.append({
            'Date': self.current_date.strftime('%Y-%m-%d'),
            'Description': description,
            'Amount': amt,
            'Checking Balance': self.checking
        })

    def calculate_disposable_cash(self) -> float:
        """
        Calculates safe disposable cash for extra debt payments.
        Simulates forward baseline cash flows from current_date to end_date
        to guarantee checking balance never breaches buffer at any future point.
        """
        if self.checking <= self.buffer:
            return 0.0

        proj_bal = self.checking
        min_margin = proj_bal - self.buffer

        # Lightweight forward trackers
        m_trackers = [{'amount': m.amount, 'due_day': m.due_day,
                       'next_due': m.next_due} for m in self.monthly]
        loan_trackers = [{
            'bal': l.amount_left + l.accrued_interest,
            'due_day': l.due_day,
            'next_due': l.next_due,
            'rem_min': l.remaining_min_due,
            'min_pay': l.minimum_payment
        } for l in self.loans]
        cc_trackers = [{
            'bal': cc.balance + cc.promo_balance,
            'due_day': cc.due_day,
            'next_due': cc.next_due,
            'rem_min': cc.remaining_min_due,
            'stmt_bal': cc.statement_balance,
            'paid_cycle': cc.amount_paid_this_cycle,
            'grace': cc.grace_active,
            'min_floor': cc.min_floor,
            'issuer': cc.issuer,
            'annual_fee': cc.annual_fee,
            'renewal_date': cc.renewal_date
        } for cc in self.ccs]

        d = self.current_date
        while d <= self.end_date:
            # 0. Annual fee posting on future dates
            if d > self.current_date:
                for cc in cc_trackers:
                    if cc['annual_fee'] > 0 and cc['renewal_date']:
                        target_day = clamp_day(
                            d.year, cc['renewal_date'].month, cc['renewal_date'].day)
                        if d.month == cc['renewal_date'].month and d.day == target_day:
                            cc['bal'] += cc['annual_fee']

            # 1. Inflow on future paydays (current_date paycheck is already in self.checking)
            if d > self.current_date and self.is_payday(d):
                proj_bal += self.get_paycheck_amount(d)

            # 2. Monthly payments
            for m in m_trackers:
                if d == m['next_due']:
                    proj_bal -= m['amount']
                    m['next_due'] = advance_month(m['next_due'], m['due_day'])

            # 3. Loan payments
            for l in loan_trackers:
                if l['bal'] > 0.005 and d == l['next_due']:
                    due_amt = min(l['bal'], l['rem_min'])
                    proj_bal -= due_amt
                    l['bal'] = max(0.0, l['bal'] - due_amt)
                    l['rem_min'] = min(l['bal'], l['min_pay'])
                    l['next_due'] = advance_month(l['next_due'], l['due_day'])

            # 4. Credit card payments
            for cc in cc_trackers:
                if cc['bal'] > 0.005 and d == cc['next_due']:
                    if cc['grace']:
                        needed = max(
                            cc['rem_min'], cc['stmt_bal'] - cc['paid_cycle'])
                        due_amt = min(needed, cc['bal'])
                    else:
                        due_amt = min(cc['rem_min'], cc['bal'])
                    proj_bal -= due_amt
                    cc['bal'] = max(0.0, cc['bal'] - due_amt)
                    cc['stmt_bal'] = cc['bal']
                    cc['paid_cycle'] = 0.0
                    cc['rem_min'] = self.calc_cc_min_payment(
                        cc['issuer'], cc['stmt_bal'], cc['min_floor'], 0.0, 0.0)
                    cc['next_due'] = advance_month(
                        cc['next_due'], cc['due_day'])

            # Evaluate cushion
            margin = proj_bal - self.buffer
            if margin < min_margin:
                min_margin = margin
                if min_margin <= 0.01:
                    return 0.0

            d += timedelta(days=1)

        # Floor to cents so we never overspend by even half a cent into the buffer
        safe_cash = math.floor(round(min_margin, 4) * 100.0) / 100.0
        return max(0.0, safe_cash)

    def run(self) -> List[Dict[str, Any]]:
        while self.current_date <= self.end_date:
            # -------------------------------------------------------------
            # 1. Annual Fees Posting on Renewal Date (as regular purchase)
            # -------------------------------------------------------------
            for cc in self.ccs:
                if cc.annual_fee > 0 and cc.renewal_date:
                    target_day = clamp_day(
                        self.current_date.year, cc.renewal_date.month, cc.renewal_date.day)
                    if self.current_date.month == cc.renewal_date.month and self.current_date.day == target_day:
                        cc.balance += cc.annual_fee

            # -------------------------------------------------------------
            # 2. Income Processing
            # -------------------------------------------------------------
            if self.is_payday(self.current_date):
                pay_amt = self.get_paycheck_amount(self.current_date)
                self.log_transaction("Paycheck Received", pay_amt)

            # -------------------------------------------------------------
            # 3. Daily Accruals & Promo Expirations
            # -------------------------------------------------------------
            for loan in self.loans:
                if loan.amount_left > 0.005:
                    loan.accrued_interest += loan.amount_left * \
                        (loan.apr / 365.0)
                else:
                    loan.amount_left = 0.0
                    loan.accrued_interest = 0.0
                    loan.remaining_min_due = 0.0

            for cc in self.ccs:
                # Promo expiration
                if cc.promo_end_date and self.current_date > cc.promo_end_date:
                    cc.balance += cc.promo_balance
                    cc.promo_balance = 0.0
                    cc.promo_end_date = None

                if cc.balance < 0.005:
                    cc.balance = 0.0
                if cc.promo_balance < 0.005:
                    cc.promo_balance = 0.0

                cc.adb_reg_acc += cc.balance
                cc.days_in_cycle += 1

            # -------------------------------------------------------------
            # 4. Look-Ahead Optimizer & Extra Payments
            # -------------------------------------------------------------
            # Run extra payment optimizer when checking balance exceeds buffer
            if self.checking > self.buffer:
                disposable = self.calculate_disposable_cash()

                # Waterfall extra payments to highest APR interest-bearing debt
                while disposable > 0.01:
                    candidates = []
                    for cc in self.ccs:
                        # Extra payments only target interest-bearing balance
                        if cc.balance > 0 and not cc.grace_active and cc.apr > 0:
                            candidates.append(('cc', cc.apr, cc))
                    for loan in self.loans:
                        if loan.amount_left > 0 and loan.apr > 0:
                            candidates.append(('loan', loan.apr, loan))

                    if not candidates:
                        break

                    candidates.sort(key=lambda x: x[1], reverse=True)
                    target_type, _, target = candidates[0]

                    # Ensure extra payment never dips checking below buffer
                    max_extra = max(0.0, math.floor(
                        round(self.checking - self.buffer, 4) * 100.0) / 100.0)
                    if max_extra <= 0.005:
                        break
                    disposable = min(disposable, max_extra)

                    if target_type == 'cc':
                        pay_amt = min(disposable, target.balance)
                        pay_amt = min(pay_amt, max_extra)
                        target.balance -= pay_amt
                        if target.balance < 0.005:
                            target.balance = 0.0
                        # Rule 9: Early payment updates remaining minimum
                        target.remaining_min_due = max(
                            0.0, target.remaining_min_due - pay_amt)
                        target.amount_paid_this_cycle += pay_amt
                        self.log_transaction(
                            f"Extra payment to {target.card}", -pay_amt)
                        disposable -= pay_amt
                    else:
                        total_loan_owed = target.amount_left + target.accrued_interest
                        pay_amt = min(disposable, total_loan_owed)
                        pay_amt = min(pay_amt, max_extra)
                        # Simple interest: pay accrued interest first, then reduce principal
                        int_pay = min(pay_amt, target.accrued_interest)
                        target.accrued_interest -= int_pay
                        target.amount_left -= (pay_amt - int_pay)
                        self.total_interest_paid += int_pay
                        if target.amount_left < 0.005:
                            target.amount_left = 0.0
                            target.accrued_interest = 0.0
                            target.remaining_min_due = 0.0
                        # Rule 9: Early payment updates remaining minimum
                        target.remaining_min_due = max(
                            0.0, target.remaining_min_due - pay_amt)
                        target.amount_paid_this_cycle += pay_amt
                        self.log_transaction(
                            f"Extra payment to {target.name}", -pay_amt)
                        disposable -= pay_amt

            # -------------------------------------------------------------
            # 5. Process Mandatory Payments Due Today
            # -------------------------------------------------------------
            # Monthly Payments
            for m in self.monthly:
                if self.current_date == m.next_due:
                    self.log_transaction(
                        f"Monthly Payment: {m.name}", -m.amount)
                    m.next_due = advance_month(m.next_due, m.due_day)

            # Loan Payments
            for loan in self.loans:
                if loan.amount_left > 0.005 and self.current_date == loan.next_due:
                    # Due is remaining minimum or remaining balance (Rule 10)
                    total_loan_owed = loan.amount_left + loan.accrued_interest
                    due_amt = min(total_loan_owed, loan.remaining_min_due)
                    # Cap at checking if deficit
                    pay_amt = min(due_amt, self.checking)
                    if pay_amt > 0:
                        # Simple interest: pay accrued interest first, then reduce principal
                        int_pay = min(pay_amt, loan.accrued_interest)
                        loan.accrued_interest -= int_pay
                        loan.amount_left -= (pay_amt - int_pay)
                        self.total_interest_paid += int_pay
                        if loan.amount_left < 0.005:
                            loan.amount_left = 0.0
                            loan.accrued_interest = 0.0
                            loan.remaining_min_due = 0.0
                        self.log_transaction(
                            f"Loan Payment: {loan.name}", -pay_amt)
                    loan.remaining_min_due = min(
                        loan.amount_left + loan.accrued_interest, loan.minimum_payment)
                    loan.amount_paid_this_cycle = 0.0
                    loan.next_due = advance_month(loan.next_due, loan.due_day)

            # Credit Card Payments Due Today
            for cc in self.ccs:
                if self.current_date == cc.next_due:
                    total_owed = round(cc.balance + cc.promo_balance, 2)
                    if total_owed <= 0.005:
                        cc.balance = 0.0
                        cc.promo_balance = 0.0
                        cc.remaining_min_due = 0.0
                        # Fix H: Even if the balance was zeroed by earlier extra payments,
                        # a full payment was effectively made — update the consecutive streak.
                        if not cc.grace_active:
                            if cc.statement_balance > 0.01:
                                cc.consecutive_full_payments += 1
                            if cc.consecutive_full_payments >= 2:
                                cc.grace_active = True
                                cc.consecutive_full_payments = 0
                        cc.amount_paid_this_cycle = 0.0
                        cc.next_due = advance_month(cc.next_due, cc.due_day)
                        continue

                    # Calculate target payment
                    if cc.grace_active:
                        target_pay = max(
                            cc.remaining_min_due, cc.statement_balance - cc.amount_paid_this_cycle)
                        cash_above_buf = max(0.0, math.floor(
                            round(self.checking - self.buffer, 4) * 100.0) / 100.0)
                        # Rule 4 & 11: Pay full statement balance if cash allows; else pay down to buffer,
                        # but always at least minimum payment (dipping into buffer if necessary)
                        pay_amt = max(cc.remaining_min_due, min(
                            target_pay, cash_above_buf))
                        # Buffer dip / cap at checking
                        pay_amt = min(pay_amt, self.checking)
                    else:
                        pay_amt = min(cc.remaining_min_due, self.checking)

                    # Fix F: Cap payment at total remaining card balance
                    pay_amt = min(pay_amt, total_owed)

                    if pay_amt > 0:
                        # Rule 7: Minimum payment goes to lowest APR first (Promo)
                        # Any extra portion above minimum goes to highest APR first
                        min_portion = min(pay_amt, cc.remaining_min_due)
                        extra_portion = pay_amt - min_portion

                        promo_pay = min(min_portion, cc.promo_balance)
                        cc.promo_balance -= promo_pay
                        reg_min_pay = min_portion - promo_pay
                        cc.balance = max(0.0, cc.balance - reg_min_pay)

                        reg_extra_pay = min(extra_portion, cc.balance)
                        cc.balance -= reg_extra_pay
                        rem_extra = extra_portion - reg_extra_pay
                        cc.promo_balance = max(
                            0.0, cc.promo_balance - rem_extra)

                        # Fix J: Clamp sub-cent residuals
                        if cc.balance < 0.005:
                            cc.balance = 0.0
                        if cc.promo_balance < 0.005:
                            cc.promo_balance = 0.0

                        cc.amount_paid_this_cycle += pay_amt
                        cc.remaining_min_due = max(
                            0.0, cc.remaining_min_due - min_portion)
                        self.log_transaction(f"Payment to {cc.card}", -pay_amt)

                    # Rule 3: Check Grace Period status
                    if cc.grace_active:
                        if cc.amount_paid_this_cycle < cc.statement_balance - 0.01:
                            cc.grace_active = False
                            cc.consecutive_full_payments = 0
                            # Retroactive interest applied to CC balance (NOT checking account)
                            if cc.grace_period_days > 0:
                                penalty_days = cc.grace_period_days
                            else:
                                prev_cycle_end = self.calculate_cycle_end_date(
                                    cc.issuer, cc.next_due, cc.grace_period_days,
                                    cc.cycle_end_nth_weekday, cc.statement_cycle_end_day
                                )
                                penalty_days = max(
                                    1, (cc.next_due - prev_cycle_end).days)
                            penalty = cc.statement_balance * \
                                (cc.apr / 365.0) * penalty_days
                            cc.balance += penalty
                            self.total_interest_paid += penalty
                    else:
                        # Fix H: Grace restores only after 2 consecutive months of full statement payment.
                        # Interest accrues on lingering charges and new purchases between cycles, so a
                        # single full payment does not clear all interest-bearing balances; two in a row does.
                        paid_in_full = cc.statement_balance > 0.01 and cc.amount_paid_this_cycle >= cc.statement_balance - 0.01
                        if paid_in_full:
                            cc.consecutive_full_payments += 1
                        else:
                            cc.consecutive_full_payments = 0
                        if cc.consecutive_full_payments >= 2:
                            cc.grace_active = True
                            cc.consecutive_full_payments = 0

                    # Advance next due date
                    cc.next_due = advance_month(cc.next_due, cc.due_day)
                    cc.amount_paid_this_cycle = 0.0

            # -------------------------------------------------------------
            # 6. Statement Cycle End Processing
            # -------------------------------------------------------------
            for cc in self.ccs:
                if self.current_date == cc.next_cycle_end:
                    interest = 0.0
                    if not cc.grace_active and cc.days_in_cycle > 0:
                        adb_reg = cc.adb_reg_acc / cc.days_in_cycle
                        interest = adb_reg * \
                            (cc.apr / 365.0) * cc.days_in_cycle
                        cc.balance += interest
                        self.total_interest_paid += interest

                    cc.statement_balance = round(
                        cc.balance + cc.promo_balance, 2)
                    cc.remaining_min_due = self.calc_cc_min_payment(
                        cc.issuer, cc.statement_balance, cc.min_floor, interest, 0.0
                    )

                    # Reset cycle accumulators
                    cc.adb_reg_acc = 0.0
                    cc.days_in_cycle = 0
                    cc.amount_paid_this_cycle = 0.0  # Fix G: Reset for the new statement cycle

                    # Advance cycle_due_date and calculate next_cycle_end independently
                    cc.cycle_due_date = advance_month(
                        cc.cycle_due_date, cc.due_day)
                    cc.next_cycle_end = self.calculate_cycle_end_date(
                        cc.issuer, cc.cycle_due_date, cc.grace_period_days,
                        cc.cycle_end_nth_weekday, cc.statement_cycle_end_day
                    )

            self.current_date += timedelta(days=1)

        return self.schedule_log


# =====================================================================
# COMPREHENSIVE SELF-TEST SUITE (All 21+ Individual Test Cases)
# =====================================================================

def run_self_tests():
    """Executes the comprehensive mathematical test suite in test_debt_optimizer.py."""
    from test_debt_optimizer import run_self_tests as _run_tests
    _run_tests()


# =====================================================================
# GOOGLE SHEETS I/O & PARSING HELPERS
# =====================================================================

def parse_date(val: Any) -> Optional[datetime.date]:
    """Robust date parsing for Google Sheets inputs."""
    if not val:
        return None
    if isinstance(val, datetime.date) and not isinstance(val, datetime.datetime):
        return val
    if isinstance(val, datetime.datetime):
        return val.date()

    val_str = str(val).strip()
    if val_str.lower() in ('none', 'n/a', '-', '', 'null'):
        return None

    # Handle recurring MM/DD format (default year 2000)
    if '/' in val_str and len(val_str.split('/')) == 2:
        try:
            m, d = map(int, val_str.split('/'))
            return datetime.date(2000, m, d)
        except ValueError:
            pass

    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y', '%d/%m/%Y'):
        try:
            return datetime.datetime.strptime(val_str, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Could not parse date string: {val_str}")


def parse_float(val: Any, default: float = 0.0) -> float:
    """Cleans currency strings, commas, and percentage signs to float."""
    if val is None or val == '':
        return default
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).replace('$', '').replace(',', '').replace('%', '').strip()
    try:
        f = float(s)
        # If original string had '%', convert percentage to decimal
        if '%' in str(val):
            return f / 100.0
        return f
    except ValueError:
        return default


def parse_apr(val: Any) -> float:
    """Parses APR ensuring decimal format (e.g. 24.99% or 24.99 -> 0.2499)."""
    f = parse_float(val)
    if f > 1.0:
        f /= 100.0
    return f


def get_google_sheets_client(creds_path: str = "credentials.json",
                             token_path: str = "token.json",
                             service_account_path: Optional[str] = None):
    """
    Connects to Google Sheets using either:
    1. OAuth 2.0 User Flow (gspread.oauth) - Recommended & Secure
    2. Service Account Key file (Credentials.from_service_account_file) - Fallback
    """
    if gspread is None:
        raise ImportError(
            "gspread library is not installed. Please run: pip install gspread google-auth-oauthlib")

    # If user explicitly requested service account or only service_account.json exists
    if service_account_path and os.path.exists(service_account_path):
        print(f"Connecting via Service Account: {service_account_path}...")
        scope = ["https://www.googleapis.com/auth/spreadsheets",
                 "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(
            service_account_path, scopes=scope)
        return gspread.authorize(creds)

    # Primary / Recommended: OAuth 2.0 Desktop App Flow
    print(
        f"Connecting via OAuth 2.0 (credentials: {creds_path}, token: {token_path})...")
    if not os.path.exists(creds_path) and not os.path.exists(token_path):
        # Fallback check if service_account.json is present
        if os.path.exists("service_account.json"):
            print(
                "Note: credentials.json not found, falling back to service_account.json...")
            scope = ["https://www.googleapis.com/auth/spreadsheets",
                     "https://www.googleapis.com/auth/drive"]
            creds = Credentials.from_service_account_file(
                "service_account.json", scopes=scope)
            return gspread.authorize(creds)
        raise FileNotFoundError(
            f"OAuth 2.0 client file '{creds_path}' not found.\n"
            "Download your Desktop Client ID JSON from GCP Console > Credentials, save it as 'credentials.json',\n"
            "or specify --service-account service_account.json"
        )

    return gspread.oauth(
        credentials_filename=creds_path,
        authorized_user_filename=token_path
    )


def run_optimizer_from_sheet(sheet_name: str, creds_path: str, token_path: str, service_account_path: Optional[str]):
    """Reads data from Google Sheets, runs the optimization, and writes schedule output."""
    client = get_google_sheets_client(
        creds_path, token_path, service_account_path)

    try:
        sheet = client.open(sheet_name)
    except Exception as e:
        print(f"Error opening Google Sheet '{sheet_name}': {e}")
        return

    print("Loading data from worksheets...")
    alerts = []

    # 1. Parameters
    param_records = sheet.worksheet("Parameters").get_all_records()
    if not param_records:
        raise ValueError("Worksheet 'Parameters' is empty.")
    p = param_records[0]
    start_d = parse_date(p.get('Start Date'))
    end_d = parse_date(p.get('End Date'))
    if not start_d:
        alerts.append(
            "Worksheet 'Parameters': 'Start Date' is missing or could not be parsed.")
    if not end_d:
        alerts.append(
            "Worksheet 'Parameters': 'End Date' is missing or could not be parsed.")
    if 'Initial Checking Balance' not in p or p.get('Initial Checking Balance') == '':
        alerts.append(
            "Worksheet 'Parameters': 'Initial Checking Balance' is missing.")
    if 'Checking Buffer' not in p or p.get('Checking Buffer') == '':
        alerts.append(
            "Worksheet 'Parameters': 'Checking Buffer' is missing (defaulting to 0.0).")

    params = {
        'Start Date': start_d,
        'End Date': end_d,
        'Initial Checking Balance': parse_float(p.get('Initial Checking Balance', 0.0)),
        'Checking Buffer': parse_float(p.get('Checking Buffer', 0.0))
    }

    # 2. Credit Cards
    cc_records = sheet.worksheet("CreditCards").get_all_records()
    ccs = []
    for idx, c in enumerate(cc_records, start=2):
        card_name = str(c.get('Card', f'Row {idx}')).strip()
        issuer = str(c.get('Issuer', '')).strip()
        if not issuer:
            alerts.append(
                f"Worksheet 'CreditCards', row {idx} ({card_name}): 'Issuer' is missing.")
        elif issuer == 'Chase' and not c.get('Statement Cycle End Day'):
            alerts.append(
                f"Worksheet 'CreditCards', row {idx} ({card_name}): 'Statement Cycle End Day' is required for Chase cards.")
        elif issuer == 'Citibank' and not c.get('Cycle End Nth Weekday'):
            alerts.append(
                f"Worksheet 'CreditCards', row {idx} ({card_name}): 'Cycle End Nth Weekday' is required for Citibank cards.")
        elif issuer in ('American Express', 'Capital One', 'Discover', 'X1') and not c.get('Grace Period Days'):
            alerts.append(
                f"Worksheet 'CreditCards', row {idx} ({card_name}): 'Grace Period Days' is required for {issuer} cards.")
        if 'Due Day' not in c or c.get('Due Day') == '':
            alerts.append(
                f"Worksheet 'CreditCards', row {idx} ({card_name}): 'Due Day' is missing.")

        ccs.append({
            'Card': card_name,
            'Issuer': issuer,
            'APR': parse_apr(c.get('APR', 0)),
            'Balance': parse_float(c.get('Balance', 0)),
            'Promo Balance': parse_float(c.get('Promo Balance', 0)),
            'Promo End Date': parse_date(c.get('Promo End Date')),
            'Statement Cycle End Day': int(parse_float(c.get('Statement Cycle End Day', 0))),
            'Statement Balance': parse_float(c.get('Statement Balance', 0)),
            'Grace Active': str(c.get('Grace Active', '')).strip().upper() == 'TRUE',
            'Due Day': int(parse_float(c.get('Due Day', 1))),
            'Grace Period Days': int(parse_float(c.get('Grace Period Days', 0))),
            'Cycle End Nth Weekday': int(parse_float(c.get('Cycle End Nth Weekday', 0))),
            'Min Floor': parse_float(c.get('Min Floor', 0)),
            'Annual Fee': parse_float(c.get('Annual Fee', 0)),
            'Renewal Date': parse_date(c.get('Renewal Date'))
        })

    # 3. Loans
    loan_records = sheet.worksheet("Loans").get_all_records()
    loans = []
    for idx, l in enumerate(loan_records, start=2):
        loan_name = str(l.get('Name', f'Row {idx}')).strip()
        if 'Due Day' not in l or l.get('Due Day') == '':
            alerts.append(
                f"Worksheet 'Loans', row {idx} ({loan_name}): 'Due Day' is missing.")
        if 'Minimum Payment' not in l or l.get('Minimum Payment') == '':
            alerts.append(
                f"Worksheet 'Loans', row {idx} ({loan_name}): 'Minimum Payment' is missing.")
        loans.append({
            'Name': loan_name,
            'Amount Left': parse_float(l.get('Amount Left', 0)),
            'APR': parse_apr(l.get('APR', 0)),
            'Minimum Payment': parse_float(l.get('Minimum Payment', 0)),
            'Due Day': int(parse_float(l.get('Due Day', 1)))
        })

    # 4. Monthly Payments
    monthly_records = sheet.worksheet("MonthlyPayments").get_all_records()
    monthly = []
    for idx, m in enumerate(monthly_records, start=2):
        m_name = str(m.get('Name', f'Row {idx}')).strip()
        if 'Due Day' not in m or m.get('Due Day') == '':
            alerts.append(
                f"Worksheet 'MonthlyPayments', row {idx} ({m_name}): 'Due Day' is missing.")
        if 'Amount' not in m or m.get('Amount') == '':
            alerts.append(
                f"Worksheet 'MonthlyPayments', row {idx} ({m_name}): 'Amount' is missing.")
        monthly.append({
            'Name': m_name,
            'Amount': parse_float(m.get('Amount', 0)),
            'Due Day': int(parse_float(m.get('Due Day', 1)))
        })

    # 5. Income
    income_records = sheet.worksheet("Income").get_all_records()
    if not income_records:
        raise ValueError("Worksheet 'Income' is empty.")
    inc = income_records[0]
    inc_start = parse_date(inc.get('Start Date'))
    if not inc_start:
        alerts.append(
            "Worksheet 'Income': 'Start Date' is missing or could not be parsed.")
    income = {
        'Start Date': inc_start,
        'Paycheck 2': parse_float(inc.get('Paycheck 2', 0)),
        'Paychecks 1 and 3': parse_float(inc.get('Paychecks 1 and 3', 0))
    }

    # Alert user if any missing or incomplete information was detected
    if alerts:
        print("\n" + "!" * 75)
        print("ALERT: MISSING OR INCOMPLETE INFORMATION DETECTED IN SPREADSHEET:")
        for a in alerts:
            print(f"  [!] {a}")
        print("!" * 75 + "\n")
        if not params['Start Date'] or not params['End Date'] or not income['Start Date']:
            raise ValueError(
                "Simulation halted: Required start/end dates are missing. See alerts above.")

    print("Data loaded successfully. Running Debt Minimizer optimization engine...")
    optimizer = DebtOptimizer(params, ccs, loans, monthly, income)
    schedule = optimizer.run()

    # Format & Sort output ledger:
    # 1. Date ascending
    # 2. Within same date, Amount descending
    schedule.sort(key=lambda r: (r['Date'], -r['Amount']))

    # Recalculate running checking balance so ledger amounts are strictly cumulative
    running_bal = round(float(params['Initial Checking Balance']), 2)
    buf = round(float(params.get('Checking Buffer', 0.0)), 2)
    for r in schedule:
        running_bal = round(running_bal + r['Amount'], 2)
        if abs(running_bal - buf) < 0.005:
            running_bal = buf
        r['Checking Balance'] = running_bal

    print(
        f"Optimization finished ({len(schedule)} transactions). Writing to 'Output' sheet...")
    try:
        output_ws = sheet.worksheet("Output")
    except Exception:
        output_ws = sheet.add_worksheet(
            title="Output", rows=len(schedule) + 10, cols=10)

    # Ensure sheet has at least 7 columns for column G
    if output_ws.col_count < 7:
        output_ws.add_cols(7 - output_ws.col_count)

    output_ws.clear()
    headers = ["Date", "Description", "Amount",
               "Checking Balance", "", "", "Total Interest Paid"]
    rows = [headers]
    for idx, r in enumerate(schedule):
        row = [r['Date'], r['Description'], r['Amount'], r['Checking Balance']]
        if idx == 0:
            row.extend(["", "", round(optimizer.total_interest_paid, 2)])
        rows.append(row)

    if not schedule:
        rows.append(["", "", "", "", "", "", round(
            optimizer.total_interest_paid, 2)])

    output_ws.update(rows)
    print(
        f"Success! Payment schedule and total interest paid (${round(optimizer.total_interest_paid, 2):,.2f}) written to Google Sheet.")


# =====================================================================
# CLI ARGUMENT PARSER
# =====================================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="Debt Minimization & Cash Flow Payment Schedule Optimizer"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run comprehensive offline mathematical verification test suite and exit."
    )
    parser.add_argument(
        "--sheet",
        default="Bank Balance Script 2",
        help="Google Spreadsheet title (default: 'Bank Balance Script 2')."
    )
    parser.add_argument(
        "--creds",
        default="credentials.json",
        help="Path to OAuth2 Desktop Client credentials.json file."
    )
    parser.add_argument(
        "--token",
        default="token.json",
        help="Path to cached OAuth2 token.json file."
    )
    parser.add_argument(
        "--service-account",
        default=None,
        help="Optional path to service_account.json for service account fallback authentication."
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.test:
        run_self_tests()
        sys.exit(0)

    run_optimizer_from_sheet(
        sheet_name=args.sheet,
        creds_path=args.creds,
        token_path=args.token,
        service_account_path=args.service_account
    )


if __name__ == "__main__":
    main()
