local ADDON_NAME, ns = ...
-- Goblin Ledger: bag cleanup.
-- When your bags fill up, this says what to get rid of first:
--   Merge stacks    : partial stacks of the same item that fit in one slot
--                     (frees a slot and costs nothing)
--   Old quest items : with Questie, quest items whose quests are all done or
--                     not in your quest log (its database says which quests
--                     use an item, and a quest item's tooltip says it too),
--                     and ones that may still be needed (check first); else
--                     quest items no quest in your log asks for, and items
--                     that start a quest you already finished. A quest in
--                     your log that names an item always keeps it
--   Worth the least : everything else, cheapest stack first, valued at the
--                     better of what a vendor pays and what it sells for at
--                     auction right now (after the 5% cut)
-- It shows as a Bag cleanup button on your open bags (in the header of
-- ElvUI's bag window when there is room, else a tab beside the bags) that
-- opens a slim panel, and in chat with /gl bags. Nothing is destroyed or
-- sold on its own. At an open vendor a click on a row sells the stack when
-- the vendor buys it. Otherwise a click on a row (or Drop cheapest) only
-- asks, naming the stack and what it is worth; Drop it in that box checks the
-- stack and its worth again and destroys that one stack (the game allows one
-- per click). Rare and better items and quest items are only picked up, so
-- the game's own check (type DELETE, quest warning) applies. /gl bagcheck
-- says what the cleanup sees; /gl questcheck why a quest item stays or can go.

ns = ns or {}

local _G = _G
local type, tonumber, tostring, pairs, ipairs, pcall, next = type, tonumber, tostring, pairs, ipairs, pcall, next
local floor, max, min, ceil, huge = math.floor, math.max, math.min, math.ceil, math.huge
local sort, tconcat = table.sort, table.concat
local slower, sformat, sgsub, smatch, sgmatch = string.lower, string.format, string.gsub, string.match, string.gmatch

local W = 236               -- panel width
local PAD = 8
local HEADER_H = 40
local TITLE_H = 16
local ROW_H = 20
local GAP = 6
local TAB_W, TAB_H = 170, 20
local HEADER_W, HEADER_MIN = 150, 100   -- the button in ElvUI's bag header: width, narrowest it may get
local FOOTER_H = 26
local MAX_MERGE = 3
local MAX_QUEST = 6
local MAX_DROP = 8
local SLASH_DROP = 5        -- /gl bags prints this many
local SELL_BATCH = 12       -- junk sold per click (the vendor's buyback holds 12)
local OUTGROWN = 15         -- food, potions and the like this many levels under you are fair game
local DROP_MAX_QUALITY = 2  -- one-click drop only below Rare (the game makes you type DELETE for Rare and up)
local DROP_GUARD = 0.5      -- seconds: Drop it ignores a click this soon after the box opens or after
                            -- the last click on it, so a double-click (the OS allows 0.5 s) or a burst never drops
local CONFIRM_H = 120       -- the panel is at least this tall while the drop box shows
local BAGS_BUILD = "2026-11-18i"   -- /gl bagcheck names it, so a screenshot says which Bags.lua runs

local CLASS_CONSUMABLE, CLASS_WEAPON, CLASS_REAGENT, CLASS_PROJECTILE = 0, 2, 5, 6
local CLASS_QUIVER, CLASS_QUEST, CLASS_KEY = 11, 12, 13
local SUB_FISHING_POLE = 20
local BIND_PICKUP, BIND_QUEST = 1, 4

-- Never suggested: things you keep for good even though a vendor pays little.
local PROTECT = {
  [6948] = true,                                   -- Hearthstone
  [2901] = true,                                   -- Mining Pick
  [7005] = true,                                   -- Skinning Knife
  [5956] = true,                                   -- Blacksmith Hammer
  [6219] = true,                                   -- Arclight Spanner
  [6218] = true, [6339] = true, [11130] = true,    -- Runed Copper, Silver and Golden Rods
  [11145] = true, [16207] = true,                  -- Runed Truesilver and Arcanite Rods
  [6256] = true, [6365] = true, [6366] = true,     -- fishing poles
  [6367] = true, [12225] = true, [19022] = true, [19970] = true,
  [5060] = true,                                   -- Thieves' Tools
  [9149] = true,                                   -- Philosopher's Stone
  [10498] = true,                                  -- Gyromatic Micro-Adjustor
  [4471] = true,                                   -- Flint and Tinder
  [6265] = true,                                   -- Soul Shard
}

-- Quest items for repeatable turn-ins: no quest in your log asks for them,
-- yet they are worth keeping.
local REPEATABLE = {
  [12840] = true, [12841] = true, [12843] = true,  -- Scourgestones
  [12844] = true,                                  -- Argent Dawn Valor Token
  [22523] = true, [22524] = true,                  -- Insignia of the Dawn and of the Crusade
  [22525] = true, [22526] = true, [22527] = true, [22528] = true, [22529] = true, -- Argent Dawn turn-ins
  [21377] = true,                                  -- Deadwood Headdress Feather
  [21383] = true,                                  -- Winterfall Spirit Beads
  [20404] = true,                                  -- Encrypted Twilight Text
  [20513] = true, [20514] = true, [20515] = true,  -- Abyssal Crest and Signets
}
for id = 19698, 19715 do REPEATABLE[id] = true end -- Zandalar coins and bijous

local WHITE = "Interface\\Buttons\\WHITE8X8"
local QUESTION = "Interface\\Icons\\INV_Misc_QuestionMark"

local FALLBACK_COLORS = {
  buy = { 0.898, 0.722, 0.259 }, sell = { 0.22, 0.74, 0.97 },
  pos = { 0.25, 0.80, 0.45 }, neg = { 0.90, 0.33, 0.33 },
  muted = { 0.55, 0.51, 0.45 }, text = { 0.90, 0.88, 0.84 },
  bg = { 0.043, 0.055, 0.078, 0.96 }, panel = { 0.07, 0.08, 0.10, 0.96 },
}

---------------------------------------------------------------------------
-- Small helpers
---------------------------------------------------------------------------
local function Col(key)
  local c = ns.COLORS and ns.COLORS[key]
  if type(c) == "table" then return c end
  return FALLBACK_COLORS[key] or FALLBACK_COLORS.text
end

local function Hex(c)
  if type(ns.Hex) == "function" then return ns.Hex(c) end
  return sformat("ff%02x%02x%02x", floor(c[1] * 255 + 0.5), floor(c[2] * 255 + 0.5), floor(c[3] * 255 + 0.5))
end

local function Paint(text, key)
  return "|c" .. Hex(Col(key)) .. tostring(text) .. "|r"
end

local function IsSecret(v)
  local f = _G.issecretvalue
  if type(f) ~= "function" then return false end
  local ok, r = pcall(f, v)
  return ok and r == true
end

local function Num(v)
  if type(v) ~= "number" or IsSecret(v) then return nil end
  if v ~= v or v == huge or v == -huge then return nil end
  return v
end

local function Str(v)
  if type(v) == "string" and not IsSecret(v) then return v end
  return nil
end

local function Print(msg)
  if type(ns.Print) == "function" then return ns.Print(msg) end
  local cf = _G.DEFAULT_CHAT_FRAME
  if type(cf) == "table" and cf.AddMessage then cf:AddMessage("Goblin Ledger: " .. tostring(msg)) end
end

local function After(sec, fn)
  local T = _G.C_Timer
  if type(T) == "table" and type(T.After) == "function" then
    T.After(sec, fn)
  else
    fn()
  end
end

local function Money(c)
  if type(ns.ShortMoney) == "function" then return ns.ShortMoney(c) end
  if type(ns.MoneyText) == "function" then return ns.MoneyText(c) end
  return tostring(floor(c or 0)) .. "c"
end

local function ChatMoney(c)
  if type(ns.MoneyText) == "function" then return ns.MoneyText(c) end
  return Money(c)
end

local function S()
  return type(ns.settings) == "table" and ns.settings or nil
end

local function KeepList()
  local st = S()
  if not st then return {} end
  if type(st.bagKeep) ~= "table" then st.bagKeep = {} end
  return st.bagKeep
end

local function AHCut()
  return Num(ns.AH_CUT) or 0.95
end

local function PlayerLevel()
  local f = _G.UnitLevel
  if type(f) ~= "function" then return nil end
  local ok, lv = pcall(f, "player")
  lv = ok and Num(lv) or nil
  if lv and lv > 0 then return lv end
  return nil
end

---------------------------------------------------------------------------
-- Bags (C_Container, or the older globals)
---------------------------------------------------------------------------
local function ContainerFn(name)
  local CC = _G.C_Container
  local f = type(CC) == "table" and CC[name] or nil
  if type(f) == "function" then return f end
  f = _G[name]
  if type(f) == "function" then return f end
  return nil
end

local function BagIDs()
  local ids = {}
  local last = Num(_G.NUM_BAG_SLOTS) or 4
  for b = 0, last do ids[#ids + 1] = b end
  local reagent = Num(_G.NUM_REAGENTBAG_SLOTS) or 0
  for b = last + 1, last + reagent do ids[#ids + 1] = b end
  return ids
end

local function NumSlots(bag)
  local f = ContainerFn("GetContainerNumSlots")
  if not f then return 0 end
  local ok, n = pcall(f, bag)
  return ok and Num(n) or 0
end

-- Free slots and the bag's family (0 = a normal bag, anything else is a
-- herb, ammo or other special bag).
local function FreeAndFamily(bag)
  local f = ContainerFn("GetContainerNumFreeSlots")
  if not f then return nil, 0 end
  local ok, free, fam = pcall(f, bag)
  if not ok then return nil, 0 end
  return Num(free), Num(fam) or 0
end

local function SlotInfo(bag, slot)
  local CC = _G.C_Container
  if type(CC) == "table" and type(CC.GetContainerItemInfo) == "function" then
    local ok, t = pcall(CC.GetContainerItemInfo, bag, slot)
    if ok and type(t) == "table" then return t end
    return nil
  end
  local f = _G.GetContainerItemInfo
  if type(f) ~= "function" then return nil end
  local ok, icon, count, locked, quality, _, _, link, _, noValue, id = pcall(f, bag, slot)
  if not ok or icon == nil then return nil end
  return { iconFileID = icon, stackCount = count, isLocked = locked, quality = quality,
           hyperlink = link, hasNoValue = noValue, itemID = id }
end

local function SlotQuest(bag, slot)
  local f = ContainerFn("GetContainerItemQuestInfo")
  if not f then return nil end
  local ok, a, b, c = pcall(f, bag, slot)
  if not ok then return nil end
  if type(a) == "table" then return a end
  return { isQuestItem = a, questID = b, isActive = c }
end

local function ItemInfo(x)
  if x == nil then return nil end
  local CI = _G.C_Item
  local f = (type(CI) == "table" and type(CI.GetItemInfo) == "function") and CI.GetItemInfo or _G.GetItemInfo
  if type(f) ~= "function" then return nil end
  local r = { pcall(f, x) }
  if not r[1] or Str(r[2]) == nil then return nil end
  return {
    name = Str(r[2]), link = Str(r[3]), quality = Num(r[4]), minLevel = Num(r[6]),
    maxStack = Num(r[9]), icon = r[11], vendor = Num(r[12]), classID = Num(r[13]),
    subclassID = Num(r[14]), bind = Num(r[15]),
  }
end

-- Whether an item has a Use effect (a tool like a scrying phial): true, false,
-- or nil when the game has not loaded the item yet (then it may be one).
local function ItemUsable(id)
  local CI = _G.C_Item
  local f = (type(CI) == "table" and type(CI.GetItemSpell) == "function") and CI.GetItemSpell or _G.GetItemSpell
  if type(f) ~= "function" then return nil end
  local ok, spell = pcall(f, id)
  if ok and spell ~= nil and not IsSecret(spell) then return true end
  if ItemInfo(id) then return false end
  return nil
end

-- Class and icon without the item being loaded yet.
local function InstantInfo(id)
  local CI = _G.C_Item
  local f = (type(CI) == "table" and type(CI.GetItemInfoInstant) == "function") and CI.GetItemInfoInstant or _G.GetItemInfoInstant
  if type(f) ~= "function" then return nil end
  local ok, _, _, _, _, icon, classID, subclassID = pcall(f, id)
  if not ok then return nil end
  return Num(classID), Num(subclassID), icon
end

local function RequestLoad(id)
  local CI = _G.C_Item
  if type(CI) == "table" and type(CI.RequestLoadItemDataByID) == "function" then
    pcall(CI.RequestLoadItemDataByID, id)
  end
end

---------------------------------------------------------------------------
-- Quest log: which item names the quests you have ask for
---------------------------------------------------------------------------
local STOP = { the = true, ["and"] = true, ["for"] = true, from = true, with = true, into = true,
  item = true, items = true, quest = true, some = true, your = true }

local function AddWords(s, out)
  s = Str(s)
  if not s then return out end
  for w in sgmatch(slower(s), "[%a']+") do
    w = sgsub(sgsub(w, "'s$", ""), "'", "")
    if #w >= 4 and not STOP[w] then out[w] = true end
  end
  return out
end

-- "Linen Cloth: 3/10" (Classic) or "3/10 Linen Cloth" (newer clients) -> "linen cloth"
local function ObjectiveName(text)
  text = Str(text)
  if not text then return nil end
  local s = sgsub(text, "%s*%d+%s*/%s*%d+%s*", " ")
  s = sgsub(sgsub(sgsub(s, "%b()", " "), ":", " "), "%s+", " ")
  s = smatch(s, "^%s*(.-)%s*$") or ""
  if s == "" then return nil end
  return slower(s)
end

-- needs: objective names; words: words of titles and objectives; ids: quest
-- ids; list: each quest { title, q (its id), index (its line in the log),
-- texts (its objectives), kinds (each one's type: "item", "monster", ...) };
-- complete: false when closed headers hide quests.
local function ReadQuestLog()
  local log = { needs = {}, words = {}, ids = {}, list = {}, count = 0, bare = 0, complete = true }
  local QL = type(_G.C_QuestLog) == "table" and _G.C_QuestLog or nil
  local function objectives(questID, index)
    local texts, kinds, read = {}, {}, false
    if QL and type(QL.GetQuestObjectives) == "function" and Num(questID) and questID > 0 then
      local ok, list = pcall(QL.GetQuestObjectives, questID)
      if ok and type(list) == "table" then
        for _, o in ipairs(list) do
          if type(o) == "table" and Str(o.text) then texts[#texts + 1], kinds[#texts + 1] = o.text, Str(o.type) end
        end
        -- Some clients give an empty list (or no texts) for a quest that has
        -- objectives: then the quest log's own lines are read too.
        read = #texts > 0
      end
    end
    if not read and index and type(_G.GetNumQuestLeaderBoards) == "function" and type(_G.GetQuestLogLeaderBoard) == "function" then
      local ok, n = pcall(_G.GetNumQuestLeaderBoards, index)
      for j = 1, (ok and Num(n) or 0) do
        local ok2, text, kind = pcall(_G.GetQuestLogLeaderBoard, j, index)
        if ok2 and Str(text) then texts[#texts + 1], kinds[#texts + 1] = text, Str(kind) end
      end
    end
    return texts, kinds
  end
  local function quest(title, questID, index)
    log.count = log.count + 1
    if Num(questID) and questID > 0 then log.ids[questID] = true end
    AddWords(title, log.words)
    local texts, kinds = objectives(questID, index)
    log.list[#log.list + 1] = { title = Str(title), q = (Num(questID) and questID > 0) and questID or nil,
                                index = Num(index), texts = texts, kinds = kinds }
    -- A quest that lists no objectives (deliver this, talk to him) may need
    -- an item it does not name.
    if #texts == 0 then log.bare = log.bare + 1 end
    for _, t in ipairs(texts) do
      local n = ObjectiveName(t)
      if n then log.needs[n] = true end
      AddWords(t, log.words)
    end
  end
  if QL and type(QL.GetNumQuestLogEntries) == "function" and type(QL.GetInfo) == "function" then
    local ok, n = pcall(QL.GetNumQuestLogEntries)
    if not ok then log.complete = false return log end
    for i = 1, Num(n) or 0 do
      local ok2, info = pcall(QL.GetInfo, i)
      if ok2 and type(info) == "table" then
        if info.isHeader then
          if info.isCollapsed then log.complete = false end
        else
          quest(info.title, info.questID, i)
        end
      end
    end
  elseif type(_G.GetNumQuestLogEntries) == "function" and type(_G.GetQuestLogTitle) == "function" then
    local ok, n = pcall(_G.GetNumQuestLogEntries)
    if not ok then log.complete = false return log end
    for i = 1, Num(n) or 0 do
      local ok2, title, _, _, isHeader, isCollapsed, _, _, questID = pcall(_G.GetQuestLogTitle, i)
      if ok2 then
        if isHeader then
          if isCollapsed then log.complete = false end
        elseif title then
          quest(title, questID, i)
        end
      end
    end
  else
    log.complete = false
  end
  return log
end

local function QuestDone(questID)
  local QL = _G.C_QuestLog
  local f = (type(QL) == "table" and type(QL.IsQuestFlaggedCompleted) == "function") and QL.IsQuestFlaggedCompleted
    or _G.IsQuestFlaggedCompleted
  if type(f) ~= "function" or not Num(questID) then return false end
  local ok, r = pcall(f, questID)
  return ok and r == true
end

local function NamedByLog(name, log)
  if not name then return false end
  if log.needs[slower(name)] then return true end
  for w in pairs(AddWords(name, {})) do
    if log.words[w] then return true end
  end
  return false
end

-- The quest log's own text, quest chains and /fah questcheck share one table:
-- Bags.lua's main chunk is close to Lua's limit of 200 locals.
local QX = {}

-- Lowercased, with a curly apostrophe made plain.
function QX.Lower(s)
  s = Str(s)
  if not s then return nil end
  return slower((sgsub(s, "\226\128\153", "'")))
end

-- Whether text holds phrase as whole words (both lowercased).
function QX.HasPhrase(text, phrase)
  if not text or not phrase or phrase == "" then return false end
  local sfind, ssub, init = string.find, string.sub, 1
  while true do
    local s, e = sfind(text, phrase, init, true)
    if not s then return false end
    if not smatch(ssub(text, s - 1, s - 1), "%w") and not smatch(ssub(text, e + 1, e + 1), "%w") then return true end
    init = s + 1
  end
end

-- Each quest's own text (its story and objectives, lowercased), kept by quest
-- id (or title) for the session: a quest's text does not change, so the log
-- is read once per quest, and never again for one already read (whatever
-- QUEST_LOG_UPDATE a read may cause). An empty text (right after login the
-- client may not have it yet) is read again, at most once every QX.WAIT
-- seconds and QX.TRIES times in all; until then the log is pending
-- (log.pending): a quest whose text is not in yet may name an item.
QX.text, QX.tries, QX.TRIES, QX.WAIT = {}, {}, 5, 2

function QX.Now()
  local f = _G.GetTime
  if type(f) ~= "function" then return nil end
  local ok, t = pcall(f)
  return ok and Num(t) or nil
end

-- How a quest is selected to read its text, and how the player's own
-- selection is put back: by log line (SelectQuestLogEntry, older clients),
-- else by quest id (C_QuestLog.SetSelectedQuest). nil when the selection
-- cannot be read, so it could not be put back: then nothing is read.
local function Selection()
  local idx, cur = _G.SelectQuestLogEntry, _G.GetQuestLogSelection
  if type(idx) == "function" and type(cur) == "function" then
    local ok, prev = pcall(cur)
    prev = ok and Num(prev) or nil
    if not prev then return nil end
    return function(e) return pcall(idx, e.index) end, function() pcall(idx, prev) end
  end
  local QL = _G.C_QuestLog
  local set = type(QL) == "table" and QL.SetSelectedQuest
  local get = type(QL) == "table" and QL.GetSelectedQuest
  if type(set) == "function" and type(get) == "function" then
    local ok, prev = pcall(get)
    if not ok then return nil end
    prev = Num(prev) or 0
    return function(e) return e.q ~= nil and pcall(set, e.q) end, function() pcall(set, prev) end
  end
  return nil
end

-- The text of the quests in log that have none yet: each one selected in
-- turn, its text read (by its log line too, which newer clients use), and
-- the player's selection put back at once. Headers are never opened or
-- closed. Every call is protected. A quest whose text was empty a moment
-- ago is not read again yet, but the log stays pending.
function QX.Texts(log)
  if log.storyRead then return end
  log.storyRead = true
  local get = _G.GetQuestLogQuestText
  if type(get) ~= "function" then return end
  local need, now, wait = {}, QX.Now(), false
  for _, e in ipairs(log.list) do
    local key = e.q or e.title
    local c = key and QX.text[key]
    if c then
      e.story = c
    elseif key and e.index then
      local tr = QX.tries[key]
      if tr and now and tr.at and now - tr.at < QX.WAIT then wait = true else need[#need + 1] = e end
    end
  end
  if #need == 0 and not wait then return end
  local pick, restore = Selection()
  if not pick then return end
  for _, e in ipairs(need) do
    local key = e.q or e.title
    local t = ""
    if pick(e) then
      local okT, desc, obj = pcall(get, e.index)
      if okT then t = (QX.Lower(desc) or "") .. "|" .. (QX.Lower(obj) or "") end
    end
    if smatch(t, "%w") then
      QX.text[key], e.story = t, t
    else
      local tr = QX.tries[key] or { n = 0 }
      tr.n, tr.at = tr.n + 1, now
      QX.tries[key] = tr
      if tr.n >= QX.TRIES then QX.text[key] = "" else wait = true end
    end
  end
  if #need > 0 then restore() end
  log.pending = wait
end

-- The quests in your log that name an item as a whole phrase: in the title,
-- an objective, or the quest's own text (read only when no title or objective
-- names it). An item objective ("Shattered Necklace Ruby: 0/1") names one
-- item, so there the whole name must match. The first one ({ title, q } from
-- log.list) and how ("title", "objective", "text"); with all, every one as a
-- list of { e = quest, how }. notext: titles and objectives only (the text
-- is read only for a quest item's verdict).
function QX.Hits(name, log, all, notext)
  local hits, seen = {}, {}
  local p = QX.Lower(name)
  if not p or p == "" or type(log) ~= "table" or type(log.list) ~= "table" then
    if all then return hits end
    return nil
  end
  for _, e in ipairs(log.list) do
    local how = QX.HasPhrase(QX.Lower(e.title), p) and "title" or nil
    if not how then
      for i, t in ipairs(e.texts) do
        local hit
        if e.kinds and e.kinds[i] == "item" then
          hit = QX.Lower(ObjectiveName(t)) == p
        else
          hit = QX.HasPhrase(QX.Lower(t), p)
        end
        if hit then how = "objective" break end
      end
    end
    if how then
      if not all then return e, how end
      hits[#hits + 1], seen[e] = { e = e, how = how }, true
    end
  end
  if notext then
    if all then return hits end
    return nil
  end
  QX.Texts(log)
  for _, e in ipairs(log.list) do
    if not seen[e] and e.story and QX.HasPhrase(e.story, p) then
      if not all then return e, "text" end
      hits[#hits + 1] = { e = e, how = "text" }
    end
  end
  if all then return hits end
  return nil
end

-- Why a quest item is no longer needed ("done", "none"), and whether we are
-- sure; nil when it may still be needed (a quest in your log names it, or
-- closed headers hide quests). While quest text is still being read
-- (log.pending) nothing is sure.
local function QuestVerdict(e, qi, log)
  if REPEATABLE[e.id] then return nil end
  local starts = qi and Num(qi.questID) or nil
  if starts and starts > 0 then
    if e.name and log.needs[slower(e.name)] then return nil end
    if not qi.isActive and QuestDone(starts) and log.complete and not QX.Hits(e.name, log) and not log.pending then
      return "done", true
    end
    return nil
  end
  if not e.name or not log.complete then return nil end
  if NamedByLog(e.name, log) or QX.Hits(e.name, log) then return nil end
  return "none", log.bare == 0 and not log.pending
end

---------------------------------------------------------------------------
-- Quest items and Questie: whether you can drop one
---------------------------------------------------------------------------
-- QuestieDB (the database Questie reads) knows which quests ask for an item,
-- which quest gives it and which quest it starts. Once per QuestieDB version
-- every quest is read, a few a frame (at most QI_BATCH quests, or about QI_MS
-- milliseconds), into an index item -> quests (the quests that ask for it,
-- as objectives or besides, and the one that gives it) that is kept in
-- ForeverAH_Settings. The quest an item starts and the quests that reward it
-- are QuestieDB's item data, read when needed. A quest item's verdict comes
-- from the quests that use it (for your race and class): the ones that ask
-- for it, the one that gives it and the quest after that one, and the one it
-- starts (QuestieDB also lists a quest starter as the item its quest gives;
-- it is handed in with that quest, so the quest after it does not count):
--   keep    one of them is in your quest log (or one is repeatable). Also
--           halfway along a chain, where a dropped item is gone for good:
--           a quest you finished gave it to you or rewarded you with it, or
--           a quest that needs it comes after one in your log; or its last
--           quest was handed in at an object where the next quest starts (a
--           scrying bowl the item calls up) and you have not taken that one.
--           An item a quest gave you is kept while a quest after that one is
--           in your log (the quests between done; for a tool, up to where a
--           quest starts at an object the one before is handed in at)
--   starts  it starts a quest you have not done
--   maybe   not sure, so never "can drop": the quest that gave you a note
--           (one you read, not use) is done but the one right after it is
--           for you and not done (why "next"), closed quest log headers
--           hide quests (why "hidden"), or a quest's own text is not in
--           yet (why "reading")
--   done    they are all done: you can drop it
--   open    you have not started them (none is in your quest log)
-- Your quest log beats QuestieDB (which may not know every quest on Forever,
-- and has items of the same name mixed up): a quest in your log whose title,
-- objectives or own text name the item keeps it, whatever QuestieDB says;
-- and while closed headers hide quests (or their text is still being read)
-- nothing is "done" or "not started".
-- Without QuestieDB, while the index is being made, for items QuestieDB does
-- not know, and when anything about it fails, quest items are judged from
-- the quest log as before (QuestVerdict).
local QDB_CONTRACT = 4      -- the LibQuestieDB contract this is written against
local QI_VERSION = 1        -- the saved index's format: a new one rebuilds it
local QI_BATCH = 150        -- quests read a frame at most
local QI_MS = 3             -- and for about this long (when debugprofilestop exists)
local QI_DELAY = 5          -- seconds after login before the index is made
local ROLE_USE, ROLE_GIVE = 0, 1   -- index codes are quest * 4 + role

-- state: "idle" (not started), "absent" (no QuestieDB), "building", "ready",
-- or "off" (QuestieDB cannot be used this session; err says why).
local QI = { state = "idle", ticks = 0, most = 0, pos = 0, total = 0, info = {} }
ns.QuestIndex = QI
local rowTip = false        -- a cleanup row draws its tooltip: the row names the quest itself

local function AddonMeta(addon, key)
  local CA = _G.C_AddOns
  local f = (type(CA) == "table" and type(CA.GetAddOnMetadata) == "function") and CA.GetAddOnMetadata
    or _G.GetAddOnMetadata
  if type(f) ~= "function" then return nil end
  local ok, v = pcall(f, addon, key)
  return ok and Str(v) or nil
end

-- LibQuestieDB when it is loaded and keeps our contract; else nil and why.
local function QuestieLib()
  local L = rawget(_G, "LibQuestieDB")
  if type(L) ~= "table" then return nil, "absent" end
  if type(L.RequireContract) ~= "function" then return nil, "no contract check" end
  local ok, good = pcall(L.RequireContract, QDB_CONTRACT)
  if not ok or good ~= true then return nil, "contract mismatch" end
  local Q, I = L.Quest, L.Item
  if type(Q) ~= "table" or type(Q.Get) ~= "function" or type(Q.GetAllIds) ~= "function"
     or type(I) ~= "table" or type(I.Get) ~= "function" then
    return nil, "no quest reads"
  end
  return L
end

-- The saved index is for one QuestieDB build: its version and commit, the
-- contract and our format. nil when QuestieDB names neither (the index is
-- then made each session and not saved).
local function IndexStamp(L)
  local name = Str(L.addonName) or "QuestieDB"
  local ver, commit = AddonMeta(name, "Version"), AddonMeta(name, "X-BUILD-COMMIT")
  if not ver and not commit then return nil end
  return sformat("%s|%s|%s|%d", ver or "", commit or "", tostring(Num(L.contractVersion) or ""), QI_VERSION)
end

-- While the index is made, an item's entry is one code, or a list of them
-- when several quests use it.
local function IndexAdd(map, item, code)
  local cur = map[item]
  if cur == nil then
    map[item] = code
  elseif type(cur) == "number" then
    if cur ~= code then map[item] = { cur, code } end
  else
    for i = 1, #cur do
      if cur[i] == code then return end
    end
    cur[#cur + 1] = code
  end
end

-- One quest into the index: the items it asks for (its objectives and the
-- items it needs besides) and the item it gives you when you take it.
local function IndexQuest(Q, q, map)
  local use, give = q * 4 + ROLE_USE, q * 4 + ROLE_GIVE
  local obj = Q.Get(q, "objectives")
  local list = type(obj) == "table" and obj[3] or nil
  if type(list) == "table" then
    for _, o in pairs(list) do
      local it = type(o) == "table" and Num(o[1]) or nil
      if it and it > 0 then IndexAdd(map, it, use) end
    end
  end
  local need = Q.Get(q, "requiredSourceItems")
  if type(need) == "table" then
    for _, it in pairs(need) do
      it = Num(it)
      if it and it > 0 then IndexAdd(map, it, use) end
    end
  end
  local src = Num(Q.Get(q, "sourceItemId"))
  if src and src > 0 then IndexAdd(map, src, give) end
end

local function ProfileMs()
  local f = _G.debugprofilestop
  if type(f) ~= "function" then return nil end
  local ok, t = pcall(f)
  return ok and Num(t) or nil
end

local function RefreshPanel()
  local bp = ns.BagPanel
  if type(bp) == "table" and type(bp.RefreshSoon) == "function" then bp:RefreshSoon() end
end

-- Anything wrong with QuestieDB or the index: quest items go back to the
-- quest log's words for this session, and the saved index is dropped.
function QI.Fail(err)
  QI.state, QI.err = "off", tostring(err)
  QI.map, QI.work, QI.ids, QI.lib = nil, nil, nil, nil
  local st = S()
  if st then st.questIndex = nil end
  RefreshPanel()
end

-- Kept compact: an item one quest uses has its code, one several quests use
-- a string of codes ("9032,10000").
local function Finish(map)
  local n = 0
  for item, v in pairs(map) do
    n = n + 1
    if type(v) == "table" then map[item] = tconcat(v, ",") end
  end
  QI.map, QI.size, QI.state, QI.work, QI.ids = map, n, "ready", nil, nil
  local st = S()
  if st and QI.stamp and ns.settingsLoaded == true then
    st.questIndex = { stamp = QI.stamp, items = map }
  end
  RefreshPanel()
end

-- One frame's share of the quests, then the next frame's.
local function Step()
  if QI.state ~= "building" then return end
  local ids, map, Q = QI.ids, QI.work, QI.lib.Quest
  local i, n, done = QI.pos, QI.total, 0
  local t0 = ProfileMs()
  while i <= n and done < QI_BATCH do
    local q = Num(ids[i])
    if q and q > 0 then IndexQuest(Q, q, map) end
    i, done = i + 1, done + 1
    local t = t0 and ProfileMs()
    if t and t - t0 >= QI_MS then break end
  end
  QI.pos, QI.ticks = i, QI.ticks + 1
  if done > QI.most then QI.most = done end
  if i > n then Finish(map) else After(0, QI.Step) end
end

function QI.Step()
  local ok, err = pcall(Step)
  if not ok then QI.Fail(err) end
end

-- The saved index when it is for this QuestieDB, else a new one made over
-- the next frames.
function QI.Start()
  local s = QI.state
  if s == "building" or s == "ready" or s == "off" then return end
  local L, why = QuestieLib()
  if not L then
    if why == "absent" then QI.state = "absent" else QI.state, QI.err = "off", why end
    return
  end
  QI.lib, QI.stamp, QI.err = L, IndexStamp(L), nil
  local st = S()
  local saved = st and st.questIndex
  if QI.stamp and type(saved) == "table" and saved.stamp == QI.stamp and type(saved.items) == "table" then
    local n = 0
    for _ in pairs(saved.items) do n = n + 1 end
    QI.map, QI.size, QI.saved, QI.state = saved.items, n, true, "ready"
    return
  end
  if st then st.questIndex = nil end
  local ids = L.Quest.GetAllIds()
  if type(ids) ~= "table" then error("QuestieDB lists no quests") end
  QI.ids, QI.total, QI.pos, QI.work = ids, #ids, 1, {}
  QI.ticks, QI.most, QI.saved, QI.state = 0, 0, false, "building"
  After(0, QI.Step)
end

local function StartIndex()
  local ok, err = pcall(QI.Start)
  if not ok then QI.Fail(err) end
end

-- LibQuestieDB once the index is ready, else nil (and the index is started).
local function QuestieReady()
  local s = QI.state
  if s == "idle" or s == "absent" then
    StartIndex()
    s = QI.state
  end
  if s == "ready" then return QI.lib end
  return nil
end

-- What a verdict needs from one quest, read once a session; false for a
-- quest QuestieDB does not have.
local PRE_KEYS = { "preQuestSingle", "preQuestGroup" }

local function QuestInfo(L, q)
  local c = QI.info[q]
  if c ~= nil then return c end
  local Q = L.Quest
  local name = (type(Q.Exists) ~= "function" or Q.Exists(q) == true) and Str(Q.Get(q, "name")) or nil
  if not name then
    QI.info[q] = false
    return false
  end
  local ex = {}
  local list = Q.Get(q, "exclusiveTo")
  if type(list) == "table" then
    for _, x in pairs(list) do
      x = Num(x)
      if x and x > 0 then ex[#ex + 1] = x end
    end
  end
  -- The quests it comes after (preQuestSingle: one of them, preQuestGroup:
  -- all of them; a negative id there is that quest too).
  local pre = {}
  for _, key in ipairs(PRE_KEYS) do
    list = Q.Get(q, key)
    if type(list) == "table" then
      for _, x in pairs(list) do
        x = Num(x)
        if x and x ~= 0 then pre[#pre + 1] = x < 0 and -x or x end
      end
    end
  end
  c = {
    name = name,
    races = Num(Q.Get(q, "requiredRaces")) or 0,
    classes = Num(Q.Get(q, "requiredClasses")) or 0,
    rep = (Num(Q.Get(q, "specialFlags")) or 0) % 2 == 1,   -- flag 1: repeatable
    nextQ = Num(Q.Get(q, "nextQuestInChain")),
    -- It can no longer be taken once you did one of these.
    ex = ex,
    untilQ = Num(Q.Get(q, "availableUntilCompleted")),
    pre = pre,
  }
  QI.info[q] = c
  return c
end

local function HasBit(mask, bit)
  return bit > 0 and mask % (bit * 2) >= bit
end

-- Whether two race masks share a race. Forever's masks reach past 32 bits,
-- so this is plain arithmetic, not the bit library.
local function Overlaps(a, b)
  local bit = 1
  while bit <= b do
    if HasBit(b, bit) and HasBit(a, bit) then return true end
    bit = bit * 2
  end
  return false
end

-- Your race's bit (QuestieDB's own mapping), your faction's races and your
-- class's bit; each nil when it cannot be read.
local function PlayerMasks(L)
  if QI.player then return QI.player end
  local p, E = {}, type(L.Enum) == "table" and L.Enum or {}
  if type(_G.UnitRace) == "function" and type(E.raceMaskById) == "table" then
    local ok, _, _, raceID = pcall(_G.UnitRace, "player")
    if ok and Num(raceID) then p.race = Num(E.raceMaskById[raceID]) end
  end
  if type(_G.UnitFactionGroup) == "function" and type(E.factionRaceMasks) == "table" then
    local ok, f = pcall(_G.UnitFactionGroup, "player")
    if ok and Str(f) then p.faction = Num(E.factionRaceMasks[f]) end
  end
  if type(_G.UnitClass) == "function" then
    local ok, _, _, classID = pcall(_G.UnitClass, "player")
    if ok and Num(classID) and classID >= 1 and classID <= 32 then p.class = 2 ^ (classID - 1) end
  end
  if p.race or p.faction then QI.player = p end
  return p
end

-- A quest you can take: for your race (or at least your faction) and class.
local function ForPlayer(L, info)
  local p = PlayerMasks(L)
  if info.races > 0 then
    if p.race then
      if not HasBit(info.races, p.race) then return false end
    elseif p.faction and not Overlaps(info.races, p.faction) then
      return false
    end
  end
  if info.classes > 0 and p.class and not HasBit(info.classes, p.class) then return false end
  return true
end

-- Done, or shut for good because you did a quest that excludes it (or the
-- one it is only available until).
local function Closed(info, q)
  if QuestDone(q) then return true end
  if info.untilQ and info.untilQ > 0 and QuestDone(info.untilQ) then return true end
  for _, x in ipairs(info.ex) do
    if QuestDone(x) then return true end
  end
  return false
end

-- The quest log a verdict reads (once per verdict, or the caller's).
function QX.CtxLog(ctx)
  if not ctx.log then ctx.log = ReadQuestLog() end
  return ctx.log
end

-- true: the quest is in your log; false: it is not; nil: closed quest log
-- headers hide some quests, so it cannot be told.
local function InLog(q, ctx)
  local QL = type(_G.C_QuestLog) == "table" and _G.C_QuestLog or nil
  if QL and type(QL.IsOnQuest) == "function" then
    local ok, r = pcall(QL.IsOnQuest, q)
    if ok and type(r) == "boolean" then return r end
  end
  local log = QX.CtxLog(ctx)
  if log.ids[q] then return true end
  local f = (QL and type(QL.GetLogIndexForQuestID) == "function") and QL.GetLogIndexForQuestID or _G.GetQuestLogIndexByID
  if type(f) == "function" then
    local ok, r = pcall(f, q)
    if ok and Num(r) and r > 0 then return true end
  end
  if log.complete then return false end
  return nil
end

-- Which quest a verdict names: one that asks for the item, else the one
-- after the quest that gives it, the one it starts, the one that gives it.
local RANK = { use = 1, nextq = 2, start = 3, give = 4, after = 5 }

-- The objects a quest is handed in at (key "finishedBy") or taken from
-- ("startedBy"), as a set; read the first time a verdict needs them.
local function QuestObjects(L, q, info, key)
  local c = info[key]
  if c then return c end
  c = {}
  local t = L.Quest.Get(q, key)
  local list = type(t) == "table" and t[2] or nil
  if type(list) == "table" then
    for _, o in pairs(list) do
      o = Num(o)
      if o and o > 0 then c[o] = true end
    end
  end
  info[key] = c
  return c
end

-- The quest after q when it starts at an object q is handed in at (a
-- scrying bowl an item calls up), it is for you and still open; else nil.
local function HandedOverTo(L, q, info)
  local n = info.nextQ
  if not n or n <= 0 then return nil end
  local ni = QuestInfo(L, n)
  if not ni or not ForPlayer(L, ni) or Closed(ni, n) then return nil end
  local ends = QuestObjects(L, q, info, "finishedBy")
  for o in pairs(QuestObjects(L, n, ni, "startedBy")) do
    if ends[o] then return n end
  end
  return nil
end

QX.CHAIN_MAX = 6         -- quests followed along a chain after the one that gave an item

-- The quests after g, a quest you did that gave you the item, past the ones
-- you did too (at most QX.CHAIN_MAX): "keep" and the first one in your log
-- (a quest in your log counts even when the game says it is done);
-- "hidden" when closed headers hide whether the first one you have not
-- done is; "maybe" when that is the quest right after g (for you, not
-- taken: you may need it for that one). nil when the item is done with.
-- tool: the item has a Use effect. A tool that calls up the object one quest
-- is handed in at, where the next one starts (a scrying bowl), is done with
-- the chain there: HandedOverTo keeps it until that quest is taken.
-- A next quest QuestieDB has no row for (it may not know every quest on
-- Forever) still counts by its id: in your log, done or not.
function QX.Chain(L, g, info, ctx, tool)
  local q, qi, seen = g, info, { [g] = true }
  for step = 1, QX.CHAIN_MAX do
    local n = qi.nextQ
    if not n or n <= 0 or seen[n] then return nil end
    seen[n] = true
    local ni = QuestInfo(L, n)
    if not ni then
      local s = InLog(n, ctx)
      if s == true then return "keep", n end
      if QuestDone(n) then return nil end
      if s == nil then return "hidden", n end
      if step == 1 then return "maybe", n end
      return nil
    end
    if not ForPlayer(L, ni) then return nil end
    if tool and step > 1 then
      local ends = QuestObjects(L, q, qi, "finishedBy")
      for o in pairs(QuestObjects(L, n, ni, "startedBy")) do
        if ends[o] then return nil end
      end
    end
    local s = InLog(n, ctx)
    if s == true then return "keep", n end
    if not Closed(ni, n) then
      if s == nil then return "hidden", n end
      if step == 1 then return "maybe", n end
      return nil
    end
    q, qi = n, ni
  end
  return nil
end

-- The item's name as the game has it, else QuestieDB's.
function QX.ItemName(id, L)
  local gi = ItemInfo(id)
  if gi and gi.name then return gi.name end
  return L and Str(L.Item.Get(id, "name")) or nil
end

-- The title of quest q in your log (for one QuestieDB has no row for).
function QX.LogTitle(log, q)
  for _, e in ipairs(log.list) do
    if e.q == q then return e.title end
  end
  return nil
end

-- QuestieDB's verdict on a quest item (see above): { kind = "keep",
-- "starts", "maybe", "done" or "open", quest = id, name = its name, rep =
-- true when it is kept for a repeatable quest, why = "next", "hidden" or
-- "reading" for maybe, log = how a quest in your log names it ("title",
-- "objective", "text") when that is what keeps it, given = true when a
-- quest for you gives you the item (it cannot be had again) }. nil when
-- there is none: QuestieDB is not loaded or not usable, the index is still
-- being made, or no quest for you uses the item (and none in your log
-- names it). log: ReadQuestLog() when the caller has it. startQ: the quest
-- the game says the item starts.
local function QuestieVerdict(id, log, startQ)
  local L = QuestieReady()
  if not L or not Num(id) then return nil end
  local usable = ItemUsable(id)
  local R, order = {}, {}
  local function add(q, role)
    q = Num(q)
    if not q or q <= 0 then return false end
    if R[q] then return true end
    local info = QuestInfo(L, q)
    if not info or not ForPlayer(L, info) then return false end
    R[q] = { role = role, info = info }
    order[#order + 1] = q
    return true
  end
  -- The quest it starts first: QuestieDB lists a quest starter as the item
  -- that quest gives too, and it has to stay the quest it starts.
  local s1, s2 = Num(L.Item.Get(id, "startQuest")), Num(startQ)
  add(s1, "start")
  add(s2, "start")
  local entry, codes = QI.map[id], {}
  if type(entry) == "string" then
    for c in sgmatch(entry, "%d+") do codes[#codes + 1] = tonumber(c) end
  elseif type(entry) == "table" then
    codes = entry
  else
    codes[1] = entry
  end
  -- givers: the quests that hand it to you. from: the quest after a giver
  -- (the one the item is for) -> that giver.
  local givers, from = {}, {}
  for i = 1, #codes do
    local c = Num(codes[i])
    if c and c > 0 then
      local q, role = floor(c / 4), c % 4
      if role == ROLE_USE then
        add(q, "use")
      elseif role == ROLE_GIVE and q ~= s1 and q ~= s2 then
        givers[#givers + 1] = q
        -- The quest after the one that gives it is the one a tool you use (a
        -- phial that calls someone) is for. A note you read may be for any
        -- quest after that one: see Chain below.
        if add(q, "give") and usable ~= false then
          local n = R[q].info.nextQ
          if add(n, "nextq") and not from[n] then from[n] = q end
        end
      end
    end
  end
  if #order == 0 then
    -- No quest for you uses it, as far as QuestieDB knows: a quest in your
    -- log that names it still keeps it.
    local hit, how = QX.Hits(QX.ItemName(id, L), log or ReadQuestLog())
    if hit then return { kind = "keep", quest = hit.q, name = hit.title, log = how } end
    return nil
  end
  local given = false
  for _, g in ipairs(givers) do
    if R[g] then given = true end
  end
  local function pick(want)
    local bq, br
    for _, q in ipairs(order) do
      if want(q) then
        local r = RANK[R[q].role] or 9
        if not br or r < br then bq, br = q, r end
      end
    end
    return bq
  end
  local function verdict(kind, q, rep, why)
    local info = R[q] and R[q].info or QuestInfo(L, q)
    return { kind = kind, quest = q, name = info and info.name or nil, rep = rep, why = why, given = given }
  end
  local ctx, inLog = { log = log }, {}
  for _, q in ipairs(order) do
    if InLog(q, ctx) == true then inLog[q] = true end
  end
  local q = pick(function(x) return inLog[x] end)
  if q then return verdict("keep", q) end
  -- Before anything says you are done with it or have not started it: a
  -- quest in your log that names it keeps it (QuestieDB may not know that
  -- quest); while closed headers hide quests, or a quest's text is not in
  -- yet, "done" and "not started" are not sure.
  local function final(v)
    local lg = QX.CtxLog(ctx)
    local hit, how = QX.Hits(QX.ItemName(id, L), lg)
    if hit then return { kind = "keep", quest = hit.q, name = hit.title, log = how, given = given } end
    if v.kind == "done" or v.kind == "open" then
      if not lg.complete then
        v.kind, v.why = "maybe", "hidden"
      elseif lg.pending then
        v.kind, v.why = "maybe", "reading"
      end
    end
    return v
  end
  -- An item a quest you did gave you: kept while a quest after that one is
  -- in your log, maybe needed while the one right after it is not done (a
  -- tool is kept for that one: see "got" below).
  local after
  for _, g in ipairs(givers) do
    if R[g] and QuestDone(g) then
      local kind, n = QX.Chain(L, g, R[g].info, ctx, usable ~= false)
      if kind == "keep" then
        local v = verdict("keep", n)
        v.name = v.name or QX.LogTitle(QX.CtxLog(ctx), n)
        return v
      end
      if kind and not after then after = verdict("maybe", n, nil, kind == "hidden" and "hidden" or "next") end
    end
  end
  local closed = {}
  for _, x in ipairs(order) do closed[x] = Closed(R[x].info, x) end
  q = pick(function(x) return R[x].role == "start" and not closed[x] end)
  if q then return verdict("starts", q) end
  local function any() return true end
  -- Turn-ins you can repeat are never done.
  q = pick(function(x) return R[x].info.rep end)
  if q or REPEATABLE[id] then return verdict("keep", q or pick(any), true) end
  q = pick(function(x) return not closed[x] end)
  if not q then
    -- All done, unless a quest it was given for is handed in at an object
    -- where the next quest starts: until you take that one, the item is
    -- the way back to it.
    local wait
    for _, x in ipairs(order) do
      local n = from[x] and QuestDone(x) and HandedOverTo(L, x, R[x].info)
      if n then
        local s = InLog(n, ctx)
        if s == false then
          add(n, "after")
          return verdict("keep", n)
        end
        if s == nil and not wait then wait = verdict("maybe", n, nil, "hidden") end
      end
    end
    return final(after or wait or verdict("done", pick(any)))
  end
  -- Halfway along a chain, where a dropped item is gone for good: a quest
  -- you finished gave it to you or rewarded you with it, or a quest that
  -- needs it comes after one in your log.
  local got = false
  for _, g in ipairs(givers) do
    if QuestDone(g) then got = true break end
  end
  if not got then
    local rw = L.Item.Get(id, "questRewards")
    if type(rw) == "table" then
      for _, r in pairs(rw) do
        if QuestDone(r) then got = true break end
      end
    end
  end
  local mid = pick(function(x)
    if closed[x] then return false end
    if got then return true end
    for _, p in ipairs(R[x].info.pre) do
      if InLog(p, ctx) == true then return true end
    end
    return false
  end)
  if mid then return verdict("keep", mid) end
  -- Not started; while closed headers hide whether it is, not sure.
  return final(verdict("open", q))
end

-- QuestieDB's verdict on one item as a tooltip sees it (the tests read it).
function ns.QuestItemVerdict(id)
  if QI.state == "off" then return nil end
  local ok, v = pcall(QuestieVerdict, id, nil, nil)
  if not ok then
    QI.Fail(v)
    return nil
  end
  return v
end

-- The verdict for the bag scan; nil (the quest log's words decide) when
-- there is none or reading it fails.
local function ScanVerdict(id, log, qi)
  if QI.state == "off" then return nil end
  local ok, v = pcall(QuestieVerdict, id, log, qi and Num(qi.questID) or nil)
  if not ok then
    QI.Fail(v)
    return nil
  end
  return v
end

-- The tooltip line for a verdict, and its colour.
local function QuestLineText(v)
  local n = v.name
  if v.kind == "done" then return "Can drop: done with " .. (n or "its quests"), "pos" end
  if v.kind == "open" then return "Only for " .. (n or "a quest") .. ", not in your quest log", "muted" end
  if v.kind == "maybe" then
    if v.why == "hidden" then return "Not sure: some quest log headers are closed", "muted" end
    if v.why == "reading" then return "Not sure yet: still reading your quest log", "muted" end
    return "May be needed for " .. (n or "the next quest"), "muted"
  end
  if v.kind == "starts" then return "Starts " .. (n or "a quest"), "buy" end
  if v.rep then return "Keep: for " .. (n and (n .. " (repeatable)") or "a repeatable quest"), "buy" end
  return "Keep: needed for " .. (n or "a quest in your log"), "buy"
end

local function IsQuestItem(id)
  local gi = ItemInfo(id)
  if gi then return gi.classID == CLASS_QUEST or gi.bind == BIND_QUEST end
  return (InstantInfo(id)) == CLASS_QUEST
end

-- Whether QuestieDB's index ties the item to a quest at all (a journal or a
-- note a quest gives you is often not marked as a quest item).
local function QuestieLinked(id)
  return QI.state == "ready" and type(QI.map) == "table" and QI.map[id] ~= nil
end

-- Core.lua's tooltip handler calls this for an item it has no prices for:
-- a quest item QuestieDB knows gets one line, green when you can drop it,
-- gold when you need it, grey when its quest is not in your log or it may
-- still be needed. Nothing while the index is being made or when QuestieDB
-- has no verdict. true when the line was added. The item is remembered for
-- /fah questcheck with no name.
function ns.QuestItemLine(tt, id)
  id = Num(id)
  if not id or rowTip or type(tt) ~= "table" or type(tt.AddLine) ~= "function" then return false end
  local st = S()
  if st and st.tooltip == false then return false end
  if QI.state == "off" then return false end
  local ok, v = pcall(function()
    -- Any item's tooltip may start the index (a journal a quest gave you is
    -- not marked as a quest item, so it would otherwise wait for one that is).
    QuestieReady()
    if not IsQuestItem(id) and not QuestieLinked(id) then return nil end
    QI.lastItem = id
    return QuestieVerdict(id, nil, nil)
  end)
  if not ok then
    QI.Fail(v)
    return false
  end
  if not v then return false end
  local text, key = QuestLineText(v)
  local c = Col(key)
  return (pcall(tt.AddLine, tt, text, c[1], c[2], c[3]))
end

-- For /gl bagcheck: what QuestieDB gives the bag cleanup, or nil without it.
local function QuestIndexText()
  QuestieReady()
  local s = QI.state
  if s == "ready" then
    return sformat("quests from QuestieDB (%d items%s)", QI.size or 0, QI.saved and ", saved" or "")
  elseif s == "building" then
    return sformat("reading QuestieDB (%d of %d quests)", max(0, QI.pos - 1), QI.total)
  elseif s == "off" then
    return "QuestieDB not used (" .. tostring(QI.err):sub(1, 60) .. ")"
  end
  return nil
end

---------------------------------------------------------------------------
-- What a stack is worth
---------------------------------------------------------------------------
local function LiveFor(id)
  if type(ns.LiveCheapest) ~= "function" then return nil end
  local ok, p, own = pcall(ns.LiveCheapest, id)
  if ok and Num(p) and p > 0 then return p, own == true end
  return nil
end

-- What one unit sells for at auction right now (before the cut), and whether
-- that price is a rough one. nil when it does not sell there.
local function AuctionUnit(pid, cache)
  local c = cache[pid]
  if c then return c.unit, c.unsure end
  local unit, unsure
  if type(ns.SellPrices) == "function" then
    local live, own = LiveFor(pid)
    local ok, sp = pcall(ns.SellPrices, pid, "1D", live, own)
    if ok and type(sp) == "table" then
      -- Listed now: the same price per item the tooltip's verdict judges
      -- (List At, never over the AH value). Totals can still differ by a few
      -- copper: the panel takes the 5% cut per item, the tooltip's stack line
      -- on the whole stack.
      local it = type(ns.GetItem) == "function" and ns.GetItem(pid) or nil
      local u = it and (Num(it.market) or 0) > 0 and type(ns.SellVerdictPrice) == "function" and ns.SellVerdictPrice(it, sp) or nil
      if u or (Num(sp.now) and sp.now > 0) then
        unit = u or sp.now
        if type(ns.PriceDoubt) == "function" then
          local okD, d = pcall(ns.PriceDoubt, pid)
          unsure = okD and d and true or false
        end
      elseif sp.nowWhy == "none" and type(ns.GetItem) == "function" then
        -- None listed right now: what it went for lately, marked rough.
        local it = ns.GetItem(pid)
        local m = it and Num(it.market) or 0
        if m > 0 then unit, unsure = m, true end
      end
      -- "unreliable": a few listed far above what a vendor pays. That is one
      -- player's asking price, not money you would get.
    end
  end
  cache[pid] = { unit = unit, unsure = unsure }
  return unit, unsure
end

local function PriceID(id, link)
  if type(ns.PriceIDForLink) == "function" then
    local ok, pid = pcall(ns.PriceIDForLink, id, link)
    if ok and Num(pid) then return pid end
  end
  return id
end

local function DataVendor(id)
  if type(ns.GetItem) ~= "function" then return 0 end
  local it = ns.GetItem(id)
  return it and Num(it.vendor) or 0
end

local function HasAuctionData(id)
  if type(ns.GetItem) ~= "function" then return false end
  local it = ns.GetItem(id)
  return it ~= nil and ((Num(it.market) or 0) > 0 or (Num(it.cheapest) or 0) > 0)
end

---------------------------------------------------------------------------
-- The scan
---------------------------------------------------------------------------
-- ns.BagScan() -> {
--   free, slots   free and total slots in your normal bags
--   drop          stacks worth the least first: { bag, slot, id, link, name,
--                 count, quality, icon, value, vendor (each), auction (each,
--                 after the cut, or nil), source "vendor"/"auction", unsure, junk,
--                 questItem (a quest item the auction house trades) }
--   quest         old quest items: the same fields plus why and sure. From
--                 QuestieDB (qdb true, quest = the quest's name): why "done"
--                 (its quests are done), "open" (not in your quest log) or
--                 "maybe" (not sure: maybe "next" when the quest after its
--                 own is not done, "hidden" when closed headers hide quests,
--                 "reading" while a quest's text is not in yet).
--                 From the quest log's words: "done" (it starts a quest you
--                 finished) or "none" (no quest in your log names it)
--   merge         { id, name, icon, quality, stacks, frees, from, to } (from/to
--                 are slot entries; nil when no two stacks fit in one)
--   junk          grey items a vendor buys (what "Sell junk" sells)
--   pending       some items were not loaded yet (the scan reruns when they are)
--   questPartial  collapsed quest log headers hid some quests
--   questBuilding QuestieDB's quests are still being read (the list is from
--                 the quest log until they are; soulbound items wait)
--   questReading  a quest's own text is not in yet (asked again shortly)
-- }
local WHY_ORDER = { done = 1, open = 2, none = 3, maybe = 4 }

-- A QuestieDB verdict the cleanup lists, and the row it makes.
QX.LISTED = { done = true, open = true, maybe = true }
function QX.Entry(e, v)
  e.why, e.sure, e.quest, e.qdb, e.maybe = v.kind, v.kind ~= "maybe", v.name, true, v.why
end

function ns.BagScan()
  local keep = KeepList()
  local level = PlayerLevel()
  local log = ReadQuestLog()
  QuestieReady()                       -- the index starts (a saved one is ready at once)
  local cut = AHCut()
  local cache = {}
  local out = { free = 0, slots = 0, drop = {}, quest = {}, merge = {}, junk = {},
                pending = false, questPartial = not log.complete }
  local stacks, order = {}, {}
  for _, bag in ipairs(BagIDs()) do
    local n = NumSlots(bag)
    if n > 0 then
      local free, fam = FreeAndFamily(bag)
      local normal = (fam or 0) == 0
      local used = 0
      for slot = 1, n do
        local info = SlotInfo(bag, slot)
        local id = info and Num(info.itemID)
        if id then
          used = used + 1
          local link = Str(info.hyperlink)
          local gi = ItemInfo(link) or ItemInfo(id)
          local classID, subclassID, icon
          if gi then
            classID, subclassID, icon = gi.classID, gi.subclassID, gi.icon
          else
            out.pending = true
            RequestLoad(id)
            classID, subclassID, icon = InstantInfo(id)
          end
          local count = Num(info.stackCount) or 1
          if count < 1 then count = 1 end
          local e = {
            bag = bag, slot = slot, id = id, link = link, count = count,
            name = (gi and gi.name) or (link and smatch(link, "%[(.-)%]")) or nil,
            quality = Num(info.quality) or (gi and gi.quality) or 1,
            icon = info.iconFileID or icon,
            classID = classID,
          }
          local maxStack = gi and gi.maxStack or 1
          local locked = info.isLocked == true
          -- Partial stacks that could merge.
          if not locked and maxStack > 1 and count < maxStack then
            local g = stacks[id]
            if not g then
              g = { max = maxStack, list = {} }
              stacks[id] = g
              order[#order + 1] = id
            end
            g.list[#g.list + 1] = e
          end
          local bind = gi and gi.bind or nil
          local bound = info.isBound == true
          local isQuest = classID == CLASS_QUEST or bind == BIND_QUEST
          local qi = SlotQuest(bag, slot)
          if qi and (Num(qi.questID) or 0) > 0 then isQuest = true end
          -- Even when the auction house trades it, a quest item is never
          -- dropped in one click: the game's own delete check warns when a
          -- quest needs it.
          e.questItem = isQuest
          -- A quest item the auction house trades (a turn-in people buy)
          -- is valued like any other item.
          if isQuest and not bound and HasAuctionData(PriceID(id, link)) then isQuest = false end
          if locked or keep[id] or PROTECT[id] then
            -- moving, kept or a tool: leave it alone
          elseif isQuest then
            -- Questie's quests for it first; the quest log's words when it has none.
            local v = ScanVerdict(id, log, qi)
            if QI.state == "building" then out.questBuilding = true end
            if v then
              -- Listed when you can drop it, have not started its quest, or
              -- it may still be needed (check first); kept otherwise.
              if QX.LISTED[v.kind] then
                QX.Entry(e, v)
                out.quest[#out.quest + 1] = e
              end
            else
              local why, sure = QuestVerdict(e, qi, log)
              if why then
                e.why, e.sure = why, sure
                out.quest[#out.quest + 1] = e
              end
            end
          else
            local vendor = (gi and gi.vendor) or DataVendor(id)
            if info.hasNoValue == true then vendor = 0 end
            local aEach, aTotal, unsure = nil, 0, nil
            if not bound and bind ~= BIND_PICKUP then
              local unit
              unit, unsure = AuctionUnit(PriceID(id, link), cache)
              -- the stack after the 5% cut, as the tooltip's stack line works it out
              if unit then aEach, aTotal = floor(unit * cut), floor(unit * count * 95 / 100) end
            end
            local vTotal = vendor * count
            e.vendor, e.auction, e.unsure = vendor, aEach, unsure and aTotal > vTotal or false
            e.value = max(vTotal, aTotal)
            e.source = (aTotal > vTotal) and "auction" or "vendor"
            -- A quest gave it or needs it (a journal, a note): Questie says
            -- whether you are done with it. Worth nothing, it is listed as a
            -- quest item is; one a quest needs (or may) is never offered to
            -- drop or sell, whatever it is worth. Not sure (maybe) holds one
            -- a quest gave you, which cannot be had again; one a quest only
            -- asks for (cloth, ore) can, so it stays on the list as before.
            local held, v = false, nil
            if QuestieLinked(id) then
              v = ScanVerdict(id, log, qi)
              if v then
                local kind = v.kind
                held = kind == "starts" or (kind == "keep" and not v.rep) or (kind == "maybe" and v.given == true)
                if held then e.questItem = true end
                if (kind == "maybe" and held) or (e.value <= 0 and QX.LISTED[kind]) then
                  e.questItem = true
                  QX.Entry(e, v)
                  out.quest[#out.quest + 1] = e
                end
              end
            end
            if not v then
              -- No verdict from QuestieDB (it does not know the item, or its
              -- quests are still being read): a quest in your log whose
              -- title or objective names it keeps it; while they are being
              -- read, a soulbound item (a note a quest gave you) waits.
              if QX.Hits(e.name, log, false, true) then
                held = true
              elseif QI.state == "building" and (bound or bind == BIND_PICKUP) then
                held, out.questBuilding = true, true
              end
            end
            e.junk = e.quality == 0 and vendor > 0 and not held
            if e.junk then out.junk[#out.junk + 1] = e end
            local skip = e.value <= 0 or held
              or classID == CLASS_PROJECTILE or classID == CLASS_QUIVER or classID == CLASS_KEY
              or classID == CLASS_REAGENT
              or (classID == CLASS_WEAPON and subclassID == SUB_FISHING_POLE)
              or (e.name ~= nil and log.needs[slower(e.name)] == true)
            if not skip and classID == CLASS_CONSUMABLE and not e.junk then
              -- Food, potions and bandages you still use stay; ones far
              -- under your level go on the list.
              local req = max((gi and gi.minLevel) or 0, 1)
              skip = not (level and level - req >= OUTGROWN)
            end
            if not skip then out.drop[#out.drop + 1] = e end
          end
        end
      end
      if normal then
        out.slots = out.slots + n
        out.free = out.free + (free or (n - used))
      end
    end
  end
  sort(out.drop, function(a, b)
    if a.value ~= b.value then return a.value < b.value end
    if a.junk ~= b.junk then return a.junk end
    local an, bn = a.name or "", b.name or ""
    if an ~= bn then return an < bn end
    if a.bag ~= b.bag then return a.bag < b.bag end
    return a.slot < b.slot
  end)
  sort(out.quest, function(a, b)
    local ra, rb = WHY_ORDER[a.why] or 9, WHY_ORDER[b.why] or 9
    if ra ~= rb then return ra < rb end
    if (a.sure and 1 or 0) ~= (b.sure and 1 or 0) then return a.sure end
    return (a.name or "") < (b.name or "")
  end)
  for _, id in ipairs(order) do
    local g = stacks[id]
    local list = g.list
    if #list >= 2 then
      local total = 0
      for _, e in ipairs(list) do total = total + e.count end
      local frees = #list - ceil(total / g.max)
      if frees > 0 then
        sort(list, function(a, b) if a.count ~= b.count then return a.count < b.count end return a.slot < b.slot end)
        local from, to = list[1], nil
        for i = #list, 2, -1 do
          if from.count + list[i].count <= g.max then to = list[i] break end
        end
        out.merge[#out.merge + 1] = { id = id, name = from.name, icon = from.icon, quality = from.quality,
          stacks = #list, frees = frees, from = from, to = to }
      end
    end
  end
  out.questReading = log.pending == true
  return out
end

---------------------------------------------------------------------------
-- Acting on a row: pick up, sell, merge, drop (always from a click)
---------------------------------------------------------------------------
local function CursorHasItem()
  local f = _G.CursorHasItem
  if type(f) == "function" then
    local ok, r = pcall(f)
    if ok then return r and true or false end
  end
  f = _G.GetCursorInfo
  if type(f) == "function" then
    local ok, kind = pcall(f)
    return ok and kind ~= nil
  end
  return false
end

local function ClearCursor()
  if type(_G.ClearCursor) == "function" then pcall(_G.ClearCursor) end
end

local function AtMerchant(bp)
  local mf = _G.MerchantFrame
  if type(mf) == "table" and type(mf.IsShown) == "function" then
    local ok, shown = pcall(mf.IsShown, mf)
    if ok then return shown and true or false end
  end
  return bp.merchant == true
end

-- The slot still holds what the list says.
local function StillThere(e)
  local info = SlotInfo(e.bag, e.slot)
  return info ~= nil and Num(info.itemID) == e.id and (Num(info.stackCount) or 1) == e.count and info.isLocked ~= true
end

local function ItemText(e)
  local n = e.link or e.name or ("Item " .. tostring(e.id))
  if e.count > 1 then n = n .. " x" .. e.count end
  return n
end

-- Stacks the drop box may destroy: below Rare and not a quest item, the same
-- ones the game itself confirms with a plain Yes/No.
local function CanDrop(e)
  return type(e) == "table" and (Num(e.quality) or 1) <= DROP_MAX_QUALITY and e.questItem ~= true
end

local function Now()
  local f = _G.GetTime
  if type(f) ~= "function" then return nil end
  local ok, t = pcall(f)
  return ok and Num(t) or nil
end

-- The cursor holds this item.
local function CursorHolds(e)
  local f = _G.GetCursorInfo
  if type(f) ~= "function" then return CursorHasItem() end
  local ok, kind, id = pcall(f)
  return ok and kind == "item" and Num(id) == e.id
end

-- The item part of a link ("item:2589:0:..."), without the name and colour,
-- which can fill in later while the item stays the same.
local function LinkKey(link)
  link = Str(link)
  if not link then return nil end
  return smatch(link, "|H(item:[^|]+)|h") or link
end

-- The slot still holds exactly the stack the drop box names (same item, count
-- and link, so a random-suffix swap counts as a change) and nothing moves it.
local function SameStack(t)
  local info = SlotInfo(t.bag, t.slot)
  if not info or info.isLocked == true or Num(info.itemID) ~= t.id
    or (Num(info.stackCount) or 1) ~= t.count then return false end
  local a, b = LinkKey(t.link), LinkKey(info.hyperlink)
  return not (a and b and a ~= b)
end

-- The stack read again right now, and still on the list (not kept, not a
-- tool, no quest asks for it, not food near your level). nil and why not
-- ("changed" or "unlisted") otherwise.
local function LiveDrop(t)
  if not SameStack(t) then return nil, "changed" end
  for _, e in ipairs(ns.BagScan().drop) do
    if e.bag == t.bag and e.slot == t.slot then
      if e.id == t.id and e.count == t.count and CanDrop(e) then return e end
      return nil, "unlisted"
    end
  end
  return nil, "unlisted"
end

local function WorthText(e)
  if e.source == "auction" then
    return "Worth " .. (e.unsure and "about " or "") .. ChatMoney(e.value) .. " at auction now."
  end
  return "Worth " .. ChatMoney(e.value) .. " at a vendor."
end

local BP = { rows = {}, built = false, merchant = false, hinted = false,
  -- For /gl bagcheck: updates asked for and run, how the last one ended, and
  -- which hooks took.
  stats = { asked = 0, ran = 0, result = "never ran", toggles = {}, elv = {} } }
ns.BagPanel = BP

local function Pickup(e)
  local f = ContainerFn("PickupContainerItem")
  if not f then return false end
  if CursorHasItem() then ClearCursor() end
  local ok = pcall(f, e.bag, e.slot)
  if ok and not BP.hinted then
    BP.hinted = true
    Print("picked up " .. ItemText(e) .. ". Click the ground to destroy it (the game asks first), or click a bag slot to put it back.")
  end
  return ok
end

local function Sell(e)
  local f = ContainerFn("UseContainerItem")
  if not f or not AtMerchant(BP) then return false end
  if CursorHasItem() then ClearCursor() end
  return (pcall(f, e.bag, e.slot))
end

local function Merge(m)
  local from, to = m.from, m.to
  if not from or not to then
    Print("stack " .. (m.name or "these") .. " by hand: no two of its stacks fit in one slot.")
    return false
  end
  if not StillThere(from) or not StillThere(to) then return false end
  local f = ContainerFn("PickupContainerItem")
  if not f then return false end
  if CursorHasItem() then ClearCursor() end
  pcall(f, from.bag, from.slot)
  pcall(f, to.bag, to.slot)
  if CursorHasItem() then ClearCursor() end
  return true
end

function ns.SellJunk()
  if not AtMerchant(BP) then
    Print("open a vendor first: junk sells there.")
    return 0
  end
  local scan = ns.BagScan()
  local sold, money = 0, 0
  for _, e in ipairs(scan.junk) do
    if sold >= SELL_BATCH then break end
    if StillThere(e) and Sell(e) then
      sold = sold + 1
      money = money + e.vendor * e.count
    end
  end
  if sold > 0 then
    local left = #scan.junk - sold
    Print(sformat("sold %d junk item%s for %s.%s", sold, sold == 1 and "" or "s", ChatMoney(money),
      left > 0 and " Click Sell junk again for the rest." or ""))
  end
  return sold
end

function ns.ToggleBagKeep(id)
  id = Num(tonumber(id))
  if not id then return nil end
  local keep = KeepList()
  if keep[id] then keep[id] = nil else keep[id] = true end
  return keep[id] == true
end

---------------------------------------------------------------------------
-- The bag frames: the game's own, and the bag window of bag addons we know.
-- Other bag addons (they hide the game's bags) get /gl bags in chat.
---------------------------------------------------------------------------
-- ElvUI's bags: one window, ElvUI_ContainerFrame. Its slots sit in
-- frame.Bags[bagID][slotID].
local ADDON_BAG_FRAMES = { "ElvUI_ContainerFrame" }

local function IsAddonWindow(f)
  for _, name in ipairs(ADDON_BAG_FRAMES) do
    if f ~= nil and _G[name] == f then return true end
  end
  return false
end

local function BagFrames()
  local out = {}
  for _, name in ipairs(ADDON_BAG_FRAMES) do
    local f = _G[name]
    if type(f) == "table" and type(f.IsShown) == "function" then out[#out + 1] = f end
  end
  local cb = _G.ContainerFrameCombinedBags
  if type(cb) == "table" and type(cb.IsShown) == "function" then out[#out + 1] = cb end
  local n = Num(_G.NUM_CONTAINER_FRAMES) or 13
  for i = 1, n do
    local f = _G["ContainerFrame" .. i]
    if type(f) == "table" and type(f.IsShown) == "function" then out[#out + 1] = f end
  end
  return out
end

-- Open bag windows. A bag addon's window above counts when it is shown, even
-- when its position cannot be read (none yet, or a secret value). The game's
-- own bags count only with a position: bag addons park them shown with none.
local function OpenBagFrames()
  local out = {}
  for _, f in ipairs(BagFrames()) do
    local ok, vis = pcall(f.IsVisible, f)
    if ok and vis then
      if IsAddonWindow(f) then
        out[#out + 1] = f
      else
        local okL, l = pcall(f.GetLeft, f)
        if okL and Num(l) then out[#out + 1] = f end
      end
    end
  end
  return out
end

-- The item button of a bag slot in the open bags, when we can find it.
local function SlotButton(bag, slot)
  for _, f in ipairs(OpenBagFrames()) do
    local bags = rawget(f, "Bags")
    if type(bags) == "table" then
      -- ElvUI: frame.Bags[bagID][slotID]
      local b = type(bags[bag]) == "table" and bags[bag][slot] or nil
      if type(b) == "table" and type(b.IsVisible) == "function" and b:IsVisible() then return b end
    elseif type(f.EnumerateValidItems) == "function" then
      local ok, iter, st, init = pcall(f.EnumerateValidItems, f)
      if ok and type(iter) == "function" then
        for _, b in iter, st, init do
          if type(b) == "table" and type(b.GetBagID) == "function" then
            local okB, bid = pcall(b.GetBagID, b)
            if okB and bid == bag and b:GetID() == slot then return b end
          end
        end
      end
    else
      local okI, fid = pcall(f.GetID, f)
      local name = type(f.GetName) == "function" and f:GetName() or nil
      if okI and fid == bag and type(name) == "string" then
        for k = 1, 40 do
          local b = _G[name .. "Item" .. k]
          if type(b) ~= "table" then break end
          if b:GetID() == slot and b:IsShown() then return b end
        end
      end
    end
  end
  return nil
end

---------------------------------------------------------------------------
-- The panel
---------------------------------------------------------------------------
local function NewTex(parent, layer, sub)
  return parent:CreateTexture(nil, layer, nil, sub)
end

local function NewFS(parent, font)
  local fs = parent:CreateFontString(nil, "OVERLAY", font or "GameFontHighlightSmall")
  fs:SetJustifyH("LEFT")
  if fs.SetWordWrap then fs:SetWordWrap(false) end
  return fs
end

local function SetFSColor(fs, key)
  local c = Col(key)
  fs:SetTextColor(c[1], c[2], c[3], 1)
end

local function HideTip(self)
  local tt = _G.GameTooltip
  if tt and tt.GetOwner and tt:GetOwner() == self then tt:Hide() end
end

local function SimpleTip(owner, title, body)
  owner:SetScript("OnEnter", function(self)
    local tt = _G.GameTooltip
    if not tt then return end
    tt:SetOwner(self, "ANCHOR_LEFT")
    tt:AddLine(type(title) == "function" and title() or title, 1, 1, 1)
    local b = type(body) == "function" and body() or body
    if b then tt:AddLine(b, nil, nil, nil, true) end
    tt:Show()
  end)
  owner:SetScript("OnLeave", HideTip)
end

local function TextButton(parent, label, w, h, name)
  local b = CreateFrame("Button", name, parent)
  b:SetSize(w, h)
  b.bg = NewTex(b, "BACKGROUND", 1)
  b.bg:SetAllPoints(b)
  local p = Col("panel")
  b.bg:SetColorTexture(p[1] + 0.05, p[2] + 0.05, p[3] + 0.06, 1)
  local hl = b:CreateTexture(nil, "HIGHLIGHT")
  hl:SetAllPoints(b)
  hl:SetColorTexture(1, 1, 1, 0.08)
  b.text = NewFS(b, "GameFontHighlightSmall")
  b.text:SetJustifyH("CENTER")
  b.text:SetPoint("CENTER", b, "CENTER", 0, 0)
  b.text:SetText(label)
  return b
end

local function QualityRGB(q)
  local CI = _G.C_Item
  if Num(q) and type(CI) == "table" and type(CI.GetItemQualityColor) == "function" then
    local ok, r, g, b = pcall(CI.GetItemQualityColor, q)
    if ok and Num(r) and Num(g) and Num(b) then return r, g, b end
  end
  local qc = Num(q) and type(_G.ITEM_QUALITY_COLORS) == "table" and _G.ITEM_QUALITY_COLORS[q]
  if type(qc) == "table" and Num(qc.r) then return qc.r, qc.g, qc.b end
  local t = Col("text")
  return t[1], t[2], t[3]
end

local function Backdrop(F)
  local bg = Col("bg")
  if F.SetBackdrop then
    F:SetBackdrop({ bgFile = WHITE, edgeFile = WHITE, edgeSize = 1 })
    F:SetBackdropColor(bg[1], bg[2], bg[3], bg[4] or 0.96)
    F:SetBackdropBorderColor(0.20, 0.20, 0.24, 1)
  else
    local t = NewTex(F, "BACKGROUND", -8)
    t:SetAllPoints(F)
    t:SetColorTexture(bg[1], bg[2], bg[3], bg[4] or 0.96)
  end
end

local QUEST_TEXT = {
  done = "It starts a quest you already finished.",
  none = "No quest in your log asks for it.",
}

local function RowTooltip(row)
  local e, kind = row.entry, row.kind
  local tt = _G.GameTooltip
  if not e or not tt then return end
  tt:SetOwner(row, "ANCHOR_LEFT")
  local slot = kind == "merge" and e.from or e
  local shown = false
  if slot and type(tt.SetBagItem) == "function" then
    -- The row says the quest itself, so the item tooltip leaves its quest line out,
    -- and the enchant lines (Enchants.lua) too: the row is about dropping or selling it.
    rowTip = true
    ns.tipQuiet = true
    shown = pcall(tt.SetBagItem, tt, slot.bag, slot.slot)
    ns.tipQuiet = false
    rowTip = false
  end
  if not shown then tt:AddLine(e.name or ("Item " .. tostring(e.id)), QualityRGB(e.quality)) end
  tt:AddLine(" ")
  local mu = Col("muted")
  if kind == "merge" then
    tt:AddLine(Paint("Merge stacks", "buy"))
    tt:AddLine(sformat("%d part stacks: merging frees %d slot%s and costs nothing.", e.stacks, e.frees,
      e.frees == 1 and "" or "s"), 0.8, 0.8, 0.8, true)
    if e.to then
      tt:AddLine("Click: merge two of them", mu[1], mu[2], mu[3])
    else
      tt:AddLine("No two of them fit in one slot: drag them together by hand.", mu[1], mu[2], mu[3], true)
    end
  elseif kind == "quest" then
    QI.lastItem = e.id
    tt:AddLine(Paint("Old quest item", "buy"))
    if e.qdb and e.why == "done" then
      tt:AddLine("Can drop: done with " .. (e.quest or "its quests") .. ".", 0.8, 0.8, 0.8, true)
    elseif e.qdb and e.why == "maybe" and e.maybe == "hidden" then
      tt:AddLine("Not sure: some quest log headers are closed. Open them so it can be checked.", 0.8, 0.8, 0.8, true)
    elseif e.qdb and e.why == "maybe" and e.maybe == "reading" then
      tt:AddLine("Not sure yet: still reading your quest log. Check again in a moment.", 0.8, 0.8, 0.8, true)
    elseif e.qdb and e.why == "maybe" then
      tt:AddLine("May be needed for " .. (e.quest or "the next quest") .. ". Keep it until that quest is done.",
        0.8, 0.8, 0.8, true)
    elseif e.qdb then
      tt:AddLine("Only for " .. (e.quest or "a quest") .. ", not in your quest log.", 0.8, 0.8, 0.8, true)
    elseif e.sure then
      tt:AddLine(QUEST_TEXT[e.why] or "It may be left over.", 0.8, 0.8, 0.8, true)
    else
      -- Not sure: no quest names it, but that is no proof nothing needs it.
      tt:AddLine("Not sure: no quest in your log names it, but some quests (deliver this, talk to someone) need "
        .. "items they do not list. Check your quest log before you drop it.", 0.8, 0.8, 0.8, true)
    end
    if e.why == "maybe" then
      tt:AddLine("Click: pick it up", mu[1], mu[2], mu[3])
    else
      tt:AddLine("Click: pick it up, then click the ground to destroy it (the game asks first)", mu[1], mu[2], mu[3], true)
    end
    tt:AddLine("Right-click: always keep it", mu[1], mu[2], mu[3])
  else
    tt:AddLine(Paint("Worth the least in your bags", "buy"))
    local where = e.source == "auction" and "at auction now, after the 5% cut" or "at a vendor"
    tt:AddDoubleLine("This stack " .. where, ChatMoney(e.value), 0.8, 0.8, 0.8, 1, 1, 1)
    if e.source == "auction" and e.vendor > 0 then
      tt:AddDoubleLine("A vendor pays", ChatMoney(e.vendor * e.count), 0.8, 0.8, 0.8, 1, 1, 1)
    end
    if e.unsure then
      tt:AddLine("Rough auction price: few listings, or none right now.", 0.6, 0.6, 0.6, true)
    end
    if AtMerchant(BP) and e.vendor > 0 then
      tt:AddLine("Click: sell it to this vendor", mu[1], mu[2], mu[3])
    elseif CanDrop(e) then
      tt:AddLine("Click: drop it (asks first)", mu[1], mu[2], mu[3])
    else
      tt:AddLine("Click: pick it up, then click the ground to destroy it (the game asks first)", mu[1], mu[2], mu[3], true)
    end
    tt:AddLine("Right-click: always keep it", mu[1], mu[2], mu[3])
  end
  tt:Show()
  local b = SlotButton(slot.bag, slot.slot)
  if b then BP:Glow(b) end
end

local function RowLeave(row)
  HideTip(row)
  -- While the drop box asks, its slot stays outlined.
  if not BP.confirm then BP:Glow(nil) end
end

local function OpenMenu(row)
  local e = row.entry
  if not e or row.kind == "merge" then return end
  local MU = _G.MenuUtil
  local name = e.name or ("Item " .. tostring(e.id))
  if type(MU) == "table" and type(MU.CreateContextMenu) == "function" then
    pcall(MU.CreateContextMenu, row, function(_, root)
      if type(root.CreateTitle) == "function" then root:CreateTitle(name) end
      root:CreateButton("Always keep (never suggest it)", function()
        ns.ToggleBagKeep(e.id)
        Print("will not suggest " .. name .. " again. /gl keep " .. name .. " undoes it.")
        BP:Refresh()
      end)
      if row.kind == "drop" and CanDrop(e) then
        root:CreateButton("Drop it (asks first)", function() if StillThere(e) then BP:AskDrop(e) end end)
      end
      root:CreateButton("Pick it up", function() if StillThere(e) then Pickup(e) end end)
      if AtMerchant(BP) and (e.vendor or 0) > 0 then
        root:CreateButton("Sell to this vendor", function() if StillThere(e) then Sell(e) end end)
      end
    end)
    return
  end
  -- No context menus in this client: right-click keeps the item.
  ns.ToggleBagKeep(e.id)
  Print("will not suggest " .. name .. " again. /gl keep " .. name .. " undoes it.")
  BP:Refresh()
end

local function RowClick(row, button)
  local e, kind = row.entry, row.kind
  if not e then return end
  if button == "RightButton" then
    OpenMenu(row)
    return
  end
  local slot = kind == "merge" and e.from or e
  if type(_G.IsModifiedClick) == "function" and _G.IsModifiedClick("CHATLINK") and slot and slot.link then
    local CFU = _G.ChatFrameUtil
    if type(CFU) == "table" and type(CFU.InsertLink) == "function" then
      pcall(CFU.InsertLink, slot.link)
    elseif type(_G.ChatEdit_InsertLink) == "function" then
      pcall(_G.ChatEdit_InsertLink, slot.link)
    end
    return
  end
  if kind == "merge" then
    Merge(e)
  elseif not StillThere(e) then
    -- The bags changed since the list was drawn.
  elseif kind == "drop" and AtMerchant(BP) and (e.vendor or 0) > 0 then
    Sell(e)
  elseif kind == "drop" and CanDrop(e) then
    -- Only asks: the drop box names the stack, Drop it there destroys it.
    BP:AskDrop(e)
    return
  else
    -- Rare and better, and quest items: the game's own check applies.
    Pickup(e)
  end
  BP:RefreshSoon()
end

function BP:Row(i)
  local r = self.rows[i]
  if r then return r end
  r = CreateFrame("Button", nil, self.frame)
  r:SetHeight(ROW_H)
  if r.RegisterForClicks then r:RegisterForClicks("LeftButtonUp", "RightButtonUp") end
  local hl = r:CreateTexture(nil, "HIGHLIGHT")
  hl:SetAllPoints(r)
  hl:SetColorTexture(1, 1, 1, 0.06)
  r.icon = NewTex(r, "ARTWORK", 0)
  r.icon:SetSize(16, 16)
  r.icon:SetPoint("LEFT", r, "LEFT", 3, 0)
  r.icon:SetTexCoord(0.07, 0.93, 0.07, 0.93)
  r.right = NewFS(r, "GameFontHighlightSmall")
  r.right:SetJustifyH("RIGHT")
  r.right:SetPoint("RIGHT", r, "RIGHT", -3, 0)
  r.name = NewFS(r, "GameFontHighlightSmall")
  r.name:SetPoint("LEFT", r.icon, "RIGHT", 5, 0)
  r.name:SetPoint("RIGHT", r, "RIGHT", -62, 0)
  r:SetScript("OnClick", RowClick)
  r:SetScript("OnEnter", RowTooltip)
  r:SetScript("OnLeave", RowLeave)
  self.rows[i] = r
  return r
end

function BP:FillRow(r, e, kind)
  r.entry, r.kind = e, kind
  r.icon:SetTexture(e.icon or QUESTION)
  local name = e.name or ("Item " .. tostring(e.id))
  if kind == "merge" then
    name = name .. " (" .. e.stacks .. " stacks)"
  elseif e.count > 1 then
    name = name .. " x" .. e.count
  end
  r.name:SetText(name)
  r.name:SetTextColor(QualityRGB(e.quality))
  if kind == "merge" then
    r.right:SetText("frees " .. e.frees)
    SetFSColor(r.right, "pos")
  elseif kind == "quest" and e.qdb and e.why == "maybe" then
    -- From QuestieDB, not sure: gold, as the quest log's unsure ones.
    r.right:SetText(e.maybe == "hidden" and "check log" or (e.maybe == "reading" and "wait" or "maybe"))
    SetFSColor(r.right, "buy")
  elseif kind == "quest" and e.qdb then
    -- From QuestieDB: green when its quests are done, grey when not started.
    r.right:SetText(e.why == "done" and "done" or "not started")
    SetFSColor(r.right, e.why == "done" and "pos" or "muted")
  elseif kind == "quest" then
    r.right:SetText(e.why == "done" and "quest done" or (e.sure and "not needed" or "check log"))
    SetFSColor(r.right, e.sure and "muted" or "buy")
  else
    r.right:SetText((e.unsure and "~" or "") .. Money(e.value))
    SetFSColor(r.right, e.junk and "muted" or "text")
  end
end

function BP:Glow(button)
  if not self.built then return end
  local g = self.glow
  if not button then
    g:Hide()
    return
  end
  g:ClearAllPoints()
  g:SetPoint("TOPLEFT", button, "TOPLEFT", -2, 2)
  g:SetPoint("BOTTOMRIGHT", button, "BOTTOMRIGHT", 2, -2)
  if g.SetFrameStrata and button.GetFrameStrata then pcall(g.SetFrameStrata, g, button:GetFrameStrata()) end
  if g.SetFrameLevel and button.GetFrameLevel then pcall(g.SetFrameLevel, g, min(10000, (button:GetFrameLevel() or 0) + 5)) end
  g:Show()
end

local function SectionTitle(parent, text)
  local fs = NewFS(parent, "GameFontNormalSmall")
  fs:SetText(text)
  SetFSColor(fs, "muted")
  return fs
end

function BP:Build()
  if self.built then return true end
  local UI = _G.UIParent
  if type(_G.CreateFrame) ~= "function" or type(UI) ~= "table" then return false end
  -- built is set only once everything exists, so one failure is retried
  -- (a few times) instead of leaving a half-made panel for good.
  self.buildTries = (self.buildTries or 0) + 1
  if self.buildTries > 3 then return false end
  local template = (_G.BackdropTemplateMixin and "BackdropTemplate") or nil

  local F = CreateFrame("Frame", "ForeverAHBagPanel", UI, template)
  self.frame = F
  F:SetWidth(W)
  F:SetHeight(120)
  F:EnableMouse(true)
  if F.SetClampedToScreen then F:SetClampedToScreen(true) end
  Backdrop(F)
  F:Hide()

  self.title = NewFS(F, "GameFontNormal")
  self.title:SetPoint("TOPLEFT", F, "TOPLEFT", PAD, -8)
  self.title:SetText(Paint("Bag", "buy") .. Paint(" cleanup", "sell"))
  self.sub = NewFS(F, "GameFontHighlightSmall")
  self.sub:SetPoint("TOPLEFT", self.title, "BOTTOMLEFT", 0, -3)
  self.sub:SetPoint("RIGHT", F, "RIGHT", -PAD, 0)
  SetFSColor(self.sub, "muted")
  local close = TextButton(F, ">", 18, 18)
  close:SetPoint("TOPRIGHT", F, "TOPRIGHT", -6, -6)
  SetFSColor(close.text, "muted")
  close:SetScript("OnClick", function() ns.SetBagPanel(false) end)
  SimpleTip(close, "Tuck the cleanup panel away", "Bag cleanup on your bags brings it back.")
  self.closeButton = close

  self.mergeTitle = SectionTitle(F, "MERGE STACKS")
  self.questTitle = SectionTitle(F, "OLD QUEST ITEMS")
  self.dropTitle = SectionTitle(F, "WORTH THE LEAST")
  local dropHelp = CreateFrame("Frame", nil, F)
  dropHelp:SetAllPoints(self.dropTitle)
  dropHelp:EnableMouse(true)
  SimpleTip(dropHelp, "Worth the least",
    "Every stack in your bags, cheapest first: the better of what a vendor pays and what it sells for at auction now (after the 5% cut). Drop or sell from the top to lose the least. At a vendor, a click sells the stack if the vendor buys it (you can buy it back). Otherwise a click on a row (or Drop cheapest) asks before it drops; Rare and better items and quest items are only picked up, so the game asks. Your hearthstone, tools, reagents, ammo, keys, and food or potions near your level are never listed.")
  self.dropHelp = dropHelp
  self.empty = NewFS(F, "GameFontHighlightSmall")
  SetFSColor(self.empty, "muted")
  self.note = NewFS(F, "GameFontHighlightSmall")
  if self.note.SetWordWrap then self.note:SetWordWrap(true) end
  SetFSColor(self.note, "muted")

  local sell = TextButton(F, "Sell junk", W - 2 * PAD, 20)
  sell:SetScript("OnClick", function() ns.SellJunk() BP:RefreshSoon() end)
  SimpleTip(sell, "Sell junk", "Sells your grey items to this vendor (12 at a time; you can buy them back from the vendor).")
  sell:Hide()
  self.sellButton = sell

  local dropB = TextButton(F, "Drop cheapest", W - 2 * PAD, 20)
  dropB:SetScript("OnClick", function() BP:Refresh() BP:AskDrop(BP:Cheapest()) end)
  SimpleTip(dropB, "Drop cheapest", "Asks first: shows the stack that goes and what it is worth.")
  dropB:Hide()
  self.dropButton = dropB

  -- The drop box: covers the panel while it asks, so nothing else in the
  -- list can be clicked. Our own frame, not a StaticPopup: those are shared
  -- with the game, and its delete popups would hide ours.
  local c = CreateFrame("Frame", "ForeverAHBagConfirm", F)
  c:SetAllPoints(F)
  c:SetFrameLevel(min(10000, (F:GetFrameLevel() or 0) + 20))
  c:EnableMouse(true)
  local cbg = NewTex(c, "BACKGROUND", 2)
  cbg:SetAllPoints(c)
  local bgc = Col("bg")
  cbg:SetColorTexture(bgc[1], bgc[2], bgc[3], 1)
  c.head = NewFS(c, "GameFontNormal")
  c.head:SetPoint("TOPLEFT", c, "TOPLEFT", PAD, -8)
  c.head:SetText("Drop this for good?")
  SetFSColor(c.head, "buy")
  c.icon = NewTex(c, "ARTWORK", 0)
  c.icon:SetSize(20, 20)
  c.icon:SetPoint("TOPLEFT", c, "TOPLEFT", PAD, -30)
  c.icon:SetTexCoord(0.07, 0.93, 0.07, 0.93)
  c.name = NewFS(c, "GameFontHighlight")
  c.name:SetPoint("LEFT", c.icon, "RIGHT", 6, 0)
  c.name:SetPoint("RIGHT", c, "RIGHT", -PAD, 0)
  c.worth = NewFS(c, "GameFontHighlightSmall")
  c.worth:SetPoint("TOPLEFT", c.icon, "BOTTOMLEFT", 0, -6)
  c.worth:SetPoint("RIGHT", c, "RIGHT", -PAD, 0)
  c.note = NewFS(c, "GameFontHighlightSmall")
  c.note:SetPoint("TOPLEFT", c.worth, "BOTTOMLEFT", 0, -3)
  c.note:SetText("There is no undo.")
  SetFSColor(c.note, "muted")
  local half = floor((W - 2 * PAD - 6) / 2)
  local yes = TextButton(c, "Drop it", half, 20, "ForeverAHBagConfirmDrop")
  yes:SetPoint("TOPLEFT", c.note, "BOTTOMLEFT", 0, -8)
  SetFSColor(yes.text, "neg")
  yes:SetScript("OnClick", function() BP:DoDrop() end)
  local no = TextButton(c, "Cancel", half, 20, "ForeverAHBagConfirmCancel")
  no:SetPoint("LEFT", yes, "RIGHT", 6, 0)
  no:SetScript("OnClick", function() BP:CloseConfirm() end)
  c.yes, c.no = yes, no
  -- Escape, Cancel, or the panel or the bags closing: nothing is dropped.
  c:SetScript("OnHide", function() BP:CloseConfirm() end)
  c:Hide()
  self.confirmFrame = c
  F:HookScript("OnHide", function() BP:CloseConfirm() end)
  local special = _G.UISpecialFrames
  if type(special) == "table" then
    local listed = false
    for _, n in ipairs(special) do if n == "ForeverAHBagConfirm" then listed = true end end
    if not listed then special[#special + 1] = "ForeverAHBagConfirm" end
  end

  -- The way in: a button in the header of ElvUI's bag window, or a tab beside
  -- the game's bags. A thin gold edge makes it stand out from the dark buttons
  -- of the bag window.
  local tab = TextButton(UI, "Bag cleanup", TAB_W, TAB_H, "ForeverAHBagTab")
  if tab.SetClampedToScreen then tab:SetClampedToScreen(true) end
  local edge = Col("buy")
  for _, side in ipairs({ "TOP", "BOTTOM", "LEFT", "RIGHT" }) do
    local t = NewTex(tab, "BORDER", 0)
    t:SetColorTexture(edge[1], edge[2], edge[3], 0.9)
    if side == "TOP" or side == "BOTTOM" then
      t:SetPoint(side .. "LEFT", tab, side .. "LEFT", 0, 0)
      t:SetPoint(side .. "RIGHT", tab, side .. "RIGHT", 0, 0)
      t:SetHeight(1)
    else
      t:SetPoint("TOP" .. side, tab, "TOP" .. side, 0, 0)
      t:SetPoint("BOTTOM" .. side, tab, "BOTTOM" .. side, 0, 0)
      t:SetWidth(1)
    end
  end
  tab:SetScript("OnClick", function()
    local st = S()
    ns.SetBagPanel(not (st ~= nil and st.bagPanel == true))
  end)
  SimpleTip(tab, "Bag cleanup", function()
    local st = S()
    local open = st ~= nil and st.bagPanel == true
    local t = "What to drop or sell first: your cheapest stacks, old quest items and stacks to merge. Click to "
      .. (open and "close" or "open") .. " the list. /gl bags prints it in chat."
    local first = BP.scan and BP.scan.drop and BP.scan.drop[1]
    if first then
      t = t .. "\nDrop first: " .. (first.name or ("Item " .. tostring(first.id))) .. " (" .. Money(first.value) .. ")"
    end
    return t
  end)
  tab:Hide()
  self.tab = tab

  -- A gold frame around the bag slot of the row under the mouse.
  local g = CreateFrame("Frame", nil, UI)
  g:Hide()
  local gold = Col("buy")
  for _, side in ipairs({ "TOP", "BOTTOM", "LEFT", "RIGHT" }) do
    local t = NewTex(g, "OVERLAY", 7)
    t:SetColorTexture(gold[1], gold[2], gold[3], 1)
    if side == "TOP" or side == "BOTTOM" then
      t:SetPoint(side .. "LEFT", g, side .. "LEFT", 0, 0)
      t:SetPoint(side .. "RIGHT", g, side .. "RIGHT", 0, 0)
      t:SetHeight(2)
    else
      t:SetPoint("TOP" .. side, g, "TOP" .. side, 0, 0)
      t:SetPoint("BOTTOM" .. side, g, "BOTTOM" .. side, 0, 0)
      t:SetWidth(2)
    end
  end
  self.glow = g
  self.built = true
  return true
end

-- A frame's or font string's rectangle on screen in UIParent units (k is
-- UIParent's effective scale), or nil.
local function RectOf(r, k)
  if type(r) ~= "table" or type(r.GetRect) ~= "function" then return nil end
  local ok, l, b, w, h = pcall(r.GetRect, r)
  if not ok or not Num(l) or not Num(b) or not Num(w) or not Num(h) then return nil end
  local es
  if type(r.GetEffectiveScale) == "function" then
    local okS, v = pcall(r.GetEffectiveScale, r)
    es = okS and Num(v) or nil
  end
  if not es and type(r.GetParent) == "function" then
    local p = r:GetParent()
    if type(p) == "table" and type(p.GetEffectiveScale) == "function" then es = Num(p:GetEffectiveScale()) end
  end
  local sc = (es or k) / k
  return l * sc, b * sc, (l + w) * sc, (b + h) * sc
end

-- ElvUI's bag window leaves the left of its top row empty: its gold, help and
-- close buttons sit at the right, the search box and its buttons on the row
-- below. Where our button fits there: offsets from the window's top left and a
-- width (UIParent units), or nil when this is not that layout or there is no room.
local function HeaderSpot(f, k)
  local box = rawget(f, "editBox")
  if type(rawget(f, "holderFrame")) ~= "table" or type(box) ~= "table" then return nil end
  local l, _, r, t = RectOf(f, k)
  local bl, _, _, bt = RectOf(box, k)
  if not l or not bl then return nil end
  local room = t - bt
  if room < TAB_H + 4 then return nil end
  local top = t - floor((room - TAB_H) / 2 + 0.5)
  local low, left, right, blocked = top - TAB_H, bl, r - 4, false
  -- Whatever ElvUI shows on that row (gold, help, close, or anything a later
  -- version adds) ends the room for the button.
  local function limit(o)
    if type(o) ~= "table" or type(o.IsShown) ~= "function" or not o:IsShown() then return end
    local ol, ob, orr, ot = RectOf(o, k)
    if not ol or ob >= top or ot <= low or orr <= left then return end
    if ol <= l + 1 and orr >= r - 1 then return end   -- a backdrop over the whole window
    if ol <= left then blocked = true return end
    if ol < right then right = ol end
  end
  limit(rawget(f, "goldText"))
  if type(f.GetChildren) == "function" then
    for _, c in ipairs({ f:GetChildren() }) do limit(c) end
  end
  local w = min(HEADER_W, right - 8 - left)
  if blocked or w < HEADER_MIN then return nil end
  return left - l, top - t, w
end

local function Strata(f)
  if type(f.GetFrameStrata) ~= "function" then return nil end
  local ok, s = pcall(f.GetFrameStrata, f)
  return ok and Str(s) or nil
end

-- ElvUI's bag window: in its header, with the panel beside the window's top.
-- The game's bags: to the left of the leftmost open bag, level with the lowest.
-- A bag addon's window whose position cannot be read: hung off its bottom
-- left corner, which needs no numbers.
function BP:Place()
  if not self.built then return false end
  local UI = _G.UIParent
  local uiScale = (UI.GetEffectiveScale and Num(UI:GetEffectiveScale())) or 1
  local frames = OpenBagFrames()
  local left, bottom, strata, leftFrame, leftBottom
  for _, f in ipairs(frames) do
    local okL, l = pcall(f.GetLeft, f)
    local okB, b = pcall(f.GetBottom, f)
    l, b = okL and Num(l) or nil, okB and Num(b) or nil
    if l and b then
      local es
      if type(f.GetEffectiveScale) == "function" then
        local okS, v = pcall(f.GetEffectiveScale, f)
        es = okS and Num(v) or nil
      end
      local s = (es or 1) / uiScale
      l, b = l * s, b * s
      if not left or l < left then
        left, leftFrame, leftBottom = l, f, b
        strata = Strata(f)
      end
      if not bottom or b < bottom then bottom = b end
    end
  end
  local blind = false
  if not left then
    for _, f in ipairs(frames) do
      if IsAddonWindow(f) then
        leftFrame, blind, strata = f, true, Strata(f)
        break
      end
    end
    if not leftFrame then return false end
  end
  local tab, F = self.tab, self.frame
  local hx, hy, hw
  if #frames == 1 then
    local ok, x, y, wd = pcall(HeaderSpot, leftFrame, uiScale)
    if ok then hx, hy, hw = x, y, wd end
  end
  self.inHeader = hx ~= nil
  tab:ClearAllPoints()
  F:ClearAllPoints()
  if hx then
    tab:SetSize(hw, TAB_H)
    tab:SetPoint("TOPLEFT", leftFrame, "TOPLEFT", hx, hy)
    -- Inside the window: never pushed off it onto the search row.
    if tab.SetClampedToScreen then tab:SetClampedToScreen(false) end
    -- The panel beside the window's top, on the left; on the right when the
    -- window sits at the left edge of the screen.
    local l, _, r = RectOf(leftFrame, uiScale)
    local screenW = (UI.GetWidth and Num(UI:GetWidth())) or 0
    if l and r and l - 4 - W < 0 and screenW - r - 4 >= W then
      F:SetPoint("TOPLEFT", leftFrame, "TOPRIGHT", 4, 0)
    else
      F:SetPoint("TOPRIGHT", leftFrame, "TOPLEFT", -4, 0)
    end
  else
    tab:SetSize(TAB_W, TAB_H)
    if tab.SetClampedToScreen then tab:SetClampedToScreen(true) end
    for _, o in ipairs({ F, tab }) do
      if blind or leftBottom <= bottom + 0.5 then
        -- One bag window (the combined bags), the leftmost is also the
        -- lowest, or its position cannot be read: hang off it, so the panel
        -- follows when it moves or resizes.
        o:SetPoint("BOTTOMRIGHT", leftFrame, "BOTTOMLEFT", -4, 0)
      else
        o:SetPoint("BOTTOMRIGHT", UI, "BOTTOMLEFT", left - 4, bottom)
      end
    end
  end
  for _, o in ipairs({ F, tab }) do
    if strata and o.SetFrameStrata then pcall(o.SetFrameStrata, o, strata) end
  end
  if hx and tab.SetFrameLevel and leftFrame.GetFrameLevel then
    -- Drawn above the window it sits on.
    pcall(tab.SetFrameLevel, tab, min(10000, (leftFrame:GetFrameLevel() or 0) + 10))
  end
  return true
end

function BP:Layout(scan)
  local F = self.frame
  for _, r in ipairs(self.rows) do r:Hide() r.entry = nil end
  local y = -HEADER_H
  local used = 0
  local function rows(list, limit, kind)
    for i = 1, min(#list, limit) do
      used = used + 1
      local r = self:Row(used)
      r:ClearAllPoints()
      r:SetPoint("TOPLEFT", F, "TOPLEFT", PAD - 3, y)
      r:SetPoint("RIGHT", F, "RIGHT", -PAD + 3, 0)
      self:FillRow(r, list[i], kind)
      r:Show()
      y = y - ROW_H
    end
  end
  local function title(fs, text, n, limit)
    fs:SetText(text .. (n > limit and (" (" .. limit .. " of " .. n .. ")") or ""))
    fs:ClearAllPoints()
    fs:SetPoint("TOPLEFT", F, "TOPLEFT", PAD, y)
    fs:Show()
    y = y - TITLE_H
  end
  if #scan.merge > 0 then
    title(self.mergeTitle, "MERGE STACKS", #scan.merge, MAX_MERGE)
    rows(scan.merge, MAX_MERGE, "merge")
    y = y - GAP
  else
    self.mergeTitle:Hide()
  end
  if #scan.quest > 0 then
    title(self.questTitle, "OLD QUEST ITEMS", #scan.quest, MAX_QUEST)
    rows(scan.quest, MAX_QUEST, "quest")
    y = y - GAP
  else
    self.questTitle:Hide()
  end
  title(self.dropTitle, "WORTH THE LEAST", #scan.drop, MAX_DROP)
  if #scan.drop == 0 then
    self.empty:SetText("Nothing to suggest.")
    self.empty:ClearAllPoints()
    self.empty:SetPoint("TOPLEFT", F, "TOPLEFT", PAD, y - 3)
    self.empty:Show()
    y = y - ROW_H
  else
    self.empty:Hide()
    rows(scan.drop, MAX_DROP, "drop")
  end
  if scan.questPartial then
    self.note:SetText("Some quest log headers are closed: open them to check old quest items too.")
    self.note:ClearAllPoints()
    self.note:SetPoint("TOPLEFT", F, "TOPLEFT", PAD, y - 4)
    self.note:SetPoint("RIGHT", F, "RIGHT", -PAD, 0)
    self.note:Show()
    y = y - 30
  else
    self.note:Hide()
  end
  local sell = self.sellButton
  if AtMerchant(self) and #scan.junk > 0 then
    local total = 0
    for _, e in ipairs(scan.junk) do total = total + e.vendor * e.count end
    sell.text:SetText(sformat("Sell junk (%d, %s)", #scan.junk, Money(total)))
    sell:ClearAllPoints()
    sell:SetPoint("TOPLEFT", F, "TOPLEFT", PAD, y - 4)
    sell:Show()
    y = y - FOOTER_H
  else
    sell:Hide()
  end
  -- Away from a vendor: Drop cheapest (it asks first).
  local drop = self.dropButton
  if not AtMerchant(self) and self:Cheapest(scan) then
    drop:ClearAllPoints()
    drop:SetPoint("TOPLEFT", F, "TOPLEFT", PAD, y - 4)
    drop:Show()
    y = y - FOOTER_H
  else
    drop:Hide()
  end
  self.layoutH = -y + PAD
  F:SetHeight(self.confirm and max(self.layoutH, CONFIRM_H) or self.layoutH)
end

function BP:Refresh()
  if not self.built then return end
  local scan = ns.BagScan()
  self.scan = scan
  local clear = #scan.merge + #scan.quest + #scan.junk
  local name = Paint("Bag", "buy") .. Paint(" cleanup", "sell")
  self.tab.text:SetText(name .. "  " .. Paint(sformat("%d free", scan.free), scan.free <= 2 and "neg" or "muted"))
  local tw = (self.tab.GetWidth and Num(self.tab:GetWidth())) or TAB_W
  if self.tab.text.GetStringWidth and (Num(self.tab.text:GetStringWidth()) or 0) > tw - 10 then
    self.tab.text:SetText(name)
  end
  self.clear = clear
  self.sub:SetText(sformat("%d of %d slots free", scan.free, scan.slots))
  if scan.free <= 2 then SetFSColor(self.sub, "neg") else SetFSColor(self.sub, "muted") end
  if self.confirm and not SameStack(self.confirm) then
    local text = ItemText(self.confirm)
    self:CloseConfirm()
    Print(text .. " moved or changed, so nothing was dropped.")
  end
  if self.frame:IsShown() then self:Layout(scan) end
  -- A quest's text was not in yet: look again once it may be.
  if scan.questReading and not self.readWait then
    self.readWait = true
    After(QX.WAIT + 0.1, function()
      BP.readWait = nil
      BP:RefreshSoon()
    end)
  end
end

---------------------------------------------------------------------------
-- Dropping: the drop box asks, Drop it destroys that one stack
---------------------------------------------------------------------------
-- The cheapest stack on the list that the drop box may destroy (a cheaper
-- Rare item is skipped, so the box names what actually goes).
function BP:Cheapest(scan)
  scan = scan or self.scan
  if type(scan) ~= "table" or type(scan.drop) ~= "table" then return nil end
  for _, e in ipairs(scan.drop) do
    if CanDrop(e) then return e end
  end
  return nil
end

function BP:CloseConfirm()
  self.confirm = nil
  local c = self.confirmFrame
  if c and c:IsShown() then c:Hide() end
  self:Glow(nil)
  if self.layoutH and self.frame then self.frame:SetHeight(self.layoutH) end
end

function BP:AskDrop(e)
  local c, F = self.confirmFrame, self.frame
  -- Only over the open panel: a menu entry picked after the bags closed does
  -- nothing, so reopening them shows the list, not a box waiting for a click.
  if not self.built or not (F and F:IsVisible()) or not CanDrop(e) or not StillThere(e) then return end
  self.confirm = { bag = e.bag, slot = e.slot, id = e.id, count = e.count, link = e.link, name = e.name,
                   value = e.value, source = e.source, unsure = e.unsure }
  self.confirmAt = Now()
  c.icon:SetTexture(e.icon or QUESTION)
  local name = e.name or ("Item " .. tostring(e.id))
  if e.count > 1 then name = name .. " x" .. e.count end
  c.name:SetText(name)
  c.name:SetTextColor(QualityRGB(e.quality))
  c.worth:SetText(WorthText(e))
  SetFSColor(c.worth, (e.value or 0) >= 10000 and "neg" or "text")
  -- Over the list, whatever layer the panel was moved to.
  local strata = Strata(F)
  if strata then pcall(c.SetFrameStrata, c, strata) end
  pcall(c.SetFrameLevel, c, min(10000, (F:GetFrameLevel() or 0) + 20))
  F:SetHeight(max(self.layoutH or 0, CONFIRM_H))
  c:Show()
  self:Glow(SlotButton(e.bag, e.slot))
end

-- Drop it: runs inside that click. DeleteCursorItem needs the click itself
-- and destroys one item per click, so it is never called from a timer, an
-- event or a slash command.
function BP:DoDrop()
  local t = self.confirm
  if not t then return end
  local now = Now()
  if now and self.confirmAt and now - self.confirmAt < DROP_GUARD then
    -- Too soon: part of a double-click or a burst of clicks. Each early click
    -- starts the wait again, so only a click on its own drops.
    self.confirmAt = now
    return
  end
  local text = ItemText(t)
  self:CloseConfirm()
  if CursorHasItem() then
    Print("put down the item on your cursor first. Nothing was dropped.")
    return
  end
  local e, why = LiveDrop(t)
  if not e then
    if why == "unlisted" then
      Print(text .. " is not on the list any more, so nothing was dropped.")
    else
      Print(text .. " moved or changed, so nothing was dropped.")
    end
    self:RefreshSoon()
    return
  end
  -- Worth more now than the box said (a live auction price came in while it
  -- asked), or valued another way: show the new worth and ask again.
  if (e.value or 0) > (t.value or 0) or e.source ~= t.source then
    self:AskDrop(e)
    Print(text .. " is worth " .. ChatMoney(e.value) .. (e.source == "auction" and " at auction" or " at a vendor")
      .. " now. Nothing was dropped: check the box again.")
    return
  end
  local pick = ContainerFn("PickupContainerItem")
  if not pick or not pcall(pick, e.bag, e.slot) or not CursorHolds(e) then
    if CursorHasItem() then ClearCursor() end
    Print("could not pick up " .. text .. ". Nothing was dropped.")
    return
  end
  local del = _G.DeleteCursorItem
  if type(del) == "function" then pcall(del) end
  if CursorHasItem() then
    -- The game said no: leave it on the cursor so it can be finished by hand.
    Print("the game did not let Goblin Ledger drop " .. text .. ". It is on your cursor: click the ground to destroy it (the game asks first), or a bag slot to put it back.")
    return
  end
  local p = { bag = e.bag, slot = e.slot, id = e.id, count = e.count, text = text, value = e.value, tries = 0 }
  self.pendingDrop = p
  After(0.5, function() BP:CheckDrop(p) end)
end

-- The server has the last word: say what happened once the slot settles
-- (while it stays locked, look again, up to 3 more seconds).
function BP:CheckDrop(p)
  if type(p) ~= "table" or p.said then return end
  local info = SlotInfo(p.bag, p.slot)
  local same = info ~= nil and Num(info.itemID) == p.id and (Num(info.stackCount) or 1) == p.count
  if same and info.isLocked == true and p.tries < 6 then
    p.tries = p.tries + 1
    After(0.5, function() BP:CheckDrop(p) end)
    return
  end
  p.said = true
  if self.pendingDrop == p then self.pendingDrop = nil end
  if same then
    Print(p.text .. " is still in your bags: the game did not destroy it.")
  else
    Print("dropped " .. p.text .. " (worth " .. ChatMoney(p.value) .. ").")
  end
  self:RefreshSoon()
end

local function PanelWanted()
  local st = S()
  return st ~= nil and st.bagPanel == true
end

-- Show the tab or the panel while bags are open; hide both when they close.
-- stats.result says how the last update ended (for /gl bagcheck).
function BP:Update()
  local st = self.stats
  st.ran = st.ran + 1
  if #OpenBagFrames() == 0 then
    if self.built then
      self.frame:Hide()
      self.tab:Hide()
      self.glow:Hide()
    end
    st.result = "no bag window open"
    return
  end
  st.result = "could not make the button"
  if not self:Build() then return end
  if not self:Place() then
    -- Nothing to hang it on: hidden, not left where it was.
    self.frame:Hide()
    self.tab:Hide()
    st.result = "button made, no place for it"
    return
  end
  st.result = "could not fill in the list"
  local open = PanelWanted()
  if open then self.frame:Show() else self.frame:Hide() end
  -- In ElvUI's header the button stays, as the way to close the list too.
  if open and not self.inHeader then self.tab:Hide() else self.tab:Show() end
  local c = Col(open and "buy" or "panel")
  if open then
    self.tab.bg:SetColorTexture(c[1] * 0.35, c[2] * 0.35, c[3] * 0.35, 1)
  else
    self.tab.bg:SetColorTexture(c[1] + 0.05, c[2] + 0.05, c[3] + 0.06, 1)
  end
  self:Refresh()
  st.result = open and "panel shown" or "button shown"
end

function BP:Visible()
  return self.built and (self.frame:IsShown() or self.tab:IsShown()) or false
end

-- A problem in the bag cleanup is kept, and said once in chat, instead of
-- vanishing (the game hides Lua errors unless they are switched on).
function BP:Fail(err)
  self.lastError = err
  if self.failSaid then return end
  self.failSaid = true
  Print("the bag cleanup hit a problem (" .. tostring(err) .. "). /gl bags still lists what to clear, /gl bagcheck says more.")
end

local updateToken, refreshToken = 0, 0
function BP:UpdateSoon()
  self.stats.asked = self.stats.asked + 1
  updateToken = updateToken + 1
  local tok = updateToken
  After(0, function()
    if tok == updateToken then
      local ok, err = pcall(BP.Update, BP)
      if not ok then BP:Fail(err) end
    end
  end)
end

function BP:RefreshSoon()
  if not self:Visible() then return end
  refreshToken = refreshToken + 1
  local tok = refreshToken
  After(0.3, function()
    if tok == refreshToken and BP:Visible() then
      local ok, err = pcall(BP.Refresh, BP)
      if not ok then BP:Fail(err) end
    end
  end)
end

function ns.SetBagPanel(on)
  local st = S()
  if st then st.bagPanel = on and true or false end
  local ok, err = pcall(BP.Update, BP)
  if not ok then BP:Fail(err) end
end

-- How the tab learns that bags opened or closed. Every hook is a post-hook
-- (nothing of the game's or ElvUI's is replaced); all but the anchor hook
-- only ask for an update on the next frame.
local hooked = setmetatable({}, { __mode = "k" })      -- bag window -> OnShow/OnHide script hooks took
local hookedShow = setmetatable({}, { __mode = "k" })  -- bag window -> { Show = ok, Hide = ok } method hooks
-- The game's bag functions (keys, the bag bar, other addons call them).
local TOGGLES = { "ToggleBag", "ToggleBackpack", "ToggleAllBags", "OpenAllBags", "CloseAllBags", "OpenBackpack", "CloseBackpack" }

local function Soon() BP:UpdateSoon() end

-- Opening or closing bags by key, button or a bag addon also looks for a bag
-- window made after we last looked (ElvUI builds its own once you log in).
local function Toggled()
  local ok, err = pcall(BP.HookBagFrames)
  if not ok then BP:Fail(err) end
  BP:UpdateSoon()
end

-- ElvUI also opens and closes its bags by itself (at a vendor, the auction
-- house, a trade) without the game's bag functions: post-hook its two.
local function HookElvUI(hsf)
  local hk = BP.stats.elv
  if hk.OpenBags and hk.CloseAllBags then return end
  local eng = _G.ElvUI
  local E = type(eng) == "table" and eng[1] or nil
  if type(E) ~= "table" then return end
  local B
  if type(E.GetModule) == "function" then
    local ok, m = pcall(E.GetModule, E, "Bags", true)
    if ok and type(m) == "table" then B = m end
  end
  if not B then return end
  for _, fn in ipairs({ "OpenBags", "CloseAllBags" }) do
    if not hk[fn] and type(B[fn]) == "function" then
      hk[fn] = pcall(hsf, B, fn, Toggled)
    end
  end
end

local function HookBagFrames()
  local hsf = type(_G.hooksecurefunc) == "function" and _G.hooksecurefunc or nil
  for _, f in ipairs(BagFrames()) do
    if not hooked[f] and type(f.HookScript) == "function" then
      local ok = pcall(f.HookScript, f, "OnShow", Soon)
      local ok2 = pcall(f.HookScript, f, "OnHide", Soon)
      pcall(f.HookScript, f, "OnDragStop", function() if BP:Visible() then BP:UpdateSoon() end end)
      hooked[f] = ok and ok2
    end
    -- Hooks on the frame's Show and Hide stay when a bag addon clears its
    -- scripts (and our script hooks with them, as ElvUI does to the game's
    -- bags).
    local h = hookedShow[f]
    if hsf and not (h and h.Show and h.Hide) then
      h = h or {}
      hookedShow[f] = h
      if not h.Show then h.Show = pcall(hsf, f, "Show", Soon) end
      if not h.Hide then h.Hide = pcall(hsf, f, "Hide", Soon) end
    end
  end
  if not hsf then return end
  if not BP.hookedAnchors and type(_G.UpdateContainerFrameAnchors) == "function" then
    BP.hookedAnchors = pcall(hsf, "UpdateContainerFrameAnchors", function()
      if BP:Visible() then pcall(BP.Place, BP) end
    end)
  end
  -- Each hook is tried until it takes: a function another addon makes
  -- later is still hooked, and none is hooked twice.
  local tg = BP.stats.toggles
  for _, fn in ipairs(TOGGLES) do
    if not tg[fn] and type(_G[fn]) == "function" then
      tg[fn] = pcall(hsf, fn, Toggled)
    end
  end
  HookElvUI(hsf)
end
BP.HookBagFrames = HookBagFrames
pcall(HookBagFrames)

---------------------------------------------------------------------------
-- Events
---------------------------------------------------------------------------
local function IsMerchant(t)
  local E = _G.Enum
  local PIT = type(E) == "table" and E.PlayerInteractionType
  return type(PIT) == "table" and PIT.Merchant ~= nil and t == PIT.Merchant
end

local function Changed() BP:RefreshSoon() end

-- By login ElvUI has built its bag window and set up its bag module: both
-- get hooked here (and again on entering the world, in case either is late).
-- A few seconds later QuestieDB's quests are read (or the saved index used).
local handlers = {
  PLAYER_LOGIN = function() HookBagFrames() BP:UpdateSoon() After(QI_DELAY, StartIndex) end,
  PLAYER_ENTERING_WORLD = function() HookBagFrames() end,
  BAG_UPDATE_DELAYED = Changed,
  BAG_UPDATE = Changed,
  QUEST_LOG_UPDATE = Changed,
  PLAYER_LEVEL_UP = Changed,
  PLAYER_MONEY = function() if BP:Visible() then BP:UpdateSoon() end end,
  GET_ITEM_INFO_RECEIVED = function() if BP.scan and BP.scan.pending then Changed() end end,
  MERCHANT_SHOW = function() BP.merchant = true Changed() end,
  MERCHANT_CLOSED = function() BP.merchant = false Changed() end,
  PLAYER_INTERACTION_MANAGER_FRAME_SHOW = function(t) if IsMerchant(t) then BP.merchant = true Changed() end end,
  PLAYER_INTERACTION_MANAGER_FRAME_HIDE = function(t) if IsMerchant(t) then BP.merchant = false Changed() end end,
}

if type(_G.CreateFrame) == "function" then
  local ok, ev = pcall(_G.CreateFrame, "Frame")
  if ok and type(ev) == "table" then
    BP.eventFrame = ev
    for name in pairs(handlers) do pcall(ev.RegisterEvent, ev, name) end
    ev:SetScript("OnEvent", function(_, event, ...)
      local h = handlers[event]
      if not h then return end
      local okH, err = pcall(h, ...)
      if not okH then BP:Fail(err) end
    end)
  end
end

---------------------------------------------------------------------------
-- Slash commands: /gl bags, /gl keep, /gl bagcheck, /gl questcheck
---------------------------------------------------------------------------
local function ListText(list, limit, fn)
  local parts = {}
  for i = 1, min(#list, limit) do parts[#parts + 1] = fn(list[i]) end
  local s = tconcat(parts, ", ")
  if #list > limit then s = s .. sformat(" and %d more", #list - limit) end
  return s
end

local function Report()
  local s = ns.BagScan()
  Print(sformat("bags: %d of %d slots free.", s.free, s.slots))
  if #s.merge > 0 then
    Print("merge stacks (frees a slot, costs nothing): " .. ListText(s.merge, 4, function(m)
      return (m.from.link or m.name or ("Item " .. m.id)) .. " (" .. m.stacks .. " stacks)"
    end))
  end
  if #s.quest > 0 then
    Print("old quest items: " .. ListText(s.quest, 6, function(e)
      if e.qdb and e.why == "maybe" then
        if e.maybe == "hidden" then return ItemText(e) .. " (check your quest log)" end
        if e.maybe == "reading" then return ItemText(e) .. " (still reading your quest log)" end
        return ItemText(e) .. " (maybe needed for " .. (e.quest or "the next quest") .. ")"
      elseif e.qdb then
        return ItemText(e) .. " (" .. (e.why == "done" and "done" or "not started") .. (e.quest and (": " .. e.quest) or "") .. ")"
      end
      return ItemText(e) .. " (" .. (e.why == "done" and "quest done" or (e.sure and "no quest needs it" or "check your quest log")) .. ")"
    end))
  end
  if s.questBuilding then
    Print("still reading Questie's quests: type /gl bags again in a moment to check every quest item.")
  end
  if s.questPartial then
    Print("some quest log headers are closed: open them so old quest items can be checked too.")
  end
  if #s.junk > 0 then
    local total = 0
    for _, e in ipairs(s.junk) do total = total + e.vendor * e.count end
    Print(sformat("junk: %d grey item%s, %s at a vendor.", #s.junk, #s.junk == 1 and "" or "s", ChatMoney(total)))
  end
  if #s.drop == 0 then
    Print("nothing else to suggest.")
  else
    Print("worth the least (drop or sell these first):")
    for i = 1, min(#s.drop, SLASH_DROP) do
      local e = s.drop[i]
      local tag = e.junk and " (junk)" or (e.source == "auction" and " (auction)" or "")
      Print(sformat("  %d. %s - %s%s%s", i, ItemText(e), e.unsure and "about " or "", ChatMoney(e.value), tag))
    end
  end
  if #OpenBagFrames() == 0 then Print("Open your bags and click Bag cleanup to clean up from a list.") end
  return s
end
ns.BagReport = Report

---------------------------------------------------------------------------
-- /gl bagcheck: what the bag cleanup sees, in one screenshot
---------------------------------------------------------------------------
local function YN(v) return v and "yes" or "no" end

local function FrameName(f)
  if type(f) ~= "table" or type(f.GetName) ~= "function" then return "nothing" end
  local ok, n = pcall(f.GetName, f)
  return ok and Str(n) or "a frame with no name"
end

-- Where a frame is: how far across and up the screen its bottom left corner
-- sits, and its size in screen pixels.
local function Where(f)
  if type(f) ~= "table" or type(f.GetRect) ~= "function" then return "no position" end
  local ok, l, b, w, h = pcall(f.GetRect, f)
  if not ok then return "no position" end
  if IsSecret(l) or IsSecret(b) or IsSecret(w) or IsSecret(h) then return "secret position" end
  l, b, w, h = Num(l), Num(b), Num(w), Num(h)
  if not (l and b and w and h) then return "no position" end
  local UI = _G.UIParent
  local okS, s = pcall(f.GetEffectiveScale, f)
  s = okS and Num(s) or 1
  local us = Num(UI:GetEffectiveScale()) or 1
  local sw, sh = (Num(UI:GetWidth()) or 0) * us, (Num(UI:GetHeight()) or 0) * us
  if sw <= 0 or sh <= 0 then return "no position" end
  local px = 1
  if type(_G.GetPhysicalScreenSize) == "function" then
    local okP, _, ph = pcall(_G.GetPhysicalScreenSize)
    if okP and Num(ph) and ph > 0 then px = ph / 768 end
  end
  return sformat("%d%% from left, %d%% up, %dx%d px", floor(l * s / sw * 100 + 0.5), floor(b * s / sh * 100 + 0.5),
    floor(w * s * px + 0.5), floor(h * s * px + 0.5))
end

local function SecretNote(f)
  if type(_G.issecretvalue) ~= "function" or type(f) ~= "table" or type(f.HasSecretValues) ~= "function" then return "" end
  local ok, r = pcall(f.HasSecretValues, f)
  if ok and r == true then return ", secret values yes" end
  return ""
end

local function ScriptErrors()
  local CV = _G.C_CVar
  local f = (type(CV) == "table" and type(CV.GetCVar) == "function") and CV.GetCVar or _G.GetCVar
  if type(f) ~= "function" then return "unknown" end
  local ok, v = pcall(f, "scriptErrors")
  if not ok or v == nil then return "unknown" end
  return (tostring(v) == "1") and "on" or "off"
end

-- The button turns gold and comes to the front for a while, so it can be
-- found on a screenshot. Its layer and colour come back after.
function BP:Flash(sec)
  local tab = self.tab
  if not tab then return end
  local strata = Strata(tab)
  pcall(tab.SetFrameStrata, tab, "TOOLTIP")
  local g = Col("buy")
  tab.bg:SetColorTexture(g[1], g[2], g[3], 1)
  self.flashToken = (self.flashToken or 0) + 1
  local tok = self.flashToken
  After(sec, function()
    if BP.flashToken ~= tok then return end
    if strata then pcall(tab.SetFrameStrata, tab, strata) end
    BP:UpdateSoon()
  end)
end

local function BagCheck()
  local st = BP.stats
  Print("bag check, Bags.lua " .. BAGS_BUILD)
  -- 1. ElvUI's bag window
  local elv = _G.ElvUI_ContainerFrame
  if type(elv) == "table" and type(elv.IsVisible) == "function" then
    local okV, vis = pcall(elv.IsVisible, elv)
    local hs = hookedShow[elv]
    Print(sformat("1. ElvUI bags: open %s, %s, %s, hooked %s%s", YN(okV and vis), Where(elv), Strata(elv) or "no layer",
      YN(hooked[elv] or (hs and hs.Show and hs.Hide)), SecretNote(elv)))
  else
    Print("1. ElvUI bags: not found" .. (type(_G.ElvUI) == "table" and "" or " (ElvUI is not loaded)"))
  end
  -- 2. The game's own bag windows that are shown
  local shown = {}
  for _, f in ipairs(BagFrames()) do
    if not IsAddonWindow(f) then
      local ok, vis = pcall(f.IsVisible, f)
      if ok and vis then shown[#shown + 1] = FrameName(f) .. " " .. Where(f) end
    end
  end
  local more = #shown > 4 and sformat(" and %d more", #shown - 4) or ""
  while #shown > 4 do shown[#shown] = nil end
  Print("2. game bag windows open: " .. (#shown > 0 and tconcat(shown, ", ") .. more or "none"))
  -- 3. What the button hangs on
  local names = {}
  for _, f in ipairs(OpenBagFrames()) do names[#names + 1] = FrameName(f) end
  Print("3. windows the button uses: " .. (#names > 0 and tconcat(names, ", ")
    or "none (open your bags, then type /gl bagcheck again)"))
  -- 4. Hooks
  local parts = {}
  for _, fn in ipairs(TOGGLES) do
    local v = st.toggles[fn]
    parts[#parts + 1] = fn .. " " .. (v == true and "yes" or (v == false and "failed" or "no"))
  end
  local elvHook = "not loaded"
  if type(_G.ElvUI) == "table" then elvHook = YN(st.elv.OpenBags and st.elv.CloseAllBags) end
  parts[#parts + 1] = "ElvUI " .. elvHook
  Print("4. key hooks: " .. tconcat(parts, ", "))
  -- 5. Updates
  Print(sformat("5. button updates asked %d, run %d, last: %s", st.asked, st.ran, st.result))
  -- 6. The button
  local tab = BP.tab
  if not BP.built or type(tab) ~= "table" then
    Print("6. button: not made yet")
  else
    local okP, _, rel = pcall(tab.GetPoint, tab, 1)
    local okL, level = pcall(tab.GetFrameLevel, tab)
    Print(sformat("6. button: shown %s, on screen %s, %s, %s level %s, hangs on %s%s%s", YN(tab:IsShown()), YN(tab:IsVisible()),
      Where(tab), Strata(tab) or "no layer", tostring(okL and Num(level) or "?"), okP and rel and FrameName(rel) or "nothing",
      BP.inHeader and " (in its header)" or "", SecretNote(tab)))
  end
  -- 7. The last problem
  Print("7. last error: " .. (BP.lastError ~= nil and tostring(BP.lastError) or "none"))
  -- 8. The scan
  local okS, s = pcall(ns.BagScan)
  if okS and type(s) == "table" then
    -- With QuestieDB: where the quest item verdicts come from.
    local okQ, q = pcall(QuestIndexText)
    q = okQ and q or nil
    Print(sformat("8. scan: %d of %d slots free, %d to clear, %d cheap stack%s%s", s.free, s.slots,
      #s.merge + #s.quest + #s.junk, #s.drop, #s.drop == 1 and "" or "s", q and (", " .. q) or ""))
  else
    Print("8. scan error: " .. tostring(s))
  end
  -- 9. Whether the game shows Lua errors
  Print("9. Lua errors on screen: " .. ScriptErrors())
  -- 10. One update now
  local okU, err = pcall(BP.Update, BP)
  if not okU then BP:Fail(err) end
  Print("10. button update now: " .. (okU and st.result or ("error: " .. tostring(err))))
  if BP.built and BP.tab:IsShown() then
    BP:Flash(10)
    Print("The Bag cleanup button flashes gold for 10 seconds. Take a screenshot of your whole screen.")
  elseif BP.built and BP.frame:IsShown() then
    Print("The cleanup panel is open beside your bags. Take a screenshot of your whole screen.")
  elseif #OpenBagFrames() == 0 then
    Print("Open your bags, then type /gl bagcheck again and take a screenshot.")
  else
    Print("The Bag cleanup button did not show. Take a screenshot of these lines.")
  end
end
ns.BagCheck = BagCheck

-- An item in your bags by its name (lowercased).
function QX.BagItemNamed(want)
  for _, bag in ipairs(BagIDs()) do
    for slot = 1, NumSlots(bag) do
      local info = SlotInfo(bag, slot)
      local iid = info and Num(info.itemID)
      local gi = iid and ItemInfo(iid)
      if gi and gi.name and slower(gi.name) == want then return iid end
    end
  end
  return nil
end

local function KeepCommand(rest)
  local keep = KeepList()
  if rest == "" then
    local names = {}
    for id in pairs(keep) do
      local gi = ItemInfo(id)
      names[#names + 1] = (gi and gi.name) or ("Item " .. tostring(id))
    end
    sort(names)
    if #names == 0 then
      Print("you keep nothing yet. /gl keep Item Name (or shift-click it) stops the bag cleanup suggesting it.")
    else
      Print("never suggested in bag cleanup: " .. tconcat(names, ", "))
    end
    return
  end
  local id = tonumber(smatch(rest, "item:(%d+)") or smatch(rest, "^%d+$") or "")
  -- An item in your bags, by name.
  if not id then id = QX.BagItemNamed(slower(rest)) end
  if not id then
    for kid in pairs(keep) do
      local gi = ItemInfo(kid)
      if gi and gi.name and slower(gi.name) == slower(rest) then id = kid break end
    end
  end
  if not id and type(ns.ResolveItem) == "function" then id = ns.ResolveItem(rest) end
  if not id then
    Print("no item called " .. rest .. " in your bags. Shift-click it into the chat box instead.")
    return
  end
  local gi = ItemInfo(id)
  local name = (gi and gi.name) or ("Item " .. tostring(id))
  if ns.ToggleBagKeep(id) then
    Print("bag cleanup will never suggest " .. name .. ".")
  else
    Print("bag cleanup may suggest " .. name .. " again.")
  end
  BP:RefreshSoon()
end

---------------------------------------------------------------------------
-- /fah questcheck: why a quest item says Keep, Can drop or not sure
---------------------------------------------------------------------------
QX.ROLE_TEXT = { use = "needs it", give = "gives it", start = "starts it" }

-- One quest's state for you: in your log (that counts first, as for the
-- verdict, even when the game says it is done), done, not started, and so on.
function QX.State(L, q, ctx)
  local s = InLog(q, ctx)
  if s == true then return QuestDone(q) and "in your log (game says done)" or "in your log" end
  local info = QuestInfo(L, q)
  if not info then return QuestDone(q) and "done (not in QuestieDB)" or "not in QuestieDB" end
  if not ForPlayer(L, info) then return "not for you" end
  if QuestDone(q) then return "done" end
  if Closed(info, q) then return "shut by one you did" end
  if s == nil then return "headers closed" end
  return "not started"
end

-- The item an argument names: a link, an id, or an item in your bags.
function QX.ItemFromText(rest)
  local id = tonumber(smatch(rest, "item:(%d+)") or smatch(rest, "^%d+$") or "")
  if id then return id end
  local name = smatch(rest, "^%[(.-)%]$") or rest
  id = QX.BagItemNamed(slower(name))
  if id then return id end
  if type(ns.ResolveItem) == "function" then
    local ok, r = pcall(ns.ResolveItem, name)
    if ok and Num(r) then return r end
  end
  return nil
end

function QX.Check(rest)
  local id
  if rest == "" then id = QI.lastItem else id = QX.ItemFromText(rest) end
  if not id then
    if rest == "" then
      Print("quest check: hover a quest item first, or type /gl questcheck Item Name.")
    else
      Print("quest check: no item called " .. rest .. " in your bags. Shift-click it into the chat box instead.")
    end
    return
  end
  local L = QuestieReady()
  local gi = ItemInfo(id)
  local name = QX.ItemName(id, L)
  local use = ItemUsable(id)
  Print(sformat("quest check: %s (item %d, Use effect %s), Bags.lua %s", (gi and gi.link) or name or ("Item " .. id), id,
    use == true and "yes" or (use == false and "no" or "not loaded"), BAGS_BUILD))
  local log = ReadQuestLog()
  local ctx = { log = log }
  if not L then
    Print("QuestieDB: " .. (QuestIndexText() or "not loaded"))
  else
    local parts, givers, seen = {}, {}, {}
    local function quest(q, role)
      q = Num(q)
      if not q or q <= 0 or seen[q] then return end
      seen[q] = true
      local info = QuestInfo(L, q)
      parts[#parts + 1] = sformat("%d %s (%s): %s", q, info and info.name or "?", QX.ROLE_TEXT[role], QX.State(L, q, ctx))
      if role == "give" then givers[#givers + 1] = q end
    end
    quest(L.Item.Get(id, "startQuest"), "start")
    local entry = QI.map and QI.map[id]
    for c in sgmatch(tostring(entry or ""), "%d+") do
      c = tonumber(c)
      if c % 4 == ROLE_USE then quest(floor(c / 4), "use") elseif c % 4 == ROLE_GIVE then quest(floor(c / 4), "give") end
    end
    Print("QuestieDB: " .. (#parts > 0 and ListText(parts, 4, function(p) return p end) or "no quest uses it"))
    -- The quests after one that gave it, up to the first you have not done.
    for i = 1, min(#givers, 2) do
      local g = givers[i]
      local info, chain, been = QuestInfo(L, g), {}, { [g] = true }
      local n = info and info.nextQ
      for _ = 1, QX.CHAIN_MAX do
        if not n or n <= 0 or been[n] then break end
        been[n] = true
        local ni = QuestInfo(L, n)
        local st = QX.State(L, n, ctx)
        chain[#chain + 1] = sformat("%d %s: %s", n, ni and ni.name or "?", st)
        if not ni or (st ~= "done" and st ~= "shut by one you did") then break end
        n = ni.nextQ
      end
      Print(sformat("after %d: %s", g, #chain > 0 and tconcat(chain, ", ") or "no quest"))
    end
  end
  local hits, hp = QX.Hits(name, log, true), {}
  for i = 1, min(#hits, 3) do
    local e = hits[i].e
    hp[#hp + 1] = sformat("%s%s (%s)", e.q and (e.q .. " ") or "", e.title or "?", hits[i].how)
  end
  Print(sformat("quest log: %s; %d quest%s read%s%s", #hp > 0 and ("named by " .. tconcat(hp, ", ")) or "no quest names it",
    log.count, log.count == 1 and "" or "s", log.complete and "" or ", some headers closed",
    log.pending and ", some quest text not in yet" or ""))
  local v
  if L and QI.state ~= "off" then
    local ok, r = pcall(QuestieVerdict, id, log, nil)
    if ok then v = r else QI.Fail(r) end
  end
  if v then
    Print("verdict: " .. (QuestLineText(v)))
    return
  end
  local why, sure = QuestVerdict({ id = id, name = name }, nil, log)
  if why == "none" and sure then
    Print("verdict (quest log only): no quest in your log asks for it")
  elseif why == "none" then
    Print("verdict (quest log only): not sure, no quest in your log names it")
  else
    Print("verdict (quest log only): keep, a quest may need it")
  end
end
ns.QuestCheck = QX.Check

local baseSlash = ns.HandleSlash
function ns.HandleSlash(msg)
  local m = type(msg) == "string" and msg:gsub("^%s+", ""):gsub("%s+$", "") or ""
  local cmd, rest = m:match("^(%S*)%s*(.-)$")
  cmd = (cmd or ""):lower()
  rest = rest or ""
  local bagsCmd = cmd == "bags" or cmd == "bag" or cmd == "cleanup" or cmd == "clean"
  if cmd == "bagcheck" or (bagsCmd and rest:lower() == "check") then
    BagCheck()
    return
  elseif bagsCmd then
    -- Said in chat when the scan breaks, instead of printing nothing.
    local ok, err = pcall(Report)
    if not ok then Print("bag scan error: " .. tostring(err)) end
    return
  elseif cmd == "keep" then
    KeepCommand(rest)
    return
  elseif cmd == "questcheck" or cmd == "qcheck" then
    local ok, err = pcall(QX.Check, rest)
    if not ok then Print("quest check failed: " .. tostring(err)) end
    return
  end
  if type(baseSlash) == "function" then baseSlash(msg) end
  if cmd == "help" or cmd == "?" then
    Print("/gl bags : what to drop or sell first, old quest items and stacks to merge (also beside your open bags)")
    Print("/gl keep Item Name : never suggest dropping an item (again to undo; no name lists them)")
    Print("/gl bagcheck : what the bag cleanup sees (type it with your bags open if the Bag cleanup button is missing)")
    Print("/gl questcheck Item Name : why a quest item says Keep or Can drop (no name: the last one you hovered)")
  end
end
