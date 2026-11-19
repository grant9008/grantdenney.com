local ADDON_NAME, ns = ...
-- Goblin Ledger: the best enchant for the gear under the mouse.
--
-- Gear in your bags (and item links): the best enchant for your role, its
-- mats and what the mats cost at today's AH value. Gear you wear (the
-- character panel): the same, plus the 2nd best (cheaper or easier to get).
-- Nothing when the item already has the best enchant (or one at least as
-- good); only the best when it has the 2nd best. The role comes from your
-- talents; /gl enchants picks one by hand or turns the lines off.
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

local E = { lastError = nil, reads = 0, builds = 0 }   -- state for /gl enchants and the tests
ns.Enchants = E

-- BEGIN DATA
-- Generated from the enchant research (enchants.json, 2026-11-18): Forever client
-- tables (wago.tools, wowf 1.60.1) checked against rankedboost's Forever pages.
-- Mats: item id = the game's name.
local MATS = {
  [4405] = "Crude Scope",
  [4406] = "Standard Scope",
  [4407] = "Accurate Scope",
  [7075] = "Core of Earth",
  [7076] = "Essence of Earth",
  [7077] = "Heart of Fire",
  [7078] = "Essence of Fire",
  [7079] = "Globe of Water",
  [7080] = "Essence of Water",
  [7081] = "Breath of Wind",
  [7082] = "Essence of Air",
  [7972] = "Ichor of Undeath",
  [8153] = "Wildvine",
  [8411] = "Lung Juice Cocktail",
  [8424] = "Gizzard Gum",
  [8831] = "Purple Lotus",
  [10546] = "Deadly Scope",
  [10548] = "Sniper Scope",
  [10940] = "Strange Dust",
  [10998] = "Lesser Astral Essence",
  [11082] = "Greater Astral Essence",
  [11083] = "Soul Dust",
  [11084] = "Large Glimmering Shard",
  [11134] = "Lesser Mystic Essence",
  [11137] = "Vision Dust",
  [11139] = "Large Glowing Shard",
  [11174] = "Lesser Nether Essence",
  [11175] = "Greater Nether Essence",
  [11176] = "Dream Dust",
  [11178] = "Large Radiant Shard",
  [11563] = "Crystal Force",
  [11732] = "Libram of Rumination",
  [11733] = "Libram of Constitution",
  [11737] = "Libram of Voracity",
  [11752] = "Black Blood of the Tormented",
  [11754] = "Black Diamond",
  [11951] = "Whipper Root Tuber",
  [11952] = "Night Dragon's Breath",
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
  -- Crude Scope (Engineering, item 4405): https://www.wowhead.com/classic/item=4405
  i4405 = { n = "Crude Scope +1", id = 30, m = { 4405, 1 }, lvl = 5 },
  -- Standard Scope (Engineering, item 4406): https://www.wowhead.com/classic/item=4406
  i4406 = { n = "Standard Scope +2", id = 32, m = { 4406, 1 }, lvl = 10 },
  -- Accurate Scope (Engineering, item 4407): https://www.wowhead.com/classic/item=4407
  i4407 = { n = "Accurate Scope +3", id = 33, m = { 4407, 1 }, lvl = 20 },
  -- Deadly Scope (Engineering, item 10546): https://www.wowhead.com/classic/item=10546
  i10546 = { n = "Deadly Scope +5", id = 663, m = { 10546, 1 }, lvl = 30 },
  -- Sniper Scope (Engineering, item 10548): https://www.wowhead.com/classic/item=10548
  i10548 = { n = "Sniper Scope +7", id = 664, m = { 10548, 1 }, lvl = 40 },
  -- Enchant Cloak - Minor Agility (spell 13419, high): https://rankedboost.com/world-of-warcraft/forever/enchants/cloak/#e-recipe-13419 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=247
  s13419 = { n = "Minor Agility +3", id = 247, m = { 10998, 1 } },
  -- Enchant Boots - Lesser Spirit (spell 13687, high): https://rankedboost.com/world-of-warcraft/forever/enchants/boots/#e-recipe-13687 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8203
  s13687 = { n = "Lesser Spirit +4", id = 8203, m = { 11134, 2 } },
  -- Enchant Cloak - Greater Defense (spell 13746, high): https://rankedboost.com/world-of-warcraft/forever/enchants/cloak/#e-recipe-13746 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=884
  s13746 = { n = "Greater Defense +60", id = 884, m = { 11137, 1 } },
  -- Enchant Cloak - Resistance (spell 13794, high): https://rankedboost.com/world-of-warcraft/forever/enchants/cloak/#e-recipe-13794 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=903
  s13794 = { n = "Resistance +3", id = 903, m = { 11174, 1 } },
  -- Enchant Gloves - Agility (spell 13815, high): https://rankedboost.com/world-of-warcraft/forever/enchants/gloves/#e-recipe-13815 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1887
  s13815 = { n = "Agility +7", id = 1887, m = { 11137, 1 } },
  -- Enchant Shield - Stamina (spell 13817, high): https://rankedboost.com/world-of-warcraft/forever/enchants/shield/#e-recipe-13817 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=929
  s13817 = { n = "Stamina +7", id = 929, m = { 11137, 2 } },
  -- Enchant Boots - Stamina (spell 13836, high): https://rankedboost.com/world-of-warcraft/forever/enchants/boots/#e-recipe-13836 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=852
  s13836 = { n = "Stamina +5", id = 852, m = { 11137, 2 } },
  -- Enchant Bracer - Deflection (spell 13931, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-13931 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=923
  s13931 = { n = "Deflection +7", id = 923, m = { 11174, 2, 11176, 1 } },
  -- Enchant Boots - Agility (spell 13935, high): https://rankedboost.com/world-of-warcraft/forever/enchants/boots/#e-recipe-13935 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=904
  s13935 = { n = "Agility +5", id = 904, m = { 11175, 1 } },
  -- Enchant Bracer - Greater Strength (spell 13939, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-13939 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=927
  s13939 = { n = "Greater Strength +7", id = 927, m = { 11176, 1, 11174, 2 } },
  -- Enchant Chest - Stats (spell 13941, high): https://rankedboost.com/world-of-warcraft/forever/enchants/chest/#e-recipe-13941 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=928
  s13941 = { n = "Stats +3", id = 928, m = { 11178, 1, 11176, 1, 11175, 1 } },
  -- Enchant Weapon - Greater Striking (spell 13943, high): https://rankedboost.com/world-of-warcraft/forever/enchants/weapon/#e-recipe-13943 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=805
  s13943 = { n = "Greater Striking +4", id = 805, m = { 11178, 1, 11175, 1 } },
  -- Enchant Gloves - Minor Haste (spell 13948, high): https://rankedboost.com/world-of-warcraft/forever/enchants/gloves/#e-recipe-13948 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=931
  s13948 = { n = "Minor Haste +1%", id = 931, m = { 11178, 1, 8153, 1 } },
  -- Enchant Bracer - Greater Intellect (spell 20008, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-20008 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1883
  s20008 = { n = "Greater Intellect +7", id = 1883, m = { 16202, 2 } },
  -- Enchant Bracer - Superior Strength (spell 20010, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-20010 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1885
  s20010 = { n = "Superior Strength +9", id = 1885, m = { 16204, 6, 16203, 6 } },
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
  -- Enchant Weapon - Crusader (spell 20034, high): https://rankedboost.com/world-of-warcraft/forever/enchants/weapon/#e-recipe-20034 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1900
  s20034 = { n = "Crusader", id = 1900, m = { 14344, 4, 12811, 2 } },
  -- Enchant 2H Weapon - Major Intellect (spell 20036, high): https://rankedboost.com/world-of-warcraft/forever/enchants/two-handed-weapon/#e-recipe-20036 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=1904
  s20036 = { n = "Major Intellect +9", id = 1904, m = { 16203, 12, 14344, 2 } },
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
  -- Enchant 2H Weapon - Mighty Healing Power (spell 1248636, high): https://rankedboost.com/world-of-warcraft/forever/enchants/two-handed-weapon/#e-recipe-1248636 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8212
  s1248636 = { n = "Mighty Healing Power +100", id = 8212, m = { 248819, 4, 16203, 6, 249726, 4, 249274, 4, 20725, 4, 248820, 4 } },
  -- Enchant Gloves - Superior Strength (spell 1248640, high): https://rankedboost.com/world-of-warcraft/forever/enchants/gloves/#e-recipe-1248640 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=2563
  s1248640 = { n = "Superior Strength +15", id = 2563, m = { 20725, 4, 16203, 8, 7076, 4 } },
  -- Enchant Gloves - Natural Power (spell 1248645, high): https://rankedboost.com/world-of-warcraft/forever/enchants/gloves/#e-recipe-1248645 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8213
  s1248645 = { n = "Natural Power +20", id = 8213, m = { 20725, 2, 14344, 5, 248822, 4 } },
  -- Enchant Bracer - Superior Deflection (spell 1248665, high): https://rankedboost.com/world-of-warcraft/forever/enchants/bracer/#e-recipe-1248665 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8214
  s1248665 = { n = "Superior Deflection +9", id = 8214, m = { 14344, 3 } },
  -- Enchant Weapon - Insight (spell 1248757, high): https://rankedboost.com/world-of-warcraft/forever/enchants/weapon/#e-recipe-1248757 https://wago.tools/db2/SpellItemEnchantment?build=1.60.1.70959&filter[ID]=8216
  s1248757 = { n = "Insight", id = 8216, m = { 10940, 10, 11084, 4, 11082, 4, 249391, 4, 249399, 4 } },
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
  -- get Agility +5, and a necklace with Deflection on is left alone (up = { 925 }).
}

-- Best first: the first entry the player can use is the best, the next one the 2nd.
-- up = enchant ids that count as the best or better (nothing is shown),
-- mid = ids better than the 2nd (only the best is shown).
local PICKS = {
  weapon = {
    ["melee"] = { "s20034", "s13943", mid = { 2563, 1897 } },
    ["agility"] = { "s20034", "s13943", mid = { 2564, 1897 } },
    ["tank"] = { "s20034", "s13943", mid = { 2564, 1897 } },
    ["agility/DRUID"] = { "s23800" },
    ["tank/DRUID"] = { "s23800" },
    ["hunter"] = { "s23800" },
    ["caster"] = { "s23804", "s1248805", up = { 2504 } },
    ["caster/MAGE3"] = { "s23804", "s1248805", up = { 2504, 2443 } },
    ["healer"] = { "s23803", "s1248757", up = { 2505 }, mid = { 2568 } },
    ["healer/PALADIN"] = { "s23804", up = { 2505 } },
    ["healer/SHAMAN"] = { "s23804", up = { 2505 } },
  },
  weapon2h = {
    ["melee"] = { "s20034", "s1248511", up = { 8215 }, mid = { 2646, 1896 } },
    ["agility"] = { "s27837", "s1248510", mid = { 2564 } },
    ["hunter"] = { "s27837", "s1248510", mid = { 2564 } },
    ["tank"] = { "s27837", "s1248510", mid = { 2564 } },
    ["caster"] = { "s1219580", "s20036", mid = { 8211, 2568 } },
    -- Mighty Spell Power (8211) and the "Enchant Weapon" Mighty Intellect / Spirit (2568 / 2567) fit
    -- two-handers too and beat Major Intellect: with one of them on, the best only.
    ["healer"] = { "s1248636", "s20036", mid = { 7662, 8211, 2568, 2567 } },
  },
  shield = {
    ["melee"] = { "s20017", "s13817", up = { 7663 } },
    ["tank"] = { "s20017", "s13817", up = { 7663 } },
    ["caster"] = { "s20017", "s13817", up = { 7663 } },
    ["healer"] = { "s20017", "s13817", up = { 7663 } },
  },
  holdable = {
    ["caster"] = { "s1219579", up = { 7659, 7660 } },
    ["healer"] = { "s1219579", up = { 7659, 7660 } },
  },
  chest = {
    ["melee"] = { "s20025", "s13941" },
    ["agility"] = { "s20025", "s13941" },
    ["hunter"] = { "s20025", "s13941" },
    ["tank"] = { "s20025", "s20026" },
    ["caster"] = { "s20025", "s20028" },
    ["healer"] = { "s20025", "s20028" },
  },
  cloak = {
    ["melee"] = { "s1219587", "s13419", mid = { 849 } },
    ["agility"] = { "s1219587", "s13419", mid = { 849 } },
    ["hunter"] = { "s1219587", "s13419", mid = { 849 } },
    ["tank"] = { "s20015", "s13746", up = { 2622 } },
    ["caster"] = { "s20014", "s13794", up = { 2621 } },
    ["healer"] = { "s20014", "s13794", up = { 2621 } },
  },
  wrist = {
    ["melee"] = { "s20010", "s13939" },
    ["agility"] = { "s1248599", "s1248500" },
    ["hunter"] = { "s1248599", "s1248500" },
    ["tank"] = { "s1248665", "s13931", mid = { 1886 } },
    ["caster"] = { "s1220624", "s20008", mid = { 1901 } },
    ["healer"] = { "s23802", "s1248498", mid = { 7665, 2565 } },
  },
  hands = {
    ["melee"] = { "s1248640", "s20013" },
    ["agility"] = { "s20012", "s13815", up = { 2564 } },
    ["hunter"] = { "s20012", "s13815", up = { 2564 } },
    ["tank"] = { "s20012", "s13815", up = { 2613, 2564, 2563 } },
    ["caster"] = { "s13948" },
    ["caster/MAGE1"] = { "s1213626", "s13948" },
    ["caster/MAGE2"] = { "s13948", up = { 2616 } },
    ["caster/MAGE3"] = { "s13948", up = { 2615 } },
    ["caster/WARLOCK"] = { "s13948", up = { 2614 } },
    ["caster/PRIEST"] = { "s13948", up = { 2614 } },
    ["caster/DRUID1"] = { "s1213626", "s13948", mid = { 8213 } },
    ["caster/SHAMAN1"] = { "s1248645", "s13948" },
    ["healer"] = { "s13948", up = { 2617 } },
  },
  feet = {
    ["melee"] = { "s20023", "s13935" },
    ["agility"] = { "s20023", "s13935" },
    ["hunter"] = { "s20023", "s13935" },
    ["tank"] = { "s20020", "s13836" },
    ["caster"] = { "s20020", "s13836" },
    ["healer"] = { "s20024", "s13687" },
    ["healer/PALADIN"] = { "s20020", "s13836" },
    ["healer/SHAMAN"] = { "s20020", "s13836" },
  },
  neck = {
    ["melee"] = { "s1249019" },
    ["agility"] = { "s1249059" },
    ["hunter"] = { "s1249059" },
    ["tank"] = { "s1249059", up = { 925 } },
    ["caster"] = { "s1249057" },
    ["healer"] = { "s1249058" },
  },
  headlegs = {
    ["melee"] = { "a18329", "a11645" },
    ["agility"] = { "a18329", "a11647" },
    ["hunter"] = { "a11647" },
    ["tank"] = { "a18331", "a11642" },
    ["caster"] = { "a18330", "a11648" },
    ["healer"] = { "a18330", "a11622" },
  },
  ranged = {
    ["hunter"] = { "i10548", "i10546", "i4407", "i4406", "i4405", up = { 2523, 8720 } },
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
  INVTYPE_HEAD = "headlegs", INVTYPE_LEGS = "headlegs",            -- Arcanums (level 50 quests)
  INVTYPE_RANGED = "ranged", INVTYPE_RANGEDRIGHT = "ranged",       -- scopes (bows, guns, crossbows)
  -- Shoulders, rings, trinkets, belts, shirts, tabards, thrown, relics: no option worth showing.
}
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

-- Slots never change: one lookup per item id a session.
local factMemo = {}
local function facts(itemID)
  local f = factMemo[itemID]
  if f ~= nil then return f or nil end
  local loc, cls, sub = itemFacts(itemID)
  if loc == nil then return nil end   -- not readable now: ask again next time
  local g = groupFor(loc, cls, sub)
  f = g and { g = g, cls = cls, sub = sub, loc = loc } or false
  factMemo[itemID] = f
  return f or nil
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

-- How many options each place shows: the 2nd best only for gear you wear.
local SHOW = { bag = 1, link = 1, script = 1, equipped = 2 }
E.SHOW = SHOW

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

local lineMemo, lineStamp = {}, nil
function E.Invalidate()
  cache, cacheAt = nil, nil
  lineMemo = {}
  factMemo = {}
end

---------------------------------------------------------------------------
-- The picks for an item, and which of them to show
---------------------------------------------------------------------------
-- role/CLASStab (top talent tree), then role/CLASS, then the role's own.
local function pickFor(group, role, class, tab)
  local g = PICKS[group]
  if not g then return nil end
  return (class and tab and g[role .. "/" .. class .. tab]) or (class and g[role .. "/" .. class]) or g[role]
end

-- The best and the 2nd: the first two entries the player's level can use.
local function choose(rec, level)
  local best, bi, second, si
  for i, key in ipairs(rec) do
    local e = ENCHANTS[key]
    if e and (not e.lvl or level >= e.lvl) then
      if not best then
        best, bi = key, i
      else
        second, si = key, i
        break
      end
    end
  end
  return best, bi, second, si
end

local function listHas(list, v)
  if type(list) ~= "table" then return false end
  for _, x in ipairs(list) do
    if x == v then return true end
  end
  return false
end

-- 0 = nothing (it has the best, or one at least as good), 1 = the best only
-- (it has the 2nd best, or one between the two), 2 = both (no enchant, or another).
local function wanted(rec, current, bi, si)
  if not current then return 2 end
  if listHas(rec.up, current) then return 0 end
  for i, key in ipairs(rec) do
    if ENCHANTS[key].id == current then
      if i <= bi then return 0 end
      if i == si then return 1 end
      return 2
    end
  end
  if listHas(rec.mid, current) then return 1 end
  return 2
end

---------------------------------------------------------------------------
-- Costs (the mats at AH value) and the lines
---------------------------------------------------------------------------
local function dataStamp()
  local db = ns.db
  return type(db) == "table" and tonumber(db.updated) or 0
end

local costMemo = {}
-- { copper = mats with an AH value (and a quest fee), missing = { "2 Skin of Shadow", ... } }
local function costOf(key)
  local c = costMemo[key]
  if c then return c end
  local e = ENCHANTS[key]
  local copper, missing, m = e.fee or 0, {}, e.m
  for i = 1, #m, 2 do
    local id, n = m[i], m[i + 1]
    local it = type(ns.GetItem) == "function" and ns.GetItem(id) or nil
    local v = it and tonumber(it.market) or 0
    if v > 0 then
      copper = copper + v * n
    else
      missing[#missing + 1] = n .. " " .. (MATS[id] or ("Item " .. id))
    end
  end
  c = { copper = copper, missing = missing }
  costMemo[key] = c
  return c
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
-- "no AH price"; nil without price data. Second value: true = grey.
local function costText(key, stale)
  if dataStamp() <= 0 then return nil end
  local c = costOf(key)
  if c.copper <= 0 then return "no AH price", true end
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

-- The lines for a best (and 2nd) pick: { left, right, grey } rows; mats rows have mats = true.
local function buildLines(best, second, full, stale)
  E.builds = E.builds + 1
  local out = {}
  local function option(label, key)
    local cost, grey = costText(key, stale)
    out[#out + 1] = { label .. ENCHANTS[key].n, cost, grey }
    if full then out[#out + 1] = { "  " .. matsText(key), mats = true } end
  end
  option("Best enchant: ", best)
  if second then option("2nd best: ", second) end
  return out
end

local function linesFor(best, second, full)
  local stamp = dataStamp()
  if lineStamp ~= stamp then
    lineMemo, costMemo, lineStamp = {}, {}, stamp
  end
  -- The age grows while the data stays the same: whether it is old is part of the key.
  local stale = stalePrices()
  local k = best .. "|" .. (second or "") .. "|" .. (full and "f" or "c") .. (stale and "|s" or "")
  local l = lineMemo[k]
  if not l then
    l = buildLines(best, second, full, stale)
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

-- The options for an item at a place, or nil: best key, 2nd key (or nil).
local function optionsFor(itemID, link, where)
  local most = SHOW[where]
  if not most then return nil end
  local f = facts(itemID)
  if not f then return nil end
  if quality(itemID, link) == 0 then return nil end   -- grey junk
  local role, _, d = currentRole()
  if not role or not canWear(d.class, f) then return nil end
  local rec = pickFor(f.g, role, d.class, d.tab)
  if not rec then return nil end
  local best, bi, second, si = choose(rec, d.level or 60)
  if not best then return nil end
  local n = wanted(rec, linkEnchant(link), bi, si)
  if n > most then n = most end
  if n <= 0 then return nil end
  return best, n > 1 and second or nil
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
  if type(link) ~= "string" or isSecret(link) then link = fallbackLink(info) end
  if not link then return false end   -- without the link the current enchant is unknown
  local best, second = optionsFor(itemID, link, where)
  if not best then return false end
  draw(tt, linesFor(best, second, not S.compact or shiftDown()))
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

local function statusText()
  local v = settings().enchants
  if v == "off" then return "enchant tips are off. /gl enchants auto turns them back on." end
  local d = detect()
  if ROLES[v] then
    return sformat("enchant tips are for %s (picked by hand; your talents say %s). /gl enchants auto follows your talents.",
      v, autoText(d))
  end
  return "enchant tips: auto, " .. autoText(d) .. "."
end

local CHOICES = "auto, melee, agility, tank, caster, healer, hunter or off"

local function enchantsCommand(rest)
  local S = settings()
  rest = slower(rest or "")
  if rest == "" or rest == "status" then
    say(statusText())
    return
  end
  if rest == "auto" then
    S.enchants = nil
  elseif rest == "off" or ROLES[rest] then
    S.enchants = rest
  else
    say("unknown choice. Use /gl enchants " .. CHOICES .. ".")
    return
  end
  E.Invalidate()
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
  if cmd == "help" or cmd == "?" then
    say("/gl enchants : the best enchant on gear tooltips for your role: " .. CHOICES .. " (auto follows your talents)")
    say("   the price is the mats at AH value; you still need an enchanter who knows the formula")
  end
end

---------------------------------------------------------------------------
-- Events: a respec, a new talent point or a level clears the role
---------------------------------------------------------------------------
if type(_G.CreateFrame) == "function" then
  local ok, f = pcall(_G.CreateFrame, "Frame")
  if ok and type(f) == "table" then
    for _, ev in ipairs({ "TRAIT_CONFIG_UPDATED", "CHARACTER_POINTS_CHANGED", "PLAYER_TALENT_UPDATE",
                          "ACTIVE_TALENT_GROUP_CHANGED", "PLAYER_LEVEL_UP", "PLAYER_LOGIN" }) do
      pcall(f.RegisterEvent, f, ev)   -- an event this client does not have is skipped
    end
    pcall(f.SetScript, f, "OnEvent", function(_, event)
      E.Invalidate()
      if event == "PLAYER_LOGIN" then
        -- A saved choice this version does not know means auto.
        local S = settings()
        if S.enchants ~= nil and S.enchants ~= "off" and not ROLES[S.enchants] then S.enchants = nil end
      end
    end)
  end
end
