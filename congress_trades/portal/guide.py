"""Plain-language guides for noob mode: one per tab and report.

Each says what the screen shows, why someone might care, what they could do
with it, and what to watch out for. Deliberately long -- noob mode is for
someone meeting stock disclosures, alpha and benchmarks for the first time.
Shown on hover only when noob mode is on, so the default view stays terse.
"""

GUIDES: dict[str, dict[str, str]] = {
    "": {
        "what": "A summary of everything collected: how many trades Congress has "
                "disclosed, how many members, and a chart of buying (above the line) "
                "and selling (below) month by month.",
        "why": "It answers \"is Congress trading more or less than usual, and which "
               "way?\" at a glance, before you dig into any one person or stock.",
        "use": "Click a month's bar to list exactly those trades. Spot a month with "
               "unusual selling, then open it to see who sold what.",
        "careful": "Trades are disclosed up to 45 days after they happen, so the most "
                   "recent months always look quieter than they will end up.",
    },
    "trends": {
        "what": "The stocks Congress traded most over a time window, ranked four ways: "
                "net buying minus selling, total dollars, number of trades, or how "
                "many different members traded it.",
        "why": "\"How many different members\" is the most interesting: several people "
               "independently buying the same smaller company means more than one "
               "person making a big trade.",
        "use": "Find names Congress is piling into or out of, then click a bar to see "
               "every member who traded it and when.",
        "careful": "Dollar figures are the lower end of the ranges members report, so "
                   "they are minimums, not exact amounts.",
    },
    "explore": {
        "what": "Every single disclosed trade, searchable and sortable: who, what, "
                "when, which direction, how big, and how it did afterwards.",
        "why": "When a headline mentions a member or a stock, this is where you check "
               "the actual filings yourself instead of trusting a summary.",
        "use": "Filter to one member, one ticker, a date range or big trades only. "
               "Click any name for that member's full record.",
        "careful": "\"Alpha\" is how the stock did against the market in the 90 days "
                   "after the trade was made public. Options and untickered assets "
                   "have none.",
    },
    "watch": {
        "what": "Members and stocks you choose to follow, for example the stocks you "
                "own yourself.",
        "why": "Instead of checking the whole site, you get told when someone in "
               "Congress trades something you care about.",
        "use": "Add your own holdings. Any congressional trade in them then raises an "
               "alert and shows up at the top of the daily digest.",
        "careful": "A member trading a stock you own is a fact, not advice: the "
                   "backtest shows following individual members has not paid.",
    },
    "page": {
        "what": "A single self-contained web page with the members, movers and "
                "scoreboard views.",
        "why": "You can save it or send it to someone; it works with no server and no "
               "installation.",
        "use": "Share it with someone curious about congressional trading without "
               "giving them access to this portal.",
        "careful": "It is a snapshot from the last refresh, and it never includes your "
                   "watchlist.",
    },
    "jobs": {
        "what": "The behind-the-scenes controls: refresh the data, pick the AI model "
                "that writes briefs, and run the longer jobs.",
        "why": "Everything else on this site reads what these jobs collect.",
        "use": "Run Refresh data if the numbers look stale. Choose an AI model if you "
               "want the written briefs and the asset labelling.",
        "careful": "Jobs write to the database and only one runs at a time; a full "
                   "refresh can take 15 to 25 minutes.",
    },
    "r/digest": {
        "what": "A one-page written briefing of the last 90 days: the stocks most "
                "members converged on, sectors being bought or sold, unusually large "
                "lone bets, and trades in sectors a member's own committee oversees.",
        "why": "It condenses thousands of filings into something you can read in two "
               "minutes, and it is the same text the AI brief is written from.",
        "use": "Read it weekly to stay current, or paste it into any AI chat and ask "
               "questions about it.",
        "careful": "It lists what happened, not what will happen. The caveats at the "
                   "top are there for a reason.",
    },
    "r/scorecard": {
        "what": "Every member with enough trades, ranked by how their trades did "
                "against the market (\"alpha\") in the 90 days after each was made "
                "public, plus how they did against their own sector.",
        "why": "It is the honest answer to \"is my representative a good trader?\" "
               "because it compares with simply owning the market, not with zero.",
        "use": "Look up a member's real record; check \"vs sector\" to see whether "
               "they picked good stocks or just rode a hot industry; check \"top name\" "
               "to see whether one big bet explains everything.",
        "careful": "Past records have not predicted future ones here: a member near "
                   "the top is no more likely to stay there than to drop. History, "
                   "not tips.",
    },
    "r/backtest": {
        "what": "A test of strategies such as \"copy the top-ranked members\" or "
                "\"copy every disclosure\", using only data that was available at "
                "the time, then grading them on what happened next.",
        "why": "Rankings always look clever in hindsight. This checks whether "
               "following them would actually have paid, with honest error bars.",
        "use": "Before acting on any \"follow Congress\" idea, see whether it "
               "survived. So far only copying everything clears zero, barely, and "
               "picking members adds nothing.",
        "careful": "No trading costs, taxes or slippage are included, so real "
                   "results would be worse than shown.",
    },
    "r/lag": {
        "what": "How long members take to disclose trades, and whether trades "
                "disclosed late did better or worse afterwards.",
        "why": "If late filings did unusually well, it would suggest someone "
               "delayed disclosing their best ideas.",
        "use": "See which members file slowly, and whether speed of filing tells you "
               "anything about a trade.",
        "careful": "Lateness can be paperwork, not intent. The pattern across many "
                   "trades matters more than any single slow filing.",
    },
    "r/timing": {
        "what": "Whether members trade more, or better, in the days around their "
                "own committee's hearings.",
        "why": "Committee members hear things before the public does. Unusual "
               "trading around hearings is the classic worry about congressional "
               "trading.",
        "use": "Tick \"sector-matched\" for the stricter version that only counts "
               "hearings about the industry that was traded.",
        "careful": "Most trades fall near some hearing by chance, which is why the "
                   "sector-matched version exists. A weak result means too little "
                   "data, not proof of innocence.",
    },
    "r/mix": {
        "what": "What kinds of assets members hold and trade: listed stocks, funds, "
                "bonds, Treasuries, private partnerships, options, crypto and more.",
        "why": "It separates members who actively pick stocks from those who mostly "
               "park money in funds and bonds.",
        "use": "Before reading a member's stock record, check whether stocks are "
               "even most of what they trade.",
        "careful": "Labels for assets without a ticker are best guesses from their "
                   "names.",
    },
    "r/alerts": {
        "what": "Recent trades that crossed a bar: very large, unusual for that "
                "member, in their committee's sector, several members on one stock, "
                "or on your watchlist.",
        "why": "It is the short list of things worth a second look, instead of "
               "every filing.",
        "use": "Check it after a refresh, or follow the alerts feed in a feed "
               "reader or on your phone.",
        "careful": "An alert means something is notable, never that anyone did "
                   "something wrong.",
    },
    "r/flags": {
        "what": "One row per member with four facts side by side: late filings, "
                "trades in sectors their own committees oversee, PAC money from those "
                "same sectors, and large trades no other member made.",
        "why": "It puts the accountability questions about each member in one place.",
        "use": "Find a member, read each column, then open the matching report "
               "(Compliance, PAC money) for the detail.",
        "careful": "There is no overall score on purpose, and one late filing in a "
                   "hundred raises a flag. Read the numbers, not the count.",
    },
    "r/compliance": {
        "what": "Trades disclosed after the STOCK Act's 45-day deadline, by member, "
                "plus the most extreme cases.",
        "why": "Late filing is a clear-cut rule violation you can check from the "
               "dates alone.",
        "use": "See which members file late most often and by how much; useful "
               "background for asking them about it.",
        "careful": "Some late filings are honest mistakes that were later corrected.",
    },
    "r/annual": {
        "what": "From members' yearly financial disclosures: their debts, outside "
                "income, and positions such as board seats.",
        "why": "Trades show what members buy and sell; annual reports show what they "
               "own, owe and are paid by outside groups.",
        "use": "Check whether a member has ties to companies or groups connected to "
               "what they trade or regulate.",
        "careful": "Only the annual reports fetched so far are included, so a missing "
                   "member means not collected yet, not nothing to report.",
    },
    "r/finance": {
        "what": "Campaign money from industry PACs, matched against the industries a "
                "member's committees oversee and the stocks they trade.",
        "why": "It shows where the same member takes money from an industry, "
               "regulates it, and trades in it.",
        "use": "Find members where all three overlap and look at the details "
               "yourself.",
        "careful": "Matching a PAC to an industry is a keyword guess, and taking PAC "
                   "money is legal and common.",
    },
    "r/lobbying": {
        "what": "Lobbying filings by industry, compared with the sectors members "
                "trade.",
        "why": "It shows which industries are lobbying Congress hardest while members "
               "trade in them.",
        "use": "See which sectors draw the most lobbying, and compare with where "
               "members' money is going.",
        "careful": "Lobbying filings never name a specific member, so this is about "
                   "industries, not individuals.",
    },
    "r/judiciary": {
        "what": "Federal judges' disclosed investments, from public bulk data.",
        "why": "Judges have similar conflict-of-interest questions to Congress, and "
               "this shows how much of that data is covered here.",
        "use": "A starting point for research on judges' holdings.",
        "careful": "Judges' filings have no tickers, so holdings cannot be priced or "
                   "scored like congressional trades.",
    },
    "r/jurisdiction": {
        "what": "Which industries each committee and subcommittee oversees.",
        "why": "Several other reports (Flags, Committee timing, PAC money) depend on "
               "this table, so it is shown openly.",
        "use": "Check what a member's committee seats actually cover before reading "
               "a \"trades in their committee's sector\" flag.",
        "careful": "It was drafted once by an AI model and reviewed by hand; it is a "
                   "judgement, not an official definition.",
    },
    "r/parser-qa": {
        "what": "A quality check on how well this tool reads the House's PDF filings: "
                "transactions found in the text against rows actually stored.",
        "why": "If the parser starts missing trades, every number on this site is "
               "quietly wrong.",
        "use": "Mostly for maintenance: run it after the House changes its forms.",
        "careful": "It checks the reading of filings, not the filings themselves.",
    },
}
