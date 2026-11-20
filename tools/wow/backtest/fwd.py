"""What happened after hour T, from the hist files: would one unit listed at price P
have sold within H hours (two proxies), and what prices came next.

The loose proxy was checked against real auctions (wf40 validate_proxy, 19 Nov
18:25 -> 19:25): 99% precision and 100% recall for stacks, 100% / 100% for gear.
On 18 Nov hours (an older sales count that counted reposts as sales) every rule's
sale rate runs about 15-20% high alike."""
import bisect


class Fwd:
    def __init__(self, hist, hours):
        self.hist = hist
        self.hours = hours
        self.hset = set(hours)
        self._pts = {}

    def pts(self, sid):
        c = self._pts.get(sid)
        if c is None:
            pts = (self.hist.get(sid) or {}).get("h") or []
            c = ([int(p[0]) for p in pts], pts)
            self._pts[sid] = c
        return c

    def after(self, sid, T, H):
        """(the point at T or None, the hourly points with T < t <= T + H)."""
        ts, pts = self.pts(sid)
        i = bisect.bisect_right(ts, T)
        j = bisect.bisect_right(ts, T + H * 3600)
        at = pts[i - 1] if i > 0 and ts[i - 1] == T else None
        return at, pts[i:j]

    def measured(self, t):
        return (t - 3600) in self.hset

    def sale_times(self, sid, T, P, H):
        """(first hour a unit at P would have sold by the strict proxy, by the loose one),
        None when it would not have sold within H hours.
        strict: an hour t with sales (sold >= 1, measured) after which nothing at or under P
                was left (cheapest at t over P), or the item sold out with P at or under the
                market value of the hour before.
        loose:  an hour with sales during which P undercut the cheapest at either end."""
        at, nxt = self.after(sid, T, H)
        prev = at
        strict = loose = None
        for p in nxt:
            t, lo, sold = int(p[0]), p[1], p[4]
            if sold >= 1 and self.measured(t):
                plo = prev[1] if prev else 0
                phi = prev[2] if prev else 0
                ok_s = P < lo if lo > 0 else P <= max(phi, plo)
                ok_l = ok_s or (plo > 0 and P < plo)
                if ok_s and strict is None:
                    strict = t
                if ok_l and loose is None:
                    loose = t
                if strict is not None and loose is not None:
                    break
            prev = p
        return strict, loose

    def sold(self, sid, T, P, H):
        """Would one unit at P have sold within H hours (the loose proxy)."""
        return self.sale_times(sid, T, P, H)[1] is not None

    def units_sold_above(self, sid, T, R, H):
        """Units seen selling in hours where the cheapest left stayed over R (a crude cap on
        how many units relisted at R could have sold)."""
        _, nxt = self.after(sid, T, H)
        n = 0
        for p in nxt:
            if p[4] >= 1 and self.measured(int(p[0])) and (p[1] == 0 or p[1] > R):
                n += int(p[4])
        return n

    def lo_at_end(self, sid, T, H):
        """The cheapest listing at the last hour of the window with something listed."""
        at, nxt = self.after(sid, T, H)
        for p in reversed(nxt):
            if p[1] > 0:
                return p[1]
        return at[1] if at else 0
