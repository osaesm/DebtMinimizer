import datetime
from datetime import timedelta
import sys

from debt_optimizer import DebtOptimizer, advance_month, make_date, clamp_day, get_first_due_date

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

    # Test 18b: Start after renewal date in year 1, fee triggers on renewal date in year 2
    opt18b = DebtOptimizer(
        {'Start Date': datetime.date(2026, 6, 1), 'End Date': datetime.date(2027, 6, 1), 'Initial Checking Balance': 0, 'Checking Buffer': 0},
        [{'Card': 'FeeCard', 'Issuer': 'American Express', 'APR': 0.0, 'Balance': 500, 'Promo Balance': 0, 'Grace Active': False, 'Due Day': 25, 'Statement Balance': 0, 'Min Floor': 0, 'Grace Period Days': 25, 'Annual Fee': 95.0, 'Renewal Date': datetime.date(2026, 4, 15)}],
        [], [], {'Start Date': datetime.date(2026, 6, 1), 'Paychecks 1 and 3': 0, 'Paycheck 2': 0}
    )
    opt18b.run()
    assert opt18b.ccs[0].balance == 595.0

    # Test 18c: Multi-year simulation triggers annual fee once per calendar year
    opt18c = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 1), 'End Date': datetime.date(2027, 12, 31), 'Initial Checking Balance': 0, 'Checking Buffer': 0},
        [{'Card': 'FeeCard', 'Issuer': 'American Express', 'APR': 0.0, 'Balance': 500, 'Promo Balance': 0, 'Grace Active': False, 'Due Day': 25, 'Statement Balance': 0, 'Min Floor': 0, 'Grace Period Days': 25, 'Annual Fee': 95.0, 'Renewal Date': '04/15'}],
        [], [], {'Start Date': datetime.date(2026, 1, 1), 'Paychecks 1 and 3': 0, 'Paycheck 2': 0}
    )
    opt18c.run()
    assert opt18c.ccs[0].balance == 690.0  # Charged on 2026-04-15 and 2027-04-15
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

    # Test 37: Two-Pass LP Optimal Allocation & Split Across Multiple Debts
    print("Test 37: Two-Pass LP Optimal Allocation & Split Across Competing Debts...")
    opt37 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 3, 1), 'End Date': datetime.date(2026, 3, 10), 'Initial Checking Balance': 1500.0, 'Checking Buffer': 500.0},
        [{'Card': 'Card1', 'Issuer': 'Chase', 'APR': 0.24, 'Balance': 600.0, 'Promo Balance': 0.0, 'Promo End Date': None, 'Statement Cycle End Day': 20, 'Statement Balance': 600.0, 'Grace Active': False, 'Due Day': 20, 'Grace Period Days': 25, 'Cycle End Nth Weekday': 0, 'Min Floor': 40.0, 'Annual Fee': 0.0, 'Renewal Date': None}],
        [{'Name': 'Loan1', 'Amount Left': 2000.0, 'APR': 0.12, 'Minimum Payment': 100.0, 'Due Day': 25}],
        [],
        {'Start Date': datetime.date(2026, 4, 1), 'Paycheck 2': 1000.0, 'Paychecks 1 and 3': 1000.0}
    )
    schedule37 = opt37.run()
    # Disposable cash is $1000 (1500 - 500 buffer).
    # Card1 (24% APR) should receive $600 (full payoff).
    # Loan1 (12% APR) should receive the remaining $400.
    # Buffer $500.0 must be preserved.
    c1_extra = [t for t in schedule37 if 'Extra payment to Card1' in t['Description']]
    l1_extra = [t for t in schedule37 if 'Extra payment to Loan1' in t['Description']]
    assert len(c1_extra) == 1 and abs(c1_extra[0]['Amount']) == 600.0, "Card1 should receive $600"
    assert len(l1_extra) == 1 and abs(l1_extra[0]['Amount']) == 400.0, "Loan1 should receive $400"
    assert round(opt37.ccs[0].balance, 2) == 0.0, "Card1 balance should be 0.0"
    # Simple interest rule: the 400 payment satisfies day-1 accrued interest ($0.66) first, then principal
    assert round(opt37.loans[0].amount_left, 2) == 1600.66, "Loan1 principal should be reduced to 1600.66"
    assert opt37.checking == 500.0, "Checking must match buffer floor"
    print("  -> Passed!")

    print("\n======================================================================")
    print("ALL 37 MATHEMATICAL TESTS PASSED PERFECTLY!")
    print("======================================================================\n")


if __name__ == '__main__':
    run_self_tests()
