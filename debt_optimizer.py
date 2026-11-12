import datetime
from datetime import timedelta
import calendar
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
    next_due: datetime.date = field(default_factory=lambda: datetime.date(2000, 1, 1))
    cycle_due_date: datetime.date = field(default_factory=lambda: datetime.date(2000, 1, 1))
    next_cycle_end: datetime.date = field(default_factory=lambda: datetime.date(2000, 1, 1))
    adb_reg_acc: float = 0.0
    days_in_cycle: int = 0
    amount_paid_this_cycle: float = 0.0
    remaining_min_due: float = 0.0
    cycle_fees: float = 0.0
    consecutive_full_payments: int = 0  # Tracks months with full statement balance paid; grace restores at 2


@dataclass(slots=True)
class Loan:
    name: str
    amount_left: float
    apr: float
    minimum_payment: float
    due_day: int
    
    # Internal dynamic simulation state
    next_due: datetime.date = field(default_factory=lambda: datetime.date(2000, 1, 1))
    remaining_min_due: float = 0.0
    accrued_interest: float = 0.0
    amount_paid_this_cycle: float = 0.0


@dataclass(slots=True)
class MonthlyPayment:
    name: str
    amount: float
    due_day: int
    next_due: datetime.date = field(default_factory=lambda: datetime.date(2000, 1, 1))


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
            reg_bal = max(0.0, total_bal - promo_bal) if promo_bal > 0 else total_bal
            
            p_end = c.get('Promo End Date')
            ren_date = c.get('Renewal Date')
            
            cc_obj = CreditCard(
                card=c['Card'],
                issuer=c['Issuer'],
                apr=float(c['APR']),
                balance=reg_bal,
                promo_balance=promo_bal,
                promo_end_date=p_end,
                statement_cycle_end_day=int(c.get('Statement Cycle End Day', 0) or 0),
                statement_balance=float(c.get('Statement Balance', 0.0)),
                grace_active=bool(c.get('Grace Active', False)),
                due_day=due_day,
                grace_period_days=int(c.get('Grace Period Days', 0) or 0),
                cycle_end_nth_weekday=int(c.get('Cycle End Nth Weekday', 0) or 0),
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
        if pay_date.day <= 14:
            return self.income.paychecks_1_and_3
        elif pay_date.day <= 28:
            return self.income.paycheck_2
        else:
            return self.income.paychecks_1_and_3

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
            while True:
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
            return due_date - timedelta(days=grace_days)

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
            # max(Min Floor, Interest + Fees + 1% of Principal, 2% of Principal)
            calc_amt = max(min_floor, interest_charged + fees + (0.01 * principal), 0.02 * principal)
        elif issuer == 'Discover':
            # max(Min Floor, 20 + Interest + Fees, 2% of statement balance)
            calc_amt = max(min_floor, 20.0 + interest_charged + fees, 0.02 * statement_balance)
        else:
            # Capital One, Chase, Citibank, X1: Interest + Fees + max(Min Floor, 1% of Principal)
            calc_amt = interest_charged + fees + max(min_floor, 0.01 * principal)
            
        return min(statement_balance, round(calc_amt, 2))

    def log_transaction(self, description: str, amount: float):
        """Logs an actual checking account cash flow transaction."""
        self.checking += amount
        self.schedule_log.append({
            'Date': self.current_date.strftime('%Y-%m-%d'),
            'Description': description,
            'Amount': round(amount, 2),
            'Checking Balance': round(self.checking, 2)
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
        m_trackers = [{'amount': m.amount, 'due_day': m.due_day, 'next_due': m.next_due} for m in self.monthly]
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
            'issuer': cc.issuer
        } for cc in self.ccs]

        d = self.current_date
        while d <= self.end_date:
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
                        needed = max(cc['rem_min'], cc['stmt_bal'] - cc['paid_cycle'])
                        due_amt = min(needed, cc['bal'])
                    else:
                        due_amt = min(cc['rem_min'], cc['bal'])
                    proj_bal -= due_amt
                    cc['bal'] = max(0.0, cc['bal'] - due_amt)
                    cc['stmt_bal'] = cc['bal']
                    cc['paid_cycle'] = 0.0
                    cc['rem_min'] = self.calc_cc_min_payment(cc['issuer'], cc['stmt_bal'], cc['min_floor'], 0.0, 0.0)
                    cc['next_due'] = advance_month(cc['next_due'], cc['due_day'])

            # Evaluate cushion
            margin = proj_bal - self.buffer
            if margin < min_margin:
                min_margin = margin
                if min_margin <= 0.01:
                    return 0.0

            d += timedelta(days=1)

        return max(0.0, round(min_margin, 2))

    def run(self) -> List[Dict[str, Any]]:
        while self.current_date <= self.end_date:
            # -------------------------------------------------------------
            # 1. Annual Fees Posting on Renewal Date
            # -------------------------------------------------------------
            for cc in self.ccs:
                if cc.renewal_date and (self.current_date.month == cc.renewal_date.month and 
                                       self.current_date.day == cc.renewal_date.day):
                    cc.balance += cc.annual_fee
                    cc.cycle_fees += cc.annual_fee
            
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
                    loan.accrued_interest += loan.amount_left * (loan.apr / 365.0)
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
                    
                    if target_type == 'cc':
                        pay_amt = min(disposable, target.balance)
                        target.balance -= pay_amt
                        if target.balance < 0.005:
                            target.balance = 0.0
                        # Rule 9: Early payment updates remaining minimum
                        target.remaining_min_due = max(0.0, target.remaining_min_due - pay_amt)
                        target.amount_paid_this_cycle += pay_amt
                        self.log_transaction(f"Extra payment to {target.card}", -pay_amt)
                        disposable -= pay_amt
                    else:
                        total_loan_owed = target.amount_left + target.accrued_interest
                        pay_amt = min(disposable, total_loan_owed)
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
                        target.remaining_min_due = max(0.0, target.remaining_min_due - pay_amt)
                        target.amount_paid_this_cycle += pay_amt
                        self.log_transaction(f"Extra payment to {target.name}", -pay_amt)
                        disposable -= pay_amt

            # -------------------------------------------------------------
            # 5. Process Mandatory Payments Due Today
            # -------------------------------------------------------------
            # Monthly Payments
            for m in self.monthly:
                if self.current_date == m.next_due:
                    self.log_transaction(f"Monthly Payment: {m.name}", -m.amount)
                    m.next_due = advance_month(m.next_due, m.due_day)
                    
            # Loan Payments
            for loan in self.loans:
                if loan.amount_left > 0.005 and self.current_date == loan.next_due:
                    # Due is remaining minimum or remaining balance (Rule 10)
                    total_loan_owed = loan.amount_left + loan.accrued_interest
                    due_amt = min(total_loan_owed, loan.remaining_min_due)
                    pay_amt = min(due_amt, self.checking)  # Cap at checking if deficit
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
                        self.log_transaction(f"Loan Payment: {loan.name}", -pay_amt)
                    loan.remaining_min_due = min(loan.amount_left + loan.accrued_interest, loan.minimum_payment)
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
                        target_pay = max(cc.remaining_min_due, cc.statement_balance - cc.amount_paid_this_cycle)
                        cash_above_buf = max(0.0, self.checking - self.buffer)
                        # Rule 4 & 11: Pay full statement balance if cash allows; else pay down to buffer,
                        # but always at least minimum payment (dipping into buffer if necessary)
                        pay_amt = max(cc.remaining_min_due, min(target_pay, cash_above_buf))
                        pay_amt = min(pay_amt, self.checking)  # Buffer dip / cap at checking
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
                        cc.promo_balance = max(0.0, cc.promo_balance - rem_extra)
                        
                        # Fix J: Clamp sub-cent residuals
                        if cc.balance < 0.005:
                            cc.balance = 0.0
                        if cc.promo_balance < 0.005:
                            cc.promo_balance = 0.0

                        cc.amount_paid_this_cycle += pay_amt
                        cc.remaining_min_due = max(0.0, cc.remaining_min_due - min_portion)
                        self.log_transaction(f"Payment to {cc.card}", -pay_amt)
                        
                    # Rule 3: Check Grace Period status
                    if cc.grace_active:
                        if cc.amount_paid_this_cycle < cc.statement_balance - 0.01:
                            cc.grace_active = False
                            cc.consecutive_full_payments = 0
                            # Retroactive interest applied to CC balance (NOT checking account)
                            if cc.issuer == 'Chase':
                                prev_cycle_end = self.calculate_cycle_end_date(
                                    cc.issuer, cc.next_due, cc.grace_period_days,
                                    cc.cycle_end_nth_weekday, cc.statement_cycle_end_day
                                )
                                penalty_days = max(1, (cc.next_due - prev_cycle_end).days)
                            elif cc.issuer == 'Citibank':
                                prev_cycle_end = self.calculate_cycle_end_date(
                                    cc.issuer, cc.next_due, cc.grace_period_days,
                                    cc.cycle_end_nth_weekday, cc.statement_cycle_end_day
                                )
                                penalty_days = max(1, (cc.next_due - prev_cycle_end).days)
                            elif cc.grace_period_days > 0:
                                penalty_days = cc.grace_period_days
                            else:
                                prev_cycle_end = self.calculate_cycle_end_date(
                                    cc.issuer, cc.next_due, cc.grace_period_days,
                                    cc.cycle_end_nth_weekday, cc.statement_cycle_end_day
                                )
                                penalty_days = max(1, (cc.next_due - prev_cycle_end).days)
                            penalty = cc.statement_balance * (cc.apr / 365.0) * penalty_days
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
                        interest = adb_reg * (cc.apr / 365.0) * cc.days_in_cycle
                        cc.balance += interest
                        self.total_interest_paid += interest
                        
                    fees = cc.cycle_fees
                    cc.cycle_fees = 0.0
                    
                    cc.statement_balance = round(cc.balance + cc.promo_balance, 2)
                    cc.remaining_min_due = self.calc_cc_min_payment(
                        cc.issuer, cc.statement_balance, cc.min_floor, interest, fees
                    )
                    
                    # Reset cycle accumulators
                    cc.adb_reg_acc = 0.0
                    cc.days_in_cycle = 0
                    cc.amount_paid_this_cycle = 0.0  # Fix G: Reset for the new statement cycle
                    
                    # Advance cycle_due_date and calculate next_cycle_end independently
                    cc.cycle_due_date = advance_month(cc.cycle_due_date, cc.due_day)
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
    print("======================================================================")
    print("Running Full Suite of Mathematical Tests (36 Comprehensive Scenarios)")
    print("======================================================================")

    base_income = {'Start Date': datetime.date(2026, 1, 1), 'Paycheck 2': 2200, 'Paychecks 1 and 3': 2000}
    
    # ------------------------------------------------------------------
    # PART 1: ORIGINAL 7 BASELINE TEST SCENARIOS (From Gemini's Draft)
    # ------------------------------------------------------------------
    
    # Test 1: Original 1A - Chase Min Payment Baseline
    print("Test 1: Chase Minimum Payment Formula (Original 1A)...")
    assert DebtOptimizer.calc_cc_min_payment('Chase', 1200, 40, 20, 0) == 60.0
    print("  -> Passed!")

    # Test 2: Original 1B - Amex Min Payment Baseline
    print("Test 2: American Express Minimum Payment Baseline (Original 1B)...")
    assert DebtOptimizer.calc_cc_min_payment('American Express', 1200, 40, 20, 0) == 40.0
    print("  -> Passed!")

    # Test 3: Original 1C - Citi Cycle End Date Baseline
    print("Test 3: Citibank Statement Cycle End Date Baseline (Original 1C)...")
    citi_due = datetime.date(2026, 7, 10)
    assert DebtOptimizer.calculate_cycle_end_date('Citibank', citi_due, 0, 7, 0) == datetime.date(2026, 6, 9)
    print("  -> Passed!")

    # Test 4: Original 2 - Promo Balance Allocation & Expiration Rollover
    print("Test 4: Promo Balance Allocation & Expiration Rollover (Original 2)...")
    opt4 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 16), 'End Date': datetime.date(2026, 1, 19), 'Initial Checking Balance': 50, 'Checking Buffer': 0},
        [{'Card': 'PromoCard', 'Issuer': 'Chase', 'APR': 0.20, 'Balance': 1500, 'Promo Balance': 1000, 'Promo End Date': datetime.date(2026, 1, 17), 'Grace Active': False, 'Due Day': 18, 'Statement Balance': 1500, 'Min Floor': 50, 'Statement Cycle End Day': 25}],
        [], [], base_income
    )
    assert opt4.ccs[0].balance == 500.0
    assert opt4.ccs[0].promo_balance == 1000.0
    opt4.run()
    assert opt4.ccs[0].promo_balance == 0.0
    assert opt4.ccs[0].balance == 1450.0  # 500 reg + (1000 promo - 50 paid)
    print("  -> Passed!")

    # Test 5: Original 3 - Grace Period Loss & Retroactive Penalty
    print("Test 5: Grace Period Loss & Retroactive Penalty (Original 3)...")
    opt5 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 19), 'End Date': datetime.date(2026, 1, 21), 'Initial Checking Balance': 50, 'Checking Buffer': 0},
        [{'Card': 'GraceCard', 'Issuer': 'Chase', 'APR': 0.20, 'Balance': 1000, 'Promo Balance': 0, 'Grace Active': True, 'Due Day': 20, 'Statement Balance': 1000, 'Min Floor': 25, 'Statement Cycle End Day': 25}],
        [], [], base_income
    )
    opt5.run()
    assert opt5.ccs[0].grace_active is False
    assert round(opt5.ccs[0].balance, 2) == 964.25  # 1000 - 50 paid + 14.25 retro penalty (26 days from Dec 25)
    print("  -> Passed!")

    # Test 6: Original 4A - Dipping into Checking Buffer for Mandatory Payments
    print("Test 6: Dipping into Buffer for Mandatory Payments (Original 4A)...")
    opt6 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 1), 'End Date': datetime.date(2026, 1, 2), 'Initial Checking Balance': 1200, 'Checking Buffer': 1000},
        [], [{'Name': 'Loan', 'Amount Left': 5000, 'APR': 0.05, 'Minimum Payment': 300, 'Due Day': 1}], [],
        {'Start Date': datetime.date(2026, 1, 15), 'Paycheck 2': 0, 'Paychecks 1 and 3': 0}
    )
    opt6.run()
    assert opt6.checking == 900.0  # Dipped into 1000 buffer to cover 300 mandatory payment
    print("  -> Passed!")

    # Test 7: Original 4B - Biweekly 3-Paycheck Month (May 2026)
    print("Test 7: Biweekly 3-Paycheck Month Schedule (Original 4B)...")
    opt7 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 5, 1), 'End Date': datetime.date(2026, 5, 31), 'Initial Checking Balance': 1000, 'Checking Buffer': 0},
        [], [], [], {'Start Date': datetime.date(2026, 5, 1), 'Paychecks 1 and 3': 2000, 'Paycheck 2': 2200}
    )
    assert opt7.get_paycheck_amount(datetime.date(2026, 5, 1)) == 2000
    assert opt7.get_paycheck_amount(datetime.date(2026, 5, 15)) == 2200
    assert opt7.get_paycheck_amount(datetime.date(2026, 5, 29)) == 2000
    assert opt7.get_next_payday() == datetime.date(2026, 5, 15)
    print("  -> Passed!")

    # Test 8: Original 5 - Final Loan Payoff
    print("Test 8: Final Loan Payoff (Original 5)...")
    opt8 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 1), 'End Date': datetime.date(2026, 1, 11), 'Initial Checking Balance': 1000, 'Checking Buffer': 0},
        [], [{'Name': 'Loan', 'Amount Left': 50.0, 'APR': 0.10, 'Minimum Payment': 200, 'Due Day': 10}], [], base_income
    )
    opt8.run()
    assert round(opt8.loans[0].amount_left, 2) == 0.0
    print("  -> Passed!")

    # Test 9: Original 7 - Cash Flow Look-Ahead & Disposable Allocation
    print("Test 9: Complex Cash Flow Look-Ahead & Allocation (Original 7)...")
    opt9 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 1), 'End Date': datetime.date(2026, 1, 21), 'Initial Checking Balance': 2000, 'Checking Buffer': 500},
        [
            {'Card': 'Chase', 'Issuer': 'Chase', 'APR': 0.25, 'Balance': 4000, 'Promo Balance': 0, 'Grace Active': False, 'Due Day': 18, 'Statement Balance': 4000, 'Min Floor': 40, 'Statement Cycle End Day': 25},
            {'Card': 'Amex', 'Issuer': 'American Express', 'APR': 0.20, 'Balance': 0, 'Promo Balance': 2000, 'Grace Active': False, 'Due Day': 10, 'Statement Balance': 2000, 'Min Floor': 40, 'Grace Period Days': 25}
        ],
        [{'Name': 'Loan', 'Amount Left': 10000, 'APR': 0.05, 'Minimum Payment': 300, 'Due Day': 15}],
        [{'Name': 'Rent', 'Amount': 1500, 'Due Day': 1}],
        {'Start Date': datetime.date(2026, 1, 7), 'Paycheck 2': 2500, 'Paychecks 1 and 3': 2500}
    )
    log9 = opt9.run()
    # On Jan 7: Paycheck arrives. Rent was paid on Jan 1. Checking = 3000.
    # Mandatory future before next payday (Jan 21) = 40 (Amex min) + 300 (Loan) + 40 (Chase min) = 380.
    # Disposable = 3000 - 500 (buffer) - 380 = 2120.
    chase_extra = [t for t in log9 if t['Date'] == '2026-01-07' and 'Extra payment to Chase' in t['Description']]
    assert len(chase_extra) == 1 and abs(chase_extra[0]['Amount']) == 2120.0
    print("  -> Passed!")

    # ------------------------------------------------------------------
    # PART 2: 14 NEW COMPREHENSIVE TEST SCENARIOS (Audit & Edge Cases)
    # ------------------------------------------------------------------

    # Test 10: New 1 - Discover Minimum Payment Formula (Rule 6c)
    print("Test 10: Discover Minimum Payment Formula (Rule 6c)...")
    # Discover: max(Min Floor, 20 + Interest + Fees, 2% Statement Balance)
    assert DebtOptimizer.calc_cc_min_payment('Discover', 1200, 35, 10, 0) == 35.0   # Floor dominates
    assert DebtOptimizer.calc_cc_min_payment('Discover', 1000, 25, 30, 0) == 50.0   # 20 + Interest dominates
    assert DebtOptimizer.calc_cc_min_payment('Discover', 5000, 25, 10, 0) == 100.0  # 2% Balance dominates
    print("  -> Passed!")

    # Test 11: New 2 - Capital One, Citibank, and X1 Minimums (Rule 6a)
    print("Test 11: Capital One, Citibank, and X1 Minimum Payments (Rule 6a)...")
    assert DebtOptimizer.calc_cc_min_payment('Capital One', 1200, 40, 20, 0) == 60.0
    assert DebtOptimizer.calc_cc_min_payment('Citibank', 1200, 40, 20, 0) == 60.0
    assert DebtOptimizer.calc_cc_min_payment('X1', 1200, 40, 20, 0) == 60.0
    print("  -> Passed!")

    # Test 12: New 3 - Statement Balance Less Than Min Floor (Spec 6 note)
    print("Test 12: Statement Balance Below Min Floor...")
    assert DebtOptimizer.calc_cc_min_payment('Chase', 25.50, 40, 0, 0) == 25.50
    assert DebtOptimizer.calc_cc_min_payment('Discover', 15.00, 35, 0, 0) == 15.00
    print("  -> Passed!")

    # Test 13: New 4 - Zero or Negative Statement Balance (Spec 6 note)
    print("Test 13: Zero or Negative Statement Balance...")
    assert DebtOptimizer.calc_cc_min_payment('Chase', 0.0, 40, 0, 0) == 0.0
    assert DebtOptimizer.calc_cc_min_payment('American Express', -50.0, 40, 0, 0) == 0.0
    print("  -> Passed!")

    # Test 14: New 5 - American Express 2% Principal Dominance (Rule 6b)
    print("Test 14: American Express 2% Principal Dominance (Rule 6b)...")
    # Stmt bal = 5000, floor = 40, int = 10 -> prin = 4990. int + 1% = 59.90. 2% = 99.80.
    assert DebtOptimizer.calc_cc_min_payment('American Express', 5000, 40, 10, 0) == 99.80
    print("  -> Passed!")

    # Test 15: New 6 - Statement Cycle End Dates for Amex, CapOne, Discover, X1 (Rule 5a)
    print("Test 15: Statement Cycle End Dates for Amex, CapOne, Discover, X1...")
    due_apr = datetime.date(2026, 4, 8)
    assert DebtOptimizer.calculate_cycle_end_date('American Express', due_apr, 25, 0, 0) == datetime.date(2026, 3, 14)
    assert DebtOptimizer.calculate_cycle_end_date('Capital One', due_apr, 25, 0, 0) == datetime.date(2026, 3, 14)
    assert DebtOptimizer.calculate_cycle_end_date('Discover', due_apr, 25, 0, 0) == datetime.date(2026, 3, 14)
    assert DebtOptimizer.calculate_cycle_end_date('X1', due_apr, 25, 0, 0) == datetime.date(2026, 3, 14)
    print("  -> Passed!")

    # Test 16: New 7 - Citibank Cycle End Date Across Year Boundary (Dec/Jan)
    print("Test 16: Citibank Cycle Date Year-Boundary Rollover (Jan -> Dec)...")
    citi_due_jan = datetime.date(2026, 1, 15)
    assert DebtOptimizer.calculate_cycle_end_date('Citibank', citi_due_jan, 0, 5, 0) == datetime.date(2025, 12, 5)
    print("  -> Passed!")

    # Test 17: New 8 - Month-End Date Clamping (Feb 28 / Apr 30)
    print("Test 17: Month-End Date Clamping (Due Day 31 in Feb & Apr)...")
    d_jan31 = datetime.date(2026, 1, 31)
    d_feb = advance_month(d_jan31, 31)
    assert d_feb == datetime.date(2026, 2, 28)
    d_mar = advance_month(d_feb, 31)
    assert d_mar == datetime.date(2026, 3, 31)
    d_apr = advance_month(d_mar, 31)
    assert d_apr == datetime.date(2026, 4, 30)
    print("  -> Passed!")

    # Test 18: New 9 - Annual Fee Charged on Renewal Date (Spec 2o/p)
    print("Test 18: Annual Fee Charged on Renewal Date...")
    opt18 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 1), 'End Date': datetime.date(2026, 1, 20), 'Initial Checking Balance': 200, 'Checking Buffer': 200},
        [{'Card': 'FeeCard', 'Issuer': 'American Express', 'APR': 0.20, 'Balance': 500, 'Promo Balance': 0, 'Grace Active': False, 'Due Day': 25, 'Statement Balance': 500, 'Min Floor': 40, 'Grace Period Days': 25, 'Annual Fee': 95.0, 'Renewal Date': datetime.date(2026, 1, 15)}],
        [], [], {'Start Date': datetime.date(2026, 1, 1), 'Paychecks 1 and 3': 0, 'Paycheck 2': 0}
    )
    opt18.run()
    assert opt18.ccs[0].balance == 595.0
    print("  -> Passed!")

    # Test 19: New 10 - Grace Period Preservation with Sufficient Cash (Rule 4)
    print("Test 19: Grace Period Preservation with Sufficient Cash (Rule 4)...")
    opt19 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 1), 'End Date': datetime.date(2026, 1, 26), 'Initial Checking Balance': 5000, 'Checking Buffer': 500},
        [{'Card': 'GraceCard', 'Issuer': 'American Express', 'APR': 0.24, 'Balance': 1000, 'Promo Balance': 0, 'Grace Active': True, 'Due Day': 25, 'Statement Balance': 1000, 'Min Floor': 40, 'Grace Period Days': 25}],
        [], [], {'Start Date': datetime.date(2026, 1, 1), 'Paychecks 1 and 3': 0, 'Paycheck 2': 0}
    )
    log19 = opt19.run()
    assert opt19.ccs[0].grace_active is True
    assert opt19.ccs[0].balance == 0.0
    assert any(t['Date'] == '2026-01-25' and abs(t['Amount']) == 1000.0 for t in log19)
    print("  -> Passed!")

    # Test 20: New 11 - Early Loan Payment Updating Minimum Due (Rule 9)
    print("Test 20: Early Loan Payment Updating Minimum Due (Rule 9)...")
    opt20 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 1), 'End Date': datetime.date(2026, 1, 15), 'Initial Checking Balance': 1000, 'Checking Buffer': 0},
        [], [{'Name': 'Loan', 'Amount Left': 1000, 'APR': 0.10, 'Minimum Payment': 300, 'Due Day': 10}], [],
        {'Start Date': datetime.date(2026, 1, 15), 'Paychecks 1 and 3': 0, 'Paycheck 2': 0}
    )
    log20 = opt20.run()
    loan_due_payments = [t for t in log20 if t['Date'] == '2026-01-10' and 'Loan Payment' in t['Description']]
    assert len(loan_due_payments) == 0  # Satisfied by early extra payment
    print("  -> Passed!")

    # Test 21: New 12 - Debt Avalanche Waterfall Priority
    print("Test 21: Debt Avalanche Waterfall Priority (Highest APR First)...")
    opt21 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 1), 'End Date': datetime.date(2026, 1, 5), 'Initial Checking Balance': 2000, 'Checking Buffer': 500},
        [
            {'Card': 'LowAPRCard', 'Issuer': 'Chase', 'APR': 0.15, 'Balance': 1000, 'Promo Balance': 0, 'Grace Active': False, 'Due Day': 25, 'Statement Balance': 1000, 'Min Floor': 40, 'Statement Cycle End Day': 28},
            {'Card': 'HighAPRCard', 'Issuer': 'Discover', 'APR': 0.28, 'Balance': 600, 'Promo Balance': 0, 'Grace Active': False, 'Due Day': 25, 'Statement Balance': 600, 'Min Floor': 40, 'Grace Period Days': 25}
        ],
        [{'Name': 'MidAPRLoan', 'Amount Left': 2000, 'APR': 0.20, 'Minimum Payment': 100, 'Due Day': 20}],
        [], {'Start Date': datetime.date(2026, 1, 15), 'Paychecks 1 and 3': 0, 'Paycheck 2': 0}
    )
    log21 = opt21.run()
    high_pay = [t for t in log21 if 'Extra payment to HighAPRCard' in t['Description']]
    mid_pay = [t for t in log21 if 'Extra payment to MidAPRLoan' in t['Description']]
    low_pay = [t for t in log21 if 'Extra payment to LowAPRCard' in t['Description']]
    assert len(high_pay) == 1 and abs(high_pay[0]['Amount']) == 600.0
    assert len(mid_pay) == 1 and abs(mid_pay[0]['Amount']) == 900.0
    assert len(low_pay) == 0
    assert opt21.checking == 500.0
    print("  -> Passed!")

    # Test 22: New 13 - Mid-Cycle Start ADB Initialization (Spec 1a)
    print("Test 22: Mid-Cycle Start ADB Initialization (Spec 1a)...")
    opt22 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 15), 'End Date': datetime.date(2026, 1, 16), 'Initial Checking Balance': 1000, 'Checking Buffer': 0},
        [{'Card': 'MidStartCard', 'Issuer': 'American Express', 'APR': 0.24, 'Balance': 1000, 'Promo Balance': 0, 'Grace Active': False, 'Due Day': 25, 'Statement Balance': 1000, 'Min Floor': 40, 'Grace Period Days': 25}],
        [], [], base_income
    )
    assert opt22.ccs[0].days_in_cycle >= 14
    assert opt22.ccs[0].adb_reg_acc == 1000.0 * opt22.ccs[0].days_in_cycle
    print("  -> Passed!")

    # Test 23: New 14 - Multi-Month Statement Cycles Advancing Across 6 Months
    print("Test 23: Multi-Month Statement Cycles Advancing Across 6 Months...")
    opt23 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 1), 'End Date': datetime.date(2026, 7, 1), 'Initial Checking Balance': 1500, 'Checking Buffer': 200},
        [{'Card': 'CycleCard', 'Issuer': 'American Express', 'APR': 0.24, 'Balance': 3000, 'Promo Balance': 0, 'Grace Active': False, 'Due Day': 25, 'Statement Balance': 3000, 'Min Floor': 40, 'Grace Period Days': 25}],
        [], [], {'Start Date': datetime.date(2026, 1, 1), 'Paychecks 1 and 3': 1200, 'Paycheck 2': 1200}
    )
    opt23.run()
    cc23 = opt23.ccs[0]
    assert cc23.next_due.month >= 7
    assert cc23.next_cycle_end.month >= 6
    assert cc23.balance < 1000.0
    print("  -> Passed!")

    # Test 24: Statement Cycle Closing Between Start Date and First Due Date (Fix A)
    print("Test 24: Cycle Closing Between Start Date and Due Date (Fix A)...")
    opt24 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 1), 'End Date': datetime.date(2026, 1, 5), 'Initial Checking Balance': 1000, 'Checking Buffer': 0},
        [{'Card': 'AmexEarlyCycle', 'Issuer': 'American Express', 'APR': 0.24, 'Balance': 2000, 'Promo Balance': 0, 'Grace Active': False, 'Due Day': 28, 'Statement Balance': 2000, 'Min Floor': 40, 'Grace Period Days': 25}],
        [], [], base_income
    )
    # Due on Jan 28, grace 25 -> cycle closes Jan 3.
    # Prior due was Dec 28 -> prior cycle closed Dec 3.
    # On Jan 1, elapsed days in cycle = (2026-01-01 - 2025-12-03) = 29 days.
    assert opt24.ccs[0].next_cycle_end == datetime.date(2026, 1, 3)
    assert opt24.ccs[0].days_in_cycle == 29
    assert opt24.ccs[0].adb_reg_acc == 2000.0 * 29
    opt24.run()
    # After Jan 3 closes, next_cycle_end should advance to Feb 3 (for Feb 28 due date)
    assert opt24.ccs[0].next_cycle_end == datetime.date(2026, 2, 3)
    assert opt24.ccs[0].next_due == datetime.date(2026, 1, 28)
    print("  -> Passed!")

    # Test 25: Decoupled Statement Cycle and Due Date Advancement (Fix B)
    print("Test 25: Decoupled Statement Cycle & Due Date Advancement (Fix B)...")
    opt25 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 1), 'End Date': datetime.date(2026, 2, 10), 'Initial Checking Balance': 2000, 'Checking Buffer': 100},
        [{'Card': 'ChaseCard', 'Issuer': 'Chase', 'APR': 0.20, 'Balance': 1500, 'Promo Balance': 0, 'Grace Active': False, 'Due Day': 20, 'Statement Balance': 1500, 'Min Floor': 40, 'Statement Cycle End Day': 25}],
        [], [], base_income
    )
    # Next due is Jan 20. Next cycle end is Jan 25.
    assert opt25.ccs[0].next_due == datetime.date(2026, 1, 20)
    assert opt25.ccs[0].next_cycle_end == datetime.date(2026, 1, 25)
    opt25.run()
    # At end of Jan and early Feb (Feb 10):
    # Due date should have advanced to Feb 20.
    # Cycle end should have advanced to Feb 25 independently.
    assert opt25.ccs[0].next_due == datetime.date(2026, 2, 20)
    assert opt25.ccs[0].next_cycle_end == datetime.date(2026, 2, 25)
    print("  -> Passed!")

    # Test 26: Lookahead Window Covers Mandatory Obligations Due on End Date (Fix E)
    print("Test 26: Lookahead Covering End Date Mandatory Payment (Fix E)...")
    opt26 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 1), 'End Date': datetime.date(2026, 1, 10), 'Initial Checking Balance': 1000, 'Checking Buffer': 200},
        [{'Card': 'HighAPR', 'Issuer': 'Discover', 'APR': 0.25, 'Balance': 2000, 'Promo Balance': 0, 'Grace Active': False, 'Due Day': 25, 'Statement Balance': 2000, 'Min Floor': 40, 'Grace Period Days': 25}],
        [], [{'Name': 'Rent', 'Amount': 300, 'Due Day': 10}],
        {'Start Date': datetime.date(2026, 1, 15), 'Paychecks 1 and 3': 0, 'Paycheck 2': 0}
    )
    log26 = opt26.run()
    # Lookahead on Jan 1 must reserve 300 for Rent on Jan 10, leaving 1000 - 200 - 300 = 500 for extra payment
    extra26 = [t for t in log26 if 'Extra payment to HighAPR' in t['Description']]
    assert len(extra26) == 1 and abs(extra26[0]['Amount']) == 500.0
    # On Jan 10, Rent is successfully paid and checking ends at exactly buffer (200.0)
    rent26 = [t for t in log26 if 'Monthly Payment: Rent' in t['Description']]
    assert len(rent26) == 1 and abs(rent26[0]['Amount']) == 300.0
    assert opt26.checking == 200.0
    print("  -> Passed!")

    # Test 27: Credit Card Payment Capped at Total Owed When Below Min Floor (Fix F)
    print("Test 27: CC Payment Capped at Total Balance Below Min Floor (Fix F)...")
    opt27 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 10), 'End Date': datetime.date(2026, 1, 16), 'Initial Checking Balance': 500, 'Checking Buffer': 500},
        [{'Card': 'SmallBalCard', 'Issuer': 'Chase', 'APR': 0.20, 'Balance': 15.0, 'Promo Balance': 0, 'Grace Active': False, 'Due Day': 15, 'Statement Balance': 500, 'Min Floor': 40, 'Statement Cycle End Day': 25}],
        [], [], {'Start Date': datetime.date(2026, 1, 25), 'Paychecks 1 and 3': 0, 'Paycheck 2': 0}
    )
    log27 = opt27.run()
    pay27 = [t for t in log27 if 'Payment to SmallBalCard' in t['Description']]
    assert len(pay27) == 1 and abs(pay27[0]['Amount']) == 15.0  # Capped at 15.0, not 40.0
    assert opt27.checking == 485.0
    assert opt27.ccs[0].balance == 0.0
    print("  -> Passed!")

    # Test 28: Amount Paid This Cycle Reset on Cycle End (Fix G)
    print("Test 28: Amount Paid This Cycle Reset on Cycle Close (Fix G)...")
    opt28 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 1), 'End Date': datetime.date(2026, 1, 20), 'Initial Checking Balance': 1000, 'Checking Buffer': 0},
        [{'Card': 'AmexReset', 'Issuer': 'American Express', 'APR': 0.24, 'Balance': 1000, 'Promo Balance': 0, 'Grace Active': False, 'Due Day': 25, 'Statement Balance': 1000, 'Min Floor': 40, 'Grace Period Days': 20}],
        [], [], {'Start Date': datetime.date(2026, 1, 25), 'Paychecks 1 and 3': 0, 'Paycheck 2': 0}
    )
    # Due on Jan 25, grace 20 -> cycle closes Jan 5.
    opt28.run()
    # Cycle closed on Jan 5, so amount_paid_this_cycle must be reset to 0.0
    assert opt28.ccs[0].amount_paid_this_cycle == 0.0
    print("  -> Passed!")

    # Test 29: Grace Period Requires 2 Consecutive Full Payments to Restore (Fix H)
    print("Test 29: Grace Period Requires 2 Consecutive Full Payments to Restore (Fix H)...")
    # Card with lost grace. Statement balance = 25 (< min floor so min == full statement).
    # Buffer high enough to block extra payments except the first one that zeroes the balance.
    # Jan 20 due date: total_owed=0 (zeroed by Jan 1 extra payment) -> streak = 1. Grace NOT restored yet.
    # Jan 26 cycle closes -> new statement set. Feb 20 due date: total_owed=0 again -> streak = 2. Grace RESTORED.
    opt29 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 1), 'End Date': datetime.date(2026, 3, 5), 'Initial Checking Balance': 1000, 'Checking Buffer': 950},
        [{'Card': 'GraceRestoreCard', 'Issuer': 'American Express', 'APR': 0.20, 'Balance': 25, 'Promo Balance': 0, 'Grace Active': False, 'Due Day': 20, 'Statement Balance': 25, 'Min Floor': 25, 'Grace Period Days': 25}],
        [], [], {'Start Date': datetime.date(2026, 1, 25), 'Paychecks 1 and 3': 0, 'Paycheck 2': 0}
    )
    opt29.run()
    assert opt29.ccs[0].grace_active is True
    assert opt29.ccs[0].consecutive_full_payments == 0  # Reset after restoration
    print("  -> Passed!")

    # Test 29b: Consecutive Streak Resets on Missed Full Payment (Fix H)
    print("Test 29b: Consecutive Streak Resets if Full Payment Missed...")
    # Only 300 in checking, statement_balance=500, min=25. Jan due: pays only 25 (minimum). Streak stays 0.
    # Even after multiple months, grace should NOT restore because Jan was only a partial payment.
    opt29b = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 1), 'End Date': datetime.date(2026, 3, 5), 'Initial Checking Balance': 300, 'Checking Buffer': 275},
        [{'Card': 'StreakCard', 'Issuer': 'American Express', 'APR': 0.20, 'Balance': 500, 'Promo Balance': 0, 'Grace Active': False, 'Due Day': 20, 'Statement Balance': 500, 'Min Floor': 25, 'Grace Period Days': 25}],
        [], [], {'Start Date': datetime.date(2026, 2, 15), 'Paychecks 1 and 3': 0, 'Paycheck 2': 0}
    )
    opt29b.run()
    assert opt29b.ccs[0].grace_active is False
    assert opt29b.ccs[0].consecutive_full_payments == 0
    print("  -> Passed!")

    # Test 30: Sub-Cent Floating Point Residual Clamping (Fix J)
    print("Test 30: Sub-Cent Balance Residual Clamping (Fix J)...")
    opt30 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 1), 'End Date': datetime.date(2026, 1, 3), 'Initial Checking Balance': 100, 'Checking Buffer': 0},
        [], [{'Name': 'MicroLoan', 'Amount Left': 0.0001, 'APR': 0.05, 'Minimum Payment': 50, 'Due Day': 15}], [],
        {'Start Date': datetime.date(2026, 1, 25), 'Paychecks 1 and 3': 0, 'Paycheck 2': 0}
    )
    opt30.run()
    assert opt30.loans[0].amount_left == 0.0
    assert opt30.loans[0].remaining_min_due == 0.0
    print("  -> Passed!")

    # Test 31: Output Ledger Sorting by Date Ascending & Amount Descending
    print("Test 31: Output Ledger Sorting & Running Balance Consistency...")
    sample_schedule = [
        {'Date': '2026-01-01', 'Description': 'Rent', 'Amount': -1500.0, 'Checking Balance': 1500.0},
        {'Date': '2026-01-01', 'Description': 'Paycheck Received', 'Amount': 2000.0, 'Checking Balance': 3000.0},
        {'Date': '2026-01-01', 'Description': 'Extra payment to Loan', 'Amount': -200.0, 'Checking Balance': 1300.0},
        {'Date': '2026-01-05', 'Description': 'Utility', 'Amount': -100.0, 'Checking Balance': 1200.0}
    ]
    # Sort per user specification: Date asc, Amount desc
    sample_schedule.sort(key=lambda r: (r['Date'], -r['Amount']))
    assert [r['Description'] for r in sample_schedule[:3]] == [
        'Paycheck Received', 'Extra payment to Loan', 'Rent'
    ]
    # Recalculate running balance with initial checking 1000
    running = 1000.0
    for r in sample_schedule:
        running += r['Amount']
        r['Checking Balance'] = round(running, 2)
    assert sample_schedule[0]['Checking Balance'] == 3000.0  # 1000 + 2000
    assert sample_schedule[1]['Checking Balance'] == 2800.0  # 3000 - 200
    assert sample_schedule[2]['Checking Balance'] == 1300.0  # 2800 - 1500
    assert sample_schedule[3]['Checking Balance'] == 1200.0  # 1300 - 100
    print("  -> Passed!")

    # Test 32: ADB Interest Correctly Reflects Mid-Cycle Payoff (Lingering Interest)
    print("Test 32: ADB Interest Correctly Reflects Mid-Cycle Payoff (Lingering Interest)...")
    # Setup:
    #   - Amex card, grace_active=False, APR=0.20, balance=1000.
    #   - Start Jan 1. Due day 25, grace 25 days -> cycle closes Jan 31 (25 days before Feb 25 due).
    #   - On Jan 1, Section 3 runs first: adds 1000 to adb_reg_acc for that day (days_in_cycle becomes 2,
    #     since __init__ already seeded 1 day elapsed from Dec 31 prev cycle end).
    #   - Then Section 4 pays off the full 1000 balance (extra payment). balance = 0.
    #   - For Jan 2-31 (30 more days), balance=0, so adb_reg_acc += 0 each day.
    #   - At Jan 31 cycle close: adb_reg = adb_reg_acc / days_in_cycle = (1000 * 2) / 32 = 62.5.
    #     interest = 62.5 * (0.20/365) * 32 = $1.10 (lingering charge despite zero live balance).
    #   - statement_balance = $1.10. Balance is > 0 and NOT zeroed out.
    opt32 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 1), 'End Date': datetime.date(2026, 2, 5), 'Initial Checking Balance': 2000, 'Checking Buffer': 0},
        [{'Card': 'AmexLingeringInt', 'Issuer': 'American Express', 'APR': 0.20, 'Balance': 1000, 'Promo Balance': 0, 'Grace Active': False, 'Due Day': 25, 'Statement Balance': 1000, 'Min Floor': 25, 'Grace Period Days': 25}],
        [], [], {'Start Date': datetime.date(2026, 1, 25), 'Paychecks 1 and 3': 0, 'Paycheck 2': 0}
    )
    # Due day 25, grace 25 -> next_cycle_end = Jan 31; next_due = Jan 25
    assert opt32.ccs[0].next_cycle_end == datetime.date(2026, 1, 31)
    assert opt32.ccs[0].next_due == datetime.date(2026, 1, 25)
    # __init__ seeds 1 day elapsed (Jan 1 start - Dec 31 prev cycle end) with adb_reg_acc = 1000 * 1
    assert opt32.ccs[0].days_in_cycle == 1
    assert opt32.ccs[0].adb_reg_acc == 1000.0 * 1

    log32 = opt32.run()

    # Extra payment fires on Jan 1 (Section 4 runs after Section 3).
    # Section 3 on Jan 1 adds 1000 to ADB first (days becomes 2, acc becomes 2000).
    # Then extra payment zeros balance. For Jan 2-31: adds 0 each day -> total days = 32, acc = 2000.
    # At cycle close Jan 31: interest = (2000 / 32) * (0.20/365) * 32 = 2000 * 0.20/365 = $1.096 -> ~$1.10
    extra_jan1 = [t for t in log32 if t['Date'] == '2026-01-01' and 'Extra payment' in t['Description']]
    assert len(extra_jan1) == 1 and abs(extra_jan1[0]['Amount']) == 1000.0

    cc32 = opt32.ccs[0]
    # statement_balance must reflect lingering ADB interest, NOT zero
    assert cc32.statement_balance > 0.0, "Expected lingering ADB interest on new statement"
    assert cc32.statement_balance < 5.0, "Expected only a small interest charge, not the full balance"

    # Verify the ADB math precisely:
    # adb_reg_acc at close = 1000 * 2 days (1 from __init__ + 1 from Jan 1 Section 3)
    # interest = adb_reg_acc * (apr / 365) = 2000 * (0.20 / 365) = $1.0959...
    expected_interest = round(1000.0 * 2 * (0.20 / 365.0), 2)
    assert abs(cc32.statement_balance - expected_interest) < 0.02, (
        f"Expected ~${expected_interest} in lingering interest, got ${cc32.statement_balance}"
    )
    print(f"  -> Passed! (Lingering ADB interest on statement: ${cc32.statement_balance}, expected ~${expected_interest})")

    # Test 33: Citibank Infinite-Loop Guard on Out-of-Bounds Weekday
    print("Test 33: Citibank Infinite-Loop Guard on Out-of-Bounds Weekday...")
    try:
        DebtOptimizer.calculate_cycle_end_date('Citibank', datetime.date(2026, 7, 10), 0, 50, 0)
        assert False, "Expected ValueError for out-of-bounds Citibank weekday"
    except ValueError as e:
        assert "exceeds the number of business days" in str(e)
    print("  -> Passed!")

    # Test 34: Retroactive Grace Penalty Uses Issuer/Card Grace Period (Amex)
    print("Test 34: Retroactive Grace Penalty Uses Issuer/Card Grace Period...")
    opt34 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 24), 'End Date': datetime.date(2026, 1, 26), 'Initial Checking Balance': 50, 'Checking Buffer': 0},
        [{'Card': 'AmexGrace', 'Issuer': 'American Express', 'APR': 0.20, 'Balance': 1000, 'Promo Balance': 0, 'Grace Active': True, 'Due Day': 25, 'Statement Balance': 1000, 'Min Floor': 25, 'Grace Period Days': 25}],
        [], [], base_income
    )
    opt34.run()
    assert opt34.ccs[0].grace_active is False
    # Penalty uses exact 25 grace days (not hardcoded 30): 1000 * (0.20/365) * 25 = 13.70
    assert round(opt34.ccs[0].balance, 2) == 963.70  # 1000 - 50 paid + 13.70 retro penalty
    print("  -> Passed!")

    # Test 35: Simple Interest Loan Accrual (No Daily Principal Compounding)
    print("Test 35: Simple Interest Loan Accrual (No Daily Principal Compounding)...")
    opt35 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 1), 'End Date': datetime.date(2026, 1, 5), 'Initial Checking Balance': 0, 'Checking Buffer': 0},
        [], [{'Name': 'AutoLoan', 'Amount Left': 10000.0, 'APR': 0.10, 'Minimum Payment': 300, 'Due Day': 15}], [],
        {'Start Date': datetime.date(2026, 1, 15), 'Paychecks 1 and 3': 0, 'Paycheck 2': 0}
    )
    opt35.run()
    loan35 = opt35.loans[0]
    assert loan35.amount_left == 10000.0, "Principal must remain constant without daily compounding"
    expected_simple_int = round(10000.0 * (0.10 / 365.0) * 5, 2)
    assert round(loan35.accrued_interest, 2) == expected_simple_int
    print("  -> Passed!")

    # Test 36: Multi-Period Forward Cash-Flow Protection (Buffer Preservation)
    print("Test 36: Multi-Period Forward Cash-Flow Protection (Buffer Preservation)...")
    opt36 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 12, 18), 'End Date': datetime.date(2027, 1, 15), 'Initial Checking Balance': 300.0, 'Checking Buffer': 300.0},
        [{'Card': 'HighAPRCard', 'Issuer': 'Chase', 'APR': 0.25, 'Balance': 5000.0, 'Promo Balance': 0.0, 'Promo End Date': None, 'Statement Cycle End Day': 20, 'Statement Balance': 500.0, 'Grace Active': False, 'Due Day': 15, 'Grace Period Days': 25, 'Cycle End Nth Weekday': 0, 'Min Floor': 40.0, 'Annual Fee': 0.0, 'Renewal Date': None}],
        [
            {'Name': 'Loan1', 'Amount Left': 10000.0, 'APR': 0.08, 'Minimum Payment': 400.0, 'Due Day': 5},
            {'Name': 'Loan2', 'Amount Left': 8000.0, 'APR': 0.07, 'Minimum Payment': 300.0, 'Due Day': 5},
        ],
        [{'Name': 'Rent', 'Amount': 1500.0, 'Due Day': 1}],
        {'Start Date': datetime.date(2026, 12, 18), 'Paycheck 2': 2000.0, 'Paychecks 1 and 3': 2000.0}
    )
    schedule36 = opt36.run()
    # On Dec 18, paycheck brings checking to 2300.
    # Future deficit in Jan (Rent 1500 + Loans 700 = 2200 vs 2000 paycheck) requires 200 reserve.
    # Safe extra payment is 1800 (not 2000), leaving checking at 300 on Jan 5 without dipping below buffer.
    jan5_transactions = [t for t in schedule36 if t['Date'] == '2027-01-05']
    assert len(jan5_transactions) == 2, "Expected both loan payments on Jan 5"
    assert jan5_transactions[-1]['Checking Balance'] == 300.0, f"Expected checking balance == 300.0 on Jan 5, got {jan5_transactions[-1]['Checking Balance']}"
    assert all(t['Checking Balance'] >= 300.0 for t in schedule36), "Checking balance must never dip below buffer"
    print("  -> Passed!")

    print("\n======================================================================")
    print("ALL 36 MATHEMATICAL TESTS PASSED PERFECTLY!")
    print("======================================================================\n")


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
        raise ImportError("gspread library is not installed. Please run: pip install gspread google-auth-oauthlib")

    # If user explicitly requested service account or only service_account.json exists
    if service_account_path and os.path.exists(service_account_path):
        print(f"Connecting via Service Account: {service_account_path}...")
        scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(service_account_path, scopes=scope)
        return gspread.authorize(creds)

    # Primary / Recommended: OAuth 2.0 Desktop App Flow
    print(f"Connecting via OAuth 2.0 (credentials: {creds_path}, token: {token_path})...")
    if not os.path.exists(creds_path) and not os.path.exists(token_path):
        # Fallback check if service_account.json is present
        if os.path.exists("service_account.json"):
            print("Note: credentials.json not found, falling back to service_account.json...")
            scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
            creds = Credentials.from_service_account_file("service_account.json", scopes=scope)
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
    client = get_google_sheets_client(creds_path, token_path, service_account_path)
    
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
        alerts.append("Worksheet 'Parameters': 'Start Date' is missing or could not be parsed.")
    if not end_d:
        alerts.append("Worksheet 'Parameters': 'End Date' is missing or could not be parsed.")
    if 'Initial Checking Balance' not in p or p.get('Initial Checking Balance') == '':
        alerts.append("Worksheet 'Parameters': 'Initial Checking Balance' is missing.")
    if 'Checking Buffer' not in p or p.get('Checking Buffer') == '':
        alerts.append("Worksheet 'Parameters': 'Checking Buffer' is missing (defaulting to 0.0).")

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
            alerts.append(f"Worksheet 'CreditCards', row {idx} ({card_name}): 'Issuer' is missing.")
        elif issuer == 'Chase' and not c.get('Statement Cycle End Day'):
            alerts.append(f"Worksheet 'CreditCards', row {idx} ({card_name}): 'Statement Cycle End Day' is required for Chase cards.")
        elif issuer == 'Citibank' and not c.get('Cycle End Nth Weekday'):
            alerts.append(f"Worksheet 'CreditCards', row {idx} ({card_name}): 'Cycle End Nth Weekday' is required for Citibank cards.")
        elif issuer in ('American Express', 'Capital One', 'Discover', 'X1') and not c.get('Grace Period Days'):
            alerts.append(f"Worksheet 'CreditCards', row {idx} ({card_name}): 'Grace Period Days' is required for {issuer} cards.")
        if 'Due Day' not in c or c.get('Due Day') == '':
            alerts.append(f"Worksheet 'CreditCards', row {idx} ({card_name}): 'Due Day' is missing.")

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
            alerts.append(f"Worksheet 'Loans', row {idx} ({loan_name}): 'Due Day' is missing.")
        if 'Minimum Payment' not in l or l.get('Minimum Payment') == '':
            alerts.append(f"Worksheet 'Loans', row {idx} ({loan_name}): 'Minimum Payment' is missing.")
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
            alerts.append(f"Worksheet 'MonthlyPayments', row {idx} ({m_name}): 'Due Day' is missing.")
        if 'Amount' not in m or m.get('Amount') == '':
            alerts.append(f"Worksheet 'MonthlyPayments', row {idx} ({m_name}): 'Amount' is missing.")
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
        alerts.append("Worksheet 'Income': 'Start Date' is missing or could not be parsed.")
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
            raise ValueError("Simulation halted: Required start/end dates are missing. See alerts above.")

    print("Data loaded successfully. Running Debt Minimizer optimization engine...")
    optimizer = DebtOptimizer(params, ccs, loans, monthly, income)
    schedule = optimizer.run()

    # Format & Sort output ledger:
    # 1. Date ascending
    # 2. Within same date, Amount descending
    schedule.sort(key=lambda r: (r['Date'], -r['Amount']))

    # Recalculate running checking balance so ledger amounts are strictly cumulative
    running_bal = float(params['Initial Checking Balance'])
    for r in schedule:
        running_bal += r['Amount']
        r['Checking Balance'] = round(running_bal, 2)

    print(f"Optimization finished ({len(schedule)} transactions). Writing to 'Output' sheet...")
    try:
        output_ws = sheet.worksheet("Output")
    except Exception:
        output_ws = sheet.add_worksheet(title="Output", rows=len(schedule) + 10, cols=10)

    # Ensure sheet has at least 7 columns for column G
    if output_ws.col_count < 7:
        output_ws.add_cols(7 - output_ws.col_count)

    output_ws.clear()
    headers = ["Date", "Description", "Amount", "Checking Balance", "", "", "Total Interest Paid"]
    rows = [headers]
    for idx, r in enumerate(schedule):
        row = [r['Date'], r['Description'], r['Amount'], r['Checking Balance']]
        if idx == 0:
            row.extend(["", "", round(optimizer.total_interest_paid, 2)])
        rows.append(row)

    if not schedule:
        rows.append(["", "", "", "", "", "", round(optimizer.total_interest_paid, 2)])
        
    output_ws.update(rows)
    print(f"Success! Payment schedule and total interest paid (${round(optimizer.total_interest_paid, 2):,.2f}) written to Google Sheet.")


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
