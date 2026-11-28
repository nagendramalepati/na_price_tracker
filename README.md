# Price Tracker

Tracks product prices on a schedule, keeps their price history in a dashboard, and sends a
Telegram alert when a price reaches your target. It runs on GitHub's servers, so your laptop
can stay closed.

## How it works

- **GitHub Actions** runs `price_tracker.py` every 30 minutes. GitHub's own cron schedule is
  unreliable (it often skips runs for hours), so each workflow run stays alive for ~5.5 hours,
  checks every 30 minutes itself, then starts the next run. Each product is checked only when
  its own `check_every_hours` is due, and it stops after `track_until`.
- To pause tracking, cancel the running "Track prices" workflow on the Actions tab (a cancelled
  run doesn't start the next one). Click "Run workflow" to start it again.
- Prices are saved to `docs/data.json`. The dashboard (`docs/index.html`) is served by
  **GitHub Pages**.
- **Telegram** sends you a message when a price drops to or below `target_price`.

## Adding a product

Edit `products.json`. You can do this on github.com, even from your phone, by opening the
file and clicking the ✏️ pencil icon. Each product is checked within 30 minutes of saving.

```json
{
  "name": "Sony headphones",
  "url": "https://www.example.com/product/123",
  "target_price": 19999,
  "check_every_hours": 6,
  "track_until": "2027-03-31",
  "notify_on_any_drop": false,
  "selector": ""
}
```

| Field | Meaning |
|---|---|
| `name` | Any label you like |
| `url` | Product page link |
| `target_price` | Alert when the price is at or below this |
| `check_every_hours` | How often to check this product (minimum 0.5 = 30 minutes) |
| `track_until` | Last day to track (YYYY-MM-DD). You get a summary message when it ends. |
| `notify_on_any_drop` | Optional. `true` also alerts on every price drop. |
| `notify_every_check` | Optional. `true` sends the latest price to Telegram after every successful check. |
| `currency_symbol` | Optional. Overrides the default in `settings`. |
| `selector` | Optional. Only needed if the price isn't found automatically (see below). |

### Gold and silver bars (for sites that block automated checks)

For bullion, the tracker can follow the live metal price instead of reading the shop page.
Add `metal` (`XAU` gold, `XAG` silver, `XPT` platinum, `XPD` palladium), `currency`,
`grams` and an optional `premium_percent` (the dealer's markup over spot):

```json
{
  "name": "ABC Gold Tael 375g",
  "url": "https://www.abcbullion.com/store/gold/product/gabgtael375g-abc-gold-tael-cast-bar",
  "metal": "XAU", "currency": "AUD", "grams": 375, "premium_percent": 0,
  "target_price": 6700.00, "check_every_hours": 0.5, "track_until": "2027-02-28"
}
```

The price comes from the free gold-api.com feed. The `url` is just kept as a link on the dashboard.

`settings.timezone` controls when `track_until` ends, e.g. `"Asia/Kolkata"` or `"America/New_York"`.

## One-time setup (about 15 minutes)

### 1. Create a Telegram bot
1. In Telegram, message **@BotFather** and send `/newbot`, then follow the prompts.
   It gives you a **bot token** (looks like `123456:ABC-...`).
2. Open your new bot and send it any message, e.g. "hi".
3. In a browser, open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and find
   `"chat":{"id": 123456789`. That number is your **chat ID**.

### 2. Put the project on GitHub
1. Create a free account at github.com, then click **New repository** and name it `price-tracker`.
   Choose **Public**, because free GitHub Pages requires a public repository. Your product
   list will be visible to anyone with the link, but your Telegram secrets stay private.
2. Upload this folder's contents, including the hidden `.github` folder. The simplest way is
   with git:
   ```bash
   cd "Track Pricing"
   git init && git add . && git commit -m "Price tracker"
   git branch -M main
   git remote add origin https://github.com/<your-username>/price-tracker.git
   git push -u origin main
   ```

### 3. Add your Telegram secrets
Go to the repo's **Settings → Secrets and variables → Actions → New repository secret** and add:
- `TELEGRAM_BOT_TOKEN` = your bot token
- `TELEGRAM_CHAT_ID` = your chat ID

### 4. Turn on the dashboard
Go to **Settings → Pages**. Under "Build and deployment", choose **Deploy from a branch**,
then select branch `main` and folder `/docs`, and click **Save**.
Your dashboard will be at `https://<your-username>.github.io/price-tracker/`.

### 5. Start it
Go to the **Actions** tab, enable workflows if GitHub asks, open **Track prices**, and click
**Run workflow**. After that it runs on its own every hour.

## When a price isn't found

Some shops hide the price or block automated checks. **Amazon and Flipkart often block
requests that come from cloud servers like GitHub's.** The dashboard shows "Check failed"
with the reason, and Telegram warns you after 3 failures in a row.

To fix "price not found", give the product a `selector`:
1. Open the product page in Chrome, right-click the price, and choose **Inspect**.
2. Note the element's tag and class, e.g. `<span class="product-price">`, which becomes
   the selector `span.product-price`.
3. Test it on your computer:
   ```bash
   pip install -r requirements.txt
   python price_tracker.py --check "<url>" --selector "span.product-price"
   ```

Other commands:
- `python price_tracker.py --check "<url>"` tests whether a link works without a selector
- `TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python price_tracker.py --test-telegram`
  sends you a test alert
