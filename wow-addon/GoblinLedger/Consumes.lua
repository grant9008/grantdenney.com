local ADDON_NAME, ns = ...
-- Goblin Ledger: the best consumes for your role and level, under the character screen.
--
-- When the character screen is open on its Character tab, a list hangs under
-- it: the best food, potions, elixirs and scrolls (and flasks, weapon stones
-- and oils when there are some) for your role and level, what each costs at
-- today's AH value (or a vendor's price) and how many you carry. Well Fed food
-- gives +5% XP from kills in Forever (below level 60): those rows say so in
-- green. A click on the header folds it to one line (remembered); All shows
-- every usable item instead of the best 4. Best / Budget (next to All) shows the
-- strongest, or good value for less gold: ones at least half as strong as the
-- strongest, the most strength per copper first (with All: cheapest first). The
-- grey number on each row is how strong it is next to the strongest one of its
-- kind you can use ("best", "62%"). Hover a row: the item's tooltip (and that in
-- words); a click while the auction house is open searches it there; shift-click
-- links it in chat. /gl consumes off turns it off, /gl consumes on brings it back,
-- /gl consumes budget or best switches the picks.
-- The role is the one the enchant tips use (talents, or /gl enchants <role>).
-- Plain ASCII only.

ADDON_NAME = ADDON_NAME or "GoblinLedger"
ns = ns or {}

local _G = _G
local type, tonumber, tostring, ipairs, pcall = type, tonumber, tostring, ipairs, pcall
local floor, min, max = math.floor, math.min, math.max
local tsort = table.sort
local smatch, slower, sformat = string.match, string.lower, string.format

local C = { lastError = nil, builds = 0, places = 0, draws = 0, polls = 0 }   -- state for /gl consumes and the tests
ns.Consumes = C

-- BEGIN DATA
-- Generated from the consumables research (consumes.json, 2026-11-19) by gen_consumes_lua.py.
-- Sources: Forever client tables on wago.tools (wowf 1.60.1.70959: Item, ItemSparse, ItemEffect,
-- Spell, SpellEffect, SpellDuration, ...), e.g. https://wago.tools/db2/SpellEffect/csv?build=1.60.1.70959;
-- Questie's Forever data for vendors, drops and recipes; Blizzard's Forever item tooltips;
-- rankedboost.com/world-of-warcraft/forever/buffs/ (consumables, food, xp-buffs).
-- Only what can be had in Forever now (on the AH, from a vendor or a craft you can learn);
-- Zul'Gurub, Molten Core, BWL, AQ and Naxxramas are not in the game, so their buffs are left out.
-- XP: every Well Fed ('Nutritious') food gives +5% XP from kills (spell 1243969, aura 200 = 5,
-- quest XP aura 291 = 0) below level 60: https://wago.tools/db2/SpellEffect?build=1.60.1.70959&filter[SpellID]=1243969
-- No potion, elixir, flask or scroll you can get gives XP.
-- id = { name, short name (nil: the name), section, level needed, quality, buff, % XP from kills,
--        vendor price in copper (0: no vendor; the Darkmoon Faire's do not count), group (two of the
--        best in one group: only the better one), classes (nil: all; the item's class mask, school
--        elixirs only for the classes with that school) }
-- Each item's line ends with its use spell: https://wago.tools/db2/Spell?build=1.60.1.70959&filter[ID]=<spell>
local ITEMS = {
  [118] = { "Minor Healing Potion", "Minor Healing", "potion", 1, 1, "heals 70-90", 0, 0, "name:Minor Healing Potion" },   -- spell 439
  [724] = { "Goretusk Liver Pie", nil, "food", 5, 1, "+3 Str", 5, 0, "food:str" },   -- spell 1248384
  [787] = { "Slitherskin Mackerel", nil, "food", 1, 1, "+6 AP", 5, 25, "food:ap,rap" },   -- spell 1249503
  [858] = { "Lesser Healing Potion", "Lesser Healing", "potion", 3, 1, "heals 140-180", 0, 100, "name:Lesser Healing Potion" },   -- spell 440
  [929] = { "Healing Potion", "Healing", "potion", 12, 1, "heals 280-360", 0, 300, "name:Healing Potion" },   -- spell 441
  [954] = { "Scroll of Strength", nil, "scroll", 10, 1, "+5 Str, 30 min", 0, 200, "scroll-str" },   -- spell 8118
  [955] = { "Scroll of Intellect", nil, "scroll", 5, 1, "+4 Int, 30 min", 0, 150, "scroll-int" },   -- spell 8096
  [1017] = { "Seasoned Wolf Kabob", nil, "food", 15, 1, "+5 Agi", 5, 0, "food:agi" },   -- spell 1248387
  [1082] = { "Redridge Goulash", nil, "food", 5, 1, "+3 Int", 5, 0, "food:int" },   -- spell 1248383
  [1180] = { "Scroll of Stamina", "Stamina", "scroll", 5, 1, "+4 Sta, 30 min", 0, 150, "scroll-sta" },   -- spell 8099
  [1181] = { "Scroll of Spirit", nil, "scroll", 1, 1, "+3 Spi, 30 min", 0, 100, "scroll-spi" },   -- spell 8112
  [1477] = { "Scroll of Agility II", "Agility II", "scroll", 25, 1, "+9 Agi, 30 min", 0, 0, "scroll-agi" },   -- spell 8116
  [1478] = { "Scroll of Protection II", "Protection II", "scroll", 15, 1, "+120 armor, 30 min", 0, 250, "scroll-armor" },   -- spell 8094
  [1710] = { "Greater Healing Potion", "Greater Healing", "potion", 21, 1, "heals 455-585", 0, 500, "name:Greater Healing Potion" },   -- spell 2024
  [1711] = { "Scroll of Stamina II", "Stamina II", "scroll", 20, 1, "+8 Sta, 30 min", 0, 300, "scroll-sta" },   -- spell 8100
  [1712] = { "Scroll of Spirit II", "Spirit II", "scroll", 15, 1, "+7 Spi, 30 min", 0, 250, "scroll-spi" },   -- spell 8113
  [2289] = { "Scroll of Strength II", "Strength II", "scroll", 25, 1, "+9 Str, 30 min", 0, 0, "scroll-str" },   -- spell 8119
  [2290] = { "Scroll of Intellect II", "Intellect II", "scroll", 20, 1, "+8 Int, 30 min", 0, 300, "scroll-int" },   -- spell 8097
  [2454] = { "Elixir of Minor Strength", "Minor Strength", "elixir", 1, 1, "+4 Str, 1 hr", 0, 0, "elixir-str" },   -- spell 2367
  [2455] = { "Minor Mana Potion", "Minor Mana", "potion", 5, 1, "140-180 mana", 0, 40, "name:Minor Mana Potion" },   -- spell 437
  [2456] = { "Minor Rejuvenation Potion", "Minor Rejuvenation", "potion", 5, 1, "heals 90-150, 90-150 mana", 0, 0, "name:Minor Rejuvenation Potion" },   -- spell 2370
  [2457] = { "Elixir of Minor Agility", "Minor Agility", "elixir", 2, 1, "+4 Agi, 1 hr", 0, 0, "elixir-agi" },   -- spell 2374
  [2458] = { "Elixir of Minor Fortitude", "Minor Fortitude", "elixir", 2, 1, "+27 HP, 1 hr", 0, 0, "elixir-hp" },   -- spell 2378
  [2459] = { "Swiftness Potion", "Swiftness", "potion", 5, 1, "+50% speed, 15 sec", 0, 0, "name:Swiftness Potion" },   -- spell 2379
  [2680] = { "Spiced Wolf Meat", nil, "food", 1, 1, "+1 Agi", 5, 0, "food:agi" },   -- spell 1248378
  [2681] = { "Roasted Boar Meat", nil, "food", 1, 1, "+1 Str", 5, 0, "food:str" },   -- spell 1248380
  [2682] = { "Cooked Crab Claw", nil, "food", 1, 1, "+1 Int", 5, 0, "food:int" },   -- spell 1248379
  [2683] = { "Crab Cake", nil, "food", 5, 1, "+3 Int", 5, 0, "food:int" },   -- spell 1248383
  [2684] = { "Coyote Steak", nil, "food", 5, 1, "+3 Agi", 5, 0, "food:agi" },   -- spell 1248382
  [2685] = { "Succulent Pork Ribs", nil, "food", 15, 1, "+5 Str", 5, 0, "food:str" },   -- spell 1248389
  [2687] = { "Dry Pork Ribs", nil, "food", 5, 1, "+3 Str", 5, 0, "food:str" },   -- spell 1248384
  [2862] = { "Rough Sharpening Stone", nil, "weapon", 1, 1, "+2 weapon dmg, 30 min", 0, 0, "weapon:bladed (sword, axe, dagger, polearm):weapon_dmg" },   -- spell 2828
  [2863] = { "Coarse Sharpening Stone", nil, "weapon", 5, 1, "+3 weapon dmg, 30 min", 0, 0, "weapon:bladed (sword, axe, dagger, polearm):weapon_dmg" },   -- spell 2829
  [2871] = { "Heavy Sharpening Stone", nil, "weapon", 15, 1, "+4 weapon dmg, 30 min", 0, 0, "weapon:bladed (sword, axe, dagger, polearm):weapon_dmg" },   -- spell 2830
  [2888] = { "Beer Basted Boar Ribs", nil, "food", 1, 1, "+1 Str", 5, 0, "food:str" },   -- spell 1248380
  [3012] = { "Scroll of Agility", nil, "scroll", 10, 1, "+5 Agi, 30 min", 0, 200, "scroll-agi" },   -- spell 8115
  [3013] = { "Scroll of Protection", "Protection", "scroll", 1, 1, "+60 armor, 30 min", 0, 100, "scroll-armor" },   -- spell 8091
  [3220] = { "Blood Sausage", nil, "food", 5, 1, "+3 Str", 5, 0, "food:str" },   -- spell 1248384
  [3239] = { "Rough Weightstone", nil, "weapon", 1, 1, "+2 weapon dmg, 30 min", 0, 0, "weapon:blunt (mace, staff, fist):weapon_dmg" },   -- spell 3112
  [3240] = { "Coarse Weightstone", nil, "weapon", 5, 1, "+3 weapon dmg, 30 min", 0, 0, "weapon:blunt (mace, staff, fist):weapon_dmg" },   -- spell 3113
  [3241] = { "Heavy Weightstone", nil, "weapon", 15, 1, "+4 weapon dmg, 30 min", 0, 0, "weapon:blunt (mace, staff, fist):weapon_dmg" },   -- spell 3114
  [3382] = { "Minor Troll's Blood Elixir", nil, "elixir", 1, 1, "+2 hp5, 1 hr", 0, 0, "elixir-hp5" },   -- spell 3219
  [3383] = { "Elixir of Wisdom", "Wisdom", "elixir", 10, 1, "+3 Int, 30 min", 0, 0, "elixir-int" },   -- spell 3166
  [3384] = { "Minor Magic Resistance Potion", "Minor Magic Resistance", "potion", 12, 1, "+25 all res, 3 min", 0, 0, "name:Minor Magic Resistance Potion" },   -- spell 2380
  [3385] = { "Lesser Mana Potion", "Lesser Mana", "potion", 14, 1, "280-360 mana", 0, 120, "name:Lesser Mana Potion" },   -- spell 438
  [3387] = { "Limited Invulnerability Potion", "Limited Invulnerability", "potion", 45, 1, "immune to physical, 6 sec", 0, 0, "name:Limited Invulnerability Potion" },   -- spell 3169
  [3388] = { "Lesser Troll's Blood Elixir", nil, "elixir", 15, 1, "+6 hp5, 1 hr", 0, 0, "elixir-hp5" },   -- spell 3222
  [3389] = { "Elixir of Lesser Defense", "Lesser Defense", "elixir", 16, 1, "+150 armor, 1 hr", 0, 0, "elixir-armor" },   -- spell 3220
  [3390] = { "Elixir of Lesser Agility", "Lesser Agility", "elixir", 18, 1, "+8 Agi, 1 hr", 0, 0, "elixir-agi" },   -- spell 3160
  [3391] = { "Elixir of Ogre Strength", "Ogre Strength", "elixir", 20, 1, "+8 Str, 1 hr", 0, 0, "elixir-str" },   -- spell 3164
  [3662] = { "Crocolisk Steak", nil, "food", 5, 1, "+3 Agi", 5, 0, "food:agi" },   -- spell 1248382
  [3663] = { "Murloc Fin Soup", nil, "food", 1, 1, "+1 Sta", 5, 0, "food:sta" },   -- spell 1248377
  [3664] = { "Crocolisk Gumbo", nil, "food", 15, 1, "+5 Agi", 5, 0, "food:agi" },   -- spell 1248387
  [3665] = { "Curiously Tasty Omelet", nil, "food", 15, 1, "+5 Sta", 5, 0, "food:sta" },   -- spell 1248386
  [3666] = { "Gooey Spider Cake", nil, "food", 15, 1, "+5 Sta", 5, 0, "food:sta" },   -- spell 1248386
  [3726] = { "Big Bear Steak", nil, "food", 5, 1, "+3 Str", 5, 0, "food:str" },   -- spell 1248384
  [3727] = { "Hot Lion Chops", nil, "food", 15, 1, "+5 Agi", 5, 0, "food:agi" },   -- spell 1248387
  [3728] = { "Tasty Lion Steak", nil, "food", 25, 1, "+10 Agi", 5, 0, "food:agi" },   -- spell 1248391
  [3729] = { "Soothing Turtle Bisque", nil, "food", 15, 1, "+5 Sta", 5, 0, "food:sta" },   -- spell 1248386
  [3825] = { "Elixir of Lesser Fortitude", "Lesser Fortitude", "elixir", 25, 1, "+120 HP, 1 hr", 0, 0, "elixir-hp" },   -- spell 3593
  [3826] = { "Troll's Blood Elixir", nil, "elixir", 26, 1, "+12 hp5, 1 hr", 0, 0, "elixir-hp5" },   -- spell 3223
  [3827] = { "Mana Potion", "Mana", "potion", 22, 1, "455-585 mana", 0, 480, "name:Mana Potion" },   -- spell 2023
  [3928] = { "Superior Healing Potion", "Superior Healing", "potion", 35, 1, "heals 700-900", 0, 1000, "name:Superior Healing Potion" },   -- spell 4042
  [4419] = { "Scroll of Intellect III", "Intellect III", "scroll", 35, 1, "+12 Int, 30 min", 0, 0, "scroll-int" },   -- spell 8098
  [4421] = { "Scroll of Protection III", "Protection III", "scroll", 30, 1, "+180 armor, 30 min", 0, 0, "scroll-armor" },   -- spell 8095
  [4422] = { "Scroll of Stamina III", "Stamina III", "scroll", 35, 1, "+12 Sta, 30 min", 0, 0, "scroll-sta" },   -- spell 8101
  [4424] = { "Scroll of Spirit III", "Spirit III", "scroll", 30, 1, "+11 Spi, 30 min", 0, 0, "scroll-spi" },   -- spell 8114
  [4425] = { "Scroll of Agility III", "Agility III", "scroll", 40, 1, "+13 Agi, 30 min", 0, 0, "scroll-agi" },   -- spell 8117
  [4426] = { "Scroll of Strength III", "Strength III", "scroll", 40, 1, "+13 Str, 30 min", 0, 0, "scroll-str" },   -- spell 8120
  [4457] = { "Barbecued Buzzard Wing", nil, "food", 25, 1, "+10 Int", 5, 0, "food:int" },   -- spell 1248392
  [4592] = { "Longjaw Mud Snapper", nil, "food", 5, 1, "+10 AP", 5, 20, "food:ap,rap" },   -- spell 1249506
  [4596] = { "Minor Discolored Healing Potion", "Minor Discolored Healing", "potion", 5, 1, "heals 150-170, 75% back as DoT", 0, 0, "name:Minor Discolored Healing Potion" },   -- spell 1244863
  [4623] = { "Lesser Stoneshield Potion", "Lesser Stoneshield", "potion", 33, 1, "+1000 armor, 90 sec", 0, 0, "name:Lesser Stoneshield Potion" },   -- spell 4941
  [5095] = { "Rainbow Fin Albacore", nil, "food", 5, 1, "+1 spell dmg", 5, 0, "food:sp" },   -- spell 1249501
  [5205] = { "Sprouted Frond", nil, "potion", 5, 1, "heals 86-102", 0, 0, "name:Sprouted Frond" },   -- spell 2052
  [5472] = { "Kaldorei Spider Kabob", nil, "food", 1, 1, "+1 Sta", 5, 0, "food:sta" },   -- spell 1248377
  [5477] = { "Strider Stew", nil, "food", 1, 1, "+1 Int", 5, 0, "food:int" },   -- spell 1248379
  [5479] = { "Crispy Lizard Tail", nil, "food", 5, 1, "+3 Sta", 5, 0, "food:sta" },   -- spell 1248381
  [5480] = { "Lean Venison", nil, "food", 15, 1, "+5 Str", 5, 0, "food:str" },   -- spell 1248389
  [5527] = { "Goblin Deviled Clams", nil, "food", 15, 1, "+6 Sta, +6 Spi", 0, 0, "food:spi,sta" },   -- spell 5006
  [5631] = { "Rage Potion", "Rage", "potion", 4, 1, "20-40 rage", 0, 0, "name:Rage Potion", "DRUID,WARRIOR" },   -- spell 6612
  [5633] = { "Great Rage Potion", "Great Rage", "potion", 25, 1, "30-60 rage", 0, 0, "name:Great Rage Potion", "DRUID,WARRIOR" },   -- spell 6613
  [5634] = { "Free Action Potion", "Free Action", "potion", 20, 1, "no root or stun, 30 sec", 0, 0, "name:Free Action Potion" },   -- spell 6615
  [5997] = { "Elixir of Minor Defense", "Minor Defense", "elixir", 1, 1, "+50 armor, 1 hr", 0, 0, "spell-673" },   -- spell 673
  [6038] = { "Giant Clam Scorcho", nil, "food", 25, 1, "+10 Sta", 5, 0, "food:sta" },   -- spell 1248390
  [6048] = { "Shadow Protection Potion", "Shadow Protection", "potion", 17, 1, "absorbs 900 shadow, 2 min", 0, 0, "name:Shadow Protection Potion" },   -- spell 7242
  [6049] = { "Fire Protection Potion", "Fire Protection", "potion", 23, 1, "absorbs 1300 fire, 2 min", 0, 0, "name:Fire Protection Potion" },   -- spell 7233
  [6050] = { "Frost Protection Potion", "Frost Protection", "potion", 28, 1, "absorbs 1800 frost, 2 min", 0, 0, "name:Frost Protection Potion" },   -- spell 7239
  [6051] = { "Holy Protection Potion", "Holy Protection", "potion", 10, 1, "absorbs 400 holy, 2 min", 0, 0, "name:Holy Protection Potion" },   -- spell 7245
  [6052] = { "Nature Protection Potion", "Nature Protection", "potion", 28, 1, "absorbs 1800 nature, 2 min", 0, 0, "name:Nature Protection Potion" },   -- spell 7254
  [6149] = { "Greater Mana Potion", "Greater Mana", "potion", 31, 1, "700-900 mana", 0, 480, "name:Greater Mana Potion" },   -- spell 11903
  [6290] = { "Brilliant Smallfish", nil, "food", 1, 1, "+2 AP", 5, 0, "food:ap,rap" },   -- spell 1249500
  [6373] = { "Elixir of Fire Power", "Fire Power", "elixir", 18, 1, "+10 fire dmg, 30 min", 0, 0, "elixir-sp_fire", "MAGE,WARLOCK" },   -- spell 7844
  [6662] = { "Elixir of Giant Growth", "Giant Growth", "elixir", 8, 1, "+8 Str, 2 min", 0, 0, "elixir-str" },   -- spell 8212
  [6887] = { "Spotted Yellowtail", nil, "food", 35, 1, "+22 spell dmg", 5, 2000, "food:sp" },   -- spell 1249513
  [6888] = { "Herb Baked Egg", nil, "food", 1, 1, "+1 Sta", 5, 0, "food:sta" },   -- spell 1248377
  [6890] = { "Smoked Bear Meat", nil, "food", 1, 1, "+1 Str", 5, 0, "food:str" },   -- spell 1248380
  [7964] = { "Solid Sharpening Stone", nil, "weapon", 25, 1, "+6 weapon dmg, 30 min", 0, 0, "weapon:bladed (sword, axe, dagger, polearm):weapon_dmg" },   -- spell 9900
  [7965] = { "Solid Weightstone", nil, "weapon", 25, 1, "+6 weapon dmg, 30 min", 0, 0, "weapon:blunt (mace, staff, fist):weapon_dmg" },   -- spell 9903
  [8364] = { "Mithril Head Trout", nil, "food", 25, 1, "+20 AP", 5, 0, "food:ap,rap" },   -- spell 1249509
  [8949] = { "Elixir of Agility", nil, "elixir", 27, 1, "+15 Agi, 1 hr", 0, 0, "elixir-agi" },   -- spell 11328
  [8951] = { "Elixir of Defense", "Defense", "elixir", 29, 1, "+250 armor, 1 hr", 0, 0, "elixir-armor" },   -- spell 11349
  [9036] = { "Magic Resistance Potion", "Magic Resistance", "potion", 32, 1, "+50 all res, 3 min", 0, 0, "name:Magic Resistance Potion" },   -- spell 11364
  [9144] = { "Wildvine Potion", "Wildvine", "potion", 35, 1, "heals 1-1499, 1-1499 mana", 0, 0, "name:Wildvine Potion" },   -- spell 11387
  [9155] = { "Arcane Elixir", nil, "elixir", 37, 1, "+20 spell dmg, 30 min", 0, 0, "elixir-sp" },   -- spell 11390
  [9179] = { "Elixir of Greater Intellect", "Greater Intellect", "elixir", 37, 1, "+25 Int, 1 hr", 0, 0, "elixir-int" },   -- spell 11396
  [9187] = { "Elixir of Greater Agility", "Greater Agility", "elixir", 38, 1, "+25 Agi, 1 hr", 0, 0, "elixir-agi" },   -- spell 11334
  [9206] = { "Elixir of Greater Strength", "Greater Strength", "elixir", 48, 1, "+25 Str, 1 hr", 0, 0, "elixir-str" },   -- spell 11405
  [9264] = { "Elixir of Shadow Power", "Shadow Power", "elixir", 40, 1, "+40 shadow dmg, 30 min", 0, 0, "elixir-sp_shadow", "PRIEST,WARLOCK" },   -- spell 11474
  [10305] = { "Scroll of Protection IV", "Protection IV", "scroll", 45, 1, "+240 armor, 30 min", 0, 0, "scroll-armor" },   -- spell 12175
  [10306] = { "Scroll of Spirit IV", "Spirit IV", "scroll", 45, 1, "+15 Spi, 30 min", 0, 0, "scroll-spi" },   -- spell 12177
  [10307] = { "Scroll of Stamina IV", "Stamina IV", "scroll", 50, 1, "+16 Sta, 30 min", 0, 0, "scroll-sta" },   -- spell 12178
  [10308] = { "Scroll of Intellect IV", "Intellect IV", "scroll", 50, 1, "+16 Int, 30 min", 0, 0, "scroll-int" },   -- spell 12176
  [10309] = { "Scroll of Agility IV", "Agility IV", "scroll", 55, 1, "+17 Agi, 30 min", 0, 0, "scroll-agi" },   -- spell 12174
  [10310] = { "Scroll of Strength IV", "Strength IV", "scroll", 55, 1, "+17 Str, 30 min", 0, 0, "scroll-str" },   -- spell 12179
  [12209] = { "Lean Wolf Steak", nil, "food", 15, 1, "+5 Agi", 5, 0, "food:agi" },   -- spell 1248387
  [12210] = { "Roast Raptor", nil, "food", 15, 1, "+5 Int", 5, 0, "food:int" },   -- spell 1248388
  [12212] = { "Jungle Stew", nil, "food", 35, 1, "+15 Agi", 5, 0, "food:agi" },   -- spell 1248395
  [12213] = { "Carrion Surprise", nil, "food", 5, 1, "+3 Sta", 5, 0, "food:sta" },   -- spell 1248381
  [12214] = { "Mystery Stew", nil, "food", 15, 1, "+5 Sta", 5, 0, "food:sta" },   -- spell 1248386
  [12215] = { "Heavy Kodo Stew", nil, "food", 25, 1, "+10 Sta", 5, 0, "food:sta" },   -- spell 1248390
  [12216] = { "Spiced Chili Crab", nil, "food", 25, 1, "+10 Int", 5, 0, "food:int" },   -- spell 1248392
  [12218] = { "Monster Omelet", nil, "food", 35, 1, "+15 Sta", 5, 0, "food:sta" },   -- spell 1248394
  [12224] = { "Crispy Bat Wing", nil, "food", 1, 1, "+1 Int", 5, 0, "food:int" },   -- spell 1248379
  [12404] = { "Dense Sharpening Stone", nil, "weapon", 35, 1, "+8 weapon dmg, 30 min", 0, 0, "weapon:bladed (sword, axe, dagger, polearm):weapon_dmg" },   -- spell 16138
  [12643] = { "Dense Weightstone", nil, "weapon", 35, 1, "+8 weapon dmg, 30 min", 0, 0, "weapon:blunt (mace, staff, fist):weapon_dmg" },   -- spell 16622
  [12820] = { "Winterfall Firewater", nil, "elixir", 45, 1, "+35 AP, 20 min", 0, 0, "firewater-ap" },   -- spell 17038
  [13442] = { "Mighty Rage Potion", "Mighty Rage", "potion", 46, 1, "45-75 rage, +60 Str, 20 sec", 0, 0, "name:Mighty Rage Potion", "DRUID,WARRIOR" },   -- spell 17528
  [13443] = { "Superior Mana Potion", "Superior Mana", "potion", 41, 1, "900-1500 mana", 0, 0, "name:Superior Mana Potion" },   -- spell 17530
  [13444] = { "Major Mana Potion", "Major Mana", "potion", 49, 1, "1350-2250 mana", 0, 0, "name:Major Mana Potion" },   -- spell 17531
  [13445] = { "Elixir of Greater Defense", "Greater Defense", "elixir", 43, 1, "+450 armor, 1 hr", 0, 0, "elixir-armor" },   -- spell 11348
  [13446] = { "Major Healing Potion", "Major Healing", "potion", 45, 1, "heals 1050-1750", 0, 0, "name:Major Healing Potion" },   -- spell 17534
  [13447] = { "Elixir of the Sages", nil, "elixir", 38, 1, "+18 Int, +18 Spi, 1 hr", 0, 0, "elixir-int" },   -- spell 17535
  [13452] = { "Elixir of the Mongoose", "Mongoose", "elixir", 46, 1, "+25 Agi, +2% crit, 30 min", 0, 0, "elixir-agi" },   -- spell 17538
  [13453] = { "Elixir of Brute Force", "Brute Force", "elixir", 45, 1, "+18 Str, +18 Sta, 1 hr", 0, 0, "elixir-str" },   -- spell 17537
  [13454] = { "Greater Arcane Elixir", nil, "elixir", 47, 1, "+35 spell dmg, 1 hr", 0, 0, "elixir-sp" },   -- spell 17539
  [13455] = { "Greater Stoneshield Potion", "Greater Stoneshield", "potion", 46, 1, "+2000 armor, 2 min", 0, 0, "name:Greater Stoneshield Potion" },   -- spell 17540
  [13456] = { "Greater Frost Protection Potion", "Greater Frost Protection", "potion", 48, 1, "absorbs 2600 frost, 2 min", 0, 0, "name:Greater Frost Protection Potion" },   -- spell 17544
  [13457] = { "Greater Fire Protection Potion", "Greater Fire Protection", "potion", 48, 1, "absorbs 2600 fire, 2 min", 0, 0, "name:Greater Fire Protection Potion" },   -- spell 17543
  [13459] = { "Greater Shadow Protection Potion", "Greater Shadow Protection", "potion", 48, 1, "absorbs 2600 shadow, 2 min", 0, 0, "name:Greater Shadow Protection Potion" },   -- spell 17548
  [13461] = { "Greater Arcane Protection Potion", "Greater Arcane Protection", "potion", 48, 1, "absorbs 2600 arcane, 2 min", 0, 0, "name:Greater Arcane Protection Potion" },   -- spell 17549
  [13510] = { "Flask of the Titans", "Titans", "flask", 50, 1, "+1200 HP, 2 hr", 0, 0, "flask:hp" },   -- spell 17626
  [13511] = { "Flask of Distilled Wisdom", "Distilled Wisdom", "flask", 50, 1, "+2000 max mana, 2 hr", 0, 0, "flask:max_mana" },   -- spell 17627
  [13512] = { "Flask of Supreme Power", "Supreme Power", "flask", 50, 1, "+150 spell dmg, 2 hr", 0, 0, "flask:sp" },   -- spell 17628
  [13513] = { "Flask of Chromatic Resistance", "Chromatic Resistance", "flask", 50, 1, "+25 all res, 2 hr", 0, 0, "flask:res_all" },   -- spell 17629
  [13851] = { "Hot Wolf Ribs", nil, "food", 25, 1, "+10 Agi", 5, 0, "food:agi" },   -- spell 1248391
  [13927] = { "Cooked Glossy Mightfish", nil, "food", 35, 1, "+30 AP", 5, 0, "food:ap,rap" },   -- spell 1249512
  [13928] = { "Grilled Squid", nil, "food", 35, 1, "+1% crit", 5, 0, "food:crit" },   -- spell 1249522
  [13931] = { "Nightfin Soup", nil, "food", 35, 1, "+22 spell dmg", 5, 0, "food:sp" },   -- spell 1249513
  [13932] = { "Poached Sunscale Salmon", nil, "food", 35, 1, "+40 AP", 5, 0, "food:ap,rap" },   -- spell 1249515
  [13933] = { "Lobster Stew", nil, "food", 45, 1, "+28 spell dmg", 5, 0, "food:sp" },   -- spell 1249516
  [13934] = { "Mightfish Steak", nil, "food", 45, 1, "+40 AP", 5, 0, "food:ap,rap" },   -- spell 1249515
  [13935] = { "Baked Salmon", nil, "food", 45, 1, "+28 spell dmg", 5, 0, "food:sp" },   -- spell 1249516
  [16971] = { "Clamlette Surprise", nil, "food", 40, 1, "+12 Sta, +12 Spi", 0, 0, "food:spi,sta" },   -- spell 10256
  [17222] = { "Spider Sausage", nil, "food", 35, 1, "+15 Sta", 5, 0, "food:sta" },   -- spell 1248394
  [17348] = { "Major Healing Draught", nil, "potion", 45, 1, "heals 980-1260, battlegrounds only", 0, 1000, "name:Major Healing Draught" },   -- spell 21393
  [17349] = { "Superior Healing Draught", nil, "potion", 35, 1, "heals 560-720, battlegrounds only", 0, 500, "name:Superior Healing Draught" },   -- spell 21394
  [17351] = { "Major Mana Draught", nil, "potion", 45, 1, "980-1260 mana, battlegrounds only", 0, 1000, "name:Major Mana Draught" },   -- spell 21395
  [17352] = { "Superior Mana Draught", nil, "potion", 35, 1, "560-720 mana, battlegrounds only", 0, 500, "name:Superior Mana Draught" },   -- spell 21396
  [17747] = { "Razorlash Root", nil, "potion", 40, 1, "heals 600, 600 mana", 0, 0, "name:Razorlash Root" },   -- spell 21955
  [18045] = { "Tender Wolf Steak", nil, "food", 35, 1, "+15 Agi", 5, 0, "food:agi" },   -- spell 1248395
  [18253] = { "Major Rejuvenation Potion", "Major Rejuvenation", "potion", 50, 1, "heals 1440-1760, 1440-1760 mana", 0, 0, "name:Major Rejuvenation Potion" },   -- spell 22729
  [18254] = { "Runn Tum Tuber Surprise", nil, "food", 35, 1, "+15 Int", 5, 0, "food:int" },   -- spell 1248396
  [18839] = { "Combat Healing Potion", "Combat Healing", "potion", 35, 1, "heals 700-900", 0, 1000, "name:Combat Healing Potion" },   -- spell 4042
  [18841] = { "Combat Mana Potion", "Combat Mana", "potion", 41, 1, "900-1500 mana", 0, 1100, "name:Combat Mana Potion" },   -- spell 17530
  [20074] = { "Heavy Crocolisk Stew", nil, "food", 25, 1, "+10 Agi", 5, 0, "food:agi" },   -- spell 1248391
  [20452] = { "Smoked Desert Dumplings", nil, "food", 45, 1, "+20 Str", 5, 0, "food:str" },   -- spell 1248401
  [20744] = { "Minor Wizard Oil", nil, "weapon", 5, 1, "+8 spell dmg, +8 healing, 30 min", 0, 0, "weapon:any weapon:heal,sp" },   -- spell 25117
  [20745] = { "Minor Mana Oil", nil, "weapon", 20, 1, "+10 healing, +5 mp5, 30 min", 0, 0, "weapon:any weapon:heal,mp5" },   -- spell 25118
  [20746] = { "Lesser Wizard Oil", nil, "weapon", 30, 1, "+16 spell dmg, +16 healing, 30 min", 0, 0, "weapon:any weapon:heal,sp" },   -- spell 25119
  [20747] = { "Lesser Mana Oil", nil, "weapon", 40, 1, "+20 healing, +10 mp5, 30 min", 0, 0, "weapon:any weapon:heal,mp5" },   -- spell 25120
  [20750] = { "Wizard Oil", nil, "weapon", 40, 1, "+24 spell dmg, +24 healing, 30 min", 0, 0, "weapon:any weapon:heal,sp" },   -- spell 25121
  [21072] = { "Smoked Sagefish", nil, "food", 10, 1, "+4 spell dmg", 5, 0, "food:sp" },   -- spell 1249504
  [21217] = { "Sagefish Delight", nil, "food", 30, 1, "+7 spell dmg", 5, 0, "food:sp" },   -- spell 1249507
  [21546] = { "Elixir of Holy Power", "Holy Power", "elixir", 40, 1, "+40 holy dmg, 30 min", 0, 0, "elixir-sp_holy", "PALADIN,PRIEST" },   -- spell 1310077
  [241650] = { "Major Discolored Healing Potion", "Major Discolored Healing", "potion", 60, 1, "heals 2625-2975, 75% back as DoT", 0, 0, "name:Major Discolored Healing Potion" },   -- spell 1231578
  [246948] = { "Distilled Firewater", nil, "elixir", 45, 1, "+35 AP, 20 min", 0, 0, "firewater-ap" },   -- spell 17038
  [247239] = { "Lesser Discolored Healing Potion", "Lesser Discolored Healing", "potion", 3, 1, "heals 300-340, 75% back as DoT", 0, 0, "name:Lesser Discolored Healing Potion" },   -- spell 1244864
  [247240] = { "Discolored Healing Potion", "Discolored Healing", "potion", 12, 1, "heals 600-680, 75% back as DoT", 0, 0, "name:Discolored Healing Potion" },   -- spell 1244865
  [247241] = { "Greater Discolored Healing Potion", "Greater Discolored Healing", "potion", 21, 1, "heals 975-1105, 75% back as DoT", 0, 0, "name:Greater Discolored Healing Potion" },   -- spell 1244866
  [247242] = { "Superior Discolored Healing Potion", "Superior Discolored Healing", "potion", 35, 1, "heals 1500-1700, 75% back as DoT", 0, 0, "name:Superior Discolored Healing Potion" },   -- spell 1244867
  [247754] = { "Minor Arcane Elixir", nil, "elixir", 1, 1, "+5 spell dmg, 30 min", 0, 0, "elixir-sp" },   -- spell 1245244
  [249865] = { "Peace Tea", nil, "food", 1, 1, "+2 healing", 5, 0, "food:heal" },   -- spell 1249920
  [249866] = { "Royal Tea", nil, "food", 5, 1, "+7 healing", 5, 0, "food:heal" },   -- spell 1249921
  [249867] = { "Root Tea", nil, "food", 15, 1, "+11 healing", 5, 0, "food:heal" },   -- spell 1249922
  [249868] = { "Triage Tea", nil, "food", 25, 1, "+22 healing", 5, 0, "food:heal" },   -- spell 1249923
  [249869] = { "Sunny Tea", nil, "food", 35, 1, "+33 healing", 5, 0, "food:heal" },   -- spell 1249924
  [249870] = { "Sage's Tea", nil, "food", 45, 1, "+44 healing", 5, 0, "food:heal" },   -- spell 1249925
  [249871] = { "Venomous Smoothie", nil, "food", 1, 1, "+1 Spi", 5, 0, "food:spi" },   -- spell 1249914
  [249872] = { "Slimy Smoothie", nil, "food", 5, 1, "+3 Spi", 5, 0, "food:spi" },   -- spell 1249915
  [249873] = { "Mrrggl Smrrthle", nil, "food", 15, 1, "+5 Spi", 5, 0, "food:spi" },   -- spell 1249916
  [249874] = { "Calcified Smoothie", nil, "food", 25, 1, "+10 Spi", 5, 0, "food:spi" },   -- spell 1249917
  [249875] = { "Spicy Smoothie", nil, "food", 35, 1, "+15 Spi", 5, 0, "food:spi" },   -- spell 1249918
  [249876] = { "Wicked Smoothie", nil, "food", 45, 1, "+20 Spi", 5, 0, "food:spi" },   -- spell 1249919
  [250065] = { "Savory Stag Sliders", nil, "food", 35, 1, "+15 Str", 5, 0, "food:str" },   -- spell 1248397
  [250067] = { "Bat Hachee", nil, "food", 45, 1, "+20 Int", 5, 0, "food:int" },   -- spell 1248400
  [250068] = { "Savory Turtle Stew", nil, "food", 45, 1, "+20 Sta", 5, 0, "food:sta" },   -- spell 1248398
  [250069] = { "Flank au Poivre", nil, "food", 45, 1, "+20 Agi", 5, 0, "food:agi" },   -- spell 1248399
  [250070] = { "Bear Bruscitti", nil, "food", 45, 1, "+20 Str", 5, 0, "food:str" },   -- spell 1248401
  [250071] = { "Steaming Stag Steak", nil, "food", 45, 1, "+20 Str", 5, 0, "food:str" },   -- spell 1248401
  [250072] = { "Swiftstrike Steak", nil, "food", 45, 1, "+20 Agi", 5, 0, "food:agi" },   -- spell 1248399
  [250073] = { "Raging Raptor Ribs", nil, "food", 25, 1, "+10 Int", 5, 0, "food:int" },   -- spell 1248392
  [250074] = { "Bear Brisket", nil, "food", 25, 1, "+10 Str", 5, 0, "food:str" },   -- spell 1248393
  [250075] = { "Prehistoric Pulled Raptor", nil, "food", 45, 1, "+20 Int", 5, 0, "food:int" },   -- spell 1248400
  [250076] = { "Raptor Rouladen", nil, "food", 35, 1, "+15 Int", 5, 0, "food:int" },   -- spell 1248396
  [250077] = { "Twice-Spiced Raptor Slice", nil, "food", 5, 1, "+3 Int", 5, 0, "food:int" },   -- spell 1248383
  [250079] = { "Tasty Raptor Bites", nil, "food", 1, 1, "+1 Int", 5, 0, "food:int" },   -- spell 1248379
  [250080] = { "Breakfast Omelette", nil, "food", 5, 1, "+3 Sta", 5, 0, "food:sta" },   -- spell 1248381
  [250328] = { "Elixir of Cunning", "Cunning", "elixir", 55, 1, "+25 Agi, +25 Int, 30 min", 0, 0, "elixir-agi" },   -- spell 1250918
  [250329] = { "Elixir of the Phalanx", "Phalanx", "elixir", 55, 1, "+400 HP, +500 armor, 30 min", 0, 0, "elixir-hp" },   -- spell 1250920
  [250330] = { "Minor Cleric's Elixir", nil, "elixir", 5, 1, "+10 healing, 30 min", 0, 0, "elixir-heal" },   -- spell 1250922
  [250331] = { "Lesser Cleric's Elixir", nil, "elixir", 25, 1, "+20 healing, 30 min", 0, 0, "elixir-heal" },   -- spell 1250924
  [250332] = { "Cleric's Elixir", nil, "elixir", 45, 1, "+30 healing, 30 min", 0, 0, "elixir-heal" },   -- spell 1250925
  [250333] = { "Greater Cleric's Elixir", nil, "elixir", 55, 1, "+40 healing, 30 min", 0, 0, "elixir-heal" },   -- spell 1250926
  [250334] = { "Elixir of Fortitude", "Fortitude", "elixir", 45, 1, "+200 HP, 30 min", 0, 0, "elixir-hp" },   -- spell 1250928
  [250335] = { "Elixir of Greater Fortitude", "Greater Fortitude", "elixir", 55, 1, "+400 HP, 30 min", 0, 0, "elixir-hp" },   -- spell 1250931
  [250336] = { "Elixir of Wicked Regeneration", "Wicked Regeneration", "elixir", 55, 1, "+10 mp5, +9 hp5, 30 min", 0, 0, "elixir-hp5" },   -- spell 1250932
  [250337] = { "Elixir of the Owl", "Owl", "elixir", 55, 1, "+25 Int, +2% crit, 30 min", 0, 0, "elixir-int" },   -- spell 1250940
  [250338] = { "Elixir of Sages", nil, "elixir", 55, 1, "+25 Spi, +2% crit, 30 min", 0, 0, "elixir-spi" },   -- spell 1250941
  [250339] = { "Minor Mageblood Elixir", nil, "elixir", 5, 1, "+3 mp5, 30 min", 0, 0, "elixir-mp5" },   -- spell 1250942
  [250340] = { "Lesser Mageblood Elixir", nil, "elixir", 25, 1, "+6 mp5, 30 min", 0, 0, "elixir-mp5" },   -- spell 1250944
  [250341] = { "Greater Mageblood Elixir", nil, "elixir", 55, 1, "+20 mp5, 30 min", 0, 0, "elixir-mp5" },   -- spell 1250948
  [250342] = { "Lesser Arcane Elixir", nil, "elixir", 25, 1, "+15 spell dmg, 30 min", 0, 0, "elixir-sp" },   -- spell 1250971
  [250343] = { "Elixir of Nature Power", "Nature Power", "elixir", 55, 1, "+40 nature dmg, 30 min", 0, 0, "elixir-sp_nature", "DRUID,SHAMAN" },   -- spell 1250972
  [250344] = { "Elixir of Minor Spirit", "Minor Spirit", "elixir", 5, 1, "+3 Spi, 30 min", 0, 0, "elixir-spi" },   -- spell 1250974
  [250345] = { "Elixir of Lesser Spirit", "Lesser Spirit", "elixir", 25, 1, "+6 Spi, 30 min", 0, 0, "elixir-spi" },   -- spell 1250976
  [250346] = { "Elixir of Spirit", nil, "elixir", 45, 1, "+10 Spi, 30 min", 0, 0, "elixir-spi" },   -- spell 1250978
  [250347] = { "Elixir of Greater Spirit", "Greater Spirit", "elixir", 55, 1, "+18 Spi, 30 min", 0, 0, "elixir-spi" },   -- spell 1250979
  [250348] = { "Elixir of the Whale", "Whale", "elixir", 55, 1, "+25 Spi, +2% crit, 30 min", 0, 0, "elixir-spi" },   -- spell 1250981
  [250349] = { "Elixir of Strength", nil, "elixir", 45, 1, "+10 Str, 30 min", 0, 0, "elixir-str" },   -- spell 1250984
  [250350] = { "Elixir of Ferocity", "Ferocity", "elixir", 55, 1, "+18 Str, +18 Agi, 30 min", 0, 0, "elixir-str" },   -- spell 1250985
  [250351] = { "Elixir of the Grizzly", "Grizzly", "elixir", 55, 1, "+25 Str, +2% crit, 30 min", 0, 0, "elixir-str" },   -- spell 1250986
  [250353] = { "Elixir of Lesser Intellect", "Lesser Intellect", "elixir", 25, 1, "+6 Int, 30 min", 0, 0, "elixir-int" },   -- spell 1250988
  [250354] = { "Elixir of Intellect", nil, "elixir", 45, 1, "+10 Int, 30 min", 0, 0, "elixir-int" },   -- spell 1250989
  [250932] = { "Minor Spellblasting Potion", "Minor Spellblasting", "potion", 5, 1, "+7 spell dmg, 30 sec", 0, 0, "name:Minor Spellblasting Potion" },   -- spell 1251919
  [250933] = { "Lesser Spellblasting Potion", "Lesser Spellblasting", "potion", 15, 1, "+11 spell dmg, 30 sec", 0, 0, "name:Lesser Spellblasting Potion" },   -- spell 1251922
  [250934] = { "Spellblasting Potion", "Spellblasting", "potion", 25, 1, "+16 spell dmg, 30 sec", 0, 0, "name:Spellblasting Potion" },   -- spell 1251923
  [250935] = { "Greater Spellblasting Potion", "Greater Spellblasting", "potion", 35, 1, "+23 spell dmg, 30 sec", 0, 0, "name:Greater Spellblasting Potion" },   -- spell 1251924
  [250936] = { "Superior Spellblasting Potion", "Superior Spellblasting", "potion", 45, 1, "+33 spell dmg, 30 sec", 0, 0, "name:Superior Spellblasting Potion" },   -- spell 1251931
  [250937] = { "Major Spellblasting Potion", "Major Spellblasting", "potion", 55, 1, "+47 spell dmg, 30 sec", 0, 0, "name:Major Spellblasting Potion" },   -- spell 1251932
  [250938] = { "Minor Frenzy Potion", "Minor Frenzy", "potion", 5, 1, "+12 AP, 30 sec", 0, 0, "name:Minor Frenzy Potion" },   -- spell 1251933
  [250940] = { "Frenzy Potion", "Frenzy", "potion", 25, 1, "+28 AP, 30 sec", 0, 0, "name:Frenzy Potion" },   -- spell 1251936
  [250941] = { "Greater Frenzy Potion", "Greater Frenzy", "potion", 35, 1, "+40 AP, 30 sec", 0, 0, "name:Greater Frenzy Potion" },   -- spell 1251937
  [250942] = { "Superior Frenzy Potion", "Superior Frenzy", "potion", 45, 1, "+56 AP, 30 sec", 0, 0, "name:Superior Frenzy Potion" },   -- spell 1251938
  [250943] = { "Major Frenzy Potion", "Major Frenzy", "potion", 55, 1, "+80 AP, 30 sec", 0, 0, "name:Major Frenzy Potion" },   -- spell 1251940
  [250944] = { "Minor Mender's Potion", "Minor Mender's", "potion", 5, 1, "+13 healing, 30 sec", 0, 0, "name:Minor Mender's Potion" },   -- spell 1251942
  [250945] = { "Lesser Mender's Potion", "Lesser Mender's", "potion", 15, 1, "+20 healing, 30 sec", 0, 0, "name:Lesser Mender's Potion" },   -- spell 1251944
  [250946] = { "Mender's Potion", "Mender's", "potion", 25, 1, "+31 healing, 30 sec", 0, 0, "name:Mender's Potion" },   -- spell 1251945
  [250947] = { "Greater Mender's Potion", "Greater Mender's", "potion", 35, 1, "+44 healing, 30 sec", 0, 0, "name:Greater Mender's Potion" },   -- spell 1251946
  [250948] = { "Superior Mender's Potion", "Superior Mender's", "potion", 45, 1, "+62 healing, 30 sec", 0, 0, "name:Superior Mender's Potion" },   -- spell 1251947
  [250949] = { "Major Mender's Potion", "Major Mender's", "potion", 55, 1, "+88 healing, 30 sec", 0, 0, "name:Major Mender's Potion" },   -- spell 1251948
  [255663] = { "Windstone", nil, "potion", 0, 1, "heals 32-88, 21-59 mana", 0, 0, "name:Windstone" },   -- spell 1259231
  [263509] = { "Skywall Souffle", nil, "food", 5, 1, "+1 Sta", 5, 0, "food:sta" },   -- spell 1248377
  [263512] = { "Pincer Bites", nil, "food", 5, 1, "+1 Int", 5, 0, "food:int" },   -- spell 1248379
  [274971] = { "Briny Seafood Stew", nil, "food", 25, 1, "+14 spell dmg", 5, 0, "food:sp" },   -- spell 1249510
  [274976] = { "Plain Ol' Paletusk", nil, "food", 25, 1, "+10 Str", 5, 0, "food:str" },   -- spell 1248393
  [275069] = { "Scroll of Rat Familiar", "Rat Familiar", "scroll", 5, 1, "+2 Int, 1 hr", 0, 0, "scroll-int", "MAGE" },   -- spell 1296202
  [277483] = { "Scroll of Frog Familiar", "Frog Familiar", "scroll", 16, 1, "+6 Int, 1 hr", 0, 0, "scroll-int", "MAGE" },   -- spell 1302285
  [277493] = { "Scroll of Cat Familiar", "Cat Familiar", "scroll", 25, 1, "+12 Int, 1 hr", 0, 0, "scroll-int", "MAGE" },   -- spell 1302303
  [286152] = { "Plated Armorfish", nil, "food", 35, 1, "+150 armor", 5, 0, "food:armor" },   -- spell 1319311
}

-- The best for a role, strongest first (ties: cheaper), per level band (1-19, 20-39, 40-49, 50-59, 60).
-- A band lists what that band's top level can use; lower ones stay as fallbacks.
local BEST = {
  melee = {
    ["food"] = {
      { 4592, 2685, 787, 3220, 3664, 3662, 6290, 2681 },   -- 1-19
      { 13932, 13927, 250065, 13928, 8364, 274976, 18045, 4592 },   -- 20-39
      { 13932, 20452, 13927, 250065, 13928, 8364, 274976, 250069 },   -- 40-49
      { 13932, 20452, 13927, 250065, 13928, 8364, 274976, 250069 },   -- 50-59
      { 13932, 20452, 13927, 250065, 13928, 8364, 274976, 250069 },   -- 60
    },
    ["potion-heal"] = {
      { 929, 858, 247240, 2456, 5205, 118, 247239, 255663 },   -- 1-19
      { 3928, 9144, 17349, 1710, 247242, 929, 247241, 858 },   -- 20-39
      { 13446, 17348, 3928, 9144, 17349, 17747, 1710, 247242 },   -- 40-49
      { 18253, 13446, 17348, 3928, 9144, 17349, 17747, 1710 },   -- 50-59
      { 18253, 13446, 17348, 3928, 9144, 241650, 17349, 17747 },   -- 60
    },
    ["potion-mana"] = {
      { 3385, 2455, 2456, 255663 },   -- 1-19
      { 6149, 9144, 17352, 3827, 3385, 2455, 2456, 255663 },   -- 20-39
      { 13444, 18841, 17351, 6149, 9144, 17352, 17747, 3827 },   -- 40-49
      { 13444, 18253, 18841, 17351, 6149, 9144, 17352, 17747 },   -- 50-59
      { 13444, 18253, 18841, 17351, 6149, 9144, 17352, 17747 },   -- 60
    },
    ["potion-rage"] = {
      { 5631 },   -- 1-19
      { 5633, 5631 },   -- 20-39
      { 13442, 5633, 5631 },   -- 40-49
      { 13442, 5633, 5631 },   -- 50-59
      { 13442, 5633, 5631 },   -- 60
    },
    ["potion-buff"] = {
      { 250938 },   -- 1-19
      { 250941, 250940, 250938 },   -- 20-39
      { 250942, 250941, 250940, 250938 },   -- 40-49
      { 250943, 250942, 250941, 250940, 250938 },   -- 50-59
      { 250943, 250942, 250941, 250940, 250938 },   -- 60
    },
    ["potion-protection"] = {
      { 6048, 6051, 3384 },   -- 1-19
      { 6052, 6050, 6049, 4623, 6048, 9036, 6051, 3384 },   -- 20-39
      { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049 },   -- 40-49
      { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049 },   -- 50-59
      { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049 },   -- 60
    },
    ["potion-utility"] = {
      { 2459 },   -- 1-19
      { 5634, 2459 },   -- 20-39
      { 3387, 5634, 2459 },   -- 40-49
      { 3387, 5634, 2459 },   -- 50-59
      { 3387, 5634, 2459 },   -- 60
    },
    ["elixir"] = {
      { 2454, 3390, 2457 },   -- 1-19
      { 9187, 3391, 8949, 2454, 3390, 2457 },   -- 20-39
      { 13452, 9206, 13453, 9187, 12820, 3391, 8949, 2454 },   -- 40-49
      { 250351, 13452, 9206, 13453, 9187, 12820, 3391, 8949 },   -- 50-59
      { 250351, 13452, 9206, 13453, 9187, 12820, 3391, 8949 },   -- 60
    },
    ["scroll"] = {
      { 954, 3012 },   -- 1-19
      { 2289, 954, 1477, 3012 },   -- 20-39
      { 4426, 2289, 4425, 954, 1477, 3012 },   -- 40-49
      { 10310, 4426, 2289, 10309, 4425, 954, 1477, 3012 },   -- 50-59
      { 10310, 4426, 2289, 10309, 4425, 954, 1477, 3012 },   -- 60
    },
    ["weapon"] = {
      { 2871, 3241, 2863, 3240, 2862, 3239 },   -- 1-19
      { 12643, 12404, 7964, 7965, 2871, 3241, 2863, 3240 },   -- 20-39
      { 12643, 12404, 7964, 7965, 2871, 3241, 2863, 3240 },   -- 40-49
      { 12643, 12404, 7964, 7965, 2871, 3241, 2863, 3240 },   -- 50-59
      { 12643, 12404, 7964, 7965, 2871, 3241, 2863, 3240 },   -- 60
    },
  },
  agility = {
    ["food"] = {
      { 4592, 3664, 787, 3662, 2685, 3220, 6290, 2680 },   -- 1-19
      { 13932, 13927, 18045, 13928, 8364, 3728, 250065, 4592 },   -- 20-39
      { 13932, 250069, 13927, 18045, 13928, 8364, 3728, 20452 },   -- 40-49
      { 13932, 250069, 13927, 18045, 13928, 8364, 3728, 20452 },   -- 50-59
      { 13932, 250069, 13927, 18045, 13928, 8364, 3728, 20452 },   -- 60
    },
    ["potion-heal"] = {
      { 929, 858, 247240, 2456, 5205, 118, 247239, 255663 },   -- 1-19
      { 3928, 9144, 17349, 1710, 247242, 929, 247241, 858 },   -- 20-39
      { 13446, 17348, 3928, 9144, 17349, 17747, 1710, 247242 },   -- 40-49
      { 18253, 13446, 17348, 3928, 9144, 17349, 17747, 1710 },   -- 50-59
      { 18253, 13446, 17348, 3928, 9144, 241650, 17349, 17747 },   -- 60
    },
    ["potion-mana"] = {
      { 3385, 2455, 2456, 255663 },   -- 1-19
      { 6149, 9144, 17352, 3827, 3385, 2455, 2456, 255663 },   -- 20-39
      { 13444, 18841, 17351, 6149, 9144, 17352, 17747, 3827 },   -- 40-49
      { 13444, 18253, 18841, 17351, 6149, 9144, 17352, 17747 },   -- 50-59
      { 13444, 18253, 18841, 17351, 6149, 9144, 17352, 17747 },   -- 60
    },
    ["potion-rage"] = {
      { 5631 },   -- 1-19
      { 5633, 5631 },   -- 20-39
      { 13442, 5633, 5631 },   -- 40-49
      { 13442, 5633, 5631 },   -- 50-59
      { 13442, 5633, 5631 },   -- 60
    },
    ["potion-buff"] = {
      { 250938 },   -- 1-19
      { 250941, 250940, 250938 },   -- 20-39
      { 250942, 250941, 250940, 250938 },   -- 40-49
      { 250943, 250942, 250941, 250940, 250938 },   -- 50-59
      { 250943, 250942, 250941, 250940, 250938 },   -- 60
    },
    ["potion-protection"] = {
      { 6048, 6051, 3384 },   -- 1-19
      { 6052, 6050, 6049, 4623, 6048, 9036, 6051, 3384 },   -- 20-39
      { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049 },   -- 40-49
      { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049 },   -- 50-59
      { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049 },   -- 60
    },
    ["potion-utility"] = {
      { 2459 },   -- 1-19
      { 5634, 2459 },   -- 20-39
      { 3387, 5634, 2459 },   -- 40-49
      { 3387, 5634, 2459 },   -- 50-59
      { 3387, 5634, 2459 },   -- 60
    },
    ["elixir"] = {
      { 3390, 2457, 2454 },   -- 1-19
      { 9187, 8949, 3390, 3391, 2457, 2454 },   -- 20-39
      { 13452, 9187, 8949, 13453, 12820, 3390, 3391, 2457 },   -- 40-49
      { 13452, 250351, 9187, 8949, 13453, 12820, 3390, 3391 },   -- 50-59
      { 13452, 250351, 9187, 8949, 13453, 12820, 3390, 3391 },   -- 60
    },
    ["scroll"] = {
      { 3012, 954 },   -- 1-19
      { 1477, 3012, 2289, 954 },   -- 20-39
      { 4425, 1477, 4426, 3012, 2289, 954 },   -- 40-49
      { 10309, 4425, 1477, 10310, 4426, 3012, 2289, 954 },   -- 50-59
      { 10309, 4425, 1477, 10310, 4426, 3012, 2289, 954 },   -- 60
    },
    ["weapon"] = {
      { 2871, 3241, 2863, 3240, 2862, 3239 },   -- 1-19
      { 12643, 12404, 7964, 7965, 2871, 3241, 2863, 3240 },   -- 20-39
      { 12643, 12404, 7964, 7965, 2871, 3241, 2863, 3240 },   -- 40-49
      { 12643, 12404, 7964, 7965, 2871, 3241, 2863, 3240 },   -- 50-59
      { 12643, 12404, 7964, 7965, 2871, 3241, 2863, 3240 },   -- 60
    },
  },
  tank = {
    ["food"] = {
      { 3666, 250080, 3664, 2685, 4592, 3662, 5472, 3220 },   -- 1-19
      { 17222, 6038, 286152, 18045, 3666, 13932, 250065, 3728 },   -- 20-39
      { 250068, 17222, 6038, 250069, 20452, 286152, 18045, 3666 },   -- 40-49
      { 250068, 17222, 6038, 250069, 20452, 286152, 18045, 3666 },   -- 50-59
      { 250068, 17222, 16971, 6038, 250069, 20452, 5527, 286152 },   -- 60
    },
    ["potion-heal"] = {
      { 929, 858, 247240, 2456, 5205, 118, 247239, 255663 },   -- 1-19
      { 3928, 9144, 17349, 1710, 247242, 929, 247241, 858 },   -- 20-39
      { 13446, 17348, 3928, 9144, 17349, 17747, 1710, 247242 },   -- 40-49
      { 18253, 13446, 17348, 3928, 9144, 17349, 17747, 1710 },   -- 50-59
      { 18253, 13446, 17348, 3928, 9144, 241650, 17349, 17747 },   -- 60
    },
    ["potion-mana"] = {
      { 3385, 2455, 2456, 255663 },   -- 1-19
      { 6149, 9144, 17352, 3827, 3385, 2455, 2456, 255663 },   -- 20-39
      { 13444, 18841, 17351, 6149, 9144, 17352, 17747, 3827 },   -- 40-49
      { 13444, 18253, 18841, 17351, 6149, 9144, 17352, 17747 },   -- 50-59
      { 13444, 18253, 18841, 17351, 6149, 9144, 17352, 17747 },   -- 60
    },
    ["potion-rage"] = {
      { 5631 },   -- 1-19
      { 5633, 5631 },   -- 20-39
      { 13442, 5633, 5631 },   -- 40-49
      { 13442, 5633, 5631 },   -- 50-59
      { 13442, 5633, 5631 },   -- 60
    },
    ["potion-buff"] = {
      { 250938 },   -- 1-19
      { 250941, 250940, 250938 },   -- 20-39
      { 250942, 250941, 250940, 250938 },   -- 40-49
      { 250943, 250942, 250941, 250940, 250938 },   -- 50-59
      { 250943, 250942, 250941, 250940, 250938 },   -- 60
    },
    ["potion-protection"] = {
      { 6048, 6051, 3384 },   -- 1-19
      { 6052, 6050, 6049, 4623, 6048, 9036, 6051, 3384 },   -- 20-39
      { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049 },   -- 40-49
      { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049 },   -- 50-59
      { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049 },   -- 60
    },
    ["potion-utility"] = {
      { 2459 },   -- 1-19
      { 5634, 2459 },   -- 20-39
      { 3387, 5634, 2459 },   -- 40-49
      { 3387, 5634, 2459 },   -- 50-59
      { 3387, 5634, 2459 },   -- 60
    },
    ["elixir"] = {
      { 3389, 3388, 3390, 2458, 5997, 2457, 2454, 3382 },   -- 1-19
      { 3825, 8951, 9187, 3826, 3389, 8949, 3388, 3390 },   -- 20-39
      { 13453, 250334, 13445, 13452, 3825, 8951, 9187, 3826 },   -- 40-49
      { 250329, 13453, 250334, 13445, 13452, 3825, 8951, 9187 },   -- 50-59
      { 250329, 13453, 250334, 13445, 13452, 3825, 8951, 9187 },   -- 60
    },
    ["flask"] = {
      {},   -- 1-19
      {},   -- 20-39
      {},   -- 40-49
      { 13510, 13513 },   -- 50-59
      { 13510, 13513 },   -- 60
    },
    ["scroll"] = {
      { 1478, 1180, 3013, 3012, 954 },   -- 1-19
      { 4422, 1711, 4421, 1478, 1180, 1477, 2289, 3013 },   -- 20-39
      { 4422, 10305, 1711, 4421, 4425, 1478, 4426, 1180 },   -- 40-49
      { 10307, 4422, 10305, 1711, 4421, 10309, 10310, 4425 },   -- 50-59
      { 10307, 4422, 10305, 1711, 4421, 10309, 10310, 4425 },   -- 60
    },
    ["weapon"] = {
      { 2871, 3241, 2863, 3240, 2862, 3239 },   -- 1-19
      { 12643, 12404, 7964, 7965, 2871, 3241, 2863, 3240 },   -- 20-39
      { 12643, 12404, 7964, 7965, 2871, 3241, 2863, 3240 },   -- 40-49
      { 12643, 12404, 7964, 7965, 2871, 3241, 2863, 3240 },   -- 50-59
      { 12643, 12404, 7964, 7965, 2871, 3241, 2863, 3240 },   -- 60
    },
  },
  caster = {
    ["food"] = {
      { 21072, 12210, 1082, 5095, 249873, 2682, 249872, 249871 },   -- 1-19
      { 6887, 274971, 13928, 18254, 21217, 4457, 21072, 12210 },   -- 20-39
      { 13933, 6887, 274971, 13928, 250067, 18254, 21217, 4457 },   -- 40-49
      { 13933, 6887, 274971, 13928, 250067, 18254, 21217, 4457 },   -- 50-59
      { 13933, 6887, 274971, 13928, 250067, 18254, 21217, 4457 },   -- 60
    },
    ["potion-heal"] = {
      { 929, 858, 247240, 2456, 5205, 118, 247239, 255663 },   -- 1-19
      { 9144, 3928, 17349, 1710, 247242, 929, 247241, 858 },   -- 20-39
      { 13446, 17348, 9144, 3928, 17747, 17349, 1710, 247242 },   -- 40-49
      { 18253, 13446, 17348, 9144, 3928, 17747, 17349, 1710 },   -- 50-59
      { 18253, 13446, 17348, 9144, 3928, 17747, 241650, 17349 },   -- 60
    },
    ["potion-mana"] = {
      { 3385, 2455, 2456, 255663 },   -- 1-19
      { 6149, 9144, 17352, 3827, 3385, 2455, 2456, 255663 },   -- 20-39
      { 13444, 18841, 17351, 6149, 9144, 17352, 17747, 3827 },   -- 40-49
      { 13444, 18253, 18841, 17351, 6149, 9144, 17352, 17747 },   -- 50-59
      { 13444, 18253, 18841, 17351, 6149, 9144, 17352, 17747 },   -- 60
    },
    ["potion-buff"] = {
      { 250933, 250932 },   -- 1-19
      { 250935, 250934, 250933, 250932 },   -- 20-39
      { 250936, 250935, 250934, 250933, 250932 },   -- 40-49
      { 250937, 250936, 250935, 250934, 250933, 250932 },   -- 50-59
      { 250937, 250936, 250935, 250934, 250933, 250932 },   -- 60
    },
    ["potion-protection"] = {
      { 6048, 6051, 3384 },   -- 1-19
      { 6052, 6050, 6049, 4623, 6048, 9036, 6051, 3384 },   -- 20-39
      { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049 },   -- 40-49
      { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049 },   -- 50-59
      { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049 },   -- 60
    },
    ["potion-utility"] = {
      { 2459 },   -- 1-19
      { 5634, 2459 },   -- 20-39
      { 3387, 5634, 2459 },   -- 40-49
      { 3387, 5634, 2459 },   -- 50-59
      { 3387, 5634, 2459 },   -- 60
    },
    ["elixir"] = {
      { 6373, 247754, 250339, 3383, 250344 },   -- 1-19
      { 9155, 250342, 9179, 250340, 6373, 247754, 250339, 250353 },   -- 20-39
      { 13454, 9264, 9155, 250342, 9179, 21546, 250340, 6373 },   -- 40-49
      { 13454, 250337, 250341, 9264, 250348, 9155, 250343, 250342 },   -- 50-59
      { 13454, 250337, 250341, 9264, 250348, 9155, 250343, 250342 },   -- 60
    },
    ["flask"] = {
      {},   -- 1-19
      {},   -- 20-39
      {},   -- 40-49
      { 13512, 13511 },   -- 50-59
      { 13512, 13511 },   -- 60
    },
    ["scroll"] = {
      { 277483, 955, 1712, 1181 },   -- 1-19
      { 4419, 277493, 2290, 277483, 955, 4424, 1712, 1181 },   -- 20-39
      { 4419, 277493, 2290, 277483, 10306, 955, 4424, 1712 },   -- 40-49
      { 10308, 4419, 277493, 2290, 277483, 10306, 955, 4424 },   -- 50-59
      { 10308, 4419, 277493, 2290, 277483, 10306, 955, 4424 },   -- 60
    },
    ["weapon"] = {
      { 20744 },   -- 1-19
      { 20746, 20744 },   -- 20-39
      { 20750, 20746, 20744 },   -- 40-49
      { 20750, 20746, 20744 },   -- 50-59
      { 20750, 20746, 20744 },   -- 60
    },
  },
  healer = {
    ["food"] = {
      { 249867, 249866, 12210, 249873, 249865, 1082, 249872, 2682 },   -- 1-19
      { 249869, 249868, 249867, 18254, 249875, 249866, 4457, 249874 },   -- 20-39
      { 249870, 249869, 249868, 250067, 249867, 249876, 18254, 249875 },   -- 40-49
      { 249870, 249869, 249868, 250067, 249867, 249876, 18254, 249875 },   -- 50-59
      { 249870, 249869, 249868, 250067, 249867, 249876, 18254, 16971 },   -- 60
    },
    ["potion-heal"] = {
      { 929, 858, 247240, 2456, 5205, 118, 247239, 255663 },   -- 1-19
      { 9144, 3928, 17349, 1710, 247242, 929, 247241, 858 },   -- 20-39
      { 13446, 17348, 9144, 3928, 17747, 17349, 1710, 247242 },   -- 40-49
      { 18253, 13446, 17348, 9144, 3928, 17747, 17349, 1710 },   -- 50-59
      { 18253, 13446, 17348, 9144, 3928, 17747, 241650, 17349 },   -- 60
    },
    ["potion-mana"] = {
      { 3385, 2455, 2456, 255663 },   -- 1-19
      { 6149, 9144, 17352, 3827, 3385, 2455, 2456, 255663 },   -- 20-39
      { 13444, 18841, 17351, 6149, 9144, 17352, 17747, 3827 },   -- 40-49
      { 13444, 18253, 18841, 17351, 6149, 9144, 17352, 17747 },   -- 50-59
      { 13444, 18253, 18841, 17351, 6149, 9144, 17352, 17747 },   -- 60
    },
    ["potion-buff"] = {
      { 250945, 250944 },   -- 1-19
      { 250947, 250946, 250945, 250944 },   -- 20-39
      { 250948, 250947, 250946, 250945, 250944 },   -- 40-49
      { 250949, 250948, 250947, 250946, 250945, 250944 },   -- 50-59
      { 250949, 250948, 250947, 250946, 250945, 250944 },   -- 60
    },
    ["potion-protection"] = {
      { 6048, 6051, 3384 },   -- 1-19
      { 6052, 6050, 6049, 4623, 6048, 9036, 6051, 3384 },   -- 20-39
      { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049 },   -- 40-49
      { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049 },   -- 50-59
      { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049 },   -- 60
    },
    ["potion-utility"] = {
      { 2459 },   -- 1-19
      { 5634, 2459 },   -- 20-39
      { 3387, 5634, 2459 },   -- 40-49
      { 3387, 5634, 2459 },   -- 50-59
      { 3387, 5634, 2459 },   -- 60
    },
    ["elixir"] = {
      { 250330, 250339, 3383, 250344 },   -- 1-19
      { 250331, 13447, 250340, 9179, 250330, 250339, 250353, 250345 },   -- 20-39
      { 250332, 250331, 13447, 250340, 9179, 250330, 250339, 250353 },   -- 40-49
      { 250341, 250333, 250332, 250337, 250348, 250331, 13447, 250340 },   -- 50-59
      { 250341, 250333, 250332, 250337, 250348, 250331, 13447, 250340 },   -- 60
    },
    ["flask"] = {
      {},   -- 1-19
      {},   -- 20-39
      {},   -- 40-49
      { 13511 },   -- 50-59
      { 13511 },   -- 60
    },
    ["scroll"] = {
      { 277483, 1712, 955, 1181 },   -- 1-19
      { 4419, 277493, 4424, 2290, 277483, 1712, 955, 1181 },   -- 20-39
      { 10306, 4419, 277493, 4424, 2290, 277483, 1712, 955 },   -- 40-49
      { 10308, 10306, 4419, 277493, 4424, 2290, 277483, 1712 },   -- 50-59
      { 10308, 10306, 4419, 277493, 4424, 2290, 277483, 1712 },   -- 60
    },
    ["weapon"] = {
      { 20744 },   -- 1-19
      { 20745, 20744 },   -- 20-39
      { 20747, 20745, 20744 },   -- 40-49
      { 20747, 20745, 20744 },   -- 50-59
      { 20747, 20745, 20744 },   -- 60
    },
  },
  hunter = {
    ["food"] = {
      { 4592, 3664, 787, 3662, 6290, 2680 },   -- 1-19
      { 13932, 13927, 18045, 13928, 8364, 3728, 4592, 3664 },   -- 20-39
      { 13932, 250069, 13927, 18045, 13928, 8364, 3728, 4592 },   -- 40-49
      { 13932, 250069, 13927, 18045, 13928, 8364, 3728, 4592 },   -- 50-59
      { 13932, 250069, 13927, 18045, 13928, 8364, 3728, 4592 },   -- 60
    },
    ["potion-heal"] = {
      { 929, 858, 247240, 2456, 5205, 118, 247239, 255663 },   -- 1-19
      { 9144, 3928, 17349, 1710, 247242, 929, 247241, 858 },   -- 20-39
      { 13446, 17348, 9144, 3928, 17747, 17349, 1710, 247242 },   -- 40-49
      { 18253, 13446, 17348, 9144, 3928, 17747, 17349, 1710 },   -- 50-59
      { 18253, 13446, 17348, 9144, 3928, 17747, 241650, 17349 },   -- 60
    },
    ["potion-mana"] = {
      { 3385, 2455, 2456, 255663 },   -- 1-19
      { 6149, 9144, 17352, 3827, 3385, 2455, 2456, 255663 },   -- 20-39
      { 13444, 18841, 17351, 6149, 9144, 17352, 17747, 3827 },   -- 40-49
      { 13444, 18253, 18841, 17351, 6149, 9144, 17352, 17747 },   -- 50-59
      { 13444, 18253, 18841, 17351, 6149, 9144, 17352, 17747 },   -- 60
    },
    ["potion-buff"] = {
      { 250938 },   -- 1-19
      { 250941, 250940, 250938 },   -- 20-39
      { 250942, 250941, 250940, 250938 },   -- 40-49
      { 250943, 250942, 250941, 250940, 250938 },   -- 50-59
      { 250943, 250942, 250941, 250940, 250938 },   -- 60
    },
    ["potion-protection"] = {
      { 6048, 6051, 3384 },   -- 1-19
      { 6052, 6050, 6049, 4623, 6048, 9036, 6051, 3384 },   -- 20-39
      { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049 },   -- 40-49
      { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049 },   -- 50-59
      { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049 },   -- 60
    },
    ["potion-utility"] = {
      { 2459 },   -- 1-19
      { 5634, 2459 },   -- 20-39
      { 3387, 5634, 2459 },   -- 40-49
      { 3387, 5634, 2459 },   -- 50-59
      { 3387, 5634, 2459 },   -- 60
    },
    ["elixir"] = {
      { 3390, 2457 },   -- 1-19
      { 9187, 8949, 3390, 2457 },   -- 20-39
      { 13452, 9187, 8949, 3390, 2457 },   -- 40-49
      { 13452, 9187, 8949, 3390, 2457 },   -- 50-59
      { 13452, 9187, 8949, 3390, 2457 },   -- 60
    },
    ["flask"] = {
      {},   -- 1-19
      {},   -- 20-39
      {},   -- 40-49
      { 13511 },   -- 50-59
      { 13511 },   -- 60
    },
    ["scroll"] = {
      { 3012 },   -- 1-19
      { 1477, 3012 },   -- 20-39
      { 4425, 1477, 3012 },   -- 40-49
      { 10309, 4425, 1477, 3012 },   -- 50-59
      { 10309, 4425, 1477, 3012 },   -- 60
    },
  },
}

-- Every usable item of a list for a role, best first (the All switch); the level is checked in game.
local ALL = {
  melee = {
    ["food"] = {
      13932, 20452, 250070, 250071, 13934, 13927, 250065, 13928, 8364, 274976, 250074, 250069, 250072, 18045, 12212, 4592,
      2685, 3728, 13851, 5480, 20074, 787, 3220, 2687, 724, 3726, 3664, 1017, 12209, 3727, 3662, 2684,
      6290, 2681, 6890, 2888, 2680,
    },
    ["potion-heal"] = {
      18253, 13446, 17348, 3928, 18839, 9144, 241650, 17349, 17747, 1710, 247242, 929, 247241, 858, 247240, 2456,
      5205, 118, 247239, 255663, 4596,
    },
    ["potion-mana"] = { 13444, 18253, 18841, 13443, 17351, 6149, 9144, 17352, 17747, 3827, 3385, 2455, 2456, 255663 },
    ["potion-rage"] = { 13442, 5633, 5631 },
    ["potion-buff"] = { 250943, 250942, 250941, 250940, 250938 },
    ["potion-protection"] = { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049, 4623, 6048, 9036, 6051, 3384 },
    ["potion-utility"] = { 3387, 5634, 2459 },
    ["elixir"] = { 250351, 13452, 250350, 9206, 13453, 9187, 250328, 12820, 246948, 250349, 3391, 8949, 2454, 3390, 2457, 6662 },
    ["scroll"] = { 10310, 4426, 2289, 10309, 4425, 954, 1477, 3012 },
    ["weapon"] = { 12643, 12404, 7964, 7965, 2871, 3241, 2863, 3240, 2862, 3239 },
  },
  agility = {
    ["food"] = {
      13932, 250069, 13934, 250072, 13927, 18045, 12212, 13928, 8364, 3728, 13851, 20074, 20452, 250070, 250071, 250065,
      4592, 3664, 1017, 274976, 12209, 3727, 250074, 787, 3662, 2684, 2685, 5480, 3220, 2687, 724, 3726,
      6290, 2680, 2681, 6890, 2888,
    },
    ["potion-heal"] = {
      18253, 13446, 17348, 3928, 18839, 9144, 241650, 17349, 17747, 1710, 247242, 929, 247241, 858, 247240, 2456,
      5205, 118, 247239, 255663, 4596,
    },
    ["potion-mana"] = { 13444, 18253, 18841, 13443, 17351, 6149, 9144, 17352, 17747, 3827, 3385, 2455, 2456, 255663 },
    ["potion-rage"] = { 13442, 5633, 5631 },
    ["potion-buff"] = { 250943, 250942, 250941, 250940, 250938 },
    ["potion-protection"] = { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049, 4623, 6048, 9036, 6051, 3384 },
    ["potion-utility"] = { 3387, 5634, 2459 },
    ["elixir"] = { 13452, 250351, 250350, 9187, 250328, 8949, 13453, 9206, 12820, 246948, 3390, 250349, 3391, 2457, 2454, 6662 },
    ["scroll"] = { 10309, 4425, 1477, 10310, 4426, 3012, 2289, 954 },
    ["weapon"] = { 12643, 12404, 7964, 7965, 2871, 3241, 2863, 3240, 2862, 3239 },
  },
  tank = {
    ["food"] = {
      250068, 17222, 12218, 6038, 12215, 250069, 250072, 20452, 250070, 250071, 286152, 18045, 12212, 3666, 3665, 3729,
      12214, 13932, 250065, 13934, 3728, 13851, 20074, 13927, 274976, 250074, 250080, 12213, 5479, 8364, 3664, 1017,
      12209, 3727, 2685, 5480, 4592, 3662, 2684, 5472, 6888, 3663, 263509, 3220, 2687, 724, 3726, 787,
      2680, 2681, 6890, 2888, 6290, 16971, 5527,
    },
    ["potion-heal"] = {
      18253, 13446, 17348, 3928, 18839, 9144, 241650, 17349, 17747, 1710, 247242, 929, 247241, 858, 247240, 2456,
      5205, 118, 247239, 255663, 4596,
    },
    ["potion-mana"] = { 13444, 18253, 18841, 13443, 17351, 6149, 9144, 17352, 17747, 3827, 3385, 2455, 2456, 255663 },
    ["potion-rage"] = { 13442, 5633, 5631 },
    ["potion-buff"] = { 250943, 250942, 250941, 250940, 250938 },
    ["potion-protection"] = { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049, 4623, 6048, 9036, 6051, 3384 },
    ["potion-utility"] = { 3387, 5634, 2459 },
    ["elixir"] = {
      250329, 250335, 13453, 250334, 13445, 13452, 250350, 3825, 250351, 8951, 9187, 250328, 9206, 3826, 3389, 8949,
      250336, 3388, 3390, 250349, 12820, 246948, 2458, 3391, 5997, 2457, 2454, 3382, 6662,
    },
    ["flask"] = { 13510, 13513 },
    ["scroll"] = { 10307, 4422, 10305, 1711, 4421, 10309, 10310, 4425, 1478, 4426, 1180, 1477, 2289, 3013, 3012, 954 },
    ["weapon"] = { 12643, 12404, 7964, 7965, 2871, 3241, 2863, 3240, 2862, 3239 },
  },
  caster = {
    ["food"] = {
      13933, 13935, 6887, 13931, 274971, 13928, 250067, 250075, 18254, 250076, 21217, 4457, 12216, 250073, 21072, 249876,
      12210, 249875, 1082, 2683, 250077, 249874, 5095, 249873, 2682, 5477, 250079, 263512, 12224, 249872, 249871, 16971,
      5527,
    },
    ["potion-heal"] = {
      18253, 13446, 17348, 9144, 3928, 18839, 17747, 241650, 17349, 1710, 247242, 929, 247241, 858, 247240, 2456,
      5205, 118, 247239, 255663, 4596,
    },
    ["potion-mana"] = { 13444, 18253, 18841, 13443, 17351, 6149, 9144, 17352, 17747, 3827, 3385, 2455, 2456, 255663 },
    ["potion-buff"] = { 250937, 250936, 250935, 250934, 250933, 250932 },
    ["potion-protection"] = { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049, 4623, 6048, 9036, 6051, 3384 },
    ["potion-utility"] = { 3387, 5634, 2459 },
    ["elixir"] = {
      13454, 250337, 250341, 9264, 250348, 250338, 9155, 250343, 250342, 250336, 9179, 250328, 21546, 13447, 250340, 6373,
      247754, 250354, 250339, 250353, 250347, 3383, 250346, 250345, 250344,
    },
    ["flask"] = { 13512, 13511 },
    ["scroll"] = { 10308, 4419, 277493, 2290, 277483, 10306, 955, 4424, 1712, 275069, 1181 },
    ["weapon"] = { 20750, 20746, 20747, 20744, 20745 },
  },
  healer = {
    ["food"] = {
      249870, 249869, 249868, 250067, 250075, 249867, 249876, 18254, 250076, 249875, 249866, 4457, 12216, 250073, 249874, 12210,
      249873, 249865, 1082, 2683, 250077, 249872, 2682, 5477, 250079, 263512, 12224, 249871, 16971, 5527,
    },
    ["potion-heal"] = {
      18253, 13446, 17348, 9144, 3928, 18839, 17747, 241650, 17349, 1710, 247242, 929, 247241, 858, 247240, 2456,
      5205, 118, 247239, 255663, 4596,
    },
    ["potion-mana"] = { 13444, 18253, 18841, 13443, 17351, 6149, 9144, 17352, 17747, 3827, 3385, 2455, 2456, 255663 },
    ["potion-buff"] = { 250949, 250948, 250947, 250946, 250945, 250944 },
    ["potion-protection"] = { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049, 4623, 6048, 9036, 6051, 3384 },
    ["potion-utility"] = { 3387, 5634, 2459 },
    ["elixir"] = {
      250341, 250333, 250332, 250337, 250336, 250348, 250338, 250331, 13447, 250340, 9179, 250328, 250330, 250347, 250339, 250354,
      250346, 250353, 250345, 3383, 250344,
    },
    ["flask"] = { 13511 },
    ["scroll"] = { 10308, 10306, 4419, 277493, 4424, 2290, 277483, 1712, 955, 1181, 275069 },
    ["weapon"] = { 20747, 20750, 20745, 20746, 20744 },
  },
  hunter = {
    ["food"] = {
      13932, 13934, 250069, 250072, 13927, 18045, 12212, 13928, 8364, 3728, 13851, 20074, 4592, 3664, 1017, 12209,
      3727, 787, 3662, 2684, 6290, 2680,
    },
    ["potion-heal"] = {
      18253, 13446, 17348, 9144, 3928, 18839, 17747, 241650, 17349, 1710, 247242, 929, 247241, 858, 247240, 2456,
      5205, 118, 247239, 255663, 4596,
    },
    ["potion-mana"] = { 13444, 18253, 18841, 13443, 17351, 6149, 9144, 17352, 17747, 3827, 3385, 2455, 2456, 255663 },
    ["potion-buff"] = { 250943, 250942, 250941, 250940, 250938 },
    ["potion-protection"] = { 13461, 13456, 13457, 13459, 13455, 6052, 6050, 6049, 4623, 6048, 9036, 6051, 3384 },
    ["potion-utility"] = { 3387, 5634, 2459 },
    ["elixir"] = { 13452, 250328, 9187, 250350, 8949, 3390, 2457 },
    ["flask"] = { 13511 },
    ["scroll"] = { 10309, 4425, 1477, 3012 },
  },
}

-- Each item's strength for the role, in the order of ALL: the ranking's own score (the role's stat weights
-- times the buff, times the duration's share of 30 min for elixirs, flasks and scrolls; a healing potion
-- what it heals (Discolored: net of the 75% that comes back; mana users: +30% of its mana), a mana potion
-- its mana, a rage potion 10 a point of rage plus its buff, a protection potion what it absorbs + 10 a
-- point of resistance + its armor). The panel's '% of best' and its Budget picks read these.
local SCORES = {
  melee = {
    ["food"] = {
      40, 40, 40, 40, 40, 30, 30, 25, 20, 20, 20, 20, 20, 15, 15, 10,
      10, 10, 10, 10, 10, 6, 6, 6, 6, 6, 5, 5, 5, 5, 3, 3,
      2, 2, 2, 2, 1,
    },
    ["potion-heal"] = {
      1600, 1400, 1120, 800, 800, 750, 700, 640, 600, 520, 400, 320, 260, 160, 160, 120,
      94, 80, 80, 60, 40,
    },
    ["potion-mana"] = { 1800, 1600, 1200, 1200, 1120, 800, 750, 640, 600, 520, 320, 160, 120, 40 },
    ["potion-rage"] = { 720, 450, 300 },
    ["potion-buff"] = { 80, 56, 40, 28, 12 },
    ["potion-protection"] = { 2600, 2600, 2600, 2600, 2000, 1800, 1800, 1300, 1000, 900, 500, 400, 250 },
    ["potion-utility"] = { 1.45, 1.2, 1.05 },
    ["elixir"] = { 100, 75, 54, 50, 43.2, 25, 25, 23.33, 23.33, 20, 16, 15, 8, 8, 4, 1.07 },
    ["scroll"] = { 34, 26, 18, 17, 13, 10, 9, 5 },
    ["weapon"] = { 40, 40, 30, 30, 20, 20, 15, 15, 10, 10 },
  },
  agility = {
    ["food"] = {
      40, 40, 40, 40, 30, 30, 30, 25, 20, 20, 20, 20, 20, 20, 20, 15,
      10, 10, 10, 10, 10, 10, 10, 6, 6, 6, 5, 5, 3, 3, 3, 3,
      2, 2, 1, 1, 1,
    },
    ["potion-heal"] = {
      1600, 1400, 1120, 800, 800, 750, 700, 640, 600, 520, 400, 320, 260, 160, 160, 120,
      94, 80, 80, 60, 40,
    },
    ["potion-mana"] = { 1800, 1600, 1200, 1200, 1120, 800, 750, 640, 600, 520, 320, 160, 120, 40 },
    ["potion-rage"] = { 660, 450, 300 },
    ["potion-buff"] = { 80, 56, 40, 28, 12 },
    ["potion-protection"] = { 2600, 2600, 2600, 2600, 2000, 1800, 1800, 1300, 1000, 900, 500, 400, 250 },
    ["potion-utility"] = { 1.45, 1.2, 1.05 },
    ["elixir"] = { 100, 75, 54, 50, 50, 30, 25.2, 25, 23.33, 23.33, 16, 10, 8, 8, 4, 0.53 },
    ["scroll"] = { 34, 26, 18, 17, 13, 10, 9, 5 },
    ["weapon"] = { 40, 40, 30, 30, 20, 20, 15, 15, 10, 10 },
  },
  tank = {
    ["food"] = {
      50, 37.5, 37.5, 25, 25, 20, 20, 16, 16, 16, 15, 15, 15, 12.5, 12.5, 12.5,
      12.5, 12, 12, 12, 10, 10, 10, 9, 8, 8, 7.5, 7.5, 7.5, 6, 5, 5,
      5, 5, 4, 4, 3, 3, 3, 2.5, 2.5, 2.5, 2.5, 2.4, 2.4, 2.4, 2.4, 1.8,
      1, 0.8, 0.8, 0.8, 0.6, 30, 15,
    },
    ["potion-heal"] = {
      1600, 1400, 1120, 800, 800, 750, 700, 640, 600, 520, 400, 320, 260, 160, 160, 120,
      94, 80, 80, 60, 40,
    },
    ["potion-mana"] = { 1800, 1600, 1200, 1200, 1120, 800, 750, 640, 600, 520, 320, 160, 120, 40 },
    ["potion-rage"] = { 648, 450, 300 },
    ["potion-buff"] = { 24, 16.8, 12, 8.4, 3.6 },
    ["potion-protection"] = { 2600, 2600, 2600, 2600, 2000, 1800, 1800, 1300, 1000, 900, 500, 400, 250 },
    ["potion-utility"] = { 1.45, 1.2, 1.05 },
    ["elixir"] = {
      150, 100, 59.4, 50, 45, 33, 32.4, 30, 28, 25, 25, 25, 20, 18, 15, 15,
      13.5, 9, 8, 8, 7, 7, 6.75, 6.4, 5, 4, 3.2, 3, 0.43,
    },
    ["flask"] = { 300, 37.5 },
    ["scroll"] = { 40, 30, 24, 20, 18, 17, 13.6, 13, 12, 10.4, 10, 9, 7.2, 6, 5, 4 },
    ["weapon"] = { 8, 8, 6, 6, 4, 4, 3, 3, 2, 2 },
  },
  caster = {
    ["food"] = {
      28, 28, 22, 22, 14, 10, 10, 10, 7.5, 7.5, 7, 5, 5, 5, 4, 3,
      2.5, 2.25, 1.5, 1.5, 1.5, 1.5, 1, 0.75, 0.5, 0.5, 0.5, 0.5, 0.5, 0.45, 0.15, 4.2,
      2.1,
    },
    ["potion-heal"] = {
      2080, 1400, 1120, 975, 800, 800, 780, 700, 640, 520, 400, 320, 260, 160, 160, 156,
      94, 80, 80, 72, 40,
    },
    ["potion-mana"] = { 1800, 1600, 1200, 1200, 1120, 800, 750, 640, 600, 520, 320, 160, 120, 40 },
    ["potion-buff"] = { 47, 33, 23, 16, 11, 7 },
    ["potion-protection"] = { 2600, 2600, 2600, 2600, 2000, 1800, 1800, 1300, 1000, 900, 500, 400, 250 },
    ["potion-utility"] = { 1.45, 1.2, 1.05 },
    ["elixir"] = {
      35, 32.5, 30, 24, 23.75, 23.75, 20, 20, 15, 15, 12.5, 12.5, 12, 11.7, 9, 6,
      5, 5, 4.5, 3, 2.7, 1.5, 1.5, 0.9, 0.45,
    },
    ["flask"] = { 150, 66 },
    ["scroll"] = { 8, 6, 6, 4, 3, 2.25, 2, 1.65, 1.05, 1, 0.45 },
    ["weapon"] = { 24, 16, 15, 8, 7.5 },
  },
  healer = {
    ["food"] = {
      44, 33, 22, 12, 12, 11, 10, 9, 9, 7.5, 7, 6, 6, 6, 5, 3,
      2.5, 2, 1.8, 1.8, 1.8, 1.5, 0.6, 0.6, 0.6, 0.6, 0.6, 0.5, 8.4, 4.2,
    },
    ["potion-heal"] = {
      2080, 1400, 1120, 975, 800, 800, 780, 700, 640, 520, 400, 320, 260, 160, 160, 156,
      94, 80, 80, 72, 40,
    },
    ["potion-mana"] = { 1800, 1600, 1200, 1200, 1120, 800, 750, 640, 600, 520, 320, 160, 120, 40 },
    ["potion-buff"] = { 88, 62, 44, 31, 20, 13 },
    ["potion-protection"] = { 2600, 2600, 2600, 2600, 2000, 1800, 1800, 1300, 1000, 900, 500, 400, 250 },
    ["potion-utility"] = { 1.45, 1.2, 1.05 },
    ["elixir"] = {
      50, 40, 30, 27, 25, 24.5, 24.5, 20, 19.8, 15, 15, 15, 10, 9, 7.5, 6,
      5, 3.6, 3, 1.8, 1.5,
    },
    ["flask"] = { 80 },
    ["scroll"] = { 9.6, 7.5, 7.2, 7.2, 5.5, 4.8, 3.6, 3.5, 2.4, 1.5, 1.2 },
    ["weapon"] = { 45, 24, 22.5, 16, 8 },
  },
  hunter = {
    ["food"] = {
      48, 48, 40, 40, 36, 30, 30, 25, 24, 20, 20, 20, 12, 10, 10, 10,
      10, 7.2, 6, 6, 2.4, 2,
    },
    ["potion-heal"] = {
      2080, 1400, 1120, 975, 800, 800, 780, 700, 640, 520, 400, 320, 260, 160, 160, 156,
      94, 80, 80, 72, 40,
    },
    ["potion-mana"] = { 1800, 1600, 1200, 1200, 1120, 800, 750, 640, 600, 520, 320, 160, 120, 40 },
    ["potion-buff"] = { 96, 67.2, 48, 33.6, 14.4 },
    ["potion-protection"] = { 2600, 2600, 2600, 2600, 2000, 1800, 1800, 1300, 1000, 900, 500, 400, 250 },
    ["potion-utility"] = { 1.45, 1.2, 1.05 },
    ["elixir"] = { 100, 57.5, 50, 36, 30, 16, 8 },
    ["flask"] = { 40 },
    ["scroll"] = { 34, 26, 18, 10 },
  },
}

-- What a protection potion guards against (one is only compared with others of its kind).
local KIND = {
  [3384] = "res", [4623] = "armor", [6048] = "shadow", [6049] = "fire", [6050] = "frost", [6051] = "holy",
  [6052] = "nature", [9036] = "res", [13455] = "armor", [13456] = "frost", [13457] = "fire", [13459] = "shadow",
  [13461] = "arcane",
}

-- Only for PvP: usable only in battlegrounds, or bound and sold only in the PvP halls (Champions' Hall,
-- Hall of Legends), which need a PvP rank. Never a Budget pick.
local PVP = { [17348] = true, [17349] = true, [17351] = true, [17352] = true, [18839] = true, [18841] = true }

-- The potion lists a role sees, in order (one of each in the best 4).
local POTIONS = {
  melee = { "potion-heal", "potion-buff", "potion-rage", "potion-mana", "potion-utility", "potion-protection" },
  agility = { "potion-heal", "potion-buff", "potion-mana", "potion-utility", "potion-protection", "potion-rage" },
  tank = { "potion-heal", "potion-rage", "potion-mana", "potion-utility", "potion-protection", "potion-buff" },
  caster = { "potion-mana", "potion-heal", "potion-buff", "potion-utility", "potion-protection" },
  healer = { "potion-mana", "potion-heal", "potion-buff", "potion-utility", "potion-protection" },
  hunter = { "potion-heal", "potion-mana", "potion-buff", "potion-utility", "potion-protection" },
}
-- END DATA
C.DATA = { items = ITEMS, best = BEST, all = ALL, potions = POTIONS, scores = SCORES, kind = KIND, pvp = PVP }

---------------------------------------------------------------------------
-- Small helpers
---------------------------------------------------------------------------
local function isSecret(v)
  local f = ns.IsSecret
  if type(f) ~= "function" then return false end
  local ok, r = pcall(f, v)
  return ok and r and true or false
end

local function plainNumber(v)
  if v == nil or isSecret(v) then return nil end
  return tonumber(v)
end

local function settings()
  return type(ns.settings) == "table" and ns.settings or {}
end

local FALLBACK = { text = { 0.90, 0.88, 0.84 }, muted = { 0.55, 0.51, 0.45 }, buy = { 0.898, 0.722, 0.259 },
                   pos = { 0.25, 0.80, 0.45 }, bg = { 0.043, 0.055, 0.078, 0.96 }, panel = { 0.07, 0.08, 0.10, 0.96 } }
local function colour(key)
  local c = type(ns.COLORS) == "table" and ns.COLORS[key]
  if type(c) == "table" then return c end
  return FALLBACK[key] or FALLBACK.text
end

local function hex(c)
  if type(ns.Hex) == "function" then return ns.Hex(c) end
  return sformat("ff%02x%02x%02x", floor(c[1] * 255 + 0.5), floor(c[2] * 255 + 0.5), floor(c[3] * 255 + 0.5))
end

local function paint(text, c)
  if type(c) == "string" then c = colour(c) end
  return "|c" .. hex(c) .. tostring(text) .. "|r"
end

local function say(msg)
  if type(ns.Print) == "function" then ns.Print(msg) end
end

local function money(c)
  if type(ns.ShortMoney) == "function" then return ns.ShortMoney(c) end
  if type(ns.MoneyText) == "function" then return ns.MoneyText(c) end
  return tostring(c) .. "c"
end

local function isFrame(f)
  return type(f) == "table" and type(f.GetObjectType) == "function"
end

-- f:Method(...) without raising; nil when it is missing or fails.
local function ask(f, method, ...)
  local fn = type(f) == "table" and f[method]
  if type(fn) ~= "function" then return nil end
  local ok, a, b = pcall(fn, f, ...)
  if ok then return a, b end
  return nil
end

local function guarded(fn)
  return function(...)
    local ok, err = pcall(fn, ...)
    if not ok then C.lastError = err end
  end
end

---------------------------------------------------------------------------
-- Who is looking: role, class and level
---------------------------------------------------------------------------
local ROLES = { melee = true, agility = true, tank = true, caster = true, healer = true, hunter = true }
local RAGE = { WARRIOR = true, DRUID = true }      -- rage potions
local NO_MANA = { WARRIOR = true, ROGUE = true }   -- mana potions

-- The role the enchant tips use (a /gl enchants pick wins; with the tips turned
-- off, the role they had), else the class's usual one, else melee.
local function whoAmI()
  local role, class
  local E = ns.Enchants
  if type(E) == "table" and type(E.DetectRole) == "function" then
    local ok, r, _, _, _, cls = pcall(E.DetectRole)
    if ok then role, class = r, cls end
  end
  local S = settings()
  if S.enchants == "off" and ROLES[S.enchantsBeforeOff] then role = S.enchantsBeforeOff end
  if not class and type(_G.UnitClass) == "function" then
    local ok, _, file = pcall(_G.UnitClass, "player")
    if ok and type(file) == "string" and not isSecret(file) then class = file end
  end
  local level = 60
  if type(_G.UnitLevel) == "function" then
    local ok, l = pcall(_G.UnitLevel, "player")
    l = ok and plainNumber(l) or nil
    if l and l >= 1 then level = floor(l) end
  end
  return ROLES[role] and role or "melee", class, level
end

---------------------------------------------------------------------------
-- Prices: what one costs (the Budget picks and the rows)
---------------------------------------------------------------------------
local function dataStamp()
  local db = ns.db
  return type(db) == "table" and tonumber(db.updated) or 0
end

-- Today's AH value: Goblin Ledger's live price when it has one (ns.LivePrice), else the
-- price data's; 0 when there is none.
local function ahValue(id)
  local f = ns.LivePrice
  if type(f) == "function" then
    local ok, v = pcall(f, id)
    v = ok and plainNumber(v) or nil
    if v and v > 0 then return v end
  end
  if dataStamp() > 0 and type(ns.GetItem) == "function" then
    local ok, g = pcall(ns.GetItem, id)
    local v = ok and type(g) == "table" and plainNumber(g.market) or nil
    if v and v > 0 then return v end
  end
  return 0
end

-- What one costs: a vendor's price when it is the cheaper, else today's AH value; nil: no price.
local function costOf(id)
  local it = ITEMS[id]
  local vendor, market = it and it[8] or 0, ahValue(id)
  if vendor > 0 and (market <= 0 or vendor <= market) then return vendor, true end
  if market > 0 then return market, false end
  return nil
end
C.CostOf = costOf

---------------------------------------------------------------------------
-- What to show: the sections and their items
---------------------------------------------------------------------------
-- The panel's sections, in order; one with nothing usable is left out.
local SECTIONS = {
  { key = "food", title = "FOOD", lists = { "food" } },
  { key = "potion", title = "POTIONS" },   -- lists: POTIONS[role]
  { key = "elixir", title = "ELIXIRS", lists = { "elixir" } },
  { key = "scroll", title = "SCROLLS", lists = { "scroll" } },
  { key = "flask", title = "FLASKS", lists = { "flask" } },
  { key = "weapon", title = "STONES AND OILS", lists = { "weapon" } },
}
local BEST_N = 4
local FLOOR = 0.5    -- Budget: at least half as strong as the strongest one of its kind
local NO_XP = 0.5    -- below 60, a food without the +5% XP counts half (XP food first while levelling)

local function bandOf(level)
  if level >= 60 then return 5 elseif level >= 50 then return 4 elseif level >= 40 then return 3 elseif level >= 20 then return 2 end
  return 1
end

local function listAllowed(list, class)
  if list == "potion-rage" then return class == nil or RAGE[class] == true end
  if list == "potion-mana" then return not NO_MANA[class] end
  return true
end

-- Can this class and level use it? An item's classes: nil = all, else e.g. "PRIEST,WARLOCK"
-- (the mage's familiar scrolls; a school's elixir only for the classes that cast that school).
local function usable(it, level, class)
  if not it or it[4] > level then return false end
  local who = it[10]
  if who == nil or class == nil then return true end
  return (("," .. who .. ","):find("," .. class .. ",", 1, true)) ~= nil
end

local function append(out, ids)
  if type(ids) == "table" then
    for _, id in ipairs(ids) do out[#out + 1] = id end
  end
  return out
end

-- Each item's strength for a role and list, from SCORES (in ALL's order).
local SCORE = {}
for role, lists in pairs(ALL) do
  SCORE[role] = {}
  for list, ids in pairs(lists) do
    local sc, map = SCORES[role] and SCORES[role][list], {}
    for i, id in ipairs(ids) do map[id] = sc and sc[i] or nil end
    SCORE[role][list] = map
  end
end

-- How strong one is for the role, list and level (the ranking's score); nil: not on the list.
local function strength(role, list, id, level)
  local s = SCORE[role] and SCORE[role][list] and SCORE[role][list][id]
  if not s then return nil end
  local it = ITEMS[id]
  if list == "food" and level < 60 and it and it[7] <= 0 then s = s * NO_XP end
  return s
end

-- What one is measured against: the rest of its list, except that a stone or oil only meets
-- those for the same weapons, a protection potion those of its kind (fire, armor, ...) and a
-- utility potion (Free Action, Swiftness, ...) only itself: nothing else does the same.
local function kindOf(list, id)
  local it = ITEMS[id]
  if list == "potion-utility" then return list .. ":" .. (it and it[9] or tostring(id)) end
  if list == "potion-protection" then return list .. ":" .. (KIND[id] or "") end
  if list == "weapon" then return list .. ":" .. (it and smatch(it[9], "^weapon:([^:]*)") or "") end
  return list
end

-- The strongest usable one of each kind for the role and level: kind -> { strength, id }
-- (the first on the list when two are as strong; PvP-only ones are not counted).
local function strongest(role, lists, level, class)
  local ref = {}
  for _, list in ipairs(lists) do
    if listAllowed(list, class) then
      for _, id in ipairs(ALL[role] and ALL[role][list] or {}) do
        if usable(ITEMS[id], level, class) and not PVP[id] then
          local s, k = strength(role, list, id, level), kindOf(list, id)
          if s and (not ref[k] or s > ref[k][1]) then ref[k] = { s, id } end
        end
      end
    end
  end
  return ref
end

-- The picks (Best and Budget): the next usable one of each list in turn (potions: one of each
-- kind first), skipping one whose group was shown (two elixirs of one stat line, two foods of one
-- stat, two stones of one kind, a potion of a name already in); 4 at most, and each one's list.
local function takeTurns(per, lists, level, class)
  local pos, out, from, seen = {}, {}, {}, {}
  for i = 1, #lists do pos[i] = 1 end
  local more = true
  while #out < BEST_N and more do
    more = false
    for i = 1, #lists do
      local ids, p = per[i], pos[i]
      while p <= #ids do
        local id = ids[p]
        p = p + 1
        local it = ITEMS[id]
        if usable(it, level, class) and not seen[it[9]] then
          seen[it[9]] = true
          out[#out + 1] = id
          from[#out] = lists[i]
          more = true
          break
        end
      end
      pos[i] = p
      if #out >= BEST_N then break end
    end
  end
  return out, from
end

-- A list as the best ones come: the research's ranking for the level's band, then every one (for a
-- level low in its band).
local function rankOrder(role, list, level)
  local b = BEST[role] and BEST[role][list]
  return append(append({}, b and b[bandOf(level)]), ALL[role] and ALL[role][list])
end

local function bestIds(role, lists, level, class)
  local per = {}
  for i, list in ipairs(lists) do
    per[i] = listAllowed(list, class) and rankOrder(role, list, level) or {}
  end
  return takeTurns(per, lists, level, class)
end

-- Budget's order for one list: the ones at least half as strong as the strongest of their kind,
-- with a price, not PvP-only, and not weaker and dearer than another of their kind; the most
-- strength per copper first (ties: the stronger), each kind's best buy first in the order the
-- best ones come (a stone for each kind of weapon, the protection potion Best shows, ...).
local function budgetList(role, list, level, class, ref)
  local cand = {}
  for _, id in ipairs(ALL[role] and ALL[role][list] or {}) do
    if usable(ITEMS[id], level, class) and not PVP[id] then
      local s, k, c = strength(role, list, id, level), kindOf(list, id), costOf(id)
      local r = ref[k]
      if s and c and c > 0 and r and s >= FLOOR * r[1] - 1e-9 then
        cand[#cand + 1] = { id = id, s = s, c = c, k = k }
      end
    end
  end
  local keep = {}
  for _, a in ipairs(cand) do
    local beaten = false
    for _, b in ipairs(cand) do
      if b ~= a and b.k == a.k and b.s >= a.s and b.c <= a.c and (b.s > a.s or b.c < a.c) then
        beaten = true
        break
      end
    end
    if not beaten then keep[#keep + 1] = a end
  end
  tsort(keep, function(a, b)
    local va, vb = a.s / a.c, b.s / b.c
    if va ~= vb then return va > vb end
    if a.s ~= b.s then return a.s > b.s end
    if a.c ~= b.c then return a.c < b.c end
    return a.id < b.id
  end)
  local out, took, kinds = {}, {}, {}
  for _, id in ipairs(rankOrder(role, list, level)) do
    local k = kindOf(list, id)
    if not kinds[k] and usable(ITEMS[id], level, class) and not PVP[id] then
      kinds[k] = true
      for _, a in ipairs(keep) do
        if a.k == k then
          out[#out + 1] = a.id
          took[a] = true
          break
        end
      end
    end
  end
  for _, a in ipairs(keep) do
    if not took[a] then out[#out + 1] = a.id end
  end
  return out
end

-- The Budget picks: as the best ones (one of each kind of potion first, a group once, 4 at most),
-- from each list's Budget order. Below 60 every Well Fed food gives the same +5% XP: the cheapest
-- of the role's foods is in too (the last row), whatever its stat.
local function budgetIds(role, key, lists, level, class, ref)
  local per = {}
  for i, list in ipairs(lists) do
    per[i] = listAllowed(list, class) and budgetList(role, list, level, class, ref) or {}
  end
  local out, from = takeTurns(per, lists, level, class)
  if key == "food" and level < 60 then
    local cheap, cc, cs
    for _, id in ipairs(ALL[role] and ALL[role].food or {}) do
      local it = ITEMS[id]
      if it and it[7] > 0 and usable(it, level, class) and not PVP[id] then
        local c, s = costOf(id), strength(role, "food", id, level) or 0
        if c and c > 0 and (not cc or c < cc or (c == cc and s > cs)) then cheap, cc, cs = id, c, s end
      end
    end
    local there = false
    for _, id in ipairs(out) do
      if id == cheap then there = true end
    end
    if cheap and not there then
      local n = min(#out + 1, BEST_N)
      out[n], from[n] = cheap, "food"
    end
  end
  return out, from
end

-- Every usable one (the All switch), best first; with Budget, cheapest first (no price: last).
local function allIds(role, lists, level, class, budget)
  local out, from, seen = {}, {}, {}
  for _, list in ipairs(lists) do
    local ids = listAllowed(list, class) and ALL[role] and ALL[role][list] or {}
    for _, id in ipairs(ids) do
      if usable(ITEMS[id], level, class) and not seen[id] then
        seen[id] = true
        out[#out + 1] = id
        from[#out] = list
      end
    end
  end
  if budget then
    local rows = {}
    for i, id in ipairs(out) do rows[i] = { id = id, list = from[i], i = i, c = costOf(id) } end
    tsort(rows, function(a, b)
      if (a.c ~= nil) ~= (b.c ~= nil) then return a.c ~= nil end
      if a.c and a.c ~= b.c then return a.c < b.c end
      return a.i < b.i
    end)
    for i, r in ipairs(rows) do out[i], from[i] = r.id, r.list end
  end
  return out, from
end

-- { role, class, level, all, budget, sections = { { key, title, ids, lists, pct, ref }, ... } }: each row's
-- list, its strength in % of the strongest usable one of its kind (100: it is that strong), and which that is.
local model

local function buildModel()
  local role, class, level = whoAmI()
  local S = settings()
  local all, budget = S.consumesAll == true, S.consumesBudget == true
  local m = { role = role, class = class, level = level, all = all, budget = budget, sections = {} }
  for _, s in ipairs(SECTIONS) do
    local lists = s.lists or POTIONS[role] or {}
    local ref = strongest(role, lists, level, class)
    local ids, from
    if all then
      ids, from = allIds(role, lists, level, class, budget)
    elseif budget then
      ids, from = budgetIds(role, s.key, lists, level, class, ref)
    else
      ids, from = bestIds(role, lists, level, class)
    end
    if #ids > 0 then
      local pct, refs = {}, {}
      for i, id in ipairs(ids) do
        local list = from[i]
        local r, sc = ref[kindOf(list, id)], strength(role, list, id, level)
        if r and sc and r[1] > 0 then
          pct[i] = sc >= r[1] - 1e-9 and 100 or max(1, min(99, floor(sc * 100 / r[1] + 1e-9)))
          refs[i] = r[2]
        end
      end
      m.sections[#m.sections + 1] = { key = s.key, title = s.title, ids = ids, lists = from, pct = pct, ref = refs }
    end
  end
  model = m
  return m
end
C.BuildModel = buildModel
C.Model = function() return model end

---------------------------------------------------------------------------
-- One row's words: name, buff, XP, count, price
---------------------------------------------------------------------------
local QUALITY = { [0] = { 0.62, 0.62, 0.62 }, [1] = { 1, 1, 1 }, [2] = { 0.12, 1, 0 }, [3] = { 0, 0.44, 0.87 },
                  [4] = { 0.64, 0.21, 0.93 }, [5] = { 1, 0.5, 0 } }
local QUESTION = "Interface\\Icons\\INV_Misc_QuestionMark"

local function qualityColour(q)
  local CI = _G.C_Item
  if type(CI) == "table" and type(CI.GetItemQualityColor) == "function" then
    local ok, r, g, b = pcall(CI.GetItemQualityColor, q)
    r, g, b = ok and plainNumber(r), ok and plainNumber(g), ok and plainNumber(b)
    if r and g and b then return { r, g, b } end
  end
  return QUALITY[q] or QUALITY[1]
end

local function iconOf(id)
  local CI = _G.C_Item
  local fn = type(CI) == "table" and CI.GetItemIconByID or _G.GetItemIcon
  if type(fn) == "function" then
    local ok, icon = pcall(fn, id)
    if ok and icon ~= nil and not isSecret(icon) then return icon end
  end
  return QUESTION
end

-- How many are in the bags (not the bank).
local function countOf(id)
  local CI = _G.C_Item
  local fn = type(CI) == "table" and CI.GetItemCount or _G.GetItemCount
  if type(fn) ~= "function" then return 0 end
  local ok, n = pcall(fn, id)
  return ok and plainNumber(n) or 0
end

-- Prices older than Core's limit (6 hours) are grey, as on the enchant tips.
local function stalePrices()
  local f, most = ns.DataAge, ns.STALE_SECONDS
  if type(f) ~= "function" or type(most) ~= "number" then return false end
  local ok, age = pcall(f)
  return ok and type(age) == "number" and age > most or false
end

local function ahOpen()
  return type(ns.AH_IsOpen) == "function" and ns.AH_IsOpen() and true or false
end

-- The price words and their colour: the cheapest listing the open auction house shows,
-- else a vendor's price when it is the cheaper, else today's AH value (grey when old).
local function priceOf(id, stale)
  local it = ITEMS[id]
  local vendor = it and it[8] or 0
  if ahOpen() and type(ns.LiveCheapest) == "function" then
    local ok, live = pcall(ns.LiveCheapest, id)
    live = ok and plainNumber(live) or nil
    if live and live > 0 then return money(live), "text" end
  end
  local market = ahValue(id)
  if vendor > 0 and (market <= 0 or vendor <= market) then return "vendor " .. money(vendor), "text" end
  if market > 0 then return money(market), stale and "muted" or "text" end
  if dataStamp() <= 0 then return "no prices", "muted" end
  return "not on AH", "muted"
end
C.PriceOf = priceOf

-- The left words: the name in its quality colour, the buff, and "+5% XP" in green on food
-- that gives it (Well Fed in Forever: experience from kills, below level 60).
local function leftText(id, level)
  local it = ITEMS[id]
  local t = paint(it[2] or it[1], qualityColour(it[5]))
  if it[6] ~= "" then t = t .. "  " .. paint(it[6], "muted") end
  if it[7] > 0 and level < 60 then t = t .. "  " .. paint("+" .. it[7] .. "% XP", "pos") end
  return t
end

local function rightText(id, stale)
  local words, key = priceOf(id, stale)
  local n = countOf(id)
  local t = paint(words, key)
  if n > 0 then t = paint("x" .. n, "muted") .. "  " .. t end
  return t
end

---------------------------------------------------------------------------
-- The panel
---------------------------------------------------------------------------
-- A child of PaperDollFrame: it shows with the Character tab and goes with it (other tabs,
-- closing the character screen), with no hooks. Never a child of PaperDollItemsFrame
-- (ElvUI skins every child of that as a slot). It hangs under the character screen, as
-- wide as it is (it follows the stats pane folding away), 2px under the lowest thing on
-- its bottom edge: the frame itself, the weapon slots, Pawn's button, GearMatic's arrows,
-- and ElvUI's enchant labels (the off hand's hangs 3px under the frame). Without room
-- there it shows fewer rows, then goes above the character screen, then scrolls.
local NAME = "GoblinLedgerConsumes"
local HEAD_H, ROW_H, PAD, COL_GAP, GAP = 18, 16, 6, 12, 2
local BODY_TOP, BODY_BOT = 1, 3
local MAX_LINES = 16   -- the All list: at most this many lines, the rest scrolls
local CAP = 24         -- a part more than this under the frame is not ours to avoid
local LOW_PARTS = { "CharacterMainHandSlot", "CharacterSecondaryHandSlot", "CharacterRangedSlot", "CharacterAmmoSlot",
                    "PawnUI_InventoryPawnButton", "GearMaticPaperdollPrev", "GearMaticPaperdollNext" }
local LABELLED = { CharacterMainHandSlot = true, CharacterSecondaryHandSlot = true, CharacterRangedSlot = true }
local HIGH_PARTS = { "CharacterFramePortrait" }

local F            -- the panel, made once
local layout = {}  -- where it is and what it shows (C.Layout)
local offset = 0   -- lines scrolled
local pollKey, pollWait, needModel, needDraw = nil, 0, false, false

local function shownPart(f)
  if not isFrame(f) then return false end
  return ask(f, "IsVisible") and true or false
end

-- The bottom (or top) of a part on screen, nil when it has none. A font string is placed
-- in its frame's scale (older clients have no GetEffectiveScale on one).
local function edgeOf(f, method)
  local v = plainNumber(ask(f, method))
  local s = plainNumber(ask(f, "GetEffectiveScale")) or plainNumber(ask(ask(f, "GetParent"), "GetEffectiveScale"))
  if not v or not s then return nil end
  return v * s
end

-- The character screen's edges on screen, the lowest thing on its bottom edge, the highest
-- on its top edge, and the screen's top.
local function measure()
  local cf = _G.CharacterFrame
  if not isFrame(cf) then return nil end
  local b, t = edgeOf(cf, "GetBottom"), edgeOf(cf, "GetTop")
  local eff = plainNumber(ask(cf, "GetEffectiveScale"))
  local w = plainNumber(ask(cf, "GetWidth"))
  if not b or not t or not eff or not w or eff <= 0 then return nil end
  local low, high = b, t
  for _, name in ipairs(LOW_PARTS) do
    local f = _G[name]
    if shownPart(f) then
      local fb = edgeOf(f, "GetBottom")
      if fb and fb < low and fb >= b - CAP * eff then low = fb end
      local label = LABELLED[name] and f.enchantText
      local txt = shownPart(label) and ask(label, "GetText")
      if type(txt) == "string" and not isSecret(txt) and txt ~= "" then
        local lb = edgeOf(label, "GetBottom")
        if lb and lb < low and lb >= b - CAP * eff then low = lb end
      end
    end
  end
  for _, name in ipairs(HIGH_PARTS) do
    local f = _G[name]
    if shownPart(f) then
      local ft = edgeOf(f, "GetTop")
      if ft and ft > high and ft <= t + CAP * eff then high = ft end
    end
  end
  local up = _G.UIParent
  local top = edgeOf(up, "GetTop") or t
  return { bottom = b, top = t, low = low, high = high, screenTop = top, eff = eff, width = w }
end
C.Measure = measure

local function colsFor(width)
  if width >= 900 then return 3 elseif width >= 360 then return 2 end
  return 1
end

-- The lines (section titles and rows) for n rows a section (nil: all), sections side by side.
local function cellsFor(m, cols, n)
  local cells, line, secs = {}, 0, m.sections
  for first = 1, #secs, cols do
    local tall = 0
    for c = 0, cols - 1 do
      local s = secs[first + c]
      if s then
        local k = #s.ids
        if n and k > n then k = n end
        cells[#cells + 1] = { line = line, col = c, title = s.title, key = s.key }
        for i = 1, k do
          cells[#cells + 1] = { line = line + i, col = c, id = s.ids[i], key = s.key, pct = s.pct and s.pct[i],
                                ref = s.ref and s.ref[i] }
        end
        if k > tall then tall = k end
      end
    end
    line = line + 1 + tall
  end
  return cells, line
end

local function fitLines(room)
  return floor((room - HEAD_H - BODY_TOP - BODY_BOT) / ROW_H)
end

local function heightFor(lines)
  if lines <= 0 then return HEAD_H end
  return HEAD_H + BODY_TOP + lines * ROW_H + BODY_BOT
end

-- Pools: section titles (font strings) and rows (buttons), made as they are needed.
local titles, rows = {}, {}

local function newFS(parent, font)
  local fs = parent:CreateFontString(nil, "OVERLAY", font or "GameFontHighlightSmall")
  fs:SetJustifyH("LEFT")
  if fs.SetWordWrap then fs:SetWordWrap(false) end
  return fs
end

local rowEnter, rowLeave, rowClick, onWheel   -- the row scripts (below)

local function newRow(i)
  local r = _G.CreateFrame("Button", nil, F)
  r:SetHeight(ROW_H)
  if r.RegisterForClicks then r:RegisterForClicks("LeftButtonUp") end
  local hl = r:CreateTexture(nil, "HIGHLIGHT")
  hl:SetAllPoints(r)
  hl:SetColorTexture(1, 1, 1, 0.06)
  r.icon = r:CreateTexture(nil, "ARTWORK")
  r.icon:SetSize(14, 14)
  r.icon:SetPoint("LEFT", r, "LEFT", 1, 0)
  r.icon:SetTexCoord(0.07, 0.93, 0.07, 0.93)
  r.right = newFS(r)
  r.right:SetJustifyH("RIGHT")
  r.right:SetPoint("RIGHT", r, "RIGHT", -2, 0)
  -- How strong it is next to the strongest of its kind ("best", "62%"), grey, left of the price;
  -- the words give way to it (cut short) when the row is tight.
  r.pct = newFS(r)
  r.pct:SetJustifyH("RIGHT")
  r.pct:SetPoint("RIGHT", r.right, "LEFT", -6, 0)
  local mu = colour("muted")
  r.pct:SetTextColor(mu[1], mu[2], mu[3], 1)
  r.text = newFS(r)
  r.text:SetPoint("LEFT", r.icon, "RIGHT", 4, 0)
  r.text:SetPoint("RIGHT", r.pct, "LEFT", -6, 0)
  r.glNoEnchants = true   -- Enchants.lua: no enchant lines on a tooltip a row owns
  r:SetScript("OnEnter", guarded(rowEnter))
  r:SetScript("OnLeave", guarded(rowLeave))
  r:SetScript("OnClick", guarded(rowClick))
  r:SetScript("OnMouseWheel", guarded(function(_, delta) onWheel(F, delta) end))
  rows[i] = r
  return r
end

local function pctText(p)
  if not p then return "" end
  if p >= 100 then return "best" end
  return p .. "%"
end
C.PctText = pctText

local function fillRow(r, id, stale, pct, ref)
  r.glID, r.glPct, r.glRef = id, pct, ref
  r.icon:SetTexture(iconOf(id))
  r.text:SetText(leftText(id, model.level))
  r.pct:SetText(pctText(pct))
  r.right:SetText(rightText(id, stale))
end

local function headerText(m)
  return paint("Consumes", "buy") .. paint(sformat(" for %s, level %d", m.role, m.level), "text")
end

-- Puts the visible lines in place (scrolled by offset) and fills the rows.
local function draw()
  if not F or not model then return end
  C.draws = C.draws + 1
  local L = layout
  F.title:SetText(headerText(model))
  F.arrow:SetText(L.collapsed and ">" or "v")
  F.allButton:SetShown(not L.collapsed and #model.sections > 0)
  local on = model.all and colour("buy") or colour("muted")
  F.allButton.text:SetTextColor(on[1], on[2], on[3], 1)
  -- Best / Budget: the word for the picks shown (gold while Budget is on), as wide as it.
  local mb = F.modeButton
  mb:SetShown(not L.collapsed and #model.sections > 0)
  mb.text:SetText(model.budget and "Budget" or "Best")
  local okW, wide = pcall(mb.text.GetStringWidth, mb.text)
  wide = okW and plainNumber(wide) or 0
  if wide <= 0 or wide > 80 then wide = 36 end
  mb:SetWidth(max(28, floor(wide + 10)))
  on = model.budget and colour("buy") or colour("muted")
  mb.text:SetTextColor(on[1], on[2], on[3], 1)
  local vis, total = L.lines or 0, L.total or 0
  if offset > total - vis then offset = total - vis end
  if offset < 0 then offset = 0 end
  local stale, nt, nr = stalePrices(), 0, 0
  -- The wheel scrolls the list only while there is more to see; else it zooms the camera as always.
  local wheel = not L.collapsed and vis > 0 and total > vis
  if F.EnableMouseWheel then F:EnableMouseWheel(wheel) end
  if not L.collapsed and vis > 0 then
    local x0, colW, top = PAD, L.colW, -(HEAD_H + BODY_TOP)
    for _, cell in ipairs(L.cells) do
      local line = cell.line - offset
      if line >= 0 and line < vis then
        local x, y = x0 + cell.col * (colW + COL_GAP), top - line * ROW_H
        if cell.title then
          nt = nt + 1
          local fs = titles[nt]
          if not fs then
            fs = newFS(F, "GameFontNormalSmall")
            titles[nt] = fs
          end
          local mu = colour("muted")
          fs:SetTextColor(mu[1], mu[2], mu[3], 1)
          fs:ClearAllPoints()
          fs:SetPoint("LEFT", F, "TOPLEFT", x + 2, y - ROW_H / 2)
          fs:SetText(cell.title)
          fs.glKey = cell.key
          fs:Show()
        else
          nr = nr + 1
          local r = rows[nr] or newRow(nr)
          r:ClearAllPoints()
          r:SetPoint("TOPLEFT", F, "TOPLEFT", x, y)
          r:SetWidth(colW)
          r.glKey = cell.key
          if r.EnableMouseWheel then r:EnableMouseWheel(wheel) end
          fillRow(r, cell.id, stale, cell.pct, cell.ref)
          r:Show()
        end
      end
    end
  end
  for i = nt + 1, #titles do titles[i]:Hide() end
  for i = nr + 1, #rows do
    rows[i]:Hide()
    rows[i].glID = nil
  end
  -- A thin bar on the right edge when there is more to scroll to.
  local bar = F.scrollBar
  if not L.collapsed and total > vis and vis > 0 then
    local h = vis * ROW_H
    local bh = max(8, floor(h * vis / total))
    local by = floor((h - bh) * offset / max(1, total - vis))
    bar:ClearAllPoints()
    bar:SetPoint("TOPRIGHT", F, "TOPRIGHT", -2, -(HEAD_H + BODY_TOP) - by)
    bar:SetSize(2, bh)
    bar:Show()
  else
    bar:Hide()
  end
  C.shown = { titles = nt, rows = nr }
end
C.Draw = function() pcall(draw) end

-- Where it goes and how many lines it shows, from where the character screen is now.
local function place()
  if not F or not model then return end
  local m = measure()
  if not m then return end
  C.places = C.places + 1
  local pe = plainNumber(ask(F, "GetEffectiveScale")) or m.eff
  if pe <= 0 then pe = m.eff end
  local width = m.width * m.eff / pe
  local cols = colsFor(width)
  local colW = floor((width - 2 * PAD - (cols - 1) * COL_GAP) / cols)
  local below = m.low / pe - GAP
  local above = (m.screenTop - m.high) / pe - GAP
  local collapsed = settings().consumesCollapsed == true or #model.sections == 0
  local fitBelow, fitAbove = fitLines(below), fitLines(above)
  local side, n, lines, cells, total = "below", nil, 0, {}, 0
  if not collapsed then
    if model.all then
      cells, total = cellsFor(model, cols, nil)
      local want = min(total, MAX_LINES)
      if fitBelow >= min(want, 2) then
        lines = min(want, fitBelow)
      elseif fitAbove > fitBelow then
        side, lines = "above", min(want, fitAbove)
      else
        lines = max(0, fitBelow)
      end
    else
      -- The best 4 if they fit, else 3 or 2 a section; under the screen first, then above it.
      for _, where in ipairs({ "below", "above" }) do
        local fit = where == "below" and fitBelow or fitAbove
        for k = BEST_N, 2, -1 do
          local c, t = cellsFor(model, cols, k)
          if t <= fit then
            side, n, cells, total, lines = where, k, c, t, t
            break
          end
        end
        if n then break end
      end
      if not n then
        -- Not even 2 rows a section fit: all 4, scrolled in what room there is.
        n = BEST_N
        cells, total = cellsFor(model, cols, n)
        if fitAbove > fitBelow and fitAbove > 0 then side, lines = "above", min(total, fitAbove)
        else lines = max(0, min(total, fitBelow)) end
      end
    end
  end
  if collapsed and below < HEAD_H and above > below then side = "above" end
  F:ClearAllPoints()
  local cf = _G.CharacterFrame
  if side == "below" then
    local dy = (m.low - m.bottom) / pe - GAP
    F:SetPoint("TOPLEFT", cf, "BOTTOMLEFT", 0, dy)
    F:SetPoint("TOPRIGHT", cf, "BOTTOMRIGHT", 0, dy)
  else
    local dy = (m.high - m.top) / pe + GAP
    F:SetPoint("BOTTOMLEFT", cf, "TOPLEFT", 0, dy)
    F:SetPoint("BOTTOMRIGHT", cf, "TOPRIGHT", 0, dy)
  end
  F:SetHeight(heightFor(collapsed and 0 or lines))
  layout = { side = side, cols = cols, colW = colW, n = n, lines = collapsed and 0 or lines, total = total,
             cells = cells, collapsed = collapsed, below = below, above = above, width = width }
  draw()
end
C.Place = function() pcall(place) end
C.Layout = function() return layout end

-- The key of what place() reads: it runs again only when one of them changed.
local function keyOf(m)
  if not m then return "none" end
  local function r(v) return floor(v * 100 + 0.5) end
  return r(m.bottom) .. ":" .. r(m.top) .. ":" .. r(m.low) .. ":" .. r(m.high) .. ":" .. r(m.width) .. ":" ..
    r(m.eff) .. ":" .. r(m.screenTop)
end

local function refresh()
  if not F then return end
  buildModel()
  place()
  pollKey = keyOf(measure())
end
C.Refresh = function() pcall(refresh) end

---------------------------------------------------------------------------
-- Rows: hover, click, wheel
---------------------------------------------------------------------------
local function owns(tt, f)
  if f == nil or type(tt) ~= "table" or type(tt.GetOwner) ~= "function" then return false end
  local ok, o = pcall(tt.GetOwner, tt)
  return ok and o == f
end

-- The game's tooltip for the item (Goblin Ledger's price lines come with it).
rowEnter = function(self)
  local id, tt = self.glID, _G.GameTooltip
  if not id or type(tt) ~= "table" or type(tt.SetOwner) ~= "function" then return end
  tt:SetOwner(self, "ANCHOR_RIGHT")
  local shown = false
  if type(tt.SetItemByID) == "function" then
    ns.tipQuiet = true
    shown = pcall(tt.SetItemByID, tt, id)
    ns.tipQuiet = false
  end
  if not shown then
    local it = ITEMS[id]
    tt:AddLine(it and it[1] or ("Item " .. id), 1, 1, 1)
  end
  local p = self.glPct
  if p and model then
    local who = sformat("a level %d %s", model.level, model.role)
    local ref = ITEMS[self.glRef]
    local line = p >= 100 and ("Best: the strongest for " .. who) or
      sformat("%d%% as strong as %s for %s", p, ref and ref[1] or "the best one", who)
    local mu = colour("muted")
    tt:AddLine(line, mu[1], mu[2], mu[3])
  end
  tt:Show()
end

rowLeave = function(self)
  local tt = _G.GameTooltip
  if owns(tt, self) then tt:Hide() end
end

local function chatLink(id)
  local CI = _G.C_Item
  local fn = type(CI) == "table" and CI.GetItemInfo or _G.GetItemInfo
  if type(fn) ~= "function" then return nil end
  local ok, _, link = pcall(fn, id)
  if ok and type(link) == "string" and not isSecret(link) then return link end
  return nil
end

-- Shift-click: the item's link in chat. A click while the auction house is open: searches it there.
rowClick = function(self)
  local id = self.glID
  if not id then return end
  local mod = _G.IsModifiedClick
  if type(mod) == "function" and mod("CHATLINK") then
    local link = chatLink(id)
    if link then
      local CFU = _G.ChatFrameUtil
      if type(CFU) == "table" and type(CFU.InsertLink) == "function" then
        pcall(CFU.InsertLink, link)
      elseif type(_G.ChatEdit_InsertLink) == "function" then
        pcall(_G.ChatEdit_InsertLink, link)
      end
    end
    return
  end
  if ahOpen() and type(ns.SearchAH) == "function" then ns.SearchAH(id) end
end

onWheel = function(self, delta)
  local L = layout
  if L.collapsed or (L.total or 0) <= (L.lines or 0) then return end
  offset = offset - (tonumber(delta) or 0) * 3
  draw()
end

---------------------------------------------------------------------------
-- Making it
---------------------------------------------------------------------------
local function backdrop()
  local bg = colour("bg")
  if F.SetBackdrop then
    F:SetBackdrop({ bgFile = "Interface\\Buttons\\WHITE8X8", edgeFile = "Interface\\Buttons\\WHITE8X8", edgeSize = 1 })
    F:SetBackdropColor(bg[1], bg[2], bg[3], bg[4] or 0.96)
    F:SetBackdropBorderColor(0.20, 0.20, 0.24, 1)
  else
    local t = F:CreateTexture(nil, "BACKGROUND", nil, -8)
    t:SetAllPoints(F)
    t:SetColorTexture(bg[1], bg[2], bg[3], bg[4] or 0.96)
  end
end

-- With ElvUI's character skin on: ElvUI's own panel look ("Transparent", as its character
-- screen), once ElvUI is set up. true = done; false = ask again the next time it shows.
local function elvLook()
  local E = ns.Enchants
  local fn = type(E) == "table" and E.ElvCharacterSkin
  if type(fn) ~= "function" then return true end
  local ok, EV, S = pcall(fn)
  if not ok then return true end
  if EV == "later" then return false end
  if not EV then return true end
  if type(F.SetTemplate) ~= "function" then return true end
  if not ((type(S) == "table" and S.Initialized) or EV.Initialized) then return false end
  if pcall(F.SetTemplate, F, "Transparent") then
    C.look = "elvui"
  else
    pcall(backdrop)
  end
  return true
end

local function isOn()
  return settings().consumes ~= false
end
C.IsOn = isOn

local function onShow(self)
  if not isOn() then
    self:Hide()
    return
  end
  if not self.glLookDone then self.glLookDone = elvLook() end
  offset = 0
  refresh()
end

local EVENTS = { "PLAYER_LOGIN", "BAG_UPDATE_DELAYED", "GET_ITEM_INFO_RECEIVED", "AUCTION_HOUSE_SHOW", "AUCTION_HOUSE_CLOSED",
                 "COMMODITY_SEARCH_RESULTS_UPDATED", "ITEM_SEARCH_RESULTS_UPDATED", "AUCTION_ITEM_LIST_UPDATE",
                 "PLAYER_LEVEL_UP", "CHARACTER_POINTS_CHANGED", "PLAYER_TALENT_UPDATE", "TRAIT_CONFIG_UPDATED",
                 "ACTIVE_TALENT_GROUP_CHANGED" }
-- These only redraw (counts, prices); the others read the role and level again.
local REDRAW = { BAG_UPDATE_DELAYED = true, GET_ITEM_INFO_RECEIVED = true, AUCTION_HOUSE_SHOW = true, AUCTION_HOUSE_CLOSED = true,
                 COMMODITY_SEARCH_RESULTS_UPDATED = true, ITEM_SEARCH_RESULTS_UPDATED = true, AUCTION_ITEM_LIST_UPDATE = true }

-- Events while it shows: bag counts, the auction house's live prices, a new level or talents.
local function onEvent(self, event, arg1)
  if event == "PLAYER_LOGIN" then
    if not isOn() then self:Hide() end
    return
  end
  if not self:IsVisible() then return end
  if REDRAW[event] then
    needDraw = true
  else
    needModel = true
  end
end

-- Each frame while it shows: what the events asked for; every quarter second, whether the
-- character screen or the parts on its bottom edge moved (the stats pane folded, the auction
-- house moved it, ElvUI filled its enchant labels after this showed).
local function onUpdate(self, elapsed)
  if needModel then
    needModel, needDraw = false, false
    refresh()
    return
  end
  if needDraw then
    needDraw = false
    draw()
  end
  pollWait = pollWait - (tonumber(elapsed) or 0)
  if pollWait > 0 then return end
  pollWait = 0.25
  C.polls = C.polls + 1
  local key = keyOf(measure())
  if key ~= pollKey then
    pollKey = key
    place()
  end
end

local function toggleCollapsed()
  local S = settings()
  if S.consumesCollapsed then S.consumesCollapsed = nil else S.consumesCollapsed = true end
  offset = 0
  place()
end

local function toggleAll()
  local S = settings()
  if S.consumesAll then S.consumesAll = nil else S.consumesAll = true end
  offset = 0
  refresh()
end

local function toggleBudget()
  local S = settings()
  if S.consumesBudget then S.consumesBudget = nil else S.consumesBudget = true end
  offset = 0
  refresh()
end

local MODE_TIP = "Best: the strongest. Budget: good value for less gold. Click to switch."

local function build()
  if F then return true end
  local parent, make = _G.PaperDollFrame, _G.CreateFrame
  if not isFrame(parent) or type(make) ~= "function" then return false end
  local template = _G.BackdropTemplateMixin and "BackdropTemplate" or nil
  F = make("Frame", NAME, parent, template)
  C.builds = C.builds + 1
  F:SetHeight(HEAD_H)
  F:EnableMouse(true)
  if F.SetClampedToScreen then F:SetClampedToScreen(true) end
  backdrop()
  C.look = "plain"
  -- The header: a click folds the list to this one line, or opens it again.
  local head = make("Button", nil, F)
  head:SetPoint("TOPLEFT", F, "TOPLEFT", 0, 0)
  head:SetPoint("TOPRIGHT", F, "TOPRIGHT", 0, 0)
  head:SetHeight(HEAD_H)
  if head.RegisterForClicks then head:RegisterForClicks("LeftButtonUp") end
  local hl = head:CreateTexture(nil, "HIGHLIGHT")
  hl:SetAllPoints(head)
  hl:SetColorTexture(1, 1, 1, 0.05)
  F.arrow = newFS(head, "GameFontNormalSmall")
  F.arrow:SetPoint("LEFT", head, "LEFT", PAD, 0)
  local mu = colour("muted")
  F.arrow:SetTextColor(mu[1], mu[2], mu[3], 1)
  F.title = newFS(head, "GameFontNormalSmall")
  F.title:SetPoint("LEFT", F.arrow, "RIGHT", 5, 0)
  head:SetScript("OnClick", guarded(toggleCollapsed))
  F.header = head
  -- All: every usable item instead of the best 4 (gold while on).
  local all = make("Button", nil, F)
  all:SetSize(28, 14)
  all:SetPoint("TOPRIGHT", F, "TOPRIGHT", -4, -2)
  if all.SetFrameLevel and head.GetFrameLevel then all:SetFrameLevel(min(10000, (head:GetFrameLevel() or 0) + 2)) end
  local abg = all:CreateTexture(nil, "BACKGROUND")
  abg:SetAllPoints(all)
  local p = colour("panel")
  abg:SetColorTexture(p[1] + 0.05, p[2] + 0.05, p[3] + 0.06, 1)
  local ahl = all:CreateTexture(nil, "HIGHLIGHT")
  ahl:SetAllPoints(all)
  ahl:SetColorTexture(1, 1, 1, 0.08)
  all.text = newFS(all, "GameFontHighlightSmall")
  all.text:SetJustifyH("CENTER")
  all.text:SetPoint("CENTER", all, "CENTER", 0, 0)
  all.text:SetText("All")
  all:SetScript("OnClick", guarded(toggleAll))
  all:SetScript("OnEnter", guarded(function(self)
    local tt = _G.GameTooltip
    if type(tt) ~= "table" or type(tt.SetOwner) ~= "function" then return end
    tt:SetOwner(self, "ANCHOR_RIGHT")
    local S = settings()
    tt:SetText(S.consumesAll and (S.consumesBudget and "Show the 4 Budget picks" or "Show the best 4") or "Show every usable one",
      1, 1, 1)
    tt:Show()
  end))
  all:SetScript("OnLeave", guarded(rowLeave))
  F.allButton = all
  -- Best / Budget, left of All: the strongest, or good value for less gold (gold while Budget is on).
  local mode = make("Button", nil, F)
  mode:SetSize(40, 14)
  mode:SetPoint("TOPRIGHT", all, "TOPLEFT", -4, 0)
  if mode.SetFrameLevel and head.GetFrameLevel then mode:SetFrameLevel(min(10000, (head:GetFrameLevel() or 0) + 2)) end
  local mbg = mode:CreateTexture(nil, "BACKGROUND")
  mbg:SetAllPoints(mode)
  mbg:SetColorTexture(p[1] + 0.05, p[2] + 0.05, p[3] + 0.06, 1)
  local mhl = mode:CreateTexture(nil, "HIGHLIGHT")
  mhl:SetAllPoints(mode)
  mhl:SetColorTexture(1, 1, 1, 0.08)
  mode.text = newFS(mode, "GameFontHighlightSmall")
  mode.text:SetJustifyH("CENTER")
  mode.text:SetPoint("CENTER", mode, "CENTER", 0, 0)
  mode.text:SetText("Best")
  mode:SetScript("OnClick", guarded(toggleBudget))
  mode:SetScript("OnEnter", guarded(function(self)
    local tt = _G.GameTooltip
    if type(tt) ~= "table" or type(tt.SetOwner) ~= "function" then return end
    tt:SetOwner(self, "ANCHOR_RIGHT")
    tt:SetText(MODE_TIP, 1, 1, 1)
    tt:Show()
  end))
  mode:SetScript("OnLeave", guarded(rowLeave))
  F.modeButton = mode
  local bar = F:CreateTexture(nil, "OVERLAY")
  bar:SetColorTexture(mu[1], mu[2], mu[3], 0.7)
  bar:Hide()
  F.scrollBar = bar
  F:SetScript("OnMouseWheel", guarded(onWheel))
  F:SetScript("OnShow", guarded(onShow))
  F:SetScript("OnUpdate", guarded(onUpdate))
  F:SetScript("OnEvent", guarded(onEvent))
  for _, ev in ipairs(EVENTS) do pcall(F.RegisterEvent, F, ev) end   -- one this client lacks is skipped
  if not isOn() then F:Hide() end
  if F:IsVisible() then onShow(F) end
  return true
end

-- The game's UI addon makes PaperDollFrame before ours loads (Blizzard_UIPanels_Game on
-- Forever); if it comes later, the panel is made then, or when the character screen opens.
local cfHooked = false
local function tryBuild()
  if F then return true end
  local ok, r = pcall(build)
  if not ok then
    C.lastError = r
    return true   -- do not try again
  end
  if not r and not cfHooked then
    local cf = _G.CharacterFrame
    if type(cf) == "table" and type(cf.HookScript) == "function" then
      cfHooked = pcall(cf.HookScript, cf, "OnShow", function() if not F then tryBuild() end end)
    end
  end
  return r
end
C.Build = tryBuild
C.Frame = function() return F end

if not tryBuild() and type(_G.CreateFrame) == "function" then
  local ok, f = pcall(_G.CreateFrame, "Frame")
  if ok and type(f) == "table" then
    pcall(f.RegisterEvent, f, "ADDON_LOADED")
    pcall(f.RegisterEvent, f, "PLAYER_LOGIN")
    pcall(f.SetScript, f, "OnEvent", function(self)
      if tryBuild() then pcall(self.UnregisterAllEvents, self) end
    end)
  end
end

---------------------------------------------------------------------------
-- /gl consumes
---------------------------------------------------------------------------
local function setOn(on)
  local S = settings()
  if on then S.consumes = nil else S.consumes = false end
  if F then F:SetShown(on) end
end
C.SetOn = setOn

-- Which picks it shows, in words, with the command for the other.
local function modeWords()
  if settings().consumesBudget then return "Budget: good value for less gold (/gl consumes best for the strongest)" end
  return "Best: the strongest (/gl consumes budget for good value for less gold)"
end

local function setBudget(on)
  settings().consumesBudget = on or nil
  offset = 0
  if F and F:IsVisible() then refresh() end
end
C.SetBudget = setBudget

local function consumesCommand(rest)
  rest = slower(rest or "")
  local on
  if rest == "" or rest == "toggle" then
    on = not isOn()
  elseif rest == "on" or rest == "show" then
    on = true
  elseif rest == "off" or rest == "hide" then
    on = false
  elseif rest == "budget" or rest == "best" then
    setBudget(rest == "budget")
    if rest == "budget" then
      say("the consumes list shows Budget picks: good value for less gold. /gl consumes best for the strongest.")
    else
      say("the consumes list shows the Best ones: the strongest. /gl consumes budget for good value for less gold.")
    end
    return
  elseif rest == "status" then
    say("the consumes list is " .. (isOn() and "on" or "off") .. ". " .. modeWords() .. ".")
    return
  else
    say("unknown choice. Use /gl consumes on, off, best, budget or status (alone: switches it on or off).")
    return
  end
  setOn(on)
  if on then
    say("the consumes list is on: it shows under the character screen.")
  else
    say("the consumes list is off. /gl consumes on brings it back.")
  end
end
C.Command = consumesCommand

local baseSlash = ns.HandleSlash
function ns.HandleSlash(msg)
  local m = type(msg) == "string" and msg or ""
  local cmd, rest = smatch(m, "^%s*(%S*)%s*(.-)%s*$")
  cmd = slower(cmd or "")
  if cmd == "consumes" or cmd == "consume" or cmd == "consumables" then
    local ok, err = pcall(consumesCommand, rest)
    if not ok then
      C.lastError = err
      say("consumes: " .. tostring(err))
    end
    return
  end
  if type(baseSlash) == "function" then baseSlash(msg) end
  if cmd == "enchants" or cmd == "enchant" then needModel = true end   -- a role picked by hand
  if cmd == "help" or cmd == "?" then
    say("/gl consumes : the best food, potions, elixirs and scrolls for you, under the character screen (on or off; "
      .. "best or budget)")
  end
end
