# Client ads · live

A live Meta ads report per client, refreshed every 15 minutes by a GitHub Action.

Everything client-specific is encrypted: the Action writes AES-GCM files to the
`data` branch, and the page decrypts them in the browser with the key carried
after `#` in each link (never sent to any server). This repo holds no client
names or numbers in plain text.

Secrets: `META_TOKEN` (ads_read user token, renew every 60 days) and `CLIENTS`.
