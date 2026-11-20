# GEMINI.md — Project Guide & Operational Manual

## Project Overview

**Debt Minimizer** (`debt_optimizer.py`) is a deterministic, day-by-day personal finance simulation and debt avalanche optimization engine in Python. It models real-world credit card statement cycles, daily simple interest loan accruals, biweekly income, and recurring bills to construct an aggressive debt payoff schedule while mathematically ensuring checking account liquidity never breaches a designated buffer.

### Key Components

- **Simulation Engine (`DebtOptimizer`)**: Simulates daily cash flow, accruals, fees, minimum payments, and debt avalanche allocations.
- **Lookahead Cushion Engine (`calculate_disposable_cash`)**: Multi-period forward simulation projecting future balances to the simulation end date to protect the checking buffer against clustered future bills.
- **Offline Test Suite (`run_self_tests`)**: 41 comprehensive mathematical unit and integration tests with zero network or external API dependencies.
- **Google Sheets Integration (`gspread`)**: Reads configuration across 5 worksheets and writes an ordered transaction ledger and cumulative interest metric to the `Output` worksheet.

---

## Agent Operational Guidelines (Antigravity Token & Quota Conservation)

The user has a tight Antigravity LLM quota. Future AI agents operating in this repository must follow these rules to minimize prompt/output tokens and context bloat:

### 1. Zero-API Offline Testing (Saves Turns, Output Tokens & Prevents Hangs)
```bash
python test_debt_optimizer.py
# Or:
python debt_optimizer.py --test
```
- **Never invoke Google Sheets live sync during test/dev cycles.** It triggers interactive browser OAuth prompts, produces large payload responses, and burns unnecessary agent turns.
- The standalone test suite in `test_debt_optimizer.py` executes all 39 test scenarios in < 0.5s offline with minimal output tokens.
- Only run live Sheets sync (`python debt_optimizer.py --sheet ...`) when the user explicitly instructs you to sync to their sheet.

### 2. Surgical File Inspection & Editing (Preserves Context Window)
- **Do not read entire files at once.** Both `debt_optimizer.py` (~1,000 lines) and `test_debt_optimizer.py` (~500 lines) will quickly exhaust context tokens if read in full.
- Use `read_file` with targeted `StartLine` and `EndLine` ranges (max 50–100 lines).
- Use `edit_file` with precise, minimal target blocks rather than rewriting whole functions or files.

### 3. File & Line-Range Quick Reference Map
Use this map to jump directly to code locations without burning search/grep tokens:
- **`debt_optimizer.py`**:
  - `Lines 20–48`: Date utilities (`clamp_day`, `advance_month`, `get_first_due_date`, `make_date`)
  - `Lines 50–135`: Dataclasses (`CreditCard`, `Loan`, `MonthlyPayment`, `IncomeSchedule`)
  - `Lines 137–365`: `DebtOptimizer.__init__` (state setup, renewal normalization, cycle date resolution)
  - `Lines 367–450`: Income schedules, issuer cycle end logic, credit card minimum payment formulas
  - `Lines 452–594`: `get_projected_min_margin()`, `calculate_disposable_cash()` (lookahead cushion engine)
  - `Lines 595–897`: `run()` (daily simulation loop, 6-step order of operations)
  - `Lines 900–908`: `run_self_tests()` test delegator
  - `Lines 910–1302`: Google Sheets I/O, parsers, batch Output writer, conditional formatting (`apply_output_formatting`)
  - `Lines 1304–1355`: CLI argument parsing and entrypoint
- **`test_debt_optimizer.py`**:
  - `Tests 1–9`: Baseline issuer formulas and cycle closures
  - `Tests 10–32`: Extended edge cases, month clamping, annual fees, debt avalanche priority
  - `Tests 33–36`: Citibank loop guard, dynamic grace penalty, simple interest loans, multi-period cushion
  - `Tests 37–39`: Initial minimum payment column support, grace card statement payment with lookahead buffer preservation, unconditional minimums driving checking negative
  - `Tests 40–41`: Semi-monthly pay schedule (1st and 15th), multiple simultaneous income schedules

### 4. Concise Communication Style
- Keep explanations direct, technical, and token-efficient.
- Avoid repeating entire code blocks or test transcripts when a concise summary suffices.

---

## Core Domain Rules & Architecture

### 1. Cash Flow Priority Hierarchy (Strict Order: Highest to Lowest)

The simulation governs all cash flow movements strictly according to this 4-tier priority hierarchy:

1. **Priority 1: Pay Monthly Payments (Fixed Living Expenses)**
   - Fixed, non-debt recurring obligations (`MonthlyPayment`: rent, utilities, insurance) are executed unconditionally on their scheduled due dates.
   - Living expenses take absolute precedence and are never reduced, deferred, or skipped.

2. **Priority 2: Pay Minimum Payments on Loans and Credit Cards**
   - Contractual debt minimum payments (`Loan.minimum_payment` and `CreditCard.remaining_min_due`) are mandatory obligations required to prevent defaults, penalty fees, and credit damage.
   - Executed unconditionally on their due dates. If available checking cash is insufficient, minimums are paid in full, driving the checking account into a negative balance (overdraft/deficit, visually flagged in **Deep Red** on the Output sheet).

3. **Priority 3: Maintain Checking Balance Above Buffer**
   - The designated safety reserve (`Checking Buffer` $\ge 0$) is protected against elective cash allocations. Because `Checking Buffer` $\ge 0$, this also strictly guarantees the checking account never dips negative for any discretionary outflow.
   - Full statement balance payments on grace-active credit cards (to prevent retroactive interest) and extra avalanche payments are permitted **only** if the multi-period forward lookahead simulation confirms checking never breaches the buffer across the entire horizon through `End Date`.
   - Any dip below buffer (while remaining $\ge 0$) caused by clustered mandatory payments is visually flagged in **Light Red** on the Output sheet.

4. **Priority 4: Allocate Extra Payments to Minimize Lifetime Interest (Debt Avalanche)**
   - Discretionary surplus cash confirmed safe by the forward lookahead cushion engine is waterfalled to active interest-bearing debts, strictly descending by APR.

---

### 2. Daily Simulation Order of Operations (`DebtOptimizer.run`)
For each calendar day from `Start Date` to `End Date`:
1. **Annual Fees**: Check recurring annual calendar match `(current_date.month == cc.renewal_date.month and current_date.day == clamp_day(current_date.year, cc.renewal_date.month, cc.renewal_date.day))`; if so, posts fee to card balance as a regular purchase (included in principal and statement balance, not treated as an upfront 100% penalty fee). Recurring yearly without date mutation.
2. **Income Processing**: Credit paychecks from all active income schedules firing today via `get_total_income(current_date)`.
3. **Daily Accruals & Expirations**:
   - Loans: `loan.accrued_interest += loan.amount_left * (loan.apr / 365.0)`.
   - Credit Cards: If `current_date > promo_end_date`, shift promo balance to regular balance.
   - Accumulate Average Daily Balance: `adb_reg_acc += balance`, `days_in_cycle += 1`.
4. **Lookahead Optimizer & Extra Payments (Priority 4)**:
   - If `checking > buffer`, call `calculate_disposable_cash()`.
   - Waterfall extra payments strictly to highest APR interest-bearing debt (non-grace credit card balance or loan balance).
5. **Mandatory Payments Due Today (Priorities 1 & 2, with Grace Statement governed by Priority 3)**:
   - Monthly non-debt bills (`MonthlyPayment`): Paid unconditionally (Priority 1).
   - Loan minimum payments (`Loan`): Paid unconditionally (Priority 2), driving checking negative if cash is insufficient.
   - Credit Card payments: Statement balance if grace is active and forward lookahead cushion allows (Priority 3: `safe_target = round(target_pay + min_margin, 2)`); otherwise mandatory minimum payment is paid unconditionally (Priority 2).
   - Evaluate grace period status and 2-month restoration streak.
6. **Statement Cycle End Processing**:
   - If `current_date == next_cycle_end`: compute ADB interest (`adb_reg_acc / days_in_cycle * (apr / 365) * days`), add to card balance, update `statement_balance`, reset cycle accumulators, and advance `cycle_due_date` and `next_cycle_end`.

---

### 3. Issuer-Specific Statement Cycle Rules (`calculate_cycle_end_date`)

| Issuer | Cycle End Logic | Configuration Fields |
| :--- | :--- | :--- |
| **Chase** | Fixed day of month, decoupled from due date. If `chase_day < due_day` and `(due_day - chase_day) >= 15`, cycle end is in the same month; otherwise prior month. | `Statement Cycle End Day` |
| **Citibank** | Nth business day (Monday=0 to Friday=4) of the month prior to due date. Loop guard raises `ValueError` if N exceeds month's business days. | `Cycle End Nth Weekday` |
| **Amex, CapOne, Discover** | Exact calendar offset: `due_date - timedelta(days=grace_period_days)`. | `Grace Period Days` |
| **X1** | Exact calendar offset using `grace_period_days` (typically 23 days), with a rollback to Friday if the calculated cycle end date lands on a Saturday: `due_date - timedelta(days=grace_period_days)` (rolled back 1 day if Saturday). | `Grace Period Days` (typically 23) |

---

### 4. Minimum Payment Formulas (`calc_cc_min_payment`)
- If `statement_balance <= 0`: `$0.00`.
- If `statement_balance < min_floor`: full `statement_balance`.
- Principal portion: `P = max(0.0, statement_balance - interest_charged - fees)`.

| Issuer | Minimum Payment Formula |
| :--- | :--- |
| **American Express** | `max(min_floor, interest_charged + fees + 0.01 * P, 0.02 * P)` |
| **Discover** | `max(min_floor, 20.0 + interest_charged + fees, 0.02 * statement_balance)` |
| **Chase, CapOne, Citi, X1** | `interest_charged + fees + max(min_floor, 0.01 * P)` |

*Note*: Capped at `min(statement_balance, calculated_amount)`. Sub-cent residuals `< 0.005` are clamped to `0.0`.

---

### 5. Credit Card Grace Period Dynamics
- **Active Grace**: No interest accrues. Statement balance is paid on due date if funds exist down to buffer.
- **Grace Loss**: If `amount_paid_this_cycle < statement_balance - 0.01` on due date:
  - Grace is revoked (`grace_active = False`).
  - Retroactive interest penalty is added to the **card balance** (not checking):
    $$\text{penalty} = \text{statement\_balance} \times \frac{\text{APR}}{365} \times \text{penalty\_days}$$
  - Penalty days = card `grace_period_days` (Amex/CapOne/Discover) or days between due date and prior cycle end (Chase/Citi).
- **Grace Restoration (Fix H)**: Requires **2 consecutive billing cycles** of paying the statement balance in full (`consecutive_full_payments >= 2`). A partial or missed payment resets streak to 0.

---

### 6. Installment Loans (Simple Interest vs. Compounding)
- Loans accrue simple daily interest: `accrued_interest += amount_left * (apr / 365.0)`.
- Principal (`amount_left`) **does not compound daily**.
- All payments (mandatory minimum or extra avalanche) satisfy `accrued_interest` first; remaining payment reduces principal.
- Cumulative interest metric (`total_interest_paid`) increments whenever accrued loan interest is paid.

---

### 7. Multi-Period Cash-Flow Lookahead (`calculate_disposable_cash` & `get_projected_min_margin`)
To avoid stranding checking balance below `buffer` during future clustered-expense weeks:
1. Projects checking balance daily from `current_date` to `end_date`.
2. Simulates all scheduled future inflows (biweekly paychecks) and baseline outflows (monthly payments, loan minimums, credit card minimums/statement payments).
3. Computes the minimum liquidity cushion over the entire horizon:
   $$\text{min\_margin} = \min_{t \in [\text{current}, \text{end}]} \left( \text{projected\_balance}(t) - \text{buffer} \right)$$
4. Safe disposable cash is `max(0.0, round(min_margin, 2))`. If `min_margin <= 0.01`, disposable cash is `$0.00`.

---

### 8. Debt Avalanche Waterfall Priority
When disposable cash is available:
1. Filters active debts for interest-bearing balances (`cc.balance > 0 and not cc.grace_active and cc.apr > 0` or `loan.amount_left > 0 and loan.apr > 0`).
2. Sorts candidate debts strictly descending by APR.
3. Allocates `min(disposable, debt_balance)` to top APR candidate.
4. Early payments reduce `remaining_min_due` for the current cycle (Rule 9).

---

## Google Sheets Schema Reference

### Worksheets & Required Headers

#### 1. `Parameters`
| Column Header | Type | Description |
| :--- | :--- | :--- |
| `Start Date` | Date (`YYYY-MM-DD` or `MM/DD/YYYY`) | Simulation start date. |
| `End Date` | Date (`YYYY-MM-DD` or `MM/DD/YYYY`) | Simulation end date. |
| `Initial Checking Balance` | Currency / Float | Starting checking account balance. |
| `Checking Buffer` | Currency / Float | Hard minimum cash floor in checking. |

#### 2. `CreditCards`
| Column Header | Type | Description |
| :--- | :--- | :--- |
| `Card` | String | Card display name. |
| `Issuer` | String | `Chase`, `Citibank`, `American Express`, `Capital One`, `Discover`, or `X1`. |
| `APR` | Percentage / Float | Annual Percentage Rate (e.g., `24.99%` or `0.2499`). |
| `Balance` | Currency / Float | Total balance currently on card. |
| `Promo Balance` | Currency / Float | Balance portion subject to 0% promotional rate. |
| `Promo End Date` | Date / Null | Expiration date of 0% promo APR. |
| `Statement Cycle End Day` | Integer (1–31) | Fixed cycle close day (**Required for Chase**). |
| `Statement Balance` | Currency / Float | Last closed statement balance. |
| `Grace Active` | Boolean (`TRUE`/`FALSE`) | `TRUE` if grace is currently intact. |
| `Due Day` | Integer (1–31) | Day of month payment is due. |
| `Grace Period Days` | Integer | Days between cycle end and due date (**Required for Amex, CapOne, Discover, X1**; X1 uses 23 with Saturday-to-Friday rollback). |
| `Cycle End Nth Weekday` | Integer (1–5) | Nth business day of prior month (**Required for Citibank**). |
| `Min Floor` | Currency / Float | Absolute minimum floor (e.g., `$25.00`, `$35.00`, or `$40.00`). |
| `Annual Fee` | Currency / Float | Card annual fee (e.g., `$95.00`, `$695.00`). |
| `Renewal Date` | Date / `MM/DD` | Fee posting date (recurring month/day). |
| `Initial Minimum Payment` | Currency / Float / Null | Optional explicit minimum payment for cycle 1 to include prior statement interest/fees. |

#### 3. `Loans`
| Column Header | Type | Description |
| :--- | :--- | :--- |
| `Name` | String | Loan display name. |
| `Amount Left` | Currency / Float | Outstanding principal balance. |
| `APR` | Percentage / Float | Annual interest rate (e.g., `8.5%`). |
| `Minimum Payment` | Currency / Float | Scheduled monthly payment amount. |
| `Due Day` | Integer (1–31) | Day of month payment is due. |

#### 4. `MonthlyPayments`
| Column Header | Type | Description |
| :--- | :--- | :--- |
| `Name` | String | Bill name (Rent, Utilities, Insurance). |
| `Amount` | Currency / Float | Monthly expense amount. |
| `Due Day` | Integer (1–31) | Day of month bill is paid. |

#### 5. `Income`
Multiple rows supported (each row is an independent income schedule):
| Column Header | Type | Description |
| :--- | :--- | :--- |
| `Name` | String | Optional label for this income source (e.g. `Primary Salary`, `Side Gig`). |
| `Type` | String | `Biweekly` or `Semi-Monthly`. |
| `Start Date` | Date | Reference anchor date for 14-day cycle (**Required for Biweekly**; ignored for Semi-Monthly). |
| `Paycheck Amount` | Currency / Float | Paycheck amount. For Semi-Monthly, paid on 1st & 15th. For Biweekly, paid on days 1–14 & 29–31 (or all days if `Paycheck 2 Amount` is omitted). |
| `Paycheck 2 Amount` | Currency / Float / Null | Optional alternate amount for Biweekly paydays landing on days 15–28. |

---

### Output Sheet Format

The script writes to worksheet **`Output`**:
- **Columns A–D**: Chronological transaction ledger
  - **Column A**: `Date` (`YYYY-MM-DD`)
  - **Column B**: `Description` (e.g., `Paycheck Received`, `Payment to Chase`, `Extra payment to HighAPRCard`, `Monthly Payment: Rent`)
  - **Column C**: `Amount` (Positive for inflows, negative for outflows)
  - **Column D**: `Checking Balance` (Cumulative running balance)
- **Sorting**: Ordered by `Date` ascending, then `Amount` descending (inflows first, then extra payments, then mandatory bills).
- **Conditional Formatting (Column D)**:
  - **Deep Red** (`#D94040` / dark red text on bold white): `Checking Balance < 0`.
  - **Light Red** (`#FFD9D9` / dark red text): `0 <= Checking Balance < Buffer`.
- **Cell G1**: Header `"Total Interest Paid"`
- **Cell G2**: Numeric total dollar amount of all interest paid over simulation period (Loan accrued interest paid + CC ADB interest billed + Retroactive grace loss penalties).

---

## Common Development Tasks & Commands

### 1. Run Offline Tests
```bash
python debt_optimizer.py --test
```

### 2. Run with Custom Google Sheet (When Requested by User)
```bash
# Using OAuth2 Desktop Client (credentials.json)
python debt_optimizer.py --sheet "Debt Optimization Sheet"

# Using Service Account JSON fallback
python debt_optimizer.py --sheet "Debt Optimization Sheet" --service-account service_account.json
```

### 3. Adding a New Unit Test Scenario
Add new test cases inside `run_self_tests()` in `test_debt_optimizer.py`:
```python
# Test NN: Short description
print("Test NN: Description...")
opt = DebtOptimizer(params, ccs, loans, monthly, income)
log = opt.run()
assert opt.checking >= opt.buffer
print("  -> Passed!")
```

### 4. Code Guidelines & Date Clamping
- Always use `clamp_day(year, month, day)` and `make_date(year, month, day)` when calculating calendar dates to prevent `ValueError` on month boundaries (e.g., Feb 28/29, Apr 30).
- Float precision: Use `round(val, 2)` for currency calculations and compare zero thresholds with `< 0.005` to prevent floating-point residual dust.
