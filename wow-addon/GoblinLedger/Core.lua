local ADDON_NAME, ns = ...
-- Goblin Ledger: core. Price data access, money text, settings, slash commands
-- and the item tooltip lines.
--
-- Price data comes from Data.lua (ForeverAH_Data), which the Windows updater
-- refreshes. Each item is one packed string. Nothing is unpacked at login:
-- an item is read the first time something asks for it, then cached.

ADDON_NAME = ADDON_NAME or "GoblinLedger"
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
-- A version with no known name looks like "Item 123 (version 2)" or " (version 2)".
local function unknownName(nm)
  if type(nm) ~= "string" or nm == "" or smatch(nm, "^Item %d+$") ~= nil then return true end
  return smatch(nm, "^%s*%(version %d+%)$") ~= nil or smatch(nm, "^Item %d+ %(version %d+%)$") ~= nil
end
ns.IsUnknownName = unknownName

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

---------------------------------------------------------------------------
-- Versions of an item (random suffix gear like "of the Bear")
---------------------------------------------------------------------------
-- Data.lua's variants table: [itemID] = "key=vid,key=vid". A key is the
-- version's bonus ids, lowest first, joined with ":" (maybe followed by
-- "m<type>=<value>" modifier parts). A vid (itemID * 1000 + n) has its own
-- item line, like an item. The plain item id keeps meaning "all versions".
-- Older data files have no variants: every item is then one plain item.
local variantData

local function variants()
  if variantData then return variantData end
  local vd = { byItem = {}, baseOf = {} }
  variantData = vd
  local raw = db.variants
  if type(raw) ~= "table" then return vd end
  for k, s in pairs(raw) do
    local base = tonumber(k)
    if base and type(s) == "string" then
      local list = {}
      for entry in string.gmatch(s, "[^,]+") do
        local key, v = smatch(entry, "^%s*(.-)=(%d+)%s*$")
        local vid = tonumber(v)
        -- A vid is itemID * 1000 + 1..999; anything else is not a version.
        if key and key ~= "" and vid and vid > base * 1000 and vid < base * 1000 + 1000 then
          local bonus, mods, nmods, ok = {}, {}, 0, true
          for tok in string.gmatch(key, "[^:]+") do
            local mt, mv = smatch(tok, "^m(%-?%d+)=(%-?%d+)$")
            if mt then
              mods[tonumber(mt)] = tonumber(mv)
              nmods = nmods + 1
            elseif smatch(tok, "^%d+$") then
              bonus[#bonus + 1] = tonumber(tok)
            else
              ok = false
            end
          end
          if ok then
            table.sort(bonus)
            list[#list + 1] = { key = key, vid = vid, bonus = tconcat(bonus, ":"), mods = mods, nmods = nmods }
            vd.baseOf[vid] = base
          end
        end
      end
      if #list > 0 then vd.byItem[base] = list end
    end
  end
  return vd
end

-- The item id behind a version id (itself for a plain item id).
function ns.BaseItemID(id)
  local n = tonumber(id)
  if not n then return nil end
  return variants().baseOf[n] or n
end

-- True for a version id (a vid from the variants table).
function ns.IsVariant(id)
  local n = tonumber(id)
  return n ~= nil and variants().baseOf[n] ~= nil
end

-- True when the data has separate prices for versions of this item.
function ns.HasVariants(id)
  local n = tonumber(id)
  return n ~= nil and variants().byItem[n] ~= nil
end

-- The version number of a vid (the "N" in "(version N)"), or nil.
function ns.VariantNumber(id)
  local n = tonumber(id)
  local base = n and variants().baseOf[n]
  if not base then return nil end
  return n - base * 1000
end

-- How many versions have their own prices.
function ns.VariantCount()
  local c = 0
  for _ in pairs(variants().baseOf) do c = c + 1 end
  return c
end

-- The fields of an item link after "item:" (strings, empty ones kept), or nil.
local function linkFields(link)
  if type(link) ~= "string" or isSecret(link) then return nil end
  local body = smatch(link, "item:([%-%d:]*)")
  if not body or body == "" then return nil end
  return split(body, ":")
end

local function wholeNumber(s)
  local v = tonumber(s)
  if v and v == floor(v) then return v end
  return nil
end

-- The bonus ids (lowest first) and modifiers ({ [type] = value }) of an item
-- link, or nil when it is not an item link. Link fields after "item:":
-- id, enchant, 4 gems, suffix, unique, level, spec, modifier mask, context,
-- number of bonus ids, the bonus ids, number of modifiers, type:value pairs.
-- Anything odd (cut short, not numbers, silly counts) is skipped.
function ns.LinkBonus(link)
  local f, n = linkFields(link)
  if not f then return nil end
  local bonus, mods = {}, {}
  local nb = wholeNumber(f[13]) or 0
  if nb < 0 or nb > 40 or 13 + nb > n then nb = 0 end
  for k = 1, nb do
    local v = wholeNumber(f[13 + k])
    if v and v > 0 then bonus[#bonus + 1] = v end
  end
  local i = 14 + nb
  local nm = wholeNumber(f[i]) or 0
  if nm > 0 and nm <= 40 and i + 2 * nm <= n then
    for k = 1, nm do
      local t, v = wholeNumber(f[i + 2 * k - 1]), wholeNumber(f[i + 2 * k])
      if t and v then mods[t] = v end
    end
  end
  table.sort(bonus)
  return bonus, mods
end

-- The variant key of a link: its bonus ids, lowest first, joined with ":"
-- ("" for none), as the collector writes it.
function ns.VariantKey(link)
  local bonus = ns.LinkBonus(link)
  if not bonus then return "" end
  return tconcat(bonus, ":")
end

-- The vid whose prices fit an item link, or nil when the data has no prices
-- for that version. The most exact match wins (modifier parts count).
function ns.FindVariant(itemID, link)
  local id = tonumber(itemID)
  local list = id and variants().byItem[id]
  if not list then return nil end
  local bonus, mods = ns.LinkBonus(link)
  if not bonus then return nil end
  local key = tconcat(bonus, ":")
  local best, bestN
  for _, e in ipairs(list) do
    if e.bonus == key and (key ~= "" or e.nmods > 0) then
      local ok = true
      for t, v in pairs(e.mods) do
        if mods[t] ~= v then ok = false break end
      end
      if ok and (not bestN or e.nmods > bestN) then best, bestN = e.vid, e.nmods end
    end
  end
  if best and type(db.items[best]) == "string" then return best end
  return nil
end

-- The vid of an item's version whose name (in the data, like "Ridge Cleaver
-- of the Bear") is `name`, or nil. Versions named "(version N)" never match.
function ns.VersionByName(itemID, name)
  local id = tonumber(itemID)
  local list = id and variants().byItem[id]
  if not list or type(name) ~= "string" or isSecret(name) or name == "" then return nil end
  for _, e in ipairs(list) do
    if type(db.items[e.vid]) == "string" and ns.ItemName(e.vid) == name then return e.vid end
  end
  return nil
end

-- An "item:..." link string for a vid, carrying that version's bonus ids and
-- modifiers (so the game's tooltip shows that version), or nil.
function ns.VariantLink(vid)
  local n = tonumber(vid)
  local base = n and variants().baseOf[n]
  if not base then return nil end
  for _, e in ipairs(variants().byItem[base] or {}) do
    if e.vid == n then
      local bonus = {}
      for tok in string.gmatch(e.bonus, "[^:]+") do bonus[#bonus + 1] = tok end
      local parts = { "item:" .. base, "", "", "", "", "", "", "", "", "", "", "", tostring(#bonus) }
      for _, b in ipairs(bonus) do parts[#parts + 1] = b end
      local mods = {}
      for t, v in pairs(e.mods) do mods[#mods + 1] = t .. ":" .. v end
      table.sort(mods)
      parts[#parts + 1] = tostring(#mods)
      for _, m in ipairs(mods) do parts[#parts + 1] = m end
      return tconcat(parts, ":")
    end
  end
  return nil
end

-- The game's own name for an item, or nil when it has not loaded it (asking
-- also makes the game load it, so a later call usually has it). A version
-- gets the item's name plus "(version N)".
local function gameName(id)
  local function good(ok, n)
    return ok and type(n) == "string" and not isSecret(n) and not unknownName(n)
  end
  local base = variants().baseOf[id]
  if base then
    local n = gameName(base)
    if n then return n .. " (version " .. (id - base * 1000) .. ")" end
    return nil
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
  -- A version of an item (random suffix gear): base is the item's own id.
  local base = variants().baseOf[id]
  if base then
    item.base = base
    item.variant = true
  end
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
  local n = gameName(id)
  if n then return n end
  local base = variants().baseOf[id]
  if base then return "Item " .. base .. " (version " .. (id - base * 1000) .. ")" end
  return "Item " .. id
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

-- How many items the data has prices for (versions of an item not counted).
local itemCount
function ns.ItemCount()
  if not itemCount then
    itemCount = 0
    local baseOf = variants().baseOf
    for id in pairs(db.items) do
      if not baseOf[tonumber(id) or -1] then itemCount = itemCount + 1 end
    end
  end
  return itemCount
end

-- Accepts an item link, an item id or an item name. Returns an id we have data
-- for: a link to a version with its own prices gives that version's id.
function ns.ResolveItem(text)
  if type(text) == "number" then return ns.GetItem(text) and text or nil end
  if type(text) ~= "string" then return nil end
  local linked = smatch(text, "item:(%d+)")
  if linked then
    local id = tonumber(linked)
    local vid = ns.FindVariant(id, text)
    if vid then return vid end
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

-- Lowest and highest cheapest price over the last 7 days of hourly prices,
-- and how many hourly prices that was. Only the count while there are fewer
-- than 6 (too few to call it a week's range).
ns.WEEK_MIN_POINTS = 6
function ns.WeekRange(itemID)
  local s = ns.GetSeries(itemID, "1W")
  if not s then return nil, nil, 0 end
  local lo, hi, n = nil, nil, 0
  for i = 1, s.n do
    local c = s.cheap[i]
    if c then
      n = n + 1
      if not lo or c < lo then lo = c end
      if not hi or c > hi then hi = c end
    end
  end
  if n < ns.WEEK_MIN_POINTS then return nil, nil, n end
  return lo, hi, n
end

-- Flip ideas the price collector worked out from one snapshot (Data.lua's
-- flips list, best first). Each line is "id|kind|buyMax|units|cost|relist|
-- profit|roi|risk|usual" (older data files stop after risk). Each flip: id
-- (an item or a version), kind ("gap": buy the cheap listings and relist
-- under the next one; "deal": a green, blue or epic item listed well under
-- its usual price, bought and relisted at most at that price, just under the
-- next listing when one sits below it; "vendor": listings under what a
-- vendor pays), buyMax (highest unit price to pay), units, cost,
-- relist (unit price, 0 for vendor flips), profit (after the 5% cut, or vs
-- the vendor), roi (profit per 1000 copper spent), risk (a list of words:
-- thin, slow, spike, unsure, suffix) and usual (its usual price: the middle
-- of the last 7 days of hourly prices, 0 when not known). Kinds this addon
-- does not know are skipped. Older data files have none: an empty list.
local flipList
function ns.DataFlips()
  if flipList then return flipList end
  flipList = {}
  local raw = db.flips
  if type(raw) ~= "table" then return flipList end
  for i = 1, #raw do
    local s = raw[i]
    if type(s) == "string" then
      local f = split(s, "|")
      local id, kind = wholeNumber(f[1]), f[2]
      local buyMax, units = wholeNumber(f[3]), wholeNumber(f[4])
      local cost, relist, profit = wholeNumber(f[5]), wholeNumber(f[6]) or 0, wholeNumber(f[7])
      -- gap and deal flips are relisted, so they need a relist price
      local relists = kind == "gap" or kind == "deal"
      if id and id > 0 and (kind == "vendor" or (relists and relist > 0)) and buyMax and buyMax > 0
        and units and units > 0 and profit and profit > 0 then
        local risk = {}
        for w in string.gmatch(f[9] or "", "[^,%s]+") do risk[#risk + 1] = slower(w) end
        local usual = wholeNumber(f[10]) or 0
        if usual < 0 then usual = 0 end
        flipList[#flipList + 1] = {
          id = id, kind = kind, buyMax = buyMax, units = units,
          cost = (cost and cost > 0) and cost or buyMax * units,
          relist = relists and relist or 0,
          profit = profit, roi = wholeNumber(f[8]) or 0, risk = risk, usual = usual,
        }
      end
    end
  end
  return flipList
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
  local line = "|c" .. ns.Hex(ns.COLORS.sell) .. "Goblin Ledger:|r " .. tostring(msg)
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
  -- Which target line the chart shows: "buy", "sell" or none (the default).
  if S.chartLine ~= "buy" and S.chartLine ~= "sell" then S.chartLine = nil end
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

-- The bigger lines, on GameTooltip and ItemRefTooltip only: the verdict
-- ("Auction it" / "Vendor it"), then the price that wins with it (AH value
-- under "Auction it", Vendor pays under "Vendor it"). Both FontStrings of
-- such a line get our own Font: the line's face and flags, 3 points bigger
-- (at most 24). It is done after all our lines exist, because a line the game
-- makes later copies the Font of the line above it. The exact Font object is
-- saved and put back when the tooltip clears or hides. GameTooltipText (ElvUI
-- styles it) is never changed, nor the comparison tooltips' fonts. Each line
-- is bigger whole or not at all: anything that fails leaves that line at the
-- normal size, and a line made bigger before it stays bigger.
local BIG_TOOLTIPS = { GameTooltip = true, ItemRefTooltip = true }
local BIG_STEP, BIG_MAX = 3, 24
local bigFonts, bigSeq = {}, 0                        -- "file|size|flags" -> our Font (false: none could be made)
local ourFonts = setmetatable({}, { __mode = "k" })   -- Font -> true for the Fonts made here
local bigSaved = setmetatable({}, { __mode = "k" })   -- FontString -> { obj, file, size, flags }
local bigLines = setmetatable({}, { __mode = "k" })   -- tooltip -> { FontString, ... }

-- Puts the saved Font back on the enlarged FontStrings of a tooltip after the
-- first keep of them (keep 0: every one). Each keeps its colour
-- (SetFontObject takes the Font's colour), so a line still on screen (a swap
-- that failed half way, or OnHide from a parent hiding) stays green or red at
-- the normal size.
local function restoreFrom(tt, keep)
  local l = bigLines[tt]
  if not l then return end
  for i = #l, keep + 1, -1 do
    local fs = l[i]
    l[i] = nil
    local s = bigSaved[fs]
    if s then
      bigSaved[fs] = nil
      local okC, r, g, b, a = pcall(fs.GetTextColor, fs)
      if s.obj then pcall(fs.SetFontObject, fs, s.obj) else pcall(fs.SetFont, fs, s.file, s.size, s.flags) end
      if okC and type(r) == "number" and type(g) == "number" and type(b) == "number"
         and not isSecret(r) and not isSecret(g) and not isSecret(b) then
        if type(a) == "number" and not isSecret(a) then
          pcall(fs.SetTextColor, fs, r, g, b, a)
        else
          pcall(fs.SetTextColor, fs, r, g, b)
        end
      end
    end
  end
  if #l == 0 then bigLines[tt] = nil end
end
local function restoreBig(tt) restoreFrom(tt, 0) end

-- One Font per face, size and flags, made once (a copy of src keeps ElvUI's shadow).
local function bigFontFor(src, file, size, flags)
  local k = file .. "|" .. size .. "|" .. flags
  local f = bigFonts[k]
  if f ~= nil then return f or nil end
  bigFonts[k] = false
  if type(_G.CreateFont) ~= "function" then return nil end
  bigSeq = bigSeq + 1
  local ok, o = pcall(_G.CreateFont, "ForeverAHBigFont" .. bigSeq)
  if not ok or type(o) ~= "table" or type(o.SetFont) ~= "function" then return nil end
  if type(src) == "table" and type(o.CopyFontObject) == "function" then pcall(o.CopyFontObject, o, src) end
  local ok2, set = pcall(o.SetFont, o, file, size, flags)
  if not ok2 or set == false then return nil end
  bigFonts[k] = o
  ourFonts[o] = true
  return o
end

-- The left or right FontString of tooltip line n, or nil.
local function lineFS(tt, name, side, n)
  local get = (side == "Left") and tt.GetLeftLine or tt.GetRightLine
  local fs
  if type(get) == "function" then
    local ok, l = pcall(get, tt, n)
    if ok then fs = l end
  end
  if type(fs) ~= "table" then fs = _G[name .. "Text" .. side .. n] end
  if type(fs) ~= "table" or type(fs.GetFont) ~= "function" or type(fs.SetFontObject) ~= "function" then return nil end
  return fs
end

-- Puts one FontString on the bigger Font and sets its colour c again
-- (SetFontObject takes the Font's colour). false when it could not.
local function enlargeFS(tt, fs, c)
  local s = bigSaved[fs]
  if not s then
    local okF, file, size, flags = pcall(fs.GetFont, fs)
    if not okF or isSecret(file) or isSecret(size) or isSecret(flags) then return false end
    if type(file) ~= "string" or file == "" or type(size) ~= "number" or size <= 0 then return false end
    local obj
    if type(fs.GetFontObject) == "function" then
      local ok, o = pcall(fs.GetFontObject, fs)
      if ok and type(o) == "table" then
        if ourFonts[o] then return false end
        obj = o
      end
    end
    s = { obj = obj, file = file, size = size, flags = type(flags) == "string" and flags or "" }
  end
  local base = floor(s.size + 0.5)
  local big = min(base + BIG_STEP, BIG_MAX)
  if big <= base then return false end
  local font = bigFontFor(s.obj, s.file, big, s.flags)
  if not font then return false end
  bigSaved[fs] = s
  local l = bigLines[tt] or {}
  l[#l + 1] = fs
  bigLines[tt] = l
  if not pcall(fs.SetFontObject, fs, font) then return false end
  pcall(fs.SetTextColor, fs, c[1], c[2], c[3])
  return true
end

-- Enlarges both sides of tooltip line n (or neither: a side already swapped
-- goes back, the lines enlarged before it stay). Only with the restore hook
-- in, on GameTooltip / ItemRefTooltip, for n >= 3 and with a line after n.
local function enlargeLine(tt, n, lc, rc)
  if clearHooked[tt] ~= true then return false end
  local okN, name = pcall(tt.GetName, tt)
  if not okN or type(name) ~= "string" or isSecret(name) or not BIG_TOOLTIPS[name] then return false end
  local okC, c = pcall(tt.NumLines, tt)
  if not okC or type(c) ~= "number" or isSecret(c) or n < 3 or c <= n then return false end
  local keep = bigLines[tt] and #bigLines[tt] or 0
  local L, R = lineFS(tt, name, "Left", n), lineFS(tt, name, "Right", n)
  if not L or not R or not enlargeFS(tt, L, lc) or not enlargeFS(tt, R, rc) then
    restoreFrom(tt, keep)
    return false
  end
  return true
end

local function numLines(tt)
  if type(tt.NumLines) ~= "function" then return nil end
  local ok, n = pcall(tt.NumLines, tt)
  if ok and type(n) == "number" and not isSecret(n) then return n end
  return nil
end

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
    hooked = pcall(tt.HookScript, tt, "OnTooltipCleared", function(self) done[self] = nil; restoreBig(self) end)
    if hooked then pcall(tt.HookScript, tt, "OnHide", restoreBig) end
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

-- The item link the tooltip shows (it carries the version's bonus ids), or nil.
local function tooltipLink(tt, data)
  local l = type(data) == "table" and data.hyperlink or nil
  if l ~= nil and not isSecret(l) and type(l) == "string" then return l end
  if type(tt.GetItem) == "function" then
    local ok, _, link = pcall(tt.GetItem, tt)
    if ok and link ~= nil and not isSecret(link) and type(link) == "string" then return link end
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

-- The id whose prices fit an item link, and what those prices cover:
--   vid, "version"   the data has prices for this exact version
--   id, "all"        versions of this item differ and these prices mix them
--                    (the data lists versions, or the link names one the
--                    data does not have, or an older data file)
--   id, nil          a plain item
function ns.PriceIDForLink(itemID, link)
  local id = tonumber(itemID)
  if not id then return nil end
  local vid = ns.FindVariant(id, link)
  if vid then return vid, "version" end
  local bonus = ns.LinkBonus(link)
  if ns.HasVariants(id) or ns.LinkHasSuffix(link) or (bonus and #bonus > 0) then return id, "all" end
  return id, nil
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

-- The live cheapest listing the open auction house showed for an id (AH.lua).
local function liveFor(id)
  if type(ns.LiveCheapest) ~= "function" then return nil end
  local ok, p, own = pcall(ns.LiveCheapest, id)
  if ok and type(p) == "number" and p > 0 then return p, own == true end
  return nil
end

-- How far today's AH value sits from the average price: "+3%" in green, "-5%"
-- in red (whole percent, rounded toward 0 so it never says more than it is),
-- a grey "0%" under 1%, or nil with no average to compare with.
local function vsAverage(value, avg)
  value, avg = tonumber(value) or 0, tonumber(avg) or 0
  if value <= 0 or avg <= 0 then return nil end
  local C = ns.COLORS
  local p = floor(abs(value - avg) * 100 / avg)
  if p < 1 then return "|c" .. ns.Hex(C.muted) .. "0%|r" end
  if value > avg then return "|c" .. ns.Hex(C.pos) .. "+" .. p .. "%|r" end
  return "|c" .. ns.Hex(C.neg) .. "-" .. p .. "%|r"
end
ns.VsAverage = vsAverage

-- "7-day average", or "3-day average" while the item has prices for fewer
-- than 7 days (the data's average is of the days it has, at most the last 7).
local function averageLabel(id)
  local days = type(ns.DailyPoints) == "function" and ns.DailyPoints(id) or 0
  if days < 1 or days > 7 then days = 7 end
  return days .. "-day average"
end
ns.AverageLabel = averageLabel

-- Adds our lines. Returns true when anything was added. Kept short:
--   the "Goblin Ledger" header (how old the prices are), then the sell verdict
--   (ns.SellVerdict: "Auction it", "Vendor it", "Close call" or "Maybe
--   auction", and by how much), AH value (today's market value) and how far
--   it is from the average, the 7-day average, Sell now and Suggested price
--   (the sell target from the price history; each in red when it nets no
--   more than a vendor pays), then for a stack what it brings at the
--   verdict's price after the AH cut (only when the AH pays more, or about
--   the same). While the prices are fresh, "Auction it" and the AH value
--   are bigger, and "Vendor it" gets a "Vendor pays" line right under it
--   (what a vendor pays each, or for the stack with Shift), both bigger
--   (enlargeLine). Compact mode is one line: the verdict and the AH value,
--   or the AH value alone.
-- itemID may be a version's id (vid). opts (optional): noData = false hides
-- the "no recent listings" line; bound = true (soulbound, bind on pickup or a
-- quest item: it can never be on the auction house) leaves out the verdict and
-- the stack line; version = "version" says the prices are for this version
-- only, version = "all" (or suffix = true) that they cover every version of
-- the item. Then the AH value and the average still show (they are what the
-- data saw), but there is no verdict, Sell now, Suggested price or stack
-- line: a price for selling would come from other versions' listings.
-- There is no verdict either for a market these prices are not from
-- (ns.MarketMismatch: Hardcore, the other faction, another auction house).
function ns.AddTooltipLines(tt, itemID, count, opts)
  local S = ns.settings
  if not S.tooltip then return false end
  if (tonumber(db.updated) or 0) <= 0 then return false end
  local C = ns.COLORS
  local mu, tx, se = C.muted, C.text, C.sell
  opts = type(opts) == "table" and opts or {}
  local item = ns.GetItem(itemID)
  if not item then
    if opts.noData == false then return false end
    tt:AddLine("Goblin Ledger: no recent listings", mu[1], mu[2], mu[3])
    return true
  end
  count = floor(tonumber(count) or 1)
  if count < 1 then count = 1 end
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
  -- How far today's AH value is from the average, in green or red.
  local vs = vsAverage(item.market, item.avg7)
  -- Sell now: 1 copper under the cheapest listing (the live one when the
  -- auction house showed it). Suggested price: the sell target from the
  -- price history. Both, and the verdict, only for one exact item or version.
  local mixed = opts.version == "all" or opts.suffix == true
  local live, own = liveFor(item.id)
  local P = (not mixed) and type(ns.SellPrices) == "function" and ns.SellPrices(item.id, S.chartTF, live, own) or nil
  local V
  if P and opts.bound ~= true and type(ns.SellVerdict) == "function"
     and not (type(ns.MarketMismatch) == "function" and ns.MarketMismatch()) then
    V = ns.SellVerdict(item, P)
  end
  local vline, xline   -- the lines to enlarge (the verdict, the price that wins), once all our lines are in
  if S.compact then
    local m = money(item.market)
    if V then
      local lc = C[V.lc]
      if m then
        tt:AddDoubleLine(V.left .. ": " .. V.right, "AH" .. tag .. " " .. m .. (vs and ("  " .. vs) or ""),
          lc[1], lc[2], lc[3], 1, 1, 1)
      else
        tt:AddDoubleLine(V.left .. ": " .. V.right, "AH: none listed", lc[1], lc[2], lc[3], mu[1], mu[2], mu[3])
      end
    else
      local line
      if m then
        line = "AH value" .. tag .. ": " .. m .. (vs and ("  " .. vs) or "")
      else
        line = "AH value: none listed now"
      end
      tt:AddLine(line, tx[1], tx[2], tx[3])
    end
  else
    tt:AddDoubleLine("Goblin Ledger", ns.AgeText(age), se[1], se[2], se[3], mu[1], mu[2], mu[3])
    local clearCut = V and (V.kind == "auction" or V.kind == "vendor") and age <= ns.STALE_SECONDS
    if V then
      local lc, rc = C[V.lc], C[V.rc]
      tt:AddDoubleLine(V.left, V.right, lc[1], lc[2], lc[3], rc[1], rc[2], rc[3])
      if clearCut then vline = numLines(tt) end
    end
    -- "Vendor it": what a vendor pays, bigger with the verdict.
    local vp = clearCut and V.kind == "vendor" and money(item.vendor)
    if vp then
      tt:AddDoubleLine("Vendor pays" .. tag, vp, tx[1], tx[2], tx[3], 1, 1, 1)
      xline = numLines(tt)
    end
    local m = money(item.market)
    if m then
      tt:AddDoubleLine("AH value" .. tag, vs and (m .. "  " .. vs) or m, tx[1], tx[2], tx[3], 1, 1, 1)
      -- "Auction it": the AH value is bigger with the verdict.
      if clearCut and V.kind == "auction" then xline = numLines(tt) end
    else
      tt:AddDoubleLine("AH value", "none listed", tx[1], tx[2], tx[3], mu[1], mu[2], mu[3])
    end
    local a = money(item.avg7)
    if a then
      tt:AddDoubleLine(averageLabel(item.id) .. tag, a, tx[1], tx[2], tx[3], 1, 1, 1)
    end
    -- In red when a vendor pays at least as much after the 5% cut (the flags
    -- the sell buttons warn with).
    local ng, wh = C.neg, { 1, 1, 1 }
    if P and P.now then
      local c = P.nowVendor and ng or wh
      tt:AddDoubleLine((P.live and "Sell now (live)" or "Sell now") .. tag, money(P.now), tx[1], tx[2], tx[3], c[1], c[2], c[3])
    end
    if P and P.best then
      local c = P.bestVendor and ng or wh
      tt:AddDoubleLine("Suggested price" .. tag, money(P.best), tx[1], tx[2], tx[3], c[1], c[2], c[3])
    end
    -- One grey line says when a price is left out because it may be one
    -- player's guess (not when the verdict already says "few listed").
    if P and (P.nowWhy == "unreliable" or P.bestWhy == "unreliable") and not (V and V.right == "few listed") then
      tt:AddLine("Few listed: the price may not be reliable", mu[1], mu[2], mu[3])
    end
    -- The stack at the verdict's price, never multiplied by Shift.
    if V and V.unit and count > 1 and (V.kind == "auction" or V.kind == "close") then
      tt:AddDoubleLine("This stack (" .. count .. "), after AH cut", ns.MoneyIcons(floor(V.unit * count * 95 / 100)),
        tx[1], tx[2], tx[3], 1, 1, 1)
    end
  end
  if opts.version == "version" then
    tt:AddLine("Prices for this version only", mu[1], mu[2], mu[3])
  elseif opts.version == "all" or opts.suffix then
    tt:AddLine("Price covers all versions of this item", mu[1], mu[2], mu[3])
  end
  if age > ns.STALE_SECONDS then
    tt:AddLine(sformat("Prices are %dh old: run Install-Goblin-Ledger.bat, then /reload", floor(age / 3600)), mu[1], mu[2], mu[3])
  end
  -- Last: every line after ours exists now (see enlargeLine). Nothing from
  -- an earlier item stays bigger. The second line only with the verdict:
  -- when it cannot be bigger, the verdict still is.
  restoreBig(tt)
  if vline and enlargeLine(tt, vline, C[V.lc], C[V.rc]) and xline then enlargeLine(tt, xline, tx, { 1, 1, 1 }) end
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
  local link = tooltipLink(tt, data)
  local pid, cover = ns.PriceIDForLink(id, link)
  pid = pid or id
  local priced = ns.GetItem(pid) ~= nil
  if not priced then
    -- No data: only worth a line for items that could be on the auction house.
    -- Comparison tooltips show what you wear, which is nearly always soulbound.
    local ok, name = pcall(tt.GetName, tt)
    local shopping = ok and type(name) == "string" and sfind(name, "ShoppingTooltip", 1, true) ~= nil
    opts.noData = not shopping and mightBeTradeable(tt, data, id)
  else
    opts.version = cover
    -- Prices, but no verdict, for an item that can never be on the auction house.
    opts.bound = not mightBeTradeable(tt, data, id)
  end
  ns.AddTooltipLines(tt, pid, stackCount(tt, data), opts)
  -- A quest item with no prices: Bags.lua says whether you can drop it.
  if not priced and type(ns.QuestItemLine) == "function" then ns.QuestItemLine(tt, id) end
  -- A recipe, a crafted item or a mat: what making it costs and brings (Crafts.lua).
  if type(ns.CraftLines) == "function" then ns.CraftLines(tt, id, info) end
  -- Gear you could enchant: the best enchants for your role (Enchants.lua), after our other lines.
  if type(ns.EnchantLines) == "function" then ns.EnchantLines(tt, id, link, info) end
end

-- OnTooltipSetItem script hook (fallback for clients without TooltipDataProcessor).
local function onTooltipSetItem(tt)
  if not allowedTooltip(tt) then return end
  local guarded = ensureClearHook(tt)
  if guarded and done[tt] then return end
  local id = tooltipItemID(tt, nil)
  if not id then return end
  if guarded then done[tt] = id end
  local link = tooltipLink(tt, nil)
  local pid, cover = ns.PriceIDForLink(id, link)
  pid = pid or id
  local hasData = ns.GetItem(pid) ~= nil
  local tradeable = mightBeTradeable(tt, nil, id)
  local opts = { noData = hasData or tradeable, version = cover, bound = hasData and not tradeable }
  local added = ns.AddTooltipLines(tt, pid, 1, opts)
  -- A quest item with no prices: Bags.lua says whether you can drop it.
  if not hasData and type(ns.QuestItemLine) == "function" and ns.QuestItemLine(tt, id) then added = true end
  -- A recipe, a crafted item or a mat (Crafts.lua).
  if type(ns.CraftLines) == "function" and ns.CraftLines(tt, id, nil) then added = true end
  -- Gear you could enchant (Enchants.lua). This path does not say where the item is.
  if type(ns.EnchantLines) == "function" and ns.EnchantLines(tt, id, link, nil) then added = true end
  -- Show lays the tooltip out again, the bigger lines included.
  if added and type(tt.Show) == "function" then tt:Show() end
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
  P("Run Install-Goblin-Ledger.bat again (it downloads the latest prices from grantdenney.com/goblin-ledger/prices into this addon), then type /reload here.")
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
  local versions = ns.VariantCount()
  if versions > 0 then
    P(sformat("%s versions of gear (like \"of the Bear\") have their own prices.", ns.Num(versions)))
  end
  if age > ns.STALE_SECONDS then
    P(sformat("Prices are %dh old: run Install-Goblin-Ledger.bat, then /reload.", floor(age / 3600)))
  end
  local faction = playerFaction()
  if faction and faction ~= "Alliance" then
    P("Note: these prices come from the " .. ns.HouseLabel() .. ", so they may not match the auction house you use.")
  end
  if isHardcore() then
    P("Note: these prices are from the normal realms, not Hardcore.")
  end
  P("Type /gl help for commands.")
end

local function printHelp()
  P("commands:")
  P("/gl : show how fresh the price data is")
  P("/gl chart : show or hide the price chart")
  P("/gl chart Linen Cloth : open the chart for an item (you can also shift-click an item into the chat box)")
  P("/gl chart reset : put the chart back in its normal place after you dragged it")
  P("/gl tooltip : turn the prices in item tooltips on or off")
  P("/gl compact : switch tooltips between full and one-line")
  P("/gl update : which Goblin Ledger version you have, and how to get a newer one")
  P("/gl help : show this list")
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
  elseif cmd == "update" or cmd == "version" then
    if type(ns.UpdateHelp) == "function" then ns.UpdateHelp() end
  else
    P("unknown command. Type /gl help for the list.")
  end
end

if type(_G.SlashCmdList) == "table" then
  -- /gl is the command players are told about. /fah and /foreverah are the
  -- names from before the rename (the addon was called Forever AH) and keep working.
  _G.SLASH_GOBLINLEDGER1 = "/gl"
  _G.SLASH_GOBLINLEDGER2 = "/goblinledger"
  _G.SLASH_GOBLINLEDGER3 = "/fah"
  _G.SLASH_GOBLINLEDGER4 = "/foreverah"
  _G.SlashCmdList["GOBLINLEDGER"] = function(msg) ns.HandleSlash(msg) end
end

---------------------------------------------------------------------------
-- New versions
---------------------------------------------------------------------------
-- Latest.lua (GoblinLedger_Latest) is written by the Windows updater: the
-- newest official version it found ("version") and the one it installed
-- ("installed", at "installedAt", "auto" = by itself, "restart" = the new
-- version has a new file list, which only a game restart loads, "waiting" =
-- it installs by itself once WoW is closed). At most one short chat line per
-- login:
--   - once after an update the updater installed by itself: "updated to"
--     (also after Update-Goblin-Ledger.bat when the game still needs a restart)
--   - else, while a newer version waits: "is out", once per login, never again
--     after a /reload; with "it installs by itself after you close WoW" when it
--     does, else pointing at /gl update (which says where Update-Goblin-Ledger.bat is)
local function versionParts(s)
  if type(s) ~= "string" then return nil end
  local a, b, c = smatch(s, "^(%d+)%.(%d+)%.(%d+)$")
  if not a then return nil end
  return { tonumber(a), tonumber(b), tonumber(c) }
end

local function newerThan(x, y)
  for i = 1, 3 do
    if x[i] ~= y[i] then return x[i] > y[i] end
  end
  return false
end

-- The ## Version of the TOC the game read when it started.
local function runningVersion()
  local CA = _G.C_AddOns
  local f = (type(CA) == "table" and type(CA.GetAddOnMetadata) == "function") and CA.GetAddOnMetadata
    or _G.GetAddOnMetadata
  if type(f) ~= "function" then return nil end
  local ok, v = pcall(f, ADDON_NAME, "Version")
  if ok and type(v) == "string" and not isSecret(v) and versionParts(v) then return v end
  return nil
end
ns.RunningVersion = runningVersion

-- The newer official version the updater found (text), or nil: newer than the
-- version the game started with and than the one installed since.
local function newerVersion()
  local L = _G.GoblinLedger_Latest
  if type(L) ~= "table" then return nil end
  local newest = versionParts(L.version)
  local have = versionParts(runningVersion())
  local inst = versionParts(L.installed)
  if inst and (not have or newerThan(inst, have)) then have = inst end
  if newest and have and newerThan(newest, have) then return L.version end
  return nil
end

-- The line to show now (plain text), or nil. reloading: a /reload, not a login.
function ns.UpdateNotice(reloading)
  local L = _G.GoblinLedger_Latest
  if type(L) ~= "table" then return nil end
  local S = ns.settings
  local running = runningVersion()
  local installed = versionParts(L.installed) and L.installed or nil
  local at = tonumber(L.installedAt)
  if installed and at and at > 0 and S.updateSeen ~= at then
    S.updateSeen = at
    local restart = L.restart == true and running ~= installed
    if L.auto == true or restart then
      return "Goblin Ledger updated to " .. installed .. "." .. (restart and " Restart WoW to finish." or "")
    end
  end
  if reloading then return nil end
  local newer = newerVersion()
  if newer and L.waiting == true then
    return "Goblin Ledger " .. newer .. " is out: it installs by itself after you close WoW."
  elseif newer then
    return "Goblin Ledger " .. newer .. " is out. Type /gl update for how to get it."
  end
  return nil
end

-- /gl update: the version, and where Update-Goblin-Ledger.bat is (the
-- unzipped folder is often deleted; the installer keeps a copy in
-- %LOCALAPPDATA%\GoblinLedger, which Windows+R opens).
function ns.UpdateHelp()
  local L = _G.GoblinLedger_Latest
  local have = runningVersion()
  local newer = newerVersion()
  if newer then
    P("version " .. newer .. " is out" .. (have and (" (you have " .. have .. ")") or "") .. ".")
    if type(L) == "table" and L.waiting == true then
      P("it installs by itself after you close WoW. To get it now:")
    else
      P("to get it:")
    end
  else
    P((have and ("you have version " .. have) or "you have this version") .. ". To look for a newer one now:")
  end
  P("press Windows+R, type %LOCALAPPDATA%\\GoblinLedger and press Enter, then double-click")
  P("Update-Goblin-Ledger.bat. When it says Updated, type /reload (or restart WoW if it says so).")
end

local noticeDone = false
local function showUpdateNotice(isInitialLogin, isReloadingUi)
  if noticeDone then return end
  noticeDone = true
  local ok, line = pcall(ns.UpdateNotice, isReloadingUi == true)
  if not ok then
    ns.lastError = line
    return
  end
  if type(line) ~= "string" then return end
  local text = "|c" .. ns.Hex(ns.COLORS.sell) .. "Goblin Ledger|r" .. ssub(line, 14)
  local cf = _G.DEFAULT_CHAT_FRAME
  if type(cf) == "table" and type(cf.AddMessage) == "function" then
    pcall(cf.AddMessage, cf, text)
  elseif type(_G.print) == "function" then
    pcall(_G.print, text)
  end
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
    frame:RegisterEvent("PLAYER_ENTERING_WORLD")
    frame:SetScript("OnEvent", function(self, event, arg1, arg2)
      if event == "ADDON_LOADED" then
        if arg1 == ADDON_NAME then loadSettings() end
      elseif event == "PLAYER_LOGIN" then
        if not ns.settingsLoaded then loadSettings() end
        pcall(installTooltipHooks)   -- picks up tooltips created after us
        if (tonumber(db.updated) or 0) <= 0 then
          P("no price data yet. Type /gl to see how to get it.")
        end
      elseif event == "PLAYER_ENTERING_WORLD" then
        -- arg1: a fresh login, arg2: a /reload (the first one after either counts)
        if not ns.settingsLoaded then loadSettings() end
        showUpdateNotice(arg1, arg2)
      end
    end)
  end
end
