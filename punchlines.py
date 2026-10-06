"""Original Tollywood-style (Telugu in English letters) reminder lines, stronger as the deadline gets closer.
These are original lines written in a mass-hero style, not quotes from any film."""
import random

TIERS = [
    # (minimum seconds left for this tier, lines)
    (12 * 3600, [  # a day or more: relaxed but firm
        "Time undi bro, kaani time ante bangaram. Ippude plan chesko!",
        "Deadline inka dooram anukunte, ade first mistake. Start cheyyi!",
        "Roju oka step vesthe chalu, deadline mundu nilchovachu.",
        "Nuvvu ready aithe, deadline ye bhayapadutundi!",
    ]),
    (2 * 3600 + 1, [  # several hours: game on
        "Ippude asalu game start. Laptop teeyyi, pani modalettu!",
        "Time tik tik antondi... nuvvu inka scroll chestunnava?",
        "Mass ga start chesi, class ga submit cheyyi!",
        "Dhairyam ga cheyyi. Doubt unte friends ni adugu, kaani time waste cheyyaku!",
    ]),
    (3600 + 1, [  # 1-2 hours: serious mode
        "Ika serious mode on ra! Phone pakkana pettu, work lo dig!",
        "Okka gantalo pani aipothundi, nuvvu focus aite chalu.",
        "Last stretch ra. Ippudu aagithe, tarvata regret!",
    ]),
    (30 * 60 + 1, [  # about 1 hour to 30 min
        "Inka konchem time matrame. Nenu cheppanu antha, nuvvu cheyyi antha!",
        "Final round start ayyindi. Submit button ni gurthupettuko!",
    ]),
    (0, [  # last minutes: panic
        "10 nimishaalu ra! Ippude upload cheyyi, aalochinchaku!",
        "Clock cheppindi: ippude submit, ippude!",
        "Last minutes ra! Ippudu kaakapothe inkepudu? SUBMIT NOW!",
    ]),
]

_last = {}


def pick(seconds_left: int) -> str:
    """Return a line for how much time is left, avoiding an immediate repeat."""
    for floor, lines in TIERS:
        if seconds_left >= floor:
            options = [l for l in lines if l != _last.get(floor)] or lines
            choice = random.choice(options)
            _last[floor] = choice
            return choice
    return TIERS[-1][1][0]
