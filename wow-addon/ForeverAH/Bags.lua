local ADDON_NAME, ns = ...
-- Forever AH: bag cleanup.
-- When your bags fill up, this says what to get rid of first:
--   Merge stacks    : partial stacks of the same item that fit in one slot
--                     (frees a slot and costs nothing)
--   Old quest items : quest items no quest in your log asks for, and items
--                     that start a quest you already finished
--   Worth the least : everything else, cheapest stack first, valued at the
--                     better of what a vendor pays and what it sells for at
--                     auction right now (after the 5% cut)
-- It shows as a small tab beside your open bags (a slim panel when you click
-- it), and in chat with /fah bags. Nothing is destroyed or sold on its own:
-- clicking a row picks the item up (click the ground and the game asks before
-- destroying it), and only at an open vendor does a click sell it.

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
local SLASH_DROP = 5        -- /fah bags prints this many
local SELL_BATCH = 12       -- junk sold per click (the vendor's buyback holds 12)
local OUTGROWN = 15         -- food, potions and the like this many levels under you are fair game

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
  if type(cf) == "table" and cf.AddMessage then cf:AddMessage("Forever AH: " .. tostring(msg)) end
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

local function ReadQuestLog()
  local log = { needs = {}, words = {}, count = 0, bare = 0, complete = true }
  local QL = type(_G.C_QuestLog) == "table" and _G.C_QuestLog or nil
  local function objectives(questID, index)
    local texts, read = {}, false
    if QL and type(QL.GetQuestObjectives) == "function" and Num(questID) and questID > 0 then
      local ok, list = pcall(QL.GetQuestObjectives, questID)
      if ok and type(list) == "table" then
        read = true
        for _, o in ipairs(list) do
          if type(o) == "table" and Str(o.text) then texts[#texts + 1] = o.text end
        end
      end
    end
    if not read and index and type(_G.GetNumQuestLeaderBoards) == "function" and type(_G.GetQuestLogLeaderBoard) == "function" then
      local ok, n = pcall(_G.GetNumQuestLeaderBoards, index)
      for j = 1, (ok and Num(n) or 0) do
        local ok2, text = pcall(_G.GetQuestLogLeaderBoard, j, index)
        if ok2 and Str(text) then texts[#texts + 1] = text end
      end
    end
    return texts
  end
  local function quest(title, questID, index)
    log.count = log.count + 1
    AddWords(title, log.words)
    local texts = objectives(questID, index)
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

-- Why a quest item is no longer needed ("done", "none"), and whether we are
-- sure; nil when it may still be needed.
local function QuestVerdict(e, qi, log)
  if REPEATABLE[e.id] then return nil end
  local starts = qi and Num(qi.questID) or nil
  if starts and starts > 0 then
    if e.name and log.needs[slower(e.name)] then return nil end
    if not qi.isActive and QuestDone(starts) then return "done", true end
    return nil
  end
  if not e.name or not log.complete then return nil end
  if NamedByLog(e.name, log) then return nil end
  return "none", log.bare == 0
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
--                 after the cut, or nil), source "vendor"/"auction", unsure, junk }
--   quest         old quest items: the same fields plus why ("done"/"none") and sure
--   merge         { id, name, icon, quality, stacks, frees, from, to } (from/to
--                 are slot entries; nil when no two stacks fit in one)
--   junk          grey items a vendor buys (what "Sell junk" sells)
--   pending       some items were not loaded yet (the scan reruns when they are)
--   questPartial  collapsed quest log headers hid some quests
-- }
function ns.BagScan()
  local keep = KeepList()
  local level = PlayerLevel()
  local log = ReadQuestLog()
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
          -- A quest item the auction house trades (a turn-in people buy)
          -- is valued like any other item.
          if isQuest and not bound and HasAuctionData(PriceID(id, link)) then isQuest = false end
          if locked or keep[id] or PROTECT[id] then
            -- moving, kept or a tool: leave it alone
          elseif isQuest then
            local why, sure = QuestVerdict(e, qi, log)
            if why then
              e.why, e.sure = why, sure
              out.quest[#out.quest + 1] = e
            end
          else
            local vendor = (gi and gi.vendor) or DataVendor(id)
            if info.hasNoValue == true then vendor = 0 end
            local aEach, unsure
            if not bound and bind ~= BIND_PICKUP then
              local unit
              unit, unsure = AuctionUnit(PriceID(id, link), cache)
              if unit then aEach = floor(unit * cut) end
            end
            local vTotal, aTotal = vendor * count, (aEach or 0) * count
            e.vendor, e.auction, e.unsure = vendor, aEach, unsure and aTotal > vTotal or false
            e.value = max(vTotal, aTotal)
            e.source = (aTotal > vTotal) and "auction" or "vendor"
            e.junk = e.quality == 0 and vendor > 0
            if e.junk then out.junk[#out.junk + 1] = e end
            local skip = e.value <= 0
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
    if a.why ~= b.why then return a.why == "done" end
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
  return out
end

---------------------------------------------------------------------------
-- Acting on a row: pick up, sell, merge (always from a click)
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

local BP = { rows = {}, built = false, merchant = false, hinted = false }
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
-- Other bag addons (they hide the game's bags) get /fah bags in chat.
---------------------------------------------------------------------------
-- ElvUI's bags: one window, ElvUI_ContainerFrame. Its slots sit in
-- frame.Bags[bagID][slotID].
local ADDON_BAG_FRAMES = { "ElvUI_ContainerFrame" }

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

-- Open bag windows that are on screen. A window with no position (bag
-- addons park the game's bags that way) does not count.
local function OpenBagFrames()
  local out = {}
  for _, f in ipairs(BagFrames()) do
    local ok, vis = pcall(f.IsVisible, f)
    if ok and vis then
      local okL, l = pcall(f.GetLeft, f)
      if okL and Num(l) then out[#out + 1] = f end
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
  done = "starts a quest you already finished",
  none = "no quest in your log asks for it",
}

local function RowTooltip(row)
  local e, kind = row.entry, row.kind
  local tt = _G.GameTooltip
  if not e or not tt then return end
  tt:SetOwner(row, "ANCHOR_LEFT")
  local slot = kind == "merge" and e.from or e
  local shown = false
  if slot and type(tt.SetBagItem) == "function" then shown = pcall(tt.SetBagItem, tt, slot.bag, slot.slot) end
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
    tt:AddLine(Paint("Old quest item", "buy"))
    tt:AddLine("This " .. (QUEST_TEXT[e.why] or "may be left over") .. ".", 0.8, 0.8, 0.8, true)
    if not e.sure then
      tt:AddLine("Some quests (deliver this, talk to someone) do not list what they need: check your quest log first.",
        0.6, 0.6, 0.6, true)
    end
    tt:AddLine("Click: pick it up, then click the ground to destroy it (the game asks first)", mu[1], mu[2], mu[3], true)
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
  BP:Glow(nil)
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
        Print("will not suggest " .. name .. " again. /fah keep " .. name .. " undoes it.")
        BP:Refresh()
      end)
      root:CreateButton("Pick it up", function() if StillThere(e) then Pickup(e) end end)
      if AtMerchant(BP) and (e.vendor or 0) > 0 then
        root:CreateButton("Sell to this vendor", function() if StillThere(e) then Sell(e) end end)
      end
    end)
    return
  end
  -- No context menus in this client: right-click keeps the item.
  ns.ToggleBagKeep(e.id)
  Print("will not suggest " .. name .. " again. /fah keep " .. name .. " undoes it.")
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
  else
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
    "Every stack in your bags, cheapest first: the better of what a vendor pays and what it sells for at auction now (after the 5% cut). Drop or sell from the top to lose the least. Your hearthstone, tools, reagents, ammo, keys, and food or potions near your level are never listed.")
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
      .. (open and "close" or "open") .. " the list. /fah bags prints it in chat."
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

-- ElvUI's bag window: in its header, with the panel beside the window's top.
-- The game's bags: to the left of the leftmost open bag, level with the lowest.
function BP:Place()
  if not self.built then return false end
  local UI = _G.UIParent
  local uiScale = (UI.GetEffectiveScale and UI:GetEffectiveScale()) or 1
  local frames = OpenBagFrames()
  local left, bottom, strata, leftFrame, leftBottom
  for _, f in ipairs(frames) do
    local l, b = f:GetLeft(), f:GetBottom()
    if Num(l) and Num(b) then
      local s = ((f.GetEffectiveScale and f:GetEffectiveScale()) or 1) / uiScale
      l, b = l * s, b * s
      if not left or l < left then
        left, leftFrame, leftBottom = l, f, b
        strata = f.GetFrameStrata and f:GetFrameStrata() or nil
      end
      if not bottom or b < bottom then bottom = b end
    end
  end
  if not left then return false end
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
      if leftBottom <= bottom + 0.5 then
        -- One bag window (the combined bags) or the leftmost is also the
        -- lowest: hang off it, so the panel follows when it moves or resizes.
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
  F:SetHeight(-y + PAD)
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
  if self.frame:IsShown() then self:Layout(scan) end
end

local function PanelWanted()
  local st = S()
  return st ~= nil and st.bagPanel == true
end

-- Show the tab or the panel while bags are open; hide both when they close.
function BP:Update()
  if #OpenBagFrames() == 0 then
    if self.built then
      self.frame:Hide()
      self.tab:Hide()
      self.glow:Hide()
    end
    return
  end
  if not self:Build() then return end
  self:Place()
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
  Print("the bag cleanup hit a problem (" .. tostring(err) .. "). /fah bags still lists what to clear.")
end

local updateToken, refreshToken = 0, 0
function BP:UpdateSoon()
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
  BP:Update()
end

local hooked = setmetatable({}, { __mode = "k" })
local function HookBagFrames()
  for _, f in ipairs(BagFrames()) do
    if not hooked[f] and type(f.HookScript) == "function" then
      local ok = pcall(f.HookScript, f, "OnShow", function() BP:UpdateSoon() end)
      local ok2 = pcall(f.HookScript, f, "OnHide", function() BP:UpdateSoon() end)
      pcall(f.HookScript, f, "OnDragStop", function() if BP:Visible() then BP:UpdateSoon() end end)
      hooked[f] = ok and ok2
    end
  end
  if type(_G.hooksecurefunc) ~= "function" then return end
  if not BP.hookedAnchors and type(_G.UpdateContainerFrameAnchors) == "function" then
    BP.hookedAnchors = pcall(_G.hooksecurefunc, "UpdateContainerFrameAnchors", function()
      if BP:Visible() then BP:Place() end
    end)
  end
  -- Opening or closing bags by key or button: catches a bag window that
  -- was made after we looked (ElvUI builds its own once you log in).
  if not BP.hookedToggles then
    BP.hookedToggles = true
    for _, fn in ipairs({ "ToggleBackpack", "ToggleAllBags", "OpenAllBags", "CloseAllBags", "OpenBackpack", "CloseBackpack" }) do
      if type(_G[fn]) == "function" then
        pcall(_G.hooksecurefunc, fn, function()
          BP.HookBagFrames()
          BP:UpdateSoon()
        end)
      end
    end
  end
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

local handlers = {
  PLAYER_LOGIN = function() HookBagFrames() BP:UpdateSoon() end,
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
-- Slash commands: /fah bags, /fah keep
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
      return ItemText(e) .. " (" .. (e.why == "done" and "quest done" or (e.sure and "no quest needs it" or "check your quest log")) .. ")"
    end))
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
      Print("you keep nothing yet. /fah keep Item Name (or shift-click it) stops the bag cleanup suggesting it.")
    else
      Print("never suggested in bag cleanup: " .. tconcat(names, ", "))
    end
    return
  end
  local id = tonumber(smatch(rest, "item:(%d+)") or smatch(rest, "^%d+$") or "")
  if not id then
    -- An item in your bags, by name.
    local want = slower(rest)
    for _, bag in ipairs(BagIDs()) do
      for slot = 1, NumSlots(bag) do
        local info = SlotInfo(bag, slot)
        local iid = info and Num(info.itemID)
        local gi = iid and ItemInfo(iid)
        if gi and gi.name and slower(gi.name) == want then id = iid break end
      end
      if id then break end
    end
  end
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

local baseSlash = ns.HandleSlash
function ns.HandleSlash(msg)
  local m = type(msg) == "string" and msg:gsub("^%s+", ""):gsub("%s+$", "") or ""
  local cmd, rest = m:match("^(%S*)%s*(.-)$")
  cmd = (cmd or ""):lower()
  rest = rest or ""
  if cmd == "bags" or cmd == "bag" or cmd == "cleanup" or cmd == "clean" then
    Report()
    return
  elseif cmd == "keep" then
    KeepCommand(rest)
    return
  end
  if type(baseSlash) == "function" then baseSlash(msg) end
  if cmd == "help" or cmd == "?" then
    Print("/fah bags : what to drop or sell first, old quest items and stacks to merge (also beside your open bags)")
    Print("/fah keep Item Name : never suggest dropping an item (again to undo; no name lists them)")
  end
end
