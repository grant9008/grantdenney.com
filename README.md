# grantdenney.com

Personal site for Grant Denney. Plain HTML, CSS and JavaScript — no framework,
no bundler, no build step. Open `index.html` and what you see is what ships.

## Looking at it locally

Double-click **`Preview-Site.bat`**. It opens <http://localhost:4321> in your
browser. Leave the black window open while you're looking; close it when done.

## Adding screenshots and photos

The project cards (Personal Space, PocketGE, Flip Tracker, RealmForge) show a
placeholder panel until you supply an image.

1. Put your images in `assets/img/work/incoming/`
2. Name them `personalspace.png`, `pocketge.png`, `fliptracker.png`, `realmforge.png`
3. Double-click **`Add-Screenshots.bat`**
4. Refresh the site

They're resized and compressed automatically. You can add them one at a time —
anything missing just keeps its placeholder.

## Changing the Red Bull Showrun photos

`tools/showrun-picks.txt` lists which photos appear in the slider and what each
caption says. One line per photo:

```
2X9A0096.jpg | Red Bull Racing show car in the Houston fan zone
```

Edit that file, then double-click **`Rebuild-Photos.bat`**. That now does the
whole job — optimizes the images, regenerates the slide markup and splices it
into `index.html` between the `SLIDES-START` / `SLIDES-END` markers, and updates
the slide counter. Adding or removing photos needs nothing extra.

Source photos live in `Desktop/Showrun Photos`. The script reads from there and
writes optimized copies into `assets/img/showrun/`.

## Layout

```
index.html                 the whole page
assets/css/site.css        design tokens first, then layout, then components
assets/js/site.js          nav, scroll reveal, slider, lightbox — no dependencies
assets/img/showrun/        optimized Showrun photos (generated)
tools/_slides.html         generated slide markup (intermediate)
tools/_template-g30p.svg   the G30P cut file, cleaned up for inlining
assets/img/work/           project screenshots (generated)
tools/                     the small Python scripts behind the .bat files
Grant-Denney-Resume.pdf    linked from the hero and the contact section
CNAME                      the custom domain, for GitHub Pages
```

## Still to fill in

- **Project screenshots and the Dale Carnegie photo** — see above.
- **The résumé PDF still lists Town & Country Animal Rescue.** It has been
  removed from the site, but `Grant-Denney-Resume.pdf` (linked from the hero and
  the contact section) hasn't been regenerated, so the two disagree right now.
- **Real GA4 numbers.** The metric tiles in the "Search & analytics" section
  currently use only claims traceable to the résumé (page-one for category
  terms, thousands of inbound requests). There's a marked comment in
  `index.html` where actual GA4 figures should go once you pull them.

## Deploying

`CNAME` and `.nojekyll` are already in place for GitHub Pages on the apex domain
`grantdenney.com`. Publishing needs a GitHub repo and a DNS change at your
registrar — not done yet.
