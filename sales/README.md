# Kevin setup sales page

This is a static, local-first validation page for the $35 managed Discord setup offer.
It does not collect or transmit form data, take payment, modify the bot, or require a
build step.

Preview it from the repository root:

```bash
python3 -m http.server 8080
```

Then open <http://localhost:8080/sales/>.

The intake form generates a request that the customer can copy and send to `7331.lol`
on Discord. Deploy only after the offer and contact details have been reviewed.
