import re
RULES=[
 ("Economy",r"(cpi|inflation|\bfed\b|bps|unemployment|interest rate|duty rate|tariff rate|gdp|jobs report|payroll|recession)"),
 ("Esports",r"\b(lol|league of legends|cs2|counter-strike|pgl|iem|blast|major|valorant|vct|dota|lck|lpl|worlds|esports|hall of legends|t1|faker|navi|vitality|parivision|faze|g2|fnatic)\b"),
 ("Crypto",r"\b(bitcoin|btc|eth|ethereum|solana|crypto|token|coinbase|binance|memecoin|airdrop|fdv)\b"),
 ("Tech & business",r"\b(anthropic|openai|nvidia|ipo|revenue|apple|tesla|spacex|google|microsoft|meta|ai|gpt|claude|gemini|stock|shares|ceo|huang|musk|amazon|xai|earnings|market cap|riemann|lean proof|proof|deepseek|gpu|fable|open-source model|arena|entity list)\b"),
 ("Pop culture",r"(box office|gta|streamer|xqc|kai cenat|ishowspeed|ludwig|kick|twitch|youtube|album|movie|film|song|spotify|netflix|oscar|grammy|mrbeast|taylor|kardashian|turkey|thanksgiving|celebrity|tweet|post again|tiktok|billboard|episode|season|honeymad|raenee|dcampion|finish|rotten tomatoes|popcornmeter|eurovision|bond girl|grand theft auto|rothko|happiness report)"),
 ("Sports",r"\b(nfl|nba|mlb|nhl|ufc|f1|formula 1|premier league|champions league|fifa|world cup|super bowl|tennis|golf|boxing|olympic)\b"),
 ("Politics",r"(trump|biden|putin|zelensky|election|president|prime minister|minister|parliament|senate|congress|democrat|republican|trifecta|party|leader|iran|israel|ceasefire|ukraine|russia|china|apec|nato|rutte|war|strike|tariff|government|shutdown|vote|referendum|governor|mayor|supreme court|pardon|sanction|bahrain|gaza|hamas|venezuela|maduro|resign|secretary|xi |takaichi)"),
]
def cat(title):
    t=title.lower()
    for name,rx in RULES:
        if re.search(rx,t): return name
    return "Other"
