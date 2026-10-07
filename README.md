# RentCover: Will the Rent Cover the Loan?

RentCover helps people buying a rental property with a mortgage answer one question: will the rent pay the loan and the costs of owning the property, and how much is left over each month? It also handles auction properties (Auction.com, sheriff sales, HUD homes), where buyers usually can't inspect the house and pay a buyer's premium on top of the price.

The agent looks up local rent and this week's mortgage rate, estimates repairs from what the buyer can see from outside, and lines up rent against the mortgage payment, taxes, insurance, vacancy, management and maintenance. It then stress tests the result with a 5,000 run simulation to show how likely the rent is to fall short, how much total cash the buyer would need if things go wrong, and the highest price at which the rent still covers the loan. The answer comes back as a one page property report.

Every report compares two renovation plans side by side: **Standard** (standard repairs at the median rent for the ZIP) and **Luxury** (luxury finishes at top quarter rent), including how long the higher rent takes to repay the luxury upgrade. The Luxury column is skipped when the top quarter rent or the square footage is unknown.

A switch at the top of the chat sets the financing and is sent with every message. **Cash → refinance** (the default) models the common auction strategy: buy with cash, renovate, rent it out, then refinance against the after repair value. The agent shows how much cash goes in, how big a loan a lender would likely give, how much cash comes back at the refinance, how much stays in the deal, and whether the rent covers the new loan. **Mortgage** models a normal purchase with a down payment.

## Tools

| Tool | Type | What it does |
|---|---|---|
| `lookup_market_rent` | External data | Median gross rent by ZIP code and bedroom count from the US Census Bureau American Community Survey (table B25031), plus an estimated 75th percentile rent for renovated or higher end units: the bedroom median scaled by the ZIP's upper quartile to median contract rent ratio (tables B25058 and B25059). |
| `get_mortgage_rate` | External data | This week's national average 15 or 30 year mortgage rate from the Freddie Mac Primary Mortgage Market Survey, via FRED. The agent adds 0.75 points because rental property loans usually cost more. |
| `estimate_repairs` | Original | Turns square footage, year built, visible condition (light, medium, heavy, unknown) and suspected problems (roof, HVAC, foundation and so on) into a low, mid and high repair range, with an age multiplier for older homes and a 10% contingency on the high end. It always returns both a standard and a luxury estimate (luxury adds the cost of higher end finishes), so the report can compare them. |
| `calculate_cash_flow` | Original | For a mortgage at purchase. Computes the mortgage payment, monthly operating costs (including any HOA or condo fee), cash left over each month and year, the debt service coverage ratio (DSCR), break even rent, cash needed upfront, cash on cash return, and the highest price where rent covers the loan at a DSCR of 1.0 and 1.25. It also builds a month by month timeline: when the loan is paid off (on schedule, and if leftover cash goes to extra principal), the first year with positive cash flow, the year rent has repaid all the upfront cash, and the year the owner is ahead once paid down principal and any appreciation are counted. For rents above the local median, it also reports the monthly premium, how many years that premium takes to repay a luxury upgrade, and the cash flow if rent only reaches the median. |
| `analyze_cash_then_refinance` | Original | For buying with cash and refinancing after the renovation. Totals the cash put in (price, buyer's premium, closing, repairs, holding costs with no mortgage), then sizes the refinance loan as the smallest of three lender limits: a share of the after repair value (default 75%), the lender's rent test (gross rent divided by mortgage, taxes, insurance and HOA, default minimum 1.0), and a seasoning cap based on the purchase cost if refinancing before about 6 months. Reports cash back at the refinance, cash left in the deal, cash flow after the refinance, and a timeline from the refinance date. |
| `stress_test_cash_flow` | Original | Runs named scenarios (rent only reaches the median for luxury plans, rent 15% lower, 20% vacancy, taxes and insurance up 30%, maintenance doubles, rate 1 point higher, a 9 month eviction or rehab) plus a seeded Monte Carlo simulation. Reports the probability that rent does not cover the loan, typical and bad month cash flow, the total cash needed in a bad case (upfront cash plus a first year shortfall), and the highest price that covers the loan 75% of the time. |

Every tool validates its inputs and returns errors as JSON with an instruction for the model. The harness also fills each stress test's inputs (rate, loan, price, rent, costs) from the matching Standard or Luxury analysis, so the stress test always tests the same deal shown in the report. For example, if FRED is unreachable the agent asks the user for their lender's quoted rate.

## Sample queries

1. `3 bed in 46201 listed at $120,000, 1,300 sq ft, built 1960. I want to renovate it to a luxury standard and charge above median rent. 25% down. Will the rent cover my mortgage?` (set the financing switch to Mortgage)
2. `Auction opening bid is $85,000 for a 2 bed in 44105, 1,100 sq ft, built 1925. Vacant for years and the roof is sagging. 20% down at 7.25%. Does the rent cover the loan?` (set the financing switch to Mortgage)
3. `Price $150,000, rent $1,600, 6.9% rate, 25% down, taxes $2,400 a year. How much is left over each month?` (set the financing switch to Mortgage)
4. `Buying a 3 bed in 46201 at auction for $90,000 cash, 1,300 sq ft, built 1960. Renovated it should be worth about $175,000. I'll refinance after 6 months. Will the rent cover the new loan, and how much of my cash do I get back?`

Follow up example in the same session: `What if I put 35% down instead?`

## Running locally

```bash
uv sync
export CENSUS_API_KEY=your_key   # free at https://api.census.gov/data/key_signup.html
export FRED_API_KEY=your_key     # free at https://fredaccount.stlouisfed.org/apikeys
uv run app.py
```

Then open http://127.0.0.1:8000. On Cloud Run, set `CENSUS_API_KEY` and `FRED_API_KEY` under Variables & Secrets. Without a FRED key the rate tool falls back to FRED's public CSV download, and if both fail the agent asks the user for their quoted rate.

## Assumptions and limits

Defaults are 25% down, no HOA fee (the agent flags a missing fee for condos), a 30 year fixed loan, 8% vacancy, 10% management, 10% maintenance reserve, $2,000 taxes, $1,500 insurance, a 5% buyer's premium for auctions (0 for regular sales), 3% closing costs, 4 months to get the unit rented, 2.5% yearly growth in rent and in taxes, insurance and HOA, and no appreciation (to keep the timeline conservative). The user can override any of them in conversation. Census rents are medians that include utilities and lag the current market. The Freddie Mac rate is a national average for owner occupied homes, so the agent adjusts it up. Many auctions require cash or proof of funds, which is why cash then refinance is the default. Refinance results depend on the appraisal matching the after repair value and on each lender's rules for loan to value, rent coverage and seasoning, which vary. Repair tiers and the luxury upgrade cost are rough rules of thumb that vary by market. The upper quartile rent describes existing rentals in the ZIP, so a truly new luxury unit could rent higher; the agent only goes above it when the user supplies comps. This is an educational tool, not financial advice.
