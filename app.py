import json
import uuid
from pathlib import Path

import litellm
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from tools import TOOLS, run_tool

# --- Config ---

SYSTEM_PROMPT = """You are RentCover, an assistant for people buying a rental property with a mortgage,
including properties bought at auction (Auction.com, sheriff sales, HUD homes). Your job is to answer one
question honestly: will the rent cover the loan and the costs of owning the property, and how much is
left over each month?

How to work:
1. Gather: purchase price (or the auction bid they plan to make), ZIP code and bedrooms. Also use
   anything they give about the loan (down payment, rate, term), taxes, insurance and condition.
   If ZIP or bedrooms are missing, ask for them in one short question.
   Price: if the user gives no price or bid (for example an auction whose starting bid is not out yet),
   do not treat an estimated market value as the price. Run the tools at the market value if one is
   given, label it "Not set: analysis run at the estimated market value", and lead the verdict with the
   highest price where rent covers the loan. If there is no price and no market value, ask for one.
   If they send only a link, say you cannot open links and ask them to paste the listing text.
2. Rent: whenever you know the ZIP and bedrooms, call lookup_market_rent so you can compare against
   the local median, even if the user gave their own rent. Use the user's rent for the cash flow if
   they gave one; otherwise use the Census median.
   Always compare two renovation plans side by side:
   - Standard: standard repairs, rent = the user's rent if given, otherwise the Census median.
   - Luxury: luxury finishes, rent = upper_quartile_rent_for_bedrooms, or rents of renovated comps the
     user gives. Never go above the upper quartile without comps. If the upper quartile is not available,
     or is not above the standard rent, or square footage is unknown (the luxury cost depends on size),
     show only the Standard plan and say in one line why the luxury plan was skipped.
   Financing: each user message ends with a bracketed note from a switch in the app with the financing
   plan. If the user's own words say otherwise, their words win.
   - Mortgage at purchase: use calculate_cash_flow for each plan, then stress_test_cash_flow for each plan.
   - Cash then refinance: use analyze_cash_then_refinance instead of calculate_cash_flow, once per plan. It needs an after
     repair value: use the user's figure or renovated comps; otherwise the listing's estimated market value,
     labeled "listing estimate, before renovation" (a renovation may raise it, but do not assume that);
     otherwise ask the user for it and run nothing until they answer. Never use the purchase price or
     bid as the after repair value. Pass the same standard value to both plans; for Luxury the tool
     raises it automatically (pass luxury_after_repair_value only if the user gives a luxury value).
     Use the refinance rate in place of the mortgage rate (step 3). Then call
     stress_test_cash_flow for each plan with that plan's monthly_rent and repair_cost. The app fills in
     the loan, rate and other inputs from the matching analysis automatically.
3. Rate: use the user's lender quote, otherwise call get_mortgage_rate and add 0.75 points, because
   rental property loans usually cost more than the national average. Say that you did this.
4. Repairs: they change the cash needed upfront, not the monthly cash flow. If the user gave square
   footage and year built, call estimate_repairs once (condition from what they described, "unknown" if
   nothing) and use both_finishes: standard.mid for Standard; luxury.mid as repair_cost and
   luxury_upgrade_mid as luxury_upgrade_cost for Luxury. If the user gave their own repair figure, use it
   for Standard and add luxury_upgrade_mid for Luxury. With no square footage, use the user's figure or 0
   and say the upfront cash excludes repairs. Do not hold up the answer for repairs.
5. Always run the cash flow analysis for each plan before giving any opinion: that is TWO calls, one
   for Standard and one for Luxury, plus one stress test for each (unless the Luxury plan is skipped).
   Pass market_median_rent and luxury_upgrade_cost only on the Luxury call, never on the Standard call.
6. HOA: only for condos and townhomes, pass hoa_monthly from the listing. If a condo or townhome has no
   fee listed, use 0 and add a "Watch out" bullet that the fee is missing. Never mention HOA fees for a
   single family house.
7. Auctions: set buyer_premium_pct from the listing if given (default 0.05); for a regular sale use 0.
   Only when the financing plan is a mortgage at purchase, add a "Watch out" bullet that many auctions
   require cash, so the loan may have to come from a refinance.

Report. When the analysis is done, answer with exactly these parts and nothing else. Keep it tight:
every number appears once, and leave out anything the user can find in the tool cards.

**Verdict:** one paragraph of two or three sentences comparing the plans: for each, does the rent cover the loan and how
much is left over or short each month (bold), and which plan comes out ahead. Then the single most
useful next step: for a mortgage plan only, the highest price where rent covers the loan (never state a
highest price for cash then refinance, since it is not computed); when the rent needed
is above the local market, say that raising the rent will not fix it and the price or renovation budget
has to come down. If no price was given, say the analysis was run at the estimated market value.

### Property details
A bulleted list, one fact per bullet, each starting with a bold label, in this order:
- **Location:** address, city, state, ZIP and county if known
- **Price:** purchase price, opening bid or planned bid, or "Not set" (see step 1)
- **Bedrooms / bathrooms**
- **Square footage**
- **Year built**
- **Property type:** single family, condo, townhome or multi family
- **Occupancy and access:** occupied or vacant, interior access yes or no
- **Condition:** what the user or listing described
- **Estimated value:** with its source, if given
- **Auction:** sale type and date, if it is an auction
Then add a bullet for any other fact the user gave that matters to a buyer (lot size, HOA fee, taxes from
the listing, title or lien notes). Write "Not provided" for location, price, bedrooms / bathrooms,
square footage and year built when missing; leave out the other bullets when missing.

Do not write any tables. The app builds the Standard vs Luxury cash flow tables from the tool results
and inserts them after Property details.

**Watch out:** at most three short bullets about risks the tables and property details do not already show (occupied,
appraisal or value uncertainty, missing condo fee, old house, the hardest stress scenario if it flips the
result). Do not repeat the verdict.

**Assumptions:** one line with the rate, down payment (or loan to value and refinance month for a
refinance, and that the Luxury value is the lower of a rent based and a cost based estimate), taxes, insurance, and "28% of rent for vacancy, management and upkeep" (use the actual
total if changed), then "Ask me to change any."

No other sections, headings, notes, disclaimers or closing questions. If the user asks for more detail
(cost breakdown, refinance limits, luxury payback, stress scenarios, timeline), answer from the tool
results in a short bulleted list.

For follow up questions that change the numbers, rerun the tools and give the full short report again,
starting with one line on what changed. For questions that need no new numbers, answer in a few
sentences without the report.

Rules: never invent numbers. Property facts come only from the user; every dollar figure and
probability comes only from a tool result. If a tool returns an error, follow its instructions.
Only if the user asks whether they should buy, add one sentence that this is not financial advice and
they should talk to a lender or advisor before bidding."""
MAX_TOOL_ROUNDS = 12  # two plans means up to two cash flow analyses and two stress tests

# Appended to each user message so the agent always knows the financing picked in the app
FINANCING_NOTES = {
    "cash_refi": "\n\n[Financing set in the app: buy with cash, renovate, then refinance.]",
    "mortgage": "\n\n[Financing set in the app: mortgage at purchase with a down payment.]",
}

# --- The Harness ---

# Inputs the stress test shares with the cash flow analysis it is testing
SHARED_INPUTS = ["purchase_price", "monthly_rent", "repair_cost", "loan_term_years", "down_payment_pct",
                 "property_taxes_annual", "insurance_annual", "hoa_monthly", "buyer_premium_pct",
                 "holding_months", "vacancy_rate", "market_median_rent"]


def matching_analysis(messages: list[dict], stress_args: dict) -> tuple[str, dict, dict] | None:
    """The cash flow analysis this stress test belongs to: (name, args, result).

    Each report has a Standard and a Luxury analysis, which differ in rent and repairs. Pick the most
    recent successful analysis whose rent and repairs match the stress test's; if none match, the latest.
    """
    results = {m.get("tool_call_id"): m.get("content") for m in messages if m.get("role") == "tool"}
    best, best_score = None, -1
    for m in reversed(messages):  # newest first, so ties keep the most recent
        for call in reversed(m.get("tool_calls") or []):
            name = call["function"]["name"]
            if name not in ("calculate_cash_flow", "analyze_cash_then_refinance") or call["id"] not in results:
                continue
            try:
                result = json.loads(results[call["id"]])
                args = json.loads(call["function"]["arguments"])
            except (ValueError, TypeError):
                continue
            if "error" in result:
                continue
            score = sum(1 for k in ("monthly_rent", "repair_cost")
                        if k in stress_args and k in args and float(stress_args[k]) == float(args[k]))
            if score > best_score:
                best, best_score = (name, args, result), score
    return best


def align_stress_test(args: dict, messages: list[dict]) -> dict:
    """Make the stress test use exactly the deal from the matching cash flow analysis.

    The model has to copy a dozen numbers between tools, and the rate is named differently in the
    refinance tool, so the harness copies them instead of trusting the model to.
    """
    found = matching_analysis(messages, args)
    if not found:
        return args
    name, analysis_args, result = found
    aligned = dict(args)
    for key in SHARED_INPUTS:
        if key in analysis_args:
            aligned[key] = analysis_args[key]
    if name == "analyze_cash_then_refinance":
        aligned["interest_rate_pct"] = analysis_args.get("refinance_rate_pct")
        aligned["loan_amount"] = result["phase_2_refinance"]["loan_amount"]
    else:
        aligned["interest_rate_pct"] = analysis_args.get("interest_rate_pct")
        aligned.pop("loan_amount", None)
    return aligned



# --- Report tables ---
# Built by code from the tool results rather than written by the model, so the Standard and Luxury
# columns always line up and every number matches the tools exactly.

ANALYSES = ("calculate_cash_flow", "analyze_cash_then_refinance")


def money(x) -> str:
    if x is None:
        return "n/a"
    return f"${x:,.0f}" if x >= 0 else f"−${-x:,.0f}"


def years(x) -> str:
    return "now" if x == 0 else ("not within 40 yrs" if x is None else f"{x} yrs")


def plan_of(args: dict) -> str:
    luxury = (args.get("luxury_upgrade_cost") or 0) > 0 or (
        args.get("market_median_rent") is not None and args.get("monthly_rent", 0) > args["market_median_rent"])
    return "Luxury" if luxury else "Standard"


def report_tables(tool_calls: list[dict]) -> str:
    """Markdown tables comparing the Standard and Luxury analyses run this turn (empty if none ran)."""
    # Successful analyses this turn, keyed by (rent, repairs); a rerun of the same plan replaces the earlier one
    found = {}
    for c in tool_calls:
        try:
            result = json.loads(c["result"])
        except (ValueError, TypeError):
            continue
        if c["name"] in ANALYSES and "error" not in result:
            key = (float(c["args"].get("monthly_rent") or 0), float(c["args"].get("repair_cost") or 0))
            found[key] = {"name": c["name"], "args": c["args"], "result": result, "stress": None}
    if not found:
        return ""
    # Label by the numbers, not by which optional arguments the model happened to pass:
    # with two or more analyses, the lowest rent and repairs is Standard and the highest is Luxury.
    keys = sorted(found)
    if len(keys) >= 2:
        plans = {"Standard": found[keys[0]], "Luxury": found[keys[-1]]}
    else:
        only = found[keys[0]]
        plans = {plan_of(only["args"]): only}
    for c in tool_calls:  # attach each stress test to the plan with the same rent and repairs
        if c["name"] != "stress_test_cash_flow":
            continue
        try:
            result = json.loads(c["result"])
        except (ValueError, TypeError):
            continue
        for p in plans.values():
            if "error" not in result and all(
                    float(c["args"].get(k) or 0) == float(p["args"].get(k) or 0) for k in ("monthly_rent", "repair_cost")):
                p["stress"] = result["simulation"]

    cols = [k for k in ("Standard", "Luxury") if k in plans]
    refi = plans[cols[0]]["name"] == "analyze_cash_then_refinance"

    def row(label: str, fn) -> str:
        cells = []
        for k in cols:
            try:
                cells.append(fn(plans[k]))
            except (KeyError, TypeError):
                cells.append("n/a")
        return f"| {label} | " + " | ".join(cells) + " |"

    def monthly(p):
        return p["result"]["monthly_after_refinance"] if refi else p["result"]["monthly"]

    def var_costs(p):
        oc = monthly(p)["operating_costs"]
        return money(oc["vacancy"] + oc["management"] + oc["maintenance_reserve"])

    def fixed_costs(p):
        oc = monthly(p)["operating_costs"]
        return money(oc["property_taxes"] + oc["insurance"] + oc["hoa_or_condo_fee"])

    def mortgage(p):
        r = p["result"]
        loan = r["phase_2_refinance"] if refi else r["loan"]
        amount = loan["loan_amount"] if refi else loan["amount"]
        return f"{money(monthly(p)['mortgage_payment'])} on {money(amount)} at {loan['rate_pct']}%"

    def left_over(p):
        s = p["stress"]
        bad = f" (bad month {money(s['monthly_cash_flow_p10'])})" if s else ""
        return f"**{money(monthly(p)['cash_flow'])}**{bad}"

    def chance(p):
        return f"{p['stress']['prob_rent_does_not_cover_loan']:.0%}" if p["stress"] else "n/a"

    def timeline(p):
        return p["result"]["timeline_from_refinance" if refi else "timeline"]

    header = "| | " + " | ".join(cols) + " |\n|---|" + "---|" * len(cols)
    month_rows = []
    if refi:
        month_rows.append(row("Before the refinance (rent after costs, no mortgage yet)",
                              lambda p: money(p["result"]["monthly_before_refinance"]["cash_flow"])))
        n = plans[cols[0]]["result"]["phase_2_refinance"]["months_after_purchase"]
    month_rows += [
        row("Rent", lambda p: money(p["args"]["monthly_rent"])),
        row("Vacancy, management, upkeep", var_costs),
        row("Taxes, insurance, HOA", fixed_costs),
        row(f"Mortgage (refinance loan, starts month {n:g})" if refi else "Mortgage", mortgage),
        row("Left over each month" + (" after the refinance" if refi else ""), left_over),
        row("Chance rent falls short", chance),
        row("Break even rent", lambda p: money(p["result"]["break_even_rent"])),
    ]
    big_rows = []
    if refi:
        big_rows += [
            row("After repair value", lambda p: money(p["result"]["phase_2_refinance"]["after_repair_value"])),
            row("Repairs", lambda p: money(p["result"]["phase_1_cash_purchase"]["repairs"])),
            row("Cash in → left after refinance", lambda p: (
                f"{money(p['result']['phase_1_cash_purchase']['total_cash_in'])} → "
                + (money(p["result"]["cash_left_in_deal"]) if p["result"]["cash_left_in_deal"] > 0 else "all cash back"))),
        ]
    else:
        big_rows += [
            row("Repairs", lambda p: money(p["result"]["cash_needed_upfront"]["repairs"])),
            row("Cash needed upfront", lambda p: money(p["result"]["cash_needed_upfront"]["total"])),
            row("Highest price where rent covers the loan", lambda p: money(p["result"]["max_price_rent_covers_loan"])),
        ]
    if "Luxury" in plans:
        big_rows.append(row("Luxury payback", lambda p: "—" if p is plans.get("Standard") else (
            years((p["result"].get("luxury_premium") or {}).get("payback_years")))))
    big_rows.append(row("All cash back / ahead overall", lambda p: (
        f"{years(timeline(p)['years_until_cash_back'])} / {years(timeline(p)['years_until_profit_including_equity'])}")))

    return ("### Monthly cash flow\n" + header + "\n" + "\n".join(month_rows)
            + "\n\n### The bigger picture\n" + header + "\n" + "\n".join(big_rows))


def add_tables(response: str, tool_calls: list[dict]) -> str:
    """Insert the tables after the property details: just before "Watch out" (or "Assumptions").

    Falls back to right after the verdict (the first paragraph) if neither marker is found.
    """
    tables = report_tables(tool_calls)
    if not tables or not response:
        return response
    text = response.strip()
    spots = [i for i in (text.find("**Watch out"), text.find("**Assumptions")) if i > 0]
    if spots:
        at = min(spots)
        return text[:at].rstrip() + "\n\n" + tables + "\n\n" + text[at:]
    first, _, rest = text.partition("\n\n")
    return first + "\n\n" + tables + ("\n\n" + rest if rest else "")


def latest_result(messages: list[dict], tool_name: str) -> dict | None:
    """Most recent successful result of a tool in this session."""
    names = {}
    for m in messages:
        for call in m.get("tool_calls") or []:
            names[call["id"]] = call["function"]["name"]
    for m in reversed(messages):
        if m.get("role") == "tool" and names.get(m.get("tool_call_id")) == tool_name:
            try:
                result = json.loads(m["content"])
            except (ValueError, TypeError):
                continue
            if "error" not in result:
                return result
    return None


def align_analysis(args: dict, messages: list[dict]) -> dict:
    """Give the Luxury analysis its luxury inputs and keep them off the Standard one.

    Whether a call is Luxury is decided from the rent lookup already in the conversation (rent at or
    above the top quarter rent), not from which optional arguments the model remembered to pass.
    """
    rent_info = latest_result(messages, "lookup_market_rent")
    if not rent_info or args.get("monthly_rent") is None:
        return args
    median = rent_info.get("median_rent_for_bedrooms") or rent_info.get("median_rent_all_units")
    top = rent_info.get("upper_quartile_rent_for_bedrooms")
    rent = float(args["monthly_rent"])
    luxury = (top is not None and rent >= top - 1) or (
        (args.get("luxury_upgrade_cost") or 0) > 0 and median is not None and rent > median)
    aligned = dict(args)
    if luxury:
        if median:
            aligned["market_median_rent"] = median
        repairs = latest_result(messages, "estimate_repairs")
        if not aligned.get("luxury_upgrade_cost") and repairs:
            aligned["luxury_upgrade_cost"] = repairs.get("luxury_upgrade_mid") or \
                repairs.get("both_finishes", {}).get("luxury_upgrade_mid", 0)
        # A "luxury value" equal to the standard value is not a real override
        if aligned.get("luxury_after_repair_value") == aligned.get("after_repair_value"):
            aligned.pop("luxury_after_repair_value", None)
    else:
        for key in ("luxury_upgrade_cost", "luxury_after_repair_value"):
            aligned.pop(key, None)
    return aligned


def run_agent(messages: list[dict]) -> tuple[str, list[dict]]:
    """Complete until the model answers without asking for a tool.

    Returns the final text and a record of every tool call made along the way.
    """
    tool_calls = []

    for _ in range(MAX_TOOL_ROUNDS):
        reply = litellm.completion(
            model="vertex_ai/gemini-3.5-flash-lite",
            vertex_location="global",
            messages=messages,
            tools=TOOLS,
        ).choices[0].message

        # Append assistant's reply (text, tool calls, or both) to the context.
        # model_dump() keeps it a plain dict: the raw object carries provider-specific
        # fields that trip Pydantic when LiteLLM re-serializes it next round.
        messages += [reply.model_dump()]

        if not reply.tool_calls:
            return reply.content, tool_calls

        # The harness, not the model, runs each tool and appends the result
        for call in reply.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except ValueError:
                # Malformed arguments: tell the model instead of failing the whole reply
                args = {}
                result = json.dumps({"error": f"The arguments for {call.function.name} were not valid JSON. "
                                              "Call the tool again with a JSON object of arguments."})
            else:
                if call.function.name == "stress_test_cash_flow":
                    args = align_stress_test(args, messages)
                elif call.function.name in ANALYSES:
                    args = align_analysis(args, messages)
                result = run_tool(call.function.name, args)
            tool_calls += [{"name": call.function.name, "args": args, "result": result}]

            messages += [{"role": "tool", "tool_call_id": call.id, "content": result}]

    return "Sorry, I hit my tool-call limit before finishing.", tool_calls


# --- Session Store ---

# session_id -> list of messages. In-memory, single process.
sessions: dict[str, list] = {}

# --- FastAPI App ---

app = FastAPI()


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    financing: str | None = None  # "cash_refi" or "mortgage", from the switch in the app


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    # Get or create the session
    session_id = request.session_id or str(uuid.uuid4())
    if session_id not in sessions:
        sessions[session_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Append user's message to the context
    sessions[session_id] += [{"role": "user", "content": request.message + FINANCING_NOTES.get(request.financing, "")}]

    try:
        response, tool_calls = run_agent(sessions[session_id])
    except Exception as e:
        # Auth, billing, a model that is not running: show it in the chat, not as a 500.
        response, tool_calls = f"Model call failed: {type(e).__name__}: {str(e)[:300]}", []

    response = add_tables(response, tool_calls)
    return ChatResponse(response=response, session_id=session_id, tool_calls=tool_calls)


@app.post("/clear")
def clear(session_id: str | None = None):
    sessions.pop(session_id, None)
    return {"status": "ok"}


if __name__ == "__main__":
    import os

    # Cloud Run sets PORT and needs 0.0.0.0; locally this falls back to 127.0.0.1:8000
    port = int(os.environ.get("PORT", 8000))
    host = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"
    uvicorn.run(app, host=host, port=port)
