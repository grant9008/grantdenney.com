local ADDON_NAME, ns = ...
-- Forever AH: core. Price data access, money text, settings, slash commands
-- and the item tooltip lines.
--
-- Price data comes from Data.lua (ForeverAH_Data), which the Windows updater
-- refreshes. Each item is one packed string. Nothing is unpacked at login:
-- an item is read the first time something asks for it, then cached.

ADDON_NAME = ADDON_NAME or "ForeverAH"
ns = ns or {}

local _G = _G
local type, tonumber, tostring, pairs, ipairs, pcall, select = type, tonumber, tostring, pairs, ipairs, pcall, select
local floor, max, min, abs = math.floor, math.max, math.min, math.abs
local sfind, ssub, sbyte, slower, sformat, smatch, sgsub = string.find, string.sub, string.byte, string.lower, string.format, string.match, string.gsub
local tconcat, tinsert = table.concat, table.insert

---------------------------------------------------------------------------
-- Data
---------------------------------------------------------------------------
local db = _G.ForeverAH_Data
if type(db) ~= "table" then
  db = { v = 1, updated = 0, items = {} }
  _G.ForeverAH_Data = db
end
if type(db.items) ~= "table" then db.items = {} end
db.updated = tonumber(db.updated) or 0
ns.db = db

local function isSecret(v)
  local f = _G.issecretvalue
  if type(f) ~= "function" then return false end
  local ok, r = pcall(f, v)
  return ok and r and true or false
end
ns.IsSecret = isSecret

-- How many of the last 24 hourly snapshots have a measured sales estimate.
-- Older data files do not say: they count as a full day.
function ns.SalesHours()
  local h = tonumber(db.salesHours)
  if not h then return 24 end
  h = floor(h)
  if h < 0 then return 0 end
  if h > 24 then return 24 end
  return h
end

-- Estimated units sold per day, scaled up to a full day when sales have only
-- been measured for part of it. Returns nil while nothing has been measured
-- yet (a first snapshot, or the first one after a gap), and a second value
-- that is true when the number is an early estimate (or still measuring).
function ns.SoldPerDay(item)
  if type(item) ~= "table" then return 0, false end
  local sold = tonumber(item.sold24) or 0
  local h = ns.SalesHours()
  if h <= 0 then return nil, true end
  if h >= 24 then return sold, false end
  return floor(sold * 24 / h + 0.5), true
end

-- "~120 sold/day", "~120 sold/day (early estimate)" or "sales: measuring".
function ns.SoldText(item)
  local per, early = ns.SoldPerDay(item)
  if per == nil then return "sales: measuring" end
  return "~" .. ns.Num(per) .. " sold/day" .. (early and " (early estimate)" or "")
end

-- Unknown item names: "" (new data files) or "Item 12345" (older ones).
local function unknownName(nm)
  return type(nm) ~= "string" or nm == "" or smatch(nm, "^Item %d+$") ~= nil
end
ns.IsUnknownName = unknownName

-- The game's own name for an item, or nil when it has not loaded it (asking
-- also makes the game load it, so a later call usually has it).
local function gameName(id)
  local function good(ok, n)
    return ok and type(n) == "string" and not isSecret(n) and not unknownName(n)
  end
  local CI = _G.C_Item
  if type(CI) == "table" then
    if type(CI.GetItemNameByID) == "function" then
      local ok, n = pcall(CI.GetItemNameByID, id)
      if good(ok, n) then return n end
    end
    if type(CI.GetItemInfo) == "function" then
      local ok, n = pcall(CI.GetItemInfo, id)
      if good(ok, n) then return n end
    end
  end
  if type(_G.GetItemInfo) == "function" then
    local ok, n = pcall(_G.GetItemInfo, id)
    if good(ok, n) then return n end
  end
  return nil
end
ns.GameItemName = gameName

ns.STALE_SECONDS = 6 * 3600
ns.AH_CUT = 0.95
ns.stats = { items = 0, series = 0 }   -- how many items/series were unpacked (for tests)

-- PocketGE palette: buy = gold, sell = blue.
ns.COLORS = {
  buy    = { 0.898, 0.722, 0.259 },
  sell   = { 0.22, 0.74, 0.97 },
  pos    = { 0.25, 0.80, 0.45 },
  neg    = { 0.90, 0.33, 0.33 },
  muted  = { 0.55, 0.51, 0.45 },
  text   = { 0.90, 0.88, 0.84 },
  bg     = { 0.043, 0.055, 0.078, 0.96 },
  panel  = { 0.07, 0.08, 0.10, 0.96 },
}

-- "ff38bdf7" style hex for |c colour codes.
function ns.Hex(c)
  local function h(v) return sformat("%02x", floor(max(0, min(1, v or 0)) * 255 + 0.5)) end
  return "ff" .. h(c[1]) .. h(c[2]) .. h(c[3])
end

function ns.Colorize(text, c)
  return "|c" .. ns.Hex(c) .. tostring(text) .. "|r"
end

-- Split on a single plain character, keeping empty fields.
local function split(s, sep)
  local out, n, start = {}, 0, 1
  while true do
    local i = sfind(s, sep, start, true)
    n = n + 1
    if not i then
      out[n] = ssub(s, start)
      return out, n
    end
    out[n] = ssub(s, start, i - 1)
    start = i + 1
  end
end

local function trim(s)
  return (sgsub(s or "", "^%s+", ""):gsub("%s+$", ""))
end

local function hourEnd()
  local t = tonumber(db.hourEnd)
  if t then return t end
  local u = tonumber(db.updated) or 0
  return u - u % 3600
end

local function dayEnd()
  local t = tonumber(db.dayEnd)
  if t then return t end
  local u = tonumber(db.updated) or 0
  return u - u % 86400
end

local function num10(s)
  return tonumber(s) or 0
end

-- Item summary ------------------------------------------------------------
local itemCache = {}   -- id -> item table, or false when the id is not in the data

-- Items the data has no name for get the game's name the first time
-- something reads item.name (and keep it once the game has loaded it).
-- Until then item.name is "".
local unnamedMeta = { __index = function(t, k)
  if k ~= "name" then return nil end
  local n = gameName(rawget(t, "id"))
  if n then
    rawset(t, "name", n)
    return n
  end
  return ""
end }

function ns.GetItem(itemID)
  local id = tonumber(itemID)
  if not id then return nil end
  local c = itemCache[id]
  if c ~= nil then return c or nil end
  local raw = db.items[id]
  if type(raw) ~= "string" then
    itemCache[id] = false
    return nil
  end
  local f = split(raw, "|")
  local item = {
    id       = id,
    cheapest = num10(f[2]),
    market   = num10(f[3]),
    listed   = num10(f[4]),
    sold24   = num10(f[5]),
    avg7     = num10(f[6]),
    vendor   = num10(f[7]),
    -- How many separate listings make up today's prices (nil in older files).
    auctions = tonumber(f[10]),
  }
  local nm = f[1] or ""
  if unknownName(nm) then
    item.unnamed = true
    setmetatable(item, unnamedMeta)
  else
    item.name = nm
  end
  itemCache[id] = item
  ns.stats.items = ns.stats.items + 1
  return item
end

-- The name to show for an item: the data's, else the game's, else "Item 123".
function ns.ItemName(itemID)
  local id = tonumber(itemID)
  if not id then return "" end
  local it = ns.GetItem(id)
  local nm = it and it.name
  if not unknownName(nm) then return nm end
  return gameName(id) or ("Item " .. id)
end

-- Series ------------------------------------------------------------------
-- Timeframes. key H = hourly grid ending at hourEnd, D = daily grid ending at dayEnd.
local TF = {
  ["1D"] = { key = "H", n = 24,  step = 3600 },
  ["5D"] = { key = "H", n = 120, step = 3600 },
  ["1W"] = { key = "H", n = 168, step = 3600 },
  ["1M"] = { key = "D", n = 30,  step = 86400 },
  ["1Y"] = { key = "D", n = 365, step = 86400 },
}
ns.TIMEFRAMES = TF
ns.TF_ORDER = { "1D", "5D", "1W", "1M", "1Y" }

function ns.NormalizeTF(tf)
  if type(tf) ~= "string" then return nil end
  local u = string.upper(trim(tf))
  if TF[u] then return u end
  return nil
end

-- Number of comma-separated tokens. An empty sub-series has no tokens.
local function countTokens(s)
  if s == "" then return 0 end
  local n, pos = 1, 1
  while true do
    local i = sfind(s, ",", pos, true)
    if not i then return n end
    n = n + 1
    pos = i + 1
  end
end

-- Base 36 to a number. WoW's tonumber(s, 36) reads through a 32-bit unsigned
-- long on Windows and stops at 4294967295, so long tokens are read 6 digits at
-- a time (36^6 is under 2^32). Copper amounts stay exact well past the gold cap.
local function b36(tok)
  local n = #tok
  if n <= 6 then return tonumber(tok, 36) end
  local v = 0
  for i = 1, n, 6 do
    local chunk = ssub(tok, i, i + 5)
    local c = tonumber(chunk, 36)
    if not c then return nil end
    v = v * 36 ^ #chunk + c
  end
  return v
end
ns.B36 = b36

-- Base-36 price deltas, right-aligned into out[1..n]. "~" = nothing listed.
-- Values of 0 copper or less are not real prices and are stored as false too.
local function decodePrices(s, cnt, n, out)
  local pad = n - cnt
  for i = 1, pad do out[i] = false end
  local prev
  local pos = 1
  for i = 1, cnt do
    local comma = sfind(s, ",", pos, true)
    local tok
    if comma then
      tok = ssub(s, pos, comma - 1)
      pos = comma + 1
    else
      tok = ssub(s, pos)
      pos = #s + 1
    end
    if tok == "~" then
      out[pad + i] = false
    else
      local v = 0
      if tok ~= "" then
        local neg = sbyte(tok, 1) == 45   -- "-"
        if neg then tok = ssub(tok, 2) end
        v = b36(tok) or 0
        if neg then v = -v end
      end
      if prev then v = prev + v end
      prev = v
      out[pad + i] = (v > 0) and v or false
    end
  end
end

-- Base-36 absolute sold counts, right-aligned. "" = 0.
local function decodeSold(s, cnt, n, out)
  local pad = n - cnt
  for i = 1, pad do out[i] = 0 end
  local pos = 1
  for i = 1, cnt do
    local comma = sfind(s, ",", pos, true)
    local tok
    if comma then
      tok = ssub(s, pos, comma - 1)
      pos = comma + 1
    else
      tok = ssub(s, pos)
      pos = #s + 1
    end
    out[pad + i] = (tok ~= "" and b36(tok)) or 0
  end
end

local fullCache = { H = {}, D = {} }

local function decodeFull(id, key)
  local cache = fullCache[key]
  local c = cache[id]
  if c then return c end
  local raw = db.items[id]
  if type(raw) ~= "string" then return nil end
  local f = split(raw, "|")
  local s = f[key == "H" and 8 or 9] or ""
  local parts = split(s, ";")
  local cs, ms, ss = parts[1] or "", parts[2] or "", parts[3] or ""
  local cc, mc, sc = countTokens(cs), countTokens(ms), countTokens(ss)
  local n = max(cc, mc, sc)
  local out = { n = n, cheap = {}, market = {}, sold = {} }
  decodePrices(cs, cc, n, out.cheap)
  decodePrices(ms, mc, n, out.market)
  decodeSold(ss, sc, n, out.sold)
  cache[id] = out
  ns.stats.series = ns.stats.series + 1
  return out
end

local windowCache = {}

-- Returns {n, step, tf, times, cheap, market, sold} for the last points of the
-- timeframe. An item with less history than the timeframe gets fewer points
-- (n is smaller), never padding. Prices with nothing listed are false.
function ns.GetSeries(itemID, tf)
  local id = tonumber(itemID)
  if not id or not ns.GetItem(id) then return nil end
  local key = ns.NormalizeTF(tf) or "1D"
  local ck = id .. ":" .. key
  local w = windowCache[ck]
  if w then return w end
  local spec = TF[key]
  local full = decodeFull(id, spec.key)
  if not full then return nil end
  local m = min(spec.n, full.n)
  local endT = (spec.key == "H") and hourEnd() or dayEnd()
  local first = full.n - m
  w = { n = m, step = spec.step, tf = key, times = {}, cheap = {}, market = {}, sold = {} }
  local times, cheap, market, sold = w.times, w.cheap, w.market, w.sold
  local fc, fm, fs = full.cheap, full.market, full.sold
  for i = 1, m do
    times[i] = endT - (m - i) * spec.step
    cheap[i] = fc[first + i]
    market[i] = fm[first + i]
    sold[i] = fs[first + i]
  end
  windowCache[ck] = w
  return w
end

-- How many days the item's daily prices cover (read without unpacking them).
local dayCount = {}
function ns.DailyPoints(itemID)
  local id = tonumber(itemID)
  if not id then return 0 end
  local c = dayCount[id]
  if c then return c end
  c = 0
  local raw = db.items[id]
  if type(raw) == "string" then
    local f = split(raw, "|")
    local parts = split(f[9] or "", ";")
    c = max(countTokens(parts[1] or ""), countTokens(parts[2] or ""), countTokens(parts[3] or ""))
  end
  dayCount[id] = c
  return c
end

-- Name lookup -------------------------------------------------------------
-- Lower-case name -> list of item ids, lowest first (a few names belong to
-- more than one item, like Squire's Shirt). Built on the first lookup, not at
-- login. Items the data has no name for are looked up by the game's name,
-- only when a lookup finds nothing, and only for the ones not found yet.
local nameIndex, unnamedIDs

local function addName(nm, id)
  -- Searches are trimmed, so the names are too (a few real names end in a space).
  local key = slower(trim(nm))
  if key == "" then return end
  local list = nameIndex[key]
  if not list then
    nameIndex[key] = { id }
    return
  end
  for i = 1, #list do
    if list[i] == id then return end
    if id < list[i] then
      tinsert(list, i, id)
      return
    end
  end
  list[#list + 1] = id
end

local function buildNameIndex()
  nameIndex, unnamedIDs = {}, {}
  for id, raw in pairs(db.items) do
    local nid = tonumber(id)
    if nid and type(raw) == "string" then
      local bar = sfind(raw, "|", 1, true)
      local nm = bar and ssub(raw, 1, bar - 1) or raw
      if unknownName(nm) then
        unnamedIDs[#unnamedIDs + 1] = nid
      else
        addName(nm, nid)
      end
    end
  end
end

local function indexGameNames()
  if #unnamedIDs == 0 then return end
  local still = {}
  for _, id in ipairs(unnamedIDs) do
    local n = gameName(id)
    if n then addName(n, id) else still[#still + 1] = id end
  end
  unnamedIDs = still
end

local function lookupName(name)
  if type(name) ~= "string" or isSecret(name) then return nil end
  local key = slower(trim(name))
  if key == "" then return nil end
  if not nameIndex then buildNameIndex() end
  local list = nameIndex[key]
  if not list then
    indexGameNames()
    list = nameIndex[key]
  end
  return list
end

-- The item id for a name (the lowest one when several items share it).
function ns.FindItemID(name)
  local list = lookupName(name)
  return list and list[1] or nil
end

-- Every item id with this name, lowest first (an empty list when none).
function ns.FindItemIDs(name)
  local list = lookupName(name)
  local out = {}
  if list then
    for i = 1, #list do out[i] = list[i] end
  end
  return out
end

local itemCount
function ns.ItemCount()
  if not itemCount then
    itemCount = 0
    for _ in pairs(db.items) do itemCount = itemCount + 1 end
  end
  return itemCount
end

-- Accepts an item link, an item id or an item name. Returns an id we have data for.
function ns.ResolveItem(text)
  if type(text) == "number" then return ns.GetItem(text) and text or nil end
  if type(text) ~= "string" then return nil end
  local linked = smatch(text, "item:(%d+)")
  if linked then
    local id = tonumber(linked)
    return ns.GetItem(id) and id or nil
  end
  local t = trim(text)
  if smatch(t, "^%d+$") then
    local id = tonumber(t)
    return ns.GetItem(id) and id or nil
  end
  t = sgsub(t, "^%[", "")
  t = sgsub(t, "%]$", "")
  -- "Item 7192" is what the chart and the watchlist show for an item with no
  -- name yet, so typing it back works the same as typing the number.
  local placeholder = smatch(t, "^[Ii]tem (%d+)$")
  if placeholder then
    local id = tonumber(placeholder)
    if ns.GetItem(id) then return id end
  end
  return ns.FindItemID(t)
end

---------------------------------------------------------------------------
-- Money and number text
---------------------------------------------------------------------------
local function splitMoney(copper)
  copper = tonumber(copper) or 0
  local neg = copper < 0
  local v = floor(abs(copper) + 0.5)
  local g = floor(v / 10000)
  local s = floor((v % 10000) / 100)
  local c = v % 100
  if v > 1000000 then c = 0 end   -- above 100g the copper is noise
  return neg, g, s, c
end

-- "12g 34s 5c". Zero parts are left out, zero is "0c".
function ns.MoneyText(copper)
  local neg, g, s, c = splitMoney(copper)
  local parts = {}
  if g > 0 then parts[#parts + 1] = g .. "g" end
  if s > 0 then parts[#parts + 1] = s .. "s" end
  if c > 0 then parts[#parts + 1] = c .. "c" end
  if #parts == 0 then return "0c" end
  local out = tconcat(parts, " ")
  if neg then out = "-" .. out end
  return out
end

-- Same as MoneyText but with coin icons when the game offers them.
function ns.MoneyIcons(copper)
  local neg, g, s, c = splitMoney(copper)
  local fn = _G.GetCoinTextureString
  if type(fn) ~= "function" then
    local ci = _G.C_CurrencyInfo
    fn = type(ci) == "table" and ci.GetCoinTextureString or nil
  end
  if type(fn) ~= "function" then return ns.MoneyText(copper) end
  local amount = g * 10000 + s * 100 + c
  local ok, txt = pcall(fn, amount)
  if not ok or type(txt) ~= "string" or txt == "" then return ns.MoneyText(copper) end
  if neg and amount > 0 then txt = "-" .. txt end
  return txt
end

-- 1234567 -> "1,234,567"
function ns.Num(n)
  n = floor((tonumber(n) or 0) + 0.5)
  local s = tostring(abs(n))
  local out = s:reverse():gsub("(%d%d%d)", "%1,"):reverse()
  if ssub(out, 1, 1) == "," then out = ssub(out, 2) end
  return (n < 0 and "-" or "") .. out
end

---------------------------------------------------------------------------
-- Data age
---------------------------------------------------------------------------
local function now()
  local gst = _G.GetServerTime
  if type(gst) == "function" then
    local ok, t = pcall(gst)
    if ok and type(t) == "number" and not isSecret(t) and t > 0 then return t end
  end
  local tm = _G.time
  if type(tm) == "function" then
    local ok, t = pcall(tm)
    if ok and type(t) == "number" then return t end
  end
  return 0
end
ns.Now = now

function ns.DataAge()
  local u = tonumber(db.updated) or 0
  if u <= 0 then return nil end
  local a = now() - u
  if a < 0 then a = 0 end
  return a
end

-- "just now", "25m ago", "2h ago", "3d ago"
function ns.AgeText(sec)
  if type(sec) ~= "number" then return "no data" end
  if sec < 60 then return "just now" end
  if sec < 3600 then return floor(sec / 60) .. "m ago" end
  if sec < 48 * 3600 then return floor(sec / 3600) .. "h ago" end
  return floor(sec / 86400) .. "d ago"
end

---------------------------------------------------------------------------
-- Chat output
---------------------------------------------------------------------------
function ns.Print(msg)
  local line = "|c" .. ns.Hex(ns.COLORS.sell) .. "Forever AH:|r " .. tostring(msg)
  local cf = _G.DEFAULT_CHAT_FRAME
  if type(cf) == "table" and type(cf.AddMessage) == "function" then
    cf:AddMessage(line)
  elseif type(_G.print) == "function" then
    _G.print(line)
  end
end
local P = ns.Print

---------------------------------------------------------------------------
-- Settings
---------------------------------------------------------------------------
local DEFAULTS = { tooltip = true, chart = true, chartTF = "1D", compact = false, chartPoint = nil }
ns.DEFAULTS = DEFAULTS

-- ns.settings is one stable table. Before ADDON_LOADED it holds the defaults;
-- on ADDON_LOADED the saved values are merged in and ForeverAH_Settings points at it.
ns.settings = {}
for k, v in pairs(DEFAULTS) do ns.settings[k] = v end
ns.settingsLoaded = false

local function loadSettings()
  local S = ns.settings
  local saved = _G.ForeverAH_Settings
  if type(saved) == "table" and saved ~= S then
    for k, v in pairs(saved) do S[k] = v end
  end
  for k, v in pairs(DEFAULTS) do
    if S[k] == nil or type(S[k]) ~= type(v) then S[k] = v end
  end
  S.chartTF = ns.NormalizeTF(S.chartTF) or "1D"
  _G.ForeverAH_Settings = S
  ns.settingsLoaded = true
end
ns.LoadSettings = loadSettings

---------------------------------------------------------------------------
-- Tooltip
---------------------------------------------------------------------------
local TOOLTIP_NAMES = {
  GameTooltip = true,
  ItemRefTooltip = true,
  ShoppingTooltip1 = true,
  ShoppingTooltip2 = true,
  ItemRefShoppingTooltip1 = true,
  ItemRefShoppingTooltip2 = true,
}
ns.TOOLTIP_NAMES = TOOLTIP_NAMES

local done = setmetatable({}, { __mode = "k" })      -- tooltip -> item id already shown
local clearHooked = setmetatable({}, { __mode = "k" }) -- tooltip -> true when OnTooltipCleared is hooked

local function allowedTooltip(tt)
  if type(tt) ~= "table" then return false end
  if type(tt.IsForbidden) == "function" then
    local ok, forbidden = pcall(tt.IsForbidden, tt)
    if not ok or forbidden then return false end
  end
  if type(tt.GetName) ~= "function" then return false end
  local ok, name = pcall(tt.GetName, tt)
  return ok and type(name) == "string" and TOOLTIP_NAMES[name] == true
end

local function ensureClearHook(tt)
  if clearHooked[tt] ~= nil then return clearHooked[tt] end
  local hooked = false
  if type(tt.HookScript) == "function" then
    hooked = pcall(tt.HookScript, tt, "OnTooltipCleared", function(self) done[self] = nil end)
  end
  clearHooked[tt] = hooked and true or false
  return clearHooked[tt]
end

local function shiftDown()
  local f = _G.IsShiftKeyDown
  if type(f) ~= "function" then return false end
  local ok, r = pcall(f)
  return ok and r and true or false
end

local function tooltipItemID(tt, data)
  local id = type(data) == "table" and data.id or nil
  if id ~= nil and isSecret(id) then return nil end
  if type(id) == "number" then return id end
  if type(tt.GetItem) == "function" then
    local ok, _, link = pcall(tt.GetItem, tt)
    if ok and type(link) == "string" and not isSecret(link) then
      local lid = smatch(link, "item:(%d+)")
      if lid then return tonumber(lid) end
    end
  end
  return nil
end

-- Stack size of the item under the mouse. Only bag and equipped items have
-- one we can read; everything else counts as 1.
local function stackCount(tt, data)
  local info = tt.info or tt.processingInfo
  local count
  if type(info) == "table" and type(info.getterArgs) == "table" then
    local args = info.getterArgs
    if info.getterName == "GetBagItem" then
      local cc = _G.C_Container
      if type(cc) == "table" and type(cc.GetContainerItemInfo) == "function" then
        local ok, ci = pcall(cc.GetContainerItemInfo, args[1], args[2])
        if ok and type(ci) == "table" then count = ci.stackCount end
      end
    elseif info.getterName == "GetInventoryItem" and type(_G.GetInventoryItemCount) == "function" then
      local ok, n = pcall(_G.GetInventoryItemCount, args[1], args[2])
      if ok then count = n end
    end
  end
  if count == nil and type(data) == "table" and data.guid ~= nil then
    local ci = _G.C_Item
    if type(ci) == "table" and type(ci.GetItemLocation) == "function" and type(ci.GetStackCount) == "function" then
      local ok, loc = pcall(ci.GetItemLocation, data.guid)
      if ok and type(loc) == "table" and type(loc.IsBagAndSlot) == "function" then
        local ok2, isBag = pcall(loc.IsBagAndSlot, loc)
        if ok2 and isBag then
          local ok3, n = pcall(ci.GetStackCount, loc)
          if ok3 then count = n end
        end
      end
    end
  end
  if count == nil or isSecret(count) then return 1 end
  count = tonumber(count)
  if not count or count < 1 then return 1 end
  return floor(count)
end

-- True when an item link carries a random suffix ("of the Monkey"). Those
-- versions share one item ID, and the price data cannot tell them apart.
-- Link fields after "item:" are itemID, enchant, 4 gems, then the suffix ID.
function ns.LinkHasSuffix(link)
  if type(link) ~= "string" or isSecret(link) then return false end
  local body = smatch(link, "item:([%-%d:]+)")
  if not body then return false end
  local i = 0
  for field in string.gmatch(body .. ":", "([^:]*):") do
    i = i + 1
    if i == 7 then return field ~= "" and field ~= "0" end
  end
  return false
end

-- The bag or equipped location of the tooltip's item, when it has one.
local function tooltipLocation(tt, data)
  local CI = _G.C_Item
  if type(data) == "table" and data.guid ~= nil and type(CI) == "table" and type(CI.GetItemLocation) == "function" then
    local ok, loc = pcall(CI.GetItemLocation, data.guid)
    if ok and type(loc) == "table" then return loc end
  end
  local info = tt.info or tt.processingInfo
  if type(info) == "table" and info.getterName == "GetBagItem" and type(info.getterArgs) == "table" then
    local IL = _G.ItemLocation
    if type(IL) == "table" and type(IL.CreateFromBagAndSlot) == "function" then
      local ok, loc = pcall(IL.CreateFromBagAndSlot, IL, info.getterArgs[1], info.getterArgs[2])
      if ok and type(loc) == "table" then return loc end
    end
  end
  return nil
end

-- False for items that can never be on the auction house: soulbound, bind on
-- pickup and quest items. True when it might be, or when we cannot tell.
local function mightBeTradeable(tt, data, id)
  local CI = _G.C_Item
  if type(CI) ~= "table" then return true end
  if type(CI.GetItemInfo) == "function" then
    local r = { pcall(CI.GetItemInfo, id) }
    local bind = r[1] and r[15] or nil   -- bindType is the 14th return value
    if bind ~= nil and not isSecret(bind) and (bind == 1 or bind == 4) then return false end
  end
  if type(CI.IsBound) == "function" then
    local loc = tooltipLocation(tt, data)
    if loc then
      local ok, bound = pcall(CI.IsBound, loc)
      if ok and bound == true then return false end
    end
  end
  return true
end
ns.MightBeTradeable = mightBeTradeable

-- Adds our lines. Returns true when anything was added.
-- opts (optional): noData = false hides the "no recent listings" line,
-- suffix = true adds a note that the price covers every version of the item.
function ns.AddTooltipLines(tt, itemID, count, opts)
  local S = ns.settings
  if not S.tooltip then return false end
  if (tonumber(db.updated) or 0) <= 0 then return false end
  local C = ns.COLORS
  local mu, tx, se = C.muted, C.text, C.sell
  local item = ns.GetItem(itemID)
  if not item then
    if type(opts) == "table" and opts.noData == false then return false end
    tt:AddLine("Forever AH: no recent listings", mu[1], mu[2], mu[3])
    return true
  end
  count = tonumber(count) or 1
  local mult, tag = 1, ""
  if count > 1 and shiftDown() then
    mult = count
    tag = " (x" .. count .. ")"
  end
  local function money(v)
    if not v or v <= 0 then return nil end
    return ns.MoneyIcons(v * mult)
  end
  local age = ns.DataAge() or 0
  if S.compact then
    local m, c = money(item.market), money(item.cheapest)
    local line
    if m and c then
      line = "AH" .. tag .. ": " .. m .. " (cheapest " .. c .. ")"
    elseif m then
      line = "AH" .. tag .. ": " .. m .. " (none listed now)"
    elseif c then
      line = "AH" .. tag .. ": cheapest " .. c
    else
      line = "AH: none listed now"
    end
    tt:AddLine(line, tx[1], tx[2], tx[3])
  else
    tt:AddDoubleLine("Forever AH", ns.AgeText(age), se[1], se[2], se[3], mu[1], mu[2], mu[3])
    local m = money(item.market)
    if m then
      tt:AddDoubleLine("Market" .. tag, m, tx[1], tx[2], tx[3], 1, 1, 1)
    else
      tt:AddDoubleLine("Market", "none listed", tx[1], tx[2], tx[3], mu[1], mu[2], mu[3])
    end
    local c = money(item.cheapest)
    if c then
      tt:AddDoubleLine("Cheapest" .. tag, c, tx[1], tx[2], tx[3], 1, 1, 1)
    else
      tt:AddDoubleLine("Cheapest", "none listed", tx[1], tx[2], tx[3], mu[1], mu[2], mu[3])
    end
    -- On the first day the 7-day average is just today's market price.
    local a = money(item.avg7)
    if a and ns.DailyPoints(item.id) >= 2 then
      tt:AddDoubleLine("7-day avg" .. tag, a, tx[1], tx[2], tx[3], 1, 1, 1)
    end
    tt:AddLine("Listed " .. ns.Num(item.listed) .. ", " .. ns.SoldText(item), mu[1], mu[2], mu[3])
  end
  if type(opts) == "table" and opts.suffix then
    tt:AddLine("Price covers all versions of this item", mu[1], mu[2], mu[3])
  end
  if age > ns.STALE_SECONDS then
    tt:AddLine(sformat("Prices are %dh old: run Update-WoW-Prices.bat, then /reload", floor(age / 3600)), mu[1], mu[2], mu[3])
  end
  return true
end

local reportedError = false
local function reportError(err)
  ns.lastError = err
  if not reportedError then
    reportedError = true
    P("tooltip error (prices hidden on that tooltip): " .. tostring(err))
  end
end

-- TooltipDataProcessor post call (modern client, used on Forever).
local function onTooltipData(tt, data)
  if not allowedTooltip(tt) then return end
  local info = tt.info or tt.processingInfo
  if type(info) == "table" and info.excludeLines then return end
  local guarded = ensureClearHook(tt)
  if guarded and done[tt] then return end
  local id = tooltipItemID(tt, data)
  if not id then return end
  if guarded then done[tt] = id end
  local opts = {}
  if not ns.GetItem(id) then
    -- No data: only worth a line for items that could be on the auction house.
    -- Comparison tooltips show what you wear, which is nearly always soulbound.
    local ok, name = pcall(tt.GetName, tt)
    local shopping = ok and type(name) == "string" and sfind(name, "ShoppingTooltip", 1, true) ~= nil
    opts.noData = not shopping and mightBeTradeable(tt, data, id)
  elseif type(tt.GetItem) == "function" then
    local ok, _, link = pcall(tt.GetItem, tt)
    opts.suffix = ok and ns.LinkHasSuffix(link)
  end
  ns.AddTooltipLines(tt, id, stackCount(tt, data), opts)
end

-- OnTooltipSetItem script hook (fallback for clients without TooltipDataProcessor).
local function onTooltipSetItem(tt)
  if not allowedTooltip(tt) then return end
  local guarded = ensureClearHook(tt)
  if guarded and done[tt] then return end
  local id = tooltipItemID(tt, nil)
  if not id then return end
  if guarded then done[tt] = id end
  local opts = { noData = ns.GetItem(id) ~= nil or mightBeTradeable(tt, nil, id) }
  if ns.AddTooltipLines(tt, id, 1, opts) and type(tt.Show) == "function" then tt:Show() end
end

ns.OnTooltipData = function(tt, data)
  local ok, err = pcall(onTooltipData, tt, data)
  if not ok then reportError(err) end
end
ns.OnTooltipSetItem = function(tt)
  local ok, err = pcall(onTooltipSetItem, tt)
  if not ok then reportError(err) end
end

local processorInstalled = false
local scriptHooked = {}

local function installTooltipHooks()
  if processorInstalled then return end
  local tdp, enum = _G.TooltipDataProcessor, _G.Enum
  if type(tdp) == "table" and type(tdp.AddTooltipPostCall) == "function"
     and type(enum) == "table" and type(enum.TooltipDataType) == "table"
     and enum.TooltipDataType.Item ~= nil then
    local ok = pcall(tdp.AddTooltipPostCall, enum.TooltipDataType.Item, ns.OnTooltipData)
    if ok then
      processorInstalled = true
      ns.tooltipMode = "processor"
      return
    end
  end
  for name in pairs(TOOLTIP_NAMES) do
    local tt = _G[name]
    if type(tt) == "table" and not scriptHooked[name] and type(tt.HookScript) == "function" then
      local has = true
      if type(tt.HasScript) == "function" then
        local ok, r = pcall(tt.HasScript, tt, "OnTooltipSetItem")
        has = ok and r and true or false
      end
      if has and pcall(tt.HookScript, tt, "OnTooltipSetItem", ns.OnTooltipSetItem) then
        scriptHooked[name] = true
        ns.tooltipMode = "script"
      end
    end
  end
end
ns.InstallTooltipHooks = installTooltipHooks

---------------------------------------------------------------------------
-- Slash commands
---------------------------------------------------------------------------
local function playerFaction()
  local f = _G.UnitFactionGroup
  if type(f) ~= "function" then return nil end
  local ok, faction = pcall(f, "player")
  if ok and type(faction) == "string" and not isSecret(faction) then return faction end
  return nil
end

local function isHardcore()
  local gr = _G.C_GameRules
  if type(gr) == "table" and type(gr.IsHardcoreActive) == "function" then
    local ok, r = pcall(gr.IsHardcoreActive)
    return ok and r == true
  end
  return false
end

function ns.HouseLabel()
  return tostring(db.houseName or "Alliance Auction House")
end

-- "Alliance", "Horde" or nil (unknown) for the house the data comes from.
function ns.HouseFaction()
  local h = ns.HouseLabel()
  if sfind(h, "Alliance", 1, true) then return "Alliance" end
  if sfind(h, "Horde", 1, true) then return "Horde" end
  return nil
end

-- Why the bundled prices may not fit the market in front of the player
-- (a note for the chart), or nil when they do. Hardcore realms, the other
-- faction, and a different auction house (the neutral goblin ones) are
-- separate markets with their own prices and fees.
function ns.MarketMismatch()
  local house = ns.HouseLabel()
  if isHardcore() then return "Prices are from the normal realms, not Hardcore" end
  local AHS = ns.AHState
  if type(AHS) == "table" and AHS.open and AHS.otherHouse then
    return "Prices are from the " .. house .. ", not this one"
  end
  local hf, pf = ns.HouseFaction(), playerFaction()
  if hf and pf and pf ~= "Neutral" and pf ~= hf then
    return "Prices are from the " .. house
  end
  return nil
end

local function printNoData()
  P("there is no price data yet.")
  P("Double-click Update-WoW-Prices.bat (it downloads the latest prices from grantdenney.com/wow into this addon), then type /reload here.")
end

local function printStatus()
  local age = ns.DataAge()
  if not age then
    printNoData()
    return
  end
  local region = string.upper(tostring(db.region or "us"))
  local ruleset = tostring(db.ruleset or "PvP")
  local when = ""
  local df = _G.date
  if type(df) == "function" then
    local ok, s = pcall(df, "%b %d, %H:%M", db.updated)
    if ok and type(s) == "string" then when = " (" .. s .. " your time)" end
  end
  P(sformat("prices for %s items from the %s (%s, %s), updated %s%s.",
    ns.Num(ns.ItemCount()), ns.HouseLabel(), region, ruleset, ns.AgeText(age), when))
  if age > ns.STALE_SECONDS then
    P(sformat("Prices are %dh old: run Update-WoW-Prices.bat, then /reload.", floor(age / 3600)))
  end
  local faction = playerFaction()
  if faction and faction ~= "Alliance" then
    P("Note: these prices come from the " .. ns.HouseLabel() .. ", so they may not match the auction house you use.")
  end
  if isHardcore() then
    P("Note: these prices are from the normal realms, not Hardcore.")
  end
  P("Type /fah help for commands.")
end

local function printHelp()
  P("commands:")
  P("/fah : show how fresh the price data is")
  P("/fah chart : show or hide the price chart")
  P("/fah chart Linen Cloth : open the chart for an item (you can also shift-click an item into the chat box)")
  P("/fah chart reset : put the chart back in its normal place after you dragged it")
  P("/fah tooltip : turn the prices in item tooltips on or off")
  P("/fah compact : switch tooltips between full and one-line")
  P("/fah help : show this list")
end

local function chartCommand(rest)
  if rest == "" then
    if type(ns.ToggleChart) == "function" then
      ns.ToggleChart()
    else
      P("the chart is not available.")
    end
    return
  end
  if slower(rest) == "reset" then
    ns.settings.chartPoint, ns.settings.chartDock = nil, nil
    if type(ns.ChartRefreshPlacement) == "function" then ns.ChartRefreshPlacement() end
    P("the chart is back in its normal place.")
    return
  end
  if (tonumber(db.updated) or 0) <= 0 then
    printNoData()
    return
  end
  local id = ns.ResolveItem(rest)
  if not id then
    P("no price data for " .. rest .. ". Check the spelling, or shift-click the item into the chat box.")
    return
  end
  if type(ns.ShowChart) == "function" then
    ns.ShowChart(id, "slash")
  else
    P("the chart is not available.")
  end
end

function ns.HandleSlash(msg)
  msg = trim(type(msg) == "string" and msg or "")
  local cmd, rest = smatch(msg, "^(%S*)%s*(.-)$")
  cmd = slower(cmd or "")
  rest = trim(rest or "")
  local S = ns.settings
  if cmd == "" or cmd == "status" then
    printStatus()
  elseif cmd == "chart" then
    chartCommand(rest)
  elseif cmd == "tooltip" or cmd == "tooltips" then
    S.tooltip = not S.tooltip
    P("tooltip prices are now " .. (S.tooltip and "on" or "off") .. ".")
  elseif cmd == "compact" then
    S.compact = not S.compact
    P("tooltips are now " .. (S.compact and "one line" or "full") .. ".")
  elseif cmd == "help" or cmd == "?" then
    printHelp()
  else
    P("unknown command. Type /fah help for the list.")
  end
end

if type(_G.SlashCmdList) == "table" then
  _G.SLASH_FOREVERAH1 = "/fah"
  _G.SLASH_FOREVERAH2 = "/foreverah"
  _G.SlashCmdList["FOREVERAH"] = function(msg) ns.HandleSlash(msg) end
end

---------------------------------------------------------------------------
-- Events
---------------------------------------------------------------------------
local okTT, errTT = pcall(installTooltipHooks)
if not okTT then ns.lastError = errTT end

if type(_G.CreateFrame) == "function" then
  local ok, frame = pcall(_G.CreateFrame, "Frame")
  if ok and type(frame) == "table" then
    ns.eventFrame = frame
    frame:RegisterEvent("ADDON_LOADED")
    frame:RegisterEvent("PLAYER_LOGIN")
    frame:SetScript("OnEvent", function(self, event, arg1)
      if event == "ADDON_LOADED" then
        if arg1 == ADDON_NAME then loadSettings() end
      elseif event == "PLAYER_LOGIN" then
        if not ns.settingsLoaded then loadSettings() end
        pcall(installTooltipHooks)   -- picks up tooltips created after us
        if (tonumber(db.updated) or 0) <= 0 then
          P("no price data yet. Type /fah to see how to get it.")
        end
      end
    end)
  end
end
