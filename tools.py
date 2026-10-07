"""Tools for RentCover: will the rent pay the mortgage, and how much is left over?

1. lookup_market_rent    -> external data (US Census Bureau ACS API)
2. estimate_repairs      -> original: repair cost range from what a buyer can see from outside
3. get_mortgage_rate     -> external data (Freddie Mac rate survey via FRED)
4. calculate_cash_flow   -> original: rent vs mortgage and costs, DSCR, break even rent, max price
5. stress_test_cash_flow -> original: Monte Carlo of the monthly cash flow and the cash reserve needed
6. analyze_cash_then_refinance -> original: buy with cash, renovate, then refinance against the new value

Every tool returns a JSON string. Errors are returned as JSON with an
"error" message that tells the model what to do next, never raised.
"""

import datetime
import json
import os
import random
import statistics
import urllib.error
import urllib.parse
import urllib.request

# --- Shared defaults (all overridable by the model) ---

DEFAULTS = {
    "vacancy_rate": 0.08,          # share of the year the unit sits empty
    "management_rate": 0.10,       # property manager fee, share of rent
    "maintenance_rate": 0.10,      # repairs and capex reserve, share of rent
    "property_taxes_annual": 2000,
    "insurance_annual": 1500,
    "buyer_premium_pct": 0.05,     # auction buyer's premium, share of price (0 for a regular sale)
    "closing_cost_pct": 0.03,      # title, lender fees, recording, transfer taxes, share of price
    "holding_months": 4,           # months from purchase until rented
    "holding_utilities_monthly": 200,
    "hoa_monthly": 0,              # HOA or condo fee; often hundreds a month for condos and townhomes
    "rent_growth_rate": 0.025,     # yearly rent increase, used only for the timeline
    "expense_growth_rate": 0.025,  # yearly increase in taxes, insurance and HOA, used only for the timeline
    "appreciation_rate": 0.0,      # yearly home value growth; 0 keeps the timeline conservative
}

BEDROOM_VARS = {
    0: "B25031_002E",
    1: "B25031_003E",
    2: "B25031_004E",
    3: "B25031_005E",
    4: "B25031_006E",
    5: "B25031_007E",
}
ALL_UNITS_VAR = "B25031_001E"
MEDIAN_CONTRACT_VAR = "B25058_001E"         # median contract rent, all units
UPPER_QUARTILE_CONTRACT_VAR = "B25059_001E"  # upper quartile (75th percentile) contract rent, all units
ACS_YEARS = ["2024", "2023"]  # try newest 5-year release first


def _err(message: str) -> str:
    return json.dumps({"error": message})


def _num(value, name: str, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{name}' must be a number, got {value!r}.")
    if minimum is not None and x < minimum:
        raise ValueError(f"'{name}' must be at least {minimum}, got {x}.")
    if maximum is not None and x > maximum:
        raise ValueError(f"'{name}' must be at most {maximum}, got {x}.")
    return x


def _assumptions(args: dict) -> dict:
    a = {}
    for key, default in DEFAULTS.items():
        a[key] = _num(args.get(key, default), key, minimum=0)
    for pct in ["vacancy_rate", "management_rate", "maintenance_rate", "buyer_premium_pct", "closing_cost_pct",
                "rent_growth_rate", "expense_growth_rate", "appreciation_rate"]:
        if a[pct] > 1:
            raise ValueError(f"'{pct}' is a decimal share, so 5% is 0.05, not 5. Got {a[pct]}.")
    return a


# --- Tool 1: external data ---


def _census_upper_quartile_ratio(zip_code: str, year: str, key: str) -> float | None:
    """75th percentile rent divided by median rent for the ZIP, or None if unavailable.

    Kept as its own request so that a problem here never breaks the main median lookup.
    """
    query = urllib.parse.urlencode({
        "get": f"{MEDIAN_CONTRACT_VAR},{UPPER_QUARTILE_CONTRACT_VAR}",
        "for": f"zip code tabulation area:{zip_code}",
        "key": key,
    })
    try:
        with urllib.request.urlopen(f"https://api.census.gov/data/{year}/acs/acs5?{query}", timeout=10) as resp:
            header, values = json.loads(resp.read().decode())[:2]
        record = dict(zip(header, values))
        median = int(record.get(MEDIAN_CONTRACT_VAR) or -1)
        upper = int(record.get(UPPER_QUARTILE_CONTRACT_VAR) or -1)
    except Exception:
        return None
    if median <= 0 or upper <= 0 or upper < median:
        return None
    return upper / median



def lookup_market_rent(zip_code: str, bedrooms: int) -> str:
    zip_code = str(zip_code).strip()
    if not (len(zip_code) == 5 and zip_code.isdigit()):
        return _err(f"'{zip_code}' is not a 5 digit US ZIP code. Ask the user for the property's ZIP code.")
    try:
        beds = int(bedrooms)
    except (TypeError, ValueError):
        return _err("'bedrooms' must be a whole number. Ask the user how many bedrooms the property has.")
    beds = max(0, min(beds, 5))

    key = os.environ.get("CENSUS_API_KEY")
    if not key:
        return _err("The server is missing CENSUS_API_KEY, so live rent data is unavailable. "
                    "Ask the user for their own rent estimate and continue the analysis with it.")

    variables = f"{ALL_UNITS_VAR},{BEDROOM_VARS[beds]}"
    last_problem = ""
    for year in ACS_YEARS:
        query = urllib.parse.urlencode({
            "get": variables,
            "for": f"zip code tabulation area:{zip_code}",
            "key": key,
        })
        url = f"https://api.census.gov/data/{year}/acs/acs5?{query}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                body = resp.read().decode()
        except urllib.error.HTTPError as e:
            last_problem = f"Census API returned HTTP {e.code} for {year}."
            continue
        except (urllib.error.URLError, TimeoutError) as e:
            return _err(f"Could not reach the Census API ({e}). Ask the user for a rent estimate "
                        "and continue with it, noting it was not verified.")
        if not body.strip():
            last_problem = f"No Census data for ZIP {zip_code} in {year}."
            continue
        try:
            rows = json.loads(body)
            header, values = rows[0], rows[1]
        except (ValueError, IndexError):
            last_problem = f"Unexpected Census response for {year}."
            continue

        record = dict(zip(header, values))
        overall = int(record.get(ALL_UNITS_VAR) or -1)
        by_beds = int(record.get(BEDROOM_VARS[beds]) or -1)
        # Census uses large negative codes for "not enough data"
        result = {
            "zip_code": zip_code,
            "bedrooms": beds,
            "median_rent_for_bedrooms": by_beds if by_beds > 0 else None,
            "median_rent_all_units": overall if overall > 0 else None,
            "source": f"US Census Bureau, American Community Survey 5-year estimates ({year}), table B25031",
            "note": "Median gross rent includes utilities and lags the current market by a year or more. "
                    "Treat it as a conservative baseline and ask if the user has a local rent comp.",
        }
        if result["median_rent_for_bedrooms"] is None and result["median_rent_all_units"] is None:
            return _err(f"The Census has too few rental units in ZIP {zip_code} to report a median. "
                        "Try a neighboring ZIP code or ask the user for a local rent estimate.")
        if result["median_rent_for_bedrooms"] is None:
            result["note"] += (f" There was not enough data for {beds} bedroom units, so use the all units "
                               "median and adjust for size.")

        # Top of the local market: scale the bedroom median by this ZIP's 75th percentile / median ratio
        ratio = _census_upper_quartile_ratio(zip_code, year, key)
        base = result["median_rent_for_bedrooms"] or result["median_rent_all_units"]
        if ratio:
            result["upper_quartile_rent_for_bedrooms"] = round(base * ratio)
            result["upper_quartile_to_median_ratio"] = round(ratio, 3)
            result["upper_quartile_note"] = (
                "Estimated rent at the 75th percentile of this ZIP: the bedroom median scaled by the ZIP's "
                "upper quartile to median contract rent ratio (tables B25058 and B25059). Use it as the target "
                "for a renovated, higher end unit. Going above it needs local comps from the user.")
        else:
            result["upper_quartile_rent_for_bedrooms"] = None
            result["upper_quartile_note"] = ("The Census upper quartile was not available for this ZIP. For a "
                                             "luxury renovation, ask the user for rents of renovated comps nearby.")
        return json.dumps(result)

    return _err(f"{last_problem} ZIP {zip_code} may be a PO box or non-residential ZIP. "
                "Ask the user to confirm the ZIP code or provide a rent estimate.")


# --- Tool 2: original ---

# Cost per square foot (low, high) by visible condition. Rough investor rules of thumb that
# vary by market; they cover finishes (paint, flooring, fixtures, kitchen and bath refresh).
CONDITION_PER_SQFT = {
    "light": (8, 18),       # paint, flooring, minor fixes; looks livable from outside
    "medium": (20, 35),    # dated kitchen and baths, some damage, overgrown, neglected
    "heavy": (40, 65),     # boarded up, visible damage, long vacancy, likely gut of interiors
    "unknown": (15, 55),   # no photos and no drive by: deliberately wide
}

# Extra cost per square foot (low, high) to finish to a higher end standard on top of the repairs:
# quartz counters, better cabinets and appliances, tile, fixtures, lighting. Rough rule of thumb.
LUXURY_UPGRADE_PER_SQFT = (25, 60)
# Share of the luxury upgrade cost assumed to show up in the home's value (renovations rarely add their
# full cost). Rough rule of thumb used for the cost based luxury value estimate.
LUXURY_VALUE_RECOVERY = 0.70

# Major systems the tiers do NOT include. (low, high) dollars per item.
ISSUE_COSTS = {
    "roof": (8000, 15000),
    "hvac": (5000, 10000),
    "water_heater": (1200, 2500),
    "electrical": (5000, 15000),
    "plumbing": (4000, 12000),
    "sewer_line": (4000, 12000),
    "foundation": (8000, 30000),
    "windows": (5000, 15000),
    "mold": (2000, 10000),
    "termites": (1500, 8000),
}


def _age_factor(year_built: int) -> tuple[float, str | None]:
    if year_built < 1950:
        return 1.20, "Built before 1950: budget for outdated wiring, plumbing and possible lead paint."
    if year_built < 1978:
        return 1.10, "Built before 1978: lead paint is likely, which adds cost and rules for any work."
    return 1.0, None


def estimate_repairs(sqft: float, year_built: int, condition: str, known_issues: list | str | None = None,
                     finish_level: str = "standard") -> str:
    try:
        area = _num(sqft, "sqft", minimum=300, maximum=10000)
        year = int(_num(year_built, "year_built", minimum=1800, maximum=datetime.date.today().year))
    except ValueError as e:
        return _err(f"{e} The listing or the county assessor's website shows square footage and year built. "
                    "Ask the user for them.")

    cond = str(condition).strip().lower()
    if cond not in CONDITION_PER_SQFT:
        return _err(f"Unknown condition '{condition}'. Use one of: {', '.join(CONDITION_PER_SQFT)}. "
                    "Pick from what the user describes, or 'unknown' if they have not seen the property.")

    finish = str(finish_level or "standard").strip().lower()
    if finish not in ("standard", "luxury"):
        return _err(f"Unknown finish_level '{finish_level}'. Use 'standard' or 'luxury'.")

    if isinstance(known_issues, str):
        known_issues = [x for x in known_issues.replace(";", ",").split(",")]
    issues = [str(x).strip().lower().replace(" ", "_") for x in (known_issues or []) if str(x).strip()]
    recognized = [i for i in dict.fromkeys(issues) if i in ISSUE_COSTS]
    unrecognized = [i for i in dict.fromkeys(issues) if i not in ISSUE_COSTS]

    factor, age_note = _age_factor(year)
    lo_psf, hi_psf = CONDITION_PER_SQFT[cond]
    base_lo, base_hi = area * lo_psf * factor, area * hi_psf * factor

    line_items = {i: {"low": ISSUE_COSTS[i][0], "high": ISSUE_COSTS[i][1]} for i in recognized}
    items_lo = sum(v["low"] for v in line_items.values())
    items_hi = sum(v["high"] for v in line_items.values())

    # Both finish levels every time, so the report can compare standard and luxury side by side
    up_lo, up_hi = area * LUXURY_UPGRADE_PER_SQFT[0], area * LUXURY_UPGRADE_PER_SQFT[1]
    contingency = 0.10  # cushion for surprises behind the walls, applied to the high end only
    std_low = base_lo + items_lo
    std_high = (base_hi + items_hi) * (1 + contingency)
    lux_low = std_low + up_lo
    lux_high = std_high + up_hi * (1 + contingency)
    lux_mid = (up_lo + up_hi * (1 + contingency)) / 2  # extra cost of luxury finishes alone

    if finish == "luxury":
        low, high = lux_low, lux_high
        lux_lo, lux_hi = up_lo, up_hi
    else:
        low, high = std_low, std_high
        lux_lo, lux_hi = 0.0, 0.0
    mid = (low + high) / 2

    notes = []
    if age_note:
        notes.append(age_note)
    if cond == "unknown":
        notes.append("Condition unknown, so the range is wide. Photos or a drive by would narrow it a lot.")
    if unrecognized:
        notes.append(f"Not priced: {', '.join(unrecognized)}. Ask the user for a quote or add an allowance.")

    return json.dumps({
        "repair_estimate_low": round(low, -2),
        "repair_estimate_mid": round(mid, -2),
        "repair_estimate_high": round(high, -2),
        "breakdown": {
            "condition_tier": cond,
            "cost_per_sqft_range": [lo_psf, hi_psf],
            "age_multiplier": factor,
            "finishes_low_high": [round(base_lo), round(base_hi)],
            "major_systems": line_items,
            "contingency_pct_on_high": contingency,
            "finish_level": finish,
            "luxury_upgrade_low_high": [round(lux_lo), round(lux_hi)],
        },
        "luxury_upgrade_mid": round(lux_mid, -2),
        "both_finishes": {
            "standard": {"low": round(std_low, -2), "mid": round((std_low + std_high) / 2, -2),
                         "high": round(std_high, -2)},
            "luxury": {"low": round(lux_low, -2), "mid": round((lux_low + lux_high) / 2, -2),
                       "high": round(lux_high, -2)},
            "luxury_upgrade_mid": round(lux_mid, -2),
        },
        "notes": notes,
        "next_step": "Use both_finishes.standard.mid as repair_cost for the standard plan, and "
                     "both_finishes.luxury.mid as repair_cost plus both_finishes.luxury_upgrade_mid as "
                     "luxury_upgrade_cost for the luxury plan. The stress test already models overruns, "
                     "so do not pad it further.",
        "caveat": "Rough rules of thumb that vary by market. A contractor walkthrough beats any estimate.",
    })


# --- Tool 3: external data ---

FRED_API = ("https://api.stlouisfed.org/fred/series/observations?series_id={series}&api_key={key}"
            "&file_type=json&sort_order=desc&limit=10")
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
FRED_SERIES = {30: "MORTGAGE30US", 15: "MORTGAGE15US"}


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (rentcover class project)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _rate_from_api(series: str, key: str) -> tuple[float, str]:
    data = json.loads(_fetch(FRED_API.format(series=series, key=key)))
    for obs in data.get("observations", []):  # newest first
        if obs.get("value") not in (None, "", "."):
            return float(obs["value"]), obs["date"]
    raise ValueError("no observations in the FRED API response")


def _rate_from_csv(series: str) -> tuple[float, str]:
    lines = _fetch(FRED_CSV.format(series=series)).strip().splitlines()
    for line in reversed(lines[1:]):  # rows look like "2026-10-01,6.12"; missing weeks are "."
        parts = line.split(",")
        if len(parts) == 2:
            try:
                return float(parts[1]), parts[0]
            except ValueError:
                continue
    raise ValueError("no usable rows in the FRED CSV")


def _describe(e: Exception) -> str:
    text = f"{type(e).__name__}: {getattr(e, 'reason', e)}"
    if "CERTIFICATE_VERIFY_FAILED" in text:
        text += (" (Python cannot verify SSL certificates on this machine. On a Mac, run "
                 "'Install Certificates.command' in your Python folder.)")
    return text


def get_mortgage_rate(loan_term_years: int = 30) -> str:
    try:
        term = int(loan_term_years)
    except (TypeError, ValueError):
        term = 30
    if term not in FRED_SERIES:
        return _err(f"Rates are only available for 15 or 30 year loans, not {loan_term_years}. "
                    "Use 30 unless the user asked for 15, or ask the user for their quoted rate.")
    series = FRED_SERIES[term]

    # Official API first (needs a free FRED_API_KEY), then the keyless CSV download as a backup
    attempts = []
    key = os.environ.get("FRED_API_KEY")
    sources = ([("FRED API", lambda: _rate_from_api(series, key))] if key else []) + \
              [("FRED CSV download", lambda: _rate_from_csv(series))]
    if not key:
        attempts.append("FRED API: skipped, FRED_API_KEY is not set")
    for name, fetch in sources:
        try:
            rate, date = fetch()
        except Exception as e:  # network, HTTP, SSL or parsing problems: try the next source
            attempts.append(f"{name}: {_describe(e)}")
            continue
        return json.dumps({
            "loan_term_years": term,
            "average_rate_pct": rate,
            "as_of_week": date,
            "source": f"Freddie Mac Primary Mortgage Market Survey via {name} ({series})",
            "note": "This is the national average for owner occupied homes. Loans on rental properties "
                    "usually cost more, often around half a point to a point higher. Ask the user if they "
                    "have a lender quote; otherwise say which rate you used.",
        })

    return _err("Could not get a mortgage rate from FRED. Ask the user for the rate their lender quoted "
                "and continue with it. Details for the developer: " + " | ".join(attempts))


# --- Shared loan math ---


def _monthly_payment(principal: float, annual_rate_pct: float, years: float) -> float:
    """Standard fixed rate mortgage payment (principal and interest)."""
    n = years * 12
    r = annual_rate_pct / 100 / 12
    if principal <= 0:
        return 0.0
    if r == 0:
        return principal / n
    return principal * r / (1 - (1 + r) ** -n)


def _loan_inputs(args: dict) -> tuple[float, float, float, float, float, dict]:
    if args.get("interest_rate_pct") is None and args.get("refinance_rate_pct") is not None:
        args = dict(args, interest_rate_pct=args["refinance_rate_pct"])
    if args.get("interest_rate_pct") is None:
        raise ValueError("Missing 'interest_rate_pct'. Use the rate the user's lender quoted, or call "
                         "get_mortgage_rate first.")
    if args.get("monthly_rent") is None:
        raise ValueError("Missing 'monthly_rent'. Use the user's rent estimate, or call lookup_market_rent first.")
    price = _num(args.get("purchase_price"), "purchase_price", minimum=1000)
    rent = _num(args.get("monthly_rent"), "monthly_rent", minimum=1)
    rate = _num(args.get("interest_rate_pct"), "interest_rate_pct", minimum=0, maximum=25)
    if rate < 1 and rate > 0:
        raise ValueError(f"'interest_rate_pct' is a percent, so 6.5% is 6.5, not 0.065. Got {rate}.")
    down = _num(args.get("down_payment_pct", 0.25), "down_payment_pct", minimum=0, maximum=1)
    term = _num(args.get("loan_term_years", 30), "loan_term_years", minimum=5, maximum=40)
    repairs = _num(args.get("repair_cost", 0), "repair_cost", minimum=0)
    a = _assumptions(args)
    a.update(down_payment_pct=down, loan_term_years=term, interest_rate_pct=rate)
    return price, rent, repairs, rate, term, a


def _monthly_costs(rent: float, a: dict) -> dict:
    return {
        "vacancy": rent * a["vacancy_rate"],
        "management": rent * a["management_rate"],
        "maintenance_reserve": rent * a["maintenance_rate"],
        "property_taxes": a["property_taxes_annual"] / 12,
        "insurance": a["insurance_annual"] / 12,
        "hoa_or_condo_fee": a["hoa_monthly"],
    }


def _timeline(price: float, loan: float, payment: float, rate: float, term: float, rent: float,
              a: dict, cash_upfront: float, horizon_years: int = 40) -> dict:
    """Month by month: when the loan is paid off and when the investment pays back.

    Rent starts after the holding months (those carrying costs are already in cash_upfront). Rent grows
    with rent_growth_rate; taxes, insurance and HOA grow with expense_growth_rate; the mortgage is fixed.
    """
    r = rate / 100 / 12
    n_term = int(round(term * 12))
    start = int(round(a["holding_months"]))
    fixed0 = (a["property_taxes_annual"] + a["insurance_annual"]) / 12 + a["hoa_monthly"]
    variable_share = a["vacancy_rate"] + a["management_rate"] + a["maintenance_rate"]

    def year_numbers(y: int) -> tuple[float, float]:  # monthly rent and monthly cash flow in year y (0 based)
        rent_y = rent * (1 + a["rent_growth_rate"]) ** y
        fixed_y = fixed0 * (1 + a["expense_growth_rate"]) ** y
        return rent_y, rent_y * (1 - variable_share) - fixed_y

    balance = balance_fast = loan
    cum_cash, principal_paid = -cash_upfront, 0.0
    first_positive = cash_back = profit_with_equity = payoff_fast = None
    snapshots = {}
    for m in range(1, horizon_years * 12 + 1):
        y = (m - 1) // 12
        _, before_loan = year_numbers(y)
        pay = payment if m <= n_term else 0.0
        cf = (before_loan - pay) if m > start else 0.0
        cum_cash += cf

        if balance > 0:  # scheduled amortization
            interest = balance * r
            principal = min(balance, pay - interest)
            balance -= principal
            principal_paid += principal
        if balance_fast > 0:  # same loan, but every dollar of positive cash flow goes to extra principal
            interest = balance_fast * r
            extra = max(cf, 0.0)
            balance_fast -= min(balance_fast, payment - interest + extra)
            if balance_fast <= 0.5 and payoff_fast is None:
                payoff_fast = m

        if m % 12 == 0:
            year = m // 12
            gain = price * ((1 + a["appreciation_rate"]) ** year - 1)
            if first_positive is None and year_numbers(y)[1] - pay > 0:
                first_positive = year
            if cash_back is None and cum_cash >= 0:
                cash_back = year
            if profit_with_equity is None and cum_cash + principal_paid + gain >= 0:
                profit_with_equity = year
            if year in (5, 10, 20, 30):
                snapshots[f"year_{year}"] = {
                    "cumulative_cash_flow_net_of_upfront": round(cum_cash),
                    "loan_principal_paid_off": round(principal_paid),
                    "appreciation_gain": round(gain),
                }

    return {
        "loan_paid_off_years": term if loan > 0 else 0,
        "loan_paid_off_years_if_extra_cash_goes_to_loan": round(payoff_fast / 12, 1) if payoff_fast else None,
        "first_year_with_positive_cash_flow": first_positive,
        "years_until_cash_back": cash_back,
        "years_until_profit_including_equity": profit_with_equity,
        "snapshots": snapshots,
        "growth_assumptions": {
            "rent_growth_rate": a["rent_growth_rate"],
            "expense_growth_rate": a["expense_growth_rate"],
            "appreciation_rate": a["appreciation_rate"],
        },
        "meaning": (
            "years_until_cash_back: years until rent, after all costs and the mortgage, has repaid every dollar "
            "put in upfront. years_until_profit_including_equity: same, but also counting loan principal paid "
            "down and any appreciation, which you only collect when you sell or refinance (before selling "
            "costs). None means not within 40 years. Rent grows each year while the mortgage stays fixed, so "
            "a thin or negative cash flow can improve over time."),
    }


def _luxury_premium(args: dict, rent: float, payment: float, a: dict) -> dict | None:
    """Is the higher rent worth the extra renovation cost? Only when the model passes a median to compare to."""
    try:
        median = args.get("market_median_rent")
        if median is None:
            return None
        median = _num(median, "market_median_rent", minimum=1)
        upgrade = _num(args.get("luxury_upgrade_cost", 0), "luxury_upgrade_cost", minimum=0)
    except ValueError as e:
        return {"error": str(e)}
    keep = 1 - a["vacancy_rate"] - a["management_rate"] - a["maintenance_rate"]  # share of rent you keep
    premium = rent - median
    net_premium = premium * keep
    at_median = median - sum(_monthly_costs(median, a).values()) - payment
    return {
        "rent_used": round(rent),
        "local_median_rent": round(median),
        "premium_over_median_monthly": round(premium),
        "premium_after_vacancy_and_upkeep_monthly": round(net_premium),
        "luxury_upgrade_cost": round(upgrade),
        "payback_years": round(upgrade / (net_premium * 12), 1) if upgrade > 0 and net_premium > 0 else None,
        "monthly_cash_flow_if_rent_stays_at_median": round(at_median),
        "meaning": "Payback years = extra renovation cost / extra rent you keep each year. If rent only reaches "
                   "the median, the cash flow falls to the 'if rent stays at median' figure.",
    }


# --- Tool 4: original ---


def calculate_cash_flow(**args) -> str:
    try:
        price, rent, repairs, rate, term, a = _loan_inputs(args)
    except ValueError as e:
        return _err(str(e))

    loan = price * (1 - a["down_payment_pct"])
    payment = _monthly_payment(loan, rate, term)
    costs = _monthly_costs(rent, a)
    total_costs = sum(costs.values())
    noi_monthly = rent - total_costs
    cash_flow = noi_monthly - payment

    # Rent at which cash flow is exactly zero: rent * (1 - variable share) = fixed costs + payment
    variable_share = a["vacancy_rate"] + a["management_rate"] + a["maintenance_rate"]
    fixed = costs["property_taxes"] + costs["insurance"] + costs["hoa_or_condo_fee"] + payment
    break_even_rent = fixed / (1 - variable_share) if variable_share < 1 else None

    # Highest price where rent still covers the loan (payment is linear in the loan amount)
    pay_per_dollar = _monthly_payment(1.0, rate, term)
    def max_price(dscr: float) -> float | None:
        if noi_monthly <= 0 or a["down_payment_pct"] >= 1:
            return None
        return round(noi_monthly / (dscr * (1 - a["down_payment_pct"]) * pay_per_dollar), -2)

    down_payment = price * a["down_payment_pct"]
    premium = price * a["buyer_premium_pct"]
    closing = price * a["closing_cost_pct"]
    holding = a["holding_months"] * (payment + costs["property_taxes"] + costs["insurance"]
                                     + costs["hoa_or_condo_fee"] + a["holding_utilities_monthly"])
    cash_upfront = down_payment + premium + closing + repairs + holding

    dscr = noi_monthly / payment if payment > 0 else None
    if cash_flow >= 0:
        verdict = f"Rent covers the loan with about ${cash_flow:,.0f} a month left over."
    else:
        verdict = f"Rent does not cover the loan. You would pay about ${-cash_flow:,.0f} a month out of pocket."

    return json.dumps({
        "verdict": verdict,
        "monthly": {
            "rent": round(rent),
            "operating_costs": {k: round(v) for k, v in costs.items()},
            "operating_costs_total": round(total_costs),
            "net_operating_income": round(noi_monthly),
            "mortgage_payment": round(payment),
            "cash_flow": round(cash_flow),
        },
        "annual_cash_flow": round(cash_flow * 12),
        "loan": {"amount": round(loan), "rate_pct": rate, "term_years": term},
        "debt_service_coverage_ratio": round(dscr, 2) if dscr is not None else None,
        "dscr_meaning": "Net operating income divided by the mortgage payment. Below 1.0 the rent does not "
                        "cover the loan; many rental lenders look for 1.2 to 1.25 or higher.",
        "break_even_rent": round(break_even_rent) if break_even_rent else None,
        "max_price_rent_covers_loan": max_price(1.0),
        "max_price_with_1_25_cushion": max_price(1.25),
        "cash_needed_upfront": {
            "down_payment": round(down_payment),
            "buyer_premium": round(premium),
            "closing_costs": round(closing),
            "repairs": round(repairs),
            "holding_costs_until_rented": round(holding),
            "total": round(cash_upfront),
        },
        "cash_on_cash_return": round(cash_flow * 12 / cash_upfront, 4) if cash_upfront > 0 else None,
        "luxury_premium": _luxury_premium(args, rent, payment, a),
        "timeline": _timeline(price, loan, payment, rate, term, rent, a, cash_upfront),
        "assumptions_used": a,
        "next_step": "Run stress_test_cash_flow with the same inputs to see how likely the rent is to fall short.",
    })


# --- Tool 5: original ---


def stress_test_cash_flow(**args) -> str:
    try:
        price, rent, repairs, rate, term, a = _loan_inputs(args)
    except ValueError as e:
        return _err(str(e))

    loan_override = args.get("loan_amount")
    try:
        loan = _num(loan_override, "loan_amount", minimum=0) if loan_override is not None \
            else price * (1 - a["down_payment_pct"])
    except ValueError as e:
        return _err(str(e))
    payment = _monthly_payment(loan, rate, term)
    pay_per_dollar = _monthly_payment(1.0, rate, term)
    # Peak cash before repairs. With a mortgage at purchase: down payment and fees. For cash then refinance
    # (loan_amount given): the whole purchase, since the loan only arrives after the renovation.
    paid_at_purchase = price * (1 + a["buyer_premium_pct"] + a["closing_cost_pct"])
    fixed_upfront = paid_at_purchase if loan_override is not None else max(0.0, paid_at_purchase - loan)

    def monthly_cf(rent_m=1.0, vacancy=None, fixed_m=1.0, maint=None, pay=payment) -> tuple[float, float]:
        sa = dict(a)
        if vacancy is not None:
            sa["vacancy_rate"] = vacancy
        if maint is not None:
            sa["maintenance_rate"] = maint
        sa["property_taxes_annual"] *= fixed_m
        sa["insurance_annual"] *= fixed_m
        r = rent * rent_m
        noi = r - sum(_monthly_costs(r, sa).values())
        return noi, noi - pay

    # Named scenarios a landlord worries about
    named = {
        "base case": monthly_cf(),
        "rent 15% lower": monthly_cf(rent_m=0.85),
        "vacant 20% of the year": monthly_cf(vacancy=0.20),
        "taxes and insurance up 30%": monthly_cf(fixed_m=1.3),
        "maintenance doubles": monthly_cf(maint=a["maintenance_rate"] * 2),
        "rate 1 point higher if not locked yet": monthly_cf(pay=_monthly_payment(loan, rate + 1, term)),
    }
    if args.get("market_median_rent"):
        try:
            median_m = _num(args["market_median_rent"], "market_median_rent", minimum=1) / rent
            named["luxury premium not achieved (rent at median)"] = monthly_cf(rent_m=median_m)
        except ValueError:
            pass
    scenarios = {k: {"monthly_cash_flow": round(v[1])} for k, v in named.items()}
    carrying = (payment + (a["property_taxes_annual"] + a["insurance_annual"]) / 12 + a["hoa_monthly"]
                + a["holding_utilities_monthly"])
    scenarios["9 months to get it rented (eviction or rehab)"] = {
        "first_year_cash_flow": round(3 * named["base case"][1] - 9 * carrying),
    }

    # Monte Carlo, seeded so results are repeatable
    rng = random.Random(42)
    n = 5000
    monthly, year_one, upfront, safe_prices = [], [], [], []
    for _ in range(n):
        noi, cf = monthly_cf(
            rent_m=max(0.5, rng.gauss(0.98, 0.07)),
            vacancy=rng.triangular(0.04, 0.18, min(max(a["vacancy_rate"], 0.04), 0.18)),
            fixed_m=rng.uniform(1.0, 1.2),
            maint=rng.triangular(0.06, 0.16, min(max(a["maintenance_rate"], 0.06), 0.16)),
        )
        months_empty = rng.triangular(2, 10, a["holding_months"])
        monthly.append(cf)
        year_one.append(cf * max(0, 12 - months_empty) - carrying * min(12, months_empty))
        upfront.append(fixed_upfront + repairs * rng.triangular(0.8, 2.2, 1.0))
        safe_prices.append(noi / ((1 - a["down_payment_pct"]) * pay_per_dollar) if a["down_payment_pct"] < 1 else 0)

    for xs in (monthly, year_one, upfront, safe_prices):
        xs.sort()
    pct = lambda xs, q: xs[int(q * (len(xs) - 1))]
    reserve = pct(upfront, 0.9) + max(0, -pct(year_one, 0.1))
    safe_price = pct(safe_prices, 0.25)

    return json.dumps({
        "purchase_price": price,
        "monthly_mortgage_payment": round(payment),
        "scenarios": scenarios,
        "simulation": {
            "runs": n,
            "prob_rent_does_not_cover_loan": round(sum(x < 0 for x in monthly) / n, 3),
            "monthly_cash_flow_p10": round(pct(monthly, 0.10)),
            "monthly_cash_flow_median": round(statistics.median(monthly)),
            "monthly_cash_flow_p90": round(pct(monthly, 0.90)),
            "first_year_cash_flow_p10": round(pct(year_one, 0.10)),
            "total_cash_needed_bad_case": round(reserve, -2),
            "max_price_covers_loan_75pct_of_time": (round(safe_price, -2) if safe_price > 0 else None)
                                                    if loan_override is None else None,
            "method": "Rent ~ Normal(98%, 7%) of estimate; vacancy ~ Triangular(4%, base, 18%); taxes and "
                      "insurance rise 0 to 20%; maintenance ~ Triangular(6%, base, 16%) of rent; months until rented ~ Triangular(2, base, 10); "
                      "repairs ~ Triangular(80%, 100%, 220%) of estimate. Total cash needed in a bad case = 90th "
                      "percentile upfront cash (down payment, fees, repairs) plus a 10th percentile first year shortfall.",
        },
    })


# --- Tool 6: original ---


def analyze_cash_then_refinance(**args) -> str:
    """Buy with cash, renovate, rent, then refinance against the after repair value."""
    try:
        price = _num(args.get("purchase_price"), "purchase_price", minimum=1000)
        if args.get("monthly_rent") is None:
            raise ValueError("Missing 'monthly_rent'. Use the user's rent, or call lookup_market_rent first.")
        rent = _num(args.get("monthly_rent"), "monthly_rent", minimum=1)
        if args.get("after_repair_value") is None:
            raise ValueError("Missing 'after_repair_value'. Use the user's estimate or renovated comps, or the "
                             "listing's estimated market value labeled as such. If none exists, ask the user.")
        arv = _num(args.get("after_repair_value"), "after_repair_value", minimum=1000)
        if args.get("refinance_rate_pct") is None:
            raise ValueError("Missing 'refinance_rate_pct'. Use the user's lender quote, or call get_mortgage_rate first.")
        rate = _num(args.get("refinance_rate_pct"), "refinance_rate_pct", minimum=0.5, maximum=25)
        repairs = _num(args.get("repair_cost", 0), "repair_cost", minimum=0)
        term = _num(args.get("loan_term_years", 30), "loan_term_years", minimum=5, maximum=40)
        ltv = _num(args.get("refinance_ltv", 0.75), "refinance_ltv", minimum=0.1, maximum=0.9)
        min_dscr = _num(args.get("min_lender_dscr", 1.0), "min_lender_dscr", minimum=0.5, maximum=2)
        refi_closing_pct = _num(args.get("refinance_closing_pct", 0.02), "refinance_closing_pct", minimum=0, maximum=0.1)
        refi_month = _num(args.get("months_until_refinance", 6), "months_until_refinance", minimum=1, maximum=36)
        a = _assumptions(args)
        median = args.get("market_median_rent")
        upgrade = _num(args.get("luxury_upgrade_cost", 0), "luxury_upgrade_cost", minimum=0)
        user_lux_value = args.get("luxury_after_repair_value")
        if user_lux_value is not None:
            user_lux_value = _num(user_lux_value, "luxury_after_repair_value", minimum=1000)
    except ValueError as e:
        return _err(str(e))

    # Luxury plan: the finished house should be worth more than the standard renovation.
    standard_arv = arv
    value_method = "as given"
    if user_lux_value is not None:
        arv, value_method = user_lux_value, "your luxury value"
    elif upgrade > 0 and median and float(median) > 0 and rent > float(median):
        income_based = standard_arv * rent / float(median)            # value moves with rent
        cost_based = standard_arv + LUXURY_VALUE_RECOVERY * upgrade   # finishes rarely add their full cost
        arv = max(standard_arv, min(income_based, cost_based))
        value_method = ("lower of rent based (standard value x luxury rent / median rent) and cost based "
                        f"(standard value + {LUXURY_VALUE_RECOVERY:.0%} of the luxury upgrade) estimates")

    costs = _monthly_costs(rent, a)
    fixed_monthly = costs["property_taxes"] + costs["insurance"] + costs["hoa_or_condo_fee"]
    noi_monthly = rent - sum(costs.values())

    # Phase 1: all cash. No mortgage while renovating, only taxes, insurance, HOA and utilities.
    premium = price * a["buyer_premium_pct"]
    closing = price * a["closing_cost_pct"]
    rehab_months = a["holding_months"]
    holding = rehab_months * (fixed_monthly + a["holding_utilities_monthly"])
    cash_in = price + premium + closing + repairs + holding
    rented_before_refi = max(0.0, refi_month - rehab_months)
    rent_before_refi = rented_before_refi * noi_monthly  # rented with no mortgage until the refinance

    # Phase 2: the refinance. A lender caps the loan three ways; the smallest wins.
    pay_per_dollar = _monthly_payment(1.0, rate, term)
    limits = {"loan_to_value": ltv * arv}
    # Rental (DSCR) lenders usually test gross rent / (mortgage + taxes + insurance + HOA)
    room = rent / min_dscr - fixed_monthly
    limits["lender_rent_coverage"] = max(0.0, room / pay_per_dollar)
    if refi_month < 6:  # before about 6 months, many lenders lend against what you paid, not the new value
        limits["seasoning_under_6_months"] = ltv * (price + premium + closing)
    binding = min(limits, key=limits.get)
    loan = max(0.0, limits[binding])

    refi_costs = loan * refi_closing_pct
    cash_back = loan - refi_costs
    cash_left = cash_in - rent_before_refi - cash_back

    payment = _monthly_payment(loan, rate, term)
    cash_flow = noi_monthly - payment
    lender_dscr = rent / (payment + fixed_monthly) if payment + fixed_monthly > 0 else None
    variable_share = a["vacancy_rate"] + a["management_rate"] + a["maintenance_rate"]
    break_even_rent = (fixed_monthly + payment) / (1 - variable_share) if variable_share < 1 else None

    # Timeline from the refinance date: rent is already coming in, cash still in the deal is the "upfront"
    a_after = dict(a, holding_months=0)
    timeline = _timeline(arv, loan, payment, rate, term, rent, a_after, cash_left)
    if cash_left <= 0:
        timeline["years_until_cash_back"] = 0
        timeline["years_until_profit_including_equity"] = 0

    if cash_flow >= 0:
        verdict = f"After the refinance, rent covers the loan with about ${cash_flow:,.0f} a month left over."
    else:
        verdict = f"After the refinance, rent does not cover the loan: about ${-cash_flow:,.0f} a month short."
    verdict += (f" You get back about ${cash_back:,.0f} of the ${cash_in:,.0f} you put in, leaving "
                f"${max(cash_left, 0):,.0f} in the deal." if cash_left > 0 else
                f" The refinance returns all ${cash_in:,.0f} you put in, plus about ${-cash_left:,.0f} extra.")

    return json.dumps({
        "verdict": verdict,
        "phase_1_cash_purchase": {
            "purchase_price": round(price),
            "buyer_premium": round(premium),
            "closing_costs": round(closing),
            "repairs": round(repairs),
            "holding_costs_while_renovating": round(holding),
            "total_cash_in": round(cash_in),
            "net_rent_collected_before_refinance": round(rent_before_refi),
        },
        "phase_2_refinance": {
            "after_repair_value": round(arv),
            "standard_after_repair_value": round(standard_arv),
            "after_repair_value_method": value_method,
            "loan_amount": round(loan),
            "what_limited_the_loan": binding,
            "loan_limits": {k: round(v) for k, v in limits.items()},
            "refinance_closing_costs": round(refi_costs),
            "cash_back_at_refinance": round(cash_back),
            "rate_pct": rate,
            "term_years": term,
            "months_after_purchase": refi_month,
        },
        "cash_left_in_deal": round(cash_left),
        "monthly_before_refinance": {
            "cash_flow": round(noi_monthly),
            "note": f"Once rented, before the refinance: rent minus costs, with no mortgage because the house "
                    f"was bought with cash. The mortgage starts at the refinance, month {refi_month:g}.",
        },
        "monthly_after_refinance": {
            "rent": round(rent),
            "operating_costs": {k: round(v) for k, v in costs.items()},
            "net_operating_income": round(noi_monthly),
            "mortgage_payment": round(payment),
            "cash_flow": round(cash_flow),
        },
        "annual_cash_flow": round(cash_flow * 12),
        "lender_dscr": round(lender_dscr, 2) if lender_dscr else None,
        "lender_dscr_meaning": f"Gross rent / (mortgage + taxes + insurance + HOA), the test rental lenders use. "
                               f"This plan assumes the lender needs at least {min_dscr}.",
        "break_even_rent": round(break_even_rent) if break_even_rent else None,
        "cash_on_cash_return": round(cash_flow * 12 / cash_left, 4) if cash_left > 0 else None,
        "luxury_premium": _luxury_premium(args, rent, payment, a),
        "timeline_from_refinance": timeline,
        "assumptions_used": dict(a, refinance_ltv=ltv, min_lender_dscr=min_dscr,
                                 refinance_closing_pct=refi_closing_pct),
        "caveats": "The loan depends on the appraisal matching the after repair value and on lender rules, "
                   "which vary. Seasoning rules differ by lender and program.",
        "next_step": "Run stress_test_cash_flow with loan_amount set to the refinance loan and interest_rate_pct "
                     "set to the refinance rate to see how likely the rent is to fall short after the refinance.",
    })


# --- Schemas the model sees ---

_LOAN_PROPS = {
    "purchase_price": {"type": "number", "description": "Purchase price or auction bid in dollars."},
    "monthly_rent": {"type": "number", "description": "Expected monthly rent in dollars, from the user or lookup_market_rent."},
    "interest_rate_pct": {"type": "number", "description": "Annual mortgage rate as a percent, e.g. 7.1. Use the user's lender quote, else get_mortgage_rate."},
    "down_payment_pct": {"type": "number", "description": "Down payment as a decimal share of the price. Default 0.25, typical for rental property loans. Use 1.0 for an all cash purchase."},
    "loan_term_years": {"type": "number", "description": "Loan term in years. Default 30."},
    "repair_cost": {"type": "number", "description": "Repair cost in dollars, from the user or repair_estimate_mid from estimate_repairs. Default 0."},
    "vacancy_rate": {"type": "number", "description": "Share of the year vacant as a decimal. Default 0.08."},
    "property_taxes_annual": {"type": "number", "description": "Annual property tax in dollars. Default 2000. Use the listing's figure if given."},
    "insurance_annual": {"type": "number", "description": "Annual landlord insurance in dollars. Default 1500."},
    "buyer_premium_pct": {"type": "number", "description": "Auction buyer's premium as a decimal share of the price. Default 0.05; use 0 for a regular sale."},
    "holding_months": {"type": "number", "description": "Months from purchase until rented, including any eviction and repairs. Default 4."},
    "hoa_monthly": {"type": "number", "description": "Monthly HOA or condo fee in dollars. Default 0. Condos and townhomes almost always have one."},
    "rent_growth_rate": {"type": "number", "description": "Yearly rent increase as a decimal, for the timeline. Default 0.025."},
    "expense_growth_rate": {"type": "number", "description": "Yearly increase in taxes, insurance and HOA as a decimal, for the timeline. Default 0.025."},
    "appreciation_rate": {"type": "number", "description": "Yearly home value growth as a decimal, for the timeline. Default 0 to stay conservative; use the user's figure if they give one."},
    "market_median_rent": {"type": "number", "description": "Local median rent from lookup_market_rent. Pass it when the rent used is above the median (for example a luxury renovation) to see the premium, its payback and a rent at median scenario."},
    "luxury_upgrade_cost": {"type": "number", "description": "Extra cost of finishing to a luxury standard: luxury_upgrade_mid from estimate_repairs, or the user's figure."},
}
_LOAN_REQUIRED = ["purchase_price", "monthly_rent", "interest_rate_pct"]


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_market_rent",
            "description": "Look up the median monthly rent for a US ZIP code and bedroom count from US Census "
                           "data, plus an estimate of the 75th percentile rent (the top quarter of the local "
                           "market) for renovated or higher end units.",
            "parameters": {
                "type": "object",
                "properties": {
                    "zip_code": {"type": "string", "description": "5 digit US ZIP code of the property, e.g. '46201'."},
                    "bedrooms": {"type": "integer", "description": "Number of bedrooms, 0 for a studio. 5 means 5 or more."},
                },
                "required": ["zip_code", "bedrooms"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "estimate_repairs",
            "description": "Estimate a low, mid and high repair cost for a property the buyer cannot inspect, "
                           "from square footage, year built, visible condition and any major problems they "
                           "noticed. Call this whenever the user has not given a dollar repair estimate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sqft": {"type": "number", "description": "Finished square footage, from the listing or county assessor."},
                    "year_built": {"type": "integer", "description": "Year the house was built, e.g. 1955."},
                    "condition": {"type": "string", "enum": list(CONDITION_PER_SQFT),
                                  "description": "light = livable, needs paint and flooring; medium = dated, neglected or some damage; "
                                                 "heavy = boarded up, major damage or long vacant; unknown = user has not seen it."},
                    "known_issues": {"type": "array", "items": {"type": "string", "enum": list(ISSUE_COSTS)},
                                     "description": "Major systems the user knows or suspects need replacing. Leave empty if none mentioned."},
                    "finish_level": {"type": "string", "enum": ["standard", "luxury"],
                                     "description": "luxury if the user plans a high end renovation to charge above median rent; otherwise standard."},
                },
                "required": ["sqft", "year_built", "condition"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_mortgage_rate",
            "description": "Get this week's national average mortgage rate from Freddie Mac via FRED. Call this "
                           "only when the user has not given the rate their lender quoted.",
            "parameters": {
                "type": "object",
                "properties": {
                    "loan_term_years": {"type": "integer", "description": "15 or 30. Default 30."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_cash_flow",
            "description": "Check whether the rent covers the mortgage: monthly payment, operating costs, cash "
                           "left over each month, debt service coverage ratio, break even rent, the highest price "
                           "where rent still covers the loan, and cash needed upfront. Call this before giving "
                           "any opinion on whether a property pays for itself.",
            "parameters": {"type": "object", "properties": _LOAN_PROPS, "required": _LOAN_REQUIRED},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stress_test_cash_flow",
            "description": "Stress test the monthly cash flow against lower rent, vacancy, rising taxes and "
                           "insurance, maintenance spikes, a higher rate and a slow start. Returns named scenarios "
                           "plus a 5,000 run simulation: the probability rent fails to cover the loan, the total cash "
                           "needed in a bad case, and the highest price that covers the loan 75% of the time. Call it "
                           "right after calculate_cash_flow with the same inputs.",
            "parameters": {"type": "object", "required": _LOAN_REQUIRED, "properties": {
                **_LOAN_PROPS,
                "loan_amount": {"type": "number", "description": "Only for the cash then refinance plan: the refinance loan amount from analyze_cash_then_refinance. Overrides the down payment."},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_cash_then_refinance",
            "description": "For buying with cash, renovating, renting, then refinancing. Shows the cash put in, "
                           "the refinance loan a lender would likely give (limited by loan to value, the lender's "
                           "rent coverage test, and seasoning), the cash returned at the refinance, the cash "
                           "left in the deal, whether rent covers the new loan, and a timeline. Use instead of "
                           "calculate_cash_flow when the financing plan is cash then refinance.",
            "parameters": {
                "type": "object",
                "properties": {
                    "purchase_price": _LOAN_PROPS["purchase_price"],
                    "monthly_rent": _LOAN_PROPS["monthly_rent"],
                    "after_repair_value": {"type": "number", "description": "Value after a STANDARD renovation in dollars: the user's figure or renovated comps; else the listing's estimated market value, labeled as such. Pass the same value for both plans; for the luxury plan the tool raises it automatically."},
                    "luxury_after_repair_value": {"type": "number", "description": "Only if the user gives a value for the house after a luxury renovation (for example from luxury comps). Overrides the tool's luxury estimate."},
                    "refinance_rate_pct": {"type": "number", "description": "Refinance rate as a percent, e.g. 7.8. The user's quote, else get_mortgage_rate plus 0.75."},
                    "repair_cost": _LOAN_PROPS["repair_cost"],
                    "market_median_rent": _LOAN_PROPS["market_median_rent"],
                    "luxury_upgrade_cost": _LOAN_PROPS["luxury_upgrade_cost"],
                    "months_until_refinance": {"type": "number", "description": "Months from purchase to the refinance. Default 6, a common seasoning period."},
                    "refinance_ltv": {"type": "number", "description": "Share of the after repair value a lender will lend, as a decimal. Default 0.75."},
                    "min_lender_dscr": {"type": "number", "description": "Minimum gross rent / (mortgage + taxes + insurance + HOA) the lender requires. Default 1.0; some require 1.2 or more."},
                    "loan_term_years": _LOAN_PROPS["loan_term_years"],
                    **{k: _LOAN_PROPS[k] for k in ["property_taxes_annual", "insurance_annual", "hoa_monthly",
                                                    "buyer_premium_pct", "holding_months", "vacancy_rate",
                                                    "rent_growth_rate", "expense_growth_rate", "appreciation_rate"]},
                },
                "required": ["purchase_price", "monthly_rent", "after_repair_value", "refinance_rate_pct"],
            },
        },
    },
]

_REGISTRY = {
    "lookup_market_rent": lookup_market_rent,
    "estimate_repairs": estimate_repairs,
    "get_mortgage_rate": get_mortgage_rate,
    "calculate_cash_flow": calculate_cash_flow,
    "stress_test_cash_flow": stress_test_cash_flow,
    "analyze_cash_then_refinance": analyze_cash_then_refinance,
}


def run_tool(name: str, args: dict) -> str:
    fn = _REGISTRY.get(name)
    if fn is None:
        return _err(f"Unknown tool '{name}'. Available tools: {', '.join(_REGISTRY)}.")
    try:
        return fn(**args)
    except TypeError as e:
        return _err(f"Bad arguments for {name}: {e}. Check the required parameters and try again.")
    except Exception as e:  # never crash the chat loop
        return _err(f"{name} failed unexpectedly: {type(e).__name__}: {e}")
