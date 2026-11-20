# Debt Minimizer & Cash Flow Payment Schedule Optimizer

A deterministic, day-by-day personal finance simulation and debt optimization engine. It models complex credit card statement cycles, daily simple interest loan accruals, biweekly income, and recurring bills to construct an aggressive debt avalanche payment schedule while mathematically guaranteeing your checking account never breaches a designated buffer.

---

## Architecture & Workflow

### 1. High-Level System Architecture

```mermaid
flowchart TD
    subgraph Input ["Google Sheets Inputs"]
        P["Parameters Tab"]
        CC["CreditCards Tab"]
        L["Loans Tab"]
        M["MonthlyPayments Tab"]
        I["Income Tab"]
    end

    subgraph Engine ["Simulation Engine (debt_optimizer.py)"]
        Init["Data Ingestion & Validation"]
        Loop["Day-by-Day Simulation Loop"]
        Lookahead["Multi-Period Forward Lookahead<br/>(Buffer Preservation)"]
        Waterfall["Debt Avalanche Waterfall<br/>(Highest APR First)"]
    end

    subgraph Output ["Google Sheets Output"]
        Ledger["Columns A-D: Sorted Ledger<br/>(Date, Description, Amount, Balance)"]
        Interest["Cells G1:G2: Total Interest Paid"]
    end

    Input --> Init
    Init --> Loop
    Loop <--> Lookahead
    Lookahead --> Waterfall
    Waterfall --> Loop
    Loop --> Output
```

### 2. Daily Simulation Cycle

```mermaid
flowchart LR
    A["1. Annual Fees<br/>(Renewal Date)"] --> B["2. Income<br/>(Payday Check)"]
    B --> C["3. Daily Accruals<br/>(Loan & ADB Accrual)"]
    C --> D["4. Lookahead<br/>(Extra Payments)"]
    D --> E["5. Mandatory<br/>(Due Payments)"]
    E --> F["6. Cycle Close<br/>(ADB & Stmt Bal)"]
```

### 3. Multi-Period Liquidity Protection

```mermaid
flowchart TD
    Surplus["Checking > Buffer on Payday"] --> Forward["Forward Projection across Future Paydays"]
    Forward --> MinBal{"Projected Future Margin<br/>min(B(t) - Buffer) > 0?"}
    MinBal -- No --> Hold["Hold Cash: $0 Extra Payment<br/>(Protects Future Clustered Bills)"]
    MinBal -- Yes --> Extra["Pay min(disposable, debt_bal) to Highest APR Debt"]
```

### 4. Cash Flow Priority Hierarchy (Highest to Lowest)

All financial cash flows in the simulation strictly observe a 4-tier priority hierarchy:

1. **Priority 1: Pay Monthly Payments (Fixed Living Expenses)**: Recurring non-debt fixed living expenses (`MonthlyPayment`: Rent, Utilities, Insurance) are paid unconditionally on their due dates.
2. **Priority 2: Pay Minimum Payments on Loans & Credit Cards**: Mandatory debt minimum payments are executed unconditionally on their due dates to prevent default, penalties, and credit damage, even if checking is depleted (driving the balance negative into overdraft, flagged in **Deep Red** on the Output sheet).
3. **Priority 3: Maintain Checking Balance Above Buffer**: The designated safety cash floor (`Checking Buffer` $\ge 0$) is protected against elective cash allocations. Because `Checking Buffer` $\ge 0$, this also strictly guarantees checking never goes negative for any discretionary outflows. Full statement payments (to maintain credit card grace) and extra avalanche payments are made only if the multi-period forward lookahead cushion guarantees the buffer is never breached. Any mandatory-only dip below buffer (while remaining $\ge 0$) is flagged in **Light Red**.
4. **Priority 4: Allocate Extra Payments to Minimize Lifetime Interest (Debt Avalanche)**: Surplus cash confirmed safe by the forward lookahead engine is waterfalled to active interest-bearing debts, strictly descending by APR.

---

## Setup & Installation

### 1. Clone & Environment Setup

```bash
# Clone repository
git clone https://github.com/osaesm/DebtMinimizer.git
cd DebtMinimizer

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# Install required dependencies
pip install -r requirements.txt
```

### 2. Google Cloud API Access

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project (e.g., `Debt-Optimizer`).
3. Under **APIs & Services > Library**, search for and enable:
   - **Google Sheets API**
   - **Google Drive API**
4. Under **APIs & Services > OAuth consent screen**:
   - User Type: **External**.
   - Fill in App Name and support email.
   - Add scopes: `https://www.googleapis.com/auth/spreadsheets` and `https://www.googleapis.com/auth/drive`.
   - Under **Test users**, add your personal Google account email address.
5. Under **APIs & Services > Credentials**:
   - Click **+ Create Credentials > OAuth client ID**.
   - Application type: **Desktop app**.
   - Download the client secret JSON.
6. Rename the file to `credentials.json` and save it directly in the project root:
   ```bash
   mv ~/Downloads/client_secret_*.json ./credentials.json
   ```

---

## Google Sheet Schema

Create a Google Spreadsheet named `Debt Optimization Sheet` (or pass a custom name via `--sheet`) containing the following tabs and headers:

| Tab | Required Column Headers | Description |
| :--- | :--- | :--- |
| **`Parameters`** | `Start Date`, `End Date`, `Initial Checking Balance`, `Checking Buffer` | Simulation date range and cash floor. |
| **`CreditCards`** | `Card`, `Issuer`, `APR`, `Balance`, `Promo Balance`, `Promo End Date`, `Statement Cycle End Day`, `Statement Balance`, `Grace Active`, `Due Day`, `Grace Period Days`, `Cycle End Nth Weekday`, `Min Floor`, `Annual Fee`, `Renewal Date`, `Initial Minimum Payment` (Optional) | Supports Chase, Citibank, Amex, Capital One, Discover, and X1 rules. `Initial Minimum Payment` overrides cycle 1 to include billed interest. |
| **`Loans`** | `Name`, `Amount Left`, `APR`, `Minimum Payment`, `Due Day` | Simple interest installment loans. |
| **`MonthlyPayments`** | `Name`, `Amount`, `Due Day` | Recurring non-debt bills (rent, utilities, etc.). |
| **`Income`** | `Type`, `Paycheck Amount`, `Start Date` (Required for Biweekly), `Paycheck 2 Amount` (Optional), `Name` (Optional) | Multiple income schedules supporting `Biweekly` (14-day cycle) and `Semi-Monthly` (1st and 15th of each month). |

### Output Tab
The script generates an **`Output`** worksheet:
- **Columns A–D**: Full chronological transaction ledger (`Date`, `Description`, `Amount`, `Checking Balance`), ordered by Date ascending and Amount descending.
- **Conditional Formatting (Column D)**:
  - **Deep Red** (`#D94040` / dark red text on bold white): `Checking Balance < 0`.
  - **Light Red** (`#FFD9D9` / dark red text): `0 <= Checking Balance < Buffer`.
- **Cell G1**: Label `"Total Interest Paid"`.
- **Cell G2**: Total dollar amount of interest paid over the entire simulation period.

---

## Usage

### Run Offline Mathematical Verification Test Suite
```bash
# Run standalone test runner
python test_debt_optimizer.py

# Or via CLI flag
python debt_optimizer.py --test
```

### Run Simulation Against Google Sheets
```bash
# Default (looks for 'Debt Optimization Sheet' and 'credentials.json')
python debt_optimizer.py

# Custom sheet name or credentials path
python debt_optimizer.py --sheet "My 2026 Debt Plan" --creds my_creds.json

# Fallback: Service Account Authentication
python debt_optimizer.py --sheet "My 2026 Debt Plan" --service-account service_account.json
```

On first run, your browser opens to authorize OAuth 2.0 access. A cached `token.json` is generated for headless execution on subsequent runs.

---

## Test Coverage & Mathematical Verification

The simulation engine is backed by a 41-scenario test suite in `test_debt_optimizer.py` verifying edge cases, complex issuer rules, and liquidity preservation.

### Coverage Summary

| Metric | Result |
| :--- | :--- |
| **Test Suite Pass Rate** | **41 / 41 (100%)** |
| **Core Engine (`debt_optimizer.py`)** | **~99% of engine logic** *(68% file total; remaining 32% is interactive Sheets API I/O)* |
| **Test File (`test_debt_optimizer.py`)** | **99%** |
| **Combined Codebase Coverage** | **78%** |

### Test Breakdown

- **Baseline Tests (1–9)**: Chase/Amex minimum payment formulas, Citibank cycle end calculation, promotional balance expirations, grace loss and retroactive interest, checking buffer dips, 3-paycheck months (May 2026), and debt avalanche lookaheads.
- **Extended Issuer & Amortization Tests (10–32)**: Discover 20 + Interest + Fees rule, CapOne/Citi/X1 formulas, sub-floor balances, zero/negative balances, Amex 2% principal dominance, December/January cycle rollovers, leap-year and month-end clamping (Feb 28 / Apr 30), annual fee billing, early loan payoffs, and lingering average daily balance (ADB) interest.
- **Engine Guards & Liquidity Protection (33–36)**:
  - **Test 33**: Out-of-bounds Citibank weekday loop guard (`ValueError`).
  - **Test 34**: Card/issuer dynamic retroactive grace penalty calculation.
  - **Test 35**: Simple interest loan accrual verifying non-compounding principal.
  - **Test 36**: Multi-period forward cash-flow projection preventing buffer breaches across clustered-expense periods.
- **Advanced Features & Schedules (37–41)**:
  - **Test 37**: Initial minimum payment column support for cycle 1 billed interest/fees.
  - **Test 38**: Grace card statement payment with multi-period forward lookahead buffer preservation.
  - **Test 39**: Unconditional debt minimum payments driving checking account negative (overdraft).
  - **Test 40**: Semi-monthly income schedule (1st and 15th paydays with fixed amounts).
  - **Test 41**: Multiple simultaneous income schedules (Biweekly + Semi-Monthly summing on shared paydays).
