import gspread
from google.oauth2.service_account import Credentials
import datetime
from datetime import timedelta
import calendar
import math

class DebtOptimizer:
    def __init__(self, params, ccs, loans, monthly, income):
        self.start_date = params['Start Date']
        self.end_date = params['End Date']
        self.checking = params['Initial Checking Balance']
        self.buffer = params['Checking Buffer']
        
        self.ccs = ccs
        self.loans = loans
        self.monthly = monthly
        self.income = income
        
        self.schedule_log = []
        self.current_date = self.start_date
        
        # Initialize CC accumulators
        for cc in self.ccs:
            cc['adb_reg_acc'] = 0.0
            cc['adb_promo_acc'] = 0.0
            cc['days_in_cycle'] = 0
            cc['amount_paid_this_cycle'] = 0.0
            cc['remaining_min_due'] = 0.0 # Will be populated on cycle end
            
    def get_next_payday(self):
        """Finds the next payday strictly after the current date."""
        d = self.income['Start Date']
        while d <= self.current_date:
            d += timedelta(days=14)
        return min(d, self.end_date)

    def get_paycheck_amount(self, pay_date):
        """Determines if this is the 1st, 2nd, or 3rd paycheck of the month."""
        month = pay_date.month
        year = pay_date.year
        
        # Rewind to the first payday of this specific month
        d = self.income['Start Date']
        while d.year < year or (d.year == year and d.month < month):
            d += timedelta(days=14)
            
        paycheck_count = 1
        while d < pay_date:
            d += timedelta(days=14)
            paycheck_count += 1
            
        if paycheck_count == 2:
            return self.income['Paycheck 2']
        else:
            return self.income['Paychecks 1 and 3']

    def calculate_cycle_end_date(self, issuer, due_date, grace_days, citi_weekday, chase_day):
        if issuer == 'Chase':
            # Chase is set day of the same month (or previous month if due is early in month)
            # Simplification: Assume the cycle ended in the month prior to the due date month
            m = due_date.month - 1
            y = due_date.year
            if m == 0: m, y = 12, y - 1
            # Handle short months
            day = min(chase_day, calendar.monthrange(y, m)[1])
            return datetime.date(y, m, day)
        elif issuer == 'Citibank':
            m = due_date.month - 1
            y = due_date.year
            if m == 0: m, y = 12, y - 1
            
            # Find the Nth weekday (Mon-Fri)
            count = 0
            d = datetime.date(y, m, 1)
            while True:
                if d.weekday() < 5: # Monday = 0, Friday = 4
                    count += 1
                if count == citi_weekday:
                    return d
                d += timedelta(days=1)
        else:
            # Amex, Cap One, Discover, X1
            return due_date - timedelta(days=grace_days)

    def calc_cc_min_payment(self, cc, interest_charged, fees):
        if cc['Statement Balance'] <= 0:
            return 0.0
        if cc['Statement Balance'] < cc['Min Floor']:
            return cc['Statement Balance']
            
        principal = cc['Statement Balance'] - interest_charged - fees
        
        if cc['Issuer'] == 'American Express':
            return max(cc['Min Floor'], interest_charged + fees + (0.01 * principal), 0.02 * principal)
        elif cc['Issuer'] == 'Discover':
            return max(cc['Min Floor'], 20.0 + interest_charged + fees, 0.02 * cc['Statement Balance'])
        else: # Capital One, Chase, Citibank, X1
            return interest_charged + fees + max(cc['Min Floor'], 0.01 * principal)

    def log_transaction(self, description, amount):
        self.checking += amount
        self.schedule_log.append({
            'Date': self.current_date.strftime('%Y-%m-%d'),
            'Description': description,
            'Amount': round(amount, 2),
            'Checking Balance': round(self.checking, 2)
        })

    def run(self):
        # Determine initial cycle end dates to know when statements cut
        for cc in self.ccs:
            cc['next_due'] = datetime.date(self.current_date.year, self.current_date.month, cc['Due Day'])
            if cc['next_due'] < self.current_date:
                m, y = (cc['next_due'].month % 12) + 1, cc['next_due'].year + (cc['next_due'].month // 12)
                cc['next_due'] = datetime.date(y, m, cc['Due Day'])
            cc['next_cycle_end'] = self.calculate_cycle_end_date(cc['Issuer'], cc['next_due'], cc.get('Grace Period Days', 0), cc.get('Cycle End Nth Weekday', 0), cc.get('Statement Cycle End Day', 0))

        # Main daily loop
        while self.current_date <= self.end_date:
            
            # 1. Income processing
            if (self.current_date - self.income['Start Date']).days % 14 == 0 and self.current_date >= self.income['Start Date']:
                pay_amt = self.get_paycheck_amount(self.current_date)
                self.log_transaction("Paycheck Received", pay_amt)

            # 2. Daily Accruals & Expirations
            for loan in self.loans:
                if loan['Amount Left'] > 0:
                    daily_interest = loan['Amount Left'] * (loan['APR'] / 365.0)
                    loan['Amount Left'] += daily_interest
            
            for cc in self.ccs:
                # Check promo expiration
                if cc.get('Promo End Date') and self.current_date > cc['Promo End Date']:
                    cc['Balance'] += cc['Promo Balance']
                    cc['Promo Balance'] = 0.0
                    cc['Promo End Date'] = None # Avoid double triggering
                    
                cc['adb_reg_acc'] += cc['Balance']
                cc['adb_promo_acc'] += cc['Promo Balance']
                cc['days_in_cycle'] += 1

            # 3. Look-Ahead Optimizer
            next_payday = self.get_next_payday()
            mandatory_total = 0.0
            
            # Sum up mandatory expenses strictly before (or on) next payday
            d = self.current_date
            while d <= next_payday and d <= self.end_date:
                # Monthly payments
                for m in self.monthly:
                    if d.day == m['Due Day']: mandatory_total += m['Amount']
                # Loans
                for loan in self.loans:
                    if loan['Amount Left'] > 0 and d.day == loan['Due Day']:
                        mandatory_total += min(loan['Amount Left'], loan['Minimum Payment'])
                # CC minimums
                for cc in self.ccs:
                    if d == cc['next_due']:
                        mandatory_total += cc['remaining_min_due']
                d += timedelta(days=1)
                
            disposable = self.checking - self.buffer - mandatory_total
            
            # 4. Execute Optimized Extra Payments
            while disposable > 0.01:
                # Find highest APR target
                best_target = None
                max_apr = -1
                
                # Check CCs (only consider regular balance APR for now if promo is 0%)
                for cc in self.ccs:
                    if cc['Balance'] > 0 and not cc['Grace Active'] and cc['APR'] > max_apr:
                        max_apr = cc['APR']
                        best_target = ('cc', cc)
                        
                for loan in self.loans:
                    if loan['Amount Left'] > 0 and loan['APR'] > max_apr:
                        max_apr = loan['APR']
                        best_target = ('loan', loan)
                        
                if not best_target or max_apr <= 0:
                    break # Nowhere useful to put extra money
                    
                target_type, target = best_target
                
                if target_type == 'cc':
                    pay_amt = min(disposable, target['Balance'])
                    target['Balance'] -= pay_amt
                    # Update minimum if we paid early
                    target['remaining_min_due'] = max(0.0, target['remaining_min_due'] - pay_amt)
                    target['amount_paid_this_cycle'] += pay_amt
                    self.log_transaction(f"Extra payment to {target['Card']}", -pay_amt)
                    disposable -= pay_amt
                else:
                    pay_amt = min(disposable, target['Amount Left'])
                    target['Amount Left'] -= pay_amt
                    self.log_transaction(f"Extra payment to {target['Name']}", -pay_amt)
                    disposable -= pay_amt
            
            # 5. Process Mandatory Payments Due Today
            for m in self.monthly:
                if self.current_date.day == m['Due Day']:
                    self.log_transaction(f"Monthly Payment: {m['Name']}", -m['Amount'])
                    
            for loan in self.loans:
                if loan['Amount Left'] > 0 and self.current_date.day == loan['Due Day']:
                    pay_amt = min(loan['Amount Left'], loan['Minimum Payment'])
                    loan['Amount Left'] -= pay_amt
                    self.log_transaction(f"Loan Payment: {loan['Name']}", -pay_amt)
                    
            for cc in self.ccs:
                if self.current_date == cc['next_due']:
                    if cc['remaining_min_due'] > 0:
                        pay_amt = cc['remaining_min_due']
                        
                        # Apply to lowest APR first (Promo) per Rule 7
                        promo_pay = min(pay_amt, cc['Promo Balance'])
                        cc['Promo Balance'] -= promo_pay
                        reg_pay = pay_amt - promo_pay
                        cc['Balance'] -= reg_pay
                        
                        cc['amount_paid_this_cycle'] += pay_amt
                        cc['remaining_min_due'] = 0.0
                        self.log_transaction(f"Min Payment: {cc['Card']}", -pay_amt)
                    
                    # Check Grace Period Loss (Rule 3)
                    if cc['Grace Active'] and cc['amount_paid_this_cycle'] < cc['Statement Balance'] - 0.01:
                        cc['Grace Active'] = False
                        # Retroactive penalty on Statement Balance
                        penalty = cc['Statement Balance'] * (cc['APR']/365) * 30 # Approx prev cycle
                        cc['Balance'] += penalty
                        self.log_transaction(f"Grace Period Lost Penalty: {cc['Card']}", -penalty) # Logged as negative to show it hits the balance
                        self.checking -= (-penalty) # Revert the checking decrement for logging logic
                    elif cc['amount_paid_this_cycle'] >= cc['Statement Balance'] - 0.01:
                        cc['Grace Active'] = True

                    # Advance due date
                    m, y = (cc['next_due'].month % 12) + 1, cc['next_due'].year + (cc['next_due'].month // 12)
                    cc['next_due'] = datetime.date(y, m, cc['Due Day'])
                    cc['amount_paid_this_cycle'] = 0.0

            # 6. Cycle End processing (Generating new statements)
            for cc in self.ccs:
                if self.current_date == cc['next_cycle_end']:
                    interest = 0.0
                    if not cc['Grace Active']:
                        adb_reg = cc['adb_reg_acc'] / cc['days_in_cycle'] if cc['days_in_cycle'] else 0
                        interest = adb_reg * (cc['APR'] / 365.0) * cc['days_in_cycle']
                        cc['Balance'] += interest
                        
                    fees = 0.0
                    if cc.get('Renewal Date') and (self.current_date.month == cc['Renewal Date'].month and self.current_date.day == cc['Renewal Date'].day):
                        fees = cc['Annual Fee']
                        cc['Balance'] += fees
                        
                    cc['Statement Balance'] = cc['Balance'] + cc['Promo Balance']
                    cc['min_due'] = self.calc_cc_min_payment(cc, interest, fees)
                    cc['remaining_min_due'] = cc['min_due']
                    
                    # Reset accumulators
                    cc['adb_reg_acc'] = 0.0
                    cc['adb_promo_acc'] = 0.0
                    cc['days_in_cycle'] = 0
                    
                    cc['next_cycle_end'] = self.calculate_cycle_end_date(cc['Issuer'], cc['next_due'], cc.get('Grace Period Days', 0), cc.get('Cycle End Nth Weekday', 0), cc.get('Statement Cycle End Day', 0))
            
            self.current_date += timedelta(days=1)
            
        return self.schedule_log

def run_self_tests():
    print("Running Mathematical Tests...")
    
    # Standard baseline objects for isolated tests
    base_params = {'Start Date': datetime.date(2026, 1, 1), 'End Date': datetime.date(2026, 1, 2), 'Initial Checking Balance': 1000, 'Checking Buffer': 0}
    base_income = {'Start Date': datetime.date(2026, 1, 1), 'Paycheck 2': 100, 'Paychecks 1 and 3': 100}
    opt = DebtOptimizer(base_params, [], [], [], base_income)
    
    # Test 1A: Chase Min Payment
    chase_cc = {'Issuer': 'Chase', 'Statement Balance': 1200, 'Min Floor': 40}
    assert opt.calc_cc_min_payment(chase_cc, 20, 0) == 60.0 
    
    # Test 1B: Amex Min Payment
    amex_cc = {'Issuer': 'American Express', 'Statement Balance': 1200, 'Min Floor': 40}
    assert opt.calc_cc_min_payment(amex_cc, 20, 0) == 40.0 
    
    # Test 1C: Citi Cycle Date
    citi_due = datetime.date(2026, 8, 15)
    assert opt.calculate_cycle_end_date('Citibank', citi_due, 0, 10, 0) == datetime.date(2026, 7, 14) 
    
    # Test 2A & 2B: Promo Split & Promo Expiration
    print("Running Test 2: Promo Allocations & Expirations...")
    opt2 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 17), 'End Date': datetime.date(2026, 1, 19), 'Initial Checking Balance': 50, 'Checking Buffer': 0},
        [{'Card': 'PromoCard', 'Issuer': 'Chase', 'APR': 0.20, 'Balance': 500, 'Promo Balance': 1000, 'Promo End Date': datetime.date(2026, 1, 17), 'Grace Active': False, 'Due Day': 18, 'Statement Balance': 1500, 'Min Floor': 50, 'Statement Cycle End Day': 25}],
        [], [], base_income
    )
    opt2.ccs[0]['remaining_min_due'] = 50.0 
    opt2.run()
    # Due day was the 18th. 50 minimum should apply entirely to the 0% Promo Balance.
    # Expiration was the 17th, so on the 18th, the remaining promo balance rolls into the regular balance.
    assert opt2.ccs[0]['Promo Balance'] == 0.0
    assert opt2.ccs[0]['Balance'] > 1400 # Started at 500, min pay of 50 went to promo, remaining 950 promo rolled over into Balance.
    
    # Test 3: Grace Period Loss & Retroactive Penalty
    print("Running Test 3: Grace Period Loss...")
    opt3 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 19), 'End Date': datetime.date(2026, 1, 21), 'Initial Checking Balance': 500, 'Checking Buffer': 0},
        [{'Card': 'GraceCard', 'Issuer': 'Chase', 'APR': 0.20, 'Balance': 0, 'Promo Balance': 0, 'Grace Active': True, 'Due Day': 20, 'Statement Balance': 1000, 'Min Floor': 25, 'Statement Cycle End Day': 25}],
        [], [], base_income
    )
    opt3.ccs[0]['remaining_min_due'] = 25.0
    opt3.run()
    # Paid 500 of the 1000 statement balance on the 20th. Grace period breaks.
    assert opt3.ccs[0]['Grace Active'] == False
    assert opt3.ccs[0]['Balance'] > 0 # Retroactive penalty was added
    
    # Test 4A: Dipping into the Buffer for Mandatory Payments
    print("Running Test 4A: Buffer Management...")
    opt4 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 1), 'End Date': datetime.date(2026, 1, 2), 'Initial Checking Balance': 1200, 'Checking Buffer': 1000},
        [], [{'Name': 'Loan', 'Amount Left': 5000, 'APR': 0.05, 'Minimum Payment': 300, 'Due Day': 1}], [],
        {'Start Date': datetime.date(2026, 1, 15), 'Paycheck 2': 0, 'Paychecks 1 and 3': 0}
    )
    opt4.run()
    assert opt4.checking == 900.0 # Breached the 1000 buffer to make the 300 mandatory payment
    
    # Test 4B: Biweekly 3-Paycheck month (May 2026)
    opt.income = {'Start Date': datetime.date(2026, 5, 1), 'Paychecks 1 and 3': 2000, 'Paycheck 2': 2200}
    assert opt.get_paycheck_amount(datetime.date(2026, 5, 1)) == 2000
    assert opt.get_paycheck_amount(datetime.date(2026, 5, 15)) == 2200
    assert opt.get_paycheck_amount(datetime.date(2026, 5, 29)) == 2000

    # Test 5: Final Loan Payoff
    print("Running Test 5: Final Loan Payoff...")
    opt5 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 1), 'End Date': datetime.date(2026, 1, 11), 'Initial Checking Balance': 1000, 'Checking Buffer': 0},
        [], [{'Name': 'Loan', 'Amount Left': 50.0, 'APR': 0.10, 'Minimum Payment': 200, 'Due Day': 10}], [], base_income
    )
    opt5.run()
    # Paid exactly the remaining balance + accrued interest, not the $200 minimum
    assert round(opt5.loans[0]['Amount Left'], 2) == 0.0

    # Test 7: Complex Cash Flow Look-Ahead
    print("Running Test 7: Complex Cash Flow Look-Ahead...")
    opt7 = DebtOptimizer(
        {'Start Date': datetime.date(2026, 1, 1), 'End Date': datetime.date(2026, 1, 8), 'Initial Checking Balance': 2000, 'Checking Buffer': 500},
        [
            {'Card': 'Chase', 'Issuer': 'Chase', 'APR': 0.25, 'Balance': 4000, 'Promo Balance': 0, 'Grace Active': False, 'Due Day': 18, 'Statement Balance': 4000, 'Min Floor': 40, 'Statement Cycle End Day': 25},
            {'Card': 'Amex', 'Issuer': 'American Express', 'APR': 0.20, 'Balance': 0, 'Promo Balance': 2000, 'Grace Active': True, 'Due Day': 10, 'Statement Balance': 2000, 'Min Floor': 40, 'Grace Period Days': 25}
        ],
        [{'Name': 'Loan', 'Amount Left': 10000, 'APR': 0.05, 'Minimum Payment': 300, 'Due Day': 15}],
        [{'Name': 'Rent', 'Amount': 1500, 'Due Day': 1}],
        {'Start Date': datetime.date(2026, 1, 7), 'Paycheck 2': 2500, 'Paychecks 1 and 3': 2500}
    )
    # Mocking the first month's minimums since simulation starts mid-cycle
    opt7.ccs[0]['remaining_min_due'] = 120.0 
    opt7.ccs[1]['remaining_min_due'] = 40.0  
    log = opt7.run()
    
    # On Jan 7: Paycheck arrives. Checking = 3000.
    # Mandatory future = 40 (Amex) + 300 (Loan) + 120 (Chase) = 460.
    # Disposable = 3000 - 500 (Buffer) - 460 = 2040.
    chase_extra_payments = [t for t in log if t['Date'] == '2026-01-07' and 'Extra payment to Chase' in t['Description']]
    
    assert len(chase_extra_payments) == 1
    assert abs(chase_extra_payments[0]['Amount']) == 2040.0
    
    print("All core logic tests passed!\n")

def main():
    run_self_tests()
    
    # ------------- GOOGLE SHEETS I/O ----------------
    SPREADSHEET_NAME = "Debt Optimization Sheet" # Update this to your exact Google Sheet name
    print(f"Connecting to Google Sheets: {SPREADSHEET_NAME}...")
    
    scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file("service_account.json", scopes=scope)
    client = gspread.authorize(creds)
    
    try:
        sheet = client.open(SPREADSHEET_NAME)
    except gspread.exceptions.SpreadsheetNotFound:
        print("Error: Could not find the spreadsheet. Make sure the exact name is correct and shared with your service account email.")
        return

    # Parse Parameters
    param_records = sheet.worksheet("Parameters").get_all_records()
    params = param_records[0]
    params['Start Date'] = datetime.datetime.strptime(str(params['Start Date']), '%Y-%m-%d').date()
    params['End Date'] = datetime.datetime.strptime(str(params['End Date']), '%Y-%m-%d').date()

    # Parse Credit Cards
    cc_records = sheet.worksheet("CreditCards").get_all_records()
    for cc in cc_records:
        cc['Grace Active'] = str(cc['Grace Active']).upper() == 'TRUE'
        if cc.get('Promo End Date'):
            cc['Promo End Date'] = datetime.datetime.strptime(str(cc['Promo End Date']), '%Y-%m-%d').date()
        if cc.get('Renewal Date'):
            cc['Renewal Date'] = datetime.datetime.strptime(str(cc['Renewal Date']) + "/2000", '%m/%d/%Y').date()
        
        # Clean percents
        if isinstance(cc['APR'], str): cc['APR'] = float(cc['APR'].replace('%','')) / 100.0

    # Parse Loans
    loan_records = sheet.worksheet("Loans").get_all_records()
    for loan in loan_records:
        if isinstance(loan['APR'], str): loan['APR'] = float(loan['APR'].replace('%','')) / 100.0

    # Parse Monthly Payments
    monthly_records = sheet.worksheet("MonthlyPayments").get_all_records()

    # Parse Income
    income_records = sheet.worksheet("Income").get_all_records()
    income = income_records[0]
    income['Start Date'] = datetime.datetime.strptime(str(income['Start Date']), '%Y-%m-%d').date()

    print("Data successfully loaded. Running optimization algorithm...")
    
    optimizer = DebtOptimizer(params, cc_records, loan_records, monthly_records, income)
    schedule = optimizer.run()
    
    print("Optimization complete. Formatting output...")
    output_ws = sheet.worksheet("Output")
    output_ws.clear()
    
    headers = ["Description", "Amount", "Checking Balance", "Date"]
    rows = [headers]
    for r in schedule:
        rows.append([r['Description'], r['Amount'], r['Checking Balance'], r['Date']])
        
    output_ws.update(rows)
    print("Success! Output written to Google Sheet.")

if __name__ == "__main__":
    main()
