"""Function words that are never offered as a suggested term.

Tokens, not words as written: lowercase and with accents stripped, the way
`relevance_service.tokenize` leaves them. Only languages with many short
function words need a list at all. The inflow share cap in the suggestions
(`relevance_suggest_service.MAX_INFLOW_SHARE`) already drops whatever is common
in the reader's own feeds, so a list only has to catch what slips under it,
which in the eval was English and Czech (`may`, `could`, `they`, `jsou`).

English is scikit-learn's `ENGLISH_STOP_WORDS` (BSD licence), the list the
eval measured with, copied rather than imported: scikit-learn is not a runtime
dependency. Czech is the eval's own list.
"""

ENGLISH = frozenset("""
a about above across after afterwards again against all almost alone along already
also although always am among amongst amoungst amount an and another any anyhow
anyone anything anyway anywhere are around as at back be became because become
becomes becoming been before beforehand behind being below beside besides between
beyond bill both bottom but by call can cannot cant co con could couldnt cry de
describe detail do done down due during each eg eight either eleven else elsewhere
empty enough etc even ever every everyone everything everywhere except few fifteen
fifty fill find fire first five for former formerly forty found four from front full
further get give go had has hasnt have he hence her here hereafter hereby herein
hereupon hers herself him himself his how however hundred i ie if in inc indeed
interest into is it its itself keep last latter latterly least less ltd made many
may me meanwhile might mill mine more moreover most mostly move much must my myself
name namely neither never nevertheless next nine no nobody none noone nor not
nothing now nowhere of off often on once one only onto or other others otherwise our
ours ourselves out over own part per perhaps please put rather re same see seem
seemed seeming seems serious several she should show side since sincere six sixty so
some somehow someone something sometime sometimes somewhere still such system take
ten than that the their them themselves then thence there thereafter thereby
therefore therein thereupon these they thick thin third this those though three
through throughout thru thus to together too top toward towards twelve twenty two un
under until up upon us very via was we well were what whatever when whence whenever
where whereafter whereas whereby wherein whereupon wherever whether which while
whither who whoever whole whom whose why will with within without would yet you your
yours yourself yourselves
""".split())

CZECH = frozenset("""
a aby ac ale ani ano asi az bez bude budou budu by byl byla byli bylo byly byt ci co
coz do ho i jak jake jaky jako je jeho jej jeji jejich jen jeste ji jiz jsem jsi
jsme jsou jste k kam kde kdo kdy kdyz ke ktera ktere kteri kterou ktery kterym
kterych ma maji mame mate me mezi mi mit mne mnou muj muze my na nad nam nami nas
nase nasi ne nebo neni nez nic nich nim o od ode on ona oni ono pak po pod podle
pokud pouze prave pred pres pri pro proc proto protoze prvni s se si sve svych svym
svou ta tak take takze tam te tedy ten tento teto tim timto to tohle toho tohoto tom
tomto tomu tu tuto ty tyto u uz v ve vice vsak vse vsechny vsech z za zde ze rok
roku let dnes jiz cela cely celou
""".split())

STOPWORDS = ENGLISH | CZECH
