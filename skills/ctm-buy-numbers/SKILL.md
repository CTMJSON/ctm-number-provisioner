---
name: ctm-buy-numbers
description: "Buy, purchase, or provision CTM tracking numbers and configure them - search available numbers, buy one or many, then attach a tracking source and a call route (receiving number, queue, voice menu, or agent). Use when the user asks to buy/purchase/provision tracking numbers, add numbers to an account, or set up routing for new numbers."
---

# Buy and configure CTM tracking numbers

You drive the `ctm-numbers` MCP server. It searches, buys, and configures
CallTrackingMetrics tracking numbers. Follow the flow below in order. **Never
buy a real (non-test) number, and never release a number, without the user
explicitly confirming.**

## 1. Confirm the account first

Call `whoami` and tell the user which account you will act on (id and name).
A token can map to a different account than its label suggests, and some CTM
clients silently fall back to a default account, so do not skip this. If the
account is wrong, stop and ask.

## 2. Find numbers to buy

Ask what they want:
- area code (e.g. 443), ZIP/address, area-code + prefix, toll-free, or international.

Then call `search_available_numbers` and show a compact table:

| # | Number | Region | Type | SMS |

Include the count and any overlay area codes. Then ask the user to choose:
- specific numbers from the table, or
- "any N in <area code>" (area-code mode — CTM picks).

## 3. Confirm the plan before buying

Call `buy_numbers(dry_run=True)` with the chosen numbers or `area_code` + `quantity`.
Show the returned plan (account name, count, numbers/area code, and the `test` flag).

**Default to `test=True`** unless the user says these are real. Then ask for
explicit confirmation ("Buy these 3 test numbers on <account name>? yes/no"). Only
after a clear yes, call `buy_numbers(..., dry_run=False)`.

Never call `dry_run=False` on the same turn you first show the plan.

## 4. Pick routing targets

Call `list_routing_targets` and let the user choose — do not choose for them.

1. **Tracking source** (optional): present the `sources` list, let them pick one.
   Use the `id` (TSO...) when calling `configure_numbers`.
2. **Route type**: ask which kind — receiving number, queue, voice menu, or agent.
3. **Specific target**: show that kind's list and let them pick one.

With more than four options, show a numbered list and have the user reply with a
number, or use `search` to narrow it down.

## 5. Name template

Ask for a label template. `{n}` becomes the 1-based index and `{number}` the
formatted phone number. Example: `"Google Ads {n}"`.

## 6. Configure

Call `configure_numbers` with the tpn_ids from the purchase, the chosen `name`,
`source_id`, and exactly one route (`receiving_number_ids` / `queue_id` /
`voice_menu_id` / `user_id`). Present the per-TPN step results and offer to retry
any failures.

## 7. Summarize

Finish with a final table: number | TPN | name | source | route.

## Cleanup

Only if the user explicitly asks: `release_numbers(tpn_ids, confirm=True)`.
This is destructive and irreversible; never do it on your own initiative.