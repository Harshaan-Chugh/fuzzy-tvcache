# Midpoint materials

| File | What it is |
| --- | --- |
| `midpoint-poster.pdf` | Poster, 36 in wide × 48 in tall. Print this. |
| `midpoint-poster.html` | Poster source. Open in Chrome to edit; re-export with the command below. |
| `midpoint-poster.png` | Preview image of the poster. |
| `team-guide.md` | Walkthrough of every component, the results and likely questions, so each of us can present any part. |
| `report.md` | Written midpoint report. |

Numbers on the poster and in the guide come from `docs/evaluation.md`. The SkyRL-SQL panel shows the
20-question pilot until the full run's replay is recorded there.

Re-export the PDF and preview after editing the HTML (macOS, Chrome):

```bash
C="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"$C" --headless=new --no-pdf-header-footer --virtual-time-budget=3000 --print-to-pdf="$PWD/midpoint/midpoint-poster.pdf" "file://$PWD/midpoint/midpoint-poster.html"
"$C" --headless=new --hide-scrollbars --window-size=1440,1920 --virtual-time-budget=3000 --screenshot="$PWD/midpoint/midpoint-poster.png" "file://$PWD/midpoint/midpoint-poster.html"
```
