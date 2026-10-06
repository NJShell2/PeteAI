# Pete AI: API Key Security

## How your key is stored

- On first run, Pete AI shows a setup modal with instructions and a link to [genai.rcac.purdue.edu](https://genai.rcac.purdue.edu/), where you generate your personal API key.
- You paste the key once and run a live connection test against Studio.
- Pete AI encrypts the key with **Windows DPAPI**, which ties decryption to your logged-in Windows account, and stores it in your per-user app data at `%LOCALAPPDATA%\PurduePeteAI`.
- A copy of that file is useless on another machine or under another user account.
- You never reenter the key.

## Key hygiene rules

1. **Keys are per person.** On a shared machine, each user enters their own key once.
2. **Treat the key like a password.** Never paste it into chat, email, tickets, or shared documents. Never commit it to a repo.
3. **If a key is ever exposed, rotate it.** Deleting a copied key is hygiene, not remediation: only revoking and regenerating the key at genai.rcac.purdue.edu invalidates a key someone else may already hold.
4. **No key ever ships with Pete AI.** Builds are assembled with an empty key store by design. If you find a key baked into any distributable, treat it as compromised and rotate it.

## What Pete AI connects to

Pete AI connects only to Purdue GenAI Studio. There are no other model-provider integrations.
