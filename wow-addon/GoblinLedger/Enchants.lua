local ADDON_NAME, ns = ...
-- Goblin Ledger: the best enchants for the gear under the mouse.
--
-- Gear you wear (the character panel's slots; /gl enchants bags on adds bags,
-- the bank and item links): the two best enchants for
-- your role (Top 2), or those and up to three budget options that cost less
-- than the best (Top 5), each with its mats and what they cost at today's AH
-- value. Upgrades you buy and use yourself are options too: Leatherworking
-- armor kits, Engineering scopes, and Blacksmithing spikes and counterweights
-- for a blacksmith. Only upgrades show: an item with the 3rd best gets the best
-- and the 2nd, one with the best gets nothing. A warrior's or rogue's gun, bow
-- or crossbow only pulls: it gets the best scope only, with a grey "(pulling
-- only)". The role comes from your talents; /gl enchants picks one by hand or
-- turns the lines off, and the Enchant tips box on the character panel (with
-- its Top 2 / Top 5 button) turns them off and on again.
--
-- Core.lua calls ns.EnchantLines after its price lines (and the quest line),
-- so these lines are made under a normal-size line and keep the normal font.
-- Plain ASCII only.

ADDON_NAME = ADDON_NAME or "GoblinLedger"
ns = ns or {}

local _G = _G
local type, tonumber, tostring, pairs, ipairs, pcall = type, tonumber, tostring, pairs, ipairs, pcall
local floor = math.floor
local smatch, slower, ssub, sformat = string.match, string.lower, string.sub, string.format
local tconcat = table.concat

local E = { lastError = nil, reads = 0, builds = 0, skillReads = 0 }   -- state for /gl enchants and the tests
ns.Enchants = E

-- BEGIN DATA
-- Generated from the enchant research (enchants.json, 2026-11-18): Forever client
-- tables (wago.tools, wowf 1.60.1) checked against rankedboost's Forever pages.
-- Mats: item id = the game's name.
local MATS = {
  [1705] = "Lesser Moonstone",
  [2775] = "Silver Ore",
  [3478] = "Coarse Grinding Stone",
  [3575] = "Iron Bar",
  [3860] = "Mithril Bar",
  [6037] = "Truesilver Bar",
  [7075] = "Core of Earth",
  [7076] = "Essence of Earth",
  [7077] = "Heart of Fire",
  [7078] = "Essence of Fire",
  [7079] = "Globe of Water",
  [7080] = "Essence of Water",
  [7081] = "Breath of Wind",
  [7082] = "Essence of Air",
  [7966] = "Solid Grinding Stone",
  [7972] = "Ichor of Undeath",
  [8153] = "Wildvine",
  [8411] = "Lung Juice Cocktail",
  [8424] = "Gizzard Gum",
  [8831] = "Purple Lotus",
  [10938] = "Lesser Magic Essence",
  [10940] = "Strange Dust",
  [10978] = "Small Glimmering Shard",
  [10998] = "Lesser Astral Essence",
  [11082] = "Greater Astral Essence",
  [11083] = "Soul Dust",
  [11084] = "Large Glimmering Shard",
  [11134] = "Lesser Mystic Essence",
  [11135] = "Greater Mystic Essence",
  [11137] = "Vision Dust",
  [11138] = "Small Glowing Shard",
  [11139] = "Large Glowing Shard",
  [11174] = "Lesser Nether Essence",
  [11175] = "Greater Nether Essence",
  [11176] = "Dream Dust",
  [11177] = "Small Radiant Shard",
  [11178] = "Large Radiant Shard",
  [11563] = "Crystal Force",
  [11732] = "Libram of Rumination",
  [11733] = "Libram of Constitution",
  [11737] = "Libram of Voracity",
  [11752] = "Black Blood of the Tormented",
  [11754] = "Black Diamond",
  [11951] = "Whipper Root Tuber",
  [11952] = "Night Dragon's Breath",
  [12359] = "Thorium Bar",
  [12644] = "Dense Grinding Stone",
  [12735] = "Frayed Abomination Stitching",
  [12753] = "Skin of Shadow",
  [12803] = "Living Essence",
  [12808] = "Essence of Undeath",
  [12811] = "Righteous Orb",
  [12938] = "Blood of Heroes",
  [14343] = "Small Brilliant Shard",
  [14344] = "Large Brilliant Shard",
  [16202] = "Lesser Eternal Essence",
  [16203] = "Greater Eternal Essence",
  [16204] = "Illusion Dust",
  [18332] = "Libram of Rapidity",
  [18333] = "Libram of Focus",
  [18334] = "Libram of Protection",
  [18335] = "Pristine Black Diamond",
  [20725] = "Nexus Crystal",
  [248819] = "Fel Crystal",
  [248820] = "Torbernite",
  [248822] = "Death Lotus",
  [249274] = "Stranglevine",
  [249391] = "Pyrite",
  [249399] = "Frilled Lichen",
  [249409] = "Cerulean Dye",
  [249410] = "Sulfuric Acid",
  [249424] = "Rosecap",
  [249425] = "Bauxite",
  [249430] = "Magenta Dye",
  [249726] = "Azerothium Bar",
}

-- n = name shown, id = the enchant id in item links (item:<id>:<enchant>:...),
-- m = mats { item id, count, ... }, lvl = level needed to use it, fee = copper paid to the quest giver.
-- Upgrades you buy and use yourself (no enchanter needed): item = its item id (the cost is its AH
-- value), how = the grey line (lw: Leatherworking kit, eng: Engineering scope, bs: Blacksmithing),
-- prof, rank = the profession (skill line id) and rank its item asks for (m = its crafting mats, the
-- cost when the item has no AH value), subs = the only weapon subclasses it fits, ilvl = the lowest
-- item level it goes on (SpellItemEnchantment.ItemLevelMin: "Only usable on items level 35 and above").
local ENCHANTS = {
  -- Lesser Arcanum of Rumination (item 11622, quest 4463, medium): https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1483 https://www.wowhead.com/classic/quest=4463
  a11622 = { n = "Lesser Arcanum of Rumination", id = 1483, m = { 11732, 1, 11754, 1, 11752, 1, 8424, 1 }, lvl = 50, fee = 300000 },
  -- Lesser Arcanum of Constitution (item 11642, quest 4481, medium): https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1503 https://www.wowhead.com/classic/quest=4481
  a11642 = { n = "Lesser Arcanum of Constitution", id = 1503, m = { 11733, 1, 11754, 1, 8411, 1, 11952, 4 }, lvl = 50, fee = 300000 },
  -- Lesser Arcanum of Voracity (item 11645, quest 4484, medium): https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1506 https://www.wowhead.com/classic/quest=4484
  a11645 = { n = "Lesser Arcanum (Str +8)", id = 1506, m = { 11737, 1, 11754, 1, 11951, 4, 11563, 4 }, lvl = 50, fee = 300000 },
  -- Lesser Arcanum of Voracity (item 11647, quest 4484, medium): https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1508 https://www.wowhead.com/classic/quest=4484
  a11647 = { n = "Lesser Arcanum (Agi +8)", id = 1508, m = { 11737, 1, 11754, 1, 11951, 4, 11563, 4 }, lvl = 50, fee = 300000 },
  -- Lesser Arcanum of Voracity (item 11648, quest 4484, medium): https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1509 https://www.wowhead.com/classic/quest=4484
  a11648 = { n = "Lesser Arcanum (Int +8)", id = 1509, m = { 11737, 1, 11754, 1, 11951, 4, 11563, 4 }, lvl = 50, fee = 300000 },
  -- Lydros's Arcanums bind when picked up, so their quests are the only way: quests 7483-7485 need
  -- level 58 in Forever's QuestieDB (field requiredLevel; Classic DBs say 57): https://classicdb.ch/?quest=7483
  -- Arcanum of Rapidity (item 18329, quest 7483, high): https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=2543 https://www.wowhead.com/classic/quest=7483
  a18329 = { n = "Arcanum of Rapidity", id = 2543, m = { 18332, 1, 18335, 1, 14344, 2, 12938, 2 }, lvl = 58 },
  -- Arcanum of Focus (item 18330, quest 7484, high): https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=2544 https://www.wowhead.com/classic/quest=7484
  a18330 = { n = "Arcanum of Focus", id = 2544, m = { 18333, 1, 18335, 1, 14344, 4, 12753, 2 }, lvl = 58 },
  -- Arcanum of Protection (item 18331, quest 7485, high): https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=2545 https://www.wowhead.com/classic/quest=7485
  a18331 = { n = "Arcanum of Protection", id = 2545, m = { 18334, 1, 18335, 1, 14344, 2, 12735, 1 }, lvl = 58 },
  -- Crude Scope (Engineering, item 4405, needs level 5): https://wago.tools/db2/ItemSparse?build=1.60.1.70959&filter[ID]=4405
  i4405 = { n = "Crude Scope +1", id = 30, item = 4405, lvl = 5, how = "eng" },
  -- Standard Scope (Engineering, item 4406, needs level 10): https://wago.tools/db2/ItemSparse?build=1.60.1.70959&filter[ID]=4406
  i4406 = { n = "Standard Scope +2", id = 32, item = 4406, lvl = 10, how = "eng" },
  -- Accurate Scope (Engineering, item 4407, needs level 20): https://wago.tools/db2/ItemSparse?build=1.60.1.70959&filter[ID]=4407
  i4407 = { n = "Accurate Scope +3", id = 33, item = 4407, lvl = 20, how = "eng" },
  -- Deadly Scope (Engineering, item 10546, needs level 30): https://wago.tools/db2/ItemSparse?build=1.60.1.70959&filter[ID]=10546
  i10546 = { n = "Deadly Scope +5", id = 663, item = 10546, lvl = 30, how = "eng" },
  -- Sniper Scope (Engineering, item 10548, needs level 40): https://wago.tools/db2/ItemSparse?build=1.60.1.70959&filter[ID]=10548
  i10548 = { n = "Sniper Scope +7", id = 664, item = 10548, lvl = 40, how = "eng" },
  -- Light Armor Kit (Leatherworking, item 2304, needs level 1): https://wago.tools/db2/ItemSparse?build=1.60.1.70959&filter[ID]=2304 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=15
  k2304 = { n = "Light Armor Kit", id = 15, item = 2304, lvl = 1, how = "lw" },
  -- Medium Armor Kit (Leatherworking, item 2313, needs level 5): https://wago.tools/db2/ItemSparse?build=1.60.1.70959&filter[ID]=2313 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8481
  k2313 = { n = "Medium Armor Kit", id = 8481, item = 2313, lvl = 5, how = "lw" },
  -- Heavy Armor Kit (Leatherworking, item 4265, needs level 20): https://wago.tools/db2/ItemSparse?build=1.60.1.70959&filter[ID]=4265 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8484
  k4265 = { n = "Heavy Armor Kit", id = 8484, item = 4265, lvl = 20, how = "lw", ilvl = 15 },
  -- Thick Armor Kit (Leatherworking, item 8173, needs level 30): https://wago.tools/db2/ItemSparse?build=1.60.1.70959&filter[ID]=8173 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8487
  k8173 = { n = "Thick Armor Kit", id = 8487, item = 8173, lvl = 30, how = "lw", ilvl = 25 },
  -- Rugged Armor Kit (Leatherworking, item 15564, needs level 40): https://wago.tools/db2/ItemSparse?build=1.60.1.70959&filter[ID]=15564 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8490
  k15564 = { n = "Rugged Armor Kit", id = 8490, item = 15564, lvl = 40, how = "lw", ilvl = 35 },
  -- Mystic Medium Armor Kit (Leatherworking, item 252436, needs level 15): https://wago.tools/db2/ItemSparse?build=1.60.1.70959&filter[ID]=252436 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8482
  k252436 = { n = "Mystic Medium Armor Kit", id = 8482, item = 252436, lvl = 15, how = "lw" },
  -- Forceful Medium Armor Kit (Leatherworking, item 252437, needs level 15): https://wago.tools/db2/ItemSparse?build=1.60.1.70959&filter[ID]=252437 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8483
  k252437 = { n = "Forceful Medium Armor Kit", id = 8483, item = 252437, lvl = 15, how = "lw" },
  -- Mystic Heavy Armor Kit (Leatherworking, item 252452, needs level 30): https://wago.tools/db2/ItemSparse?build=1.60.1.70959&filter[ID]=252452 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8485
  k252452 = { n = "Mystic Heavy Armor Kit", id = 8485, item = 252452, lvl = 30, how = "lw", ilvl = 15 },
  -- Forceful Heavy Armor Kit (Leatherworking, item 252453, needs level 30): https://wago.tools/db2/ItemSparse?build=1.60.1.70959&filter[ID]=252453 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8486
  k252453 = { n = "Forceful Heavy Armor Kit", id = 8486, item = 252453, lvl = 30, how = "lw", ilvl = 15 },
  -- Mystic Thick Armor Kit (Leatherworking, item 252462, needs level 40): https://wago.tools/db2/ItemSparse?build=1.60.1.70959&filter[ID]=252462 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8489
  k252462 = { n = "Mystic Thick Armor Kit", id = 8489, item = 252462, lvl = 40, how = "lw", ilvl = 25 },
  -- Forceful Thick Armor Kit (Leatherworking, item 252463, needs level 40): https://wago.tools/db2/ItemSparse?build=1.60.1.70959&filter[ID]=252463 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8488
  k252463 = { n = "Forceful Thick Armor Kit", id = 8488, item = 252463, lvl = 40, how = "lw", ilvl = 25 },
  -- Mystic Rugged Armor Kit (Leatherworking, item 252487, needs level 50): https://wago.tools/db2/ItemSparse?build=1.60.1.70959&filter[ID]=252487 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8492
  k252487 = { n = "Mystic Rugged Armor Kit", id = 8492, item = 252487, lvl = 50, how = "lw", ilvl = 35 },
  -- Forceful Rugged Armor Kit (Leatherworking, item 252488, needs level 50): https://wago.tools/db2/ItemSparse?build=1.60.1.70959&filter[ID]=252488 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8491
  k252488 = { n = "Forceful Rugged Armor Kit", id = 8491, item = 252488, lvl = 50, how = "lw", ilvl = 35 },
  -- Iron Shield Spike (Blacksmithing 150 to use it; made by spell 7221): https://wago.tools/db2/ItemSparse?build=1.60.1.70959&filter[ID]=6042 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=43
  b6042 = { n = "Iron Shield Spike", id = 43, item = 6042, prof = 164, rank = 150, m = { 3575, 6, 3478, 4 }, how = "bs" },
  -- Iron Counterweight (Blacksmithing 165 to use it; made by spell 7222): https://wago.tools/db2/ItemSparse?build=1.60.1.70959&filter[ID]=6043 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=34
  b6043 = { n = "Iron Counterweight", id = 34, item = 6043, prof = 164, rank = 165, m = { 3575, 4, 3478, 2, 1705, 1 }, how = "bs", subs = { 1, 5, 6, 8 } },
  -- Mithril Shield Spike (Blacksmithing 215 to use it; made by spell 9939): https://wago.tools/db2/ItemSparse?build=1.60.1.70959&filter[ID]=7967 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=463
  b7967 = { n = "Mithril Shield Spike", id = 463, item = 7967, prof = 164, rank = 215, m = { 3860, 4, 6037, 2, 7966, 4 }, how = "bs" },
  -- Thorium Shield Spike (Blacksmithing 250 to use it; made by spell 16651): https://wago.tools/db2/ItemSparse?build=1.60.1.70959&filter[ID]=12645 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1704
  b12645 = { n = "Thorium Shield Spike", id = 1704, item = 12645, prof = 164, rank = 250, m = { 12359, 4, 12644, 4, 7076, 2 }, how = "bs" },
  -- Enchant Cloak - Minor Resistance (spell 7454, high): https://rankedboost.com/world-of-warcraft/forever/enchants/cloak/#e-recipe-7454 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=65
  s7454 = { n = "Minor Resistance +2", id = 65, m = { 10940, 1, 10938, 1 } },
  -- Enchant Bracer - Minor Stamina (spell 7457, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-7457 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=66
  s7457 = { n = "Minor Stamina +3", id = 66, m = { 10940, 2 } },
  -- Enchant 2H Weapon - Minor Impact (spell 7745, trainer, high): https://rankedboost.com/world-of-warcraft/forever/enchants/two-handed-weapon/#e-recipe-7745 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=805
  -- On Forever it gives enchant 805 (Weapon Damage +4, the id Greater Striking gives) for two cheap mats.
  s7745 = { n = "Minor Impact +4", id = 805, m = { 10940, 1, 10978, 1 } },
  -- Enchant Chest - Lesser Intellect (spell 7776, high): https://rankedboost.com/world-of-warcraft/forever/enchants/chest/#e-recipe-7776 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=246
  s7776 = { n = "Lesser Intellect +3", id = 246, m = { 10938, 2 } },
  -- Enchant Bracer - Minor Agility (spell 7779, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-7779 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=247
  s7779 = { n = "Minor Agility +3", id = 247, m = { 10940, 1, 10938, 2 } },
  -- Enchant Bracer - Minor Strength (spell 7782, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-7782 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=248
  s7782 = { n = "Minor Strength +3", id = 248, m = { 10940, 3 } },
  -- Enchant Weapon - Minor Striking (spell 7788, high): https://rankedboost.com/world-of-warcraft/forever/enchants/weapon/#e-recipe-7788 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=250
  s7788 = { n = "Minor Striking +1", id = 250, m = { 10940, 1, 10938, 1, 10978, 1 } },
  -- Enchant 2H Weapon - Lesser Intellect (spell 7793, high): https://rankedboost.com/world-of-warcraft/forever/enchants/two-handed-weapon/#e-recipe-7793 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=723
  s7793 = { n = "Lesser Intellect +5", id = 723, m = { 10938, 4 } },
  -- Enchant Chest - Stamina (spell 7857, high): https://rankedboost.com/world-of-warcraft/forever/enchants/chest/#e-recipe-7857 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=254
  s7857 = { n = "Stamina +4", id = 254, m = { 10940, 2, 10998, 1 } },
  -- Enchant Boots - Minor Agility (spell 7867, high): https://rankedboost.com/world-of-warcraft/forever/enchants/boots/#e-recipe-7867 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=247
  s7867 = { n = "Minor Agility +3", id = 247, m = { 10940, 3, 10998, 1 } },
  -- Enchant Shield - Minor Stamina (spell 13378, high): https://rankedboost.com/world-of-warcraft/forever/enchants/shield/#e-recipe-13378 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=66
  s13378 = { n = "Minor Stamina +3", id = 66, m = { 10998, 1, 10940, 1 } },
  -- Enchant 2H Weapon - Lesser Spirit (spell 13380, high): https://rankedboost.com/world-of-warcraft/forever/enchants/two-handed-weapon/#e-recipe-13380 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=851
  s13380 = { n = "Lesser Spirit +5", id = 851, m = { 10998, 1, 10940, 3 } },
  -- Enchant Cloak - Minor Agility (spell 13419, high): https://rankedboost.com/world-of-warcraft/forever/enchants/cloak/#e-recipe-13419 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=247
  s13419 = { n = "Minor Agility +3", id = 247, m = { 10998, 1 } },
  -- Enchant Cloak - Lesser Protection (spell 13421, high): https://rankedboost.com/world-of-warcraft/forever/enchants/cloak/#e-recipe-13421 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=744
  s13421 = { n = "Lesser Protection +20", id = 744, m = { 10940, 3, 10978, 1 } },
  -- Enchant Bracer - Lesser Stamina (spell 13501, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-13501 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=254
  s13501 = { n = "Lesser Stamina +4", id = 254, m = { 11083, 1 } },
  -- Enchant Weapon - Lesser Striking (spell 13503, high): https://rankedboost.com/world-of-warcraft/forever/enchants/weapon/#e-recipe-13503 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=241
  s13503 = { n = "Lesser Striking +2", id = 241, m = { 11083, 1, 11084, 1 } },
  -- Enchant 2H Weapon - Lesser Impact (spell 13529, trainer, high): https://rankedboost.com/world-of-warcraft/forever/enchants/two-handed-weapon/#e-recipe-13529 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1897
  -- Enchant 1897 (Weapon Damage +5) for Lesser Striking's mats (+2): on a 2H weapon it replaces Lesser Striking.
  s13529 = { n = "Lesser Impact +5", id = 1897, m = { 11083, 1, 11084, 1 } },
  -- Enchant Bracer - Lesser Strength (spell 13536, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-13536 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=823
  s13536 = { n = "Lesser Strength +4", id = 823, m = { 11083, 1 } },
  -- Enchant Chest - Intellect (spell 13607, high): https://rankedboost.com/world-of-warcraft/forever/enchants/chest/#e-recipe-13607 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=843
  s13607 = { n = "Intellect +4", id = 843, m = { 11082, 1 } },
  -- Enchant Chest - Minor Stats (spell 13626, high): https://rankedboost.com/world-of-warcraft/forever/enchants/chest/#e-recipe-13626 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=847
  s13626 = { n = "Minor Stats +1", id = 847, m = { 10998, 1, 11083, 1, 11084, 1 } },
  -- Enchant Shield - Lesser Stamina (spell 13631, high): https://rankedboost.com/world-of-warcraft/forever/enchants/shield/#e-recipe-13631 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=852
  s13631 = { n = "Lesser Stamina +5", id = 852, m = { 11134, 1 } },
  -- Enchant Cloak - Defense (spell 13635, high): https://rankedboost.com/world-of-warcraft/forever/enchants/cloak/#e-recipe-13635 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=848
  s13635 = { n = "Defense +30", id = 848, m = { 11138, 1, 11083, 1 } },
  -- Enchant Boots - Lesser Agility (spell 13637, high): https://rankedboost.com/world-of-warcraft/forever/enchants/boots/#e-recipe-13637 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8204
  s13637 = { n = "Lesser Agility +4", id = 8204, m = { 11083, 1 } },
  -- Enchant Chest - Greater Stamina (spell 13640, high): https://rankedboost.com/world-of-warcraft/forever/enchants/chest/#e-recipe-13640 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=850
  s13640 = { n = "Greater Stamina +6", id = 850, m = { 11083, 2 } },
  -- Enchant Boots - Lesser Stamina (spell 13644, high): https://rankedboost.com/world-of-warcraft/forever/enchants/boots/#e-recipe-13644 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=254
  s13644 = { n = "Lesser Stamina +4", id = 254, m = { 11083, 2 } },
  -- Enchant Bracer - Stamina (spell 13648, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-13648 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=852
  s13648 = { n = "Stamina +5", id = 852, m = { 11083, 3 } },
  -- Enchant Bracer - Strength (spell 13661, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-13661 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=856
  s13661 = { n = "Strength +5", id = 856, m = { 11137, 1 } },
  -- Enchant Chest - Greater Intellect (spell 13663, high): https://rankedboost.com/world-of-warcraft/forever/enchants/chest/#e-recipe-13663 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=857
  s13663 = { n = "Greater Intellect +6", id = 857, m = { 11134, 1 } },
  -- Enchant Boots - Lesser Spirit (spell 13687, high): https://rankedboost.com/world-of-warcraft/forever/enchants/boots/#e-recipe-13687 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8203
  s13687 = { n = "Lesser Spirit +4", id = 8203, m = { 11134, 2 } },
  -- Enchant Weapon - Striking (spell 13693, high): https://rankedboost.com/world-of-warcraft/forever/enchants/weapon/#e-recipe-13693 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=943
  s13693 = { n = "Striking +3", id = 943, m = { 11135, 1, 11139, 1 } },
  -- Enchant 2H Weapon - Impact (spell 13695, high): https://rankedboost.com/world-of-warcraft/forever/enchants/two-handed-weapon/#e-recipe-13695 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8205
  s13695 = { n = "Impact +6", id = 8205, m = { 11137, 2, 11139, 1 } },
  -- Enchant Chest - Lesser Stats (spell 13700, high): https://rankedboost.com/world-of-warcraft/forever/enchants/chest/#e-recipe-13700 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=866
  s13700 = { n = "Lesser Stats +2", id = 866, m = { 11135, 1, 11137, 1, 11139, 1 } },
  -- Enchant Cloak - Greater Defense (spell 13746, high): https://rankedboost.com/world-of-warcraft/forever/enchants/cloak/#e-recipe-13746 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=884
  s13746 = { n = "Greater Defense +60", id = 884, m = { 11137, 1 } },
  -- Enchant Cloak - Resistance (spell 13794, high): https://rankedboost.com/world-of-warcraft/forever/enchants/cloak/#e-recipe-13794 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=903
  s13794 = { n = "Resistance +3", id = 903, m = { 11174, 1 } },
  -- Enchant Gloves - Agility (spell 13815, high): https://rankedboost.com/world-of-warcraft/forever/enchants/gloves/#e-recipe-13815 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1887
  s13815 = { n = "Agility +7", id = 1887, m = { 11137, 1 } },
  -- Enchant Shield - Stamina (spell 13817, high): https://rankedboost.com/world-of-warcraft/forever/enchants/shield/#e-recipe-13817 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=929
  s13817 = { n = "Stamina +7", id = 929, m = { 11137, 2 } },
  -- Enchant Bracer - Intellect (spell 13822, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-13822 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=905
  s13822 = { n = "Intellect +5", id = 905, m = { 11174, 1 } },
  -- Enchant Boots - Stamina (spell 13836, high): https://rankedboost.com/world-of-warcraft/forever/enchants/boots/#e-recipe-13836 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=852
  s13836 = { n = "Stamina +5", id = 852, m = { 11137, 2 } },
  -- Enchant Chest - Superior Stamina (spell 13858, high): https://rankedboost.com/world-of-warcraft/forever/enchants/chest/#e-recipe-13858 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=908
  s13858 = { n = "Superior Stamina +8", id = 908, m = { 11137, 3 } },
  -- Enchant Gloves - Strength (spell 13887, high): https://rankedboost.com/world-of-warcraft/forever/enchants/gloves/#e-recipe-13887 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=927
  s13887 = { n = "Strength +7", id = 927, m = { 11174, 1, 11137, 2 } },
  -- Enchant Weapon - Fiery Weapon (spell 13898, high; formula 11207 drops from Pyromancer Loregrain in Blackrock Depths, on the AH): https://rankedboost.com/world-of-warcraft/forever/enchants/weapon/#e-recipe-13898 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=803
  s13898 = { n = "Fiery Weapon", id = 803, m = { 11177, 2, 7078, 1 } },
  -- Enchant Chest - Superior Intellect (spell 13917, high): https://rankedboost.com/world-of-warcraft/forever/enchants/chest/#e-recipe-13917 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=913
  s13917 = { n = "Superior Intellect +8", id = 913, m = { 11175, 1 } },
  -- Enchant Bracer - Deflection (spell 13931, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-13931 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=923
  s13931 = { n = "Deflection +7", id = 923, m = { 11174, 2, 11176, 1 } },
  -- Enchant Boots - Agility (spell 13935, high): https://rankedboost.com/world-of-warcraft/forever/enchants/boots/#e-recipe-13935 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=904
  s13935 = { n = "Agility +5", id = 904, m = { 11175, 1 } },
  -- Enchant 2H Weapon - Greater Impact (spell 13937, high): https://rankedboost.com/world-of-warcraft/forever/enchants/two-handed-weapon/#e-recipe-13937 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=963
  s13937 = { n = "Greater Impact +7", id = 963, m = { 11178, 1, 11176, 1 } },
  -- Enchant Bracer - Greater Strength (spell 13939, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-13939 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=927
  s13939 = { n = "Greater Strength +7", id = 927, m = { 11176, 1, 11174, 2 } },
  -- Enchant Chest - Stats (spell 13941, high): https://rankedboost.com/world-of-warcraft/forever/enchants/chest/#e-recipe-13941 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=928
  s13941 = { n = "Stats +3", id = 928, m = { 11178, 1, 11176, 1, 11175, 1 } },
  -- Enchant Weapon - Greater Striking (spell 13943, high): https://rankedboost.com/world-of-warcraft/forever/enchants/weapon/#e-recipe-13943 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=805
  s13943 = { n = "Greater Striking +4", id = 805, m = { 11178, 1, 11175, 1 } },
  -- Enchant Bracer - Greater Stamina (spell 13945, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-13945 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=929
  s13945 = { n = "Greater Stamina +7", id = 929, m = { 11176, 3 } },
  -- Enchant Gloves - Minor Haste (spell 13948, high): https://rankedboost.com/world-of-warcraft/forever/enchants/gloves/#e-recipe-13948 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=931
  s13948 = { n = "Minor Haste +1%", id = 931, m = { 11178, 1, 8153, 1 } },
  -- Enchant Bracer - Greater Intellect (spell 20008, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-20008 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1883
  s20008 = { n = "Greater Intellect +7", id = 1883, m = { 16202, 2 } },
  -- Enchant Bracer - Superior Strength (spell 20010, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-20010 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1885
  s20010 = { n = "Superior Strength +9", id = 1885, m = { 16204, 6, 16203, 6 } },
  -- Enchant Bracer - Superior Stamina (spell 20011, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-20011 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1886
  s20011 = { n = "Superior Stamina +9", id = 1886, m = { 16204, 15 } },
  -- Enchant Gloves - Greater Agility (spell 20012, high): https://rankedboost.com/world-of-warcraft/forever/enchants/gloves/#e-recipe-20012 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8206
  s20012 = { n = "Greater Agility +10", id = 8206, m = { 16202, 2, 16204, 2 } },
  -- Enchant Gloves - Greater Strength (spell 20013, high): https://rankedboost.com/world-of-warcraft/forever/enchants/gloves/#e-recipe-20013 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8207
  s20013 = { n = "Greater Strength +10", id = 8207, m = { 16203, 4, 16204, 4 } },
  -- Enchant Cloak - Greater Resistance (spell 20014, high): https://rankedboost.com/world-of-warcraft/forever/enchants/cloak/#e-recipe-20014 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1888
  s20014 = { n = "Greater Resistance +5", id = 1888, m = { 16202, 1, 7077, 1, 7075, 1, 7079, 1, 7081, 1, 7972, 1 } },
  -- Enchant Cloak - Superior Defense (spell 20015, high): https://rankedboost.com/world-of-warcraft/forever/enchants/cloak/#e-recipe-20015 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1889
  s20015 = { n = "Superior Defense +70", id = 1889, m = { 16204, 8 } },
  -- Enchant Shield - Greater Stamina (spell 20017, high): https://rankedboost.com/world-of-warcraft/forever/enchants/shield/#e-recipe-20017 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1886
  s20017 = { n = "Greater Stamina +9", id = 1886, m = { 11176, 6 } },
  -- Enchant Boots - Greater Stamina (spell 20020, high): https://rankedboost.com/world-of-warcraft/forever/enchants/boots/#e-recipe-20020 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=929
  s20020 = { n = "Greater Stamina +7", id = 929, m = { 11176, 6 } },
  -- Enchant Boots - Greater Agility (spell 20023, high): https://rankedboost.com/world-of-warcraft/forever/enchants/boots/#e-recipe-20023 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1887
  s20023 = { n = "Greater Agility +7", id = 1887, m = { 16203, 8 } },
  -- Enchant Boots - Spirit (spell 20024, high): https://rankedboost.com/world-of-warcraft/forever/enchants/boots/#e-recipe-20024 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=851
  s20024 = { n = "Spirit +5", id = 851, m = { 16203, 2, 16202, 1 } },
  -- Enchant Chest - Greater Stats (spell 20025, high): https://rankedboost.com/world-of-warcraft/forever/enchants/chest/#e-recipe-20025 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1891
  s20025 = { n = "Greater Stats +4", id = 1891, m = { 14344, 4, 16204, 15, 16203, 10 } },
  -- Enchant Chest - Major Stamina (spell 20026, high): https://rankedboost.com/world-of-warcraft/forever/enchants/chest/#e-recipe-20026 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1892
  s20026 = { n = "Major Stamina +10", id = 1892, m = { 16204, 6, 14343, 1 } },
  -- Enchant Chest - Major Intellect (spell 20028, high): https://rankedboost.com/world-of-warcraft/forever/enchants/chest/#e-recipe-20028 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1893
  s20028 = { n = "Major Intellect +10", id = 1893, m = { 16203, 3, 14343, 1 } },
  -- Enchant 2H Weapon - Superior Impact (spell 20030, high): https://rankedboost.com/world-of-warcraft/forever/enchants/two-handed-weapon/#e-recipe-20030 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1896
  s20030 = { n = "Superior Impact +9", id = 1896, m = { 14344, 4, 16204, 10 } },
  -- Enchant Weapon - Superior Striking (spell 20031, high; formula 16250 drops in Blackrock Spire): https://rankedboost.com/world-of-warcraft/forever/enchants/weapon/#e-recipe-20031 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1897
  s20031 = { n = "Superior Striking +5", id = 1897, m = { 14344, 2, 16203, 10 } },
  -- Enchant Weapon - Lifestealing (spell 20032, high; formula 16254 drops in Scholomance, on the AH): https://rankedboost.com/world-of-warcraft/forever/enchants/weapon/#e-recipe-20032 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1898
  s20032 = { n = "Lifestealing", id = 1898, m = { 14344, 6, 12808, 6, 12803, 6 } },
  -- Enchant Weapon - Crusader (spell 20034, high; formula 16252 drops from Scarlet Spellbinder, on the AH): https://rankedboost.com/world-of-warcraft/forever/enchants/weapon/#e-recipe-20034 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1900
  s20034 = { n = "Crusader", id = 1900, m = { 14344, 4, 12811, 2 } },
  -- Enchant 2H Weapon - Major Spirit (spell 20035, high): https://rankedboost.com/world-of-warcraft/forever/enchants/two-handed-weapon/#e-recipe-20035 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1903
  s20035 = { n = "Major Spirit +9", id = 1903, m = { 16203, 12, 14344, 2 } },
  -- Enchant 2H Weapon - Major Intellect (spell 20036, high): https://rankedboost.com/world-of-warcraft/forever/enchants/two-handed-weapon/#e-recipe-20036 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1904
  s20036 = { n = "Major Intellect +9", id = 1904, m = { 16203, 12, 14344, 2 } },
  -- Enchant Weapon - Strength (spell 23799, high): https://rankedboost.com/world-of-warcraft/forever/enchants/weapon/#e-recipe-23799 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=2563
  s23799 = { n = "Strength +15", id = 2563, m = { 14344, 6, 16203, 6, 16204, 4, 7076, 2 } },
  -- Enchant Weapon - Agility (spell 23800, high): https://rankedboost.com/world-of-warcraft/forever/enchants/weapon/#e-recipe-23800 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=2564
  s23800 = { n = "Agility +15", id = 2564, m = { 14344, 6, 16203, 6, 16204, 4, 7082, 2 } },
  -- Enchant Bracer - Healing Power (spell 23802, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-23802 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=2566
  s23802 = { n = "Healing Power +24", id = 2566, m = { 14344, 2, 16204, 20, 16203, 4, 12803, 6 } },
  -- Enchant Weapon - Mighty Spirit (spell 23803, high): https://rankedboost.com/world-of-warcraft/forever/enchants/weapon/#e-recipe-23803 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=2567
  s23803 = { n = "Mighty Spirit +22", id = 2567, m = { 14344, 10, 16203, 8, 16204, 15 } },
  -- Enchant Weapon - Mighty Intellect (spell 23804, high): https://rankedboost.com/world-of-warcraft/forever/enchants/weapon/#e-recipe-23804 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=2568
  s23804 = { n = "Mighty Intellect +22", id = 2568, m = { 14344, 15, 16203, 12, 16204, 20 } },
  -- Enchant 2H Weapon - Agility (spell 27837, high): https://rankedboost.com/world-of-warcraft/forever/enchants/two-handed-weapon/#e-recipe-27837 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=2646
  s27837 = { n = "Agility +25", id = 2646, m = { 14344, 10, 16203, 6, 16204, 14, 7082, 4 } },
  -- Enchant Gloves - Arcane Power (spell 1213626, high): https://rankedboost.com/world-of-warcraft/forever/enchants/gloves/#e-recipe-1213626 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=7647
  s1213626 = { n = "Arcane Power +20", id = 7647, m = { 20725, 2, 14344, 5, 12753, 2 } },
  -- Enchant Off-Hand - Wisdom (spell 1219579, high): https://rankedboost.com/world-of-warcraft/forever/enchants/off-hand/#e-recipe-1219579 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=7661
  s1219579 = { n = "Wisdom", id = 7661, m = { 14344, 6, 16204, 18, 7082, 1, 7080, 1 } },
  -- Enchant 2H Weapon - Spellblasting (spell 1219580, high): https://rankedboost.com/world-of-warcraft/forever/enchants/two-handed-weapon/#e-recipe-1219580 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=7662
  s1219580 = { n = "Spellblasting +65", id = 7662, m = { 20725, 3, 16204, 24, 12938, 1, 8831, 6 } },
  -- Enchant Cloak - Agility (spell 1219587, high): https://rankedboost.com/world-of-warcraft/forever/enchants/cloak/#e-recipe-1219587 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=7667
  s1219587 = { n = "Agility +5", id = 7667, m = { 16203, 3, 16204, 9, 7078, 1 } },
  -- Enchant Bracer - Greater Spellpower (spell 1220624, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-1220624 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=7665
  s1220624 = { n = "Greater Spellpower +16", id = 7665, m = { 16203, 3, 16204, 9, 12808, 12, 8831, 2 } },
  -- Enchant Bracer - Minor Intellect (spell 1248458, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-1248458 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=246
  s1248458 = { n = "Minor Intellect +3", id = 246, m = { 10938, 1 } },
  -- Enchant Bracer - Minor Healing Power (spell 1248459, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-1248459 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8208
  s1248459 = { n = "Minor Healing Power +8", id = 8208, m = { 10938, 2 } },
  -- Enchant Bracer - Lesser Agility (spell 1248460, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-1248460 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8204
  s1248460 = { n = "Lesser Agility +4", id = 8204, m = { 11134, 1 } },
  -- Enchant Bracer - Agility (spell 1248497, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-1248497 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=904
  s1248497 = { n = "Agility +5", id = 904, m = { 11137, 1 } },
  -- Enchant Bracer - Lesser Healing Power (spell 1248498, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-1248498 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8209
  s1248498 = { n = "Lesser Healing Power +16", id = 8209, m = { 11134, 2 } },
  -- Enchant Bracer - Greater Agility (spell 1248500, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-1248500 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1887
  s1248500 = { n = "Greater Agility +7", id = 1887, m = { 11176, 3, 11174, 1 } },
  -- Enchant 2H Weapon - Lesser Agility (spell 1248510, low): https://rankedboost.com/world-of-warcraft/forever/enchants/two-handed-weapon/#e-recipe-1248510 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=2618
  -- No number: the label says Agility +15, its aura (spell 19989) gives +9. Either way it ranks below Agility +15 (2564).
  s1248510 = { n = "Lesser Agility", id = 2618, m = { 11137, 2, 11139, 1, 11174, 1 } },
  -- Enchant 2H Weapon - Lesser Strength (spell 1248511, high): https://rankedboost.com/world-of-warcraft/forever/enchants/two-handed-weapon/#e-recipe-1248511 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=2563
  s1248511 = { n = "Lesser Strength +15", id = 2563, m = { 11137, 1, 11139, 1, 11174, 2 } },
  -- Enchant Bracer - Superior Agility (spell 1248599, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-1248599 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=7656
  s1248599 = { n = "Superior Agility +9", id = 7656, m = { 16204, 3, 16203, 3, 14344, 1 } },
  -- Enchant 2H Weapon - Mighty Spell Power (spell 1248607, high): https://rankedboost.com/world-of-warcraft/forever/enchants/two-handed-weapon/#e-recipe-1248607 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8211
  s1248607 = { n = "Mighty Spell Power +55", id = 8211, m = { 248819, 4, 16203, 6, 7076, 4, 12808, 4, 20725, 4, 248820, 2 } },
  -- Enchant 2H Weapon - Mighty Healing Power (spell 1248636, high): https://rankedboost.com/world-of-warcraft/forever/enchants/two-handed-weapon/#e-recipe-1248636 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8212
  s1248636 = { n = "Mighty Healing Power +100", id = 8212, m = { 248819, 4, 16203, 6, 249726, 4, 249274, 4, 20725, 4, 248820, 4 } },
  -- Enchant Gloves - Superior Strength (spell 1248640, high): https://rankedboost.com/world-of-warcraft/forever/enchants/gloves/#e-recipe-1248640 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=2563
  s1248640 = { n = "Superior Strength +15", id = 2563, m = { 20725, 4, 16203, 8, 7076, 4 } },
  -- Enchant Gloves - Natural Power (spell 1248645, high): https://rankedboost.com/world-of-warcraft/forever/enchants/gloves/#e-recipe-1248645 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8213
  s1248645 = { n = "Natural Power +20", id = 8213, m = { 20725, 2, 14344, 5, 248822, 4 } },
  -- Enchant Bracer - Superior Intellect (spell 1248661, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-1248661 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1901
  s1248661 = { n = "Superior Intellect +9", id = 1901, m = { 16203, 10 } },
  -- Enchant Bracer - Superior Deflection (spell 1248665, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-1248665 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8214
  s1248665 = { n = "Superior Deflection +9", id = 8214, m = { 14344, 3 } },
  -- Enchant Weapon - Insight (spell 1248757, high): https://rankedboost.com/world-of-warcraft/forever/enchants/weapon/#e-recipe-1248757 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8216
  s1248757 = { n = "Insight", id = 8216, m = { 10940, 10, 11084, 4, 11082, 4, 249391, 4, 249399, 4 } },
  -- Enchant Weapon - Recovery (spell 1248760, high): https://rankedboost.com/world-of-warcraft/forever/enchants/weapon/#e-recipe-1248760 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8721
  s1248760 = { n = "Recovery", id = 8721, m = { 11083, 6, 11084, 4, 11082, 6, 249391, 4, 2775, 4 } },
  -- Enchant Weapon - Revelation (spell 1248805, high): https://rankedboost.com/world-of-warcraft/forever/enchants/weapon/#e-recipe-1248805 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8217
  s1248805 = { n = "Revelation", id = 8217, m = { 11083, 8, 11084, 6, 11082, 2, 249409, 4, 249410, 4 } },
  -- Enchant Necklace - Strength (spell 1249019, high): https://rankedboost.com/world-of-warcraft/forever/enchants/necklace/#e-recipe-1249019 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=856
  s1249019 = { n = "Strength +5", id = 856, m = { 11175, 4, 11178, 4, 11176, 10, 249410, 4, 249425, 4 } },
  -- Enchant Necklace - Spell Power (spell 1249057, high): https://rankedboost.com/world-of-warcraft/forever/enchants/necklace/#e-recipe-1249057 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8218
  s1249057 = { n = "Spell Power +6", id = 8218, m = { 11175, 5, 11178, 3, 11176, 8, 249409, 4, 249430, 4 } },
  -- Enchant Necklace - Healing Power (spell 1249058, high): https://rankedboost.com/world-of-warcraft/forever/enchants/necklace/#e-recipe-1249058 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8219
  s1249058 = { n = "Healing Power +11", id = 8219, m = { 11175, 3, 11178, 5, 11176, 4, 249391, 4, 249424, 4 } },
  -- Enchant Necklace - Agility (spell 1249059, high): https://rankedboost.com/world-of-warcraft/forever/enchants/necklace/#e-recipe-1249059 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=904
  s1249059 = { n = "Agility +5", id = 904, m = { 11175, 2, 11178, 8, 11176, 6, 249410, 4, 249430, 4 } },
  -- Not shown: Enchant Necklace - Deflection (spell 1249060, enchant 925). Its label says Defense +5, but
  -- its aura (spell 13930) gives +2 (rankedboost leaves the value out). Until that is checked in game, tanks
  -- get Strength +5 and Agility +5, and a necklace with Deflection on is left alone (up = { 925 }).
}

-- Best first, in value order for the role (rankedboost's Forever stat weights, averaged over the
-- role's specs): an item shows the entries above the enchant it has (nothing when it has the first),
-- as far as the player's level, professions and the item's level allow. up = enchant ids at least as
-- good as the first (nothing is shown); at = { [id] = n }: an enchant not in the list that ranks just
-- above entry n (entries 1 to n - 1 are shown). Any other enchant: the whole list (it is worth less).
-- Belts, shoulders, rings, trinkets: nothing (Forever has no belt buckle, and its Engineering belt
-- tinkers need schematics and parts that are not in the game).
-- Weapons: "Enchant Weapon" recipes fit every melee weapon, two-handers too (SpellEquippedItems), so
-- the 2H lists weigh Fiery Weapon, Lifestealing, Crusader and the Striking ids against the Impact ones.
-- A proc counts what it adds a second, 14 attack power for each damage a second (the weights' unit):
-- Fiery Weapon 40 fire (spell 13897) about 6 times a minute (Forever's tables store no rate for it;
-- rankedboost's players measure about 6) at 96% spell hit = 3.84 a second, 54 AP; Lifestealing 30
-- (spell 20004) 6 a minute = 2.88, 40 AP (it heals as much); Crusader +100 Strength for 15 sec
-- (spell 20007) once a minute, up 22% of the time = +22 Strength, 45 AP. A proc a minute adds the same
-- on any weapon, +N weapon damage adds N / speed: on a 3.6 sec 2H Fiery Weapon is worth +13.8 weapon
-- damage (more than Superior Impact +9), on a 2.6 sec 1H +10 (Superior Striking is +5). The 2H order
-- holds for 2H speeds 2.9 to 3.9 sec, the 1H one for 2.3 to 2.9 sec. Daggers are faster (most of
-- Forever's are 1.5 to 2.0 sec), so a dagger you can hold in the main hand has its own lists
-- (role/dagger, in order for 1.7 to 2.0 sec: Greater Striking +4 beats Crusader for a rogue there).
-- An off-hand-only weapon keeps the 1H list: off-hand hits do half damage (5/8 with Dual Wield
-- Specialization), so a fast off-hand's weapon damage is worth what a 2.6 sec main hand's is.
-- Crusader's rate decides it against Fiery Weapon: at the 1 a minute used here Fiery Weapon leads by
-- about 19% on mobs of your level; from 1.2 a minute (or against raid bosses, who resist spells) the
-- two are level. Fiery Weapon stays first: as good or better, and its mats cost far less.
-- The 2H tank list is mostly for bears (a druid's tank role on a staff): weapon damage counts 0 there.
-- Enchant: Fiery Blaze (36, quest
-- 706's reward) hits 15% of swings (its own table entry) for 9 to 13 fire: 0.47 a second on a 3.4 sec
-- 2H (+1.6 weapon damage), so every 2H entry beats it; on a 1H it beats Minor Striking +1 only (a
-- tank's Lesser Striking +2).
-- Icy Chill, Unholy Weapon, Demonslaying and the Steel Weapon Chain deal no damage to most targets: not
-- listed (an item with one gets the whole list). Lesser Striking +2 costs what Lesser Impact +5 does.
-- Ranged for a warrior or rogue (pull = true): the best scope they can use, in Top 2 and Top 5, with a
-- grey "(pulling only)". A hunter who picks a role by hand keeps the hunter's scopes (role/HUNTER).
-- Scopes ask their item's level, not Engineering (ItemSparse.RequiredSkill 0), and fit bows, guns and
-- crossbows only (SpellEquippedItems): thrown weapons and wands get nothing.
local PICKS = {
  weapon = {
    ["melee"] = { "s13898", "s20034", "s20032", "s23799", "s20031", "s13943", "s13693", "s13503", "s7788", at = { [36] = 9, [2564] = 8, [2568] = 9 } },
    ["agility"] = { "s13898", "s20032", "s20031", "s20034", "s23800", "s13943", "s13693", "s13503", "s7788", at = { [36] = 9, [2563] = 8 } },
    ["tank"] = { "s13898", "s20034", "s20032", "s23799", "s23800", "s20031", "s1248760", "s13943", "s13693", "s13503", at = { [36] = 10 } },
    ["melee/dagger"] = { "s13898", "s20034", "s20032", "s20031", "s13943", "s23799", "s13693", "s13503", "s7788", at = { [36] = 9, [2564] = 9, [2568] = 9 } },
    ["agility/dagger"] = { "s13898", "s20032", "s20031", "s13943", "s23800", "s13693", "s20034", "s13503", "s7788", at = { [36] = 9, [2563] = 8 } },
    ["tank/dagger"] = { "s13898", "s20034", "s20032", "s23799", "s20031", "s1248760", "s13943", "s23800", "s13693", "s13503", at = { [36] = 10 } },
    ["agility/DRUID"] = { "s13898", "s20034", "s20032", "s23799", "s23800", at = { [36] = 6 } },
    ["tank/DRUID"] = { "s13898", "s20032", "s20034", "s23799", "s23800", at = { [36] = 6 } },
    ["hunter"] = { "s23800" },
    ["caster"] = { "s23804", "s1248805", "s1248757", up = { 2504 } },
    ["caster/MAGE3"] = { "s23804", "s1248805", "s1248757", up = { 2504, 2443 } },
    ["healer"] = { "s23804", "s23803", "s1248757", up = { 2505 } },
    ["healer/PALADIN"] = { "s23804", up = { 2505 } },
    ["healer/SHAMAN"] = { "s23804", "s1248757", up = { 2505 } },
  },
  weapon2h = {
    ["melee"] = { "s13898", "s20034", "b6043", "s20032", "s20030", "s1248511", "s13937", "s13695", "s13529", "s7745", up = { 8215 }, at = { [36] = 11, [2564] = 11, [2646] = 9 } },
    ["agility"] = { "s13898", "s27837", "s20034", "s20032", "s1248511", "s1248510", at = { [36] = 7, [2564] = 6 } },
    ["hunter"] = { "s27837", "s1248510", at = { [2564] = 2, [2568] = 2 } },
    ["tank"] = { "s13898", "s20034", "s20032", "s27837", "s1248511", "s1248510", at = { [36] = 7, [2564] = 6, [7662] = 6, [8211] = 6 } },
    ["caster"] = { "s1219580", "s1248607", "s20036", "s7793", at = { [2568] = 3, [8212] = 3 } },
    ["healer"] = { "s1248636", "s1219580", "s20036", "s7793", "s20035", "s13380", at = { [2567] = 5, [2568] = 3, [8211] = 3 } },
  },
  shield = {
    ["melee"] = { "s20017", "s13817", "s13631", "s13378", up = { 7663 } },
    ["tank"] = { "s20017", "b12645", "s13817", "b7967", "s13631", "b6042", "s13378", up = { 7663 } },
    ["caster"] = { "s20017", "s13817", "s13631", "s13378", up = { 7663 } },
    ["healer"] = { "s20017", "s13817", "s13631", "s13378", up = { 7663 } },
  },
  holdable = {
    ["caster"] = { "s1219579", up = { 7659 } },
    ["healer"] = { "s1219579", up = { 7659 } },
  },
  chest = {
    ["melee"] = { "s20025", "s13941", "k252488", "k252463", "s13700", "k252453", "k252437", "s13626" },
    ["agility"] = { "s20025", "k252488", "s13941", "k252463", "k252453", "s13700", "k252437", "s13626" },
    ["hunter"] = { "s20025", "s13941", "k252488", "k252463", "s13700", "k252453", "k252437", "s13626", at = { [843] = 7, [857] = 6, [913] = 4, [1893] = 3 } },
    ["tank"] = { "s20026", "s13858", "k15564", "s20025", "s13640", "k8173", "k4265", "s7857", "k2313", up = { 8719 }, at = { [242] = 9, [866] = 9, [928] = 7, [8488] = 9, [8491] = 7 } },
    ["caster"] = { "k252487", "k252462", "s20028", "k252452", "s13917", "s13663", "k252436", "s13607", "s7776", at = { [866] = 9, [928] = 8, [1891] = 7, [1892] = 9 } },
    ["healer"] = { "s20028", "s13917", "k252487", "s13663", "k252462", "k252452", "s13607", "s7776", "k252436", at = { [866] = 9, [928] = 7, [1891] = 5 } },
  },
  cloak = {
    ["melee"] = { "s1219587", "s13419", at = { [849] = 2 } },
    ["agility"] = { "s1219587", "s13419", at = { [849] = 2 } },
    ["hunter"] = { "s1219587", "s13419", at = { [849] = 2 } },
    ["tank"] = { "s20015", "s13746", "s13635", "s13421", up = { 2622 }, at = { [7667] = 4 } },
    ["caster"] = { "s20014", "s13794", "s7454", up = { 2621 } },
    ["healer"] = { "s20014", "s13794", "s7454", up = { 2621 } },
  },
  wrist = {
    ["melee"] = { "s20010", "s13939", "s13661", "s13536", "s7782", at = { [1887] = 5, [7656] = 4 } },
    ["agility"] = { "s1248599", "s1248500", "s1248497", "s1248460", "s7779", at = { [927] = 4, [1885] = 3 } },
    ["hunter"] = { "s1248599", "s1248500", "s1248497", "s1248460", "s7779", at = { [1901] = 4 } },
    ["tank"] = { "s1248665", "s13931", "s20011", "s13945", "s13648", "s13501", "s7457", at = { [924] = 6, [927] = 7, [1885] = 6, [7656] = 7 } },
    ["caster"] = { "s1220624", "s1248661", "s20008", "s13822", "s1248458", at = { [1886] = 5, [2566] = 2, [8208] = 4, [8209] = 2, [8734] = 5 } },
    ["healer"] = { "s23802", "s1248498", "s1248661", "s1248459", "s20008", at = { [2565] = 2, [7665] = 2 } },
  },
  hands = {
    ["melee"] = { "s1248640", "s20013", "s13887", "k252488", "k252463", "k252453", "k252437", at = { [931] = 3, [1887] = 6, [8206] = 5 } },
    ["agility"] = { "s20012", "s13815", "k252488", "k252463", "k252453", "k252437", up = { 2564 }, at = { [927] = 5, [931] = 2, [2563] = 2, [8207] = 4 } },
    ["hunter"] = { "s20012", "s13815", "k252488", "k252463", "k252453", "k252437", up = { 2564 }, at = { [931] = 2 } },
    ["tank"] = { "s1248640", "k15564", "k8173", "s20013", "k4265", "s20012", "k2313", "s13815", up = { 2613, 8719 }, at = { [927] = 6, [931] = 7, [2564] = 4, [8486] = 8, [8488] = 6, [8491] = 5, [8492] = 8 } },
    ["caster"] = { "s13948", "k252487", "k252462", "k252452", "k252436" },
    ["caster/MAGE1"] = { "s1213626", "s13948", "k252487", "k252462", "k252452", "k252436" },
    ["caster/MAGE2"] = { "s13948", "k252487", "k252462", "k252452", "k252436", up = { 2616 } },
    ["caster/MAGE3"] = { "s13948", "k252487", "k252462", "k252452", "k252436", up = { 2615 } },
    ["caster/WARLOCK"] = { "s13948", "k252487", "k252462", "k252452", "k252436", up = { 2614 } },
    ["caster/PRIEST"] = { "s13948", "k252487", "k252462", "k252452", "k252436", up = { 2614 } },
    ["caster/DRUID1"] = { "s1213626", "s13948", "k252487", "k252462", "k252452", "k252436", at = { [8213] = 2 } },
    ["caster/SHAMAN1"] = { "s1248645", "s13948", "k252487", "k252462", "k252452", "k252436" },
    ["healer"] = { "s13948", "k252487", "k252462", "k252452", "k252436", up = { 2617 } },
  },
  feet = {
    ["melee"] = { "k252488", "k252463", "s20023", "k252453", "s13935", "k252437", "s13637", "s7867", up = { 8737, 911, 464 } },
    ["agility"] = { "s20023", "k252488", "s13935", "k252463", "s13637", "k252453", "s7867", "k252437", up = { 8737, 911, 464 } },
    ["hunter"] = { "s20023", "s13935", "k252488", "s13637", "k252463", "s7867", "k252453", "k252437", up = { 8737, 911, 464 } },
    ["tank"] = { "s20020", "k15564", "k8173", "s13836", "k4265", "s13644", "k2313", up = { 8737, 911, 464, 8719 }, at = { [8488] = 6, [8491] = 5 } },
    ["caster"] = { "k252487", "k252462", "k252452", "k252436", "s20020", "s13836", "s13644", up = { 8737, 911, 464 }, at = { [8487] = 7, [8490] = 6 } },
    ["healer"] = { "k252487", "k252462", "k252452", "k252436", "s20024", "s13687", up = { 8737, 911, 464 } },
    ["healer/PALADIN"] = { "k252487", "k252462", "k252452", "k252436", "s20020", "s13836", up = { 8737, 911, 464 }, at = { [8490] = 6 } },
    ["healer/SHAMAN"] = { "k252487", "k252462", "k252452", "k252436", "s20020", "s13836", up = { 8737, 911, 464 }, at = { [8490] = 6 } },
  },
  neck = {
    ["melee"] = { "s1249019", "s1249059" },
    ["agility"] = { "s1249059", "s1249019" },
    ["hunter"] = { "s1249059" },
    ["tank"] = { "s1249019", "s1249059", up = { 925 } },
    ["caster"] = { "s1249057" },
    ["healer"] = { "s1249058", "s1249057" },
  },
  head = {
    ["melee"] = { "a18329", "a11645" },
    ["agility"] = { "a18329", "a11647" },
    ["hunter"] = { "a18329", "a11647" },
    ["tank"] = { "a18331", "a11642" },
    ["caster"] = { "a18330", "a11648", at = { [1483] = 2 } },
    ["healer"] = { "a18330", "a11622" },
  },
  legs = {
    ["melee"] = { "a18329", "a11645", "k252488", "k252463", "k252453", "k252437", "k15564", "k8173", "k4265", "k2313", "k2304", at = { [1503] = 7, [1507] = 7, [1508] = 5, [1509] = 7, [2545] = 10 } },
    ["agility"] = { "a18329", "a11647", "k252488", "k252463", "k252453", "k252437", "k15564", "k8173", "k4265", "k2313", "k2304", at = { [1503] = 7, [1506] = 5, [1507] = 7, [2545] = 7 } },
    ["hunter"] = { "a18329", "a11647", "k252488", "k252463", "k252453", "k252437", "k15564", "k8173", "k4265", "k2313", "k2304", at = { [1483] = 5, [1503] = 7, [1507] = 7, [1509] = 4, [1510] = 11 } },
    ["tank"] = { "a18331", "a11642", "k15564", "k8173", "k4265", "k2313", "k2304", up = { 8719 }, at = { [1504] = 3, [1506] = 6, [1507] = 3, [1508] = 7, [2543] = 6, [8483] = 7, [8485] = 7, [8486] = 6, [8488] = 6, [8489] = 7, [8491] = 5, [8492] = 7 } },
    ["caster"] = { "a18330", "k252487", "k252462", "a11648", "k252452", "k252436", "k15564", "k8173", "k4265", "k2313", "k2304", at = { [1483] = 6, [1503] = 7, [1507] = 7, [1510] = 11 } },
    ["healer"] = { "a18330", "a11622", "k252487", "k252462", "k252452", "k252436", "k15564", "k8173", "k4265", "k2313", "k2304", at = { [1503] = 7, [1507] = 7, [1509] = 3, [1510] = 7 } },
  },
  ranged = {
    ["hunter"] = { "i10548", "i10546", "i4407", "i4406", "i4405", up = { 2523, 8720 } },
    ["melee"] = { "i10548", "i10546", "i4407", "i4406", "i4405", up = { 2523, 8720 }, pull = true },
    ["agility"] = { "i10548", "i10546", "i4407", "i4406", "i4405", up = { 2523, 8720 }, pull = true },
    ["tank"] = { "i10548", "i10546", "i4407", "i4406", "i4405", up = { 2523, 8720 }, pull = true },
    ["melee/HUNTER"] = { "i10548", "i10546", "i4407", "i4406", "i4405", up = { 2523, 8720 } },
    ["agility/HUNTER"] = { "i10548", "i10546", "i4407", "i4406", "i4405", up = { 2523, 8720 } },
    ["tank/HUNTER"] = { "i10548", "i10546", "i4407", "i4406", "i4405", up = { 2523, 8720 } },
  },
}
-- END DATA
E.DATA = { mats = MATS, enchants = ENCHANTS, picks = PICKS }

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

local FALLBACK = { text = { 0.90, 0.88, 0.84 }, muted = { 0.55, 0.51, 0.45 } }
local WHITE = { 1, 1, 1 }
local function colour(key)
  local C = ns.COLORS
  local c = type(C) == "table" and C[key]
  if type(c) == "table" then return c end
  return FALLBACK[key]
end

local function say(msg)
  if type(ns.Print) == "function" then ns.Print(msg) end
end

local function now()
  local f = _G.GetTime
  if type(f) == "function" then
    local ok, t = pcall(f)
    if ok and type(t) == "number" and not isSecret(t) then return t end
  end
  return 0
end

local function shiftDown()
  local f = _G.IsShiftKeyDown
  if type(f) ~= "function" then return false end
  local ok, r = pcall(f)
  return ok and r and true or false   -- older clients answer 1
end

---------------------------------------------------------------------------
-- The item: which enchant group its slot is in (never for slots with no option)
---------------------------------------------------------------------------
local GROUP_OF = {
  INVTYPE_WEAPON = "weapon", INVTYPE_WEAPONMAINHAND = "weapon", INVTYPE_WEAPONOFFHAND = "weapon",
  INVTYPE_2HWEAPON = "weapon2h", INVTYPE_SHIELD = "shield", INVTYPE_HOLDABLE = "holdable",
  INVTYPE_CHEST = "chest", INVTYPE_ROBE = "chest", INVTYPE_CLOAK = "cloak", INVTYPE_WRIST = "wrist",
  INVTYPE_HAND = "hands", INVTYPE_FEET = "feet", INVTYPE_NECK = "neck",
  INVTYPE_HEAD = "head",                                           -- Arcanums (level 50 quests)
  INVTYPE_LEGS = "legs",                                           -- Arcanums and armor kits
  INVTYPE_RANGED = "ranged", INVTYPE_RANGEDRIGHT = "ranged",       -- scopes (bows, guns, crossbows)
  -- Shoulders, rings, trinkets, belts, shirts, tabards, thrown, relics: no option worth showing.
}
-- Armor kits fit cloth, leather, mail and plate (SpellEquippedItems of the kit spells: subclass mask 30).
local KIT_SUBS = { [1] = true, [2] = true, [3] = true, [4] = true }
-- Weapon subclasses the recipes take ("Enchant Weapon": every melee weapon; 2H: axes, maces,
-- polearms, swords, staves; scopes: bows, guns, crossbows). Fishing poles (20) and wands (19) get none.
local WEAPON_SUBS = { [0] = true, [1] = true, [4] = true, [5] = true, [6] = true, [7] = true, [8] = true,
  [10] = true, [13] = true, [14] = true, [15] = true, [17] = true }
local TWOHAND_SUBS = { [1] = true, [5] = true, [6] = true, [8] = true, [10] = true }
local SCOPE_SUBS = { [2] = true, [3] = true, [18] = true }
local CLASS_WEAPON, CLASS_ARMOR = 2, 4
-- Armor a class can wear (cloth 1, leather 2, mail 3, plate 4) and who carries a shield.
local ARMOR_MAX = { WARRIOR = 4, PALADIN = 4, HUNTER = 3, SHAMAN = 3, ROGUE = 2, DRUID = 2,
  PRIEST = 1, MAGE = 1, WARLOCK = 1 }
local SHIELD_CLASS = { WARRIOR = true, PALADIN = true, SHAMAN = true }
local SUB_SHIELD = 6
-- Weapon skills a class can learn (weapon subclass ids): Classic Era's, plus one-hand axes for
-- rogues (Forever's SkillRaceClassInfo gives Axes to warriors and rogues). Misc (14) and spears (17)
-- need no skill. Off-hand-only weapons need dual wield (no shaman on Forever).
local function set(...)
  local t = {}
  for _, v in ipairs({ ... }) do t[v] = true end
  return t
end
local WEAPON_OK = {
  WARRIOR = set(0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 13, 15, 16, 18), PALADIN = set(0, 1, 4, 5, 6, 7, 8),
  HUNTER = set(0, 1, 2, 3, 6, 7, 8, 10, 13, 15, 16, 18), ROGUE = set(0, 2, 3, 4, 7, 13, 15, 16, 18),
  PRIEST = set(4, 10, 15, 19), SHAMAN = set(0, 1, 4, 5, 10, 13, 15), MAGE = set(7, 10, 15, 19),
  WARLOCK = set(7, 10, 15, 19), DRUID = set(4, 5, 10, 13, 15),
}
local NO_SKILL = set(14, 17)
local DUAL_WIELD = set("WARRIOR", "ROGUE", "HUNTER")

-- equipLoc, classID, subclassID of an item id or link (instant: no server query).
local function itemFacts(v)
  local CI = _G.C_Item
  local f = (type(CI) == "table" and CI.GetItemInfoInstant) or _G.GetItemInfoInstant
  if type(f) == "function" then
    local ok, id, _, _, loc, _, cls, sub = pcall(f, v)
    if ok and id ~= nil and not isSecret(loc) and type(loc) == "string" then
      return loc, plainNumber(cls), plainNumber(sub)
    end
  end
  f = (type(CI) == "table" and CI.GetItemInfo) or _G.GetItemInfo
  if type(f) == "function" then
    local r = { pcall(f, v) }
    if r[1] and r[2] ~= nil and not isSecret(r[10]) and type(r[10]) == "string" then
      return r[10], plainNumber(r[13]), plainNumber(r[14])
    end
  end
  return nil
end

local function groupFor(loc, cls, sub)
  local g = GROUP_OF[loc]
  if not g then return nil end
  if g == "weapon" or g == "weapon2h" then
    if cls and cls ~= CLASS_WEAPON then return nil end
    if sub and not (g == "weapon2h" and TWOHAND_SUBS or WEAPON_SUBS)[sub] then return nil end
  elseif g == "ranged" then
    if cls ~= CLASS_WEAPON or not sub or not SCOPE_SUBS[sub] then return nil end
  elseif cls and cls ~= CLASS_ARMOR then
    return nil
  end
  return g
end

-- The item's level (GetItemInfo's 4th value), or nil while the game has not loaded the item.
local function itemLevelOf(itemID)
  local CI = _G.C_Item
  local f = (type(CI) == "table" and CI.GetItemInfo) or _G.GetItemInfo
  if type(f) ~= "function" then return nil end
  local ok, name, _, _, lvl = pcall(f, itemID)
  if not ok or name == nil then return nil end
  lvl = plainNumber(lvl)
  return lvl and lvl > 0 and lvl or nil
end

-- Slots never change: one lookup per item id a session.
local factMemo = {}
local function facts(itemID)
  local f = factMemo[itemID]
  if f ~= nil then return f or nil end
  local loc, cls, sub = itemFacts(itemID)
  if loc == nil then return nil end   -- not readable now: ask again next time
  local g = groupFor(loc, cls, sub)
  f = g and { g = g, cls = cls, sub = sub, loc = loc, id = itemID } or false
  factMemo[itemID] = f
  return f or nil
end

-- The item's level, read once (an armor kit needs items of its level or higher); nil = not known yet.
local function factLevel(f)
  if f.ilvl == nil then f.ilvl = itemLevelOf(f.id) end
  return f.ilvl
end
E.SlotGroup = function(v)
  local loc, cls, sub = itemFacts(v)
  return loc and groupFor(loc, cls, sub) or nil
end

-- Gear this class cannot use (a mage hovering plate or a two-hand sword) gets nothing.
local function canWear(class, f)
  if not class or not f.sub then return true end
  if f.cls == CLASS_WEAPON then
    local ok = WEAPON_OK[class]
    if not ok then return true end   -- a class this file does not know
    if not NO_SKILL[f.sub] and not ok[f.sub] then return false end
    return f.loc ~= "INVTYPE_WEAPONOFFHAND" or DUAL_WIELD[class] == true
  end
  if f.cls ~= CLASS_ARMOR then return true end
  if f.sub == SUB_SHIELD then return SHIELD_CLASS[class] == true end
  local most = ARMOR_MAX[class]
  if most and f.sub >= 1 and f.sub <= 4 then return f.sub <= most end
  return true
end

-- Item quality from the link's colour (|cnIQ2: on Forever, |cff9d9d9d for grey), else the game.
local function quality(itemID, link)
  if type(link) == "string" then
    local q = smatch(link, "|cnIQ(%d)")
    if q then return tonumber(q) end
    if slower(ssub(link, 1, 10)) == "|cff9d9d9d" then return 0 end
  end
  local CI = _G.C_Item
  if type(CI) == "table" and type(CI.GetItemQualityByID) == "function" then
    local ok, q = pcall(CI.GetItemQualityByID, itemID)
    if ok then return plainNumber(q) end
  end
  return nil
end

-- The permanent enchant id of an item link (field 2 after "item:"), or nil.
local function linkEnchant(link)
  if type(link) ~= "string" or isSecret(link) then return nil end
  local e = tonumber(smatch(link, "item:%-?%d+:(%d*)") or "")
  if e and e > 0 then return e end
  return nil
end
E.LinkEnchant = linkEnchant

---------------------------------------------------------------------------
-- Where the tooltip's item is
---------------------------------------------------------------------------
-- "bag" (GetBagItem, or a bank slot), "equipped" (GetInventoryItem, player,
-- slots 1-19), "link" (GetHyperlink: chat links), "script" (the older
-- OnTooltipSetItem path, which does not say); nil = skip (someone you
-- inspect, merchants, loot, quest rewards, the auction house).
local function whereOf(info)
  if info == nil then return "script" end
  if type(info) ~= "table" then return nil end
  local g = info.getterName
  local a = type(info.getterArgs) == "table" and info.getterArgs or {}
  if g == "GetBagItem" then return "bag" end
  if g == "GetInventoryItem" then
    if a[1] ~= "player" then return nil end
    local slot = plainNumber(a[2])
    if slot and slot >= 1 and slot <= 19 then return "equipped" end
    return "bag"   -- the bank's own slots are inventory slots after 19
  end
  if g == "GetHyperlink" then return "link" end
  return nil
end
E.WhereOf = whereOf

-- The places that get the lines (the same number of options everywhere): your own worn gear;
-- with /gl enchants bags on (settings().enchantsBags) bags, the bank and links too.
local SHOW = { equipped = true }
local SHOW_BAGS = { equipped = true, bag = true, link = true, script = true }
E.SHOW, E.SHOW_BAGS = SHOW, SHOW_BAGS
local function shows(where)
  return (settings().enchantsBags == true and SHOW_BAGS or SHOW)[where] == true
end
E.Shows = shows

-- The older path ("script") does not say where the item is: worn gear when the tooltip's owner
-- is one of the character panel's slot buttons (CharacterHeadSlot...: its id is the slot) and
-- that slot holds this item.
local function scriptWhere(tt, itemID)
  if type(tt.GetOwner) ~= "function" or type(_G.GetInventoryItemLink) ~= "function" then return "script" end
  local ok, owner = pcall(tt.GetOwner, tt)
  if not ok or type(owner) ~= "table" or type(owner.GetName) ~= "function" or type(owner.GetID) ~= "function" then
    return "script"
  end
  local okN, name = pcall(owner.GetName, owner)
  if not okN or type(name) ~= "string" or isSecret(name) or not smatch(name, "^Character%w+Slot$") then return "script" end
  local okI, slot = pcall(owner.GetID, owner)
  slot = okI and plainNumber(slot) or nil
  if not slot or slot < 1 or slot > 19 then return "script" end
  local okL, worn = pcall(_G.GetInventoryItemLink, "player", slot)
  if okL and type(worn) == "string" and not isSecret(worn) and tonumber(smatch(worn, "item:(%d+)") or "") == itemID then
    return "equipped"
  end
  return "script"
end

-- Top 2 (the two best, the default) or Top 5 (the best, then cheaper budget options).
local TOPS = { [2] = true, [5] = true }
local function topN()
  local n = settings().enchantsTop
  return TOPS[n] and n or 2
end
E.TopN = topN

-- The item's link when the tooltip did not give one.
local function fallbackLink(info)
  if type(info) ~= "table" or type(info.getterArgs) ~= "table" then return nil end
  local a, l = info.getterArgs, nil
  if info.getterName == "GetBagItem" then
    local cc = _G.C_Container
    if type(cc) == "table" and type(cc.GetContainerItemLink) == "function" then
      local ok, r = pcall(cc.GetContainerItemLink, a[1], a[2])
      if ok then l = r end
    end
  elseif info.getterName == "GetInventoryItem" and type(_G.GetInventoryItemLink) == "function" then
    local ok, r = pcall(_G.GetInventoryItemLink, a[1], a[2])
    if ok then l = r end
  end
  if type(l) == "string" and not isSecret(l) then return l end
  return nil
end

---------------------------------------------------------------------------
-- Role: from talents (cached), else the class default, or the player's pick
---------------------------------------------------------------------------
local ROLES = { melee = true, agility = true, tank = true, caster = true, healer = true, hunter = true }
-- Talent tab order is fixed per class (Forever groups: orderIndex + 1).
local TAB_ROLE = {
  WARRIOR = { "melee", "melee", "tank" },
  PALADIN = { "healer", "tank", "melee" },
  HUNTER  = { "hunter", "hunter", "hunter" },
  ROGUE   = { "agility", "agility", "agility" },
  PRIEST  = { "healer", "healer", "caster" },
  SHAMAN  = { "caster", "melee", "healer" },
  MAGE    = { "caster", "caster", "caster" },
  WARLOCK = { "caster", "caster", "caster" },
  DRUID   = { "caster", "agility", "healer" },
}
local TAB_NAME = {
  WARRIOR = { "Arms", "Fury", "Protection" }, PALADIN = { "Holy", "Protection", "Retribution" },
  HUNTER = { "Beast Mastery", "Marksmanship", "Survival" }, ROGUE = { "Assassination", "Combat", "Subtlety" },
  PRIEST = { "Discipline", "Holy", "Shadow" }, SHAMAN = { "Elemental", "Enhancement", "Restoration" },
  MAGE = { "Arcane", "Fire", "Frost" }, WARLOCK = { "Affliction", "Demonology", "Destruction" },
  DRUID = { "Balance", "Feral Combat", "Restoration" },
}
-- With fewer talent points than this in one tree (low level), or a tie between
-- trees of different roles: the class's usual role.
local CLASS_DEFAULT = {
  WARRIOR = "melee", PALADIN = "melee", HUNTER = "hunter", ROGUE = "agility", PRIEST = "caster",
  SHAMAN = "melee", MAGE = "caster", WARLOCK = "caster", DRUID = "agility",
}
local MIN_POINTS = 5
local ROLE_TTL, UNSURE_TTL = 10, 2   -- seconds; talent and level events also clear it

local function classFile()
  local f = _G.UnitClass
  if type(f) ~= "function" then return nil end
  local ok, _, file = pcall(f, "player")
  if ok and type(file) == "string" and not isSecret(file) then return file end
  return nil
end

local function playerLevel()
  local f = _G.UnitLevel
  if type(f) ~= "function" then return nil end
  local ok, l = pcall(f, "player")
  return ok and plainNumber(l) or nil
end

-- WoW Forever 1.60.1: the talent trees are node groups of one C_Traits tree
-- (GetTalentTabInfo is gone). { [tab] = points } or nil when it cannot be read.
local function foreverPoints()
  local T, CT, CSI = _G.C_Traits, _G.C_ClassTalents, _G.C_SpecializationInfo
  if type(T) ~= "table" or type(T.GetConfigInfo) ~= "function" or type(T.GetGroupDisplayInfoByTreeID) ~= "function"
     or type(T.GetGroupCurrencyInfo) ~= "function" then return nil end
  local configID
  if type(CT) == "table" and type(CT.GetActiveConfigID) == "function" then
    local ok, id = pcall(CT.GetActiveConfigID)
    if ok then configID = plainNumber(id) end
  end
  if not configID and type(CSI) == "table" and type(CSI.GetActiveSpecGroup) == "function"
     and type(CSI.GetCombatConfigIDForSpecGroup) == "function" then
    local okG, grp = pcall(CSI.GetActiveSpecGroup)
    if okG and grp then
      local ok, id = pcall(CSI.GetCombatConfigIDForSpecGroup, grp)
      if ok then configID = plainNumber(id) end
    end
  end
  if not configID then return nil end
  local okC, cfg = pcall(T.GetConfigInfo, configID)
  local treeID = okC and type(cfg) == "table" and type(cfg.treeIDs) == "table" and cfg.treeIDs[1]
  if not treeID then return nil end
  local okD, groups = pcall(T.GetGroupDisplayInfoByTreeID, treeID)
  if not okD or type(groups) ~= "table" or #groups == 0 then return nil end
  local ids = {}
  for i, g in ipairs(groups) do ids[i] = type(g) == "table" and g.groupID or nil end
  local okS, cur = pcall(T.GetGroupCurrencyInfo, configID, ids)
  if not okS or type(cur) ~= "table" then return nil end
  local spent = {}
  for _, c in ipairs(cur) do
    local info = type(c) == "table" and type(c.currencyInfos) == "table" and c.currencyInfos[1]
    if type(info) == "table" and info.spent ~= nil then
      local n = plainNumber(info.spent)
      if n == nil then return nil end   -- secret or odd: unreadable, not "0"
      if c.traitNodeGroupID then spent[c.traitNodeGroupID] = n end
    end
  end
  local out = {}
  for i, g in ipairs(groups) do
    if type(g) == "table" then
      local tab = plainNumber(g.orderIndex)
      tab = tab and (tab + 1) or i
      out[tab] = spent[g.groupID] or 0   -- a group with nothing spent has no count
    end
  end
  return out
end

-- Classic Era 1.15.8+: C_SpecializationInfo.GetSpecializationInfo(tab), points
-- are the 7th value. On Forever it answers one class-named starter spec with 0
-- points, so it is only trusted when some tab has points.
local function specInfoPoints()
  local CSI = _G.C_SpecializationInfo
  if type(CSI) ~= "table" or type(CSI.GetSpecializationInfo) ~= "function" then return nil end
  local out, any = {}, false
  for tab = 1, 3 do
    local ok, _, name, _, _, _, _, pts = pcall(CSI.GetSpecializationInfo, tab)
    if not ok or type(name) ~= "string" then return nil end
    out[tab] = plainNumber(pts) or 0
    if out[tab] > 0 then any = true end
  end
  return any and out or nil
end

-- Older clients: GetTalentTabInfo, name first (name, icon, points, file) or
-- id first (id, name, description, icon, points, ...); points may be a string.
local function tabInfoPoints()
  local num, info = _G.GetNumTalentTabs, _G.GetTalentTabInfo
  if type(num) ~= "function" or type(info) ~= "function" then return nil end
  local okN, n = pcall(num)
  n = okN and plainNumber(n) or 0
  if n < 1 then return nil end
  local out = {}
  for tab = 1, (n < 3 and n or 3) do
    local ok, a, _, c, _, e = pcall(info, tab)
    if not ok then return nil end
    out[tab] = plainNumber(type(a) == "string" and c or e) or 0
  end
  return out
end

local cache, cacheAt, cacheTTL
-- { class, level, role, how ("talents" | "class"), tab, tabPoints, points }
local function detect()
  local t = now()
  if cache and cacheAt and t >= cacheAt and t - cacheAt < cacheTTL then return cache end
  E.reads = E.reads + 1
  local r = { class = classFile(), level = playerLevel() }
  local pts = foreverPoints() or specInfoPoints() or tabInfoPoints()
  local cls, total = r.class, 0
  if pts then
    for tab = 1, 3 do total = total + (pts[tab] or 0) end
  end
  r.points = pts
  local roles = cls and TAB_ROLE[cls]
  if roles and pts then
    local best, bestPts = nil, 0
    for tab = 1, 3 do
      local p = pts[tab] or 0
      if p > bestPts then best, bestPts = tab, p end
    end
    if best and bestPts >= MIN_POINTS then
      local tie = false
      for tab = 1, 3 do
        if tab ~= best and (pts[tab] or 0) == bestPts and roles[tab] ~= roles[best] then tie = true end
      end
      if not tie then r.role, r.how, r.tab, r.tabPoints = roles[best], "talents", best, bestPts end
    end
  end
  if not r.role and cls and CLASS_DEFAULT[cls] then r.role, r.how = CLASS_DEFAULT[cls], "class" end
  -- Talents can be unreadable for a moment after login: such an answer is kept briefly.
  local unsure = not cls or not pts or (total == 0 and (r.level or 0) >= 10)
  cache, cacheAt, cacheTTL = r, t, unsure and UNSURE_TTL or ROLE_TTL
  return r
end

-- role, how ("pick" | "talents" | "class"), the detection
local function currentRole()
  local d = detect()
  local pick = settings().enchants
  if ROLES[pick] then return pick, "pick", d end
  return d.role, d.how, d
end
E.DetectRole = function()
  local role, how, d = currentRole()
  return role, how, d.tab, d.tabPoints, d.class
end

---------------------------------------------------------------------------
-- Professions: Blacksmithing's spikes and counterweight need the skill to use
---------------------------------------------------------------------------
-- The skill lines the upgrades ask for, with their English names (older clients' list gives names only).
local SKILL_NAME = { [164] = "Blacksmithing" }
local SKILL_UNSURE_TTL = 2   -- seconds: nothing readable yet (just after login)
local skills, skillsAt, skillsTTL

-- { [skill line id] = rank } and whether any skill line could be read.
local function readSkills()
  E.skillReads = E.skillReads + 1
  local out, seen = {}, false
  local function note(id, rank)
    local r = plainNumber(rank)
    if id and r and r > (out[id] or 0) then out[id] = r end
  end
  local CS = _G.C_SkillInfo
  if type(CS) == "table" then
    -- By id: also finds a profession under a collapsed header in the Skills tab.
    if type(CS.GetSkillLineInfoByID) == "function" then
      for id in pairs(SKILL_NAME) do
        local ok, info = pcall(CS.GetSkillLineInfoByID, id)
        if ok and type(info) == "table" then note(id, info.rank) end
      end
    end
    -- The Skills tab's list, as Blizzard's Forever SkillsFrame reads it.
    if type(CS.GetNumSkillLines) == "function" and type(CS.GetSkillLineInfo) == "function" then
      local okN, n = pcall(CS.GetNumSkillLines)
      n = okN and plainNumber(n) or 0
      for i = 1, (n < 200 and n or 200) do
        local ok, info = pcall(CS.GetSkillLineInfo, i)
        if ok and type(info) == "table" then
          seen = true
          local id = plainNumber(info.skillID)
          if id and SKILL_NAME[id] then note(id, info.rank) end
        end
      end
    end
  end
  -- Older clients: GetSkillLineInfo(i) answers name, isHeader, isExpanded, rank, ...
  local num, info = _G.GetNumSkillLines, _G.GetSkillLineInfo
  if not seen and type(num) == "function" and type(info) == "function" then
    local okN, n = pcall(num)
    n = okN and plainNumber(n) or 0
    for i = 1, (n < 200 and n or 200) do
      local ok, name, _, _, rank = pcall(info, i)
      if ok and type(name) == "string" and not isSecret(name) then
        seen = true
        for id, nm in pairs(SKILL_NAME) do
          if name == nm then note(id, rank) end
        end
      end
    end
  end
  return out, seen or next(out) ~= nil
end

-- The player's rank in a profession (0 = not learned). Read once, again on SKILL_LINES_CHANGED.
local function skillRank(id)
  local t = now()
  if not skills or not skillsAt or t < skillsAt or t - skillsAt >= skillsTTL then
    local r, sure = readSkills()
    skills, skillsAt, skillsTTL = r, t, sure and 1e9 or SKILL_UNSURE_TTL
  end
  return skills[id] or 0
end
E.SkillRank = skillRank

local lineMemo, lineStamp = {}, nil
function E.Invalidate()
  cache, cacheAt = nil, nil
  skills = nil
  lineMemo = {}
  factMemo = {}
end

---------------------------------------------------------------------------
-- The picks for an item, and which of them to show
---------------------------------------------------------------------------
-- role/CLASStab (top talent tree), then role/CLASS, then role/dagger (a dagger that can go in the
-- main hand), then the role's own.
local SUB_DAGGER = 15
local function pickFor(group, role, class, tab, f)
  local g = PICKS[group]
  if not g then return nil end
  local dagger = f and f.sub == SUB_DAGGER and f.loc ~= "INVTYPE_WEAPONOFFHAND"
  return (class and tab and g[role .. "/" .. class .. tab]) or (class and g[role .. "/" .. class])
    or (dagger and g[role .. "/dagger"]) or g[role]
end

local function listHas(list, v)
  if type(list) ~= "table" then return false end
  for _, x in ipairs(list) do
    if x == v then return true end
  end
  return false
end

-- Whether the player can use an entry on this item: the level it needs, the profession rank
-- its item asks for, the item's level (armor kits: Heavy 15, Thick 25, Rugged 35; an item whose level
-- is not known yet gets them) and its kind (armor kits: cloth to plate; the counterweight: its weapons).
local function usable(e, f, level)
  if e.lvl and level < e.lvl then return false end
  if e.prof and skillRank(e.prof) < (e.rank or 1) then return false end
  if e.ilvl then
    local il = factLevel(f)
    if il and il < e.ilvl then return false end
  end
  if e.how == "lw" then return not f.sub or KIT_SUBS[f.sub] == true end
  if e.subs and f.sub then return listHas(e.subs, f.sub) end
  return true
end

-- Where the item's enchant stops the list: entries before this place are upgrades. 0 = nothing
-- (an "up" enchant, or the first entry), nil = the whole list (no enchant, or one the data does not know).
local function stopAt(rec, current)
  if not current then return nil end
  if listHas(rec.up, current) then return 0 end
  for i, key in ipairs(rec) do
    if ENCHANTS[key].id == current then return i end
  end
  local at = rec.at
  return type(at) == "table" and at[current] or nil
end

---------------------------------------------------------------------------
-- Costs (the mats at AH value) and the lines
---------------------------------------------------------------------------
local function dataStamp()
  local db = ns.db
  return type(db) == "table" and tonumber(db.updated) or 0
end

-- An item's AH value in copper (0 = none).
local function marketOf(id)
  local it = type(ns.GetItem) == "function" and ns.GetItem(id) or nil
  local v = it and tonumber(it.market) or 0
  return v > 0 and v or 0
end

local costMemo = {}
-- New price data: the costs and lines are made again.
local function freshMemos()
  local stamp = dataStamp()
  if lineStamp ~= stamp then
    lineMemo, costMemo, lineStamp = {}, {}, stamp
  end
end
-- { copper = mats with an AH value (and a quest fee), missing = { "2 Skin of Shadow", ... },
--   by = "item" (an upgrade's own AH value, or none) or "mats" }
local function costOf(key)
  local c = costMemo[key]
  if c then return c end
  local e = ENCHANTS[key]
  local missing = {}
  if e.item then
    -- An upgrade you buy: its own AH value. A blacksmith's one with none: what its mats cost.
    local v = marketOf(e.item)
    if v > 0 or not e.m then c = { copper = v, missing = missing, by = "item" } end
  end
  if not c then
    local copper, m = e.fee or 0, e.m
    for i = 1, #m, 2 do
      local id, n = m[i], m[i + 1]
      local v = marketOf(id)
      if v > 0 then
        copper = copper + v * n
      else
        missing[#missing + 1] = n .. " " .. (MATS[id] or ("Item " .. id))
      end
    end
    c = { copper = copper, missing = missing, by = "mats" }
  end
  costMemo[key] = c
  return c
end

-- A budget option costs less than the best, with every mat on the AH (a full price).
local function cheaper(key, limit)
  local c = costOf(key)
  return c.copper > 0 and #c.missing == 0 and c.copper < limit
end

-- The options to show, best first: the usable entries before the stop, at most `most`. Past the two
-- best (Top 5) only budget options, while the best has an AH price; without prices, the next ones.
local function pickKeys(rec, f, level, current, most)
  local stop = stopAt(rec, current)
  if stop == 0 then return nil end
  local out, limit
  for i, key in ipairs(rec) do
    if stop and i >= stop then break end
    local e = ENCHANTS[key]
    if e and usable(e, f, level) then
      local n = out and #out or 0
      if n < 2 or not limit or cheaper(key, limit) then
        out = out or {}
        out[n + 1] = key
        if n + 1 >= most then break end
        if n == 0 and most > 2 and dataStamp() > 0 then
          freshMemos()
          local c = costOf(key).copper
          limit = c > 0 and c or nil
        end
      end
    end
  end
  return out
end

-- Whole gold from 10g, whole silver under it: it is an estimate (1g 47s stays 1g 47s).
local function roundCopper(c)
  if c >= 100000 then return floor(c / 10000 + 0.5) * 10000 end
  if c >= 100 then return floor(c / 100 + 0.5) * 100 end
  return floor(c + 0.5)
end
E.RoundCopper = roundCopper

-- Prices older than Core's limit (6 hours): the costs are grey. Core's own age line is
-- not on gear with no price lines (most worn gear), so the grey is the hint there.
local function stalePrices()
  local f, most = ns.DataAge, ns.STALE_SECONDS
  if type(f) ~= "function" or type(most) ~= "number" then return false end
  local ok, age = pcall(f)
  return ok and type(age) == "number" and age > most or false
end

-- "~38g", "~38g + 2 Skin of Shadow", "~38g + more" (mats with no AH price),
-- "no AH price", "not on AH" (an upgrade item nobody lists); nil without price data.
-- Second value: true = grey.
local function costText(key, stale)
  if dataStamp() <= 0 then return nil end
  local c = costOf(key)
  if c.copper <= 0 then return c.by == "item" and "not on AH" or "no AH price", true end
  local money = "~" .. ns.MoneyIcons(roundCopper(c.copper))
  if #c.missing == 1 then return money .. " + " .. c.missing[1], stale end
  if #c.missing > 1 then return money .. " + more", stale end
  return money, stale
end

-- "4 Large Brilliant Shard, 2 Righteous Orb" (and a quest fee).
local function matsText(key)
  local e, parts = ENCHANTS[key], {}
  for i = 1, #e.m, 2 do
    parts[#parts + 1] = e.m[i + 1] .. " " .. (MATS[e.m[i]] or ("Item " .. e.m[i]))
  end
  if e.fee then
    local mt = type(ns.MoneyText) == "function" and ns.MoneyText(e.fee) or (floor(e.fee / 10000) .. "g")
    parts[#parts + 1] = mt .. " quest fee"
  end
  return tconcat(parts, ", ")
end
E.MatsText = matsText

-- The grey line: an enchant's mats, or how you get an upgrade and use it yourself.
local HOW = { lw = "Leatherworking kit, use it on the item", eng = "Engineering scope, use it on the item",
  bs = "Your Blacksmithing: use it on the item" }
local function detailText(key)
  local e = ENCHANTS[key]
  if not e.item then return matsText(key) end
  if e.how == "bs" and costOf(key).by == "mats" then return "Your Blacksmithing: make it from " .. matsText(key) end
  return HOW[e.how] or "Use it on the item"
end
E.DetailText = detailText

-- The labels by place among the entries the player can use.
local LABELS = { "Best enchant: ", "2nd: ", "3rd: ", "4th: ", "5th: " }

-- The lines for the options: { left, right, grey } rows; mats rows (and the note) have mats = true.
local function buildLines(keys, full, stale, note)
  E.builds = E.builds + 1
  local out = {}
  for i, key in ipairs(keys) do
    local cost, grey = costText(key, stale)
    out[#out + 1] = { (LABELS[i] or (i .. "th: ")) .. ENCHANTS[key].n, cost, grey }
    if full then out[#out + 1] = { "  " .. detailText(key), mats = true } end
  end
  if note then out[#out + 1] = { note, mats = true } end   -- shown in compact mode too
  return out
end

local function linesFor(keys, full, note)
  freshMemos()
  -- The age grows while the data stays the same: whether it is old is part of the key.
  local stale = stalePrices()
  local k = tconcat(keys, "|") .. (full and "|f" or "|c") .. (stale and "|s" or "") .. (note and "|n" or "")
  local l = lineMemo[k]
  if not l then
    l = buildLines(keys, full, stale, note)
    lineMemo[k] = l
  end
  return l
end

local function draw(tt, lines)
  local tx, mu = colour("text"), colour("muted")
  for _, ln in ipairs(lines) do
    if ln.mats then
      tt:AddLine(ln[1], mu[1], mu[2], mu[3], true)
    elseif ln[2] then
      local rc = ln[3] and mu or WHITE
      tt:AddDoubleLine(ln[1], ln[2], tx[1], tx[2], tx[3], rc[1], rc[2], rc[3])
    else
      tt:AddLine(ln[1], tx[1], tx[2], tx[3])
    end
  end
end

---------------------------------------------------------------------------
-- Tooltip lines (called by Core.lua's handlers)
---------------------------------------------------------------------------
local TIPS = { GameTooltip = true, ItemRefTooltip = true }   -- not the comparison tooltips

-- Our bag cleanup and sidebar rows mark themselves (glNoEnchants): a tooltip they own gets no
-- enchant lines, also when the game builds it again later (TOOLTIP_DATA_UPDATE), after the
-- ns.tipQuiet moment around their own Set call is over.
local function quietOwner(tt)
  if type(tt.GetOwner) ~= "function" then return false end
  local ok, owner = pcall(tt.GetOwner, tt)
  return ok and type(owner) == "table" and owner.glNoEnchants == true
end

-- A ranged weapon for a class that only pulls with it (a pull = true list): the best only, with this note.
local PULL_NOTE = "(pulling only)"
E.PULL_NOTE = PULL_NOTE

-- The options for an item at a place, best first (a list of keys), or nil; then the note under them.
local function optionsFor(itemID, link, where)
  if not shows(where) then return nil end
  local f = facts(itemID)
  if not f then return nil end
  if quality(itemID, link) == 0 then return nil end   -- grey junk
  local role, _, d = currentRole()
  if not role or not canWear(d.class, f) then return nil end
  local rec = pickFor(f.g, role, d.class, d.tab, f)
  if not rec then return nil end
  local keys = pickKeys(rec, f, d.level or 60, linkEnchant(link), rec.pull and 1 or topN())
  return keys, keys and rec.pull and PULL_NOTE or nil
end
E.OptionsFor = optionsFor

local function addLines(tt, itemID, link, info)
  local S = settings()
  if S.tooltip == false or S.enchants == "off" or ns.tipQuiet then return false end
  if type(tt) ~= "table" or type(tt.GetName) ~= "function" then return false end
  local okN, name = pcall(tt.GetName, tt)
  if not okN or isSecret(name) or not TIPS[name] then return false end
  if quietOwner(tt) then return false end
  local where = whereOf(info)
  if not where then return false end
  itemID = plainNumber(itemID)
  if not itemID then return false end
  if where == "script" then where = scriptWhere(tt, itemID) end
  if not shows(where) then return false end
  if type(link) ~= "string" or isSecret(link) then link = fallbackLink(info) end
  if not link then return false end   -- without the link the current enchant is unknown
  local keys, note = optionsFor(itemID, link, where)
  if not keys then return false end
  draw(tt, linesFor(keys, not S.compact or shiftDown(), note))
  return true
end

-- true when lines were added. Never raises: an error is kept in ns.Enchants.lastError.
function ns.EnchantLines(tt, itemID, link, info)
  local ok, r = pcall(addLines, tt, itemID, link, info)
  if not ok then
    E.lastError = r
    return false
  end
  return r == true
end

---------------------------------------------------------------------------
-- Off and back on
---------------------------------------------------------------------------
-- settings().enchants is "off" while the tips are off (as before), and
-- settings().enchantsBeforeOff keeps what it was ("auto" or the role picked
-- by hand), so turning them back on brings that back.
local function tipsOn()
  return settings().enchants ~= "off"
end

local function setTipsOn(on)
  local S = settings()
  if on then
    if S.enchants == "off" then
      local was = S.enchantsBeforeOff
      S.enchants = ROLES[was] and was or nil
    end
    S.enchantsBeforeOff = nil
  elseif S.enchants ~= "off" then
    S.enchantsBeforeOff = ROLES[S.enchants] and S.enchants or "auto"
    S.enchants = "off"
  end
  E.Invalidate()
end
E.SetOn = setTipsOn
E.IsOn = tipsOn

---------------------------------------------------------------------------
-- The Enchant tips box on the character panel
---------------------------------------------------------------------------
-- A small check box on the Character tab only (a child of PaperDollFrame, so
-- Reputation, Skills and the other tabs do not show it). Forever's panel
-- (Blizzard_UIPanels_Game, Camelot PaperDollFrame.xml): a 36px strip with the
-- level line on top (the portrait over its left end), the slots from 60px down,
-- the model's zoom and turn buttons in the middle 40px down, and the model scene
-- (level 50) over PaperDollFrame's own children, so the box is raised to the
-- slots' level. There the box sits just above the head slot. ElvUI's skin hides
-- the portrait and boxes the model in between the slot columns from 31px down,
-- so there the box sits in the empty top left corner, over the left slot column.
-- Pawn's button and GearMatic's arrows are at the bottom. The Top 2 / Top 5
-- button goes with the box: in Forever's layout right of the head slot (and its
-- flyout button), under the end of the label and left of the model's buttons,
-- since beside the label is too narrow; with ElvUI right under the box, since
-- the label ends just before the level line.
local TOGGLE_NAME, TOGGLE_SIZE = "GoblinLedgerEnchantToggle", 22
local TOGGLE_ART = {   -- the game's check box look, put back if a skin stops half way
  SetNormalTexture = "Interface\\Buttons\\UI-CheckBox-Up",
  SetPushedTexture = "Interface\\Buttons\\UI-CheckBox-Down",
  SetHighlightTexture = "Interface\\Buttons\\UI-CheckBox-Highlight",
  SetCheckedTexture = "Interface\\Buttons\\UI-CheckBox-Check",
}
local ROLE_HOW = { pick = "picked by hand", talents = "from your talents", class = "your class's usual role" }
local GREY = { 0.5, 0.5, 0.5 }   -- the label while Goblin Ledger's tooltips are off (/gl tooltip)
local toggle   -- the box, made once
local labelColour = { 1, 0.82, 0 }   -- the label's own colour (GameFontNormalSmall's), read when it is made
local TOP_NAME = "GoblinLedgerEnchantTop"
local TOP_TIP = "Top 2: the two best. Top 5: adds cheaper budget options. Click to switch."
local topBtn   -- the Top 2 / Top 5 button, made with the box

local function guarded(fn)
  return function(...)
    local ok, err = pcall(fn, ...)
    if not ok then E.lastError = err end
  end
end

local function owns(tt, f)
  if f == nil or type(tt) ~= "table" or type(tt.GetOwner) ~= "function" then return false end
  local ok, o = pcall(tt.GetOwner, tt)
  return ok and o == f
end

local function isFrame(f)
  return type(f) == "table" and type(f.GetObjectType) == "function"
end

-- /gl tooltip turned all Goblin Ledger tooltip lines off: the box then changes nothing.
local function tooltipsOff()
  return settings().tooltip == false
end

-- The role the lines are for (or the one ticking the box brings back) and how it was found.
local function toggleRole()
  local S = settings()
  local v = S.enchants
  if v == "off" then v = S.enchantsBeforeOff end
  if ROLES[v] then return v, "pick" end
  local d = detect()
  return d.role, d.how
end

local function showToggleTip(self)
  local tt = _G.GameTooltip
  if type(tt) ~= "table" or type(tt.SetOwner) ~= "function" then return end
  local tx, mu = colour("text"), colour("muted")
  local role, how = toggleRole()
  local r = role and sformat("Role: %s (%s).", role, ROLE_HOW[how] or "auto") or "Role: none for this class."
  tt:SetOwner(self, "ANCHOR_RIGHT")
  tt:SetText("Goblin Ledger: Enchant tips", 1, 1, 1)
  tt:AddLine("The best enchants for your role, with their cost, on the gear you wear.", tx[1], tx[2], tx[3], true)
  if tooltipsOff() then
    tt:AddLine("Goblin Ledger tooltips are off: /gl tooltip turns them on.", mu[1], mu[2], mu[3], true)
  else
    tt:AddLine(r .. " /gl enchants picks the role.", mu[1], mu[2], mu[3], true)
  end
  tt:Show()
end

local function showTopTip(self)
  local tt = _G.GameTooltip
  if type(tt) ~= "table" or type(tt.SetOwner) ~= "function" then return end
  tt:SetOwner(self, "ANCHOR_RIGHT")
  tt:SetText(TOP_TIP, 1, 1, 1)
  tt:Show()
end

-- The button's words ("Top 2" or "Top 5"), its size, and grey while the tips are off.
local function refreshTop()
  local b = topBtn
  if not b then return end
  local fs = b.Text
  fs:SetText("Top " .. topN())
  local okW, wide = pcall(fs.GetStringWidth, fs)
  wide = okW and plainNumber(wide) or 0
  if wide <= 0 or wide > 80 then wide = 30 end
  b:SetWidth(wide + 4)
  local c = (tooltipsOff() or not tipsOn()) and GREY or (b.glHover and WHITE or labelColour)
  if type(fs.SetTextColor) == "function" then pcall(fs.SetTextColor, fs, c[1], c[2], c[3]) end
  local tt = _G.GameTooltip
  if owns(tt, b) and tt:IsShown() then showTopTip(b) end
end

local function refreshToggle()
  if not toggle then return end
  toggle:SetChecked(tipsOn())
  -- The label goes grey while /gl tooltip has every tooltip line off.
  local fs, c = toggle.Text, tooltipsOff() and GREY or labelColour
  if type(fs) == "table" and type(fs.SetTextColor) == "function" and c then pcall(fs.SetTextColor, fs, c[1], c[2], c[3]) end
  if owns(_G.GameTooltip, toggle) then showToggleTip(toggle) end
  refreshTop()
end

-- An item tooltip that is up (a chat link's, say) is built again, so the change shows at once.
local function refreshItemTips()
  for name in pairs(TIPS) do
    local tt = _G[name]
    if type(tt) == "table" and type(tt.RefreshData) == "function" and not owns(tt, toggle) and not owns(tt, topBtn) then
      local okS, shown = pcall(tt.IsShown, tt)
      local okI, _, link = pcall(tt.GetItem, tt)
      if okS and shown and okI and type(link) == "string" then pcall(tt.RefreshData, tt) end
    end
  end
end

local function levelOf(f)
  if type(f) ~= "table" or type(f.GetFrameLevel) ~= "function" then return nil end
  local ok, l = pcall(f.GetFrameLevel, f)
  return ok and plainNumber(l) or nil
end

-- Level with the slots, over the model (a click on the box must not turn the model).
local function raiseToggle()
  local lvl = levelOf(toggle:GetParent()) or 0
  for _, name in ipairs({ "PaperDollItemsFrame", "CharacterModelScene", "CharacterModelFrame" }) do
    local l = levelOf(_G[name])
    if l and l > lvl then lvl = l end
  end
  if lvl < 10000 then
    toggle:SetFrameLevel(lvl + 1)
    if topBtn then topBtn:SetFrameLevel(lvl + 1) end
  end
end

-- ElvUI's engine and Skins module when ElvUI skins the character panel (its
-- Character skin, on by default; Skins\Skins.lua LoadSkin asks the same), nil
-- when it does not, "later" while ElvUI has not read its settings yet.
local function elvCharacterSkin()
  local eng = _G.ElvUI
  local EV = type(eng) == "table" and eng[1]
  if type(EV) ~= "table" or type(EV.GetModule) ~= "function" then return nil end
  local ok, S = pcall(EV.GetModule, EV, "Skins", true)
  if not ok or type(S) ~= "table" or type(S.HandleCheckBox) ~= "function" then return nil end
  local P = EV.private
  local bz = type(P) == "table" and type(P.skins) == "table" and P.skins.blizzard
  if type(bz) ~= "table" then return "later" end
  if not (bz.enable and bz.character) then return nil end
  return EV, S
end
E.ElvCharacterSkin = elvCharacterSkin   -- Consumes.lua: ElvUI's look for its panel too

-- With ElvUI's skin the box gets ElvUI's check box look too, once ElvUI is set
-- up (at login). true = done (skinned, or nothing to do); false = ask again.
local function elvSkin(cb)
  local EV, S = elvCharacterSkin()
  if EV == "later" then return false end
  if not EV then return true end
  if type(S.db) ~= "table" or not (S.Initialized or EV.Initialized) then return false end
  if not pcall(S.HandleCheckBox, S, cb) then
    for k, v in pairs(TOGGLE_ART) do
      if type(cb[k]) == "function" then pcall(cb[k], cb, v) end
    end
  end
  return true
end

-- Where the box goes, asked again each time it shows. Just above the head slot
-- (it follows the slot when a skin moves it). ElvUI's skin moves the head slot
-- to the pane's edge and the model's framed box to 46px in, where the label
-- would run over the model's border: there the box goes in the top left
-- corner, over the slot column (the portrait and the strip are hidden there).
local function placeToggle()
  local EV = elvCharacterSkin()
  local pane, head = _G.CharacterFrameLeftPaneHost, _G.CharacterHeadSlot
  local where = "frame"
  if EV and EV ~= "later" and isFrame(pane) then where = "corner" elseif isFrame(head) then where = "head" end
  if toggle.glPlace == where then return end
  toggle.glPlace = where
  toggle:ClearAllPoints()
  if where == "corner" then
    toggle:SetPoint("TOPLEFT", pane, "TOPLEFT", 2, -4)
  elseif where == "head" then
    toggle:SetPoint("BOTTOMLEFT", head, "TOPLEFT", -3, 1)
  else
    toggle:SetPoint("TOPLEFT", toggle:GetParent(), "TOPLEFT", 21, -57)
  end
  local b = topBtn
  if not b then return end
  b:ClearAllPoints()
  if where == "corner" then
    b:SetPoint("TOPLEFT", toggle, "BOTTOMLEFT", 2, -2)
  elseif where == "head" then
    b:SetPoint("TOPLEFT", head, "TOPRIGHT", 25, 0)
  else
    b:SetPoint("TOPLEFT", b:GetParent(), "TOPLEFT", 86, -80)
  end
end

local function onToggleShow(self)
  placeToggle()
  raiseToggle()
  refreshToggle()
  if not self.glSkinDone then self.glSkinDone = elvSkin(self) end
end

local function onToggleClick(self)
  local on = self:GetChecked() and true or false   -- the game ticks or clears it before OnClick
  setTipsOn(on)
  local play, kit = _G.PlaySound, _G.SOUNDKIT
  if type(play) == "function" and type(kit) == "table" then
    local id = on and kit.IG_MAINMENU_OPTION_CHECKBOX_ON or kit.IG_MAINMENU_OPTION_CHECKBOX_OFF
    if id then pcall(play, id) end
  end
  refreshItemTips()
  refreshToggle()
end

-- One click flips Top 2 and Top 5 (the role and the box stay as they are).
local function onTopClick()
  local S = settings()
  S.enchantsTop = topN() == 5 and 2 or 5
  local play, kit = _G.PlaySound, _G.SOUNDKIT
  local id = type(kit) == "table" and kit.IG_MAINMENU_OPTION_CHECKBOX_ON
  if type(play) == "function" and id then pcall(play, id) end
  refreshItemTips()
  refreshTop()
end

-- The Top 2 / Top 5 button: a few words you click, no window.
local function buildTop(parent, make)
  local ok, b = pcall(make, "Button", TOP_NAME, parent)
  if not ok or type(b) ~= "table" then return end
  topBtn = b
  b:SetSize(34, 14)
  local fs = b:CreateFontString(nil, "ARTWORK", "GameFontNormalSmall")
  fs:SetPoint("LEFT", b, "LEFT", 2, 0)
  b.Text = fs
  b:SetScript("OnClick", guarded(onTopClick))
  b:SetScript("OnEnter", guarded(function(self)
    self.glHover = true
    refreshTop()
    showTopTip(self)
  end))
  b:SetScript("OnLeave", guarded(function(self)
    self.glHover = nil
    local tt = _G.GameTooltip
    if owns(tt, self) then tt:Hide() end
    refreshTop()
  end))
end

local function buildToggle()
  if toggle then return true end
  local parent, make = _G.PaperDollFrame, _G.CreateFrame
  if not isFrame(parent) or type(make) ~= "function" then return false end
  local cb = make("CheckButton", TOGGLE_NAME, parent, "UICheckButtonTemplate")
  toggle = cb
  cb:SetSize(TOGGLE_SIZE, TOGGLE_SIZE)
  buildTop(parent, make)
  placeToggle()
  local fs = cb.Text or cb.text
  if type(fs) ~= "table" then
    fs = cb:CreateFontString(nil, "ARTWORK", "GameFontNormalSmall")
    fs:SetPoint("LEFT", cb, "RIGHT", -2, 0)
    cb.Text = fs
  end
  fs:SetText("Enchant tips")
  local okC, r, g, b = pcall(fs.GetTextColor, fs)
  r, g, b = plainNumber(r), plainNumber(g), plainNumber(b)
  if okC and r and g and b then labelColour = { r, g, b } end
  -- A click on the words ticks the box too.
  local okW, wide = pcall(fs.GetStringWidth, fs)
  wide = okW and plainNumber(wide) or 0
  if wide <= 0 or wide > 150 then wide = 60 end
  if type(cb.SetHitRectInsets) == "function" then cb:SetHitRectInsets(0, -wide, 0, 0) end
  cb:SetScript("OnClick", guarded(onToggleClick))
  cb:SetScript("OnEnter", guarded(showToggleTip))
  cb:SetScript("OnLeave", guarded(function(self)
    local tt = _G.GameTooltip
    if owns(tt, self) then tt:Hide() end
  end))
  cb:SetScript("OnShow", guarded(onToggleShow))
  -- Forever hides the slots (PaperDollItemsFrame) on the pet's page: the box and the button go with them.
  local items = _G.PaperDollItemsFrame
  if type(items) == "table" and type(items.HookScript) == "function" then
    items:HookScript("OnShow", guarded(function()
      if toggle then toggle:Show() end
      if topBtn then topBtn:Show() end
    end))
    items:HookScript("OnHide", guarded(function(self)
      if self:IsShown() then return end
      if toggle then toggle:Hide() end
      if topBtn then topBtn:Hide() end
    end))
    cb:SetShown(items:IsShown())
    if topBtn then topBtn:SetShown(items:IsShown()) end
  end
  raiseToggle()
  refreshToggle()
  return true
end

-- The game's UI addon makes PaperDollFrame before ours loads (Blizzard_UIPanels_Game on
-- Forever); if it comes later, the box is made then, or when the character frame opens.
local cfHooked = false
local function tryBuild()
  if toggle then return true end
  local ok, r = pcall(buildToggle)
  if not ok then
    E.lastError = r
    return true   -- do not try again
  end
  if not r and not cfHooked then
    local cf = _G.CharacterFrame
    if type(cf) == "table" and type(cf.HookScript) == "function" then
      cfHooked = pcall(cf.HookScript, cf, "OnShow", function() if not toggle then tryBuild() end end)
    end
  end
  return r
end
E.BuildToggle = tryBuild
E.RefreshToggle = function() pcall(refreshToggle) end
E.Toggle = function() return toggle end
E.TopButton = function() return topBtn end

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
-- /gl enchants
---------------------------------------------------------------------------
local function autoText(d)
  if not d.role then return "no role (this class is not known)" end
  if d.how == "talents" then
    local names = TAB_NAME[d.class]
    return sformat("%s (%s, %d points)", d.role, names and names[d.tab] or ("tree " .. tostring(d.tab)), d.tabPoints or 0)
  end
  return sformat("%s (the %s default: fewer than %d points in one talent tree)", d.role, slower(d.class or "class"), MIN_POINTS)
end

-- " Top 2 (...)." or " Top 5 (...).": which one is on, and the word for the other.
local function topStatus()
  if topN() == 5 then return " Top 5: the best, then cheaper budget options (/gl enchants top2 for the two best)." end
  return " Top 2: the two best (/gl enchants top5 adds cheaper budget options)."
end

-- Worn gear only (the default) or bags and links too: the sentence for each.
local BAGS_ON = "On worn gear, bags and links (/gl enchants bags off: worn gear only)."
local BAGS_OFF = "On worn gear only (/gl enchants bags on adds bags and links)."
local function bagsStatus()
  return " " .. (settings().enchantsBags == true and BAGS_ON or BAGS_OFF)
end

local function statusText()
  local v = settings().enchants
  if v == "off" then return "enchant tips are off. /gl enchants auto turns them back on." end
  local d = detect()
  if ROLES[v] then
    return sformat("enchant tips are for %s (picked by hand; your talents say %s). /gl enchants auto follows your talents.",
      v, autoText(d)) .. topStatus() .. bagsStatus()
  end
  return "enchant tips: auto, " .. autoText(d) .. "." .. topStatus() .. bagsStatus()
end

local CHOICES = "auto, melee, agility, tank, caster, healer, hunter, off, top2 or top5"
-- /gl enchants top2 or top5 (or just 2 or 5): how many options the tooltips show.
local TOP_WORDS = { top2 = 2, ["2"] = 2, ["top 2"] = 2, top5 = 5, ["5"] = 5, ["top 5"] = 5 }

local function enchantsCommand(rest)
  local S = settings()
  rest = slower(rest or "")
  if rest == "" or rest == "status" then
    say(statusText())
    return
  end
  -- /gl enchants bags on / off: bags, the bank and links too, or worn gear only (the default).
  local bags = smatch(rest, "^bags?%s*(%a*)$")
  if bags then
    if bags == "on" then
      S.enchantsBags = true
    elseif bags == "off" then
      S.enchantsBags = nil
    elseif bags ~= "" then
      say("use /gl enchants bags on or /gl enchants bags off.")
      return
    end
    if bags ~= "" then refreshItemTips() end
    say("enchant tips: " .. (S.enchantsBags == true and BAGS_ON or BAGS_OFF))
    return
  end
  local top = TOP_WORDS[rest]
  if top then
    S.enchantsTop = top   -- the role and the off state stay as they are
    refreshItemTips()
    refreshToggle()
    local off = S.enchants == "off" and " (the tips are off now: /gl enchants auto turns them on)" or ""
    if top == 5 then
      say("enchant tips show the top 5: the best, then cheaper budget options." .. off)
    else
      say("enchant tips show the top 2: the two best." .. off)
    end
    return
  end
  if rest == "auto" then
    S.enchants = nil
  elseif rest == "off" then
    setTipsOn(false)   -- keeps the role, for the box on the character panel
  elseif ROLES[rest] then
    S.enchants = rest
  else
    say("unknown choice. Use /gl enchants " .. CHOICES .. ".")
    return
  end
  if S.enchants ~= "off" then S.enchantsBeforeOff = nil end
  E.Invalidate()
  refreshItemTips()
  refreshToggle()
  if rest == "off" then
    say("enchant tips are off. /gl enchants auto turns them back on.")
  elseif rest == "auto" then
    say("enchant tips follow your talents: " .. autoText(detect()) .. ".")
  else
    say("enchant tips are now for " .. rest .. ".")
  end
end
E.Command = enchantsCommand
E.StatusText = statusText

local baseSlash = ns.HandleSlash
function ns.HandleSlash(msg)
  local m = type(msg) == "string" and msg or ""
  local cmd, rest = smatch(m, "^%s*(%S*)%s*(.-)%s*$")
  cmd = slower(cmd or "")
  if cmd == "enchants" or cmd == "enchant" then
    local ok, err = pcall(enchantsCommand, rest)
    if not ok then
      E.lastError = err
      say("enchant tips: " .. tostring(err))
    end
    return
  end
  if type(baseSlash) == "function" then baseSlash(msg) end
  if cmd == "tooltip" or cmd == "tooltips" then pcall(refreshToggle) end   -- Core's /gl tooltip: the label greys or not
  if cmd == "help" or cmd == "?" then
    say("/gl enchants : the best enchants on gear tooltips for your role: auto, melee, agility, tank, caster, healer, "
      .. "hunter or off (auto follows your talents); top2 shows the two best, top5 adds cheaper budget options")
    say("   the price is the mats at AH value (a kit, scope or other upgrade: its own); an enchant needs an enchanter")
    say("   the Enchant tips box on the character panel turns them off and on")
    say("   they show on worn gear only; /gl enchants bags on adds bags and links")
  end
end

---------------------------------------------------------------------------
-- Events: a respec, a new talent point or a level clears the role; a new skill, the professions
---------------------------------------------------------------------------
if type(_G.CreateFrame) == "function" then
  local ok, f = pcall(_G.CreateFrame, "Frame")
  if ok and type(f) == "table" then
    for _, ev in ipairs({ "TRAIT_CONFIG_UPDATED", "CHARACTER_POINTS_CHANGED", "PLAYER_TALENT_UPDATE",
                          "ACTIVE_TALENT_GROUP_CHANGED", "PLAYER_LEVEL_UP", "PLAYER_LOGIN", "SKILL_LINES_CHANGED" }) do
      pcall(f.RegisterEvent, f, ev)   -- an event this client does not have is skipped
    end
    pcall(f.SetScript, f, "OnEvent", function(_, event)
      -- A profession learned or a skill point (weapon skills too): only the professions are read again.
      if event == "SKILL_LINES_CHANGED" then
        skills = nil
        return
      end
      E.Invalidate()
      if event == "PLAYER_LOGIN" then
        -- A saved choice this version does not know means auto.
        local S = settings()
        if S.enchants ~= nil and S.enchants ~= "off" and not ROLES[S.enchants] then S.enchants = nil end
        -- What the tips were before off: "auto" or a role, kept only while they are off.
        local was = S.enchantsBeforeOff
        if S.enchants ~= "off" or (was ~= "auto" and not ROLES[was]) then S.enchantsBeforeOff = nil end
        -- Top 2 or Top 5, kept apart from the role; anything else is the default (Top 2).
        if S.enchantsTop ~= nil and not TOPS[S.enchantsTop] then S.enchantsTop = nil end
        -- Bags and links too only when it is true; anything else is the default (worn gear only).
        if S.enchantsBags ~= true then S.enchantsBags = nil end
      end
    end)
  end
end
